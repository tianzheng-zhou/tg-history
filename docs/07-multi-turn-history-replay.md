# 聊天记录管理迁移文档（7/7）多轮对话完整工具结果回放

> 此章节是**项目演进过程中**针对"多轮对话 Agent 看不到前几轮工具调用结果"的问题做的一次结构性改造，参考 Claude Code / OpenCode 风格——把每轮 `tool_calls` 和完整 `tool_results` 都还原回 `messages`，让 Agent 跨轮拥有完整记忆。
>
> 本章是 [`04-agent-core.md`](./04-agent-core.md) 和 [`06-runtime-and-artifact.md`](./06-runtime-and-artifact.md) 的扩展，**迁移到新项目时如果你也想要 Agent 的多轮记忆，这套机制可以直接复用**。

## 1. 背景：旧版的盲点

改造前 `session_service.get_history_messages` 只回放纯文本：

```python
# 改造前
def get_history_messages(db, session_id):
    turns = get_turns(db, session_id)
    result = []
    for t in turns:
        if t.role not in ("user", "assistant") or not t.content:
            continue
        # ... 只取 t.content 拼回 messages
        result.append({"role": t.role, "content": content})
    return result
```

**症状**：
- 第 1 轮 Agent 调用 `keyword_search(["EFunCard"])` 找到了 5 条消息，写出最终答案 A1
- 第 2 轮用户问「再深挖一下刚才那些卡的充值方式」
- Agent 第 2 轮看到的 messages 是：`system + user1 + assistant_text(A1) + user2`
  - **完全不知道**第 1 轮搜过 `EFunCard` 这个关键词、命中了哪些 `message_id`、当时拉到的原文长什么样
- 结果：Agent 要么瞎猜（答得不准），要么把同样的搜索再做一遍（浪费 token + 时间）

**根因**：
- `_build_trajectory` 已经把 `tool_calls[{id, name, args}]` 持久化到 `ChatTurn.trajectory`
- 但 `tool_calls[i].preview` 是 `_make_preview()` 出来的紧凑摘要（`{count: 30, items: [前3条]}`），**不是完整 tool_results**
- 完整工具结果只在 run 内存的 `messages: list[dict]` 里流转，**run 结束后就丢了**

## 2. 业界对比

| 项目 | 多轮记忆机制 | 上下文管理 |
|------|------------|-----------|
| **Claude Code** | 完整保留所有 messages（含 `tool_use` / `tool_result` blocks） | Anthropic prompt caching；接近上限触发 `/compact` |
| **OpenCode (sst)** | 完整持久化所有 messages | 同 Claude Code 风格 |
| **Cursor** | 自动总结老轮次 | 滑动窗口 |
| **Cline** | Sliding window | 超 token 阈值砍最旧 |

我们项目特殊性：
- 工具结果体积大（一次 `keyword_search(limit=80)` ≈ 30K-50K tokens 原文）
- 用 Qwen/Kimi 256K 上下文 + 已开显式缓存
- 子 Agent 有 100K 软上限 backstop

最终选择 **Claude Code 风格的完整重放**：依靠显式缓存让重复 prefix 几乎免费、依靠现有的 `_trim_messages_to_budget` 在突破 256K 时按比例砍 tool 消息兜底。

## 3. 改造架构

```
┌─────────────────────── qa_agent.run_agent ───────────────────────┐
│                                                                   │
│  执行 tool → 得到 result_str (≤50K chars by _truncate_tool_output)│
│                                                                   │
│  yield {"type": "tool_result",                                    │
│         "id": tool_call_id,                                       │
│         "output_preview": _make_preview(result),  ← 给前端 UI    │
│         "output_full": result_str}                ← 给持久化      │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
┌────────────────────── run_registry._emit ─────────────────────────┐
│                                                                   │
│  if event["type"] == "tool_result" and "output_full" in event:    │
│      run.tool_outputs[event["id"]] = event.pop("output_full")     │
│      # 大字段不再随 event 进 run.events / SSE 队列                │
│                                                                   │
│  event["seq"] = run.seq; run.events.append(event)                 │
│  for q in subscribers: q.put_nowait(event)  # 前端只看到 preview  │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
┌────────────────────── _build_trajectory(run) ─────────────────────┐
│                                                                   │
│  steps = ...从 events 重组（thinking / tool_calls / preview）...  │
│  for s in steps:                                                  │
│      for tc in s["tool_calls"]:                                   │
│          if tc["id"] in run.tool_outputs:                         │
│              tc["output"] = run.tool_outputs[tc["id"]]   ← 合并   │
│  return {"steps": steps}                                          │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
                  ChatTurn.trajectory (DB JSON)
                               │
                               ▼
┌─────────────── session_service.get_history_messages ──────────────┐
│  for each ChatTurn:                                               │
│    user → {role:user, content: prefix + content}                  │
│    assistant + has full output → _replay_assistant_turn_full()    │
│        → [{role:assistant, tool_calls:[...]},                     │
│           {role:tool, tool_call_id, content: tc.output},          │
│           {role:tool, tool_call_id, content: tc.output},          │
│           {role:assistant, content: 最终答案}]                    │
│    assistant + 老格式      → {role:assistant, content: t.content} │
└───────────────────────────────────────────────────────────────────┘
```

## 4. 改动文件清单（4 处）

| # | 文件 | 改动 |
|---|------|------|
| 1 | `backend/config.py` | 新增 `enable_full_history_replay: bool = True` feature flag |
| 2 | `backend/services/qa_agent.py` | 3 处 `yield tool_result` 事件附加 `output_full` 字段（完整工具结果 JSON） |
| 3 | `backend/services/run_registry.py` | `Run` 加 `tool_outputs` dict、`_emit` 剥离 `output_full`、`_build_trajectory` 合并到 `tool_calls[i].output` |
| 4 | `backend/services/session_service.py` | 重写 `get_history_messages`，从 trajectory 还原完整 OpenAI messages 序列 |

### 4.1 `qa_agent.py` — `output_full` 字段

3 个位置（参数 JSON 解析失败 / `asyncio.gather` 异常 / 正常工具结果）都加：

```python
# 正常工具结果路径
result_str = _truncate_tool_output(result)  # 已经截到 ≤ MAX_TOOL_OUTPUT_CHARS=50000
yield {
    "type": "tool_result",
    "step": step,
    "id": call["id"],
    "name": call["name"],
    "output_preview": _make_preview(result),
    # 完整结果供下游持久化到 trajectory.tool_calls[i].output（多轮对话重放用）
    # _emit 会在入 run.events 前 pop 走、不推送给前端 SSE 订阅者
    "output_full": result_str,
    "duration_ms": duration_ms,
    "error": "error" in result,
}
```

> **设计要点**：`output_full` 复用 `_truncate_tool_output` 已经截到 50K 字符的版本，**不需要额外截断逻辑**。错误路径下用 `json.dumps(err_result, ensure_ascii=False)` 保证字符串类型一致。

### 4.2 `run_registry.py` — Run dataclass + _emit

```python
@dataclass
class Run:
    # ... existing fields ...
    error: str | None = None

    # tool_call_id → 完整 tool_result JSON 字符串。不走 run.events 避免给 SSE 订阅者
    # 反复推送 50KB+ 大负载；build_trajectory 时合并进 tool_calls[i].output 持久化。
    tool_outputs: dict = field(default_factory=dict)


def _emit(run: Run, event: dict) -> None:
    """特殊处理：tool_result 事件里的 output_full（可能 50KB 量级）
    不走 run.events 也不推给 SSE 订阅者，只存到 run.tool_outputs[tool_call_id]。
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
            pass
```

> **关键设计**：把大字段从 event 中 pop 出来存到 `run.tool_outputs`（key=tool_call_id），是为了：
> 1. 避免 SSE 订阅者反复收到 50KB 大 payload（前端只需要 `output_preview`）
> 2. 避免 `run.events` buffer 体积随调用次数线性膨胀（订阅者断线重连时 replay 全部 events 会爆）
> 3. `_build_trajectory` 直接从 `run.tool_outputs` 拿，不需要在 events 里二次解析

### 4.3 `run_registry.py` — `_build_trajectory` 合并

在原有 trajectory 构造的末尾加一段合并：

```python
# 把 run.tool_outputs 合并进每个 step 的 tool_calls[i].output。
# _truncate_tool_output 已经保证每条 ≤ MAX_TOOL_OUTPUT_CHARS（50000），这里不再截。
for s in steps_by_idx.values():
    for tc in s.get("tool_calls", []):
        tc_id = tc.get("id")
        if tc_id and tc_id in run.tool_outputs:
            tc["output"] = run.tool_outputs[tc_id]
```

### 4.4 `session_service.py` — 历史重建

新增 3 个辅助函数：

```python
def _user_content_with_prefix(turn) -> str | None:
    """重建 user message 的实际 LLM content：injected_prefix + 原始 question。
    历史重放时必须和当时提交给 LLM 的 user message 完全一致，否则前缀缓存失效。"""

def _has_full_tool_outputs(trajectory) -> bool:
    """判断 trajectory 是否含完整 tool_results（用于老 session fallback 决策）"""

def _replay_assistant_turn_full(turn) -> list[dict]:
    """把一个 assistant turn 还原成完整 OpenAI messages 序列：
       step → assistant(content + reasoning_content + tool_calls) + N×tool"""
```

**核心还原逻辑**（节选）：

```python
def _replay_assistant_turn_full(turn):
    trajectory = _parse_json(turn.trajectory) or {}
    steps = trajectory.get("steps") or []
    result = []
    for i, step in enumerate(steps):
        is_last = i == len(steps) - 1
        tool_calls_raw = step.get("tool_calls") or []
        thinking = step.get("thinking") or ""
        reasoning = step.get("reasoning") or ""

        asst = {"role": "assistant"}
        # 最后一步无 tool_calls → 用 turn.content（数据库纯净答案）
        if is_last and not tool_calls_raw:
            asst["content"] = turn.content or thinking or None
        else:
            asst["content"] = thinking or None

        # Kimi 多步要求保留 reasoning_content
        if reasoning:
            asst["reasoning_content"] = reasoning

        if tool_calls_raw:
            asst["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["args"], ensure_ascii=False),
                    },
                }
                for tc in tool_calls_raw
            ]

        if asst.get("content") is None and not asst.get("tool_calls"):
            continue
        result.append(asst)

        # 紧跟 tool messages（顺序必须和 assistant.tool_calls 完全一致）
        for tc in tool_calls_raw:
            output = tc.get("output") or json.dumps(tc.get("preview") or {})
            result.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": output,
            })
    return result
```

**最终入口**：

```python
def get_history_messages(db, session_id):
    turns = get_turns(db, session_id)
    full_replay = settings.enable_full_history_replay

    result = []
    for t in turns:
        if t.role == "user":
            content = _user_content_with_prefix(t)
            if content:
                result.append({"role": "user", "content": content})
        elif t.role == "assistant":
            trajectory = _parse_json(t.trajectory)
            if full_replay and _has_full_tool_outputs(trajectory):
                result.extend(_replay_assistant_turn_full(t))
            else:
                # 老 session / RAG turn / feature flag 关闭 → 单条 content
                if t.content:
                    result.append({"role": "assistant", "content": t.content})
    return result
```

## 5. 关键设计权衡

### 5.1 `tool_call_id` 严格配对（OpenAI/DashScope/Moonshot 强制）

OpenAI 风格 API 要求：每条 `assistant.tool_calls[i]` 后必须紧跟一条 `tool` 消息，`tool.tool_call_id` 必须等于 `assistant.tool_calls[i].id`。错位直接 400。

**实现细节**：
- trajectory 里 `tool_calls[i].id` 是 LLM 流式返回的原始 ID（`call_xxx`），全局唯一
- 极老 trajectory 没有 `id` 字段时用 `f"call_{turn.id}_{step}_{j}"` 兜底（`turn.id` 来自 ChatTurn 自增主键，跨 turn 不会冲突）
- 重放时 assistant.tool_calls 列表和后跟的 tool messages 严格按 trajectory 里的顺序输出

### 5.2 Kimi `reasoning_content` 必须保留

Kimi (`kimi-k2.6` / 百炼直供 `kimi/kimi-k2.6`) 的多步工具调用要求：
> 如果 LLM 在某轮输出了 `reasoning_content`，那么后续把该轮 message 喂回给它时也必须带上 `reasoning_content` 字段，否则 API 报错。

trajectory 已经有 `reasoning` 字段。重放时 `assistant_msg["reasoning_content"] = reasoning`。

OpenAI / Qwen 不支持这个字段，**会忽略未知字段不报错**，所以加了不会有副作用。

### 5.3 老 session 自动 fallback

`_has_full_tool_outputs(trajectory)` 检测是否含 `tool_calls[i].output`：
- **改造后生成**的 trajectory 都有 → 走完整重放
- **改造前生成**的 trajectory 没有 → 退回旧的"只回放 user/assistant content"路径

**不需要写迁移脚本**，老 session 自然兼容。

### 5.4 前缀缓存友好

`_user_content_with_prefix` 重新拼出 `injected_prefix + content`，和当时提交给 LLM 的 user message **完全一致**，跨轮显式缓存命中：

```
第 1 轮 prompt: system + user1(prefix1+q1)
第 2 轮 prompt: system + user1(prefix1+q1) + assistant1(tool_calls) + tool1 + ... + assistant1(final) + user2(prefix2+q2)
                ↑─── 这段和第 1 轮 prefix 完全一致 → 缓存命中 ───↑
```

显式缓存命中后这段按 10% 计费，**实际多轮成本远低于天真估算**。

### 5.5 SSE 订阅者隔离

`_emit` 在事件入 `run.events` 前 pop 掉 `output_full`：
- 前端通过 `/api/runs/{run_id}/events` SSE 订阅，只看到紧凑的 `output_preview`
- 客户端断线重连时 `subscribe` replay `run.events` 也不会被大字段拖累
- 50KB × 多次 tool_call ≈ 几 MB 量级的 SSE 推送被消除

## 6. Token / DB 影响

### 6.1 Token 估算（含显式缓存）

| 轮次 | prompt_tokens | cached% | 实际计费等效 tokens |
|------|--------------|---------|------------------|
| 第 1 轮 | ~5K | 0% | 5K |
| 第 2 轮 | ~30K | ~80% | ~10K 等效 |
| 第 5 轮 | ~100K | ~85% | ~25K 等效 |
| 第 8 轮 | ~180K | ~88% | ~40K 等效 |
| 第 10 轮 | ~230K（接近 256K） | ~90% | ~45K 等效 |

**256K context 突破点**：约 10-12 轮（取决于工具结果实际大小）。突破后 `_trim_messages_to_budget`（`@d:\python_programs\tg-history\backend\services\qa_agent.py:59-90`）按比例砍最老的 tool 消息——agent 退化为"只记得最近几轮"，可接受。

### 6.2 DB 影响

| 字段 | 改造前 | 改造后 |
|------|--------|--------|
| 单条 `ChatTurn.trajectory` JSON | ~5 KB | ~30-100 KB |
| 一个 session 跑 10 轮 | ~50 KB | ~500 KB - 1 MB |

SQLite 单行 JSON 字段没硬上限，1MB 完全没问题。**不需要拆独立表**。

## 7. Feature Flag

`backend/config.py`：

```python
class Settings(BaseSettings):
    # 多轮对话历史完整重放开关（Claude Code 风格）
    # 开启时：把每轮 trajectory 里的 tool_calls + 完整 tool_results 还原回 messages，
    #        让 Agent 能"看到"前几轮调用了什么工具、找到了哪些消息。
    # 关闭时：仅回放 user/assistant 的纯文本 content（旧行为，省 token）。
    # DB 体积影响：单 ChatTurn.trajectory 5KB → 30~100KB；显式缓存命中后实际费用约为关闭时 1.5~2x。
    enable_full_history_replay: bool = True
```

**回滚方式**：`.env` 里设 `ENABLE_FULL_HISTORY_REPLAY=false`，秒级生效。已落库的新版 trajectory 仍然兼容（数据多了 `output` 字段而已）。

## 8. 验证测试

`scripts/_test_full_replay.py` 包含 9 个不调真实 LLM 的单元测试：

```
[OK] _emit collects tool_outputs and strips output_full from events
[OK] _build_trajectory merges tool_outputs into tool_calls[i].output
[OK] _has_full_tool_outputs differentiates old vs new trajectory
[OK] _replay_assistant_turn_full: 2-step trajectory replay correct
[OK] _replay_assistant_turn_full: reasoning_content preserved (Kimi compat)
[OK] _replay_assistant_turn_full: fallback to preview when no output
[OK] tool_call_id pairing strictly preserved (a/b/c -> a/b/c)
[OK] feature flag OFF -> legacy text-only replay
[OK] full replay E2E: user/assistant/tool/assistant/user sequence
```

跑法：

```pwsh
venv\Scripts\python scripts\_test_full_replay.py
```

## 9. 已知约束 & 边界情况

| 场景 | 行为 |
|------|------|
| 工具结果原始 > 50K 字符 | 已被 `_truncate_tool_output` 在 qa_agent 侧截到 50K，trajectory 持久化也是这个截断版（按 list 字段条目截，保 JSON 结构完整） |
| 单 turn trajectory > 1MB | 极端罕见。SQLite 单 JSON 字段无硬上限；超大时会拖慢 `get_history_messages` 但不会失败 |
| 子 Agent 嵌套调用 | 主 Agent 视角下 `research` 工具就是普通 tool_call，返回的 `{report, message_ids, usage}` 会被完整持久化。子 Agent 自己的 sub-trajectory **不**单独存（避免数据爆炸） |
| 上下文过 budget | 现有 `_trim_messages_to_budget` 按比例砍 tool 消息（保留 system/user/assistant），自动兜底，**不需额外处理** |
| RAG 模式 turn | RAG 没有 trajectory.steps，`_replay_assistant_turn_full` 直接退回 `{role:assistant, content: turn.content}` |
| 第 N 轮 LLM 失败强制总结后 turn | trajectory 含部分 step + final answer，正常重放 |

## 10. 迁移到新项目的提示

如果你想把这套机制平移到其他 Agent 项目，**核心要点**：

1. **trajectory 持久化**：每个 turn 必须存 `tool_calls[{id, name, args, output}]`，缺一不可
2. **`tool_call_id` 一致性**：trajectory 里的 id 和重放时 `assistant.tool_calls[i].id` 必须严格一致
3. **`output` 长度上限**：建议设 50K 字符（约 28K tokens），既覆盖 99% 工具结果又不会单条爆 budget
4. **重放顺序**：每个 step 必须 `assistant(tool_calls) → N×tool`，不能错位
5. **Kimi 模型记得回传 `reasoning_content`**（其他模型可忽略）
6. **依赖显式缓存**：不开 prompt cache 的话多轮对话费用直接 5x↑，不可接受
7. **`_trim_messages_to_budget` 兜底**：必须有，否则 10 轮+ 必爆 context

迁移工作量：在已有 `qa_agent + run_registry + session_service` 架构上**约 1-2 天**（含写测试），改动只涉及 4 个文件、~150 行新增代码。

---

> 主索引：[`README.md`](./README.md)
> 上一篇：[`06-runtime-and-artifact.md`](./06-runtime-and-artifact.md)
