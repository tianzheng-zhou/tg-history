"""Run 注册表：把 agent / rag 执行从 HTTP 请求中解耦。

每次提问创建一个 `Run`（带事件 buffer + 订阅者队列 + asyncio.Task），
后台任务负责跑 agent、fan-out 事件给所有订阅者、完成时持久化 assistant turn。
HTTP 层仅负责 `POST /api/ask/*` 启动 run 和 `GET /api/runs/{id}/events` 订阅。

切页面/刷新浏览器都不影响 run；进入时按 `last_event_id` 续播未见事件。
"""

from __future__ import annotations

import asyncio
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator

from backend.models.database import SessionLocal
from backend.services import session_service
from backend.services.qa_agent import (
    _build_artifact_summary,
    build_current_time_hint,
    run_agent,
)
from backend.services.rag_engine import answer_question_stream


# ---------- dataclass ----------

@dataclass
class Run:
    id: str
    session_id: str
    mode: str  # "agent" | "rag"
    question: str
    chat_ids: list[str] | None = None
    date_range: list[str] | None = None
    sender: str | None = None

    status: str = "pending"  # pending | running | completed | aborted | failed
    events: list[dict] = field(default_factory=list)
    seq: int = 0
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    task: asyncio.Task | None = None

    started_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None

    final_answer: str = ""
    final_sources: list = field(default_factory=list)
    final_usage: dict | None = None
    final_task_usage: dict | None = None
    error: str | None = None

    # tool_call_id → 完整 tool_result JSON 字符串。不走 run.events 避免给 SSE 订阅者
    # 反复推送 50KB+ 大负载；build_trajectory 时合并进 tool_calls[i].output 持久化。
    tool_outputs: dict = field(default_factory=dict)


# ---------- Registry ----------

class RunRegistry:
    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}
        self._session_active: dict[str, str] = {}  # session_id → current run_id
        self._lock = asyncio.Lock()

    # --- mutation ---

    async def start(
        self,
        session_id: str,
        question: str,
        mode: str = "agent",
        chat_ids: list[str] | None = None,
        date_range: list[str] | None = None,
        sender: str | None = None,
    ) -> tuple[str, bool]:
        """启动 run。返回 (run_id, already_running)。

        同一 session 若已有 pending/running run，返回该 run_id 并 already_running=True。
        """
        async with self._lock:
            existing_rid = self._session_active.get(session_id)
            if existing_rid:
                existing = self._runs.get(existing_rid)
                if existing and existing.status in ("pending", "running"):
                    return existing_rid, True

            run_id = uuid.uuid4().hex
            run = Run(
                id=run_id,
                session_id=session_id,
                mode=mode,
                question=question,
                chat_ids=chat_ids,
                date_range=date_range,
                sender=sender,
                started_at=datetime.utcnow(),
            )
            self._runs[run_id] = run
            self._session_active[session_id] = run_id

        run.task = asyncio.create_task(_run_worker(run))
        return run_id, False

    async def abort(self, run_id: str) -> bool:
        run = self._runs.get(run_id)
        if not run:
            return False
        if run.task and not run.task.done():
            run.task.cancel()
        return True

    # --- query ---

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def list_active(self, session_id: str | None = None) -> list[Run]:
        out: list[Run] = []
        for r in self._runs.values():
            if r.status not in ("pending", "running"):
                continue
            if session_id and r.session_id != session_id:
                continue
            out.append(r)
        return out

    def get_active_for_session(self, session_id: str) -> Run | None:
        rid = self._session_active.get(session_id)
        if not rid:
            return None
        run = self._runs.get(rid)
        if not run or run.status not in ("pending", "running"):
            return None
        return run

    # --- subscription ---

    async def subscribe(
        self, run_id: str, last_event_id: int = -1
    ) -> AsyncIterator[dict]:
        """订阅 run 的事件流。

        1. 先回放 buffer 中 seq > last_event_id 的事件；
        2. 若 run 已结束，发 sentinel 后退出；
        3. 否则挂订阅队列等待后续事件，直到收到 sentinel。
        """
        run = self._runs.get(run_id)
        if not run:
            return

        # 1. replay
        for ev in list(run.events):
            if ev.get("seq", -1) > last_event_id:
                yield ev

        # 2. terminal states
        if run.status not in ("pending", "running"):
            yield {"type": "__end__", "status": run.status}
            return

        # 3. live subscription
        queue: asyncio.Queue = asyncio.Queue()
        run.subscribers.add(queue)
        try:
            while True:
                ev = await queue.get()
                yield ev
                if ev.get("type") == "__end__":
                    break
        finally:
            run.subscribers.discard(queue)

    # --- cleanup ---

    async def cleanup_expired(self, ttl_seconds: int = 300) -> int:
        """删除已完成且超过 ttl 的 run。返回清理数量。"""
        now = datetime.utcnow()
        to_delete: list[str] = []
        for run_id, run in self._runs.items():
            if run.status in ("pending", "running"):
                continue
            if run.completed_at is None:
                continue
            age = (now - run.completed_at).total_seconds()
            if age >= ttl_seconds:
                to_delete.append(run_id)
        for rid in to_delete:
            run = self._runs.pop(rid, None)
            if run and self._session_active.get(run.session_id) == rid:
                self._session_active.pop(run.session_id, None)
        return len(to_delete)


# 模块级单例
registry = RunRegistry()


# ---------- event emit ----------

def _emit(run: Run, event: dict) -> None:
    """给事件打 seq、追加到 buffer、广播给所有订阅者。

    特殊处理：tool_result 事件里的 ``output_full`` 字段（可能 50KB 量级）
    不走 run.events 也不推给 SSE 订阅者，只存到 ``run.tool_outputs[tool_call_id]``，
    由 ``_build_trajectory`` 最后合并进 tool_calls[i].output。
    这样可以：
      - 避免订阅者反复拿到大重复 payload（前端只需 output_preview）。
      - 避免 run.events buffer 随调用次数线性增长。
    """
    if event.get("type") == "tool_result" and "output_full" in event:
        full = event.pop("output_full")
        tc_id = event.get("id")
        if tc_id and isinstance(full, str):
            run.tool_outputs[tc_id] = full

    event["seq"] = run.seq
    run.seq += 1
    run.events.append(event)
    for q in list(run.subscribers):
        try:
            q.put_nowait(event)
        except Exception:
            # 订阅者队列异常不影响 run 本身
            pass


# ---------- trajectory 构造 ----------

def _build_trajectory(run: Run) -> dict:
    """从 run.events + run.tool_outputs 构造紧凑 trajectory JSON。

    输出结构跟原来一致，额外给每个 tool_calls[i] 打上 ``output``（完整 JSON 字符串）。
    ``output`` 会被 session_service.get_history_messages 读出来还原成完整的 tool 角色 message。
    """
    steps_by_idx: dict[int, dict] = {}

    def _ensure(step: int) -> dict:
        s = steps_by_idx.get(step)
        if s is None:
            s = {
                "step": step,
                "thinking": "",
                "reasoning": "",
                "tool_calls": [],
                "had_tool_calls": False,
            }
            steps_by_idx[step] = s
        return s

    for ev in run.events:
        t = ev.get("type")
        step = ev.get("step")
        if t == "step_start" and step is not None:
            _ensure(step)
        elif t == "thinking_delta" and step is not None:
            _ensure(step)["thinking"] += ev.get("text", "")
        elif t == "reasoning_delta" and step is not None:
            _ensure(step)["reasoning"] += ev.get("text", "")
        elif t == "tool_call" and step is not None:
            s = _ensure(step)
            s["had_tool_calls"] = True
            s["tool_calls"].append({
                "id": ev.get("id"),
                "name": ev.get("name"),
                "args": ev.get("args"),
            })
        elif t == "tool_result" and step is not None:
            s = steps_by_idx.get(step)
            if s:
                for tc in s["tool_calls"]:
                    if tc.get("id") == ev.get("id"):
                        tc["preview"] = ev.get("output_preview")
                        tc["duration_ms"] = ev.get("duration_ms")
                        tc["error"] = ev.get("error", False)
                        break
        elif t == "step_done" and step is not None:
            s = _ensure(step)
            if ev.get("had_tool_calls") is not None:
                s["had_tool_calls"] = bool(ev.get("had_tool_calls"))

    # 截断超长文本防 DB 爆炸
    MAX_CHARS = 20000
    for s in steps_by_idx.values():
        if len(s["thinking"]) > MAX_CHARS:
            s["thinking"] = s["thinking"][:MAX_CHARS] + "\n...[truncated]"
        if len(s["reasoning"]) > MAX_CHARS:
            s["reasoning"] = s["reasoning"][:MAX_CHARS] + "\n...[truncated]"

    # 把 run.tool_outputs 合并进每个 step 的 tool_calls[i].output。
    # _truncate_tool_output 已经保证每条 ≤ MAX_TOOL_OUTPUT_CHARS（50000），这里不再截。
    for s in steps_by_idx.values():
        for tc in s.get("tool_calls", []):
            tc_id = tc.get("id")
            if tc_id and tc_id in run.tool_outputs:
                tc["output"] = run.tool_outputs[tc_id]

    # RAG 模式的额外事件（status/search_result/rerank/context）
    rag_events = []
    for ev in run.events:
        if ev.get("type") in ("status", "search_result", "rerank", "context"):
            rag_events.append({k: v for k, v in ev.items() if k != "seq"})

    out: dict = {
        "steps": [steps_by_idx[k] for k in sorted(steps_by_idx.keys())],
    }
    if rag_events:
        out["rag_events"] = rag_events
    return out


# ---------- worker ----------

async def _run_worker(run: Run) -> None:
    """后台任务：跑 agent / rag，fan-out 事件，完成后持久化 assistant turn。"""
    db = SessionLocal()
    try:
        run.status = "running"

        # 构造 **agent 模式** 的注入前缀（时间戳 + 当前 artifacts 快照）。
        # 这段前缀**不落到 ChatTurn.content**（content 保持干净的原始 question），
        # 而是存到 meta["injected_prefix"] —— LLM 看到的 user content 会由
        # `get_history_messages` 在重放时用 prefix+content 拼出。
        # 这样：
        #   1. 前端 / 搜索 / 导出 / 标题生成 都用 content（纯净的用户问题）
        #   2. LLM 历史重放时拿到 `prefix+content`，和上次传给 LLM 的一致 → 前缀缓存命中
        #   3. artifact 摘要是"当时"的快照，事后不变 → 缓存稳定
        injected_prefix = ""
        if run.mode == "agent":
            parts: list[str] = [build_current_time_hint()]
            artifact_summary = _build_artifact_summary(db, run.session_id)
            if artifact_summary:
                parts.append(artifact_summary)
            injected_prefix = "\n\n---\n\n".join(parts)

        # 读取 session 现有 history（含历史的 injected_prefix）——此时 user turn 还未 append
        history = session_service.get_history_messages(db, run.session_id)

        # 先把 user turn 落库，这样前端即使中途断开也能看到自己的问题
        try:
            session_service.append_turn(
                db,
                run.session_id,
                role="user",
                content=run.question,  # 纯净的用户问题
                mode=run.mode,
                meta={
                    "run_id": run.id,
                    **({"injected_prefix": injected_prefix} if injected_prefix else {}),
                },
            )
        except Exception:
            traceback.print_exc()
            db.rollback()

        # 当前轮传给 LLM 的 user content = 前缀 + 原始 question
        augmented_user_content = (
            f"{injected_prefix}\n\n---\n\n{run.question}" if injected_prefix else run.question
        )

        if run.mode == "rag":
            async for ev in answer_question_stream(
                db=db,
                question=run.question,
                chat_ids=run.chat_ids,
                date_range=run.date_range,
                sender=run.sender,
            ):
                _emit(run, ev)
                t = ev.get("type")
                if t == "done":
                    run.final_answer = ev.get("answer", "") or ""
                    run.final_sources = ev.get("sources", []) or []
                elif t == "usage":
                    run.final_usage = {
                        k: v for k, v in ev.items()
                        if k not in ("type", "seq", "step")
                    }
        else:  # agent
            async for ev in run_agent(
                db=db,
                question=augmented_user_content,
                chat_ids=run.chat_ids,
                history=history if history else None,
                session_id=run.session_id,
            ):
                _emit(run, ev)
                t = ev.get("type")
                if t == "final_answer":
                    run.final_answer = ev.get("answer", "") or ""
                    run.final_sources = ev.get("sources", []) or []
                    if ev.get("task_usage"):
                        run.final_task_usage = ev["task_usage"]
                elif t == "usage":
                    run.final_usage = {
                        k: v for k, v in ev.items()
                        if k not in ("type", "seq", "step")
                    }

        run.status = "completed"

    except asyncio.CancelledError:
        run.status = "aborted"
        # 不再 raise —— 让 finally 把 assistant turn 保存完整

    except Exception as e:
        traceback.print_exc()
        run.status = "failed"
        run.error = str(e)
        _emit(run, {"type": "error", "error": str(e)})

    finally:
        run.completed_at = datetime.utcnow()

        # 持久化 assistant turn（包含 trajectory + usage）
        trajectory = _build_trajectory(run)
        meta: dict = {"run_id": run.id}
        if run.final_usage:
            meta["usage"] = run.final_usage
        if run.final_task_usage:
            meta["task_usage"] = run.final_task_usage
        if run.status == "aborted":
            meta["aborted"] = True
        elif run.status == "failed":
            meta["failed"] = True
            if run.error:
                meta["error"] = run.error

        has_any_content = bool(run.final_answer) or bool(trajectory.get("steps")) or bool(trajectory.get("rag_events"))
        if has_any_content:
            try:
                session_service.append_turn(
                    db,
                    run.session_id,
                    role="assistant",
                    content=run.final_answer or "",
                    sources=run.final_sources or None,
                    trajectory=trajectory or None,
                    mode=run.mode,
                    meta=meta,
                )
            except Exception:
                traceback.print_exc()
                db.rollback()

        # 广播 sentinel 告诉所有订阅者流结束
        sentinel = {"type": "__end__", "status": run.status}
        sentinel["seq"] = run.seq
        run.seq += 1
        run.events.append(sentinel)
        for q in list(run.subscribers):
            try:
                q.put_nowait(sentinel)
            except Exception:
                pass

        try:
            db.close()
        except Exception:
            pass


# ---------- periodic cleanup ----------

async def periodic_cleanup(interval_seconds: int = 60) -> None:
    """后台协程：每隔 interval_seconds 秒清理一次过期 run。"""
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await registry.cleanup_expired()
        except asyncio.CancelledError:
            break
        except Exception:
            traceback.print_exc()
