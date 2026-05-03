import asyncio
import hashlib
import json
import logging
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from backend.models.database import (
    Import,
    ImportedFile,
    Message,
    SessionLocal,
    SummaryReport,
    Topic,
    WatchedFolder,
    get_db,
)
from backend.models.schemas import (
    ChatInfo,
    ChatStats,
    FolderAddRequest,
    FolderValidateRequest,
    FolderValidateResponse,
    ImportResult,
    MessageItem,
    MessageQuery,
    ScanFileResult,
    ScanResult,
    WatchedFolderInfo,
)
from backend.services.parser import parse_export_file
from backend.services.embedding import (
    build_index_for_chat,
    build_index_for_chat_incremental,
)
from backend.services.folder_scanner import (
    diff_pending,
    find_result_jsons,
    resolve_path,
    upsert_imported_file,
    validate_folder,
)
from backend.services.main_loop import schedule_on_main_loop
from backend.services.topic_builder import build_topics, build_topics_incremental

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["import"])


def _stable_id_offset(chat_id: str) -> int:
    """生成稳定的 chat_id 哈希偏移（跨进程一致），用于消息全局唯一 ID。

    曾用 ``abs(hash(chat_id))``，但 Python 内置 hash() 对字符串以 PYTHONHASHSEED
    随进程启动加盐 → 重启后同一 chat_id 算出的偏移会变 → 导致 existing_ids 比对失效，
    所有旧消息会被当作新消息再次插入（出现 message_count 翻倍 + 索引整库重建）。
    改用 SHA-256 取前 8 字节，跨进程严格一致。
    """
    digest = hashlib.sha256(chat_id.encode("utf-8")).digest()
    return (int.from_bytes(digest[:8], "big") % (10**9)) * 1000000


def import_messages_for_chat(
    db: Session,
    *,
    chat_id: str,
    chat_name: str,
    messages: list[dict],
    date_range: str,
) -> ImportResult:
    """把一批已解析的消息写入数据库 + FTS（同步，不含向量索引）。

    messages 中每个元素须符合 parser.parse_message 的输出格式：
        id (int, 原始 chat 内 id), chat_id, date, sender, sender_id,
        text, text_plain, reply_to_id, forwarded_from, media_type, entities

    去重逻辑：
    - 用 chat_id 稳定哈希偏移把每条消息映射到全局唯一 id；
    - 对该 chat 已存在的 message id 取交集，跳过重复；
    - 同步 FTS5 索引；如果有新消息且已存在导入记录，标记摘要 stale + index_built=False。

    返回 ImportResult，message_count 表示**本次新增**条数（增量）。
    """
    # 检查是否已导入（支持增量）
    existing = db.query(Import).filter(Import.chat_id == chat_id).first()
    existing_ids = set()
    if existing:
        existing_ids = set(
            row[0]
            for row in db.query(Message.id).filter(Message.chat_id == chat_id).all()
        )

    # 为避免跨群聊 message id 冲突，使用 chat_id 稳定哈希偏移
    id_offset = _stable_id_offset(chat_id)

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
        existing.date_range = date_range
    else:
        db.add(Import(
            chat_name=chat_name,
            chat_id=chat_id,
            message_count=total_count,
            date_range=date_range,
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
        date_range=date_range,
    )


def _import_single_chat(parsed: dict, db: Session) -> ImportResult:
    """从 parser.parse_export_file 返回的单个 chat dict 写入数据库（兼容老调用）。"""
    return import_messages_for_chat(
        db,
        chat_id=parsed["chat_id"],
        chat_name=parsed["chat_name"],
        messages=parsed["messages"],
        date_range=parsed["date_range"],
    )


_index_lock = threading.Lock()
_index_queue: list[tuple[str, bool]] = []  # [(chat_id, force)]
_index_progress: dict = {
    "running": False,
    "total": 0,
    "completed": 0,
    "current_chat": "",
    "active_chats": [],
    "chat_details": {},       # chat_name → {stage, topic_done, topic_total, index_done, index_total, mode}
    "results": [],
}

MAX_PARALLEL = 16  # 群聊级并发上限（实际 LLM/embed 并发由 llm_adapter 全局 semaphore 控制）


def _enqueue_index(chat_ids: list[str], force: bool = False):
    """把 chat_ids 入队等待索引构建。

    - force=False（默认）：走增量路径（build_topics_incremental → 只处理 topic_id IS NULL 的新消息）
    - force=True：走全量路径（build_topics → 清空重建）

    同一 chat_id 若已在队列里：force=True 会覆盖原先的 force=False（就高不就低）。
    """
    with _index_lock:
        existing = {cid: i for i, (cid, _) in enumerate(_index_queue)}
        for cid in chat_ids:
            if cid in existing:
                if force:
                    # 覆盖为全量
                    idx = existing[cid]
                    _index_queue[idx] = (cid, True)
            else:
                _index_queue.append((cid, force))
                existing[cid] = len(_index_queue) - 1

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

    async def _process_one(chat_id: str, force: bool, sem: asyncio.Semaphore):
        async with sem:
            db = SessionLocal()
            try:
                imp = db.query(Import).filter(Import.chat_id == chat_id).first()
                chat_name = imp.chat_name if imp else chat_id

                mode = "force" if force else "incremental"
                detail = {"stage": "topics", "topic_done": 0, "topic_total": 0,
                          "index_done": 0, "index_total": 0, "mode": mode}
                with _index_lock:
                    _index_progress["active_chats"].append(chat_name)
                    _index_progress["chat_details"][chat_name] = detail
                    _index_progress["current_chat"] = chat_name

                try:
                    logger.info(f"话题构建中 [{mode}]: {chat_name}")
                    detail["stage"] = "topics"

                    if force:
                        await build_topics(db, chat_id, progress=detail)
                        logger.info(f"向量索引构建中 [全量]: {chat_name}")
                        detail["stage"] = "indexing"
                        indexed = await build_index_for_chat(
                            db, chat_id, progress=detail
                        )
                    else:
                        _total, changed_topic_ids = await build_topics_incremental(
                            db, chat_id, progress=detail
                        )
                        logger.info(
                            f"向量索引构建中 [增量 {len(changed_topic_ids)} topics]: {chat_name}"
                        )
                        detail["stage"] = "indexing"
                        indexed = await build_index_for_chat_incremental(
                            db, chat_id, changed_topic_ids, progress=detail
                        )

                    if imp:
                        imp.index_built = True
                        db.commit()
                    _index_progress["results"].append({
                        "chat_id": chat_id,
                        "chat_name": chat_name,
                        "status": "ok",
                        "topics": indexed,
                        "mode": mode,
                    })
                    logger.info(f"构建完成 [{mode}]: {chat_name} ({indexed} chunks)")
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
        all_items = list(_index_queue)  # [(chat_id, force), ...]
        _index_queue.clear()
        _index_progress["total"] = _index_progress["completed"] + len(all_items)

    tasks = [
        asyncio.create_task(_process_one(cid, force, sem))
        for cid, force in all_items
    ]
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
def rebuild_single_index(
    chat_id: str,
    force: bool = False,
    db: Session = Depends(get_db),
):
    """重建单个群聊的向量索引。

    - force=false（默认）：增量 —— 只处理 topic_id IS NULL 的新消息
    - force=true：全量 —— 清空所有话题和向量重新构建
    """
    imp = db.query(Import).filter(Import.chat_id == chat_id).first()
    if not imp:
        raise HTTPException(404, "群聊未找到")
    _enqueue_index([chat_id], force=force)
    return {"status": "started", "chat_name": imp.chat_name, "mode": "force" if force else "incremental"}


@router.post("/rebuild-index-all")
def rebuild_all_index(force: bool = False, db: Session = Depends(get_db)):
    """重建向量索引。

    - force=false（默认）：只对 index_built=False 的群聊做增量构建
    - force=true：对所有群聊做全量重建（慎用，token 开销巨大）
    """
    query = db.query(Import)
    if not force:
        query = query.filter(Import.index_built == False)
    imports = query.all()
    if not imports:
        raise HTTPException(400, "没有需要重建索引的群聊")
    chat_ids = [imp.chat_id for imp in imports]
    _enqueue_index(chat_ids, force=force)
    return {
        "status": "started",
        "total": len(chat_ids),
        "mode": "force" if force else "incremental",
    }


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


# ---------- Watched Folders ----------

def _to_folder_info(f: WatchedFolder) -> WatchedFolderInfo:
    return WatchedFolderInfo(
        id=f.id,
        path=f.path,
        alias=f.alias,
        added_at=f.added_at,
        last_scan_at=f.last_scan_at,
        last_scan_total=f.last_scan_total or 0,
        last_scan_imported=f.last_scan_imported or 0,
        last_scan_skipped=f.last_scan_skipped or 0,
        last_scan_failed=f.last_scan_failed or 0,
    )


@router.post("/folders/validate", response_model=FolderValidateResponse)
def folder_validate(req: FolderValidateRequest):
    """校验路径是否可作为绑定目录：检查存在/可读/统计 result.json 数量"""
    info = validate_folder(req.path)
    return FolderValidateResponse(**info)


@router.get("/folders", response_model=list[WatchedFolderInfo])
def folder_list(db: Session = Depends(get_db)):
    folders = db.query(WatchedFolder).order_by(WatchedFolder.added_at.desc()).all()
    return [_to_folder_info(f) for f in folders]


@router.post("/folders", response_model=WatchedFolderInfo)
def folder_add(req: FolderAddRequest, db: Session = Depends(get_db)):
    info = validate_folder(req.path)
    if not info["valid"]:
        raise HTTPException(400, info["reason"] or "路径不合法")

    abs_path = info["resolved_path"] or resolve_path(req.path)
    existing = db.query(WatchedFolder).filter(WatchedFolder.path == abs_path).first()
    if existing:
        raise HTTPException(400, "该目录已绑定")

    alias = (req.alias or "").strip() or Path(abs_path).name or abs_path
    folder = WatchedFolder(path=abs_path, alias=alias)
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return _to_folder_info(folder)


@router.delete("/folders/{folder_id}")
def folder_delete(folder_id: int, db: Session = Depends(get_db)):
    folder = db.query(WatchedFolder).filter(WatchedFolder.id == folder_id).first()
    if not folder:
        raise HTTPException(404, "目录未找到")
    db.delete(folder)
    # 不删除 imported_files，保留历史去重信息
    db.commit()
    return {"status": "ok"}


@router.post("/folders/{folder_id}/scan", response_model=ScanResult)
def folder_scan(folder_id: int, db: Session = Depends(get_db)):
    """递归扫描绑定目录下的 result.json，导入未处理或 mtime 已变的文件"""
    folder = db.query(WatchedFolder).filter(WatchedFolder.id == folder_id).first()
    if not folder:
        raise HTTPException(404, "目录未找到")

    files = find_result_jsons(folder.path)
    pending, skipped = diff_pending(db, folder.id, files)

    file_results: list[ScanFileResult] = []
    imported_count = 0
    failed_count = 0
    new_chat_ids: list[str] = []

    for item in pending:
        path = item["path"]
        mtime = item.get("mtime") or 0.0
        size = item.get("size") or 0

        if item.get("stat_error"):
            err = f"读取文件信息失败: {item['stat_error']}"
            upsert_imported_file(
                db, folder_id=folder.id, abs_path=path, mtime=mtime,
                size=size, chat_count=0, status="error", error=err,
            )
            file_results.append(ScanFileResult(path=path, status="error", error=err))
            failed_count += 1
            continue

        try:
            chat_list = parse_export_file(path)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            err = f"JSON 解析失败: {e}"
            logger.warning("解析失败 %s: %s", path, e)
            upsert_imported_file(
                db, folder_id=folder.id, abs_path=path, mtime=mtime,
                size=size, chat_count=0, status="error", error=err,
            )
            db.commit()
            file_results.append(ScanFileResult(path=path, status="error", error=err))
            failed_count += 1
            continue
        except OSError as e:
            err = f"读取文件失败: {e}"
            logger.warning("读取失败 %s: %s", path, e)
            upsert_imported_file(
                db, folder_id=folder.id, abs_path=path, mtime=mtime,
                size=size, chat_count=0, status="error", error=err,
            )
            db.commit()
            file_results.append(ScanFileResult(path=path, status="error", error=err))
            failed_count += 1
            continue

        if not chat_list:
            err = "文件中没有有效消息"
            upsert_imported_file(
                db, folder_id=folder.id, abs_path=path, mtime=mtime,
                size=size, chat_count=0, status="error", error=err,
            )
            db.commit()
            file_results.append(ScanFileResult(path=path, status="error", error=err))
            failed_count += 1
            continue

        chat_results: list[ImportResult] = []
        try:
            for parsed in chat_list:
                r = _import_single_chat(parsed, db)
                chat_results.append(r)
                if r.message_count > 0:
                    new_chat_ids.append(r.chat_id)
        except Exception as e:
            err = f"导入数据库失败: {e}"
            logger.exception("导入失败 %s", path)
            db.rollback()
            upsert_imported_file(
                db, folder_id=folder.id, abs_path=path, mtime=mtime,
                size=size, chat_count=0, status="error", error=err,
            )
            db.commit()
            file_results.append(ScanFileResult(path=path, status="error", error=err))
            failed_count += 1
            continue

        upsert_imported_file(
            db, folder_id=folder.id, abs_path=path, mtime=mtime,
            size=size, chat_count=len(chat_results), status="ok", error=None,
        )
        db.commit()
        file_results.append(ScanFileResult(
            path=path, status="ok", chats=chat_results, error=None,
        ))
        imported_count += 1

    # 更新 folder 扫描元数据
    folder.last_scan_at = datetime.now()
    folder.last_scan_total = len(files)
    folder.last_scan_imported = imported_count
    folder.last_scan_skipped = skipped
    folder.last_scan_failed = failed_count
    db.commit()

    # 触发后台索引（去重 chat_id）
    if new_chat_ids:
        seen = set()
        unique_ids = []
        for cid in new_chat_ids:
            if cid not in seen:
                seen.add(cid)
                unique_ids.append(cid)
        _enqueue_index(unique_ids)

    return ScanResult(
        folder_id=folder.id,
        folder_path=folder.path,
        total=len(files),
        skipped=skipped,
        imported=imported_count,
        failed=failed_count,
        files=file_results,
    )


# ---------- Admin: 一次性数据修复 ----------

@router.post("/admin/dedupe-messages")
def dedupe_messages(dry_run: bool = False, db: Session = Depends(get_db)):
    """清理由历史 hash 不稳定 bug 造成的重复消息。

    分组键：(chat_id, date, sender_id, text_plain, COALESCE(reply_to_id,0),
             COALESCE(media_type,''))。每组只保留最小 id 的一行，删除其余。
    完成后：重建 messages_fts，修正 Import.message_count，标记受影响 chat 为待索引。

    传 ``?dry_run=true`` 只统计、不删数据。
    """
    # 删除前各 chat 的总数
    before_counts: dict[str, int] = dict(
        db.query(Message.chat_id, func.count(Message.id))
        .group_by(Message.chat_id)
        .all()
    )

    # 计算每个 chat 的"应保留"数量（即 distinct signature 数）
    after_counts: dict[str, int] = {}
    rows = db.execute(text("""
        SELECT chat_id, COUNT(*) AS keep
        FROM (
            SELECT chat_id
            FROM messages
            GROUP BY chat_id, date, sender_id, text_plain,
                     COALESCE(reply_to_id, 0), COALESCE(media_type, '')
        )
        GROUP BY chat_id
    """)).fetchall()
    for cid, keep in rows:
        after_counts[cid] = keep

    affected = []
    for cid, before in before_counts.items():
        after = after_counts.get(cid, 0)
        if after < before:
            affected.append({
                "chat_id": cid,
                "before": before,
                "after": after,
                "deleted": before - after,
            })

    total_deleted = sum(a["deleted"] for a in affected)

    if dry_run or total_deleted == 0:
        return {
            "dry_run": dry_run,
            "affected_chats": len(affected),
            "total_deleted": total_deleted,
            "details": affected,
        }

    # 实际删除：保留每组最小 id
    deleted_total = db.execute(text("""
        DELETE FROM messages
        WHERE id NOT IN (
            SELECT MIN(id) FROM messages
            GROUP BY chat_id, date, sender_id, text_plain,
                     COALESCE(reply_to_id, 0), COALESCE(media_type, '')
        )
    """)).rowcount or 0
    db.commit()

    # 重建 FTS（只重建受影响 chat 的）
    affected_chat_ids = [a["chat_id"] for a in affected]
    for cid in affected_chat_ids:
        db.execute(
            text("DELETE FROM messages_fts WHERE chat_id = :cid"), {"cid": cid}
        )
        db.execute(text(
            "INSERT INTO messages_fts(text_plain, sender, chat_id, msg_id) "
            "SELECT text_plain, sender, chat_id, id FROM messages "
            "WHERE chat_id = :cid AND text_plain IS NOT NULL AND text_plain != ''"
        ), {"cid": cid})

    # 修正 Import.message_count + 标记待索引（旧索引指向已删除的 message id）
    for cid in affected_chat_ids:
        new_count = db.query(Message).filter(Message.chat_id == cid).count()
        imp = db.query(Import).filter(Import.chat_id == cid).first()
        if imp:
            imp.message_count = new_count
            imp.index_built = False

    # 旧的 topics 记录里保存的 root_message_id 可能已被删掉 → 一并清理
    if affected_chat_ids:
        db.query(Topic).filter(Topic.chat_id.in_(affected_chat_ids)).delete(
            synchronize_session=False
        )
        db.query(SummaryReport).filter(
            SummaryReport.chat_id.in_(affected_chat_ids),
            SummaryReport.stale == False,
        ).update({"stale": True}, synchronize_session=False)

    db.commit()

    return {
        "dry_run": False,
        "affected_chats": len(affected),
        "total_deleted": deleted_total,
        "details": affected,
    }
