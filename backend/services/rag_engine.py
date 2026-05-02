"""RAG 检索与问答引擎"""

import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.config import settings
from backend.models.database import Message, Topic
from backend.models.schemas import AskResponse, SourceItem
from backend.services import llm_adapter
from backend.services.embedding import get_or_create_collection, search_similar

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _format_chunk(messages: list[Message]) -> str:
    lines = []
    for m in messages:
        date_str = m.date.strftime("%Y-%m-%d %H:%M") if m.date else "未知时间"
        sender = m.sender or "未知"
        text = m.text_plain or ""
        if text:
            lines.append(f"[{date_str}] {sender}: {text}")
    return "\n".join(lines)


async def _keyword_search(
    db: Session,
    keyword: str,
    chat_ids: list[str] | None = None,
    limit: int = 10,
) -> list[Message]:
    """基于 SQLite FTS5 的关键词搜索"""
    try:
        rows = db.execute(
            text("SELECT rowid FROM messages_fts WHERE messages_fts MATCH :kw LIMIT :lim"),
            {"kw": keyword, "lim": limit},
        ).fetchall()
    except Exception:
        return []

    if not rows:
        return []

    ids = [r[0] for r in rows]
    query = db.query(Message).filter(Message.id.in_(ids))
    if chat_ids:
        query = query.filter(Message.chat_id.in_(chat_ids))
    return query.all()


async def _semantic_search(
    query: str,
    chat_ids: list[str] | None = None,
    date_range: list[str] | None = None,
    n_results: int = 10,
) -> list[dict]:
    """向量语义检索"""
    where_filter = {}
    if chat_ids and len(chat_ids) == 1:
        where_filter["chat_id"] = chat_ids[0]

    results = await search_similar(query, n_results=n_results, where=where_filter or None)
    return results


def _get_topic_context(db: Session, message_ids: list[int], max_context: int = 10) -> list[Message]:
    """获取消息所在话题的上下文"""
    if not message_ids:
        return []

    topic_ids = (
        db.query(Message.topic_id)
        .filter(Message.id.in_(message_ids), Message.topic_id.isnot(None))
        .distinct()
        .all()
    )
    topic_ids = [t[0] for t in topic_ids]

    if not topic_ids:
        return []

    context_msgs = (
        db.query(Message)
        .filter(Message.topic_id.in_(topic_ids))
        .order_by(Message.date)
        .limit(max_context * len(topic_ids))
        .all()
    )
    return context_msgs


async def answer_question(
    db: Session,
    question: str,
    chat_ids: list[str] | None = None,
    date_range: list[str] | None = None,
    sender: str | None = None,
) -> AskResponse:
    """RAG 问答主流程"""
    # 1. 语义检索
    semantic_results = await _semantic_search(
        question, chat_ids=chat_ids, date_range=date_range, n_results=10
    )

    # 2. 关键词检索
    keyword_msgs = await _keyword_search(db, question, chat_ids=chat_ids, limit=10)

    # 3. 收集所有相关消息 ID
    semantic_msg_ids = []
    for r in semantic_results:
        meta = r.get("metadata", {})
        msg_ids = meta.get("message_ids", [])
        if isinstance(msg_ids, str):
            msg_ids = json.loads(msg_ids)
        semantic_msg_ids.extend(msg_ids)

    keyword_msg_ids = [m.id for m in keyword_msgs]
    all_msg_ids = list(set(semantic_msg_ids + keyword_msg_ids))

    if not all_msg_ids:
        return AskResponse(
            answer="根据现有聊天记录，未找到与您问题相关的信息。",
            sources=[],
            confidence="low",
        )

    # 4. 获取话题上下文
    context_msgs = _get_topic_context(db, all_msg_ids)
    if not context_msgs:
        context_msgs = db.query(Message).filter(Message.id.in_(all_msg_ids)).order_by(Message.date).all()

    # 5. 可选：用 Rerank 重排序
    if len(context_msgs) > 5:
        docs = [_format_chunk([m]) for m in context_msgs if m.text_plain]
        if docs:
            try:
                reranked = await llm_adapter.rerank(question, docs, top_n=8)
                reranked_indices = [r["index"] for r in reranked]
                filtered = [m for m in context_msgs if m.text_plain]
                context_msgs = [filtered[i] for i in reranked_indices if i < len(filtered)]
            except Exception:
                context_msgs = context_msgs[:8]

    # 6. 格式化上下文
    chunks_text = _format_chunk(context_msgs[:10])

    # 7. 生成回答
    prompt_template = _load_prompt("qa_answer.txt")
    prompt = prompt_template.replace("{retrieved_chunks}", chunks_text).replace("{question}", question)
    answer = await llm_adapter.chat(
        messages=[{"role": "user", "content": prompt}],
        model=settings.llm_model_qa,
        temperature=0.3,
        max_tokens=2048,
    )

    # 8. 构建来源引用
    sources = []
    seen_topics: set[int | None] = set()
    for m in context_msgs[:5]:
        if m.topic_id in seen_topics and m.topic_id is not None:
            continue
        seen_topics.add(m.topic_id)
        sources.append(SourceItem(
            message_ids=[m.id],
            sender=m.sender,
            date=m.date.strftime("%Y-%m-%d") if m.date else None,
            preview=(m.text_plain or "")[:200],
            topic_id=m.topic_id,
        ))

    confidence = "high" if len(context_msgs) >= 5 else "medium" if context_msgs else "low"

    return AskResponse(answer=answer, sources=sources, confidence=confidence)
