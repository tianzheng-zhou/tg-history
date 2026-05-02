"""向量嵌入服务 — ChromaDB 集成"""

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
    batch_size: int = 50,
):
    """批量添加文档到向量索引"""
    collection = get_or_create_collection()

    for i in range(0, len(texts), batch_size):
        batch_ids = ids[i : i + batch_size]
        batch_texts = texts[i : i + batch_size]
        batch_meta = metadatas[i : i + batch_size] if metadatas else None

        embeddings = await llm_adapter.embed(batch_texts)

        collection.upsert(
            ids=batch_ids,
            embeddings=embeddings,
            documents=batch_texts,
            metadatas=batch_meta,
        )


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


async def build_index_for_chat(db_session, chat_id: str):
    """为指定群聊构建向量索引"""
    from backend.models.database import Message, Topic

    # 按话题分组获取消息
    topics = db_session.query(Topic).filter(Topic.chat_id == chat_id).all()

    ids = []
    texts = []
    metadatas = []

    for topic in topics:
        msgs = (
            db_session.query(Message)
            .filter(Message.topic_id == topic.id)
            .order_by(Message.date)
            .all()
        )
        if not msgs:
            continue

        # 合并同一话题的消息为一个文档
        lines = []
        msg_ids = []
        senders = set()
        for m in msgs:
            if m.text_plain:
                date_str = m.date.strftime("%Y-%m-%d %H:%M") if m.date else ""
                lines.append(f"[{date_str}] {m.sender or '未知'}: {m.text_plain}")
                msg_ids.append(m.id)
                if m.sender:
                    senders.add(m.sender)

        if not lines:
            continue

        doc_text = "\n".join(lines)
        doc_id = f"{chat_id}_topic_{topic.id}"

        ids.append(doc_id)
        texts.append(doc_text[:8000])  # 限制文档长度
        metadatas.append({
            "chat_id": chat_id,
            "topic_id": topic.id,
            "message_ids": str(msg_ids),
            "participants": ", ".join(senders),
            "start_date": topic.start_date.isoformat() if topic.start_date else "",
            "end_date": topic.end_date.isoformat() if topic.end_date else "",
            "message_count": len(msgs),
        })

    if ids:
        await add_documents(ids, texts, metadatas)

    return len(ids)
