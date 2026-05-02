import json
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from backend.models.database import Import, Message, Topic, get_db
from backend.models.schemas import ChatInfo, ChatStats, ImportResult, MessageItem, MessageQuery
from backend.services.parser import parse_export_file
from backend.services.topic_builder import build_topics

router = APIRouter(prefix="/api", tags=["import"])


@router.post("/import", response_model=ImportResult)
async def import_chat(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """上传并导入 Telegram 导出的 JSON 文件"""
    if not file.filename or not file.filename.endswith(".json"):
        raise HTTPException(400, "请上传 .json 文件")

    # 读取上传文件到临时目录
    content = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="wb") as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        parsed = parse_export_file(tmp_path)
    except (json.JSONDecodeError, KeyError) as e:
        raise HTTPException(400, f"JSON 解析失败: {e}")
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    chat_id = parsed["chat_id"]
    chat_name = parsed["chat_name"]
    messages = parsed["messages"]

    if not messages:
        raise HTTPException(400, "文件中没有有效消息")

    # 检查是否已导入（支持增量）
    existing = db.query(Import).filter(Import.chat_id == chat_id).first()
    existing_ids = set()
    if existing:
        existing_ids = set(
            row[0]
            for row in db.query(Message.id).filter(Message.chat_id == chat_id).all()
        )

    # 批量插入新消息
    new_count = 0
    batch = []
    for m in messages:
        if m["id"] in existing_ids:
            continue
        msg = Message(
            id=m["id"],
            chat_id=m["chat_id"],
            date=m["date"],
            sender=m["sender"],
            sender_id=m["sender_id"],
            text=m["text"],
            text_plain=m["text_plain"],
            reply_to_id=m["reply_to_id"],
            forwarded_from=m["forwarded_from"],
            media_type=m["media_type"],
        )
        if m.get("entities"):
            msg.set_entities(m["entities"])
        batch.append(msg)
        new_count += 1

        if len(batch) >= 500:
            db.bulk_save_objects(batch)
            batch = []

    if batch:
        db.bulk_save_objects(batch)

    # 同步 FTS 索引
    db.execute(text("DELETE FROM messages_fts WHERE rowid IN (SELECT id FROM messages WHERE chat_id = :cid)"), {"cid": chat_id})
    db.execute(text(
        "INSERT INTO messages_fts(rowid, text_plain, sender) "
        "SELECT id, text_plain, sender FROM messages WHERE chat_id = :cid AND text_plain IS NOT NULL AND text_plain != ''"
    ), {"cid": chat_id})

    # 更新/创建导入记录
    total_count = db.query(Message).filter(Message.chat_id == chat_id).count()
    if existing:
        existing.message_count = total_count
        existing.date_range = parsed["date_range"]
    else:
        db.add(Import(
            chat_name=chat_name,
            chat_id=chat_id,
            message_count=total_count,
            date_range=parsed["date_range"],
        ))

    db.commit()

    # 构建话题树
    build_topics(db, chat_id)

    return ImportResult(
        chat_id=chat_id,
        chat_name=chat_name,
        message_count=new_count,
        date_range=parsed["date_range"],
    )


@router.get("/chats", response_model=list[ChatInfo])
def list_chats(db: Session = Depends(get_db)):
    """获取已导入的群聊列表"""
    imports = db.query(Import).order_by(Import.imported_at.desc()).all()
    return [
        ChatInfo(
            id=imp.id,
            chat_name=imp.chat_name,
            chat_id=imp.chat_id,
            imported_at=imp.imported_at,
            message_count=imp.message_count,
            date_range=imp.date_range or "",
        )
        for imp in imports
    ]


@router.get("/chats/{chat_id}/stats", response_model=ChatStats)
def chat_stats(chat_id: str, db: Session = Depends(get_db)):
    """获取群聊统计信息"""
    imp = db.query(Import).filter(Import.chat_id == chat_id).first()
    if not imp:
        raise HTTPException(404, "群聊未找到")

    # 活跃发言人 Top 10
    top_senders = (
        db.query(Message.sender, func.count(Message.id).label("count"))
        .filter(Message.chat_id == chat_id, Message.sender.isnot(None))
        .group_by(Message.sender)
        .order_by(func.count(Message.id).desc())
        .limit(10)
        .all()
    )

    # 每日消息数
    messages_per_day = (
        db.query(
            func.date(Message.date).label("day"),
            func.count(Message.id).label("count"),
        )
        .filter(Message.chat_id == chat_id, Message.date.isnot(None))
        .group_by(func.date(Message.date))
        .order_by(func.date(Message.date))
        .all()
    )

    topic_count = db.query(Topic).filter(Topic.chat_id == chat_id).count()

    return ChatStats(
        chat_id=chat_id,
        chat_name=imp.chat_name,
        message_count=imp.message_count,
        date_range=imp.date_range or "",
        top_senders=[{"sender": s, "count": c} for s, c in top_senders],
        messages_per_day=[{"date": str(d), "count": c} for d, c in messages_per_day],
        topic_count=topic_count,
    )


@router.get("/messages", response_model=dict)
def search_messages(
    chat_id: str | None = None,
    sender: str | None = None,
    keyword: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
):
    """搜索/浏览消息，支持分页和过滤"""
    query = db.query(Message)

    if chat_id:
        query = query.filter(Message.chat_id == chat_id)
    if sender:
        query = query.filter(Message.sender == sender)
    if date_from:
        query = query.filter(Message.date >= date_from)
    if date_to:
        query = query.filter(Message.date <= date_to)

    # 关键词搜索使用 FTS
    if keyword:
        fts_ids = db.execute(
            text("SELECT rowid FROM messages_fts WHERE messages_fts MATCH :kw LIMIT 500"),
            {"kw": keyword},
        ).fetchall()
        ids = [row[0] for row in fts_ids]
        if ids:
            query = query.filter(Message.id.in_(ids))
        else:
            return {"total": 0, "page": page, "page_size": page_size, "messages": []}

    total = query.count()
    msgs = (
        query.order_by(Message.date.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "messages": [
            {
                "id": m.id,
                "chat_id": m.chat_id,
                "date": m.date.isoformat() if m.date else None,
                "sender": m.sender,
                "text_plain": m.text_plain,
                "reply_to_id": m.reply_to_id,
                "topic_id": m.topic_id,
                "media_type": m.media_type,
            }
            for m in msgs
        ],
    }
