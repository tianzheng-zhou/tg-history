"""ChatSession / ChatTurn CRUD 封装。

每个 session 是一次多轮对话容器，turns 按 seq 顺序保存用户问题与 assistant 回答。
assistant turn 附带完整 Agent trajectory 和 usage meta。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, desc, func, or_
from sqlalchemy.orm import Session

from backend.config import settings
from backend.models.database import Artifact, ArtifactVersion, ChatSession, ChatTurn


# ---------- utilities ----------

def _parse_json(s: str | None) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def _dump_json(obj: Any) -> str | None:
    if obj is None:
        return None
    return json.dumps(obj, ensure_ascii=False)


def _derive_title(question: str, max_len: int = 24) -> str:
    q = (question or "").strip().replace("\n", " ")
    if len(q) <= max_len:
        return q or "新对话"
    return q[:max_len] + "…"


def _derive_preview(text: str | None, max_len: int = 80) -> str:
    if not text:
        return ""
    s = text.strip().replace("\n", " ")
    if len(s) <= max_len:
        return s
    return s[:max_len] + "…"


def session_to_dict(s: ChatSession, artifact_count: int = 0) -> dict:
    return {
        "id": s.id,
        "title": s.title or "新对话",
        "mode": s.mode or "agent",
        "chat_ids": _parse_json(s.chat_ids),
        "pinned": bool(s.pinned),
        "archived": bool(s.archived),
        "turn_count": s.turn_count or 0,
        "artifact_count": artifact_count,
        "last_preview": s.last_preview,
        "created_at": s.created_at or datetime.utcnow(),
        "updated_at": s.updated_at or datetime.utcnow(),
    }


def count_artifacts(db: Session, session_id: str) -> int:
    """统计某 session 下的 artifact 数量（不含版本数）。"""
    return (
        db.query(func.count(Artifact.id))
        .filter(Artifact.session_id == session_id)
        .scalar()
        or 0
    )


def count_artifacts_bulk(db: Session, session_ids: list[str]) -> dict[str, int]:
    """批量统计多个 session 的 artifact 数量。返回 {session_id: count}，未出现的视为 0。"""
    if not session_ids:
        return {}
    rows = (
        db.query(Artifact.session_id, func.count(Artifact.id))
        .filter(Artifact.session_id.in_(session_ids))
        .group_by(Artifact.session_id)
        .all()
    )
    return {sid: cnt for sid, cnt in rows}


def turn_to_dict(t: ChatTurn) -> dict:
    return {
        "id": t.id,
        "seq": t.seq,
        "role": t.role,
        "content": t.content,
        "sources": _parse_json(t.sources),
        "trajectory": _parse_json(t.trajectory),
        "mode": t.mode,
        "meta": _parse_json(t.meta),
        "created_at": t.created_at or datetime.utcnow(),
    }


# ---------- CRUD ----------

def create_session(
    db: Session,
    title: str | None = None,
    mode: str = "agent",
    chat_ids: list[str] | None = None,
) -> ChatSession:
    sid = uuid.uuid4().hex
    now = datetime.utcnow()
    s = ChatSession(
        id=sid,
        title=title or "新对话",
        mode=mode,
        chat_ids=_dump_json(chat_ids) if chat_ids else None,
        pinned=False,
        archived=False,
        turn_count=0,
        last_preview=None,
        created_at=now,
        updated_at=now,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def get_session(db: Session, session_id: str) -> ChatSession | None:
    return db.query(ChatSession).filter(ChatSession.id == session_id).first()


def list_sessions(
    db: Session,
    archived: bool = False,
    pinned: bool | None = None,
    q: str | None = None,
    limit: int = 30,
    offset: int = 0,
) -> tuple[list[ChatSession], int]:
    """列出会话。按 pinned DESC, updated_at DESC 排序。

    q 搜索：对 title 或 任一 turn.content 做 LIKE 匹配。
    """
    query = db.query(ChatSession).filter(ChatSession.archived == archived)

    if pinned is not None:
        query = query.filter(ChatSession.pinned == pinned)

    if q:
        pattern = f"%{q}%"
        # 匹配 title 或 turns.content
        matched_session_ids = (
            db.query(ChatTurn.session_id)
            .filter(ChatTurn.content.like(pattern))
            .distinct()
            .subquery()
        )
        query = query.filter(
            or_(
                ChatSession.title.like(pattern),
                ChatSession.id.in_(matched_session_ids),
            )
        )

    total = query.count()
    items = (
        query.order_by(desc(ChatSession.pinned), desc(ChatSession.updated_at))
        .limit(limit)
        .offset(offset)
        .all()
    )
    return items, total


def update_session(
    db: Session,
    session_id: str,
    *,
    title: str | None = None,
    pinned: bool | None = None,
    archived: bool | None = None,
    mode: str | None = None,
    chat_ids: list[str] | None = None,
    bump_updated: bool = True,
) -> ChatSession | None:
    s = get_session(db, session_id)
    if not s:
        return None
    if title is not None:
        s.title = title
    if pinned is not None:
        s.pinned = pinned
    if archived is not None:
        s.archived = archived
    if mode is not None:
        s.mode = mode
    if chat_ids is not None:
        s.chat_ids = _dump_json(chat_ids) if chat_ids else None
    if bump_updated:
        s.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(s)
    return s


def delete_session(db: Session, session_id: str) -> bool:
    s = get_session(db, session_id)
    if not s:
        return False
    # 级联删除 artifacts + 版本（FK CASCADE 也会兜底，但 ORM 层显式删更稳）
    artifact_ids = [
        aid for (aid,) in db.query(Artifact.id).filter(Artifact.session_id == session_id).all()
    ]
    if artifact_ids:
        db.query(ArtifactVersion).filter(
            ArtifactVersion.artifact_id.in_(artifact_ids)
        ).delete(synchronize_session=False)
        db.query(Artifact).filter(Artifact.id.in_(artifact_ids)).delete(synchronize_session=False)
    # 级联删除 turns
    db.query(ChatTurn).filter(ChatTurn.session_id == session_id).delete()
    db.delete(s)
    db.commit()
    return True


# ---------- Turn ops ----------

def get_turns(db: Session, session_id: str) -> list[ChatTurn]:
    return (
        db.query(ChatTurn)
        .filter(ChatTurn.session_id == session_id)
        .order_by(ChatTurn.seq)
        .all()
    )


def _next_seq(db: Session, session_id: str) -> int:
    max_seq = (
        db.query(func.max(ChatTurn.seq))
        .filter(ChatTurn.session_id == session_id)
        .scalar()
    )
    return (max_seq + 1) if max_seq is not None else 0


def append_turn(
    db: Session,
    session_id: str,
    role: str,
    content: str | None,
    sources: list | None = None,
    trajectory: dict | None = None,
    mode: str | None = None,
    meta: dict | None = None,
) -> ChatTurn:
    """追加一条 turn，并更新 session 的 turn_count / last_preview / updated_at。"""
    seq = _next_seq(db, session_id)
    now = datetime.utcnow()
    t = ChatTurn(
        session_id=session_id,
        seq=seq,
        role=role,
        content=content,
        sources=_dump_json(sources) if sources else None,
        trajectory=_dump_json(trajectory) if trajectory else None,
        mode=mode,
        meta=_dump_json(meta) if meta else None,
        created_at=now,
    )
    db.add(t)

    # 更新 session 汇总字段
    s = get_session(db, session_id)
    if s:
        s.turn_count = (s.turn_count or 0) + 1
        s.last_preview = _derive_preview(content)
        s.updated_at = now

    db.commit()
    db.refresh(t)
    return t


def _user_content_with_prefix(turn: ChatTurn) -> str | None:
    """重建 user message 的实际 LLM content：injected_prefix + 原始 question。

    历史重放时必须**和当时提交给 LLM 的 user message 完全一致**，否则前缀缓存失效。
    """
    if not turn.content:
        return None
    meta = _parse_json(turn.meta) or {}
    prefix = meta.get("injected_prefix")
    if prefix:
        return f"{prefix}\n\n---\n\n{turn.content}"
    return turn.content


def _has_full_tool_outputs(trajectory: dict | None) -> bool:
    """trajectory 是否包含完整 tool_results（新版 trajectory）。

    判断条件：任意 step 的任意 tool_call 含非空 ``output`` 字段。
    老 session（改造前生成的）只有 ``preview``，这里返回 False，调用方走 fallback。
    """
    if not isinstance(trajectory, dict):
        return False
    for s in trajectory.get("steps") or []:
        for tc in s.get("tool_calls") or []:
            if tc.get("output"):
                return True
    return False


def _replay_assistant_turn_full(turn: ChatTurn) -> list[dict]:
    """把一个 assistant turn 还原成完整 OpenAI messages 序列。

    每个 trajectory step → 1 条 assistant message（含 content + reasoning_content + tool_calls）
    + N 条 tool messages（每条对应该 step 里的一个 tool_call）。

    最后一个 step 如果没有 tool_calls，它就是"最终答案"那一步：
      - 优先用 ``turn.content``（数据库里持久化的纯净答案）作为 assistant.content
      - fallback 用 step.thinking（trajectory 里累积的 thinking_delta）

    Kimi 多步要求保留 ``reasoning_content`` —— 有就带上，OpenAI/Qwen 也会忽略未知字段。
    """
    trajectory = _parse_json(turn.trajectory) or {}
    steps = trajectory.get("steps") or []
    if not steps:
        # 没有 trajectory steps（比如 RAG 模式）→ 退回单条 assistant content
        if turn.content:
            return [{"role": "assistant", "content": turn.content}]
        return []

    result: list[dict] = []
    for i, step in enumerate(steps):
        is_last = i == len(steps) - 1
        tool_calls_raw = step.get("tool_calls") or []
        thinking = step.get("thinking") or ""
        reasoning = step.get("reasoning") or ""

        asst: dict = {"role": "assistant"}

        # content：最后一步且无 tool_calls 时优先用 turn.content（最终答案的纯净版）
        if is_last and not tool_calls_raw:
            asst["content"] = turn.content or thinking or None
        else:
            asst["content"] = thinking or None

        # Kimi 思考链 —— 多步工具调用时必须保留
        if reasoning:
            asst["reasoning_content"] = reasoning

        # tool_calls 还原
        if tool_calls_raw:
            asst["tool_calls"] = []
            for j, tc in enumerate(tool_calls_raw):
                tc_id = tc.get("id") or f"call_{turn.id}_{i}_{j}"
                args = tc.get("args") or {}
                asst["tool_calls"].append({
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": tc.get("name") or "",
                        "arguments": json.dumps(args, ensure_ascii=False)
                                       if not isinstance(args, str) else args,
                    },
                })

        # 跳过完全空的 assistant message（没 content 也没 tool_calls）
        if asst.get("content") is None and not asst.get("tool_calls"):
            continue

        result.append(asst)

        # 紧跟 tool messages（顺序必须和 assistant.tool_calls 完全一致）
        if tool_calls_raw:
            for j, tc in enumerate(tool_calls_raw):
                tc_id = tc.get("id") or f"call_{turn.id}_{i}_{j}"
                output = tc.get("output")
                if not output:
                    # 兜底：用 preview 序列化（虽然信息有损，但至少 tool_call_id 配对成功
                    # 不会让 OpenAI API 报 "tool message must follow tool_calls" 错）
                    preview = tc.get("preview") or {"note": "no full output recorded"}
                    output = json.dumps(preview, ensure_ascii=False)
                result.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": output,
                })

    return result


def get_history_messages(db: Session, session_id: str) -> list[dict]:
    """返回给 agent 用的对话历史（不含当前正在生成的 turn）。

    两种模式（由 ``settings.enable_full_history_replay`` 控制）：

    1. **完整重放**（默认开启，Claude Code 风格）：
       从 ``ChatTurn.trajectory`` 还原每轮的完整 OpenAI messages 序列——
       assistant message 带 ``tool_calls``、紧跟 N 条 ``tool`` 角色消息（带完整 output）。
       Agent 第二轮开始能"看到"前几轮调用了哪些工具、找到了哪些 message_id。
       老 session（trajectory 没有 ``output`` 字段）自动 fallback 到模式 2。

    2. **仅文本回放**（旧行为，feature flag 关闭时）：
       只回放 user.content + assistant.content。

    **前缀缓存关键（两种模式共有）**：user turn 的 content 是纯净问题，
    LLM 看到的实际是 ``meta.injected_prefix + content``（时间戳 + artifact 快照）。
    这里重新拼出 prefix+content，保证和上一轮提交给 LLM 的 user message 完全一致，
    从而显式缓存能跨轮命中。
    """
    turns = get_turns(db, session_id)
    full_replay = settings.enable_full_history_replay

    result: list[dict] = []
    for t in turns:
        if t.role == "user":
            content = _user_content_with_prefix(t)
            if content:
                result.append({"role": "user", "content": content})
            continue

        if t.role != "assistant":
            continue

        trajectory = _parse_json(t.trajectory)
        if full_replay and _has_full_tool_outputs(trajectory):
            result.extend(_replay_assistant_turn_full(t))
        else:
            # 老 session / RAG turn / feature flag 关闭 → 单条 content
            if t.content:
                result.append({"role": "assistant", "content": t.content})

    return result


def ensure_session_for_question(
    db: Session,
    session_id: str | None,
    question: str,
    mode: str = "agent",
    chat_ids: list[str] | None = None,
) -> ChatSession:
    """若 session_id 存在则取出；否则新建并用 question 派生 title。"""
    if session_id:
        s = get_session(db, session_id)
        if s:
            return s
    title = _derive_title(question)
    return create_session(db, title=title, mode=mode, chat_ids=chat_ids)
