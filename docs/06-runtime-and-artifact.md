# 聊天记录管理迁移文档（6/6）Run / Session / Artifact + 工程实践 + 迁移 Checklist

> 上一篇 [`05-tools-and-subagent.md`](./05-tools-and-subagent.md) 介绍了工具集和子 Agent。本篇是收尾：Run/Session 持久化、SSE 事件流、关键工程实践、迁移清单。

## 13. RAG 引擎（备用模式 — `services/rag_engine.py`）

> RAG 是简化版本：**单轮、不带工具调用、固定流程**（语义+关键词→上下文扩展→rerank→LLM 生成）。适合"快问快答"场景，不适合调研型问题。

主要流程：

```python
async def answer_question_stream(db, question, chat_ids, ...):
    # 1. 语义检索（top 10）
    # 2. 关键词检索（FTS5 top 10）
    # 3. 合并去重 → all_msg_ids
    # 4. 话题上下文扩展（按 topic_id 拉同话题所有消息）
    # 5. Rerank（候选 > 5 时）
    # 6. 格式化上下文 + 注入 prompt 模板
    # 7. 流式 LLM 调用
    # 8. 构建 sources（按话题去重前 5）
```

Prompt 模板（`backend/prompts/qa_answer.txt`）：

```
你是一个群聊记录分析助手。根据以下检索到的聊天片段回答用户的问题。

要求：
- 引用具体的发言人和时间
- 如果信息不足以回答问题，明确说明"根据现有记录未找到相关信息"
- 如果有多种观点或讨论，都列出来
- 使用用户提问的语言回答
- 使用 Markdown 格式，保持条理清晰

检索到的聊天片段：
{retrieved_chunks}

用户问题：{question}
```

**事件流类型**：`status / search_result / rerank / context / token / usage / done / error`。

**何时用 RAG 而不是 Agent**：
- 用户期望快速响应（< 3 秒）
- 问题是"明确事实查询"
- 不需要跨群聊/跨时间段汇总

---

## 14. Run Registry（`services/run_registry.py`）

### 14.1 设计目标

把 agent / rag 执行**从 HTTP 请求中解耦**：
- 切页面 / 刷新浏览器都不影响 run
- 进入时按 `last_event_id` 续播未见事件
- 多个客户端（多 tab）可以同时订阅同一 run

### 14.2 Run 数据结构

```python
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
    events: list[dict] = field(default_factory=list)  # 全量事件 buffer
    seq: int = 0
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    task: asyncio.Task | None = None

    started_at: datetime
    completed_at: datetime | None = None

    final_answer: str = ""
    final_sources: list = field(default_factory=list)
    final_usage: dict | None = None
    final_task_usage: dict | None = None
    error: str | None = None
```

### 14.3 启动逻辑（防重复）

```python
async def start(self, session_id, question, mode="agent", ...):
    """启动 run。同 session 已有 pending/running run 时返回该 run_id 并 already_running=True"""
    async with self._lock:
        existing_rid = self._session_active.get(session_id)
        if existing_rid:
            existing = self._runs.get(existing_rid)
            if existing and existing.status in ("pending", "running"):
                return existing_rid, True  # 已有活跃 run，复用

        run_id = uuid.uuid4().hex
        run = Run(id=run_id, session_id=session_id, mode=mode, question=question, ...)
        self._runs[run_id] = run
        self._session_active[session_id] = run_id

    run.task = asyncio.create_task(_run_worker(run))
    return run_id, False
```

### 14.4 订阅（带回放 + 续播）

```python
async def subscribe(self, run_id, last_event_id=-1):
    """订阅 run 事件流。
    1. 先回放 buffer 中 seq > last_event_id 的事件
    2. 若 run 已结束，发 sentinel 后退出
    3. 否则挂订阅队列等待后续事件
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
```

> **关键 — 客户端断开不影响 run**：HTTP 连接断开 → `subscribe` 退出 → run.task 仍在跑 → 下次客户端再来订阅时按 `last_event_id` 续播未见事件。

### 14.5 事件 fan-out

```python
def _emit(run, event):
    """给事件打 seq、追加到 buffer、广播给所有订阅者"""
    event["seq"] = run.seq
    run.seq += 1
    run.events.append(event)
    for q in list(run.subscribers):
        try:
            q.put_nowait(event)
        except Exception:
            pass  # 订阅者队列异常不影响 run 本身
```

### 14.6 Worker 主体

```python
async def _run_worker(run):
    db = SessionLocal()
    try:
        run.status = "running"

        # 注入时间戳 + artifact 摘要
        injected_prefix = ""
        if run.mode == "agent":
            parts = [build_current_time_hint()]
            artifact_summary = _build_artifact_summary(db, run.session_id)
            if artifact_summary:
                parts.append(artifact_summary)
            injected_prefix = "\n\n---\n\n".join(parts)

        # 读历史（不含当前 turn）
        history = session_service.get_history_messages(db, run.session_id)

        # 先把 user turn 落库（前端断开时也能看到自己的问题）
        session_service.append_turn(
            db, run.session_id, role="user", content=run.question,
            mode=run.mode,
            meta={"run_id": run.id,
                  **({"injected_prefix": injected_prefix} if injected_prefix else {})},
        )

        augmented_user_content = (
            f"{injected_prefix}\n\n---\n\n{run.question}" if injected_prefix else run.question
        )

        if run.mode == "rag":
            async for ev in answer_question_stream(db, run.question, run.chat_ids, ...):
                _emit(run, ev)
                if ev.get("type") == "done":
                    run.final_answer = ev.get("answer", "")
                    run.final_sources = ev.get("sources", [])
                elif ev.get("type") == "usage":
                    run.final_usage = {k: v for k, v in ev.items()
                                        if k not in ("type", "seq", "step")}
        else:
            async for ev in run_agent(db, augmented_user_content, run.chat_ids,
                                        history if history else None, run.session_id):
                _emit(run, ev)
                if ev.get("type") == "final_answer":
                    run.final_answer = ev.get("answer", "")
                    run.final_sources = ev.get("sources", [])
                    if ev.get("task_usage"):
                        run.final_task_usage = ev["task_usage"]
                elif ev.get("type") == "usage":
                    run.final_usage = {...}

        run.status = "completed"

    except asyncio.CancelledError:
        run.status = "aborted"
        # 不再 raise — 让 finally 把 assistant turn 保存完整

    except Exception as e:
        run.status = "failed"
        run.error = str(e)
        _emit(run, {"type": "error", "error": str(e)})

    finally:
        run.completed_at = datetime.utcnow()

        # 持久化 assistant turn（含 trajectory + usage）
        trajectory = _build_trajectory(run)
        meta = {"run_id": run.id}
        if run.final_usage: meta["usage"] = run.final_usage
        if run.final_task_usage: meta["task_usage"] = run.final_task_usage
        if run.status == "aborted": meta["aborted"] = True
        elif run.status == "failed":
            meta["failed"] = True
            meta["error"] = run.error

        if run.final_answer or trajectory.get("steps") or trajectory.get("rag_events"):
            session_service.append_turn(
                db, run.session_id, role="assistant",
                content=run.final_answer or "",
                sources=run.final_sources, trajectory=trajectory,
                mode=run.mode, meta=meta,
            )

        # 广播 sentinel
        sentinel = {"type": "__end__", "status": run.status, "seq": run.seq}
        run.seq += 1
        run.events.append(sentinel)
        for q in list(run.subscribers):
            q.put_nowait(sentinel)

        db.close()
```

### 14.7 Trajectory 构造（持久化用）

```python
def _build_trajectory(run):
    """从 run.events 构造紧凑 trajectory JSON"""
    steps_by_idx = {}

    for ev in run.events:
        t = ev.get("type")
        step = ev.get("step")
        if t == "step_start" and step:
            _ensure(step)
        elif t == "thinking_delta" and step:
            _ensure(step)["thinking"] += ev.get("text", "")
        elif t == "reasoning_delta" and step:
            _ensure(step)["reasoning"] += ev.get("text", "")
        elif t == "tool_call" and step:
            s = _ensure(step)
            s["had_tool_calls"] = True
            s["tool_calls"].append({
                "id": ev.get("id"), "name": ev.get("name"),
                "args": ev.get("args"),
            })
        elif t == "tool_result" and step:
            s = steps_by_idx.get(step)
            if s:
                for tc in s["tool_calls"]:
                    if tc.get("id") == ev.get("id"):
                        tc["preview"] = ev.get("output_preview")
                        tc["duration_ms"] = ev.get("duration_ms")
                        tc["error"] = ev.get("error", False)
                        break

    # 截断超长文本防 DB 爆炸
    MAX_CHARS = 20000
    for s in steps_by_idx.values():
        if len(s["thinking"]) > MAX_CHARS:
            s["thinking"] = s["thinking"][:MAX_CHARS] + "\n...[truncated]"

    return {"steps": [steps_by_idx[k] for k in sorted(steps_by_idx)]}
```

> **目的**：让用户重新打开历史对话时，能看到 Agent 完整的思考链 + 工具调用流程，而不是只有最终答案。

### 14.8 SSE Endpoint

```python
@router.get("/runs/{run_id}/events")
async def run_events(run_id: str, last_event_id: int = Query(-1)):
    run = registry.get(run_id)
    if not run:
        raise HTTPException(404, "Run 不存在或已过期")

    async def event_gen():
        try:
            async for ev in registry.subscribe(run_id, last_event_id=last_event_id):
                seq = ev.get("seq", "")
                yield f"id: {seq}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            return  # 客户端断开 — 不影响 run

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )
```

> **`X-Accel-Buffering: no`**：让 nginx / 反代不要缓冲 SSE，关键。

### 14.9 周期性清理

```python
async def periodic_cleanup(interval_seconds=60):
    """删除已完成且超过 ttl 的 run（默认 5 分钟）"""
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await registry.cleanup_expired(ttl_seconds=300)
        except asyncio.CancelledError:
            break

# 在 main.py 的 startup event 启动这个任务
```

---

## 15. Session / Turn / Artifact 服务（`session_service.py` + `artifact_service.py`）

### 15.1 Session CRUD 关键设计

```python
def append_turn(db, session_id, role, content, sources=None, trajectory=None, mode=None, meta=None):
    """追加一条 turn，并更新 session 的 turn_count / last_preview / updated_at"""
    seq = _next_seq(db, session_id)
    t = ChatTurn(session_id=session_id, seq=seq, role=role, content=content,
                  sources=_dump_json(sources), trajectory=_dump_json(trajectory),
                  mode=mode, meta=_dump_json(meta), created_at=datetime.utcnow())
    db.add(t)

    # 更新 session 汇总字段（一次事务）
    s = get_session(db, session_id)
    if s:
        s.turn_count = (s.turn_count or 0) + 1
        s.last_preview = _derive_preview(content)
        s.updated_at = datetime.utcnow()

    db.commit()
    return t


def get_history_messages(db, session_id):
    """返回给 agent 用的对话历史（仅 role + content）。

    **前缀缓存关键**：user turn 的 content 只存纯净问题，
    LLM 看到的 = meta.injected_prefix + content。
    重放历史时**重新拼出那份内容**，保证和上一轮提交给 LLM 的 user message 完全一致 → 缓存命中。
    """
    turns = get_turns(db, session_id)
    result = []
    for t in turns:
        if t.role not in ("user", "assistant") or not t.content:
            continue
        content = t.content
        if t.role == "user":
            meta = _parse_json(t.meta) or {}
            prefix = meta.get("injected_prefix")
            if prefix:
                content = f"{prefix}\n\n---\n\n{content}"
        result.append({"role": t.role, "content": content})
    return result
```

### 15.2 Artifact 写操作（关键 — `str_replace` 协议）

```python
def update_artifact(db, session_id, artifact_key, old_str, new_str, *, turn_id=None):
    """str_replace 风格的增量编辑。old_str 必须在当前最新版本中**唯一**出现。

    成功后 bump current_version，新增一行 ArtifactVersion。
    """
    art = get_artifact(db, session_id, artifact_key)
    if art is None:
        raise ArtifactNotFound(...)

    latest = get_version(db, art.id)
    cur = latest.content
    match_count = cur.count(old_str)
    if match_count != 1:
        nearby = _find_nearby_snippets(cur, old_str) if match_count == 0 else []
        raise StrReplaceError(match_count, old_str, nearby)

    new_content = cur.replace(old_str, new_str, 1)
    next_version = (art.current_version or 0) + 1

    ver = ArtifactVersion(artifact_id=art.id, version=next_version,
                            content=new_content, op="update",
                            op_meta=_dump_op_meta({...}), turn_id=turn_id)
    db.add(ver)
    art.current_version = next_version
    art.updated_at = datetime.utcnow()
    db.commit()
    return art, ver
```

**`StrReplaceError` 带回引导信息**：

```python
class StrReplaceError(ArtifactError):
    def __init__(self, match_count, old_str, nearby_snippets=None):
        self.match_count = match_count            # 0 / 2+
        self.old_str_preview = old_str[:200]
        self.nearby_snippets = nearby_snippets or []  # 当 match_count=0 时给"形似"片段

# 调用方（tool_update_artifact）把这些信息组成 suggestion 给 LLM：
{
    "error": "old_str matched 0 times (expected 1)",
    "code": "no_unique_match",
    "match_count": 0,
    "old_str_preview": "...",
    "suggestion": "old_str 在文档中匹配 0 次：检查拼写、缩进、换行；可以用 fetch 查看当前正文后再试。",
    "nearby_snippets": ["...看起来形似的片段1...", "...片段2..."]
}
```

> **效果**：LLM 看到具体错误码 + suggestion + 形似片段 → 一次就能修对，不会陷入"反复尝试相同 old_str"的死循环。

### 15.3 Artifact 版本历史浏览

```python
@router.get("/{artifact_key}/versions", response_model=list[ArtifactVersionItem])
def list_artifact_versions(session_id, artifact_key, db):
    """列出 artifact 的全部版本（不含正文）"""
    art = artifact_service.get_artifact(db, session_id, artifact_key)
    versions = artifact_service.list_versions(db, art.id)
    return [{"version": v.version, "op": v.op, "op_meta": ...,
              "turn_id": v.turn_id, "created_at": v.created_at}
             for v in versions]

@router.get("/{artifact_key}", response_model=ArtifactDetail)
def get_session_artifact(session_id, artifact_key, version=None, db):
    """取最新或指定版本完整内容"""
```

---

## 16. 关键工程实践（必看 — 迁移时容易踩的坑）

### 16.1 SQLite 大量并发写

**问题**：50w 条消息导入 + 10 个 chat 并发索引 + 用户 QA → 经常 `database is locked`

**解决**：
1. `PRAGMA journal_mode=WAL` — 多读不阻写
2. `PRAGMA busy_timeout=30000` — 写锁等 30s 而不是立即报错
3. `pool_size=30, max_overflow=50` — 连接池容量足够
4. 长 transaction 拆分（`FLUSH_EVERY=2000`）

### 16.2 Async / Thread 协作

**问题**：在 thread 里 `asyncio.run` 会创建新 loop，而 `llm_adapter` 的 Semaphore 绑定到主 loop，跨 loop 用会抛 `RuntimeError`。

**解决**：用 `_await_on_loop` 把协程派回主 loop：

```python
def _await_on_loop(coro, main_loop):
    future = asyncio.run_coroutine_threadsafe(coro, main_loop)
    return future.result()
```

### 16.3 跨 Provider 限流

**问题**：DashScope 和 Moonshot RPM 不同（30000 vs 3）。统一一个 semaphore 会让 Moonshot 拖慢全局。

**解决**：分 Provider 单独 semaphore：

```python
_DASHSCOPE_CHAT_SEM = asyncio.Semaphore(10)
_MOONSHOT_CHAT_SEM = asyncio.Semaphore(3)
_EMBED_SEM = asyncio.Semaphore(20)
```

### 16.4 Stream API 的 usage 捕获

**问题**：开了 `stream_options.include_usage` 后，usage chunk 在 `finish_reason` 之后才到，写 break on finish_reason 就拿不到 usage。

**解决**：让 `async for` 自然结束。

### 16.5 chromadb metadata 限制

**问题**：metadata 不支持 list 字段。

**解决**：`message_ids` 用 `json.dumps()` 存字符串，回放时 `json.loads()`。同时兼容老版 Python `repr()` 编码：

```python
if isinstance(msg_ids_raw, str):
    try:
        msg_ids_raw = json.loads(msg_ids_raw)
    except Exception:
        import ast
        msg_ids_raw = ast.literal_eval(msg_ids_raw)
```

### 16.6 跨 chat ID 冲突

**问题**：Telegram 不同 chat 的 message id 范围重叠（chat A 的 msg 1234 = chat B 的 msg 1234）。

**解决**：用 SHA-256 稳定哈希给每个 chat 算偏移：

```python
def _stable_id_offset(chat_id):
    digest = hashlib.sha256(chat_id.encode()).digest()
    return (int.from_bytes(digest[:8], "big") % (10**9)) * 1000000

# message.id = id_offset + raw_telegram_id
```

> **必须用稳定哈希**（SHA-256），不要用 Python 内置 `hash()`（PYTHONHASHSEED 加盐 → 重启后变）。

### 16.7 前缀缓存命中率

**问题**：每次提问注入"当前时间"会让 prefix 每次都变化 → 缓存失效。

**解决**：把"时间 + artifact 摘要"的 prefix **存到当时那条 user turn 的 meta**。历史重放时用 meta 里的旧 prefix 拼回去。这样：
- 第 1 轮：prefix1 + question1
- 第 2 轮：prefix1 + question1 + answer1 + prefix2 + question2 ← prefix1+question1 完全一致 → 命中前缀缓存

### 16.8 SSE 客户端断开恢复

**问题**：用户刷新页面 → SSE 断开 → 重连后丢失了部分事件。

**解决**：客户端记录 `last_event_id`，重连时通过 `?last_event_id=N` 让服务端按 buffer 回放未见事件。

### 16.9 工具结果智能截断

**问题**：单次工具结果可能 100K+ tokens（如 fetch 50 条长消息），直接塞 messages 会爆上下文。

**解决**：两层截断 — 先按 list 字段条目截（保 JSON 结构完整），还超就字符级 backstop。

```python
truncated_obj["_truncated"] = {"messages": {"shown": 60, "total": 200, "hidden": 140}}
truncated_obj["_truncation_hint"] = "结果较多，已展示前 N 条；如需更多，可缩小过滤范围..."
```

让 Agent 看到"还有多少没展示"，而不是 JSON 中间被砍断报错。

### 16.10 子 Agent 上下文软上限

**问题**：子 Agent 用 qwen3.5-plus，超过 128K 进入高价档 + 性能塌方。

**解决**：监控累计 token 估算，超过 100K 就**静默触发**强制总结（不告诉模型，避免诱发偷懒）：

```python
if _estimate_messages_tokens(messages) > SUB_CONTEXT_SOFT_LIMIT_TOKENS:
    soft_limit_hit = True
    break  # 跳到 forced_summary 分支
```

---

## 17. 迁移 Checklist（按优先级）

### P0 — 必须保留的核心模块

- [ ] **数据库模型**（`backend/models/database.py`）
  - Message / Topic / ChatSession / ChatTurn / Artifact / ArtifactVersion 表结构
  - SQLite PRAGMA 配置（WAL / busy_timeout / cache_size）
  - 稳定哈希 ID 偏移（`_stable_id_offset`）
  - FTS5 trigram 表初始化

- [ ] **LLM Adapter**（`backend/services/llm_adapter.py`）
  - 多 Provider 单例 client（DashScope + Moonshot）
  - 分 Provider Semaphore 限流
  - 显式缓存 `inject_cache_control`（Qwen 系列）
  - Provider 路由 `get_client_for_model`
  - 上下文窗口表 `MODEL_CONTEXT_WINDOW`
  - 定价表 `MODEL_PRICING` + `estimate_cost`
  - Kimi 思考链参数 `kimi_chat_kwargs`
  - Embedding + Rerank 接口

- [ ] **主 Agent**（`backend/services/qa_agent.py`）
  - System prompt（**整段复制不要改**）
  - 主循环（`run_agent`）
  - 流式 LLM 调用（`_stream_llm_step`）
  - 上下文预算管理（`_trim_messages_to_budget`）
  - 时间戳 + Artifact 摘要前缀注入

- [ ] **子 Agent**（`backend/services/sub_agent.py`）
  - System prompt（**整段复制不要改**）
  - 自适应步数 `_auto_max_steps`
  - 智能截断 `_truncate_tool_output`
  - Filters 自动注入

- [ ] **工具集**（`backend/services/qa_tools.py`）
  - 全部 schema（**description 写得越详细越好**）
  - Dispatcher（含 research / artifact 特殊路径）
  - 错误带 `suggestion` 字段

- [ ] **向量索引**（`backend/services/embedding.py`）
  - chromadb 配置（cosine + HNSW）
  - chunk 切分（2000 字符 + 3 行重叠）
  - chunk metadata 设计
  - 增量索引 `build_index_for_chat_incremental`

### P1 — 应该保留但可调整

- [ ] **话题构建**（`backend/services/topic_builder.py`）
  - reply chain path-compression
  - LLM 双向重叠窗口切分
  - 跨批 merge_check
  - 增量构建（只动新消息）

- [ ] **Run Registry**（`backend/services/run_registry.py`）
  - 异步任务 + 事件 buffer
  - SSE 订阅 + last_event_id 续播
  - Trajectory 持久化

- [ ] **Session / Artifact Service**
  - 前缀缓存友好的历史重放（meta.injected_prefix）
  - Artifact 版本历史（不存 diff，全量保留）
  - StrReplaceError 带 nearby_snippets

### P2 — 可选 / 视场景

- [ ] **RAG 引擎**（`rag_engine.py`）— 有"快问快答"场景再保留
- [ ] **导入解析**（`parser.py`）— 仅 Telegram 数据源需要；其他数据源换实现
- [ ] **Telegram User Profile**（`tg_user_profile.py`）— 仅 Telegram 场景

### P3 — 配置项

- [ ] **环境变量**（`.env.example`）
  ```
  DASHSCOPE_API_KEY=...
  MOONSHOT_API_KEY=...
  LLM_MODEL_QA=kimi/kimi-k2.6
  LLM_MODEL_SUB_AGENT=qwen3.5-plus
  EMBEDDING_MODEL=text-embedding-v4
  RERANK_MODEL=qwen3-rerank
  ENABLE_QWEN_EXPLICIT_CACHE=true
  DATA_DIR=./data
  ```

- [ ] **依赖**（`requirements.txt`）
  ```
  fastapi==0.115.9
  uvicorn==0.34.2
  sqlalchemy==2.0.40
  pydantic==2.11.2
  httpx==0.28.1
  openai==1.78.1
  chromadb==1.0.7
  ijson==3.3.0
  sse-starlette==2.2.1
  ```

### 可裁剪的模块

如果你的目标项目不需要：
- **导入解析**：直接喂 `Message` ORM 对象到表里即可，跳过 `parser.py`
- **话题构建**：如果数据天然有"对话片段"边界（如客服工单按 ticket 分），跳过 `topic_builder`
- **Telegram 特定**：跳过 `tg_user_profile.py` + `get_user_profile` 工具
- **Artifact**：如果不需要"活文档"功能，删除 5 个 artifact 工具（但保留主循环里 `_artifact_event` 处理逻辑会更安全）

### 不可裁剪的核心

| 模块 | 不可裁剪原因 |
|------|---------|
| **System Prompts** | 多年迭代调出来的 — 决策树/侦察原则/质量门槛/不确定性分级是项目精华 |
| **两层 Agent 架构** | 主 Agent 强模型 + 子 Agent 便宜模型，是成本/质量平衡的核心 |
| **显式缓存** | 多轮对话不开缓存 → 费用 5x↑ |
| **流式 + Run Registry** | SSE 解耦执行/订阅是用户体验关键 |
| **工具错误的 `suggestion`** | LLM 自动纠错全靠它 |
| **`_truncate_tool_output` 智能截断** | 不做的话上下文必爆 |
| **前缀缓存友好的历史重放**（meta.injected_prefix）| 多轮对话费用直接×几倍 |

---

## 18. 端到端时序示例

完整一次 Agent 提问的执行链：

```
用户在前端输入"帮我梳理最近三个月群里讨论 GPU 价格的话题"
   ↓
POST /api/ask/agent {session_id, question, mode: "agent"}
   ↓
qa_router._start_run_handler:
  - ensure_session_for_question → ChatSession
  - registry.start(...) → 创建 Run + asyncio.create_task(_run_worker)
  - 返回 {run_id, session_id, already_running: false}
   ↓ (前端立即开始订阅)
GET /api/runs/{run_id}/events
  - registry.subscribe → SSE stream
   ↓ (并行进行)
_run_worker:
  1. injected_prefix = build_current_time_hint() + _build_artifact_summary()
  2. history = session_service.get_history_messages(session_id)  # 历史 user/assistant
  3. session_service.append_turn(role="user", content=question, meta={injected_prefix})
  4. async for ev in run_agent(question_with_prefix, history, session_id):
     - run_agent 主循环（见下）
     - 每个 ev → _emit(run, ev) → SSE 推给前端
   ↓
run_agent 主循环（step 1）:
  a. _stream_llm_step → LLM 输出 tool_calls=[research(task_A), research(task_B), research(task_C)]
  b. yield "step_start" → "thinking_delta"... → "tool_calls"
  c. 并发执行 3 个 research（asyncio.gather）：
     - 每个 research → run_sub_agent
     - run_sub_agent 内部又跑 8~16 步 search/fetch
     - 子 Agent 进度事件 → event_callback → 累积在 sub_events
  d. asyncio.gather 完成 → 发射所有 sub_events → 发射 tool_result × 3
  e. 把 tool 消息加到 messages
   ↓
run_agent 主循环（step 2）:
  a. LLM 看到 3 份 research 报告 → 评估质量
  b. 若发现"缺引用"/"维度缺失"/"越界线索" → 发新一轮 research
  c. 否则 → 输出最终答案（可能调用 create_artifact）
   ↓
run_agent 主循环（step 3+）:
  a. LLM 输出文本（无 tool_calls）→ 循环结束
  b. yield "final_answer" {answer, sources, task_usage}
   ↓
_run_worker (finally):
  - trajectory = _build_trajectory(run.events)
  - session_service.append_turn(role="assistant", content=answer, sources, trajectory, meta)
  - 广播 sentinel {type: "__end__", status: "completed"}
   ↓
前端 SSE 收到 __end__ → 关闭连接 → UI 切回"已完成"状态
```

---

## 19. 总结：迁移建议

1. **先把 LLM Adapter + 主子 Agent + 工具集**这套搬过去（上面 P0 部分）。这是一个 **可独立运行的 Agent 框架**，给定一个数据库 + 一组工具，就能立刻有"自主调用工具的智能助手"能力。
2. **再考虑你目标项目的领域工具**，按 `qa_tools.py` 的格式定义新工具（schema 描述详尽 + handler 返回带 `error` / `suggestion` / `_artifact_event`）。
3. **数据接入**：你的数据可以是聊天记录、文档、邮件、JIRA ticket、客户反馈等等 — 把 `messages` 表换成对应实体表，保留 `topics`/`chunks` 的"语义连续片段"概念即可。
4. **Run Registry + SSE** 几乎是通用基础设施，可以直接复用。
5. **System Prompts** 是项目精华 — **不要重写**。可以替换里面的"Telegram 群聊" → 你的领域名词（如"客户工单"/"代码审查讨论"），但**决策树/侦察原则/质量门槛/不确定性分级**这些方法论部分整段保留。

> **预计工作量**（参考）：
> - 复制核心模块（P0）+ 改 import 路径：1~2 天
> - 数据接入 + 工具改写：3~5 天
> - 前端集成（如需）：3~5 天
> - 调优 system prompt 适配新领域 + 测试：2~3 天
> - **共计：约 2 周**

祝迁移顺利！如果迁移后遇到具体问题，建议先检查这几个点：
1. SQLite PRAGMA 是不是都设了（`busy_timeout`/`WAL` 缺一不可）
2. Semaphore 是不是按 Provider 分开（不分会被慢的拖累）
3. 显式缓存有没有正确注入（多轮对话费用差很多）
4. 前缀缓存重放历史时是不是用 `meta.injected_prefix` 重新拼了 user content
5. SSE 的 `X-Accel-Buffering: no` 头有没有设（nginx 反代会缓冲）
