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
    top_k: int = 30,
) -> dict:
    """向量语义检索（top_k 上限 200）"""
    top_k = max(1, min(int(top_k), 200))
    where_filter = None
    if chat_ids:
        if len(chat_ids) == 1:
            where_filter = {"chat_id": chat_ids[0]}
        else:
            where_filter = {"chat_id": {"$in": chat_ids}}

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
    """关键词检索：优先 FTS5（trigram），命中 0 时回退 LIKE 模糊匹配（兼容短中文词）。
    结果经 rerank 模型按相关性排序。"""
    limit = max(1, min(int(limit), 200))
    # 先多取一些候选，再 rerank 筛选
    fetch_limit = min(limit * 3, 200)
    from sqlalchemy import text as sa_text

    def _fts_then_like() -> tuple[list[Message], str]:
        msgs_local: list[Message] = []
        method = "fts5"
        try:
            rows = db.execute(
                sa_text("SELECT rowid FROM messages_fts WHERE messages_fts MATCH :kw LIMIT :lim"),
                {"kw": keyword, "lim": fetch_limit},
            ).fetchall()
            if rows:
                ids = [r[0] for r in rows]
                q = db.query(Message).filter(Message.id.in_(ids))
                if chat_ids:
                    q = q.filter(Message.chat_id.in_(chat_ids))
                msgs_local = q.order_by(Message.date).all()
        except Exception as e:
            method = f"fts5_err({type(e).__name__})"

        if not msgs_local:
            method = "like"
            primary_kw = keyword.split(" OR ")[0].split()[0].strip('"').strip()
            if primary_kw:
                q = db.query(Message).filter(Message.text_plain.like(f"%{primary_kw}%"))
                if chat_ids:
                    q = q.filter(Message.chat_id.in_(chat_ids))
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
                query=keyword, documents=docs, top_n=min(limit, len(msgs)),
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
            # rerank 失败，fallback 到原始顺序并截断
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
) -> dict:
    """按 message_id 列表获取完整消息内容"""
    if not message_ids:
        return {"messages": [], "count": 0}

    limit = min(len(message_ids), 50)

    def _q():
        return (
            db.query(Message)
            .filter(Message.id.in_(message_ids[:limit]))
            .order_by(Message.date)
            .all()
        )

    msgs = await asyncio.to_thread(_q)
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

    def _q():
        topic_local = db.query(Topic).filter(Topic.id == topic_id).first()
        if not topic_local:
            return None, []
        msgs_local = (
            db.query(Message)
            .filter(Message.topic_id == topic_id)
            .order_by(Message.date)
            .limit(max_messages)
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
        "messages": [_msg_to_dict(m, preview_len=400) for m in msgs],
    }


async def tool_search_by_sender(
    db: Session,
    sender: str,
    chat_ids: list[str] | None = None,
    keyword: str | None = None,
    limit: int = 50,
) -> dict:
    """查询某发言人的消息，可选关键词过滤"""

    def _q():
        q = db.query(Message).filter(Message.sender.like(f"%{sender}%"))
        if chat_ids:
            q = q.filter(Message.chat_id.in_(chat_ids))
        if keyword:
            q = q.filter(Message.text_plain.like(f"%{keyword}%"))
        return q.order_by(Message.date.desc()).limit(limit).all()

    msgs = await asyncio.to_thread(_q)
    return {
        "results": [_msg_to_dict(m, preview_len=300) for m in msgs],
        "count": len(msgs),
    }


async def tool_search_by_date(
    db: Session,
    chat_id: str,
    start_date: str,
    end_date: str | None = None,
    limit: int = 50,
) -> dict:
    """按日期范围查询消息。start_date / end_date 格式: YYYY-MM-DD"""
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d") if end_date else datetime.now()
    except ValueError as e:
        return {"error": f"日期格式错误（应为 YYYY-MM-DD）: {e}", "messages": []}

    def _q():
        return (
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

    msgs = await asyncio.to_thread(_q)
    return {
        "messages": [_msg_to_dict(m, preview_len=300) for m in msgs],
        "count": len(msgs),
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
                    "limit": {"type": "integer", "default": 50},
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
                    "limit": {"type": "integer", "default": 50},
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

# Artifact handlers 需要从 context 注入 session_id；与普通检索类 handler 分开映射
# 避免误用（普通工具不接 session_id 参数）。
ARTIFACT_TOOL_HANDLERS = {
    "create_artifact": tool_create_artifact,
    "update_artifact": tool_update_artifact,
    "rewrite_artifact": tool_rewrite_artifact,
}


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

    # Artifact 工具：需要 session_id 上下文
    artifact_handler = ARTIFACT_TOOL_HANDLERS.get(name)
    if artifact_handler is not None:
        session_id = (context or {}).get("session_id")
        if not session_id:
            return {
                "error": "Artifact 工具必须在会话上下文中调用（缺失 session_id）",
                "code": "no_session",
            }
        try:
            return await artifact_handler(db, session_id=session_id, **args)
        except TypeError as e:
            return {"error": f"参数错误: {e}", "code": "bad_args"}
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"error": f"Artifact 工具执行失败: {e}"}

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
