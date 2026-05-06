"""QA Agent 工具集。

每个工具由 (schema, handler) 组成：
- schema: OpenAI Function Calling 格式的工具定义，供 LLM 理解
- handler: async 执行函数，输入 kwargs，返回 JSON-serializable dict
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from backend.models.database import Import, Message, Topic
from backend.services import artifact_service, llm_adapter
from backend.services.embedding import search_similar
from backend.services.artifact_service import (
    ArtifactError,
    ArtifactKeyConflict,
    ArtifactNotFound,
    StrReplaceError,
)


def _msg_to_dict(m: Message, preview_len: int = 400) -> dict:
    """统一的消息 dict 格式"""
    return {
        "message_id": m.id,
        "chat_id": m.chat_id,
        "sender": m.sender,
        "date": m.date.strftime("%Y-%m-%d %H:%M") if m.date else None,
        "text": (m.text_plain or "")[:preview_len],
        "topic_id": m.topic_id,
    }


def _parse_date(s: str | None) -> datetime | None:
    """解析 YYYY-MM-DD 日期字符串。空/非法返回 None。"""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _coerce_str_list(v) -> list[str]:
    """宽容地转为 list[str]：接受单个 str / list / None。"""
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if x is not None and str(x).strip()]
    return []


def _coerce_int_list(v) -> list[int]:
    """宽容地转为 list[int]：接受单个 int / list / None。"""
    if v is None:
        return []
    if isinstance(v, int):
        return [v]
    if isinstance(v, (list, tuple)):
        out: list[int] = []
        for x in v:
            try:
                out.append(int(x))
            except (ValueError, TypeError):
                pass
        return out
    return []


def _build_chroma_where(
    chat_ids: list[str] | None,
    topic_ids: list[int] | None,
    start_date: str | None,
    end_date: str | None,
) -> dict | None:
    """构造 Chroma metadata 过滤表达式。多条件 $and 合并。

    按 metadata 字段：chat_id / topic_id / start_date / end_date。
    返回 None 表示无过滤（避免传空 dict 给 chromadb）。
    """
    clauses: list[dict] = []
    if chat_ids:
        if len(chat_ids) == 1:
            clauses.append({"chat_id": chat_ids[0]})
        else:
            clauses.append({"chat_id": {"$in": list(chat_ids)}})
    if topic_ids:
        if len(topic_ids) == 1:
            clauses.append({"topic_id": topic_ids[0]})
        else:
            clauses.append({"topic_id": {"$in": list(topic_ids)}})
    # 日期过滤：chunk 的 [start_date, end_date] 区间与查询区间有交集
    # chunk.end_date >= query.start_date AND chunk.start_date <= query.end_date
    sd = _parse_date(start_date)
    ed = _parse_date(end_date)
    if sd is not None:
        clauses.append({"end_date": {"$gte": sd.isoformat()}})
    if ed is not None:
        clauses.append({"start_date": {"$lte": ed.isoformat() + "T23:59:59"}})
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def _post_filter_msgs_by_sender(
    db: Session, msg_ids: list[int], senders: list[str]
) -> set[int]:
    """从 SQL messages 表查出 sender 匹配的 message_id 集合（senders 之间 OR）。"""
    if not msg_ids or not senders:
        return set(msg_ids or [])
    sender_clauses = [Message.sender.like(f"%{s}%") for s in senders]
    rows = (
        db.query(Message.id)
        .filter(Message.id.in_(msg_ids), or_(*sender_clauses))
        .all()
    )
    return {r[0] for r in rows}


# ---------------------- Tool handlers ----------------------

async def tool_list_chats(db: Session) -> dict:
    """列出所有已导入的群聊"""

    def _q():
        imports = db.query(Import).order_by(Import.message_count.desc()).all()
        return [
            {
                "chat_id": i.chat_id,
                "chat_name": i.chat_name,
                "message_count": i.message_count,
                "date_range": i.date_range,
                "index_built": bool(i.index_built),
            }
            for i in imports
        ]

    return {"chats": await asyncio.to_thread(_q)}


async def tool_semantic_search(
    db: Session,
    query: str,
    chat_ids: list[str] | None = None,
    limit: int = 30,
    start_date: str | None = None,
    end_date: str | None = None,
    topic_ids: list[int] | None = None,
    senders: list[str] | None = None,
    min_messages_in_chunk: int = 0,
) -> dict:
    """向量语义检索（limit 上限 200）。支持多维交叉过滤：

    - chat_ids / topic_ids / start_date / end_date → Chroma metadata 过滤
    - senders → SQL post-filter（metadata 只有 participants 汇总串，无法精确命中）
    - min_messages_in_chunk → post-filter 小 chunk
    """
    limit = max(1, min(int(limit), 200))
    chat_ids = _coerce_str_list(chat_ids) or None
    topic_ids = _coerce_int_list(topic_ids) or None
    senders = _coerce_str_list(senders) or None

    where_filter = _build_chroma_where(chat_ids, topic_ids, start_date, end_date)

    # senders / min_messages 是 post-filter；为了保证最终返回足够，启用 post-filter
    # 时预取更多候选。
    fetch_n = limit
    if senders or min_messages_in_chunk > 0:
        fetch_n = min(limit * 4, 200)

    results = await search_similar(query, n_results=fetch_n, where=where_filter)

    # 展开 chunk metadata
    items = []
    all_msg_ids: list[int] = []
    for r in results:
        meta = r.get("metadata", {})
        msg_ids_raw = meta.get("message_ids", [])
        if isinstance(msg_ids_raw, str):
            # 兼容两种编码：JSON（新版）和 Python repr（老版）
            parsed = None
            try:
                parsed = json.loads(msg_ids_raw)
            except Exception:
                try:
                    import ast
                    parsed = ast.literal_eval(msg_ids_raw)
                except Exception:
                    parsed = []
            msg_ids_raw = parsed or []
        msg_ids = [int(x) for x in (msg_ids_raw or []) if isinstance(x, (int, str))]
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

    # senders post-filter：一次 SQL 拉所有 chunk 覆盖的 message，按 sender 过滤
    if senders and all_msg_ids:
        def _q():
            return _post_filter_msgs_by_sender(db, all_msg_ids, senders)
        matched = await asyncio.to_thread(_q)
        items = [it for it in items if any(mid in matched for mid in it["message_ids"])]

    if min_messages_in_chunk > 0:
        items = [it for it in items if it["total_messages_in_chunk"] >= min_messages_in_chunk]

    items = items[:limit]
    return {"results": items, "count": len(items)}


async def tool_keyword_search(
    db: Session,
    keyword: str | None = None,
    keywords: list[str] | None = None,
    chat_ids: list[str] | None = None,
    limit: int = 30,
    start_date: str | None = None,
    end_date: str | None = None,
    senders: list[str] | None = None,
    topic_ids: list[int] | None = None,
) -> dict:
    """关键词检索：FTS5 trigram 优先，0 命中时回退 LIKE。rerank 按相关性重排。

    参数：
    - keyword: 单关键词 / FTS5 表达式（兼容老参数名）
    - keywords: 多关键词列表，自动 OR 拼接
    - chat_ids / topic_ids / start_date / end_date / senders: SQL where 过滤
    """
    # 参数规范化
    kws = _coerce_str_list(keywords)
    if keyword:
        kws.append(keyword)
    kws = [k.strip() for k in kws if k and k.strip()]
    if not kws:
        return {"results": [], "count": 0, "error": "需提供 keyword 或 keywords"}
    fts_query = " OR ".join(kws)
    primary_kw = kws[0]

    chat_ids = _coerce_str_list(chat_ids)
    topic_ids = _coerce_int_list(topic_ids)
    senders = _coerce_str_list(senders)
    sd = _parse_date(start_date)
    ed = _parse_date(end_date)

    limit = max(1, min(int(limit), 200))
    fetch_limit = min(limit * 3, 200)
    from sqlalchemy import text as sa_text

    def _apply_filters(q):
        if chat_ids:
            q = q.filter(Message.chat_id.in_(chat_ids))
        if topic_ids:
            q = q.filter(Message.topic_id.in_(topic_ids))
        if sd is not None:
            q = q.filter(Message.date >= sd)
        if ed is not None:
            q = q.filter(Message.date <= ed.replace(hour=23, minute=59, second=59))
        if senders:
            q = q.filter(or_(*[Message.sender.like(f"%{s}%") for s in senders]))
        return q

    def _fts_then_like() -> tuple[list[Message], str]:
        msgs_local: list[Message] = []
        method = "fts5"
        try:
            rows = db.execute(
                sa_text("SELECT rowid FROM messages_fts WHERE messages_fts MATCH :kw LIMIT :lim"),
                {"kw": fts_query, "lim": fetch_limit},
            ).fetchall()
            if rows:
                ids = [r[0] for r in rows]
                q = db.query(Message).filter(Message.id.in_(ids))
                q = _apply_filters(q)
                msgs_local = q.order_by(Message.date).all()
        except Exception as e:
            method = f"fts5_err({type(e).__name__})"

        if not msgs_local:
            method = "like"
            kw_clean = primary_kw.split(" OR ")[0].split()[0].strip('"').strip()
            if kw_clean:
                q = db.query(Message).filter(Message.text_plain.like(f"%{kw_clean}%"))
                q = _apply_filters(q)
                msgs_local = q.order_by(Message.date.desc()).limit(fetch_limit).all()
                msgs_local.reverse()
        return msgs_local, method

    msgs, used_method = await asyncio.to_thread(_fts_then_like)

    # rerank：用语义相关性重排候选结果
    reranked = False
    if len(msgs) > 1:
        try:
            docs = [(m.text_plain or "")[:500] for m in msgs]
            rerank_results = await llm_adapter.rerank(
                query=fts_query, documents=docs, top_n=min(limit, len(msgs)),
            )
            if rerank_results:
                reranked_msgs = []
                for rr in rerank_results:
                    idx = rr["index"]
                    if 0 <= idx < len(msgs):
                        reranked_msgs.append(msgs[idx])
                msgs = reranked_msgs
                reranked = True
                used_method += "+rerank"
        except Exception:
            pass

    if not reranked:
        msgs = msgs[:limit]

    return {
        "results": [_msg_to_dict(m, preview_len=250) for m in msgs],
        "count": len(msgs),
        "method": used_method,
    }


async def tool_fetch_messages(
    db: Session,
    message_ids: list[int],
    full_text: bool = False,
    limit: int | None = None,
    context_window: int = 0,
) -> dict:
    """按 message_id 列表获取完整消息内容。

    - limit: 返回上限（默认 50 / 最大 200）
    - context_window: >0 时顺带拉每条消息在同 chat 时序上的前后 N 条上下文
    """
    message_ids = _coerce_int_list(message_ids)
    if not message_ids:
        return {"messages": [], "count": 0}

    eff_limit = limit if limit else 50
    eff_limit = max(1, min(int(eff_limit), 200))
    truncated = len(message_ids) > eff_limit
    target_ids = message_ids[:eff_limit]
    context_window = max(0, min(int(context_window or 0), 20))

    def _q():
        base = (
            db.query(Message)
            .filter(Message.id.in_(target_ids))
            .order_by(Message.date)
            .all()
        )
        if context_window <= 0 or not base:
            return base, []

        # 按 chat_id 分组收集时序邻居的 id（SQL 起点：Message.date 有索引）
        extra_ids: set[int] = set()
        for m in base:
            if not m.date or not m.chat_id:
                continue
            before = (
                db.query(Message.id)
                .filter(
                    Message.chat_id == m.chat_id,
                    Message.date < m.date,
                    Message.id != m.id,
                )
                .order_by(Message.date.desc())
                .limit(context_window)
                .all()
            )
            after = (
                db.query(Message.id)
                .filter(
                    Message.chat_id == m.chat_id,
                    Message.date > m.date,
                    Message.id != m.id,
                )
                .order_by(Message.date)
                .limit(context_window)
                .all()
            )
            extra_ids.update(b[0] for b in before)
            extra_ids.update(a[0] for a in after)
        base_id_set = {m.id for m in base}
        extra_ids -= base_id_set
        if not extra_ids:
            return base, []
        ctx = (
            db.query(Message)
            .filter(Message.id.in_(extra_ids))
            .order_by(Message.chat_id, Message.date)
            .all()
        )
        return base, ctx

    msgs, ctx_msgs = await asyncio.to_thread(_q)
    preview_len = 2000 if full_text else 500
    out: dict = {
        "messages": [_msg_to_dict(m, preview_len=preview_len) for m in msgs],
        "count": len(msgs),
        "truncated": truncated,
    }
    if ctx_msgs:
        out["context_messages"] = [_msg_to_dict(m, preview_len=preview_len) for m in ctx_msgs]
        out["context_count"] = len(ctx_msgs)
    return out


async def tool_fetch_topic_context(
    db: Session,
    topic_id: int,
    limit: int = 30,
) -> dict:
    """获取某话题的完整消息列表（按时间排序）。limit 默认 30、最大 200。"""
    limit = max(1, min(int(limit), 200))

    def _q():
        topic_local = db.query(Topic).filter(Topic.id == topic_id).first()
        if not topic_local:
            return None, []
        msgs_local = (
            db.query(Message)
            .filter(Message.topic_id == topic_id)
            .order_by(Message.date)
            .limit(limit)
            .all()
        )
        return topic_local, msgs_local

    topic, msgs = await asyncio.to_thread(_q)
    if not topic:
        return {"error": f"话题 {topic_id} 不存在", "messages": []}

    return {
        "topic_id": topic_id,
        "chat_id": topic.chat_id,
        "start_date": topic.start_date.strftime("%Y-%m-%d %H:%M") if topic.start_date else None,
        "end_date": topic.end_date.strftime("%Y-%m-%d %H:%M") if topic.end_date else None,
        "participant_count": topic.participant_count,
        "message_count": topic.message_count,
        "summary": topic.summary,
        "category": topic.category,
        "messages": [_msg_to_dict(m, preview_len=400) for m in msgs],
    }


async def tool_search_by_sender(
    db: Session,
    senders: list[str] | None = None,
    sender: str | None = None,
    chat_ids: list[str] | None = None,
    keywords: list[str] | None = None,
    keyword: str | None = None,
    topic_ids: list[int] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50,
) -> dict:
    """查询某些发言人的消息。senders 和 keywords 组内都是 OR 关系。"""
    senders_list = _coerce_str_list(senders)
    if sender:
        senders_list.append(sender)
    senders_list = [s.strip() for s in senders_list if s and s.strip()]
    if not senders_list:
        return {"results": [], "count": 0, "error": "需提供 sender 或 senders"}

    keywords_list = _coerce_str_list(keywords)
    if keyword:
        keywords_list.append(keyword)
    keywords_list = [k.strip() for k in keywords_list if k and k.strip()]

    chat_ids = _coerce_str_list(chat_ids)
    topic_ids = _coerce_int_list(topic_ids)
    sd = _parse_date(start_date)
    ed = _parse_date(end_date)
    limit = max(1, min(int(limit), 200))

    def _q():
        q = db.query(Message).filter(
            or_(*[Message.sender.like(f"%{s}%") for s in senders_list])
        )
        if chat_ids:
            q = q.filter(Message.chat_id.in_(chat_ids))
        if topic_ids:
            q = q.filter(Message.topic_id.in_(topic_ids))
        if keywords_list:
            q = q.filter(or_(*[Message.text_plain.like(f"%{k}%") for k in keywords_list]))
        if sd is not None:
            q = q.filter(Message.date >= sd)
        if ed is not None:
            q = q.filter(Message.date <= ed.replace(hour=23, minute=59, second=59))
        return q.order_by(Message.date.desc()).limit(limit).all()

    msgs = await asyncio.to_thread(_q)
    return {
        "results": [_msg_to_dict(m, preview_len=300) for m in msgs],
        "count": len(msgs),
    }


async def tool_get_user_profile(
    db: Session,
    *,
    sender_id: str | None = None,
    username: str | None = None,
    use_cache: bool = True,
) -> dict:
    """按需调 Telegram API 拉用户主页（display name / username / bio / 共同群数等）。

    至少传 ``sender_id`` 或 ``username`` 之一：
        sender_id="user6747261966"   # 来自 messages.sender_id
        username="cwoiuhwooiv"        # 来自截图或链接，可带 @

    24h 内同 sender_id 的二次调用走缓存；超过则重新调 API。
    Telegram 未登录、用户不存在、隐私限制等都返回结构化错误（带 ``code``）。
    """
    # 委托给 service 模块（限流 / 缓存 / 错误转换都在那边）
    from backend.services.tg_user_profile import fetch_user_profile
    return await fetch_user_profile(
        db,
        sender_id=sender_id,
        username=username,
        use_cache=use_cache,
    )


async def tool_search_by_date(
    db: Session,
    start_date: str,
    end_date: str | None = None,
    chat_ids: list[str] | None = None,
    chat_id: str | None = None,
    senders: list[str] | None = None,
    keywords: list[str] | None = None,
    keyword: str | None = None,
    limit: int = 50,
) -> dict:
    """按日期范围查询消息。start_date / end_date 格式: YYYY-MM-DD。

    - chat_ids / senders / keywords 均为可选过滤
    - chat_id / keyword 是老参数别名
    """
    sd = _parse_date(start_date)
    if sd is None:
        return {"messages": [], "count": 0,
                "error": f"start_date 格式错误（应为 YYYY-MM-DD）: {start_date!r}"}
    ed = _parse_date(end_date) if end_date else datetime.now()
    if ed is None:
        return {"messages": [], "count": 0,
                "error": f"end_date 格式错误（应为 YYYY-MM-DD）: {end_date!r}"}
    ed = ed.replace(hour=23, minute=59, second=59)

    chat_ids_list = _coerce_str_list(chat_ids)
    if chat_id:
        chat_ids_list.append(chat_id)

    senders_list = _coerce_str_list(senders)
    keywords_list = _coerce_str_list(keywords)
    if keyword:
        keywords_list.append(keyword)
    keywords_list = [k.strip() for k in keywords_list if k and k.strip()]
    limit = max(1, min(int(limit), 200))

    def _q():
        q = db.query(Message).filter(Message.date >= sd, Message.date <= ed)
        if chat_ids_list:
            q = q.filter(Message.chat_id.in_(chat_ids_list))
        if senders_list:
            q = q.filter(or_(*[Message.sender.like(f"%{s}%") for s in senders_list]))
        if keywords_list:
            q = q.filter(or_(*[Message.text_plain.like(f"%{k}%") for k in keywords_list]))
        return q.order_by(Message.date).limit(limit).all()

    msgs = await asyncio.to_thread(_q)
    return {
        "messages": [_msg_to_dict(m, preview_len=300) for m in msgs],
        "count": len(msgs),
    }


async def tool_list_topics(
    db: Session,
    chat_ids: list[str] | None = None,
    chat_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    category: str | None = None,
    limit: int = 30,
) -> dict:
    """列出符合条件的话题，按 end_date 降序。

    - chat_ids: 限定群聊（可单可多）
    - start_date / end_date: 话题区间与查询区间相交（YYYY-MM-DD）
    - category: tech / business / resource / general（按数据库实际值）
    """
    chat_ids_list = _coerce_str_list(chat_ids)
    if chat_id:
        chat_ids_list.append(chat_id)
    sd = _parse_date(start_date)
    ed = _parse_date(end_date)
    if ed is not None:
        ed = ed.replace(hour=23, minute=59, second=59)
    limit = max(1, min(int(limit), 200))

    def _q():
        q = db.query(Topic)
        if chat_ids_list:
            q = q.filter(Topic.chat_id.in_(chat_ids_list))
        # 话题区间 [topic.start_date, topic.end_date] 与查询区间 [sd, ed] 相交
        if sd is not None:
            q = q.filter(Topic.end_date >= sd)
        if ed is not None:
            q = q.filter(Topic.start_date <= ed)
        if category:
            q = q.filter(Topic.category == category)
        q = q.order_by(Topic.end_date.desc()).limit(limit)
        return q.all()

    topics = await asyncio.to_thread(_q)
    return {
        "topics": [
            {
                "topic_id": t.id,
                "chat_id": t.chat_id,
                "start_date": t.start_date.strftime("%Y-%m-%d %H:%M") if t.start_date else None,
                "end_date": t.end_date.strftime("%Y-%m-%d %H:%M") if t.end_date else None,
                "participant_count": t.participant_count,
                "message_count": t.message_count,
                "category": t.category,
                "summary_preview": (t.summary or "")[:200],
            }
            for t in topics
        ],
        "count": len(topics),
    }


# ---------------------- Artifact tool handlers ----------------------

# 这三个工具与检索类工具不同：它们需要 session_id 上下文，必须由 dispatch_tool
# 通过 context 参数注入，因此 handler 签名带 session_id 而非纯 args。
# 返回结果含 `_artifact_event` 私有字段（下划线开头），qa_agent 会把它转成 SSE
# 事件给前端，自身不喂回 LLM 的工具消息。

def _artifact_op_event(kind: str, art, ver) -> dict:
    """构造 artifact_event 的 payload（kind = created / updated / rewritten）。"""
    return {
        "kind": kind,
        "artifact_key": art.artifact_key,
        "title": art.title,
        "version": ver.version,
        "current_version": art.current_version,
        "content_type": art.content_type or "text/markdown",
        "session_id": art.session_id,
        "op_meta": artifact_service._parse_op_meta(ver.op_meta),
    }


async def tool_create_artifact(
    db: Session,
    *,
    session_id: str,
    artifact_key: str,
    title: str,
    content: str,
) -> dict:
    """创建一篇 markdown artifact（session 内可有多篇，artifact_key 必须唯一）。"""
    if not session_id:
        return {"error": "create_artifact 需要在会话上下文中调用"}

    def _do():
        return artifact_service.create_artifact(
            db,
            session_id=session_id,
            artifact_key=artifact_key,
            title=title,
            content=content,
        )

    try:
        art, ver = await asyncio.to_thread(_do)
    except ArtifactKeyConflict as e:
        return {
            "error": str(e),
            "code": "key_conflict",
            "suggestion": "换一个 artifact_key（同 session 内不可重复），"
                          "或者用 update_artifact / rewrite_artifact 修改已存在的同名 artifact。",
        }
    except ArtifactError as e:
        return {"error": str(e), "code": "invalid"}

    return {
        "ok": True,
        "artifact_key": art.artifact_key,
        "title": art.title,
        "version": ver.version,
        "content_length": len(content),
        "_artifact_event": _artifact_op_event("created", art, ver),
    }


async def tool_update_artifact(
    db: Session,
    *,
    session_id: str,
    artifact_key: str,
    old_str: str,
    new_str: str,
) -> dict:
    """str_replace 风格增量编辑：old_str 必须在当前正文中恰好出现一次。"""
    if not session_id:
        return {"error": "update_artifact 需要在会话上下文中调用"}

    def _do():
        return artifact_service.update_artifact(
            db,
            session_id=session_id,
            artifact_key=artifact_key,
            old_str=old_str,
            new_str=new_str,
        )

    try:
        art, ver = await asyncio.to_thread(_do)
    except ArtifactNotFound as e:
        return {
            "error": str(e),
            "code": "not_found",
            "suggestion": "请先 create_artifact 建立这篇文档，或者用 list 工具检查 key 拼写。",
        }
    except StrReplaceError as e:
        out = {
            "error": str(e),
            "code": "no_unique_match",
            "match_count": e.match_count,
            "old_str_preview": e.old_str_preview,
            "suggestion": (
                "old_str 在文档中匹配 0 次：检查拼写、缩进、换行；可以用 fetch 查看当前正文后再试。"
                if e.match_count == 0 else
                f"old_str 在文档中出现了 {e.match_count} 次（必须恰好 1 次）：扩大 old_str 范围"
                "（多带几行上下文）让它唯一，或改用 rewrite_artifact 整体重写。"
            ),
        }
        if e.nearby_snippets:
            out["nearby_snippets"] = e.nearby_snippets
        return out
    except ArtifactError as e:
        return {"error": str(e), "code": "invalid"}

    return {
        "ok": True,
        "artifact_key": art.artifact_key,
        "title": art.title,
        "version": ver.version,
        "current_version": art.current_version,
        "_artifact_event": _artifact_op_event("updated", art, ver),
    }


async def tool_rewrite_artifact(
    db: Session,
    *,
    session_id: str,
    artifact_key: str,
    content: str,
    title: str | None = None,
) -> dict:
    """整体重写 artifact（生成新版本）。可选 title 同步改标题。"""
    if not session_id:
        return {"error": "rewrite_artifact 需要在会话上下文中调用"}

    def _do():
        return artifact_service.rewrite_artifact(
            db,
            session_id=session_id,
            artifact_key=artifact_key,
            content=content,
            title=title,
        )

    try:
        art, ver = await asyncio.to_thread(_do)
    except ArtifactNotFound as e:
        return {
            "error": str(e),
            "code": "not_found",
            "suggestion": "请先 create_artifact 建立这篇文档。",
        }
    except ArtifactError as e:
        return {"error": str(e), "code": "invalid"}

    return {
        "ok": True,
        "artifact_key": art.artifact_key,
        "title": art.title,
        "version": ver.version,
        "current_version": art.current_version,
        "content_length": len(content),
        "_artifact_event": _artifact_op_event("rewritten", art, ver),
    }


async def tool_list_artifacts(
    db: Session,
    *,
    session_id: str,
) -> dict:
    """列出当前 session 已有的所有 artifacts（仅元数据 + 正文预览，不返回完整内容）。

    用途：
    - Agent 想知道之前创建过哪些 artifact、避免重复建同主题
    - update / rewrite 之前先 list 确认 key 拼写
    """
    if not session_id:
        return {"error": "list_artifacts 需要在会话上下文中调用"}

    def _do():
        arts = artifact_service.list_artifacts(db, session_id)
        result = []
        for art in arts:
            ver = artifact_service.get_version(db, art.id)
            preview = ""
            content_length = 0
            if ver and ver.content:
                content_length = len(ver.content)
                preview = ver.content[:200] + ("..." if content_length > 200 else "")
            result.append({
                "artifact_key": art.artifact_key,
                "title": art.title,
                "current_version": art.current_version,
                "content_length": content_length,
                "preview": preview,
                "updated_at": art.updated_at.isoformat() if art.updated_at else None,
            })
        return result

    items = await asyncio.to_thread(_do)
    return {
        "ok": True,
        "count": len(items),
        "artifacts": items,
    }


async def tool_read_artifact(
    db: Session,
    *,
    session_id: str,
    artifact_key: str,
    version: int | None = None,
) -> dict:
    """读取指定 artifact 的完整内容（指定 version=None 则取最新）。

    用途：
    - 在 update_artifact 之前查看现有正文（确定 old_str 锚点）
    - 在用户问"之前的 artifact 写了什么"时复盘
    - 在新 research 之前看历史结论避免重复劳动
    """
    if not session_id:
        return {"error": "read_artifact 需要在会话上下文中调用"}

    def _do():
        art = artifact_service.get_artifact(db, session_id, artifact_key)
        if art is None:
            return None, None
        ver = artifact_service.get_version(db, art.id, version)
        return art, ver

    art, ver = await asyncio.to_thread(_do)
    if art is None:
        return {
            "error": f"artifact '{artifact_key}' 不存在",
            "code": "not_found",
            "suggestion": "用 list_artifacts 看看现有 key；或如果是新主题，用 create_artifact 新建",
        }
    if ver is None:
        return {
            "error": f"artifact '{artifact_key}' 没有 version={version}",
            "code": "version_not_found",
        }
    return {
        "ok": True,
        "artifact_key": art.artifact_key,
        "title": art.title,
        "version": ver.version,
        "current_version": art.current_version,
        "content_length": len(ver.content),
        "content": ver.content,
        "updated_at": art.updated_at.isoformat() if art.updated_at else None,
    }


# ---------------------- Tool schemas（OpenAI function calling） ----------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_chats",
            "description": "列出所有已导入、可供查询的 Telegram 群聊及其元信息（消息数、日期范围、是否已建索引）。"
                           "在用户问题不明确指定群聊时，可先用此工具了解可用数据。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "semantic_search",
            "description": "**首选检索工具**。用向量相似度语义搜索相关消息片段。适合自然语言查询、找概念/主题相关内容。"
                           "返回的每个结果是一个消息片段（可能含多条消息），带 message_ids、topic_id、participants。"
                           "**针对'调研型'问题（统计/对比/全面梳理）建议设较大 limit=50~150**，"
                           "精确事实问题 limit=10~20 即可。"
                           "支持多维交叉过滤（chat_ids / topic_ids / 日期 / senders）以精确缩小检索空间。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "自然语言查询，建议直接用用户原问题或其变体"},
                    "chat_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选：限定在这些群聊 ID 中搜索（chat_id，非 chat_name）",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 30,
                        "description": "返回片段数量。范围 1-200。简单问题 10-20，调研性问题 50-150",
                    },
                    "start_date": {"type": "string", "description": "YYYY-MM-DD，只返回 end_date ≥ 该日期的 chunk"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD，只返回 start_date ≤ 该日期的 chunk"},
                    "topic_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "可选：限定在这些话题 ID 内（用于同一话题的深挖）",
                    },
                    "senders": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选：只保留含这些发言人的 chunk（SQL post-filter，支持昵称模糊）",
                    },
                    "min_messages_in_chunk": {
                        "type": "integer",
                        "description": "可选：只保留消息数 ≥ 该值的 chunk（过滤嘈杂小片段）",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "keyword_search",
            "description": "关键词搜索（FTS5 trigram，0 命中时自动回退 LIKE，兼容短中文词）。"
                           "适合找具体的词/短语/代码/URL 等精确匹配内容，语义检索找不到时作为备选。"
                           "支持多关键词（keywords 列表自动 OR 拼接）、日期/发言人/群聊/话题多维过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "关键词列表（多个自动 OR），如 [\"H100\", \"A100\", \"B200\"]",
                    },
                    "keyword": {"type": "string", "description": "单关键词或 FTS5 表达式（兼容老参数名）"},
                    "chat_ids": {"type": "array", "items": {"type": "string"}},
                    "topic_ids": {"type": "array", "items": {"type": "integer"}},
                    "senders": {"type": "array", "items": {"type": "string"}, "description": "发言人列表（模糊匹配）"},
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "limit": {
                        "type": "integer",
                        "default": 30,
                        "description": "返回数量。范围 1-200。调研性问题建议 50-100",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_messages",
            "description": "按 message_id 列表获取消息的完整文本内容（用于查看检索到的消息细节）。"
                           "可选 context_window 顺带拉每条消息前后 N 条同 chat 上下文，便于还原语境。",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "消息 ID 列表（默认最多 50 个，可用 limit 扩展到 200）",
                    },
                    "full_text": {
                        "type": "boolean",
                        "default": False,
                        "description": "是否返回完整原文（true）还是 500 字预览（false）",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回上限（默认 50 / 最大 200）",
                    },
                    "context_window": {
                        "type": "integer",
                        "default": 0,
                        "description": "每条消息在同 chat 时序上的前后 N 条上下文（0 = 不拉，最大 20）",
                    },
                },
                "required": ["message_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_topic_context",
            "description": "获取整个话题的完整消息上下文（按时间排序）。"
                           "当某个 semantic_search 结果看起来很相关、想完整阅读时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic_id": {"type": "integer"},
                    "limit": {
                        "type": "integer",
                        "default": 30,
                        "description": "返回消息数上限（默认 30 / 最大 200）",
                    },
                },
                "required": ["topic_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_by_sender",
            "description": "查询某些发言人的消息。支持多发言人（OR）、多关键词（OR）、群聊/话题/日期过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "senders": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "发言人列表（模糊匹配）。多人之间 OR 关系",
                    },
                    "sender": {"type": "string", "description": "单个发言人（兼容老参数名）"},
                    "chat_ids": {"type": "array", "items": {"type": "string"}},
                    "topic_ids": {"type": "array", "items": {"type": "integer"}},
                    "keywords": {"type": "array", "items": {"type": "string"}, "description": "关键词列表（OR）"},
                    "keyword": {"type": "string", "description": "单关键词（兼容老参数名）"},
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "limit": {"type": "integer", "default": 50, "description": "范围 1-200"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_profile",
            "description": (
                "按需调 Telegram API 拉某用户的实时主页（display name / username / bio / 共同群数）。\n"
                "**何时用**：\n"
                "- 你在某条消息里看到一个 sender_id（如 'user6747261966'），想知道这人到底是谁、bio 写了什么\n"
                "- 用户给了一个 @username，想看他的主页\n"
                "- 在调研'卖家是否靠谱'、'这个频道作者背景'时，bio 经常有联系方式 / 业务标签\n"
                "**何时不用**：\n"
                "- 你只是想搜历史消息（用 keyword_search / search_by_sender）\n"
                "- 你已经在 24h 内查过同一个用户（agent 自己应该记得，不必重查；非要重查可设 use_cache=false）\n"
                "**注意**：\n"
                "- 此工具会消耗 Telegram API quota，**不要批量调用**（限流：1 req/sec）\n"
                "- 频道（channel...）和群组（chat...）不支持，仅限真实用户（user...）\n"
                "- 未登录 Telegram 时返回 code='no_login'\n"
                "- 隐私设置严格的用户可能拿不到 bio（返回 fallback 含基础信息）"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sender_id": {
                        "type": "string",
                        "description": "本地消息表里的 sender_id，如 'user6747261966'。优先使用这个",
                    },
                    "username": {
                        "type": "string",
                        "description": "Telegram username，如 'cwoiuhwooiv' 或 '@cwoiuhwooiv'",
                    },
                    "use_cache": {
                        "type": "boolean",
                        "default": True,
                        "description": "是否优先用 24h 内的本地缓存。设 false 强制重新调 API",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_by_date",
            "description": "按日期范围查询消息（按时间顺序）。适合'某月发生了什么'这种时间型问题。"
                           "可选 chat_ids / senders / keywords 做进一步过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD，缺省为今天"},
                    "chat_ids": {"type": "array", "items": {"type": "string"}},
                    "chat_id": {"type": "string", "description": "单群聊（兼容老参数名）"},
                    "senders": {"type": "array", "items": {"type": "string"}},
                    "keywords": {"type": "array", "items": {"type": "string"}, "description": "关键词列表（OR）"},
                    "keyword": {"type": "string", "description": "单关键词（兼容老参数名）"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["start_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_topics",
            "description": "列出符合条件的话题（按 end_date 降序）。"
                           "用于获取话题的整体概览、定位相关话题 id，然后交给 semantic_search(topic_ids=...) "
                           "或 fetch_topic_context 深挖。比 semantic_search 更适合'某群最近讨论了哪些话题'之类的问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_ids": {"type": "array", "items": {"type": "string"}},
                    "chat_id": {"type": "string", "description": "单群聊（兼容老参数名）"},
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "category": {
                        "type": "string",
                        "description": "可选分类过滤：tech / business / resource / general",
                    },
                    "limit": {"type": "integer", "default": 30, "description": "范围 1-200"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "research",
            "description": (
                "委派一个独立的检索子任务给子 Agent。子 Agent 拥有独立上下文窗口，"
                "会自主执行多轮搜索和分析，返回详细报告。\n"
                "**何时用**：用户问题规模大（需跨多个维度/时间段/群聊汇总）、单轮 "
                "semantic_search 一次拉不完、或主 Agent 不想被检索细节占满上下文时。\n"
                "⚠️ **不要给单个 research 过重的任务**：子 Agent 上下文越长，费用阶梯式上涨（128K 后翻倍、"
                "256K 后再翻倍）、质量衰减。**完整覆盖广话题靠'横向多拆 research + 多轮迭代'**，"
                "而不是一个 research 说'找出所有 X'让子 Agent 穷举。\n"
                "**写好 task 的关键**（子 Agent 用较弱模型，必须把指令写清楚）：\n"
                "  1) 具体要搜什么：主题 + 3~5 个关键词（不要超过 5）\n"
                "  2) 分析维度：时间/发言人/平台/观点对比\n"
                "  3) 期望输出结构：列表 / 按人汇总 / 时间线 / '平台名+链接+用途'三元组\n"
                "  4) 范围边界：'在这个范围内尽量完整，越界新线索写在 ## 越界线索 区块'\n"
                "  5) 排除项：如'不需要闲聊'\n"
                "**多轮迭代**：第一轮收到报告后评估完整性——稀薄/缺引用/缺维度/有越界线索/有矛盾 → 发后续轮次 "
                "research 验证、补维度、拓展。直到信息完整再写最终答案。\n"
                "**并行策略**：独立子任务同一轮多发几个 research 调用，子 Agent 并发跑。\n"
                "**filters 建议**：把主 Agent 已经清楚的约束（时间段/群聊/发言人）直接传给子 "
                "Agent，子 Agent 会把这些注入每次工具调用，避免无效搜索。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "详细的检索任务描述，建议 3 段以上：1) 搜什么 2) 分析维度 3) 期望输出结构",
                    },
                    "scope": {
                        "type": "string",
                        "description": "可选：对检索范围的自然语言说明（例：'最近三个月内'、'只看 A 群'），"
                                       "和 filters 互补——scope 给语境，filters 给机器可执行的约束",
                    },
                    "filters": {
                        "type": "object",
                        "description": "结构化约束，子 Agent 会在每次工具调用里注入这些字段",
                        "properties": {
                            "chat_ids": {"type": "array", "items": {"type": "string"}},
                            "topic_ids": {"type": "array", "items": {"type": "integer"}},
                            "senders": {"type": "array", "items": {"type": "string"}},
                            "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                            "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                        },
                    },
                    "expected_output": {
                        "type": "string",
                        "description": "希望子 Agent 报告包含的字段/结构。例："
                                       "'按月份 timeline + 每条 bullet 标 [msg:123]'，"
                                       "'按发言人分组汇总，每人列核心观点 2-3 条'",
                    },
                    "max_steps": {
                        "type": "integer",
                        "description": "可选：子 Agent 最大工具调用轮数（默认按任务难度自适应 8~16，硬上限 20）。"
                                       "一般不用显式给——任务范围窄了步数自然就够；很复杂的验证任务才显式给 16+"
                    },
                    "chat_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "老参数名，等价于 filters.chat_ids（保留兼容）",
                    },
                },
                "required": ["task"],
            },
        },
    },
    # ---------- Artifact 工具（产出可迭代的侧边文档）----------
    {
        "type": "function",
        "function": {
            "name": "create_artifact",
            "description": (
                "创建一篇 markdown artifact（侧边活文档）。\n"
                "**何时使用**：用户请求需要产出 30 行以上结构化长文（梳理 / 汇总 / 列表 / 报告）时主动建。\n"
                "短回答、单条事实、解释类回答**不要**用 artifact。\n"
                "**多篇规则**：一个 session 内可有多篇 artifact——不同独立主题应分开建。\n"
                "**artifact_key**：英文小写 slug（字母/数字/下划线/短横线，1~64 字符），同 session 内 unique。\n"
                "建好之后在最终答复里**简短**点出『已生成 artifact 《标题》』即可，不必复述全文。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "artifact_key": {
                        "type": "string",
                        "description": "英文小写 slug，如 'tech-summary' / 'gpu-pricing' / 'decisions-2025q4'。"
                                       "session 内不可重复",
                    },
                    "title": {
                        "type": "string",
                        "description": "中英文标题，例如 '技术讨论汇总'",
                    },
                    "content": {
                        "type": "string",
                        "description": "完整的 markdown 正文",
                    },
                },
                "required": ["artifact_key", "title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_artifact",
            "description": (
                "对已有 artifact 做**小改动**：用 str_replace 精准替换一段文本。\n"
                "**关键约束**：old_str 必须在当前正文中**恰好出现一次**，否则失败并告诉你命中数。\n"
                "适合：插入新章节（old_str 取上一节末尾几行作为锚点）、改正某条信息、修订标题。\n"
                "如要大幅重构，使用 rewrite_artifact。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "artifact_key": {
                        "type": "string",
                        "description": "之前 create_artifact 用过的 key",
                    },
                    "old_str": {
                        "type": "string",
                        "description": "在当前正文中要被替换的文本。必须**恰好命中一次**——"
                                       "若不唯一，请扩大 old_str 包含上下文使其唯一",
                    },
                    "new_str": {
                        "type": "string",
                        "description": "用来替换 old_str 的新文本（可以为空字符串实现纯删除）",
                    },
                },
                "required": ["artifact_key", "old_str", "new_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rewrite_artifact",
            "description": (
                "整体重写 artifact 正文，生成新版本。\n"
                "**何时用**：要换章节结构 / 大段重构 / update_artifact 多次失败。\n"
                "代价是 token 消耗大；优先考虑 update_artifact。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "artifact_key": {
                        "type": "string",
                        "description": "之前 create_artifact 用过的 key",
                    },
                    "content": {
                        "type": "string",
                        "description": "新的完整 markdown 正文（替换全部旧内容）",
                    },
                    "title": {
                        "type": "string",
                        "description": "可选：同时改标题。不传则保留原标题",
                    },
                },
                "required": ["artifact_key", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_artifacts",
            "description": (
                "列出当前 session 已有的所有 artifacts（仅元数据 + 正文预览 200 字符，不返回完整内容）。\n"
                "**何时用**：\n"
                "- session 启动时已经在 user message 注入了 artifacts 摘要——一般不需要再调用本工具\n"
                "- 但如果你刚 create/update 了一篇、想确认状态、或忘了之前的 key 拼写时调用\n"
                "返回字段：artifact_key / title / current_version / content_length / preview / updated_at"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_artifact",
            "description": (
                "读取指定 artifact 的完整正文。\n"
                "**何时用**：\n"
                "- update_artifact 之前需要查看现有正文（确定 old_str 锚点的精确文本）\n"
                "- 用户问'之前的报告里 X 那部分是怎么写的'\n"
                "- 想基于已有 artifact 拓展（不重复劳动）\n"
                "**注意**：完整正文可能很长（5K~30K tokens），不需要时不要随便调；优先用 list_artifacts 的 preview"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "artifact_key": {
                        "type": "string",
                        "description": "要读取的 artifact 的 key",
                    },
                    "version": {
                        "type": "integer",
                        "description": "可选：历史版本号（≥1）。不传则取最新版本",
                    },
                },
                "required": ["artifact_key"],
            },
        },
    },
]


# ---------------------- Dispatcher ----------------------

TOOL_HANDLERS = {
    "list_chats": tool_list_chats,
    "semantic_search": tool_semantic_search,
    "keyword_search": tool_keyword_search,
    "fetch_messages": tool_fetch_messages,
    "fetch_topic_context": tool_fetch_topic_context,
    "search_by_sender": tool_search_by_sender,
    "search_by_date": tool_search_by_date,
    "list_topics": tool_list_topics,
    "get_user_profile": tool_get_user_profile,
    # research 工具在 dispatch_tool 中特殊处理（调用 sub_agent）
}

# Artifact handlers 需要从 context 注入 session_id；与普通检索类 handler 分开映射
# 避免误用（普通工具不接 session_id 参数）。
ARTIFACT_TOOL_HANDLERS = {
    "create_artifact": tool_create_artifact,
    "update_artifact": tool_update_artifact,
    "rewrite_artifact": tool_rewrite_artifact,
    "list_artifacts": tool_list_artifacts,
    "read_artifact": tool_read_artifact,
}


def _normalize_tool_args(name: str, args: dict) -> dict:
    """老参数名 → 新参数名兼容映射（只做单向 rename，不删信息）。

    - semantic_search: top_k → limit
    - fetch_topic_context: max_messages → limit

    其他工具（keyword_search / search_by_sender / search_by_date / list_topics /
    fetch_messages）的 handler 签名已直接兼容老参数名（keyword|keywords、
    sender|senders、chat_id|chat_ids 等），无需在此处理。
    """
    if not args:
        return args
    args = dict(args)
    if name == "semantic_search" and "top_k" in args and "limit" not in args:
        args["limit"] = args.pop("top_k")
    if name == "fetch_topic_context" and "max_messages" in args and "limit" not in args:
        args["limit"] = args.pop("max_messages")
    return args


async def dispatch_tool(
    db: Session,
    name: str,
    args: dict,
    event_callback=None,
    context: dict | None = None,
) -> dict:
    """根据名字 dispatch 到对应 handler，统一错误处理。

    Args:
        context: 调用方上下文，目前只用 ``session_id``（artifact 类工具必需）。
    """
    args = _normalize_tool_args(name, args or {})

    # research 工具特殊处理：调用子 Agent
    if name == "research":
        from backend.services.sub_agent import run_sub_agent
        # filters 字典里的 chat_ids 优先于顶层 chat_ids（两处都给时以 filters 为准）
        filters = args.get("filters") or {}
        effective_chat_ids = filters.get("chat_ids") or args.get("chat_ids")
        try:
            return await run_sub_agent(
                db=db,
                task=args.get("task", ""),
                chat_ids=effective_chat_ids,
                scope=args.get("scope"),
                filters=filters if filters else None,
                expected_output=args.get("expected_output"),
                max_steps=args.get("max_steps"),
                event_callback=event_callback,
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {
                "error": f"子 Agent 执行失败: {e}",
                "suggestion": "可尝试简化 task 描述，或把 task 拆成更小的 research；也可直接用 "
                              "semantic_search / keyword_search 自己查",
            }

    # Artifact 工具：需要 session_id 上下文
    artifact_handler = ARTIFACT_TOOL_HANDLERS.get(name)
    if artifact_handler is not None:
        session_id = (context or {}).get("session_id")
        if not session_id:
            return {
                "error": "Artifact 工具必须在会话上下文中调用（缺失 session_id）",
                "code": "no_session",
                "suggestion": "artifact 只能在聊天会话中使用，不能在独立脚本里调用",
            }
        try:
            return await artifact_handler(db, session_id=session_id, **args)
        except TypeError as e:
            return {
                "error": f"参数错误: {e}",
                "code": "bad_args",
                "suggestion": "检查参数名拼写和类型。create_artifact 需要 (artifact_key, title, content)；"
                              "update_artifact 需要 (artifact_key, old_str, new_str)",
            }
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {
                "error": f"Artifact 工具执行失败: {e}",
                "suggestion": "检查 artifact_key 是否存在（update/rewrite 需要 create 过）；"
                              "update 时 old_str 必须在当前正文恰好出现一次",
            }

    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return {
            "error": f"未知工具: {name}",
            "suggestion": f"可用工具：{sorted(list(TOOL_HANDLERS.keys()) + ['research'])}",
        }
    try:
        return await handler(db, **args)
    except TypeError as e:
        # 构造针对性 suggestion：列出该工具的合法参数名
        schema = next((s for s in TOOL_SCHEMAS if s["function"]["name"] == name), None)
        valid_params: list[str] = []
        if schema:
            valid_params = list(schema["function"]["parameters"].get("properties", {}).keys())
        return {
            "error": f"参数错误: {e}",
            "suggestion": f"{name} 合法参数：{valid_params}。注意 chat_ids/senders/keywords 等是 list 类型，"
                          "date 用 YYYY-MM-DD",
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {
            "error": f"工具执行失败: {e}",
            "suggestion": "可尝试缩小查询范围（加日期/群聊过滤），或换一个工具（semantic_search ↔ keyword_search）",
        }
