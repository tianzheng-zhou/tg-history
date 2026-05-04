import asyncio
import logging
import threading

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.models.database import Import, SummaryReport, SessionLocal, get_db
from backend.models.schemas import SummarizeRequest, SummaryItem
from backend.services.main_loop import schedule_on_main_loop
from backend.services.summarizer import run_summarize

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["summary"])

# ---------- 后台摘要生成队列 ----------

_summary_lock = threading.Lock()
_summary_queue: list[dict] = []  # [{"chat_id", "chat_name", "force"}]
_summary_progress: dict = {
    "running": False,
    "total": 0,
    "completed": 0,
    "active_chats": [],          # 正在并行处理的群聊名
    "chat_details": {},          # chat_name → {stage, map_done, map_total}
    "results": [],               # [{chat_name, status, error}]
    # 兼容旧字段
    "chat_id": "",
    "chat_name": "",
    "stage": "",
    "map_total": 0,
    "map_done": 0,
    "queued": 0,
    "error": "",
}

MAX_PARALLEL_SUMMARY = 8  # 群聊级并发上限


def _enqueue_summary(chat_ids: list[tuple[str, str, bool]]):
    """chat_ids: [(chat_id, chat_name, force), ...]"""
    with _summary_lock:
        existing = {item["chat_id"] for item in _summary_queue}
        for cid, cname, force in chat_ids:
            if cid not in existing:
                _summary_queue.append({"chat_id": cid, "chat_name": cname, "force": force})
                existing.add(cid)
        _summary_progress["queued"] = len(_summary_queue)

        if _summary_progress["running"]:
            _summary_progress["total"] = _summary_progress["completed"] + len(_summary_queue)
            return

    # 调度到 FastAPI 主循环上跑（不要另起线程 + asyncio.run，
    # 否则会和 llm_adapter 模块级 Semaphore / httpx 客户端绑定的循环冲突）
    schedule_on_main_loop(_summary_runner())


async def _summary_runner():
    with _summary_lock:
        _summary_progress["running"] = True
        _summary_progress["completed"] = 0
        _summary_progress["total"] = len(_summary_queue)
        _summary_progress["active_chats"] = []
        _summary_progress["chat_details"] = {}
        _summary_progress["results"] = []
        _summary_progress["error"] = ""

    async def _process_one(task: dict, sem: asyncio.Semaphore):
        async with sem:
            chat_id = task["chat_id"]
            chat_name = task["chat_name"]
            force = task["force"]

            db = SessionLocal()
            try:
                detail = {"stage": "map", "map_done": 0, "map_total": 0}
                with _summary_lock:
                    _summary_progress["active_chats"].append(chat_name)
                    _summary_progress["chat_details"][chat_name] = detail
                    _summary_progress["chat_name"] = chat_name
                    _summary_progress["stage"] = "map"

                try:
                    # async 函数主循环里做 sync db.query 会让 /summary-progress 等
                    # 轮询抖动；派 thread 保证主循环干净
                    def _load_existing():
                        rows = db.query(SummaryReport).filter(
                            SummaryReport.chat_id == chat_id
                        ).all()
                        ids = [r.id for r in rows]
                        has_valid_local = any(not (r.stale or False) for r in rows)
                        return rows, ids, has_valid_local
                    existing, old_ids, has_valid = await asyncio.to_thread(_load_existing)
                    if existing and not force and has_valid:
                        with _summary_lock:
                            _summary_progress["results"].append({
                                "chat_name": chat_name, "status": "skipped"
                            })
                        return

                    # 先生成新摘要（run_summarize 会 commit 一条新 SummaryReport）
                    await run_summarize(db, chat_id, progress=detail)

                    # 生成成功后才删除旧的，确保失败时旧摘要不丢
                    if old_ids:
                        def _delete_old():
                            db.query(SummaryReport).filter(
                                SummaryReport.id.in_(old_ids)
                            ).delete(synchronize_session=False)
                            db.commit()
                        await asyncio.to_thread(_delete_old)

                    with _summary_lock:
                        _summary_progress["results"].append({
                            "chat_name": chat_name, "status": "ok"
                        })
                    logger.info(f"摘要生成完成: {chat_name}")
                except Exception as e:
                    logger.warning(f"摘要生成失败({chat_name}): {e}")
                    with _summary_lock:
                        _summary_progress["results"].append({
                            "chat_name": chat_name, "status": "error", "error": str(e)[:200]
                        })
                    db.rollback()
                finally:
                    with _summary_lock:
                        _summary_progress["completed"] += 1
                        if chat_name in _summary_progress["active_chats"]:
                            _summary_progress["active_chats"].remove(chat_name)
                        _summary_progress["chat_details"].pop(chat_name, None)
            finally:
                db.close()

    sem = asyncio.Semaphore(MAX_PARALLEL_SUMMARY)
    with _summary_lock:
        all_tasks = list(_summary_queue)
        _summary_queue.clear()
        _summary_progress["queued"] = 0

    coros = [asyncio.create_task(_process_one(t, sem)) for t in all_tasks]
    if coros:
        await asyncio.gather(*coros)

    with _summary_lock:
        _summary_progress["running"] = False
        _summary_progress["active_chats"] = []
        _summary_progress["chat_details"] = {}
        _summary_progress["stage"] = "done"


# ---------- API ----------

@router.post("/summarize")
def trigger_summarize(req: SummarizeRequest, db: Session = Depends(get_db)):
    """触发单个群聊摘要生成（后台执行）"""
    imp = db.query(Import).filter(Import.chat_id == req.chat_id).first()
    if not imp:
        raise HTTPException(404, "群聊未找到")

    existing = db.query(SummaryReport).filter(SummaryReport.chat_id == req.chat_id).first()
    if existing and not req.force and not (existing.stale if existing.stale else False):
        return {"status": "exists", "message": "摘要已存在，设置 force=true 可重新生成"}

    _enqueue_summary([(req.chat_id, imp.chat_name, req.force)])
    return {"status": "started", "message": "摘要生成已加入队列"}


@router.post("/summarize-all")
def trigger_summarize_all(force: bool = False, db: Session = Depends(get_db)):
    """批量生成所有已索引群聊的摘要。force=true 强制重新生成已存在的"""
    # 只对已建好向量索引的群聊生成摘要
    imports = db.query(Import).filter(Import.index_built == True).all()
    if not imports:
        raise HTTPException(400, "没有已索引的群聊")

    tasks: list[tuple[str, str, bool]] = []
    for imp in imports:
        existing = db.query(SummaryReport).filter(SummaryReport.chat_id == imp.chat_id).first()
        if existing and not force and not (existing.stale if existing.stale else False):
            continue  # 已存在且未过期，跳过
        tasks.append((imp.chat_id, imp.chat_name, force))

    if not tasks:
        return {"status": "exists", "message": "所有群聊摘要均已存在", "total": 0}

    _enqueue_summary(tasks)
    return {"status": "started", "total": len(tasks)}


@router.get("/summary-progress")
def get_summary_progress():
    """查询摘要生成进度。

    返回前需要 deep-copy 可变字段（results / active_chats / chat_details），
    否则 worker 在 FastAPI 序列化期间 append / setitem 会触发
    ``RuntimeError: dictionary changed size during iteration`` 或类似错误。
    同 /import-progress 与 /index-progress 的处理。
    """
    with _summary_lock:
        snap = dict(_summary_progress)
        snap["active_chats"] = list(_summary_progress["active_chats"])
        snap["results"] = list(_summary_progress["results"])
        snap["chat_details"] = {
            k: dict(v) for k, v in _summary_progress["chat_details"].items()
        }
        snap["queued"] = len(_summary_queue)
        return snap


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
