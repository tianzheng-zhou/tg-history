"""检索子 Agent：在独立上下文窗口中执行搜索任务并返回详细报告。

由主 Agent（Orchestrator）通过 research 工具调用。每个子 Agent 拥有：
  - 独立的 messages 列表（不共享主 Agent 上下文）
  - 全部 7 个检索工具（不含 research，防递归）
  - 自己的 tool-calling 循环（最多 SUB_MAX_STEPS 步）

返回值是 LLM 生成的文本报告（一般 ≤ 8k tokens），不会撑爆主 Agent 上下文。
"""

from __future__ import annotations

import json
import time
from typing import AsyncIterator

from sqlalchemy.orm import Session

from backend.config import settings
from backend.services import llm_adapter
from backend.services.qa_tools import TOOL_SCHEMAS, dispatch_tool

SUB_MAX_STEPS = 15  # 子 Agent 最大步数
MAX_TOOL_OUTPUT_CHARS = 50000  # 单次工具输出截断

# 子 Agent 可用的工具：排除 research（防递归）
SUB_TOOL_SCHEMAS = [t for t in TOOL_SCHEMAS if t["function"]["name"] != "research"]

SUB_SYSTEM_PROMPT = """你是一个 Telegram 聊天记录检索子助手，负责完成一个具体的搜索任务。

你被授予一组检索工具来查询聊天数据库。

工作流程：
1. 根据任务描述，选择合适的检索工具广泛搜索
2. 阅读相关消息的完整内容确认相关性
3. 如果初次搜索结果不够，调整查询再搜一次或换工具
4. 综合所有发现，输出详细报告

检索建议：
- **semantic_search** 是首选——用自然语言描述你要找什么
- **检索数量要够**：调研型任务建议 top_k=80~150；精确型任务 top_k=15~30
- **keyword_search** 用于精确关键词（型号、URL、代码等）
- 找到 message_ids / topic_id 后用 fetch_messages / fetch_topic_context 读完整内容

报告要求：
- 按主题/时间/人物组织信息
- 引用具体发言人和日期
- 如果信息不足，明确说明"未找到相关记录"
- 不要省略重要细节，你的报告会交给上层 Agent 合成最终答案
- 用 Markdown 格式

你最多可以进行 {max_steps} 轮工具调用。请高效完成任务。
""".format(max_steps=SUB_MAX_STEPS)


def _truncate_tool_output(obj: dict) -> str:
    """把工具输出 dict 转为 JSON 字符串，过长时截断"""
    s = json.dumps(obj, ensure_ascii=False)
    if len(s) > MAX_TOOL_OUTPUT_CHARS:
        s = s[:MAX_TOOL_OUTPUT_CHARS] + f"\n\n...[截断，完整长度 {len(s)} 字符]"
    return s


async def _stream_sub_llm(
    messages: list[dict],
    model: str,
) -> AsyncIterator[dict]:
    """子 Agent 的单轮 LLM 流式调用（走 semaphore）。

    yield 事件和 qa_agent._stream_llm_step 类似，但使用 SUB_TOOL_SCHEMAS。
    """
    client = llm_adapter.get_client_for_model(model)
    is_kimi = llm_adapter.is_kimi_model(model)
    sem = llm_adapter.get_chat_semaphore(model)

    kwargs = dict(
        model=model,
        messages=messages,
        tools=SUB_TOOL_SCHEMAS,
        tool_choice="auto",
        stream=True,
        stream_options={"include_usage": True},
    )

    if is_kimi:
        kwargs.update(llm_adapter.kimi_chat_kwargs(model, True))
    else:
        kwargs["parallel_tool_calls"] = True
        kwargs["temperature"] = 0.3

    last_usage = None

    async with sem:
        stream = await client.chat.completions.create(**kwargs)
        tool_calls_acc: dict[int, dict] = {}
        reasoning_parts: list[str] = []

        async for chunk in stream:
            # 捕获 usage（通常在最后一个 chunk）
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                last_usage = usage

            choice = chunk.choices[0] if chunk.choices else None
            if not choice:
                continue
            delta = choice.delta

            rc = getattr(delta, "reasoning_content", None)
            if rc:
                reasoning_parts.append(rc)

            if delta.content:
                yield {"type": "text_delta", "text": delta.content}

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {"id": tc.id or "", "name": "", "arguments": ""}
                    slot = tool_calls_acc[idx]
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] += tc.function.name
                        if tc.function.arguments:
                            slot["arguments"] += tc.function.arguments

    if tool_calls_acc:
        calls = [tool_calls_acc[i] for i in sorted(tool_calls_acc) if tool_calls_acc[i]["name"]]
        if calls:
            yield {"type": "tool_calls", "calls": calls}

    if reasoning_parts:
        yield {"type": "reasoning_content", "text": "".join(reasoning_parts)}

    # yield usage 事件
    yield {"type": "usage", "usage": llm_adapter.parse_usage(last_usage)}

    yield {"type": "done"}


async def run_sub_agent(
    db: Session,
    task: str,
    chat_ids: list[str] | None = None,
    event_callback=None,
) -> dict:
    """运行检索子 Agent，返回 {"report", "message_ids", "steps", "tool_calls_count"}。

    event_callback: 可选的 async callable(event_dict) 用于向上层透传进度事件。
    """
    model = settings.llm_model_qa

    user_content = task
    if chat_ids:
        user_content += f"\n\n[限定在这些群聊中检索: {json.dumps(chat_ids, ensure_ascii=False)}]"

    messages: list[dict] = [
        {"role": "system", "content": SUB_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    cited_message_ids: set[int] = set()
    total_tool_calls = 0
    cumulative_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0}

    def _add_usage(u: dict):
        for k in cumulative_usage:
            cumulative_usage[k] += u.get(k, 0)

    async def _emit(event: dict):
        if event_callback:
            await event_callback(event)

    for step in range(1, SUB_MAX_STEPS + 1):
        await _emit({"type": "sub_step", "step": step})

        step_text = ""
        step_tool_calls: list[dict] = []
        step_reasoning = ""

        try:
            async for ev in _stream_sub_llm(messages, model):
                if ev["type"] == "text_delta":
                    step_text += ev["text"]
                elif ev["type"] == "reasoning_content":
                    step_reasoning = ev["text"]
                elif ev["type"] == "tool_calls":
                    step_tool_calls = ev["calls"]
                elif ev["type"] == "usage":
                    _add_usage(ev["usage"])
                elif ev["type"] == "done":
                    break
        except Exception as e:
            await _emit({"type": "sub_error", "error": str(e)})
            # 如果已有一些文本产出，作为报告返回
            if step_text:
                return {
                    "report": step_text,
                    "message_ids": sorted(cited_message_ids),
                    "steps": step,
                    "tool_calls_count": total_tool_calls,
                    "usage": cumulative_usage,
                }
            return {
                "report": f"子 Agent LLM 调用失败: {e}",
                "message_ids": [],
                "steps": step,
                "tool_calls_count": total_tool_calls,
                "usage": cumulative_usage,
            }

        # 构建 assistant 消息
        assistant_msg: dict = {"role": "assistant"}
        assistant_msg["content"] = step_text or None
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

        # 没有 tool_calls → 最终报告
        if not step_tool_calls:
            return {
                "report": step_text,
                "message_ids": sorted(cited_message_ids),
                "steps": step,
                "tool_calls_count": total_tool_calls,
                "usage": cumulative_usage,
            }

        # 执行工具
        for call in step_tool_calls:
            name = call["name"]
            args_str = call["arguments"] or "{}"
            try:
                args = json.loads(args_str) if args_str else {}
            except json.JSONDecodeError:
                args = {}

            t0 = time.time()
            result = await dispatch_tool(db, name, args)
            duration_ms = int((time.time() - t0) * 1000)
            total_tool_calls += 1

            # 收集引用 IDs
            _collect_ids(result, cited_message_ids)

            result_str = _truncate_tool_output(result)

            await _emit({
                "type": "sub_tool",
                "step": step,
                "name": name,
                "duration_ms": duration_ms,
            })

            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": result_str,
            })

    # 达到 SUB_MAX_STEPS 仍在调用工具 → 强制总结
    messages.append({
        "role": "user",
        "content": "你已达到最大工具调用次数。请基于上面已收集到的所有工具结果，"
                   "输出你的最终报告。**不要再调用任何工具**。",
    })
    try:
        sem = llm_adapter.get_chat_semaphore(model)
        client = llm_adapter.get_client_for_model(model)
        force_kwargs = dict(
            model=model,
            messages=messages,
            stream=False,
        )
        if llm_adapter.is_kimi_model(model):
            force_kwargs.update(llm_adapter.kimi_chat_kwargs(model, False))
        else:
            force_kwargs["temperature"] = 0.3
        async with sem:
            resp = await client.chat.completions.create(**force_kwargs)
        report = resp.choices[0].message.content or ""
        _add_usage(llm_adapter.parse_usage(getattr(resp, "usage", None)))
    except Exception as e:
        report = f"子 Agent 强制总结失败: {e}"

    return {
        "report": report,
        "message_ids": sorted(cited_message_ids),
        "steps": SUB_MAX_STEPS,
        "tool_calls_count": total_tool_calls,
        "usage": cumulative_usage,
    }


def _collect_ids(result: dict, acc: set[int]) -> None:
    """从工具结果中提取所有 message_id"""
    if not isinstance(result, dict):
        return
    for key in ("messages", "results"):
        items = result.get(key)
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    mid = it.get("message_id")
                    if isinstance(mid, int):
                        acc.add(mid)
                    mids = it.get("message_ids")
                    if isinstance(mids, list):
                        for x in mids:
                            if isinstance(x, int):
                                acc.add(x)
