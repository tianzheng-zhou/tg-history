import asyncio
import json
import logging
import tempfile
import threading
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from backend.models.database import Import, Message, SummaryReport, Topic, get_db, SessionLocal
from backend.models.schemas import ChatInfo, ChatStats, ImportResult, MessageItem, MessageQuery
from backend.services.parser import parse_export_file
from backend.services.embedding import build_index_for_chat
from backend.services.main_loop import schedule_on_main_loop
from backend.services.topic_builder import build_topics

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["import"])


def _import_single_chat(parsed: dict, db: Session) -> ImportResult:
    """导入单个群聊数据到数据库（同步，不含向量索引）"""
    chat_id = parsed["chat_id"]
    chat_name = parsed["chat_name"]
    messages = parsed["messages"]

    # 检查是否已导入（支持增量）
    existing = db.query(Import).filter(Import.chat_id == chat_id).first()
    existing_ids = set()
    if existing:
        existing_ids = set(
            row[0]
            for row in db.query(Message.id).filter(Message.chat_id == chat_id).all()
        )

    # 为避免跨群聊 message id 冲突，使用 chat_id 哈希偏移
    id_offset = abs(hash(chat_id)) % (10**9) * 1000000

    # 批量插入新消息
    new_count = 0
    batch = []
    for m in messages:
        unique_id = id_offset + m["id"]
        if m["id"] in existing_ids or unique_id in existing_ids:
            continue
        msg = Message(
            id=unique_id,
            chat_id=m["chat_id"],
            date=m["date"],
            sender=m["sender"],
            sender_id=m["sender_id"],
            text=m["text"],
            text_plain=m["text_plain"],
            reply_to_id=(id_offset + m["reply_to_id"]) if m["reply_to_id"] else None,
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
    db.execute(text("DELETE FROM messages_fts WHERE chat_id = :cid"), {"cid": chat_id})
    db.execute(text(
        "INSERT INTO messages_fts(text_plain, sender, chat_id, msg_id) "
        "SELECT text_plain, sender, chat_id, id FROM messages WHERE chat_id = :cid AND text_plain IS NOT NULL AND text_plain != ''"
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

    # 增量导入时，标记该群聊的旧摘要为过期 + 索引已过期
    if new_count > 0 and existing:
        db.query(SummaryReport).filter(
            SummaryReport.chat_id == chat_id,
            SummaryReport.stale == False,
        ).update({"stale": True})
        existing.index_built = False

    db.commit()

    return ImportResult(
        chat_id=chat_id,
        chat_name=chat_name,
        message_count=new_count,
        date_range=parsed["date_range"],
    )


_index_lock = threading.Lock()
_index_queue: list[str] = []
_index_progress: dict = {
    "running": False,
    "total": 0,
    "completed": 0,
    "current_chat": "",
    "active_chats": [],
    "chat_details": {},       # chat_name → {stage, topic_done, topic_total, index_done, index_total}
    "results": [],
}

MAX_PARALLEL = 16  # 群聊级并发上限（实际 LLM/embed 并发由 llm_adapter 全局 semaphore 控制）


def _enqueue_index(chat_ids: list[str]):
    with _index_lock:
        existing = set(_index_queue)
        for cid in chat_ids:
            if cid not in existing:
                _index_queue.append(cid)
                existing.add(cid)

        if _index_progress["running"]:
            _index_progress["total"] = _index_progress["completed"] + len(_index_queue)
            return

    # 调度到 FastAPI 主循环上跑（不要另起线程 + asyncio.run，
    # 否则会和 llm_adapter 模块级 Semaphore / httpx 客户端绑定的循环冲突）
    schedule_on_main_loop(_index_runner())


async def _index_runner():
    with _index_lock:
        _index_progress["running"] = True
        _index_progress["completed"] = 0
        _index_progress["total"] = len(_index_queue)
        _index_progress["current_chat"] = ""
        _index_progress["active_chats"] = []
        _index_progress["chat_details"] = {}
        _index_progress["results"] = []

    async def _process_one(chat_id: str, sem: asyncio.Semaphore):
        async with sem:
            db = SessionLocal()
            try:
                imp = db.query(Import).filter(Import.chat_id == chat_id).first()
                chat_name = imp.chat_name if imp else chat_id

                detail = {"stage": "topics", "topic_done": 0, "topic_total": 0,
                          "index_done": 0, "index_total": 0}
                with _index_lock:
                    _index_progress["active_chats"].append(chat_name)
                    _index_progress["chat_details"][chat_name] = detail
                    _index_progress["current_chat"] = chat_name

                try:
                    logger.info(f"话题构建中: {chat_name}")
                    detail["stage"] = "topics"
                    await build_topics(db, chat_id, progress=detail)

                    logger.info(f"向量索引构建中: {chat_name}")
                    detail["stage"] = "indexing"
                    indexed = await build_index_for_chat(db, chat_id, progress=detail)

                    if imp:
                        imp.index_built = True
                        db.commit()
                    _index_progress["results"].append({
                        "chat_id": chat_id,
                        "chat_name": chat_name,
                        "status": "ok",
                        "topics": indexed,
                    })
                    logger.info(f"构建完成: {chat_name} ({indexed} chunks)")
                except Exception as e:
                    logger.warning(f"构建失败({chat_name}): {e}")
                    _index_progress["results"].append({
                        "chat_id": chat_id,
                        "chat_name": chat_name,
                        "status": "error",
                        "error": str(e)[:200],
                    })
                    db.rollback()
                    # 标记为未索引（重建失败时旧的索引状态需失效）
                    try:
                        imp_fail = db.query(Import).filter(Import.chat_id == chat_id).first()
                        if imp_fail:
                            imp_fail.index_built = False
                            db.commit()
                    except Exception:
                        db.rollback()
                finally:
                    with _index_lock:
                        _index_progress["completed"] += 1
                        if chat_name in _index_progress["active_chats"]:
                            _index_progress["active_chats"].remove(chat_name)
                        _index_progress["chat_details"].pop(chat_name, None)
                        _index_progress["current_chat"] = (
                            _index_progress["active_chats"][0]
                            if _index_progress["active_chats"] else ""
                        )
            finally:
                db.close()

    sem = asyncio.Semaphore(MAX_PARALLEL)
    with _index_lock:
        all_ids = list(_index_queue)
        _index_queue.clear()
        _index_progress["total"] = _index_progress["completed"] + len(all_ids)

    tasks = [asyncio.create_task(_process_one(cid, sem)) for cid in all_ids]
    if tasks:
        await asyncio.gather(*tasks)

    _index_progress["running"] = False
    _index_progress["current_chat"] = ""
    _index_progress["active_chats"] = []
    _index_progress["chat_details"] = {}


@router.post("/import", response_model=list[ImportResult])
async def import_chat(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """上传并导入 Telegram 导出的 JSON 文件（支持单群聊和全量导出）"""
    if not file.filename or not file.filename.endswith(".json"):
        raise HTTPException(400, "请上传 .json 文件")

    # 读取上传文件到临时目录
    content = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="wb") as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        chat_list = parse_export_file(tmp_path)
    except (json.JSONDecodeError, KeyError) as e:
        raise HTTPException(400, f"JSON 解析失败: {e}")
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if not chat_list:
        raise HTTPException(400, "文件中没有有效消息，请确认文件格式正确")

    results = []
    imported_chat_ids = []
    for parsed in chat_list:
        result = _import_single_chat(parsed, db)
        results.append(result)
        if result.message_count > 0:
            imported_chat_ids.append(result.chat_id)

    # 向量索引构建放后台
    if imported_chat_ids:
        _enqueue_index(imported_chat_ids)

    return results


@router.get("/index-progress")
def get_index_progress():
    """查询向量索引构建进度"""
    with _index_lock:
        return {**_index_progress, "queued": len(_index_queue)}


@router.post("/rebuild-index/{chat_id}")
def rebuild_single_index(chat_id: str, db: Session = Depends(get_db)):
    """重建单个群聊的向量索引"""
    imp = db.query(Import).filter(Import.chat_id == chat_id).first()
    if not imp:
        raise HTTPException(404, "群聊未找到")
    _enqueue_index([chat_id])
    return {"status": "started", "chat_name": imp.chat_name}


@router.post("/rebuild-index-all")
def rebuild_all_index(force: bool = False, db: Session = Depends(get_db)):
    """重建向量索引。默认只重建过期的，force=true 重建所有"""
    query = db.query(Import)
    if not force:
        query = query.filter(Import.index_built == False)
    imports = query.all()
    if not imports:
        raise HTTPException(400, "没有需要重建索引的群聊")
    chat_ids = [imp.chat_id for imp in imports]
    _enqueue_index(chat_ids)
    return {"status": "started", "total": len(chat_ids)}


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
            index_built=imp.index_built or False,
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
            text("SELECT msg_id FROM messages_fts WHERE messages_fts MATCH :kw LIMIT 500"),
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
