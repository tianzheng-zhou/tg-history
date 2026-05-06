# 聊天记录管理迁移文档（2/6）消息导入 + 话题构建

> 上一篇 [`01-overview.md`](./01-overview.md) 介绍了总览和数据模型。本篇深入消息导入解析和话题构建。

## 3. 消息导入与解析

### 3.1 解析器设计目标

**目标**：把 Telegram 导出的 JSON（500MB-2GB）解析成标准化的消息 dict 列表，**绝不能 OOM**。

**核心策略 — 流式解析（`ijson`）**：

```python
# backend/services/parser.py
def iter_export_chats(file_path) -> Iterator[dict]:
    """按 chat 流式迭代。每次 yield 一个完整 chat 后调用方应 del 释放"""
    fmt = _detect_top_format(path)
    if fmt == "bulk":
        yield from _iter_chats_bulk(path, "chats.list.item")  # {"chats":{"list":[...]}}
    elif fmt == "bulk_list":
        yield from _iter_chats_bulk(path, "chats.item")        # {"chats":[...]}
    else:
        yield from _iter_chats_single(path)                    # {"name":..., "messages":[...]}
```

> **`_detect_top_format`**：先用 ijson 浅扫一下 root 类型（"chats" 是 obj 还是 array），自动选择正确的 path。

### 3.2 单条消息标准化（`parse_message`）

```python
{
    "id": int,                # 原始 telegram message id（**未加 offset**）
    "chat_id": str,
    "date": datetime | None,
    "sender": str,            # from / actor 字段
    "sender_id": str,         # "user{id}" / "channel{id}"
    "text": str,              # 原始 JSON 序列化（保留 entities 结构）
    "text_plain": str,        # 拼接后的纯文本（用于 FTS/preview）
    "reply_to_id": int | None,
    "forwarded_from": str | None,
    "media_type": str | None,
    "entities": list | None,  # 链接/@/code 等
}
```

**`text` 字段的处理 — `normalize_text`**：

Telegram 的 text 可能是：
- 纯字符串 `"hello"`
- 混合数组 `["hello ", {"type":"link","text":"https://..."}, " world"]`

输出 `(纯文本, entities列表)`，entities 带 `offset`/`length`/`href`。

### 3.3 导入入库（`routers/import_router.py:import_messages_for_chat`）

**关键性能参数**（针对 50w+ 量级群聊）：

```python
FLUSH_EVERY = 2000      # 每 2000 条 flush 一次（避免 50w ORM 对象一次塞 session）
FTS_CHUNK = 500         # FTS 增量插入单批（远低于 SQLITE_MAX_VARIABLE_NUMBER=999）
```

**去重策略（两种模式）**：

| 模式 | 触发条件 | 内存 | 适用场景 |
|------|---------|------|---------|
| **batch-scope** | `existing_ids=None` | O(batch) | 增量同步（每批 ~2000 条做一次 IN 查询） |
| **full-set** | 调用方传入完整 set | O(N_chat) | 文件导入（一次性提交全 chat，全集加载更划算） |

**FTS 增量插入**（不要每次 DELETE + 全量重灌）：

```python
if inserted_ids:
    for i in range(0, len(inserted_ids), FTS_CHUNK):
        sub = inserted_ids[i:i + FTS_CHUNK]
        placeholders = ",".join(f":id{k}" for k in range(len(sub)))
        params = {f"id{k}": v for k, v in enumerate(sub)}
        db.execute(text(
            f"INSERT INTO messages_fts(text_plain, sender, chat_id, msg_id) "
            f"SELECT text_plain, sender, chat_id, id FROM messages "
            f"WHERE id IN ({placeholders}) AND text_plain != ''"
        ), params)
```

**进度上报**：通过 `progress_cb(processed, total)` 让 worker 写入全局 `_import_progress` dict，前端轮询 `/api/import-progress` 拿进度。

### 3.4 文件上传后台任务模式

50w 条 JSON 解析 + 入库要几分钟到几十分钟，**HTTP 请求不能挂这么久**。所以：

```python
@router.post("/import")
async def import_chat(file: UploadFile):
    # 1. 流式写盘（8MB 一块，避免一次性 read 整个 2GB 文件 OOM）
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="wb") as tmp:
        while True:
            chunk = await file.read(8 * 1024 * 1024)
            if not chunk: break
            tmp.write(chunk)

    # 2. check + reserve（防 TOCTOU race —— 两个请求同时进来都看到 running=False）
    with _import_lock:
        if _import_progress["running"]:
            raise HTTPException(409, "已有任务在跑")
        _import_progress.update({"running": True, "task_id": task_id, ...})

    # 3. fire-and-forget
    asyncio.create_task(asyncio.to_thread(_run_upload_import, tmp_path, task_id))
    return {"status": "started", "task_id": task_id}
```

后台 worker 完成后自动调 `_enqueue_index(imported_chat_ids)` 触发话题构建 + 向量索引。

---

## 4. 话题构建（reply chain + LLM 语义切分）

`backend/services/topic_builder.py` 是整个项目最复杂的模块之一。

> **核心思想**：把"几万条消息" → "几百到几千个 topic"，每个 topic 是语义连续的对话片段。

### 4.1 全量构建 `build_topics`

```
1. 加载该 chat 所有 messages，构建 reply_to_id 关系图
   ↓
2. 用 path-compression 沿 reply_to_id 找根
   → 同一回复链的所有消息归到一个 reply_group
   ↓
3. 未关联到回复链的消息（孤立消息）→ _llm_split 做语义切分
   ↓
4. 删除该 chat 所有旧 topic
   ↓
5. 写入新 topic 行 + bulk update Message.topic_id
```

**关键优化 — path-compression 防 O(N×depth)**：

```python
root_cache: dict[int, int] = {}

def find_root(mid: int) -> int:
    chain: list[int] = []
    cur = mid
    while cur in msg_map and cur not in root_cache:
        parent = msg_map[cur].reply_to_id
        if not parent or parent not in msg_map or parent == cur:
            break
        chain.append(cur)
        cur = parent
    root = root_cache.get(cur, cur)
    for node in chain:
        root_cache[node] = root  # 回填整条链
    return root
```

> 对 8w 条消息 + 长回复链群聊，从分钟级降到亚秒。

### 4.2 LLM 语义切分 `_llm_split`

**双向重叠窗口**：

```python
BATCH_SIZE = 300        # 每批 LLM 看到的消息数
OVERLAP = 50            # 左右各 50 条上下文
claim_size = BATCH_SIZE - 2 * OVERLAP  # 200 条实际"认领"
```

每批的可见区间：
```
visible: [claim_start - OVERLAP, claim_end + OVERLAP]
claim:   [claim_start, claim_end]
```

LLM 看到完整上下文，但只**认领**中间 200 条的话题边界。这避免了话题被批次边界硬切。

**LLM Prompt（话题切分）**：

```
你是一个聊天记录分析助手。下面是一段群聊消息，每条消息前有编号 [N]。

请你根据**语义和话题变化**将这些消息分成若干个话题段落。
- 同一个讨论话题的消息归为一组
- 话题切换的地方就是分割点
- 每个话题给一个简短标题（10字以内）

输出格式（JSON数组）：
```json
[{"title": "话题标题", "start": 起始编号, "end": 结束编号}, ...]
```

只输出JSON，不要其他内容。

---
{messages}
```

**跨批合并检查**：
1. 如果批次 N 最后一个 segment 与批次 N+1 第一个 segment 的 LLM 范围重叠 → 自动合并
2. 否则标记为"边界候选"，并发跑 `_llm_merge_check` 决定是否合并：

```
判断以下两段相邻群聊消息是否属于**同一个话题**。

片段A 标题: {title_a}
片段A 最后几条消息:
{tail_a}
---
片段B 标题: {title_b}
片段B 开头几条消息:
{head_b}

这两个片段是否在讨论同一个话题？只回答 "是" 或 "否"，不要其他内容。
```

### 4.3 增量构建 `build_topics_incremental`

**目标**：新消息导入后，**不动旧 topic**，只把新消息归入合适的 topic。

```
1. 找 topic_id IS NULL 的新消息
   ↓
2. 沿 reply_to_id 向上找祖先（带 memoization 防长链 O(N×depth)）
   - 找到有 topic_id 的祖先 → 挂到那个 topic
   - 否则放入 unassigned
   ↓
3. unassigned 内部小型 reply chain 分组（new ↔ new）
   ↓
4. 真孤立消息 → _llm_split（只切新消息，不动旧消息）
   ↓
5. last-topic merge_check：第一组新消息 vs 最后一个旧 topic？
   - LLM 判断"是同一话题" → 合并到旧 topic
   - 否则新建 topic
   ↓
6. 写新 topic 行 + 更新已合并旧 Topic 的 message_count / end_date
```

**关键返回**：`(total_topic_count, changed_topic_ids)` — 给增量索引用，**只对变化的 topic 重切 chunk + 重 embed**。

### 4.4 异步调度（关键工程问题）

```python
async def build_topics(db, chat_id, progress=None):
    """thin async wrapper —— 派 thread pool 跑同步重计算"""
    main_loop = asyncio.get_running_loop()
    return await asyncio.to_thread(_build_topics_sync, chat_id, main_loop, progress)

def _build_topics_sync(chat_id, main_loop, progress):
    # 在 thread 里跑，需要 LLM 时通过 _await_on_loop 派回 main loop
    db = SessionLocal()  # 独立 session（thread 不能共享 main session）
    ...
    # reply chain 解析（纯同步 CPU 工作）
    # ↓
    # LLM 语义切分（异步 I/O，必须派回 main loop）
    semantic_groups = _await_on_loop(_llm_split(unassigned, progress), main_loop)

def _await_on_loop(coro, main_loop):
    """从 thread 派协程到 main loop，阻塞 thread 等结果"""
    future = asyncio.run_coroutine_threadsafe(coro, main_loop)
    return future.result()
```

> **为什么不在主循环直接跑同步段**：reply chain 解析对几万条消息要数秒到数十秒同步 CPU 时间，会阻死所有 HTTP API（前端进度条都拉不到）。
>
> **为什么不在 thread 里 `asyncio.run` 起新循环**：`llm_adapter` 的模块级 `Semaphore` 绑定到主循环，跨循环触碰会抛 `RuntimeError`。
>
> **正确做法**：thread 跑同步段；遇到 LLM 调用时通过 `run_coroutine_threadsafe` 派回主循环，thread 阻塞等结果。

### 4.5 进度上报字段

`progress` 是一个共享 dict，topic_builder 会写入：

```python
progress["topic_state"] = "loading_messages" | "building_reply_groups" | "llm_splitting" | "merging" | "writing"
progress["topic_total_messages"] = N
progress["topic_processed_messages"] = M
progress["topic_llm_batches_total"] = K       # 待跑批次数
progress["topic_llm_batches_done"] = J         # 已完成批次数
progress["topic_count"] = T                    # 当前已生成话题数
```

前端实时轮询展示给用户："正在生成 LLM 切分批次 12/45"。

---

> 下一篇：[`03-vector-index.md`](./03-vector-index.md) — 向量索引：chunk 切分、embedding 并发、增量更新。
