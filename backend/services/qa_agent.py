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

import asyncio
import json
import time
from typing import AsyncIterator

from sqlalchemy.orm import Session

from backend.config import settings
from backend.models.database import Message
from backend.services import llm_adapter
from backend.services.qa_tools import TOOL_SCHEMAS, dispatch_tool

# Orchestrator 拥有全部工具：简单查询直接搜，复杂调研走子 Agent
ORCHESTRATOR_TOOL_SCHEMAS = list(TOOL_SCHEMAS)

MAX_STEPS = 30  # 最大 agent 迭代轮数，防止死循环
MAX_TOOL_OUTPUT_CHARS = 50000  # 塞回 LLM 的单次工具输出最大字符数（降低防止上下文溢出）
CONTEXT_RESERVE_TOKENS = 40000  # 为 LLM 输出预留的 token 数
CHARS_PER_TOKEN = 1.8  # 中英混合文本的大致 chars/token 比


def _estimate_tokens(messages: list[dict]) -> int:
    """粗略估算 messages 的 token 数（中英混合按 1.8 chars/token）"""
    total_chars = 0
    for m in messages:
        c = m.get("content")
        if c:
            total_chars += len(c)
        tcs = m.get("tool_calls")
        if tcs:
            total_chars += len(json.dumps(tcs, ensure_ascii=False))
        rc = m.get("reasoning_content")
        if rc:
            total_chars += len(rc)
    return int(total_chars / CHARS_PER_TOKEN)


def _trim_messages_to_budget(messages: list[dict], model: str) -> tuple[list[dict], bool]:
    """如果 messages 预估 token 超出模型上下文预算，按比例截断 tool 消息。
    返回 (trimmed_messages, was_trimmed)。
    """
    max_ctx = llm_adapter.get_context_window(model)
    budget_tokens = max_ctx - CONTEXT_RESERVE_TOKENS
    est = _estimate_tokens(messages)
    if est <= budget_tokens:
        return messages, False

    excess_chars = int((est - budget_tokens) * CHARS_PER_TOKEN)

    # 收集 tool 消息的索引和大小
    tool_indices: list[tuple[int, int]] = []
    for i, m in enumerate(messages):
        if m.get("role") == "tool":
            tool_indices.append((i, len(m.get("content", ""))))

    if not tool_indices:
        return messages, False

    total_tool_chars = sum(s for _, s in tool_indices)
    result = list(messages)
    for i, size in tool_indices:
        cut = int(excess_chars * size / total_tool_chars) if total_tool_chars > 0 else 0
        new_size = max(size - cut, 1000)
        if new_size < size:
            content = result[i]["content"][:new_size]
            content += f"\n\n...[截断以适应上下文窗口，原长度 {size} 字符]"
            result[i] = {**result[i], "content": content}

    return result, True


SYSTEM_PROMPT = """你是一个 Telegram 聊天记录分析的智能助手。你同时拥有直接检索工具和子 Agent 委派能力。

## 工具选择策略

### 简单/精确查询 → 直接使用检索工具
当问题简单明确时（“XX 说了什么”、“某天发生了什么”、“找包含 XX 的消息”），直接用以下工具搜索：
- **semantic_search**: 语义检索，首选工具，适合自然语言查询
- **keyword_search**: 关键词精确匹配（型号、URL、代码等）
- **search_by_sender**: 按发言人搜索
- **search_by_date**: 按日期范围查询
- **fetch_messages / fetch_topic_context**: 获取完整消息内容

直接搜索更快、更省 token，适合 1-3 次工具调用即可回答的问题。

### 复杂/大范围查询 → 委派子 Agent
当问题需要多维度、跨群、或多轮检索时（“全面梳理 XX”、“对比 XX 和 YY”、“各群讨论了哪些方案”），用 **research** 工具委派给子 Agent：
- 每个子 Agent 拥有独立上下文窗口，会自主多轮搜索
- 一次性发起多个 research 调用来并行执行（放在同一轮 tool_calls 中）
- 每个子任务描述要详细：要搜什么、关注哪些方面、预期返回什么

任务拆分原则：
- **对比/列举型**：2-3 个 research，按维度分
- **复杂调研型**：3-5 个 research，按子主题分

## 通用规则
- 如果不确定数据范围，可先用 **list_chats** 了解可用群聊
- 收到子 Agent 报告后，综合所有报告生成结构化最终答案
- 如果某个子任务报告信息不足，可发起补充 research 或直接用检索工具补查
- 最终答案用 Markdown 格式，**标注来源**（发言人 + 日期）
- 信息不足时大方承认“根据现有记录未找到”，不要编造

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
       - {"type": "usage", "prompt_tokens", "completion_tokens", "total_tokens", "model"}
       - {"type": "done"}
    """
    model = settings.llm_model_qa
    client = llm_adapter.get_client_for_model(model)
    is_kimi = llm_adapter.is_kimi_model(model)

    kwargs = dict(
        model=model,
        messages=messages,
        tools=ORCHESTRATOR_TOOL_SCHEMAS,
        tool_choice="auto",
        stream=True,
        # 启用 usage 统计（最后一个 chunk 会带 usage 字段）
        stream_options={"include_usage": True},
    )

    if is_kimi:
        # Kimi: 思考模式 + tool_choice 只能 auto/none
        kwargs.update(llm_adapter.kimi_chat_kwargs(model, True))
        # Kimi 不支持 parallel_tool_calls 参数
    else:
        kwargs["parallel_tool_calls"] = True
        kwargs["temperature"] = 0.3

    # semaphore 覆盖 create + 整个流式迭代（HTTP 连接期间占用并发 slot）
    sem = llm_adapter.get_chat_semaphore(model)
    tool_calls_acc: dict[int, dict] = {}
    reasoning_parts: list[str] = []
    last_usage: dict | None = None

    async with sem:
        stream = await client.chat.completions.create(**kwargs)

        async for chunk in stream:
            # 捕获 usage（通常在最后一个 chunk，choices 可能为空）
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                last_usage = {
                    "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                    "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                    "total_tokens": getattr(usage, "total_tokens", 0) or 0,
                    "model": model,
                }

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

            # 注意：有 stream_options.include_usage 时，usage 事件在 finish_reason
            # 之后的下一个 chunk 才出现，所以不能 break on finish_reason

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

    # yield usage（供上层记录当前上下文占比）
    if last_usage is not None:
        max_ctx = llm_adapter.get_context_window(model)
        yield {
            "type": "usage",
            "prompt_tokens": last_usage["prompt_tokens"],
            "completion_tokens": last_usage["completion_tokens"],
            "total_tokens": last_usage["total_tokens"],
            "max_context": max_ctx,
            "percent": round(last_usage["prompt_tokens"] / max_ctx, 4) if max_ctx else 0.0,
            "model": model,
        }

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

        # 上下文预算管理：在调用 LLM 前检查并截断
        model = settings.llm_model_qa
        messages, was_trimmed = _trim_messages_to_budget(messages, model)
        if was_trimmed:
            # 发送估算的 usage 事件，让前端 ContextBadge 及时更新
            est_tokens = _estimate_tokens(messages)
            max_ctx = llm_adapter.get_context_window(model)
            yield {
                "type": "usage",
                "step": step,
                "prompt_tokens": est_tokens,
                "completion_tokens": 0,
                "total_tokens": est_tokens,
                "max_context": max_ctx,
                "percent": round(est_tokens / max_ctx, 4) if max_ctx else 0.0,
                "model": model,
            }
            yield {"type": "status", "message": f"上下文已截断以适应 {model} 窗口（估算 {est_tokens} tokens）"}

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
                elif ev["type"] == "usage":
                    # 向上透传 usage（上下文占比显示）
                    yield {**ev, "step": step}
                elif ev["type"] == "done":
                    break
        except Exception as e:
            # 如果有工具结果积累，尝试强制总结而非直接失败
            if step > 1 or any(m.get("role") == "tool" for m in messages):
                yield {"type": "status", "message": f"LLM 调用失败({e})，尝试缩减上下文后强制总结..."}
                recovery_text = await _force_summarize_recovery(messages, cited_message_ids)
                if recovery_text:
                    final_text_parts.append(recovery_text)
                    break
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

        # 解析所有 tool_calls 的参数
        parsed_calls: list[tuple[dict, dict]] = []  # (call, args)
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
            parsed_calls.append((call, args))

        # 发射所有 tool_call 事件
        for call, args in parsed_calls:
            yield {
                "type": "tool_call",
                "step": step,
                "id": call["id"],
                "name": call["name"],
                "args": args,
            }

        # 收集子 Agent 进度事件，稍后统一 yield（因为 asyncio.gather 中不能 yield）
        sub_events: list[dict] = []

        async def _sub_event_cb(ev: dict):
            sub_events.append(ev)

        # 并发执行所有 research 调用（semaphore 自动限流），其他工具顺序执行
        async def _exec_one(call: dict, args: dict) -> tuple[dict, dict, int]:
            """执行单个工具，返回 (call, result, duration_ms)"""
            t0 = time.time()
            cb = _sub_event_cb if call["name"] == "research" else None
            result = await dispatch_tool(db, call["name"], args, event_callback=cb)
            duration_ms = int((time.time() - t0) * 1000)
            return call, result, duration_ms

        # 判断是否有 research 调用 → 并发执行全部（包括 list_chats 等轻量工具）
        has_research = any(c["name"] == "research" for c, _ in parsed_calls)
        if has_research and len(parsed_calls) > 1:
            # 并发执行所有工具调用，由 semaphore 自然限流
            exec_results = await asyncio.gather(
                *[_exec_one(c, a) for c, a in parsed_calls],
                return_exceptions=True,
            )
        else:
            # 单个或无 research：顺序执行（同样捕获异常，保持与 gather 行为一致）
            exec_results = []
            for c, a in parsed_calls:
                try:
                    exec_results.append(await _exec_one(c, a))
                except Exception as exc:
                    exec_results.append(exc)

        # 先 yield 子 Agent 进度事件
        for ev in sub_events:
            yield {"type": "sub_agent_event", "step": step, **ev}

        # 处理执行结果
        for i, item in enumerate(exec_results):
            if isinstance(item, Exception):
                # asyncio.gather return_exceptions=True
                err_call = parsed_calls[i][0]
                err_result = {"error": f"工具执行异常: {item}"}
                yield {
                    "type": "tool_result",
                    "step": step,
                    "id": err_call["id"],
                    "name": err_call["name"],
                    "output_preview": err_result,
                    "duration_ms": 0,
                    "error": True,
                }
                messages.append({
                    "role": "tool",
                    "tool_call_id": err_call["id"],
                    "content": json.dumps(err_result, ensure_ascii=False),
                })
                continue

            call, result, duration_ms = item

            # 收集消息 ID 用于后续引用
            _collect_ids(result, cited_message_ids)

            # 截断过长输出
            result_str = _truncate_tool_output(result)

            yield {
                "type": "tool_result",
                "step": step,
                "id": call["id"],
                "name": call["name"],
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
        # 截断上下文
        _model = settings.llm_model_qa
        messages, _ = _trim_messages_to_budget(messages, _model)
        try:
            _client = llm_adapter.get_client_for_model(_model)
            _force_kwargs = dict(
                model=_model,
                messages=messages,
                stream=True,
            )
            if llm_adapter.is_kimi_model(_model):
                _force_kwargs.update(llm_adapter.kimi_chat_kwargs(_model, False))
            else:
                _force_kwargs["temperature"] = 0.3
            _sem = llm_adapter.get_chat_semaphore(_model)
            forced_text = ""
            async with _sem:
                stream = await _client.chat.completions.create(**_force_kwargs)
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


async def _force_summarize_recovery(messages: list[dict], cited_ids: set[int]) -> str:
    """LLM 调用失败后的恢复策略：激进截断工具输出后强制总结。"""
    model = settings.llm_model_qa

    # 只保留 system + user + 大幅截断的工具结果
    recovery_msgs: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            recovery_msgs.append(m)
        elif role == "user":
            recovery_msgs.append(m)
        elif role == "tool":
            content = m.get("content", "")
            # 每个工具结果最多保留 3000 字符
            if len(content) > 3000:
                content = content[:3000] + "\n...[截断]"
            recovery_msgs.append({**m, "content": content})
        elif role == "assistant":
            # 保留 assistant 消息但去掉 reasoning_content
            slim = {"role": "assistant", "content": m.get("content")}
            if m.get("tool_calls"):
                slim["tool_calls"] = m["tool_calls"]
            recovery_msgs.append(slim)

    recovery_msgs.append({
        "role": "user",
        "content": "之前的 LLM 调用因上下文过长失败了。请基于上面已收集到的工具结果（可能被截断），"
                   "给出最终答案。**不要再调用任何工具**。",
    })

    # 再次做预算检查
    recovery_msgs, _ = _trim_messages_to_budget(recovery_msgs, model)

    try:
        client = llm_adapter.get_client_for_model(model)
        sem = llm_adapter.get_chat_semaphore(model)
        kwargs = dict(
            model=model,
            messages=recovery_msgs,
            stream=False,
        )
        if llm_adapter.is_kimi_model(model):
            kwargs.update(llm_adapter.kimi_chat_kwargs(model, False))
        else:
            kwargs["temperature"] = 0.3
        async with sem:
            resp = await client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""
    except Exception:
        return ""


def _collect_ids(result: dict, acc: set[int]) -> None:
    """从工具结果中提取所有 message_id，用来构建最终引用"""
    if not isinstance(result, dict):
        return
    # sub_agent 返回的 message_ids 列表（research 工具）
    mids_top = result.get("message_ids")
    if isinstance(mids_top, list):
        for x in mids_top:
            if isinstance(x, int):
                acc.add(x)
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

    # research 工具（子 Agent）返回
    if "report" in result:
        report_text = result["report"] or ""
        return {
            "summary": f"子 Agent 报告 ({result.get('steps', '?')} 步, "
                       f"{result.get('tool_calls_count', '?')} 次工具调用, "
                       f"{len(result.get('message_ids', []))} 条引用)",
            "report_preview": report_text[:300],
        }

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
