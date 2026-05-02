"""QA Agent 工具集。

每个工具由 (schema, handler) 组成：
- schema: OpenAI Function Calling 格式的工具定义，供 LLM 理解
- handler: async 执行函数，输入 kwargs，返回 JSON-serializable dict
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from backend.models.database import Import, Message, Topic
from backend.services.embedding import search_similar


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


# ---------------------- Tool handlers ----------------------

async def tool_list_chats(db: Session) -> dict:
    """列出所有已导入的群聊"""
    imports = db.query(Import).order_by(Import.message_count.desc()).all()
    return {
        "chats": [
            {
                "chat_id": i.chat_id,
                "chat_name": i.chat_name,
                "message_count": i.message_count,
                "date_range": i.date_range,
                "index_built": bool(i.index_built),
            }
            for i in imports
        ]
    }


async def tool_semantic_search(
    db: Session,
    query: str,
    chat_ids: list[str] | None = None,
    top_k: int = 30,
) -> dict:
    """向量语义检索（top_k 上限 200）"""
    top_k = max(1, min(int(top_k), 200))
    where_filter = None
    if chat_ids and len(chat_ids) == 1:
        where_filter = {"chat_id": chat_ids[0]}

    results = await search_similar(query, n_results=top_k, where=where_filter)

    # 把 chunk metadata 展开；保持 preview 紧凑以便容纳更多结果
    items = []
    for r in results:
        meta = r.get("metadata", {})
        msg_ids = meta.get("message_ids", [])
        if isinstance(msg_ids, str):
            try:
                msg_ids = json.loads(msg_ids)
            except Exception:
                msg_ids = []
        items.append({
            "chunk_preview": (r.get("document") or "")[:1000],
            "distance": round(r.get("distance") or 0, 4),
            "chat_id": meta.get("chat_id"),
            "topic_id": meta.get("topic_id"),
            "start_date": meta.get("start_date"),
            "end_date": meta.get("end_date"),
            "message_ids": msg_ids[:30],  # 单 chunk 最多 30 个 msg_id
            "total_messages_in_chunk": len(msg_ids),
        })
    return {"results": items, "count": len(items)}


async def tool_keyword_search(
    db: Session,
    keyword: str,
    chat_ids: list[str] | None = None,
    limit: int = 30,
) -> dict:
    """关键词检索：优先 FTS5（trigram），命中 0 时回退 LIKE 模糊匹配（兼容短中文词）"""
    limit = max(1, min(int(limit), 200))
    from sqlalchemy import text as sa_text

    msgs: list[Message] = []
    used_method = "fts5"
    try:
        rows = db.execute(
            sa_text("SELECT rowid FROM messages_fts WHERE messages_fts MATCH :kw LIMIT :lim"),
            {"kw": keyword, "lim": limit},
        ).fetchall()
        if rows:
            ids = [r[0] for r in rows]
            q = db.query(Message).filter(Message.id.in_(ids))
            if chat_ids:
                q = q.filter(Message.chat_id.in_(chat_ids))
            msgs = q.order_by(Message.date).all()
    except Exception as e:
        # FTS 解析失败（如非法 FTS5 表达式），跳过去走 LIKE
        used_method = f"fts5_err({type(e).__name__})"

    # FTS 0 命中 → 用 LIKE 兜底（适配 trigram 不支持的短中文词）
    if not msgs:
        used_method = "like"
        # 取出第一个 OR 操作数（FTS 表达式拆分），用最朴素的关键词做 LIKE
        primary_kw = keyword.split(" OR ")[0].split()[0].strip('"').strip()
        if primary_kw:
            q = db.query(Message).filter(Message.text_plain.like(f"%{primary_kw}%"))
            if chat_ids:
                q = q.filter(Message.chat_id.in_(chat_ids))
            msgs = q.order_by(Message.date.desc()).limit(limit).all()
            msgs.reverse()  # 改回时间正序

    return {
        "results": [_msg_to_dict(m, preview_len=250) for m in msgs],
        "count": len(msgs),
        "method": used_method,
    }


async def tool_fetch_messages(
    db: Session,
    message_ids: list[int],
    full_text: bool = False,
) -> dict:
    """按 message_id 列表获取完整消息内容"""
    if not message_ids:
        return {"messages": [], "count": 0}

    limit = min(len(message_ids), 50)
    msgs = (
        db.query(Message)
        .filter(Message.id.in_(message_ids[:limit]))
        .order_by(Message.date)
        .all()
    )
    preview_len = 2000 if full_text else 500
    return {
        "messages": [_msg_to_dict(m, preview_len=preview_len) for m in msgs],
        "count": len(msgs),
        "truncated": len(message_ids) > limit,
    }


async def tool_fetch_topic_context(
    db: Session,
    topic_id: int,
    max_messages: int = 30,
) -> dict:
    """获取某话题的完整消息列表（按时间排序）"""
    topic = db.query(Topic).filter(Topic.id == topic_id).first()
    if not topic:
        return {"error": f"话题 {topic_id} 不存在", "messages": []}

    msgs = (
        db.query(Message)
        .filter(Message.topic_id == topic_id)
        .order_by(Message.date)
        .limit(max_messages)
        .all()
    )
    return {
        "topic_id": topic_id,
        "chat_id": topic.chat_id,
        "start_date": topic.start_date.strftime("%Y-%m-%d %H:%M") if topic.start_date else None,
        "end_date": topic.end_date.strftime("%Y-%m-%d %H:%M") if topic.end_date else None,
        "participant_count": topic.participant_count,
        "message_count": topic.message_count,
        "messages": [_msg_to_dict(m, preview_len=400) for m in msgs],
    }


async def tool_search_by_sender(
    db: Session,
    sender: str,
    chat_ids: list[str] | None = None,
    keyword: str | None = None,
    limit: int = 20,
) -> dict:
    """查询某发言人的消息，可选关键词过滤"""
    q = db.query(Message).filter(Message.sender.like(f"%{sender}%"))
    if chat_ids:
        q = q.filter(Message.chat_id.in_(chat_ids))
    if keyword:
        q = q.filter(Message.text_plain.like(f"%{keyword}%"))
    msgs = q.order_by(Message.date.desc()).limit(limit).all()
    return {
        "results": [_msg_to_dict(m, preview_len=300) for m in msgs],
        "count": len(msgs),
    }


async def tool_search_by_date(
    db: Session,
    chat_id: str,
    start_date: str,
    end_date: str | None = None,
    limit: int = 30,
) -> dict:
    """按日期范围查询消息。start_date / end_date 格式: YYYY-MM-DD"""
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d") if end_date else datetime.now()
    except ValueError as e:
        return {"error": f"日期格式错误（应为 YYYY-MM-DD）: {e}", "messages": []}

    msgs = (
        db.query(Message)
        .filter(
            Message.chat_id == chat_id,
            Message.date >= start_dt,
            Message.date <= end_dt,
        )
        .order_by(Message.date)
        .limit(limit)
        .all()
    )
    return {
        "messages": [_msg_to_dict(m, preview_len=300) for m in msgs],
        "count": len(msgs),
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
                           "返回的每个结果是一个消息片段（可能含多条消息），带 message_ids 和 topic_id。"
                           "**针对'调研型'问题（统计/对比/全面梳理）建议设较大 top_k=50~150**，"
                           "针对'精确事实'问题用小 top_k=10~20 即可。"
                           "如需完整内容请用 fetch_messages / fetch_topic_context。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "自然语言查询，建议直接用用户原问题或其变体"},
                    "chat_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选：限定在这些群聊 ID 中搜索（注意是 chat_id 不是 chat_name）",
                    },
                    "top_k": {
                        "type": "integer",
                        "default": 30,
                        "description": "返回片段数量。范围 1-200。简单问题 10-20，调研性问题 50-150",
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
            "description": "关键词搜索（FTS5 trigram，0 命中时自动回退 LIKE 模糊匹配，兼容短中文词）。"
                           "适合找具体的词/短语/代码/URL 等精确匹配内容，语义检索找不到时作为备选。"
                           "支持 FTS5 操作符（AND/OR/NEAR），但回退 LIKE 时只用第一个关键词。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "关键词或 FTS5 表达式，如 \"GPU 租\" 或 \"H100 OR A100\""},
                    "chat_ids": {"type": "array", "items": {"type": "string"}},
                    "limit": {
                        "type": "integer",
                        "default": 30,
                        "description": "返回数量。范围 1-200。调研性问题建议 50-100",
                    },
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_messages",
            "description": "按 message_id 列表获取消息的完整文本内容（用于查看检索到的消息细节）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "消息 ID 列表（最多 50 个）",
                    },
                    "full_text": {
                        "type": "boolean",
                        "default": False,
                        "description": "是否返回完整原文（true）还是 500 字预览（false）",
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
                    "max_messages": {"type": "integer", "default": 30},
                },
                "required": ["topic_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_by_sender",
            "description": "查询某个用户的发言。可附加关键词过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sender": {"type": "string", "description": "发言人昵称（支持模糊匹配）"},
                    "chat_ids": {"type": "array", "items": {"type": "string"}},
                    "keyword": {"type": "string", "description": "可选的关键词过滤"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["sender"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_by_date",
            "description": "按日期范围查询某群聊的消息（按时间顺序）。适合'某月发生了什么'这种时间型问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_id": {"type": "string"},
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD，可选"},
                    "limit": {"type": "integer", "default": 30},
                },
                "required": ["chat_id", "start_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "research",
            "description": "委派一个独立的检索子任务给子 Agent。子 Agent 拥有独立上下文窗口，"
                           "会自主执行多轮搜索和分析，返回详细报告。"
                           "你应该将复杂问题拆分为多个子任务，并行发起多个 research 调用。"
                           "每个子任务描述要详细，包含：要搜什么、关注哪些方面、预期返回什么信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "详细的检索任务描述（要搜什么、关注哪些方面、预期返回什么）",
                    },
                    "chat_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选：限定搜索范围的群聊 ID 列表",
                    },
                },
                "required": ["task"],
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
    # research 工具在 dispatch_tool 中特殊处理（调用 sub_agent）
}


async def dispatch_tool(db: Session, name: str, args: dict, event_callback=None) -> dict:
    """根据名字 dispatch 到对应 handler，统一错误处理"""
    # research 工具特殊处理：调用子 Agent
    if name == "research":
        from backend.services.sub_agent import run_sub_agent
        try:
            return await run_sub_agent(
                db=db,
                task=args.get("task", ""),
                chat_ids=args.get("chat_ids"),
                event_callback=event_callback,
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"error": f"子 Agent 执行失败: {e}"}

    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return {"error": f"未知工具: {name}"}
    try:
        return await handler(db, **args)
    except TypeError as e:
        return {"error": f"参数错误: {e}"}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": f"工具执行失败: {e}"}
