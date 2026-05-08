"""RAG 检索与问答引擎"""

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator

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
    """基于 SQLite FTS5 的关键词搜索（db 部分派 thread）"""

    def _q() -> list[Message]:
        try:
            rows = db.execute(
                text("SELECT msg_id FROM messages_fts WHERE messages_fts MATCH :kw LIMIT :lim"),
                {"kw": keyword, "lim": limit},
            ).fetchall()
        except Exception:
            return []
        if not rows:
            return []
        ids = [r[0] for r in rows]
        q = db.query(Message).filter(Message.id.in_(ids))
        if chat_ids:
            q = q.filter(Message.chat_id.in_(chat_ids))
        return q.all()

    return await asyncio.to_thread(_q)


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


def _get_topic_context_sync(db: Session, message_ids: list[int], max_context: int = 10) -> list[Message]:
    """获取消息所在话题的上下文（同步版本，供 thread pool 调用）"""
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


async def _get_topic_context(db: Session, message_ids: list[int], max_context: int = 10) -> list[Message]:
    """异步 wrapper：同步查询派到 thread，避免阻塞主循环"""
    return await asyncio.to_thread(_get_topic_context_sync, db, message_ids, max_context)


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
    context_msgs = await _get_topic_context(db, all_msg_ids)
    if not context_msgs:
        context_msgs = await asyncio.to_thread(
            lambda: db.query(Message).filter(Message.id.in_(all_msg_ids)).order_by(Message.date).all()
        )

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


async def answer_question_stream(
    db: Session,
    question: str,
    chat_ids: list[str] | None = None,
    date_range: list[str] | None = None,
    sender: str | None = None,
) -> AsyncIterator[dict]:
    """流式 RAG 问答：yield 各阶段事件 dict
    
    事件类型：
    - status: 阶段状态变化
    - search_result: 检索阶段产出
    - rerank: 重排序结果
    - context: 最终上下文消息预览
    - token: LLM 流式 token
    - done: 完整答案 + 来源
    - error: 错误
    """
    try:
        # ---------- 1. 语义检索 ----------
        yield {"type": "status", "stage": "semantic_search",
               "message": "正在向量数据库中检索相似内容..."}
        semantic_results = await _semantic_search(
            question, chat_ids=chat_ids, date_range=date_range, n_results=10
        )
        yield {
            "type": "search_result",
            "kind": "semantic",
            "count": len(semantic_results),
            "preview": [
                {
                    "snippet": (r.get("document") or "")[:120],
                    "distance": r.get("distance"),
                }
                for r in semantic_results[:5]
            ],
        }

        # ---------- 2. 关键词检索 ----------
        yield {"type": "status", "stage": "keyword_search",
               "message": "正在使用 FTS5 全文索引搜索关键词..."}
        keyword_msgs = await _keyword_search(db, question, chat_ids=chat_ids, limit=10)
        yield {
            "type": "search_result",
            "kind": "keyword",
            "count": len(keyword_msgs),
            "preview": [
                {
                    "sender": m.sender,
                    "date": m.date.strftime("%Y-%m-%d %H:%M") if m.date else None,
                    "snippet": (m.text_plain or "")[:120],
                }
                for m in keyword_msgs[:5]
            ],
        }

        # ---------- 3. 合并消息 ID ----------
        semantic_msg_ids: list[int] = []
        for r in semantic_results:
            meta = r.get("metadata", {})
            msg_ids = meta.get("message_ids", [])
            if isinstance(msg_ids, str):
                msg_ids = json.loads(msg_ids)
            semantic_msg_ids.extend(msg_ids)
        keyword_msg_ids = [m.id for m in keyword_msgs]
        all_msg_ids = list(set(semantic_msg_ids + keyword_msg_ids))

        if not all_msg_ids:
            yield {
                "type": "done",
                "answer": "根据现有聊天记录，未找到与您问题相关的信息。",
                "sources": [],
                "confidence": "low",
            }
            return

        # ---------- 4. 话题上下文扩展 ----------
        yield {"type": "status", "stage": "context_expand",
               "message": f"找到 {len(all_msg_ids)} 条相关消息，正在扩展话题上下文..."}
        context_msgs = await _get_topic_context(db, all_msg_ids)
        if not context_msgs:
            context_msgs = await asyncio.to_thread(
                lambda: db.query(Message)
                .filter(Message.id.in_(all_msg_ids))
                .order_by(Message.date)
                .all()
            )
        before_rerank = len(context_msgs)

        # ---------- 5. Rerank ----------
        if len(context_msgs) > 5:
            yield {"type": "status", "stage": "rerank",
                   "message": f"对 {before_rerank} 条候选消息进行 rerank 重排序..."}
            docs = [_format_chunk([m]) for m in context_msgs if m.text_plain]
            if docs:
                try:
                    reranked = await llm_adapter.rerank(question, docs, top_n=8)
                    reranked_indices = [r["index"] for r in reranked]
                    filtered = [m for m in context_msgs if m.text_plain]
                    context_msgs = [filtered[i] for i in reranked_indices if i < len(filtered)]
                    yield {
                        "type": "rerank",
                        "before": before_rerank,
                        "after": len(context_msgs),
                        "top_scores": [round(r.get("relevance_score", 0), 3) for r in reranked[:5]],
                    }
                except Exception as e:
                    context_msgs = context_msgs[:8]
                    yield {"type": "status", "stage": "rerank_skip",
                           "message": f"Rerank 失败，使用原序前 8 条: {e}"}

        # ---------- 6. 上下文预览 ----------
        final_ctx = context_msgs[:10]
        yield {
            "type": "context",
            "count": len(final_ctx),
            "preview": [
                {
                    "sender": m.sender,
                    "date": m.date.strftime("%Y-%m-%d %H:%M") if m.date else None,
                    "snippet": (m.text_plain or "")[:120],
                }
                for m in final_ctx[:5]
            ],
        }

        # ---------- 7. LLM 流式生成（带 usage 捕获） ----------
        yield {"type": "status", "stage": "generating",
               "message": "正在生成回答..."}
        prompt_template = _load_prompt("qa_answer.txt")
        chunks_text = _format_chunk(final_ctx)
        prompt = prompt_template.replace("{retrieved_chunks}", chunks_text).replace("{question}", question)

        model = settings.llm_model_qa
        client = llm_adapter.get_client_for_model(model)
        is_kimi = llm_adapter.is_kimi_model(model)
        kwargs = dict(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048,
            stream=True,
            stream_options={"include_usage": True},
        )
        if is_kimi:
            kwargs.update(llm_adapter.kimi_chat_kwargs(model, False))
        else:
            kwargs["temperature"] = 0.3

        sem = llm_adapter.get_chat_semaphore(model)
        full_answer_parts: list[str] = []
        last_usage: dict | None = None
        async with sem:
            stream = await client.chat.completions.create(**kwargs)
            async for chunk in stream:
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    last_usage = {
                        "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                        "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                        "total_tokens": getattr(usage, "total_tokens", 0) or 0,
                    }
                choice = chunk.choices[0] if chunk.choices else None
                if not choice:
                    continue
                if choice.delta.content:
                    full_answer_parts.append(choice.delta.content)
                    yield {"type": "token", "text": choice.delta.content}

        full_answer = "".join(full_answer_parts)

        # 推送 usage 事件（供前端 ContextBadge 更新）
        if last_usage is not None:
            max_ctx = llm_adapter.get_context_window(model)
            yield {
                "type": "usage",
                "prompt_tokens": last_usage["prompt_tokens"],
                "completion_tokens": last_usage["completion_tokens"],
                "total_tokens": last_usage["total_tokens"],
                "max_context": max_ctx,
                "percent": round(last_usage["prompt_tokens"] / max_ctx, 4) if max_ctx else 0.0,
                "model": model,
            }

        # ---------- 8. 构建来源 ----------
        sources: list[dict] = []
        seen_topics: set[int | None] = set()
        for m in final_ctx[:5]:
            if m.topic_id in seen_topics and m.topic_id is not None:
                continue
            seen_topics.add(m.topic_id)
            sources.append({
                "message_ids": [m.id],
                "sender": m.sender,
                "date": m.date.strftime("%Y-%m-%d") if m.date else None,
                "preview": (m.text_plain or "")[:200],
                "topic_id": m.topic_id,
            })

        confidence = "high" if len(final_ctx) >= 5 else "medium" if final_ctx else "low"

        yield {
            "type": "done",
            "answer": full_answer,
            "sources": sources,
            "confidence": confidence,
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        yield {"type": "error", "error": str(e)[:300]}
