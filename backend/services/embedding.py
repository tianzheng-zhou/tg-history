"""向量嵌入服务 — ChromaDB 集成"""

import asyncio
from pathlib import Path

import chromadb

from backend.config import settings
from backend.services import llm_adapter

_client: chromadb.ClientAPI | None = None
COLLECTION_NAME = "tg_messages"


def _get_chroma_client() -> chromadb.ClientAPI:
    global _client
    if _client is None:
        path = Path(settings.chroma_dir)
        path.mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(path=str(path))
    return _client


def get_or_create_collection():
    client = _get_chroma_client()
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


async def add_documents(
    ids: list[str],
    texts: list[str],
    metadatas: list[dict] | None = None,
    batch_size: int = 10,
    progress: dict | None = None,
):
    """批量添加文档到向量索引（并发 embedding）"""
    collection = get_or_create_collection()

    async def _embed_batch(start: int) -> tuple[int, list[list[float]]]:
        batch_texts = texts[start : start + batch_size]
        embeddings = await llm_adapter.embed(batch_texts)
        return start, embeddings

    starts = list(range(0, len(texts), batch_size))
    # 并发计算所有 batch 的 embedding（受 _EMBED_SEM 限流）
    embed_tasks = [_embed_batch(s) for s in starts]

    # 按完成顺序写入 chroma，及时更新进度
    for coro in asyncio.as_completed(embed_tasks):
        start, embeddings = await coro
        batch_ids = ids[start : start + batch_size]
        batch_texts = texts[start : start + batch_size]
        batch_meta = metadatas[start : start + batch_size] if metadatas else None
        collection.upsert(
            ids=batch_ids,
            embeddings=embeddings,
            documents=batch_texts,
            metadatas=batch_meta,
        )
        if progress is not None:
            progress["index_done"] = progress.get("index_done", 0) + len(batch_ids)


async def search_similar(
    query: str,
    n_results: int = 10,
    where: dict | None = None,
) -> list[dict]:
    """语义相似度搜索"""
    collection = get_or_create_collection()

    if collection.count() == 0:
        return []

    query_embedding = await llm_adapter.embed([query])

    kwargs = {
        "query_embeddings": query_embedding,
        "n_results": min(n_results, collection.count()),
    }
    if where:
        kwargs["where"] = where

    results = collection.query(**kwargs)

    output = []
    if results and results["ids"]:
        for i, doc_id in enumerate(results["ids"][0]):
            item = {
                "id": doc_id,
                "document": results["documents"][0][i] if results["documents"] else "",
                "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                "distance": results["distances"][0][i] if results["distances"] else 0,
            }
            output.append(item)

    return output


MAX_CHUNK_CHARS = 2000
OVERLAP_LINES = 3


def _chunk_lines(lines: list[str], msg_ids: list[int]) -> list[tuple[str, list[int]]]:
    """将过长的话题按字符数切分成多个 chunk，相邻 chunk 有少量重叠"""
    chunks = []
    cur_lines = []
    cur_ids = []
    cur_len = 0

    for line, mid in zip(lines, msg_ids):
        line_len = len(line) + 1  # +1 for newline
        if cur_len + line_len > MAX_CHUNK_CHARS and cur_lines:
            chunks.append(("\n".join(cur_lines), list(cur_ids)))
            # 保留尾部几行作为下一个 chunk 的上文重叠
            overlap = min(OVERLAP_LINES, len(cur_lines))
            cur_lines = cur_lines[-overlap:]
            cur_ids = cur_ids[-overlap:]
            cur_len = sum(len(l) + 1 for l in cur_lines)
        cur_lines.append(line)
        cur_ids.append(mid)
        cur_len += line_len

    if cur_lines:
        chunks.append(("\n".join(cur_lines), list(cur_ids)))

    return chunks


def _collect_topic_chunks(
    db_session, chat_id: str, topics: list,
) -> tuple[list[str], list[str], list[dict]]:
    """从给定 topic 列表生成 (ids, texts, metadatas)。供全量/增量复用。"""
    from backend.models.database import Message

    ids: list[str] = []
    texts: list[str] = []
    metadatas: list[dict] = []

    for topic in topics:
        msgs = (
            db_session.query(Message)
            .filter(Message.topic_id == topic.id)
            .order_by(Message.date)
            .all()
        )
        if not msgs:
            continue

        lines: list[str] = []
        msg_ids: list[int] = []
        senders: set[str] = set()
        for m in msgs:
            if m.text_plain:
                date_str = m.date.strftime("%Y-%m-%d %H:%M") if m.date else ""
                lines.append(f"[{date_str}] {m.sender or '未知'}: {m.text_plain}")
                msg_ids.append(m.id)
                if m.sender:
                    senders.add(m.sender)

        if not lines:
            continue

        participants = ", ".join(senders)
        chunks = _chunk_lines(lines, msg_ids)

        for ci, (chunk_text, chunk_ids) in enumerate(chunks):
            doc_id = (
                f"{chat_id}_topic_{topic.id}_c{ci}"
                if len(chunks) > 1
                else f"{chat_id}_topic_{topic.id}"
            )
            ids.append(doc_id)
            texts.append(chunk_text)
            metadatas.append({
                "chat_id": chat_id,
                "topic_id": topic.id,
                "chunk_index": ci,
                "chunk_total": len(chunks),
                "message_ids": str(chunk_ids),
                "participants": participants,
                "start_date": topic.start_date.isoformat() if topic.start_date else "",
                "end_date": topic.end_date.isoformat() if topic.end_date else "",
                "message_count": len(chunk_ids),
            })

    return ids, texts, metadatas


def _delete_chunks_for_topics(chat_id: str, topic_ids: set[int]) -> None:
    """从 ChromaDB 删除指定 chat + topic_ids 的所有 chunks。

    使用 metadata where 过滤；ChromaDB 0.4+ 支持 ``$and`` 与 ``$in``。
    出错时降级为按整 chat 删除（最坏情况下也不会留脏向量）。
    """
    if not topic_ids:
        return
    collection = get_or_create_collection()
    topic_id_list = list(topic_ids)
    try:
        if len(topic_id_list) == 1:
            collection.delete(where={
                "$and": [
                    {"chat_id": chat_id},
                    {"topic_id": topic_id_list[0]},
                ]
            })
        else:
            collection.delete(where={
                "$and": [
                    {"chat_id": chat_id},
                    {"topic_id": {"$in": topic_id_list}},
                ]
            })
    except Exception as e:
        # 兜底：删整个 chat 的旧 chunks（之后 build_index_for_chat 会重新写入完整索引）
        import logging
        logging.getLogger(__name__).warning(
            "按 topic_id 删除失败，降级到 chat 级清空: %s", e
        )
        try:
            collection.delete(where={"chat_id": chat_id})
        except Exception as e2:
            logging.getLogger(__name__).error("chat 级清空也失败: %s", e2)
            raise


async def build_index_for_chat_incremental(
    db_session,
    chat_id: str,
    changed_topic_ids: set[int],
    progress: dict | None = None,
) -> int:
    """仅对 changed_topic_ids 重新切 chunk + 重新 embed。

    - changed_topic_ids 为空：什么都不做（旧向量保留），返回 0
    - 否则先按 topic_id 删除 ChromaDB 里这些 topic 的旧 chunks，再重新写入
    - 返回本次写入的 chunk 数量
    """
    if not changed_topic_ids:
        if progress is not None:
            progress["index_total"] = 0
            progress["index_done"] = 0
        return 0

    from backend.models.database import Topic

    topics = (
        db_session.query(Topic)
        .filter(Topic.chat_id == chat_id, Topic.id.in_(changed_topic_ids))
        .all()
    )

    ids, texts, metadatas = _collect_topic_chunks(db_session, chat_id, topics)

    if progress is not None:
        progress["index_total"] = len(ids)
        progress["index_done"] = 0

    # 先删旧的（按 topic_id 精确删除，未变 topic 的向量保持不动）
    _delete_chunks_for_topics(chat_id, changed_topic_ids)

    if ids:
        await add_documents(ids, texts, metadatas, progress=progress)

    return len(ids)


async def build_index_for_chat(db_session, chat_id: str, progress: dict | None = None):
    """为指定群聊构建向量索引（大话题自动切分）。

    全量重建：先清空该 chat 的所有旧 chunks，再重新写入。
    """
    from backend.models.database import Topic

    topics = db_session.query(Topic).filter(Topic.chat_id == chat_id).all()
    ids, texts, metadatas = _collect_topic_chunks(db_session, chat_id, topics)

    if progress is not None:
        progress["index_total"] = len(ids)
        progress["index_done"] = 0

    # 全量重建：先清空该 chat 的所有旧 chunks，避免遗留已删 topic 的向量
    try:
        collection = get_or_create_collection()
        collection.delete(where={"chat_id": chat_id})
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            "全量重建前清空 chat=%s 旧 chunks 失败（继续 upsert，可能有残留）: %s",
            chat_id, e,
        )

    if ids:
        await add_documents(ids, texts, metadatas, progress=progress)

    return len(ids)
