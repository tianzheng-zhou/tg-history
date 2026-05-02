"""QA 路由：启动后台 Run + SSE 订阅事件流。

旧的 /api/ask /api/ask/stream /api/ask/agent 三个 SSE endpoint 已改造：
- POST /api/ask/agent  → 同步返回 {run_id, session_id}（启动后台 run）
- POST /api/ask/stream → 同上（RAG 模式）
- GET  /api/runs/{run_id}/events?last_event_id=N → SSE 订阅
- POST /api/runs/{run_id}/abort → 取消 run
- GET  /api/runs/active → 列出所有 running run
- GET  /api/sessions/{sid}/active-run → 某 session 当前 run
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from backend.models.database import get_db
from backend.models.schemas import RunInfo, RunStartRequest, RunStartResponse
from backend.services import session_service
from backend.services.run_registry import registry

router = APIRouter(prefix="/api", tags=["qa"])


# ---------- Start ----------

async def _start_run_handler(req: RunStartRequest, mode_override: str | None, db: Session) -> RunStartResponse:
    if not req.question.strip():
        raise HTTPException(400, "问题不能为空")

    mode = mode_override or req.mode or "agent"
    if mode not in ("agent", "rag"):
        raise HTTPException(400, f"未知模式: {mode}")

    # 找到或创建 session（带默认 chat_ids / mode）
    session = session_service.ensure_session_for_question(
        db=db,
        session_id=req.session_id,
        question=req.question,
        mode=mode,
        chat_ids=req.chat_ids,
    )

    # 若前端带了 chat_ids 但 session 里没有，则同步到 session（首次设定）
    if req.chat_ids and not session.chat_ids:
        session_service.update_session(
            db, session.id, chat_ids=req.chat_ids, bump_updated=False
        )

    run_id, already = await registry.start(
        session_id=session.id,
        question=req.question,
        mode=mode,
        chat_ids=req.chat_ids,
        date_range=req.date_range,
        sender=req.sender,
    )
    return RunStartResponse(
        run_id=run_id,
        session_id=session.id,
        title=session.title or "新对话",
        already_running=already,
    )


@router.post("/ask/agent", response_model=RunStartResponse)
async def ask_agent(req: RunStartRequest, db: Session = Depends(get_db)):
    """启动 Agent 式问答 run。立刻返回 {run_id, session_id}，前端订阅事件流。"""
    return await _start_run_handler(req, mode_override="agent", db=db)


@router.post("/ask/stream", response_model=RunStartResponse)
async def ask_stream(req: RunStartRequest, db: Session = Depends(get_db)):
    """启动 RAG 式流式问答 run。立刻返回 {run_id, session_id}。"""
    return await _start_run_handler(req, mode_override="rag", db=db)


# ---------- Runs ----------

@router.get("/runs/{run_id}/events")
async def run_events(run_id: str, last_event_id: int = Query(-1)):
    """订阅 run 的事件流（SSE）。支持 last_event_id 续播。

    事件以 `id: {seq}\\ndata: {json}\\n\\n` 推送。
    流结束时会收到 `data: {"type":"__end__","status":"..."}`。
    """
    run = registry.get(run_id)
    if not run:
        raise HTTPException(404, "Run 不存在或已过期")

    async def event_gen():
        try:
            async for ev in registry.subscribe(run_id, last_event_id=last_event_id):
                seq = ev.get("seq", "")
                # SSE 格式：id + data
                yield f"id: {seq}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            # 客户端断开——不影响 run 本身（run 在后台继续）
            return

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/runs/{run_id}/abort")
async def run_abort(run_id: str):
    ok = await registry.abort(run_id)
    if not ok:
        raise HTTPException(404, "Run 不存在")
    return {"ok": True}


def _run_to_info(run) -> RunInfo:
    return RunInfo(
        run_id=run.id,
        session_id=run.session_id,
        mode=run.mode,
        question=run.question,
        status=run.status,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


@router.get("/runs/active", response_model=list[RunInfo])
async def list_active_runs():
    """列出所有当前 running 的 run（页面刷新时前端恢复用）。"""
    return [_run_to_info(r) for r in registry.list_active()]


@router.get("/sessions/{session_id}/active-run", response_model=RunInfo)
async def get_session_active_run(session_id: str):
    run = registry.get_active_for_session(session_id)
    if not run:
        raise HTTPException(404, "该会话无活跃 run")
    return _run_to_info(run)
