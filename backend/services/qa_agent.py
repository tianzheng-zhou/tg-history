"""QA Agent 主循环：LLM + Tool Calling + 流式输出

参考 opencode 的架构：LLM 在循环中自主调用工具直到给出最终答案。
每轮 LLM 可以：
  1. 调用一个或多个工具（tool_calls）
  2. 输出普通文本（thinking/explanation）
  3. 输出最终答案（无 tool_calls 时循环结束）

事件类型（yield 出去给上层 SSE 使用）：
  - status: 阶段状态
  - thinking_delta: LLM 流式 token（文本思考/回答）
  - tool_call: 工具调用开始（name + args）
  - tool_result: 工具返回结果（截断预览）
  - step_done: 一轮 LLM 完成（usage 统计）
  - final_answer: 最终答案 + 来源
  - error: 错误
"""

from __future__ import annotations

import json
import time
from typing import AsyncIterator

from sqlalchemy.orm import Session

from backend.config import settings
from backend.models.database import Message
from backend.services import llm_adapter
from backend.services.qa_tools import TOOL_SCHEMAS, dispatch_tool

MAX_STEPS = 30  # 最大 agent 迭代轮数，防止死循环
MAX_TOOL_OUTPUT_CHARS = 120000  # 塞回 LLM 的单次工具输出最大字符数（qwen3.6-plus 1M 上下文）


SYSTEM_PROMPT = """你是一个专业的 Telegram 聊天记录分析助手。用户可能会问各种关于群聊记录的问题：技术讨论、商业信息、某人的观点、某事件的来龙去脉等。

你被授予一组检索工具来查询聊天数据库。你的工作流程应当：

1. **理解问题**：先思考用户真正想知道什么；如不确定数据范围，可用 list_chats 了解可用群聊
2. **制定检索计划**：复杂问题应拆成多步，每步用合适的工具
3. **执行工具**：
   - **semantic_search** 是首选——用自然语言描述你要找什么
   - **检索数量要够**：调研型问题（梳理/统计/对比）建议 top_k=80~150；精确事实型问题 top_k=15~30
   - **keyword_search** 用于精确关键词（型号、URL、代码等）
   - 结果不够充分时调整查询再搜一次，或换工具
   - 找到相关的 message_ids / topic_id 后用 fetch_messages / fetch_topic_context 读完整内容
4. **综合答案**：基于工具返回的真实证据回答，**引用具体发言人和日期**

关键原则：
- 如果信息不足，大方承认"根据现有记录未找到"，不要编造
- 简短问题可以一次搜索就回答，复杂问题多次迭代
- **宁可多检索一些再筛选，也不要因为样本太少而遗漏关键信息**
- 最终答案用 Markdown 格式，**标注来源**（发言人 + 日期）

你最多可以进行 {max_steps} 轮工具调用。请高效完成任务。
""".format(max_steps=MAX_STEPS)


def _truncate_tool_output(obj: dict) -> str:
    """把工具输出 dict 转为 JSON 字符串，过长时截断"""
    s = json.dumps(obj, ensure_ascii=False)
    if len(s) > MAX_TOOL_OUTPUT_CHARS:
        s = s[:MAX_TOOL_OUTPUT_CHARS] + f"\n\n...[截断，完整长度 {len(s)} 字符]"
    return s


async def _stream_llm_step(
    messages: list[dict],
) -> AsyncIterator[dict]:
    """单轮 LLM 流式调用，yield：
       - {"type": "text_delta", "text": str}
       - {"type": "reasoning_delta", "text": str}  (Kimi 思考链)
       - {"type": "tool_calls", "calls": [{"id", "name", "arguments"}]}  (完整聚合后)
       - {"type": "reasoning_content", "text": str}  (完整思考内容，供回传上下文)
       - {"type": "done"}
    """
    model = settings.llm_model_qa
    client = llm_adapter.get_client_for_model(model)
    is_kimi = llm_adapter.is_kimi_model(model)

    kwargs = dict(
        model=model,
        messages=messages,
        tools=TOOL_SCHEMAS,
        tool_choice="auto",
        max_tokens=32768 if is_kimi else 2048,
        stream=True,
    )

    if is_kimi:
        # Kimi: 思考模式 + tool_choice 只能 auto/none
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        kwargs["temperature"] = 1.0
        # Kimi 不支持 parallel_tool_calls 参数
    else:
        kwargs["parallel_tool_calls"] = True
        kwargs["temperature"] = 0.3

    stream = await client.chat.completions.create(**kwargs)

    # 需要把 streaming tool_calls 聚合
    tool_calls_acc: dict[int, dict] = {}
    reasoning_parts: list[str] = []

    async for chunk in stream:
        choice = chunk.choices[0] if chunk.choices else None
        if not choice:
            continue
        delta = choice.delta

        # Kimi reasoning_content（思考链 token）
        rc = getattr(delta, "reasoning_content", None)
        if rc:
            reasoning_parts.append(rc)
            yield {"type": "reasoning_delta", "text": rc}

        # 文本 token
        if delta.content:
            yield {"type": "text_delta", "text": delta.content}

        # 工具调用分片聚合
        if delta.tool_calls:
            for tc in delta.tool_calls:
                idx = tc.index
                if idx not in tool_calls_acc:
                    tool_calls_acc[idx] = {
                        "id": tc.id or "",
                        "name": "",
                        "arguments": "",
                    }
                slot = tool_calls_acc[idx]
                if tc.id:
                    slot["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        slot["name"] += tc.function.name
                    if tc.function.arguments:
                        slot["arguments"] += tc.function.arguments

        if choice.finish_reason:
            break

    # 聚合完成，yield 完整 tool_calls
    if tool_calls_acc:
        calls = []
        for idx in sorted(tool_calls_acc.keys()):
            c = tool_calls_acc[idx]
            if c["name"]:
                calls.append(c)
        if calls:
            yield {"type": "tool_calls", "calls": calls}

    # Kimi 要求多步工具调用时 assistant message 必须保留 reasoning_content
    if reasoning_parts:
        yield {"type": "reasoning_content", "text": "".join(reasoning_parts)}

    yield {"type": "done"}


async def run_agent(
    db: Session,
    question: str,
    chat_ids: list[str] | None = None,
    history: list[dict] | None = None,
) -> AsyncIterator[dict]:
    """Agent 主循环，yield 事件给上层"""
    # 构造 user 消息；如果指定了 chat_ids，注入上下文
    user_content = question
    if chat_ids:
        user_content += f"\n\n[用户限定只在这些群聊 chat_id 中检索: {json.dumps(chat_ids, ensure_ascii=False)}]"

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]

    # 注入多轮对话历史（只保留 user/assistant 的文本，不含工具调用细节）
    if history:
        for h in history:
            role = h.get("role") if isinstance(h, dict) else h.role
            content = h.get("content") if isinstance(h, dict) else h.content
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": user_content})

    yield {"type": "status", "message": f"Agent 启动（最多 {MAX_STEPS} 步）"}

    # 收集引用的消息 ID，最后用来生成 sources
    cited_message_ids: set[int] = set()
    final_text_parts: list[str] = []

    for step in range(1, MAX_STEPS + 1):
        yield {"type": "step_start", "step": step}

        # 本轮要收集的文本 & tool_calls & 思考链
        step_text = ""
        step_tool_calls: list[dict] = []
        step_reasoning = ""  # Kimi reasoning_content

        try:
            async for ev in _stream_llm_step(messages):
                if ev["type"] == "text_delta":
                    step_text += ev["text"]
                    yield {"type": "thinking_delta", "step": step, "text": ev["text"]}
                elif ev["type"] == "reasoning_delta":
                    yield {"type": "reasoning_delta", "step": step, "text": ev["text"]}
                elif ev["type"] == "reasoning_content":
                    step_reasoning = ev["text"]
                elif ev["type"] == "tool_calls":
                    step_tool_calls = ev["calls"]
                elif ev["type"] == "done":
                    break
        except Exception as e:
            yield {"type": "error", "error": f"LLM 调用失败: {e}"}
            return

        # 把本轮 assistant 消息加回去
        assistant_msg: dict = {"role": "assistant"}
        if step_text:
            assistant_msg["content"] = step_text
        else:
            assistant_msg["content"] = None
        # Kimi 要求多步工具调用时 assistant message 保留 reasoning_content
        if step_reasoning:
            assistant_msg["reasoning_content"] = step_reasoning
        if step_tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {"name": c["name"], "arguments": c["arguments"]},
                }
                for c in step_tool_calls
            ]
        messages.append(assistant_msg)

        # 没有 tool_calls —— LLM 给出最终答案，循环结束
        if not step_tool_calls:
            final_text_parts.append(step_text)
            yield {"type": "step_done", "step": step, "had_tool_calls": False}
            break

        # 有 tool_calls —— 执行工具并把结果喂回去
        yield {"type": "step_done", "step": step, "had_tool_calls": True,
               "tool_count": len(step_tool_calls)}

        for call in step_tool_calls:
            name = call["name"]
            args_str = call["arguments"] or "{}"
            try:
                args = json.loads(args_str) if args_str else {}
            except json.JSONDecodeError as e:
                args = {}
                err_result = {"error": f"参数 JSON 解析失败: {e}", "raw": args_str[:200]}
                yield {
                    "type": "tool_call",
                    "step": step,
                    "id": call["id"],
                    "name": name,
                    "args": {},
                    "args_raw": args_str,
                }
                yield {
                    "type": "tool_result",
                    "step": step,
                    "id": call["id"],
                    "name": name,
                    "output_preview": err_result,
                    "duration_ms": 0,
                    "error": True,
                }
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(err_result, ensure_ascii=False),
                })
                continue

            yield {
                "type": "tool_call",
                "step": step,
                "id": call["id"],
                "name": name,
                "args": args,
            }

            t0 = time.time()
            result = await dispatch_tool(db, name, args)
            duration_ms = int((time.time() - t0) * 1000)

            # 收集消息 ID 用于后续引用
            _collect_ids(result, cited_message_ids)

            # 截断过长输出
            result_str = _truncate_tool_output(result)

            yield {
                "type": "tool_result",
                "step": step,
                "id": call["id"],
                "name": name,
                "output_preview": _make_preview(result),
                "duration_ms": duration_ms,
                "error": "error" in result,
            }

            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": result_str,
            })

    else:
        # 走到 MAX_STEPS 仍在调用工具——强制让 LLM 基于已有信息总结答案（禁用工具）
        yield {"type": "status", "message": f"已达最大步数 {MAX_STEPS}，强制总结..."}
        messages.append({
            "role": "user",
            "content": "你已经达到最大工具调用次数。请基于上面已经收集到的所有工具结果，给出最终答案。"
                       "**不要再调用任何工具**。如果信息不足，明确说明'根据已检索信息无法完整回答'，"
                       "并总结已找到的相关内容。",
        })
        try:
            _model = settings.llm_model_qa
            _client = llm_adapter.get_client_for_model(_model)
            _force_kwargs = dict(
                model=_model,
                messages=messages,
                max_tokens=32768 if llm_adapter.is_kimi_model(_model) else 2048,
                stream=True,
            )
            if llm_adapter.is_kimi_model(_model):
                _force_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
                _force_kwargs["temperature"] = 0.6
            else:
                _force_kwargs["temperature"] = 0.3
            stream = await _client.chat.completions.create(**_force_kwargs)
            forced_text = ""
            async for chunk in stream:
                choice = chunk.choices[0] if chunk.choices else None
                if choice and choice.delta.content:
                    forced_text += choice.delta.content
                    yield {"type": "thinking_delta", "step": MAX_STEPS + 1, "text": choice.delta.content}
            final_text_parts.append(forced_text)
        except Exception as e:
            yield {"type": "error", "error": f"强制总结失败: {e}"}

    # 构造 sources（从引用过的消息中选前 5 条，按话题去重）
    sources = _build_sources(db, cited_message_ids)

    yield {
        "type": "final_answer",
        "answer": "".join(final_text_parts),
        "sources": sources,
    }


def _collect_ids(result: dict, acc: set[int]) -> None:
    """从工具结果中提取所有 message_id，用来构建最终引用"""
    if not isinstance(result, dict):
        return
    # 直接的 messages 列表
    for key in ("messages", "results"):
        items = result.get(key)
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    mid = it.get("message_id")
                    if isinstance(mid, int):
                        acc.add(mid)
                    # semantic_search 返回的 message_ids 列表
                    mids = it.get("message_ids")
                    if isinstance(mids, list):
                        for x in mids:
                            if isinstance(x, int):
                                acc.add(x)


def _make_preview(result: dict) -> dict:
    """为前端生成紧凑的工具结果预览"""
    if not isinstance(result, dict):
        return {"value": str(result)[:200]}

    if "error" in result:
        return {"error": result["error"]}

    # 根据不同工具返回，生成简洁摘要
    preview: dict = {}
    if "count" in result:
        preview["count"] = result["count"]
    if "chats" in result and isinstance(result["chats"], list):
        preview["count"] = len(result["chats"])
        preview["summary"] = f"{len(result['chats'])} 个群聊"
    if "results" in result and isinstance(result["results"], list):
        items = result["results"]
        preview["items"] = [
            {
                "text": (it.get("chunk_preview") or it.get("text") or "")[:100],
                "sender": it.get("sender"),
                "date": it.get("date") or it.get("start_date"),
                "distance": it.get("distance"),
            }
            for it in items[:3]
        ]
    if "messages" in result and isinstance(result["messages"], list):
        msgs = result["messages"]
        preview["items"] = [
            {
                "text": (m.get("text") or "")[:100],
                "sender": m.get("sender"),
                "date": m.get("date"),
            }
            for m in msgs[:3]
        ]
    return preview


def _build_sources(db: Session, msg_ids: set[int]) -> list[dict]:
    """从引用过的 message_ids 构建最终来源列表（按话题去重，最多 5 条）"""
    if not msg_ids:
        return []

    msgs = (
        db.query(Message)
        .filter(Message.id.in_(list(msg_ids)[:100]))
        .order_by(Message.date)
        .all()
    )

    sources: list[dict] = []
    seen_topics: set[int | None] = set()
    for m in msgs:
        if m.topic_id in seen_topics and m.topic_id is not None:
            continue
        seen_topics.add(m.topic_id)
        sources.append({
            "message_ids": [m.id],
            "sender": m.sender,
            "date": m.date.strftime("%Y-%m-%d") if m.date else None,
            "preview": (m.text_plain or "")[:200],
            "topic_id": m.topic_id,
        })
        if len(sources) >= 5:
            break
    return sources
