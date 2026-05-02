from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.models.database import Import, SummaryReport, get_db
from backend.models.schemas import SummarizeRequest, SummaryItem
from backend.services.summarizer import run_summarize

router = APIRouter(prefix="/api", tags=["summary"])


@router.post("/summarize")
async def trigger_summarize(req: SummarizeRequest, db: Session = Depends(get_db)):
    """触发摘要生成（同步执行，后续可改为异步任务）"""
    imp = db.query(Import).filter(Import.chat_id == req.chat_id).first()
    if not imp:
        raise HTTPException(404, "群聊未找到")

    existing = (
        db.query(SummaryReport)
        .filter(SummaryReport.chat_id == req.chat_id)
        .first()
    )
    if existing and not req.force:
        return {"status": "exists", "message": "摘要已存在，设置 force=true 可重新生成"}

    if req.force:
        db.query(SummaryReport).filter(SummaryReport.chat_id == req.chat_id).delete()
        db.commit()

    result = await run_summarize(db, req.chat_id)
    return {"status": "ok", "categories": list(result.keys())}


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
    )
