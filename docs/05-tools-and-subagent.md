# 聊天记录管理迁移文档（5/6）工具集 + 子 Agent + Prompt 全文

> 上一篇 [`04-agent-core.md`](./04-agent-core.md) 介绍了 Agent 主循环。本篇深入工具定义、Dispatcher、子 Agent 实现，以及主/子 Agent 的 system prompt 全文。

## 9. 工具定义与 Dispatcher

### 9.1 工具列表（`qa_tools.TOOL_SCHEMAS`）

| 工具 | 用途 | 关键参数 |
|------|------|---------|
| `list_chats` | 列出所有已导入群聊 | — |
| `semantic_search` | 向量语义检索（**首选**） | query, chat_ids, topic_ids, dates, senders, limit, min_messages_in_chunk |
| `keyword_search` | FTS5 关键词搜索（精确） | keywords (OR 拼接), filters, limit |
| `fetch_messages` | 按 id 取原文 + 邻居 | message_ids, full_text, context_window |
| `fetch_topic_context` | 拉整个话题 | topic_id, limit |
| `search_by_sender` | 按发言人查 | senders, keywords, dates, chat_ids |
| `search_by_date` | 按日期查 | start_date, end_date, filters |
| `list_topics` | 列话题（侦察利器） | chat_ids, dates, category, limit |
| `get_user_profile` | 调 Telegram API 拉用户主页 | sender_id 或 username |
| `research` | 派子 Agent 检索 | task, scope, filters, expected_output, max_steps |
| `create_artifact` | 新建 artifact | artifact_key, title, content |
| `update_artifact` | str_replace 编辑 | artifact_key, old_str, new_str |
| `rewrite_artifact` | 整体重写 | artifact_key, content, title? |
| `list_artifacts` | 列已有 artifacts | — |
| `read_artifact` | 读完整正文 | artifact_key, version? |

### 9.2 工具 schema 示例

```python
{
    "type": "function",
    "function": {
        "name": "semantic_search",
        "description": "**首选检索工具**。用向量相似度语义搜索相关消息片段。"
                       "适合自然语言查询、找概念/主题相关内容。"
                       "返回的每个结果是一个消息片段（可能含多条消息），带 message_ids、topic_id、participants。"
                       "**针对'调研型'问题（统计/对比/全面梳理）建议设较大 limit=50~150**，"
                       "精确事实问题 limit=10~20 即可。"
                       "支持多维交叉过滤（chat_ids / topic_ids / 日期 / senders）以精确缩小检索空间。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "自然语言查询，建议直接用用户原问题或其变体"},
                "chat_ids": {"type": "array", "items": {"type": "string"},
                             "description": "可选：限定在这些群聊 ID 中搜索"},
                "limit": {"type": "integer", "default": 30,
                          "description": "范围 1-200。简单问题 10-20，调研性问题 50-150"},
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                "topic_ids": {"type": "array", "items": {"type": "integer"}},
                "senders": {"type": "array", "items": {"type": "string"},
                            "description": "可选：只保留含这些发言人的 chunk（SQL post-filter）"},
                "min_messages_in_chunk": {"type": "integer",
                                            "description": "可选：只保留消息数 ≥ 该值的 chunk"},
            },
            "required": ["query"],
        },
    },
}
```

> **关键 — description 写得越详细 LLM 用得越好**：每个 description 包括"何时用 / 何时不用 / 注意事项"。这是 prompt engineering 的一部分，**不要省略**。

### 9.3 Dispatcher

```python
async def dispatch_tool(db, name, args, event_callback=None, context=None):
    args = _normalize_tool_args(name, args or {})  # 老参数名兼容（如 top_k → limit）

    # 1. research 工具特殊处理（调子 Agent）
    if name == "research":
        from backend.services.sub_agent import run_sub_agent
        filters = args.get("filters") or {}
        effective_chat_ids = filters.get("chat_ids") or args.get("chat_ids")
        try:
            return await run_sub_agent(
                db=db, task=args.get("task", ""),
                chat_ids=effective_chat_ids,
                scope=args.get("scope"),
                filters=filters if filters else None,
                expected_output=args.get("expected_output"),
                max_steps=args.get("max_steps"),
                event_callback=event_callback,
            )
        except Exception as e:
            return {"error": f"子 Agent 执行失败: {e}",
                    "suggestion": "可尝试简化 task 描述，或把 task 拆成更小的 research"}

    # 2. Artifact 工具 — 需要 session_id
    artifact_handler = ARTIFACT_TOOL_HANDLERS.get(name)
    if artifact_handler:
        session_id = (context or {}).get("session_id")
        if not session_id:
            return {"error": "Artifact 工具必须在会话上下文中调用",
                    "code": "no_session"}
        try:
            return await artifact_handler(db, session_id=session_id, **args)
        except TypeError as e:
            return {"error": f"参数错误: {e}", "code": "bad_args",
                    "suggestion": "检查参数名和类型..."}

    # 3. 普通检索工具
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return {"error": f"未知工具: {name}",
                "suggestion": f"可用工具：{sorted(...)}"}
    try:
        return await handler(db, **args)
    except TypeError as e:
        # 列出该工具的合法参数名做 suggestion
        schema = next((s for s in TOOL_SCHEMAS if s["function"]["name"] == name), None)
        valid_params = list(schema["function"]["parameters"].get("properties", {}).keys()) if schema else []
        return {"error": f"参数错误: {e}",
                "suggestion": f"{name} 合法参数：{valid_params}"}
```

> **每个错误都带 `suggestion`**：让 LLM 知道怎么修正。这显著降低 LLM 重复犯错的概率。

### 9.4 关键工具实现要点

#### `tool_semantic_search`

```python
async def tool_semantic_search(db, query, chat_ids=None, limit=30,
                                start_date=None, end_date=None,
                                topic_ids=None, senders=None,
                                min_messages_in_chunk=0):
    limit = max(1, min(int(limit), 200))
    chat_ids = _coerce_str_list(chat_ids) or None  # 宽容转换：单 str / list / None
    where_filter = _build_chroma_where(chat_ids, topic_ids, start_date, end_date)

    # senders / min_messages 是 post-filter；启用时预取更多
    fetch_n = limit
    if senders or min_messages_in_chunk > 0:
        fetch_n = min(limit * 4, 200)

    results = await search_similar(query, n_results=fetch_n, where=where_filter)

    items = []; all_msg_ids = []
    for r in results:
        meta = r.get("metadata", {})
        msg_ids_raw = meta.get("message_ids", [])
        if isinstance(msg_ids_raw, str):
            # 兼容 JSON / Python repr
            try:
                msg_ids_raw = json.loads(msg_ids_raw)
            except Exception:
                import ast
                msg_ids_raw = ast.literal_eval(msg_ids_raw)
        msg_ids = [int(x) for x in msg_ids_raw]
        items.append({
            "chunk_preview": (r.get("document") or "")[:1000],
            "distance": round(r.get("distance") or 0, 4),
            "chat_id": meta.get("chat_id"),
            "topic_id": meta.get("topic_id"),
            "start_date": meta.get("start_date"),
            "end_date": meta.get("end_date"),
            "participants": meta.get("participants"),
            "message_ids": msg_ids[:30],
            "total_messages_in_chunk": len(msg_ids),
        })
        all_msg_ids.extend(msg_ids)

    if senders and all_msg_ids:
        matched = await asyncio.to_thread(_post_filter_msgs_by_sender, db, all_msg_ids, senders)
        items = [it for it in items if any(mid in matched for mid in it["message_ids"])]

    if min_messages_in_chunk > 0:
        items = [it for it in items if it["total_messages_in_chunk"] >= min_messages_in_chunk]

    return {"results": items[:limit], "count": len(items)}
```

#### `tool_keyword_search`（FTS5 + Rerank）

```python
async def tool_keyword_search(db, keyword=None, keywords=None, ...):
    fts_query = " OR ".join(kws)
    primary_kw = kws[0]

    def _fts_then_like():
        try:
            rows = db.execute(
                text("SELECT rowid FROM messages_fts WHERE messages_fts MATCH :kw LIMIT :lim"),
                {"kw": fts_query, "lim": fetch_limit},
            ).fetchall()
            if rows:
                ids = [r[0] for r in rows]
                msgs = _apply_filters(db.query(Message).filter(Message.id.in_(ids))).all()
                return msgs, "fts5"
        except Exception:
            pass
        # FTS 0 命中 → 回退 LIKE
        kw_clean = primary_kw.split(" OR ")[0].split()[0].strip('"')
        msgs = _apply_filters(db.query(Message).filter(Message.text_plain.like(f"%{kw_clean}%"))).all()
        return msgs, "like"

    msgs, used_method = await asyncio.to_thread(_fts_then_like)

    # Rerank
    if len(msgs) > 1:
        try:
            docs = [(m.text_plain or "")[:500] for m in msgs]
            rerank_results = await llm_adapter.rerank(query=fts_query, documents=docs,
                                                       top_n=min(limit, len(msgs)))
            msgs = [msgs[r["index"]] for r in rerank_results]
            used_method += "+rerank"
        except Exception:
            pass

    return {"results": [_msg_to_dict(m) for m in msgs[:limit]],
            "count": len(msgs), "method": used_method}
```

> **FTS5 0 命中时回退 LIKE 是核心可用性保障**：trigram FTS 对 1-2 字短词偶尔会 miss，LIKE 兜底能避免 Agent 因为搜不到任何东西卡住。

#### `tool_fetch_messages` + `context_window`

```python
async def tool_fetch_messages(db, message_ids, full_text=False, limit=None, context_window=0):
    target_ids = message_ids[:eff_limit]
    context_window = max(0, min(int(context_window or 0), 20))

    def _q():
        base = db.query(Message).filter(Message.id.in_(target_ids)).order_by(Message.date).all()
        if context_window <= 0 or not base:
            return base, []
        # 按 chat_id 分组拉前后 N 条邻居
        extra_ids = set()
        for m in base:
            before = db.query(Message.id).filter(
                Message.chat_id == m.chat_id, Message.date < m.date, Message.id != m.id
            ).order_by(Message.date.desc()).limit(context_window).all()
            after = db.query(Message.id).filter(
                Message.chat_id == m.chat_id, Message.date > m.date, Message.id != m.id
            ).order_by(Message.date).limit(context_window).all()
            extra_ids.update(b[0] for b in before)
            extra_ids.update(a[0] for a in after)
        ctx = db.query(Message).filter(Message.id.in_(extra_ids - {m.id for m in base})).all()
        return base, ctx

    msgs, ctx_msgs = await asyncio.to_thread(_q)
    return {"messages": [...], "context_messages": [...],
            "count": len(msgs), "context_count": len(ctx_msgs)}
```

> **`context_window` 是 sub-agent 还原语境的关键工具**：单条消息的 snippet 经常误导，看前后 N 条才能确定真实含义。

### 9.5 Artifact 工具的特殊设计

Artifact 工具的返回里会塞一个 `_artifact_event` 字段，用 `pop` 在 Agent 主循环里剥掉，避免回灌到 LLM 上下文（节省 token），但同时通过 `yield {"type":"artifact_event", ...}` 发给前端用于刷新侧边面板：

```python
async def tool_create_artifact(db, *, session_id, artifact_key, title, content):
    art, ver = await asyncio.to_thread(_do)
    return {
        "ok": True,
        "artifact_key": art.artifact_key,
        "title": art.title,
        "version": ver.version,
        "content_length": len(content),
        "_artifact_event": _artifact_op_event("created", art, ver),  # ← 私有字段
    }

# 主 Agent 循环里：
artifact_ev_payload = None
if isinstance(result, dict) and "_artifact_event" in result:
    artifact_ev_payload = result.pop("_artifact_event")  # ← 剥掉

yield {"type": "tool_result", ...}  # 这里的 result 已经没有 _artifact_event 了
if artifact_ev_payload is not None:
    yield {"type": "artifact_event", **artifact_ev_payload}  # 单独发给前端
```

---

## 10. 子 Agent (research) — `services/sub_agent.py`

### 10.1 配置

```python
SUB_MAX_STEPS_DEFAULT = 12             # 默认轮数
SUB_MAX_STEPS_HARD_CAP = 20            # 硬上限
MAX_TOOL_OUTPUT_CHARS = 40_000         # 单次工具输出上限
MAX_LIST_ITEMS_PER_TOOL = 60           # list 类输出条目上限
SUB_CONTEXT_SOFT_LIMIT_TOKENS = 100_000  # 上下文软上限（防 qwen3.5-plus 进 128K 高价档）
_CHARS_PER_TOKEN = 1.8

# 子 Agent 不能用 research（防递归）和 artifact（不在 session 上下文）
_SUB_EXCLUDED_TOOLS = {"research", "create_artifact", "update_artifact",
                        "rewrite_artifact", "list_artifacts", "read_artifact"}
SUB_TOOL_SCHEMAS = [t for t in TOOL_SCHEMAS if t["function"]["name"] not in _SUB_EXCLUDED_TOOLS]

_FILTER_INJECTABLE_TOOLS = {"semantic_search", "keyword_search",
                             "search_by_sender", "search_by_date", "list_topics"}
_INJECTABLE_FILTER_KEYS = {"chat_ids", "topic_ids", "senders", "start_date", "end_date"}
```

### 10.2 自适应步数

```python
def _auto_max_steps(task, expected_output, override):
    if override and override > 0:
        return min(override, SUB_MAX_STEPS_HARD_CAP)

    text = (task or "") + " " + (expected_output or "")
    # 复杂信号 → 16 步
    if any(s in text.lower() for s in ["汇总", "对比", "比较", "timeline", "时间线",
                                        "按月", "按周", "按人", "按群", "跨群",
                                        "梳理", "整理", "综述", "统计", "分布",
                                        "验证", "交叉"]):
        return min(16, SUB_MAX_STEPS_HARD_CAP)
    # 短任务 → 8 步
    if len(text) < 200 and not expected_output:
        return 8
    return SUB_MAX_STEPS_DEFAULT  # 12
```

### 10.3 智能截断（`_truncate_tool_output`）

```python
def _truncate_tool_output(obj):
    """优先级：list 字段截条目 > 字符级截断"""
    if not isinstance(obj, dict):
        return json.dumps(obj)[:MAX_TOOL_OUTPUT_CHARS]

    # 第一步：list 字段截条目（保持 JSON 结构）
    truncated_obj = dict(obj)
    truncation_meta = {}
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
            "结果较多，已展示前 N 条；如需更多，可缩小过滤范围（加日期/发言人/topic_ids）"
        )

    # 第二步：字符级 backstop
    s = json.dumps(truncated_obj, ensure_ascii=False)
    if len(s) > MAX_TOOL_OUTPUT_CHARS:
        s = s[:MAX_TOOL_OUTPUT_CHARS] + f'\n\n...[字符级截断 backstop，完整长度 {len(s)} 字符]'
    return s
```

> **为什么按条目截而非简单字符截**：让 Agent 看到"还有多少条没展示"，而不是 JSON 中间被砍断 → JSON parse error。

### 10.4 子 Agent user prompt 构造

```python
def _build_user_prompt(task, scope, filters, expected_output):
    parts = [build_current_time_hint(), f"\n[Task]\n{task}"]
    if scope:
        parts.append(f"\n[Scope]\n{scope}")
    if filters:
        clean = {k: v for k, v in filters.items()
                 if k in _INJECTABLE_FILTER_KEYS and v}
        if clean:
            parts.append(f"\n[Filters]\n{json.dumps(clean, ensure_ascii=False)}")
    if expected_output:
        parts.append(f"\n[Expected Output]\n{expected_output}")
    return "\n".join(parts)
```

### 10.5 Filters 自动注入

```python
def _inject_filters_into_args(tool_name, args, filters):
    """子 Agent 没显式给该字段时才补"""
    if not filters or tool_name not in _FILTER_INJECTABLE_TOOLS:
        return args
    new_args = dict(args)
    for k in _INJECTABLE_FILTER_KEYS:
        v = filters.get(k)
        if v and k not in new_args:
            new_args[k] = v
    return new_args
```

> **为什么需要**：子 Agent 用便宜模型，**经常忘记把约束传给工具**。自动补上能避免无效搜索（搜了所有群、再人工过滤）。

### 10.6 子 Agent 主循环

```python
async def run_sub_agent(db, task, chat_ids=None, scope=None, filters=None,
                        expected_output=None, max_steps=None, event_callback=None):
    model = settings.effective_sub_agent_model
    step_cap = _auto_max_steps(task, expected_output, max_steps)
    user_content = _build_user_prompt(task, scope, effective_filters, expected_output)

    messages = [
        {"role": "system", "content": SUB_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    cited_message_ids = set()
    total_tool_calls = 0
    cumulative_usage = {...}
    soft_limit_hit = False

    for step in range(1, step_cap + 1):
        # 上下文软上限静默检查（不暴露给模型，避免诱发偷懒）
        if _estimate_messages_tokens(messages) > SUB_CONTEXT_SOFT_LIMIT_TOKENS:
            await _emit({"type": "sub_status", "message": "进入总结阶段"})
            soft_limit_hit = True
            break

        await _emit({"type": "sub_step", "step": step})

        # 流式 LLM 调用
        step_text = ""; step_tool_calls = []; step_reasoning = ""
        try:
            async for ev in _stream_sub_llm(messages, model):
                if ev["type"] == "text_delta": step_text += ev["text"]
                elif ev["type"] == "reasoning_content": step_reasoning = ev["text"]
                elif ev["type"] == "tool_calls": step_tool_calls = ev["calls"]
                elif ev["type"] == "usage": _add_usage(cumulative_usage, ev["usage"])
                elif ev["type"] == "done": break
        except Exception as e:
            return {"report": step_text or f"子 Agent LLM 调用失败: {e}",
                    "message_ids": sorted(cited_message_ids), ...}

        # 构建 assistant 消息
        assistant_msg = {"role": "assistant", "content": step_text or None}
        if step_reasoning: assistant_msg["reasoning_content"] = step_reasoning
        if step_tool_calls:
            assistant_msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                              "function": {"name": c["name"],
                                                           "arguments": c["arguments"]}}
                                             for c in step_tool_calls]
        messages.append(assistant_msg)

        # 没 tool_calls → 最终报告
        if not step_tool_calls:
            return {"report": step_text, "message_ids": sorted(cited_message_ids),
                    "steps": step, "tool_calls_count": total_tool_calls,
                    "usage": cumulative_usage, "model": model}

        # 执行工具
        for call in step_tool_calls:
            args = json.loads(call["arguments"]) if call["arguments"] else {}
            args = _inject_filters_into_args(call["name"], args, effective_filters)
            t0 = time.time()
            result = await dispatch_tool(db, call["name"], args)
            duration_ms = int((time.time() - t0) * 1000)
            total_tool_calls += 1
            _collect_ids(result, cited_message_ids)

            await _emit({"type": "sub_tool", "step": step,
                          "name": call["name"], "duration_ms": duration_ms})
            messages.append({"role": "tool", "tool_call_id": call["id"],
                              "content": _truncate_tool_output(result)})

    # 强制总结
    force_msg = (
        "你已经收集到足够的资料。请基于上面所有工具结果，按 task 要求的结构输出最终报告。"
        "**不要再调用任何工具**。如果还有未覆盖的子方向（task 范围外的新线索），"
        "在报告末尾的 '## 越界线索' 区块列出，主 Agent 会决定是否再发 research 跟进。"
    )
    messages.append({"role": "user", "content": force_msg})
    resp = await client.chat.completions.create(
        model=model, messages=messages, stream=False, temperature=0.3
    )
    report = resp.choices[0].message.content or ""
    _add_usage(cumulative_usage, llm_adapter.parse_usage(resp.usage))

    return {"report": report, "message_ids": sorted(cited_message_ids),
            "steps": step_cap, "tool_calls_count": total_tool_calls,
            "usage": cumulative_usage, "model": model}
```

---

## 11. 主 Agent System Prompt 全文

完整内容（直接复制到迁移项目使用，无需改动）：

````markdown
你是一个 Telegram 聊天记录分析的智能助手（Orchestrator）。
你拥有直接检索工具 + 子 Agent 委派（research）+ Artifact 协同文档三类能力。
**你的角色偏重"规划 + 合成"**：检索的苦活尽量外包给 research，但**任务要拆得足够细**——
子 Agent 用 qwen3.5-plus，**128K 是它的关键性能阈值**：
- 上下文 < 128K：模型注意力集中，能精准引用、严守指令
- 上下文 ≥ 128K：注意力衰减明显（容易漏引用、跑偏指令、复读重复内容），且费用 2.5x ↑
- 上下文 ≥ 256K：质量塌方，费用 5x ↑
所以单个 research 任务要小到子 Agent **天然装不满 128K**——单次 ~100 条消息原文 ≈ 30K~50K tokens。

## 决策树：先判断问题类型

1. **单点事实 / 精确关键词**（"X 说过啥"、"包含 Y 的消息"、"某天的消息"）
   → 直接用检索工具 1~3 次搞定，不要派 research

2. **中等广度**（"最近讨论 GPU 的话题"、"讨论过哪几个方案"）
   → 先 `list_topics` 或 `semantic_search(limit=50~100)` 扫一次，够用就直接合成答案

3. **调研型 / 列举型 / 跨维度 / 跨群**（"全面梳理"、"找出所有 X"、"按人对比"、"timeline"）
   → **先侦察，后拆 research**（见下一节 "📍 侦察原则"）

## 📍 侦察原则：调研型任务的第一步总是侦察

**不要拍脑袋拆 research**——调研型问题的**第一个工具调用永远是侦察**：
- 不知道有哪些群聊 → `list_chats`
- 知道群聊、不知道话题分布 → `list_topics(chat_ids=[...], limit=50)`看话题概要
- 不确定该问题能不能检索到 → `semantic_search(limit=20)` 试探

**根据侦察结果决定拆几个 research**：
- 10 个相关话题内 → 1~2 个 research 够了
- 30~50 个话题 → 3~5 个 research 按话题子集拆
- 100+ 话题 → 按 topic_ids 拆、或按时间段拆、或两者组合

成本记账：`list_topics` 的调用仅 ≈ 100~500 tokens，但能让你拆对 research——这是最高 ROI 的工具调用。
**反面教材**：跳过侦察直接拆 5 个 research——可能有3个重叠的、或者拆了不存在的话题、或者遗漏主要话题。

## ⚠️ 关键规则：通过"横向多拆 research"实现完整搜索，而不是"单个 research 穷举"

**核心思路**：你想完整覆盖一个广话题，**正确做法是横向拆成多个 research 各管一片**，
而不是给一个子 Agent 说"找全部 X"——后者会让子 Agent 上下文爆炸 + 性能塌方。

**症状识别**：一个 research 跑了 > 60 秒、调用 > 12 次工具，几乎一定是任务太重——
子 Agent 试图穷举导致上下文撑到 128K+ 进入性能衰减区。**这种 research 的报告也会很烂**
（漏引用、复读、跑偏）。

**正确的拆分（每个 research 都"窄而完整"）**：
- 每个 research 的范围要**窄**：限定一个主题维度 + 一个关键词集合（3~5 个词）
- 在那个窄范围内，要让子 Agent **完整搜索**——不要写"代表性样本即可"这种偷懒话术
- 全局完整性靠**多个 research 横向覆盖** + **多轮迭代**实现，不靠单个 research 穷举

## 开放式多轮 research：两阶段思维 + 质量门槛 ✨

**research 不限于一轮、两轮——根据每轮结果决定下一轮，直到能完整回答用户问题再停**。
但不同轮次的任务性质不同：

### 第 1 轮 = 探索型（摸清版图）
目的是看清有哪些**品牌 / 关键词 / 话题 / 发言人**，可以用宽一点的 task：
- "在 chat_X 里找提及虚拟卡的话题，列出有哪些品牌名 / 平台名"
- "用 list_topics 看下近 3 个月讨论虚拟卡的话题，记下主要几个 topic_id"

### 第 2~N 轮 = 验证/拓展型（针对第 1 轮发现精查）
任务要更聚焦（已经知道在找什么）：
- "**验证型**：拉取 [msg:1234, 1567, 1890] 的完整原文 + 前后 3 条邻居（fetch_messages context_window=3），确认 EFunCard 是否支持 USDT 充值"
- "**拓展型**：搜 'monobank'、'Wise' 这两个新出现的卡平台，找它们的提及和用途"
- "**补维度型**：列出第一轮提到 EFunCard 的所有发言人 + 评价倾向（正/负/中性）"

### 收到每份报告先"打分"（质量门槛）
**报告到手后不要马上写入最终答案。先检查：**
- 引用数量 < 3 个 [msg:id] → 质量不足，应重发一个更聚焦的 research
- 报告主体是"未找到" / 字数 < 500 → 质量不足，重发（换关键词 / 缩范围 / 换工具）
- 报告跨出 task 范围（未在"## 越界线索"区块里汇报，而是在正文里乱谈）→ 重发、明确限定范围
- **最多重发 2 次同一个子任务**；仍质量不足就接受"该范围信息确实不多"并在最终答案里说明

### 每轮收到报告后的检查清单
- **稀薄**：某 research 只返回 1~3 条结果，但任务本身应该有更多 → 换关键词 / 换维度再发
- **缺引用**：报告说"群里讨论了 X 方案"但没给具体 [msg:id] → 发**验证 research** 拉原文
- **维度缺失**：用户问"哪些人在用 X"但报告只列了 X 没列人 → 发**补维度 research**
- **新线索**：报告里出现没想到的关键词/产品/人 → 发**拓展 research** 顺藤摸瓜
- **越界线索**：子 Agent 在 "## 越界线索" 区块汇报的新方向 → 决定要不要单独发 research 跟进
- **矛盾**：两个 research 给出冲突信息 → 发**交叉验证 research**

### 整体节奏
```
用户提问
  ↓
第 1 轮：3~5 个 research 并行（按主题/关键词集合维度横切）
  ↓
评估：稀薄? 缺引用? 缺维度? 越界线索? 矛盾?
  ↓
第 N 轮（N=2,3,4...）：按需补查 / 验证 / 拓展，每轮 1~3 个 research
  ↓
当信息能覆盖用户问题的所有维度，且关键事实都有引用 → 写最终答案
```
**判断"够了"的标准**：把最终答案的初稿在脑中过一遍——每个论点都有 [msg:id]、覆盖了用户问的所有维度、没有"我估计/可能/大概"这种含糊词——OK 了再停。
**判断"还要查"的标准**：你心里在用"应该"、"可能"、"听起来"这种猜测词——立即发补查 research，不要把不确定写进最终答案。

## research 拆分原则

### 列举/搜集型（"找出所有 X"、"哪些人提到 Y"）
按**关键词集合**或**子类别**横向切，每个 research 负责一个集合：
- 例：找虚拟卡 → research-A "找 EFunCard/Roogoo/野卡 等已知卡平台"
       + research-B "找 USDT/支付宝/Apple Pay 充值相关讨论"
       + research-C "找 GPT/Claude/Netflix 订阅卡相关讨论"
- **不要**用一个 research 说 "找所有提到的虚拟卡"——这会让子 Agent 反复穷举

### 对比型（"X 和 Y 哪个好"）
按**对比对象**纵向切：每个 research 负责一个对象的全部信息

### 时间线型
按**时间段**切：每个 research 负责 1~3 个月

### 多群聊场景
若涉及 ≥3 个群聊且每群消息量大，**先用 `list_chats` + `list_topics` 评估规模**，
然后按群聊或话题维度再切一层。

## research 调用规范
`research` 启动一个独立上下文的子 Agent。**每个 task 必须包含**：
1) **搜什么**：具体主题 + 3~5 个关键词（不要超过 5 个，否则范围太宽）
2) **分析维度**：时间线 / 按人 / 按平台 / 列表
3) **期望输出格式**：bullet / 表格 / "平台名 + 链接 + 用途" 三元组
4) **范围边界**：明确告诉子 Agent "在这个范围内尽量完整，超出范围的新线索写在'## 越界线索'区块"
5) **排除项**（可选）：避免方向跑偏

**结构化字段优先于 task 文本**：
- `filters` 字段：chat_ids / senders / date 直接传，比 task 里写更可靠（自动注入工具调用）
- `scope` 字段：自然语言说明范围
- `expected_output`：对输出结构的硬性要求
- `max_steps`：默认按任务自适应（8/12/16）。一般不用显式给——任务范围窄了步数自然就够

**并行**：独立的 research 必须在**同一个 assistant 回合里**同时发起（一次返回多个 tool_calls）。

## 检索工具速查（直接用 / 给 research 参考）
- **semantic_search**：语义检索首选。chat_ids / topic_ids / 日期 / senders / min_messages_in_chunk 过滤。调研 limit=50~100，精确 10~30
- **keyword_search**：精确词 / FTS5。keywords 列表 OR 合并
- **list_topics**：列话题（带 summary + 时间段）——拿 topic_id 后传给 semantic_search/fetch_topic_context 深挖
- **list_chats**：评估群聊规模时先看看
- **fetch_topic_context**：整话题原文
- **fetch_messages**：按 id 拉原文 + 可选 `context_window` 拉前后 N 条邻居
- **search_by_sender / search_by_date**：按人 / 按日期
- **get_user_profile**(sender_id 或 username)：调 Telegram API 拉**实时**用户主页

## Artifact：调研型问题默认产出侧边文档

**调研 / 列举 / 对比 / 梳理 型问题默认用 artifact**：详细内容写 artifact、
对话区只放 3~5 句摘要 + "已生成 artifact 《标题》"。这能：
- 大幅减少对话区 token （最终答案不是 5K+ 而是 200~500）
- 提高后续轮会话的缓存命中率
- 用户可复制/导出、迭代修改

**例外**：用户明确说"快速告诉我" / "TL;DR" / "一句话回答" 时不用 artifact。单点事实也不用。

### ✨ session 启动时已注入 artifacts 摘要
**每次对话开始时**，user message 前缀会自动注入"## 当前 session 已有 artifacts (N 篇)"
段——你启动就能看到当前 session 有哪些 artifacts（key / title / 字符数 / 预览）。
**用户的新问题如果与某个已有 artifact 同主题，优先 update / rewrite，不要建重复主题的新 artifact**。

### 工具
- **create_artifact**(artifact_key, title, content) — 新建（key 同 session 不可重复）
- **update_artifact**(artifact_key, old_str, new_str) — 增量修改，old_str 必须在正文**恰好出现一次**
- **rewrite_artifact**(artifact_key, content[, title]) — 整体重写（代价大、慎用）
- **list_artifacts**() — 列当前 session 所有 artifacts（启动注入摘要后一般不必再调）
- **read_artifact**(artifact_key, version?) — 读完整正文（update 前确定锚点 / 查看历史结论时用）

**判断同主题 vs 新主题的标准**：
- 用户问"在原报告里加 X"、"修一下"、"补充 Y" → **update / rewrite 已有 artifact**
- 用户问完全新方向（不同群聊、不同主题）→ **create 新 artifact**（用不同 key）
- 不确定时先 `read_artifact(key)` 看现有内容再决定

同一 session 可以有多篇 artifact（独立主题分开建，artifact_key 用英文小写 slug）。
小改动优先 update，大重构才用 rewrite。

## 最终答案规范
- Markdown 格式，分段清晰
- **每个事实标注来源**：发言人 + 日期；引用用 `[msg:123]` 或 `[topic:456]` 锚点
- 数据不足时直说"未找到 X"+ 列已查过的关键词/过滤器——**不要编造**
- 多 research 间矛盾时指出来，不要掩盖

### 不确定性分级（诚实表达证据强度）
不要把一条证据说成"定论"。用三档表达信心：
- **已确认**（默认）：≥ 2 条独立消息提到、有具体细节、能 cross-reference → 直接陈述，带 [msg:id]
- **疑似**：仅 1 条提及 / 只有间接证据 → 用"疑似 / 看起来 / 可能"，带 [msg:id]
- **推测**：基于上下文推断、无直接消息 → 用"推测 / 估计"明确标出，不伪装成事实

例："EFunCard 支持 USDT 充值 [msg:1234][msg:1567]；疑似也支持其它加密货币 [msg:1890]；推测面向业内用户（根据讨论语境）。"

你最多 {max_steps} 轮工具调用，超出强制总结。**能外包就外包、外包就拆细、能并行就并行**。
````

---

## 12. 子 Agent System Prompt 全文

完整内容：

````markdown
你是一个 Telegram 聊天记录检索子助手。
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
````

---

> 下一篇：[`06-runtime-and-artifact.md`](./06-runtime-and-artifact.md) — Run/Session/Artifact 服务 + 工程实践 + 迁移 Checklist。
