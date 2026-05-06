"""ChatSession / ChatTurn 的 REST API。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from backend.config import settings
from backend.models.database import get_db
from backend.models.schemas import (
    SessionCreateRequest,
    SessionDetailResponse,
    SessionListResponse,
    SessionSummary,
    SessionUpdateRequest,
    TurnItem,
)
from backend.services import artifact_service, llm_adapter, session_service

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _to_summary(s, artifact_count: int = 0) -> SessionSummary:
    d = session_service.session_to_dict(s, artifact_count=artifact_count)
    return SessionSummary(**d)


def _to_summary_with_count(db: Session, s) -> SessionSummary:
    """便捷封装：单 session 时直接计数。批量场景应该用 count_artifacts_bulk 避免 N+1。"""
    cnt = session_service.count_artifacts(db, s.id)
    return _to_summary(s, artifact_count=cnt)


def _to_turn(t) -> TurnItem:
    d = session_service.turn_to_dict(t)
    return TurnItem(**d)


@router.post("", response_model=SessionSummary)
def create_session(req: SessionCreateRequest, db: Session = Depends(get_db)):
    s = session_service.create_session(
        db,
        title=req.title or "新对话",
        mode=req.mode or "agent",
        chat_ids=req.chat_ids,
    )
    return _to_summary(s)


@router.get("", response_model=SessionListResponse)
def list_sessions(
    archived: bool = Query(False),
    pinned: bool | None = Query(None),
    q: str | None = Query(None),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    items, total = session_service.list_sessions(
        db, archived=archived, pinned=pinned, q=q, limit=limit, offset=offset
    )
    counts = session_service.count_artifacts_bulk(db, [s.id for s in items])
    return SessionListResponse(
        sessions=[_to_summary(s, artifact_count=counts.get(s.id, 0)) for s in items],
        total=total,
    )


@router.get("/{session_id}", response_model=SessionDetailResponse)
def get_session(session_id: str, db: Session = Depends(get_db)):
    s = session_service.get_session(db, session_id)
    if not s:
        raise HTTPException(404, "会话不存在")
    turns = session_service.get_turns(db, session_id)
    artifacts = artifact_service.list_artifacts(db, session_id)
    art_dicts = [artifact_service.artifact_to_summary_dict(a) for a in artifacts]
    return SessionDetailResponse(
        session=_to_summary(s, artifact_count=len(artifacts)),
        turns=[_to_turn(t) for t in turns],
        artifacts=art_dicts,
    )


@router.patch("/{session_id}", response_model=SessionSummary)
def update_session(
    session_id: str,
    req: SessionUpdateRequest,
    db: Session = Depends(get_db),
):
    s = session_service.update_session(
        db,
        session_id,
        title=req.title,
        pinned=req.pinned,
        archived=req.archived,
        mode=req.mode,
        chat_ids=req.chat_ids,
        # 只有切换 pinned / archived / chat_ids / mode 都算"改配置"，
        # 真正的对话活动走 append_turn 内部更新；这里仅重命名时不该 bump_updated
        bump_updated=not (req.title is not None and req.pinned is None
                          and req.archived is None and req.mode is None
                          and req.chat_ids is None),
    )
    if not s:
        raise HTTPException(404, "会话不存在")
    return _to_summary_with_count(db, s)


@router.delete("/{session_id}")
def delete_session(session_id: str, db: Session = Depends(get_db)):
    ok = session_service.delete_session(db, session_id)
    if not ok:
        raise HTTPException(404, "会话不存在")
    return {"ok": True}


@router.post("/{session_id}/autotitle", response_model=SessionSummary)
async def autotitle_session(session_id: str, db: Session = Depends(get_db)):
    """用 LLM 生成一行短标题（基于首轮 Q&A）。"""
    s = session_service.get_session(db, session_id)
    if not s:
        raise HTTPException(404, "会话不存在")
    turns = session_service.get_turns(db, session_id)
    if len(turns) < 2:
        return _to_summary_with_count(db, s)

    first_q = turns[0].content or ""
    first_a = turns[1].content or ""
    prompt = (
        "请用不超过 16 个汉字（或 24 个英文字符）为下面这轮问答起一个精炼标题，"
        "不要加标点、不要加引号、只输出标题本身。\n\n"
        f"问题：{first_q[:500]}\n\n回答：{first_a[:800]}"
    )
    try:
        title = await llm_adapter.chat(
            messages=[{"role": "user", "content": prompt}],
            model=settings.llm_model_qa,
            temperature=0.3,
            max_tokens=64,
            enable_thinking=False,
        )
        title = (title or "").strip().replace("\n", " ").strip('"“”\'「」')[:32]
        if title:
            s = session_service.update_session(db, session_id, title=title, bump_updated=False)
    except Exception:
        pass
    return _to_summary_with_count(db, s)


@router.get("/{session_id}/export")
def export_session(
    session_id: str,
    format: str = Query("md", regex="^(md|json)$"),
    db: Session = Depends(get_db),
):
    s = session_service.get_session(db, session_id)
    if not s:
        raise HTTPException(404, "会话不存在")
    turns = session_service.get_turns(db, session_id)

    if format == "json":
        payload = {
            "session": session_service.session_to_dict(s),
            "turns": [session_service.turn_to_dict(t) for t in turns],
        }
        # datetime → isoformat
        def _default(o):
            try:
                return o.isoformat()
            except Exception:
                return str(o)
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=_default)
        return PlainTextResponse(
            text,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{session_id}.json"'},
        )

    # Markdown
    # s.created_at 是 aware UTC（UtcDateTime 列），导出文档显示服务器本地时间更友好
    created_local = s.created_at.astimezone().strftime("%Y-%m-%d %H:%M") if s.created_at else "—"
    lines: list[str] = [f"# {s.title}", ""]
    lines.append(f"- 创建时间：{created_local}")
    lines.append(f"- 模式：{s.mode}")
    lines.append(f"- 消息数：{s.turn_count}")
    lines.append("")
    for t in turns:
        if t.role == "user":
            lines.append(f"## 🧑 用户（#{t.seq}）")
        else:
            lines.append(f"## 🤖 助手（#{t.seq}）")
        lines.append("")
        lines.append(t.content or "")
        lines.append("")
        if t.role == "assistant" and t.sources:
            try:
                src_list = json.loads(t.sources)
            except Exception:
                src_list = None
            if src_list:
                lines.append("**来源引用：**")
                for src in src_list:
                    lines.append(f"- {src.get('sender', '?')} · {src.get('date', '?')} — {src.get('preview', '')}")
                lines.append("")
    text = "\n".join(lines)
    return PlainTextResponse(
        text,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{session_id}.md"'},
    )
