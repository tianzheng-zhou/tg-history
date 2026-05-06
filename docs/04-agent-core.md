# 聊天记录管理迁移文档（4/6）Agent 主循环（最核心）

> 上一篇 [`03-vector-index.md`](./03-vector-index.md) 介绍了向量索引。本篇是整个项目的灵魂 — Agent 主循环。

## 7. LLM Adapter 与并发控制（`services/llm_adapter.py`）

### 7.1 多 Provider 单例 client

```python
_dashscope_client: AsyncOpenAI | None = None
_moonshot_client: AsyncOpenAI | None = None

def _make_http_client(timeout=180.0):
    """统一构造 httpx.AsyncClient。"""
    return httpx.AsyncClient(
        trust_env=False,                            # 国内 API 走系统代理只会变慢
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=50),
        timeout=httpx.Timeout(timeout, connect=10.0),
    )

def _get_client():
    """DashScope 单例（含 kimi/ 百炼直供）"""
    global _dashscope_client
    if _dashscope_client is None:
        _dashscope_client = AsyncOpenAI(
            api_key=settings.dashscope_api_key,
            base_url=settings.dashscope_base_url,
            http_client=_make_http_client(),
        )
    return _dashscope_client

def _get_moonshot_client():
    """官方 Moonshot 单例"""
    global _moonshot_client
    if _moonshot_client is None:
        _moonshot_client = AsyncOpenAI(
            api_key=settings.moonshot_api_key,
            base_url=settings.moonshot_base_url,
            http_client=_make_http_client(),
        )
    return _moonshot_client
```

### 7.2 分 Provider 并发控制

```python
# 关键 — 不同 provider RPM 不同，必须分别限流
_DASHSCOPE_CHAT_SEM = asyncio.Semaphore(10)  # DashScope RPM 30,000，10 并发足够
_MOONSHOT_CHAT_SEM = asyncio.Semaphore(3)    # 官方 Moonshot 限制 = 3
_EMBED_SEM = asyncio.Semaphore(20)
```

> **为什么分开**：如果用统一 semaphore，Moonshot 限流会把 DashScope 一起带慢。

### 7.3 Provider 路由

```python
def is_kimi_model(model):
    return model.startswith("kimi-") or model.startswith("kimi/")

def is_qwen_model(model):
    return model.startswith("qwen") or model.startswith("qvq") or model.startswith("qwq")

def _is_dashscope_kimi(model):
    """百炼直供的 kimi 模型 (kimi/...)"""
    return model.startswith("kimi/")

def get_client_for_model(model):
    if is_kimi_model(model) and not _is_dashscope_kimi(model):
        return _get_moonshot_client()  # 官方 Moonshot
    return _get_client()  # DashScope（含 kimi/ 百炼直供）

def get_chat_semaphore(model):
    if is_kimi_model(model) and not _is_dashscope_kimi(model):
        return _MOONSHOT_CHAT_SEM
    return _DASHSCOPE_CHAT_SEM
```

### 7.4 显式缓存（`inject_cache_control` — Qwen 系列）

```python
CACHE_CONTROL_MIN_CHARS = 1843  # ≈1024 token (1.8 chars/token)，低于此阈值不打标记

def inject_cache_control(messages):
    """给 messages 最后一条非空 content 打 cache_control 标记。

    实现要点：
    - 不修改原列表，返回新列表（仅最后一条被替换）
    - content 升级为 [{"type":"text","text":"...","cache_control":{"type":"ephemeral"}}]
    - 总字符 < 1843 时不打（避免浪费 1.25× 创建费）
    """
    total = sum(len(m.get("content") or "") for m in messages if isinstance(m.get("content"), str))
    if total < CACHE_CONTROL_MIN_CHARS:
        return messages

    out = list(messages)
    # 找最后一条非空 content（不能在 tool_calls 消息上打）
    for i in range(len(out) - 1, -1, -1):
        m = out[i]
        c = m.get("content")
        if not c:
            continue
        if isinstance(c, str):
            out[i] = {
                **m,
                "content": [{"type": "text", "text": c, "cache_control": {"type": "ephemeral"}}],
            }
        break
    return out
```

> **为什么需要显式缓存**：Qwen 隐式缓存命中率近 0%，改用 `cache_control` 后 99%+。命中价 = 输入 × 10%，创建价 = 输入 × 1.25。**多轮对话时这个收益巨大** —— 一个 50K 输入的 turn，命中和不命中能差 5x 费用。

> **为什么打"最后一条"**：Qwen 的 cache 是 prefix cache —— 标记位置之前的所有 tokens 都会被缓存。打最后一条 = 全部缓存。下次请求 prefix 一致就命中。

### 7.5 模型上下文窗口表

```python
MODEL_CONTEXT_WINDOW = {
    "qwen3.6-plus": 1_000_000,
    "qwen3.5-plus": 1_000_000,
    "qwen3.5-flash": 1_000_000,
    "qwen3-max": 32_768,
    "qwen3-max-2025-09-23": 32_768,
    "kimi-k2.6": 262_144,
    "kimi-k2.5": 262_144,
    "kimi/kimi-k2.6": 262_144,
    "kimi/kimi-k2.5": 262_144,
}
DEFAULT_CONTEXT_WINDOW = 131_072

def get_context_window(model):
    """精确匹配 → prefix 匹配 → 默认 128K（处理带快照日期的模型）"""
    if model in MODEL_CONTEXT_WINDOW:
        return MODEL_CONTEXT_WINDOW[model]
    # prefix 匹配（处理 kimi-k2.6-2025-11-01 这种）
    for k, v in MODEL_CONTEXT_WINDOW.items():
        if model.startswith(k):
            return v
    return DEFAULT_CONTEXT_WINDOW
```

### 7.6 模型定价表（用于估算费用）

```python
MODEL_PRICING = {  # (input_per_M, output_per_M, cached_input_per_M) — 单位 RMB
    "kimi/kimi-k2.6": (6.5, 27.0, 6.5 * 0.169),    # 百炼直供 cache 8.3%
    "kimi-k2.6":      (6.5, 27.0, 6.5 * 0.1),       # 官方 Moonshot cache 10%
    "qwen3.6-plus":   (2.0, 12.0, 2.0 * 0.1),
    "qwen3.5-plus":   (1.6, 8.0, 1.6 * 0.1),
    "qwen3.5-flash":  (0.2, 2.0, 0.2 * 0.1),
    "qwen3-max":      (12.0, 60.0, 12.0 * 0.1),
    "text-embedding-v4": (0.5, 0, 0),
    "qwen3-rerank":      (0.4, 0, 0),
}

def estimate_cost(model, prompt_tokens, completion_tokens, cached_tokens=0, cache_creation_tokens=0):
    input_price, output_price, cache_price = _match_pricing(model)
    create_price = input_price * 1.25  # cache 创建多收 25%

    uncached_prompt = max(prompt_tokens - cached_tokens - cache_creation_tokens, 0)

    cost = (uncached_prompt / 1e6) * input_price
    cost += (cached_tokens / 1e6) * cache_price
    cost += (cache_creation_tokens / 1e6) * create_price
    cost += (completion_tokens / 1e6) * output_price
    return round(cost, 6)
```

### 7.7 Kimi 思考链特殊参数

不同 provider 控制 Kimi 思考链开关的字段名不同：

```python
def kimi_chat_kwargs(model, enable_thinking):
    """构造 Kimi 模型的 extra_body / temperature。

    官方 Moonshot: thinking: {type: "enabled"/"disabled"}
    百炼直供 kimi/: enable_thinking: true/false
    温度固定：思考=1.0，非思考=0.6
    """
    kwargs = {}
    is_dashscope = _is_dashscope_kimi(model)
    if enable_thinking is False:
        if is_dashscope:
            kwargs["extra_body"] = {"enable_thinking": False}
        else:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        kwargs["temperature"] = 0.6
    else:
        if is_dashscope:
            kwargs["extra_body"] = {"enable_thinking": True}
        else:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        kwargs["temperature"] = 1.0
    return kwargs
```

### 7.8 Usage 解析

```python
def parse_usage(usage_obj):
    """解析 OpenAI 风格的 usage 对象。

    DashScope cached_tokens：在 usage.prompt_tokens_details.cached_tokens
    DashScope cache_creation：暂未提供，全 0
    """
    if usage_obj is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                "cached_tokens": 0, "cache_creation_tokens": 0}

    prompt_tokens = getattr(usage_obj, "prompt_tokens", 0) or 0
    completion_tokens = getattr(usage_obj, "completion_tokens", 0) or 0
    total_tokens = getattr(usage_obj, "total_tokens", 0) or (prompt_tokens + completion_tokens)

    cached = 0
    details = getattr(usage_obj, "prompt_tokens_details", None)
    if details:
        cached = getattr(details, "cached_tokens", 0) or 0

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached,
        "cache_creation_tokens": 0,
    }
```

### 7.9 统一接口

```python
async def chat(messages, model=None, temperature=0.3, max_tokens=4096, enable_thinking=None) -> str
async def chat_stream(messages, ...) -> AsyncIterator[str]
async def embed(texts, model=None) -> list[list[float]]
async def rerank(query, documents, top_n=5, model=None) -> list[dict]
```

---

## 8. 主 Agent (Orchestrator) 主循环（`services/qa_agent.py`）

### 8.1 配置常量

```python
MAX_STEPS = 20                       # 最大迭代轮数
MAX_TOOL_OUTPUT_CHARS = 50000        # 单次工具输出上限（注入到 messages 之前）
CONTEXT_RESERVE_TOKENS = 40000       # 为 LLM 输出预留
CHARS_PER_TOKEN = 1.8                # 中英混合估算
```

### 8.2 主循环骨架

```python
async def run_agent(db, question, chat_ids=None, history=None, session_id=None):
    """主 Agent 流式运行。yield 事件 dict。"""
    model = settings.llm_model_qa
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": question})

    cited_message_ids: set[int] = set()
    final_text_parts: list[str] = []
    cumulative_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                         "cached_tokens": 0, "cache_creation_tokens": 0}
    sub_agent_usage = dict(cumulative_usage)  # 仅子 Agent

    forced_summary = False
    actual_steps = 0

    for step in range(1, MAX_STEPS + 1):
        actual_steps = step

        # 上下文预算检查 + 截断
        messages, was_trimmed = _trim_messages_to_budget(messages, model)
        if was_trimmed:
            yield {"type": "status", "message": "上下文已截断"}

        yield {"type": "step_start", "step": step}

        # 流式 LLM 调用
        step_text = ""
        step_tool_calls: list[dict] = []
        step_reasoning = ""

        try:
            async for ev in _stream_llm_step(messages, model):
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
                    _add_usage(cumulative_usage, ev["usage"])
                    yield {"type": "usage", **ev["usage"], "model": model,
                           "max_context": llm_adapter.get_context_window(model),
                           "percent": ev["usage"]["prompt_tokens"] /
                                       llm_adapter.get_context_window(model)}
                elif ev["type"] == "done":
                    break
        except Exception as e:
            # 上下文过长等异常 → 走恢复策略
            recovery = await _force_summarize_recovery(messages, cited_message_ids)
            if recovery:
                final_text_parts.append(recovery)
                yield {"type": "thinking_delta", "step": step, "text": recovery}
            else:
                yield {"type": "error", "error": f"LLM 调用失败: {e}"}
            break

        # 把本轮 assistant 消息加回去
        assistant_msg: dict = {"role": "assistant"}
        assistant_msg["content"] = step_text or None
        if step_reasoning:
            assistant_msg["reasoning_content"] = step_reasoning
        if step_tool_calls:
            assistant_msg["tool_calls"] = [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in step_tool_calls
            ]
        messages.append(assistant_msg)

        yield {"type": "step_done", "step": step,
               "had_tool_calls": bool(step_tool_calls)}

        # 没 tool_calls → 给出最终答案，循环结束
        if not step_tool_calls:
            final_text_parts.append(step_text)
            break

        # 有 tool_calls → 解析参数 + 并发执行
        parsed_calls: list[tuple[dict, dict]] = []
        for call in step_tool_calls:
            try:
                args = json.loads(call["arguments"]) if call["arguments"] else {}
            except json.JSONDecodeError:
                args = {}
            yield {"type": "tool_call", "step": step, "id": call["id"],
                   "name": call["name"], "args": args}
            parsed_calls.append((call, args))

        # ----- 并发跑工具 -----
        sub_events: list[dict] = []  # 子 Agent 的进度事件先攒着，在工具结果之前发

        async def _exec_one(call, args):
            t0 = time.time()
            ctx = {"session_id": session_id} if session_id else None

            async def _on_sub_event(ev):
                # 子 Agent 进度事件
                sub_events.append({"step": step, "tool_call_id": call["id"], **ev})

            result = await dispatch_tool(db, call["name"], args,
                                          event_callback=_on_sub_event, context=ctx)
            return call, result, int((time.time() - t0) * 1000)

        exec_results = await asyncio.gather(
            *[_exec_one(c, a) for c, a in parsed_calls],
            return_exceptions=True,
        )

        # 先 yield 子 Agent 的进度事件
        for ev in sub_events:
            yield {"type": "sub_agent_event", "step": step, **ev}

        # 处理工具结果
        for i, item in enumerate(exec_results):
            if isinstance(item, Exception):
                err_call = parsed_calls[i][0]
                yield {"type": "tool_result", "step": step, "id": err_call["id"],
                       "name": err_call["name"], "error": True,
                       "output_preview": {"error": str(item)}}
                messages.append({"role": "tool", "tool_call_id": err_call["id"],
                                  "content": json.dumps({"error": str(item)})})
                continue

            call, result, duration_ms = item

            # 子 Agent 返回的 usage 累加（research 工具）
            sub_usage = result.get("usage") if isinstance(result, dict) else None
            if sub_usage:
                _add_usage(cumulative_usage, sub_usage)
                _add_usage(sub_agent_usage, sub_usage)

            # 收集消息 ID 用于后续引用
            _collect_ids(result, cited_message_ids)

            # 提取 artifact_event（artifact 类工具会在 result 里塞 _artifact_event）
            # 用 pop 把它从 result 中剥掉，避免回灌到 LLM 上下文里浪费 token
            artifact_ev_payload = None
            if isinstance(result, dict) and "_artifact_event" in result:
                artifact_ev_payload = result.pop("_artifact_event")

            yield {"type": "tool_result", "step": step, "id": call["id"],
                   "name": call["name"],
                   "output_preview": _make_preview(result),
                   "duration_ms": duration_ms,
                   "error": "error" in result}

            # 紧随 tool_result 之后发射 artifact_event（前端用于刷新侧边面板）
            if artifact_ev_payload is not None:
                yield {"type": "artifact_event", "step": step,
                       "tool_call_id": call["id"], **artifact_ev_payload}

            # 截断后回灌到 messages
            result_str = _truncate_tool_output(result)
            messages.append({"role": "tool", "tool_call_id": call["id"],
                              "content": result_str})

    else:  # 走到 MAX_STEPS 还在调工具
        forced_summary = True
        actual_steps = MAX_STEPS
        yield {"type": "status", "message": f"已达最大步数 {MAX_STEPS}，强制总结..."}
        messages.append({"role": "user",
            "content": "你已经达到最大工具调用次数。请基于上面已经收集到的所有工具结果，"
                       "给出最终答案。**不要再调用任何工具**。如果信息不足，明确说明"
                       "'根据已检索信息无法完整回答'，并总结已找到的相关内容。"})
        # 截断 + 流式调一次（不带 tools）
        messages, _ = _trim_messages_to_budget(messages, model)
        # ... 走 stream，把文本累积到 final_text_parts ...

    # 构造 sources（按话题去重，最多 5 条）
    sources = _build_sources(db, cited_message_ids)

    # 计算预估费用 — 主/子分模型计价
    main_model = settings.llm_model_qa
    sub_model = settings.effective_sub_agent_model
    has_sub = sub_agent_usage["total_tokens"] > 0

    main_usage = {k: max(cumulative_usage[k] - sub_agent_usage[k], 0)
                   for k in cumulative_usage}
    main_cost = llm_adapter.estimate_cost(main_model, **main_usage)
    sub_cost = llm_adapter.estimate_cost(sub_model, **sub_agent_usage) if has_sub else 0
    total_cost = round(main_cost + sub_cost, 6)

    yield {
        "type": "final_answer",
        "answer": "".join(final_text_parts),
        "sources": sources,
        "task_usage": {
            "main": {"model": main_model, **main_usage, "cost_yuan": main_cost},
            **cumulative_usage,
            "estimated_cost_yuan": total_cost,
            "model": main_model,
            "steps": actual_steps,
            "max_steps": MAX_STEPS,
            "forced_summary": forced_summary,
            **({"sub": {"model": sub_model, **sub_agent_usage, "cost_yuan": sub_cost}}
                if has_sub else {}),
        },
    }
```

### 8.3 流式 LLM 调用 `_stream_llm_step`

返回事件流：
- `text_delta` — 文本 token
- `reasoning_delta` — Kimi 思考链 token
- `tool_calls` — 完整聚合后的工具调用列表
- `reasoning_content` — 完整思考链（供回传上下文）
- `usage` — token 用量
- `done` — 流结束

```python
async def _stream_llm_step(messages, model):
    client = llm_adapter.get_client_for_model(model)
    is_kimi = llm_adapter.is_kimi_model(model)
    sem = llm_adapter.get_chat_semaphore(model)

    # 显式缓存注入
    effective_messages = messages
    if (settings.enable_qwen_explicit_cache
            and llm_adapter.is_qwen_model(model)
            and not is_kimi):
        effective_messages = llm_adapter.inject_cache_control(messages)

    kwargs = dict(
        model=model,
        messages=effective_messages,
        tools=ORCHESTRATOR_TOOL_SCHEMAS,
        tool_choice="auto",
        stream=True,
        stream_options={"include_usage": True},  # 关键：让最后一个 chunk 带 usage
    )

    if is_kimi:
        kwargs.update(llm_adapter.kimi_chat_kwargs(model, True))  # 思考模式
        # Kimi 不支持 parallel_tool_calls 参数
    else:
        kwargs["parallel_tool_calls"] = True
        kwargs["temperature"] = 0.3

    last_usage = None

    async with sem:
        stream = await client.chat.completions.create(**kwargs)
        tool_calls_acc: dict[int, dict] = {}
        reasoning_parts: list[str] = []

        async for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                last_usage = usage

            choice = chunk.choices[0] if chunk.choices else None
            if not choice:
                continue
            delta = choice.delta

            # Kimi 思考链单独字段
            rc = getattr(delta, "reasoning_content", None)
            if rc:
                reasoning_parts.append(rc)
                yield {"type": "reasoning_delta", "text": rc}

            if delta.content:
                yield {"type": "text_delta", "text": delta.content}

            # tool_calls 是分片增量，按 index 累积
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

    yield {"type": "usage", "usage": llm_adapter.parse_usage(last_usage)}
    yield {"type": "done"}
```

> ⚠️ **`stream_options.include_usage` 的坑**：开启后 usage 事件在 `finish_reason` 之后的下一个 chunk 才出现，**不能 break on finish_reason**，否则拿不到 usage。让 `async for` 自然结束才是对的。

### 8.4 上下文预算管理 `_trim_messages_to_budget`

```python
def _trim_messages_to_budget(messages, model):
    max_ctx = llm_adapter.get_context_window(model)
    budget_tokens = max_ctx - CONTEXT_RESERVE_TOKENS
    est = _estimate_tokens(messages)
    if est <= budget_tokens:
        return messages, False

    # 按比例截断 tool 消息（保留 system / user / assistant）
    excess_chars = int((est - budget_tokens) * CHARS_PER_TOKEN)
    tool_indices = [(i, len(m["content"]))
                    for i, m in enumerate(messages) if m.get("role") == "tool"]
    if not tool_indices:
        return messages, False

    total_tool_chars = sum(s for _, s in tool_indices)
    if total_tool_chars <= excess_chars:
        # 极端情况：tool 消息全砍也不够，按比例砍
        for i, _ in tool_indices:
            messages[i] = {**messages[i], "content": "[内容已截断]"}
        return messages, True

    for i, size in tool_indices:
        cut = int(excess_chars * size / total_tool_chars)
        new_size = max(size - cut, 1000)  # 至少保留 1000 字
        messages[i] = {**messages[i],
                        "content": messages[i]["content"][:new_size] + "\n\n...[截断]"}
    return messages, True
```

### 8.5 时间戳 + Artifact 摘要前缀注入

每次提问时（`run_registry._run_worker`）：

```python
parts = [build_current_time_hint()]
# build_current_time_hint() 返回：
# "[系统提示：当前时间是 2026-05-05 14:05（周一，北京时间 UTC+8）]"

artifact_summary = _build_artifact_summary(db, run.session_id)
# _build_artifact_summary() 返回：
# "[当前会话已有 artifacts]\n- key: tech-summary | 标题: 技术讨论汇总 | 当前 v3 | 长度 5234 字符\n..."

if artifact_summary:
    parts.append(artifact_summary)
injected_prefix = "\n\n---\n\n".join(parts)

# 关键：prefix 不存到 ChatTurn.content，而是存到 meta["injected_prefix"]
# 这样 content 保持纯净的用户问题（前端 / 搜索 / 导出 都用 content）
# 但 LLM 看到的 user content = prefix + content
session_service.append_turn(
    db, sid, role="user", content=question,
    meta={"injected_prefix": injected_prefix},
)

augmented_user_content = f"{injected_prefix}\n\n---\n\n{question}"

# 历史重放时也用相同的拼接方式（session_service.get_history_messages）
# → 前缀缓存命中
```

> **为什么这样做**：
> - 让 Agent 知道当前时间（处理"最近一周"等相对表达）
> - 已有 artifacts 让 Agent 不会重复创建同主题文档
> - 用户/前端不应看到这些技术细节
> - 历史重放时 prefix 是"当时"的快照（snapshot），不会因 artifact 后来的更新而变化 → 前缀缓存命中率最高

---

> 下一篇：[`05-tools-and-subagent.md`](./05-tools-and-subagent.md) — 工具集 + 子 Agent + 主/子 Agent system prompt 全文。
