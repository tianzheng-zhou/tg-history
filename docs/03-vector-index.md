# 聊天记录管理迁移文档（3/6）向量索引

> 上一篇 [`02-import-and-topics.md`](./02-import-and-topics.md) 介绍了消息导入和话题构建。本篇深入向量索引的设计。

## 5. 向量索引

`backend/services/embedding.py` + `backend/services/article_service.py`（chunk 切分逻辑）。

### 5.1 ChromaDB 配置

```python
COLLECTION_NAME = "tg_messages"

def get_or_create_collection():
    client = chromadb.PersistentClient(path=settings.chroma_dir)
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )
```

**单 collection 多 chat 共存**（用 metadata `chat_id` 过滤）。

### 5.2 Chunk 切分

```python
MAX_CHUNK_CHARS = 2000      # 一个 chunk 最多 2000 字符
OVERLAP_LINES = 3           # 相邻 chunk 重叠 3 行（保留上文）

def _chunk_lines(lines, msg_ids):
    """按字符数切，超出阈值就开新 chunk，相邻重叠 3 行"""
    chunks = []
    current = []
    current_ids = []
    current_size = 0
    for line, mid in zip(lines, msg_ids):
        line_size = len(line) + 1  # +1 for newline
        if current_size + line_size > MAX_CHUNK_CHARS and current:
            chunks.append(("\n".join(current), current_ids))
            # 保留最后 OVERLAP_LINES 行作为下一 chunk 开头
            overlap_lines = current[-OVERLAP_LINES:]
            overlap_ids = current_ids[-OVERLAP_LINES:]
            current = list(overlap_lines)
            current_ids = list(overlap_ids)
            current_size = sum(len(l) + 1 for l in current)
        current.append(line)
        current_ids.append(mid)
        current_size += line_size
    if current:
        chunks.append(("\n".join(current), current_ids))
    return chunks
```

每个 topic 至少产出 1 个 chunk，长 topic 产出多个（带行级重叠）。

### 5.3 Chunk 元数据（必看 — 检索全靠它）

```python
metadatas.append({
    "chat_id": chat_id,                    # 用于按群聊过滤
    "topic_id": topic.id,                  # 用于按话题过滤
    "chunk_index": ci,                     # chunk 在 topic 内的序号
    "chunk_total": len(chunks),
    "message_ids": json.dumps(chunk_ids),  # JSON 字符串（chromadb 不支持 list 字段）
    "participants": participants,          # 逗号分隔的发言人字符串
    "start_date": topic.start_date.isoformat(),
    "end_date": topic.end_date.isoformat(),
    "message_count": len(chunk_ids),
})
```

> **关键 — `message_ids` 用 JSON 字符串存而非 list**：chromadb 的 metadata 只支持 scalar，不支持 list。回放时 `json.loads(meta["message_ids"])`。

**检索时的过滤构造**（`qa_tools._build_chroma_where`）：

```python
def _build_chroma_where(chat_ids, topic_ids, start_date, end_date):
    clauses = []
    if chat_ids:
        clauses.append({"chat_id": {"$in": chat_ids}} if len(chat_ids) > 1
                       else {"chat_id": chat_ids[0]})
    if topic_ids:
        clauses.append({"topic_id": {"$in": topic_ids}} if len(topic_ids) > 1
                       else {"topic_id": topic_ids[0]})
    # 日期过滤：chunk 的 [start_date, end_date] 区间与查询区间相交
    if start_date:
        clauses.append({"end_date": {"$gte": sd.isoformat()}})
    if end_date:
        clauses.append({"start_date": {"$lte": ed.isoformat() + "T23:59:59"}})
    return {"$and": clauses} if len(clauses) > 1 else (clauses[0] if clauses else None)
```

> **日期过滤的细节**：用 chunk 的区间和查询区间做相交判断（`chunk.end >= query.start AND chunk.start <= query.end`），而不是简单的"chunk 在区间内"。

### 5.4 Search 接口

```python
async def search_similar(query, n_results=10, where=None):
    """语义相似度检索。where 是 chromadb metadata 过滤表达式。"""
    embeddings = await embed([query])
    collection = await asyncio.to_thread(get_or_create_collection)

    def _query():
        return collection.query(
            query_embeddings=embeddings,
            n_results=n_results,
            where=where,
        )
    res = await asyncio.to_thread(_query)
    # chromadb 返回的是 list-of-lists（query 接受多个 query），取 [0]
    return [
        {
            "document": res["documents"][0][i],
            "metadata": res["metadatas"][0][i],
            "distance": res["distances"][0][i],
            "id": res["ids"][0][i],
        }
        for i in range(len(res["ids"][0]))
    ]
```

> **distance 含义（cosine）**：0 = 完全相同，2 = 完全相反。一般 < 0.5 算相关。

### 5.5 增量索引 `build_index_for_chat_incremental`

```python
async def build_index_for_chat_incremental(db, chat_id, changed_topic_ids, progress):
    if not changed_topic_ids:
        return 0  # 没变化就不动，旧向量保留

    # 1. 拉 changed topics → 切新 chunks
    ids, texts, metadatas = _prepare_incremental_index_sync(chat_id, changed_topic_ids)

    # 2. ChromaDB 按 metadata 精确删除旧 chunks
    _delete_chunks_for_topics(chat_id, changed_topic_ids)

    # 3. 写入新 chunks
    await add_documents(ids, texts, metadatas, progress=progress)
    return len(ids)
```

**精确删除**（`_delete_chunks_for_topics`）：

```python
def _delete_chunks_for_topics(chat_id, topic_ids):
    collection = get_or_create_collection()
    try:
        collection.delete(where={
            "$and": [
                {"chat_id": chat_id},
                {"topic_id": {"$in": topic_ids}},
            ]
        })
    except Exception:
        # 失败兜底：删整个 chat（最坏情况下也不会留脏向量）
        collection.delete(where={"chat_id": chat_id})
```

### 5.6 并发 embedding

```python
# llm_adapter.py
_EMBED_SEM = asyncio.Semaphore(20)

async def add_documents(ids, texts, metadatas, batch_size=10, progress=None):
    starts = list(range(0, len(texts), batch_size))

    async def _embed_batch(start):
        end = min(start + batch_size, len(texts))
        batch_texts = texts[start:end]
        embeddings = await embed(batch_texts)  # 走 _EMBED_SEM
        return start, embeddings

    embed_tasks = [_embed_batch(s) for s in starts]

    # asyncio.as_completed：按完成顺序写入 chromadb，及时更新进度
    for coro in asyncio.as_completed(embed_tasks):
        start, embeddings = await coro
        end = min(start + batch_size, len(texts))
        batch_ids = ids[start:end]
        batch_texts = texts[start:end]
        batch_metas = metadatas[start:end]

        # chromadb upsert 是同步阻塞 IO，派到 thread
        def _upsert_sync():
            collection = get_or_create_collection()
            collection.upsert(
                ids=batch_ids,
                documents=batch_texts,
                metadatas=batch_metas,
                embeddings=embeddings,
            )
        await asyncio.to_thread(_upsert_sync)

        if progress is not None:
            progress["index_done"] = progress.get("index_done", 0) + len(batch_ids)
```

> **为什么用 `as_completed` 而非 `gather`**：as_completed 让前端进度条平滑增长（每 batch 完成立即更新），gather 要等全部完成才返回。

### 5.7 索引调度（`_index_runner` in `import_router.py`）

```python
MAX_PARALLEL = 16  # 群聊级并发上限

async def _index_runner():
    """后台单例任务，从 _index_queue 取出 (chat_id, force) 跑索引"""
    sem = asyncio.Semaphore(MAX_PARALLEL)

    async def _process_one(chat_id, force):
        async with sem:
            db = SessionLocal()
            try:
                detail = _index_progress["details"].setdefault(chat_id, {})
                if force:
                    await build_topics(db, chat_id, progress=detail)
                    await build_index_for_chat(db, chat_id, progress=detail)
                else:
                    _total, changed = await build_topics_incremental(db, chat_id, progress=detail)
                    await build_index_for_chat_incremental(db, chat_id, changed, progress=detail)
                # 标记 import.index_built = True
                imp = db.query(Import).filter(Import.chat_id == chat_id).first()
                if imp:
                    imp.index_built = True
                    db.commit()
            finally:
                db.close()

    tasks = [_process_one(cid, f) for cid, f in queue]
    await asyncio.gather(*tasks, return_exceptions=True)
```

**`_enqueue_index` 的"就高不就低"策略**：

```python
def _enqueue_index(chat_ids, force=False):
    """把 chat_id 加入索引队列。同 chat 已在队列时：force=True 覆盖原 force=False"""
    with _index_lock:
        for cid in chat_ids:
            existing = next((i for i, (c, _) in enumerate(_index_queue) if c == cid), -1)
            if existing >= 0:
                _, old_force = _index_queue[existing]
                if force and not old_force:
                    _index_queue[existing] = (cid, True)  # 升级为 force
            else:
                _index_queue.append((cid, force))
        # 确保 runner 任务在跑
        if _index_runner_task is None or _index_runner_task.done():
            _index_runner_task = asyncio.create_task(_index_runner())
```

> **为什么"就高不就低"**：用户先点了"增量索引"再点"重建索引"，应该按重建跑，否则索引会是脏的。

### 5.8 Embedding API 封装（`llm_adapter.embed`）

```python
async def embed(texts, model=None):
    """text-embedding-v4 单次最多 25 条（DashScope 限制）"""
    if not texts:
        return []
    model = model or settings.embedding_model
    client = _get_client()  # DashScope

    BATCH = 25
    if len(texts) <= BATCH:
        async with _EMBED_SEM:
            resp = await client.embeddings.create(model=model, input=texts, encoding_format="float")
        return [d.embedding for d in resp.data]

    # 分批 + 并发
    starts = list(range(0, len(texts), BATCH))
    async def _one(s):
        async with _EMBED_SEM:
            resp = await client.embeddings.create(
                model=model, input=texts[s:s + BATCH], encoding_format="float"
            )
        return s, [d.embedding for d in resp.data]

    parts = await asyncio.gather(*[_one(s) for s in starts])
    parts.sort(key=lambda x: x[0])
    out = []
    for _, embs in parts:
        out.extend(embs)
    return out
```

### 5.9 Rerank API（HTTP 直调）

```python
async def rerank(query, documents, top_n=5, model=None):
    """DashScope rerank API，不是 OpenAI 兼容接口，必须 HTTP 直调。"""
    model = model or settings.rerank_model
    url = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
    headers = {
        "Authorization": f"Bearer {settings.dashscope_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "input": {"query": query, "documents": documents},
        "parameters": {"top_n": top_n, "return_documents": False},
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    # 返回格式：[{"index": int, "relevance_score": float}, ...]
    return data.get("output", {}).get("results", [])
```

**Rerank 的应用**：在 `tool_keyword_search` 里，FTS5 拉到 N 条候选 → rerank 重排到 top_n。这显著提升精度（FTS5 只是关键词命中，rerank 知道语义）。

---

## 6. 索引服务的进度上报

每个 chat 一个 `details[chat_id]` dict，里面包含：

```python
{
    # 话题构建阶段
    "topic_state": "loading_messages" | "building_reply_groups" | "llm_splitting" | "merging" | "writing",
    "topic_total_messages": 12345,
    "topic_processed_messages": 8000,
    "topic_llm_batches_total": 45,
    "topic_llm_batches_done": 32,
    "topic_count": 234,

    # 向量索引阶段
    "index_state": "preparing" | "embedding" | "writing",
    "index_total": 567,        # 待写入 chunk 总数
    "index_done": 234,         # 已写入 chunk 数
}
```

外层全局：

```python
_index_progress = {
    "running": True,
    "queue": [...],              # 还在排队的 (chat_id, force) 列表
    "current_chats": [...],      # 正在跑的 chat_id 列表（最多 MAX_PARALLEL 个）
    "details": {chat_id: {...}, ...},
    "completed": [...],
    "failed": [{"chat_id": ..., "error": ...}],
}
```

前端用 polling（不是 SSE）拿全局状态，简单可靠。

---

> 下一篇：[`04-agent-core.md`](./04-agent-core.md) — **Agent 主循环（最核心）**：LLM Adapter、流式调用、并发、缓存。
