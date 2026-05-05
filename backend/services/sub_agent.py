"""检索子 Agent：在独立上下文窗口中执行搜索任务并返回详细报告。

由主 Agent（Orchestrator）通过 research 工具调用。每个子 Agent 拥有：
  - 独立的 messages 列表（不共享主 Agent 上下文）
  - 全部 7 个检索工具（不含 research，防递归）
  - 自己的 tool-calling 循环（轮数按任务自适应 8~16，硬上限 20，上下文软上限 100K tokens）

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

SUB_MAX_STEPS_DEFAULT = 12   # 子 Agent 默认最大步数（开放式多轮，不要太严苛）
SUB_MAX_STEPS_HARD_CAP = 20  # 硬上限（防死循环）
MAX_TOOL_OUTPUT_CHARS = 40_000  # 单次工具输出 backstop（≈22K tokens）；优先按列表条目级截断
MAX_LIST_ITEMS_PER_TOOL = 60  # 单次工具结果最多保留多少条 list 项（messages / topics 等）

# 子 Agent 上下文软上限（estimated tokens）。仅用作后端 safety net，**不向模型暴露**——
# 让模型自然按任务规模工作，到达上限再静默触发 forced_summary。
# 阈值 100K：留 28K buffer 防 qwen3.5-plus 进入 128K 高价/低性能档。
SUB_CONTEXT_SOFT_LIMIT_TOKENS = 100_000
_CHARS_PER_TOKEN = 1.8  # 中英混合粗估

# 子 Agent 可用的工具：
# - 排除 research（防递归）
# - 排除 artifact 工具（它们是 Orchestrator 的最终交付职责，且需 session 上下文）
_SUB_EXCLUDED_TOOLS = {
    "research",
    "create_artifact", "update_artifact", "rewrite_artifact",
    "list_artifacts", "read_artifact",
}
SUB_TOOL_SCHEMAS = [t for t in TOOL_SCHEMAS if t["function"]["name"] not in _SUB_EXCLUDED_TOOLS]

# filters 会被自动注入到这些检索类工具的 args 里（若 args 没显式给该字段）
_FILTER_INJECTABLE_TOOLS = {
    "semantic_search", "keyword_search",
    "search_by_sender", "search_by_date",
    "list_topics",
}
_INJECTABLE_FILTER_KEYS = {"chat_ids", "topic_ids", "senders", "start_date", "end_date"}

SUB_SYSTEM_PROMPT = """你是一个 Telegram 聊天记录检索子助手。
主 Agent 已经把具体任务写在了 user 消息里——请认真完整地完成它。

## 你的目标：在 task 范围内尽可能完整

主 Agent 已经把"全局问题"拆成了你这个子任务，给了明确的范围（关键词集合 / 主题维度 /
时间段 / 群聊）。**在这个范围内你要做扎实**：
- 所有相关线索都要查到、追下去
- 多角度验证（比如同一事实在多人发言中是否一致）
- 关键消息拉原文 + 上下文（用 `fetch_messages` 的 `context_window`）
- 不要"敷衍交差"——你的报告会被主 Agent 合成给用户，缺漏会变成最终答案的盲点

## 但不要越界

主 Agent 会发**多个并行 research** 覆盖不同子范围。你只负责自己这一片：
- 出现的新线索如果**超出你的 task 范围**（比如任务说找虚拟卡，结果消息提到卡网），
  在报告末尾用 "## 越界线索（建议主 Agent 跟进）" 列出来——不要自己跑去搜
- 主 Agent 会基于你的报告决定要不要发新的 research

## 高效检索：search → fetch 两阶段漏斗 ✨

**最重要的工作流：先 search 拿索引，再 fetch 拉原文**——不要试图从 search 的 snippet 直接答题。

### 阶段 1：search 拿候选 msg_id 索引
- `semantic_search` 或 `keyword_search`（limit=50~80）只为**找出哪些消息可能相关**
- 不要从 snippet 直接下结论——snippet 只是片段，可能误导
- 看 search 结果时，**记下相关的 msg_id**，准备进入阶段 2

### 阶段 2：fetch 精读原文 + 上下文
- 从阶段 1 的候选里挑出真正相关的 5~15 个 msg_id
- 用 `fetch_messages(message_ids=[...], context_window=2~3)` **拉原文 + 前后邻居**——还原语境
- 或者 `fetch_topic_context(topic_id=N)` 读整话题（仅当话题不太大时）

**好处**：避免一次 search 把 50~80 条完整原文塞进上下文（可能 50K+ tokens 大部分是无关内容）；
精读阶段只看 ~15 条原文（≈5K~10K tokens）但都是高质量的，引用更准。

## keyword vs semantic：优先级反转

**默认优先 keyword_search**（结果更精准、上下文更省）——以下场景必须用 keyword：
- 已知具体名字 / 品牌 / 产品（"EFunCard"、"OpenAI"、"GPT-4"）
- 型号 / 版本号 / URL / 缩写 / 数字（"RTX 4090"、"v1.2"、"github.com/..."）
- 精确表达式（FTS5 支持 OR / NEAR）

**只有以下场景才用 semantic_search**：
- 自然语言概念查询（"GPU 价格趋势"、"用户对 Claude 的看法"）
- 模糊匹配（"那种新出的虚拟卡平台"——名字不确定）

反例：要找 "EFunCard 怎么充值" → 用 `keyword_search(["EFunCard"])`，**不要** `semantic_search("EFunCard 充值方式")`——
keyword 精准命中，semantic 会带回相关性尾部的无关结果。

## 去重意识：避免反复检索同一片信息

每次工具调用前心里维护一个"**已收集的 msg_id 集合**"，问自己：
- "这个查询会大量返回我已经看过的 ids 吗？" 如果是 → **换不同维度**（按人 / 按时间 / 按 topic_id），不要换近义词
- 如果上一次 search 已返回 50 条候选，下一步该是 **fetch_messages 精读**，不是再搜一次相似 query

## 反模式（这些是无效努力，不是认真努力）
- ❌ 同一关键词反复搜不同 limit
- ❌ 用近义词链式扩搜（"虚拟卡"→"卡平台"→"充值卡"→"信用卡"...）—— 一次 semantic_search 已经覆盖语义近邻
- ❌ 在 task 范围外漫游搜索（看到新名词就追下去）—— 写到"## 越界线索"区块汇报给主 Agent
- ❌ search 返回的 50 条候选**全部**直接当作答案——应该 fetch 精读筛选

**搜索结果带 `_truncated` 标记时**（系统按条目截断保护上下文）：基于已展示的样本判断够不够，
需要更多就**缩小过滤范围**（加日期/topic_ids/senders）再搜，而不是反复用同一查询。

## 可用工具
- **keyword_search**（已知具体词时**优先**）：精确关键词 / FTS5。keywords 列表 OR 合并 + 多维过滤
- **semantic_search**（自然语言查询时用）：语义检索。chat_ids / topic_ids / 日期 / senders / min_messages_in_chunk 过滤。limit=50~80
- **list_topics**：列群聊话题（带 summary / 时间段 / 参与人数）。**侦察利器**——拿到 topic_id 后传给后续工具精检索
- **fetch_topic_context**：按 topic_id 拉整话题原文（话题大时谨慎，可能很长）
- **fetch_messages**：按 ids 拉原文；`context_window=N` 拉前后 N 条同 chat 邻居（**强烈推荐 N=2~3**，还原语境）
- **search_by_sender / search_by_date**：按人或日期 + 多维过滤

## 过滤器注入
任务里如有 `[Filters: ...]` 段，**每次工具调用主动带上**这些参数
（chat_ids / topic_ids / senders / start_date / end_date）。即便忘了，系统会自动补（仅在你没显式给时）。

## 报告要求
- 严格按主 Agent 要求的结构组织（timeline / 按人分组 / 观点-证据对 / 列表）
- **每个事实必须有引用**：`[msg:<id>]` 或 `[topic:<id>]`；日期/发言人尽量写出
- **越界线索**：单独列在末尾"## 越界线索"区块（如有）
- 信息缺失时直说"未找到 X，已尝试关键词 [...]"——主 Agent 会决定补查
- 用 Markdown 格式，简洁但不省略关键证据
"""


def _truncate_tool_output(obj: dict) -> str:
    """把工具输出 dict 转为 JSON 字符串，过长时智能截断。

    策略（优先级从高到低，保持 JSON 结构有效）：
      1. 找出 obj 顶层的 list 字段（如 messages / topics / results / chats），
         若长度 > MAX_LIST_ITEMS_PER_TOOL，截断到该值并加 `_truncated_*` 元数据
      2. 序列化后若仍超 MAX_TOOL_OUTPUT_CHARS，再退化为字符串截断（少见）

    这样 agent 能看到：还有多少条没展示、被截断字段是什么——而不是 JSON 中间被砍断。
    """
    if not isinstance(obj, dict):
        return json.dumps(obj, ensure_ascii=False)[:MAX_TOOL_OUTPUT_CHARS]

    # 第一步：list 字段截条目
    truncated_obj = dict(obj)
    truncation_meta: dict = {}
    for key, val in obj.items():
        if isinstance(val, list) and len(val) > MAX_LIST_ITEMS_PER_TOOL:
            truncated_obj[key] = val[:MAX_LIST_ITEMS_PER_TOOL]
            truncation_meta[key] = {
                "shown": MAX_LIST_ITEMS_PER_TOOL,
                "total": len(val),
                "hidden": len(val) - MAX_LIST_ITEMS_PER_TOOL,
            }
    if truncation_meta:
        truncated_obj["_truncated"] = truncation_meta
        truncated_obj["_truncation_hint"] = (
            "结果较多，已展示前 N 条；如需更多，可缩小过滤范围（加日期/发言人/topic_ids）后再搜，"
            "或基于这些样本判断够不够再决定是否补查"
        )

    s = json.dumps(truncated_obj, ensure_ascii=False)

    # 第二步：字符级 backstop（每条消息原文很长时才会触发）
    if len(s) > MAX_TOOL_OUTPUT_CHARS:
        s = s[:MAX_TOOL_OUTPUT_CHARS] + (
            f'\n\n...[字符级截断 backstop，完整长度 {len(s)} 字符]'
        )
    return s


def _estimate_messages_tokens(messages: list[dict]) -> int:
    """粗略估算 messages 累积 token 数（中英混合按 1.8 chars/token）。

    用途：检查子 Agent 上下文是否接近软上限，及时强制总结。
    """
    total_chars = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total_chars += len(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict):
                    t = part.get("text") or part.get("content") or ""
                    if isinstance(t, str):
                        total_chars += len(t)
        rc = m.get("reasoning_content")
        if rc:
            total_chars += len(rc)
        # tool_calls 的 arguments
        for tc in m.get("tool_calls", []) or []:
            args = tc.get("function", {}).get("arguments")
            if isinstance(args, str):
                total_chars += len(args)
    return int(total_chars / _CHARS_PER_TOKEN)


def _auto_max_steps(task: str, expected_output: str | None, override: int | None) -> int:
    """根据任务难度估算子 Agent 的最大工具调用轮数。

    设计哲学：开放式多轮——给子 Agent 足够空间在 task 范围内完整搜索 + 验证。
    上下文软上限（SUB_CONTEXT_SOFT_LIMIT_TOKENS）作为后端 safety net 兜底。

    规则：
    - override > 0 时优先（受 HARD_CAP 约束）
    - 超短任务（<200 字符 且 无 expected_output） → 8
    - 默认 → 12
    - 复杂信号关键词 → 16（验证 + 多角度搜索都需要轮次）
    """
    if isinstance(override, int) and override > 0:
        return max(1, min(override, SUB_MAX_STEPS_HARD_CAP))

    text = (task or "") + " " + (expected_output or "")
    length = len(text)
    lower = text.lower()

    complex_signals = [
        "汇总", "对比", "比较", "timeline", "时间线",
        "按月", "按周", "按人", "按群", "跨群",
        "梳理", "整理", "综述", "统计", "分布", "验证", "交叉",
    ]
    if any(s in lower for s in complex_signals):
        return min(16, SUB_MAX_STEPS_HARD_CAP)

    if length < 200 and not expected_output:
        return 8
    return SUB_MAX_STEPS_DEFAULT


def _build_user_prompt(
    task: str,
    scope: str | None,
    filters: dict | None,
    expected_output: str | None,
) -> str:
    """构造子 Agent 首轮 user 消息内容。

    结构：
      [系统提示：当前时间是 ...]   # 自动注入，帮子 Agent 处理相对时间表达

      [Task]
      <task>

      [Scope]          # 可选
      <scope>

      [Filters]        # 可选
      <filters json>

      [Expected Output] # 可选
      <expected_output>
    """
    # 延迟 import 避免循环依赖
    from backend.services.qa_agent import build_current_time_hint

    parts: list[str] = [build_current_time_hint(), f"\n[Task]\n{task}"]
    if scope:
        parts.append(f"\n[Scope]\n{scope}")
    if filters:
        # 只保留可注入的字段，避免把未知字段也写进去
        clean = {k: v for k, v in filters.items() if k in _INJECTABLE_FILTER_KEYS and v}
        if clean:
            parts.append(f"\n[Filters]\n{json.dumps(clean, ensure_ascii=False)}")
    if expected_output:
        parts.append(f"\n[Expected Output]\n{expected_output}")
    return "\n".join(parts)


def _inject_filters_into_args(tool_name: str, args: dict, filters: dict | None) -> dict:
    """若 filters 非空且工具支持过滤字段，把 filters 的值注入 args（仅补未给的字段）。"""
    if not filters or tool_name not in _FILTER_INJECTABLE_TOOLS:
        return args
    new_args = dict(args)
    for k in _INJECTABLE_FILTER_KEYS:
        v = filters.get(k)
        if v and k not in new_args:
            new_args[k] = v
    return new_args


async def _stream_sub_llm(
    messages: list[dict],
    model: str,
) -> AsyncIterator[dict]:
    """子 Agent 的单轮 LLM 流式调用（走 semaphore）。

    yield 事件和 qa_agent._stream_llm_step 类似，但使用 SUB_TOOL_SCHEMAS。
    Qwen 模型 + enable_qwen_explicit_cache=True 时会在最后一条消息
    加 cache_control 标记（足 1024 token 阈值才生效）。
    """
    client = llm_adapter.get_client_for_model(model)
    is_kimi = llm_adapter.is_kimi_model(model)
    sem = llm_adapter.get_chat_semaphore(model)

    # 显式缓存注入：仅 qwen 模型 + 开关打开时
    effective_messages = messages
    if (
        settings.enable_qwen_explicit_cache
        and llm_adapter.is_qwen_model(model)
        and not is_kimi
    ):
        effective_messages = llm_adapter.inject_cache_control(messages)

    kwargs = dict(
        model=model,
        messages=effective_messages,
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
    scope: str | None = None,
    filters: dict | None = None,
    expected_output: str | None = None,
    max_steps: int | None = None,
    event_callback=None,
) -> dict:
    """运行检索子 Agent，返回 {"report", "message_ids", "steps", "tool_calls_count", "usage", "model"}。

    Args:
        task: 详细的任务描述（必填）
        chat_ids: 兼容老参数——等价于 filters["chat_ids"]；两者都给时 filters 优先
        scope: 可选的自然语言范围说明
        filters: 结构化过滤器，子 Agent 会把这些字段自动注入每次工具调用的 args
        expected_output: 希望子 Agent 报告包含的字段/结构
        max_steps: 显式上限；未给时按 task 难度自适应（6/12/18）
        event_callback: 可选 async callable(event_dict)，向上层透传进度事件
    """
    model = settings.effective_sub_agent_model

    # 合并 chat_ids 与 filters.chat_ids（filters 优先）
    effective_filters: dict = dict(filters) if isinstance(filters, dict) else {}
    if chat_ids and not effective_filters.get("chat_ids"):
        effective_filters["chat_ids"] = list(chat_ids)

    step_cap = _auto_max_steps(task, expected_output, max_steps)
    user_content = _build_user_prompt(task, scope, effective_filters, expected_output)

    messages: list[dict] = [
        {"role": "system", "content": SUB_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    cited_message_ids: set[int] = set()
    total_tool_calls = 0
    cumulative_usage = {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "cached_tokens": 0, "cache_creation_tokens": 0,
    }

    def _add_usage(u: dict):
        for k in cumulative_usage:
            cumulative_usage[k] += u.get(k, 0)

    async def _emit(event: dict):
        if event_callback:
            await event_callback(event)

    # 标记：是否因为上下文软上限提前 break（决定要不要走强制总结分支）
    soft_limit_hit = False

    for step in range(1, step_cap + 1):
        # 上下文软上限检查（静默 safety net，不暴露给模型，避免诱发偷懒）：
        # 累积 token 超过软上限就强制总结，避免进入 qwen3.5-plus 的 128K 高价/低性能档
        ctx_tokens = _estimate_messages_tokens(messages)
        if ctx_tokens > SUB_CONTEXT_SOFT_LIMIT_TOKENS:
            await _emit({
                "type": "sub_status",
                "message": f"已收集 {ctx_tokens} tokens 资料，进入总结阶段",
            })
            soft_limit_hit = True
            break

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
                    "model": model,
                }
            return {
                "report": f"子 Agent LLM 调用失败: {e}",
                "message_ids": [],
                "steps": step,
                "tool_calls_count": total_tool_calls,
                "usage": cumulative_usage,
                "model": model,
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
                "model": model,
            }

        # 执行工具
        for call in step_tool_calls:
            name = call["name"]
            args_str = call["arguments"] or "{}"
            try:
                args = json.loads(args_str) if args_str else {}
            except json.JSONDecodeError:
                args = {}

            # 自动注入 effective_filters（子 Agent 没显式给该字段时才补）
            args = _inject_filters_into_args(name, args, effective_filters)

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

    # 达到 step_cap 或上下文软上限 → 强制总结
    # 措辞中性：避免让模型误以为"被惩罚"而输出敷衍内容
    force_msg = (
        "你已经收集到足够的资料。请基于上面所有工具结果，按 task 要求的结构输出最终报告。"
        "**不要再调用任何工具**。如果还有未覆盖的子方向（task 范围外的新线索），"
        "在报告末尾的 '## 越界线索' 区块列出，主 Agent 会决定是否再发 research 跟进。"
    )
    messages.append({"role": "user", "content": force_msg})
    try:
        sem = llm_adapter.get_chat_semaphore(model)
        client = llm_adapter.get_client_for_model(model)
        force_messages = messages
        if (
            settings.enable_qwen_explicit_cache
            and llm_adapter.is_qwen_model(model)
            and not llm_adapter.is_kimi_model(model)
        ):
            force_messages = llm_adapter.inject_cache_control(messages)
        force_kwargs = dict(
            model=model,
            messages=force_messages,
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
        "steps": step_cap,
        "tool_calls_count": total_tool_calls,
        "usage": cumulative_usage,
        "model": model,
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
