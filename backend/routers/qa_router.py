import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from backend.models.database import QAHistory, SessionLocal, get_db
from backend.models.schemas import AskRequest, AskResponse, QAHistoryItem
from backend.services.qa_agent import run_agent
from backend.services.rag_engine import answer_question, answer_question_stream

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
    history = QAHistory(
        question=req.question,
        answer=result.answer,
        sources=json.dumps([s.model_dump() for s in result.sources], ensure_ascii=False),
        chat_ids=json.dumps(req.chat_ids, ensure_ascii=False) if req.chat_ids else None,
    )
    db.add(history)
    db.commit()

    return result


@router.post("/ask/stream")
async def ask_stream(req: AskRequest):
    """流式 RAG 问答（SSE）。每行 `data: {json}\\n\\n` 推送一个事件"""
    if not req.question.strip():
        raise HTTPException(400, "问题不能为空")

    async def event_gen():
        # 用独立 Session 避免与主请求 db session 生命周期冲突
        db = SessionLocal()
        final_answer = ""
        final_sources: list[dict] = []
        try:
            async for ev in answer_question_stream(
                db=db,
                question=req.question,
                chat_ids=req.chat_ids,
                date_range=req.date_range,
                sender=req.sender,
            ):
                if ev.get("type") == "done":
                    final_answer = ev.get("answer", "")
                    final_sources = ev.get("sources", [])
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

            # 保存历史
            try:
                history = QAHistory(
                    question=req.question,
                    answer=final_answer,
                    sources=json.dumps(final_sources, ensure_ascii=False),
                    chat_ids=json.dumps(req.chat_ids, ensure_ascii=False) if req.chat_ids else None,
                )
                db.add(history)
                db.commit()
            except Exception:
                db.rollback()
        finally:
            db.close()

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲
        },
    )


@router.post("/ask/agent")
async def ask_agent(req: AskRequest):
    """Agent 式问答（SSE）：LLM 自主调用工具多轮迭代。
    
    事件类型：status, step_start, thinking_delta, tool_call, tool_result,
              step_done, final_answer, error
    """
    if not req.question.strip():
        raise HTTPException(400, "问题不能为空")

    async def event_gen():
        db = SessionLocal()
        final_answer = ""
        final_sources: list[dict] = []
        try:
            async for ev in run_agent(
                db=db,
                question=req.question,
                chat_ids=req.chat_ids,
                history=[{"role": h.role, "content": h.content} for h in req.history] if req.history else None,
            ):
                if ev.get("type") == "final_answer":
                    final_answer = ev.get("answer", "")
                    final_sources = ev.get("sources", [])
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

            # 保存历史
            try:
                history = QAHistory(
                    question=req.question,
                    answer=final_answer,
                    sources=json.dumps(final_sources, ensure_ascii=False),
                    chat_ids=json.dumps(req.chat_ids, ensure_ascii=False) if req.chat_ids else None,
                )
                db.add(history)
                db.commit()
            except Exception:
                db.rollback()
        finally:
            db.close()

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
