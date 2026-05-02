import asyncio
import logging
import threading

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.models.database import Import, SummaryReport, SessionLocal, get_db
from backend.models.schemas import SummarizeRequest, SummaryItem
from backend.services.summarizer import run_summarize

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["summary"])

# ---------- 后台摘要生成队列 ----------

_summary_lock = threading.Lock()
_summary_queue: list[dict] = []  # [{"chat_id", "chat_name", "force"}]
_summary_progress: dict = {
    "running": False,
    "chat_id": "",
    "chat_name": "",
    "stage": "",       # "map" | "reduce" | "done" | "error"
    "map_total": 0,
    "map_done": 0,
    "queued": 0,
    "error": "",
}


def _enqueue_summary(chat_id: str, chat_name: str, force: bool):
    with _summary_lock:
        # 去重
        for item in _summary_queue:
            if item["chat_id"] == chat_id:
                return
        if _summary_progress["running"] and _summary_progress["chat_id"] == chat_id:
            return
        _summary_queue.append({"chat_id": chat_id, "chat_name": chat_name, "force": force})
        _summary_progress["queued"] = len(_summary_queue)

        if _summary_progress["running"]:
            return

    t = threading.Thread(target=_summary_worker, daemon=True)
    t.start()


def _summary_worker():
    with _summary_lock:
        _summary_progress["running"] = True
        _summary_progress["error"] = ""

    async def _run():
        db = SessionLocal()
        try:
            while True:
                with _summary_lock:
                    if not _summary_queue:
                        break
                    task = _summary_queue.pop(0)
                    _summary_progress["queued"] = len(_summary_queue)

                chat_id = task["chat_id"]
                chat_name = task["chat_name"]
                force = task["force"]

                _summary_progress["chat_id"] = chat_id
                _summary_progress["chat_name"] = chat_name
                _summary_progress["stage"] = "map"
                _summary_progress["map_total"] = 0
                _summary_progress["map_done"] = 0
                _summary_progress["error"] = ""

                try:
                    # 删除旧摘要
                    existing = db.query(SummaryReport).filter(SummaryReport.chat_id == chat_id).first()
                    if existing and (force or existing.stale):
                        db.query(SummaryReport).filter(SummaryReport.chat_id == chat_id).delete()
                        db.commit()
                    elif existing and not force and not existing.stale:
                        continue  # 已存在且未过期，跳过

                    await run_summarize(db, chat_id, progress=_summary_progress)
                    _summary_progress["stage"] = "done"
                    logger.info(f"摘要生成完成: {chat_name}")
                except Exception as e:
                    logger.warning(f"摘要生成失败({chat_name}): {e}")
                    _summary_progress["stage"] = "error"
                    _summary_progress["error"] = str(e)[:200]
                    db.rollback()
        finally:
            _summary_progress["running"] = False
            db.close()

    asyncio.run(_run())


# ---------- API ----------

@router.post("/summarize")
def trigger_summarize(req: SummarizeRequest, db: Session = Depends(get_db)):
    """触发摘要生成（后台执行）"""
    imp = db.query(Import).filter(Import.chat_id == req.chat_id).first()
    if not imp:
        raise HTTPException(404, "群聊未找到")

    existing = db.query(SummaryReport).filter(SummaryReport.chat_id == req.chat_id).first()
    if existing and not req.force and not (existing.stale if existing.stale else False):
        return {"status": "exists", "message": "摘要已存在，设置 force=true 可重新生成"}

    _enqueue_summary(req.chat_id, imp.chat_name, req.force)
    return {"status": "started", "message": "摘要生成已加入队列"}


@router.get("/summary-progress")
def get_summary_progress():
    """查询摘要生成进度"""
    with _summary_lock:
        return {**_summary_progress, "queued": len(_summary_queue)}


@router.get("/summaries/{chat_id}", response_model=list[SummaryItem])
def get_summaries(chat_id: str, db: Session = Depends(get_db)):
    """获取群聊的摘要报告"""
    reports = (
        db.query(SummaryReport)
        .filter(SummaryReport.chat_id == chat_id)
        .order_by(SummaryReport.category)
        .all()
    )
    return [
        SummaryItem(
            id=r.id,
            chat_id=r.chat_id,
            category=r.category,
            content=r.content,
            generated_at=r.generated_at,
            stale=r.stale or False,
        )
        for r in reports
    ]


@router.get("/summaries/{chat_id}/category/{category}", response_model=SummaryItem | None)
def get_summary_by_category(chat_id: str, category: str, db: Session = Depends(get_db)):
    """按分类获取摘要"""
    report = (
        db.query(SummaryReport)
        .filter(SummaryReport.chat_id == chat_id, SummaryReport.category == category)
        .first()
    )
    if not report:
        raise HTTPException(404, "该分类暂无摘要")
    return SummaryItem(
        id=report.id,
        chat_id=report.chat_id,
        category=report.category,
        content=report.content,
        generated_at=report.generated_at,
        stale=report.stale or False,
    )
