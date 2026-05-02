from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.models.database import QAHistory, get_db
from backend.models.schemas import AskRequest, AskResponse, QAHistoryItem
from backend.services.rag_engine import answer_question

router = APIRouter(prefix="/api", tags=["qa"])


@router.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest, db: Session = Depends(get_db)):
    """提交问题，获取 RAG 回答"""
    if not req.question.strip():
        raise HTTPException(400, "问题不能为空")

    result = await answer_question(
        db=db,
        question=req.question,
        chat_ids=req.chat_ids,
        date_range=req.date_range,
        sender=req.sender,
    )

    # 保存问答历史
    import json

    history = QAHistory(
        question=req.question,
        answer=result.answer,
        sources=json.dumps([s.model_dump() for s in result.sources], ensure_ascii=False),
        chat_ids=json.dumps(req.chat_ids, ensure_ascii=False) if req.chat_ids else None,
    )
    db.add(history)
    db.commit()

    return result


@router.get("/ask/history", response_model=list[QAHistoryItem])
def get_qa_history(limit: int = 50, db: Session = Depends(get_db)):
    """获取历史问答"""
    records = (
        db.query(QAHistory)
        .order_by(QAHistory.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        QAHistoryItem(
            id=r.id,
            question=r.question,
            answer=r.answer,
            created_at=r.created_at,
        )
        for r in records
    ]
