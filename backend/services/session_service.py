"""ChatSession / ChatTurn CRUD 封装。

每个 session 是一次多轮对话容器，turns 按 seq 顺序保存用户问题与 assistant 回答。
assistant turn 附带完整 Agent trajectory 和 usage meta。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, desc, func, or_
from sqlalchemy.orm import Session

from backend.models.database import Artifact, ArtifactVersion, ChatSession, ChatTurn


# ---------- utilities ----------

def _parse_json(s: str | None) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def _dump_json(obj: Any) -> str | None:
    if obj is None:
        return None
    return json.dumps(obj, ensure_ascii=False)


def _derive_title(question: str, max_len: int = 24) -> str:
    q = (question or "").strip().replace("\n", " ")
    if len(q) <= max_len:
        return q or "新对话"
    return q[:max_len] + "…"


def _derive_preview(text: str | None, max_len: int = 80) -> str:
    if not text:
        return ""
    s = text.strip().replace("\n", " ")
    if len(s) <= max_len:
        return s
    return s[:max_len] + "…"


def session_to_dict(s: ChatSession, artifact_count: int = 0) -> dict:
    return {
        "id": s.id,
        "title": s.title or "新对话",
        "mode": s.mode or "agent",
        "chat_ids": _parse_json(s.chat_ids),
        "pinned": bool(s.pinned),
        "archived": bool(s.archived),
        "turn_count": s.turn_count or 0,
        "artifact_count": artifact_count,
        "last_preview": s.last_preview,
        "created_at": s.created_at or datetime.utcnow(),
        "updated_at": s.updated_at or datetime.utcnow(),
    }


def count_artifacts(db: Session, session_id: str) -> int:
    """统计某 session 下的 artifact 数量（不含版本数）。"""
    return (
        db.query(func.count(Artifact.id))
        .filter(Artifact.session_id == session_id)
        .scalar()
        or 0
    )


def count_artifacts_bulk(db: Session, session_ids: list[str]) -> dict[str, int]:
    """批量统计多个 session 的 artifact 数量。返回 {session_id: count}，未出现的视为 0。"""
    if not session_ids:
        return {}
    rows = (
        db.query(Artifact.session_id, func.count(Artifact.id))
        .filter(Artifact.session_id.in_(session_ids))
        .group_by(Artifact.session_id)
        .all()
    )
    return {sid: cnt for sid, cnt in rows}


def turn_to_dict(t: ChatTurn) -> dict:
    return {
        "id": t.id,
        "seq": t.seq,
        "role": t.role,
        "content": t.content,
        "sources": _parse_json(t.sources),
        "trajectory": _parse_json(t.trajectory),
        "mode": t.mode,
        "meta": _parse_json(t.meta),
        "created_at": t.created_at or datetime.utcnow(),
    }


# ---------- CRUD ----------

def create_session(
    db: Session,
    title: str | None = None,
    mode: str = "agent",
    chat_ids: list[str] | None = None,
) -> ChatSession:
    sid = uuid.uuid4().hex
    now = datetime.utcnow()
    s = ChatSession(
        id=sid,
        title=title or "新对话",
        mode=mode,
        chat_ids=_dump_json(chat_ids) if chat_ids else None,
        pinned=False,
        archived=False,
        turn_count=0,
        last_preview=None,
        created_at=now,
        updated_at=now,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def get_session(db: Session, session_id: str) -> ChatSession | None:
    return db.query(ChatSession).filter(ChatSession.id == session_id).first()


def list_sessions(
    db: Session,
    archived: bool = False,
    pinned: bool | None = None,
    q: str | None = None,
    limit: int = 30,
    offset: int = 0,
) -> tuple[list[ChatSession], int]:
    """列出会话。按 pinned DESC, updated_at DESC 排序。

    q 搜索：对 title 或 任一 turn.content 做 LIKE 匹配。
    """
    query = db.query(ChatSession).filter(ChatSession.archived == archived)

    if pinned is not None:
        query = query.filter(ChatSession.pinned == pinned)

    if q:
        pattern = f"%{q}%"
        # 匹配 title 或 turns.content
        matched_session_ids = (
            db.query(ChatTurn.session_id)
            .filter(ChatTurn.content.like(pattern))
            .distinct()
            .subquery()
        )
        query = query.filter(
            or_(
                ChatSession.title.like(pattern),
                ChatSession.id.in_(matched_session_ids),
            )
        )

    total = query.count()
    items = (
        query.order_by(desc(ChatSession.pinned), desc(ChatSession.updated_at))
        .limit(limit)
        .offset(offset)
        .all()
    )
    return items, total


def update_session(
    db: Session,
    session_id: str,
    *,
    title: str | None = None,
    pinned: bool | None = None,
    archived: bool | None = None,
    mode: str | None = None,
    chat_ids: list[str] | None = None,
    bump_updated: bool = True,
) -> ChatSession | None:
    s = get_session(db, session_id)
    if not s:
        return None
    if title is not None:
        s.title = title
    if pinned is not None:
        s.pinned = pinned
    if archived is not None:
        s.archived = archived
    if mode is not None:
        s.mode = mode
    if chat_ids is not None:
        s.chat_ids = _dump_json(chat_ids) if chat_ids else None
    if bump_updated:
        s.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(s)
    return s


def delete_session(db: Session, session_id: str) -> bool:
    s = get_session(db, session_id)
    if not s:
        return False
    # 级联删除 artifacts + 版本（FK CASCADE 也会兜底，但 ORM 层显式删更稳）
    artifact_ids = [
        aid for (aid,) in db.query(Artifact.id).filter(Artifact.session_id == session_id).all()
    ]
    if artifact_ids:
        db.query(ArtifactVersion).filter(
            ArtifactVersion.artifact_id.in_(artifact_ids)
        ).delete(synchronize_session=False)
        db.query(Artifact).filter(Artifact.id.in_(artifact_ids)).delete(synchronize_session=False)
    # 级联删除 turns
    db.query(ChatTurn).filter(ChatTurn.session_id == session_id).delete()
    db.delete(s)
    db.commit()
    return True


# ---------- Turn ops ----------

def get_turns(db: Session, session_id: str) -> list[ChatTurn]:
    return (
        db.query(ChatTurn)
        .filter(ChatTurn.session_id == session_id)
        .order_by(ChatTurn.seq)
        .all()
    )


def _next_seq(db: Session, session_id: str) -> int:
    max_seq = (
        db.query(func.max(ChatTurn.seq))
        .filter(ChatTurn.session_id == session_id)
        .scalar()
    )
    return (max_seq + 1) if max_seq is not None else 0


def append_turn(
    db: Session,
    session_id: str,
    role: str,
    content: str | None,
    sources: list | None = None,
    trajectory: dict | None = None,
    mode: str | None = None,
    meta: dict | None = None,
) -> ChatTurn:
    """追加一条 turn，并更新 session 的 turn_count / last_preview / updated_at。"""
    seq = _next_seq(db, session_id)
    now = datetime.utcnow()
    t = ChatTurn(
        session_id=session_id,
        seq=seq,
        role=role,
        content=content,
        sources=_dump_json(sources) if sources else None,
        trajectory=_dump_json(trajectory) if trajectory else None,
        mode=mode,
        meta=_dump_json(meta) if meta else None,
        created_at=now,
    )
    db.add(t)

    # 更新 session 汇总字段
    s = get_session(db, session_id)
    if s:
        s.turn_count = (s.turn_count or 0) + 1
        s.last_preview = _derive_preview(content)
        s.updated_at = now

    db.commit()
    db.refresh(t)
    return t


def get_history_messages(db: Session, session_id: str) -> list[dict]:
    """返回给 agent 用的对话历史（仅 role + content，忽略 trajectory）。

    不含当前正在生成的 turn（调用此函数时尚未追加）。

    **前缀缓存关键**：user turn 的 content 只存纯净的用户问题，
    但 LLM 看到的 user message 实际是 `meta.injected_prefix + content`
    （时间戳 + artifact 快照等注入信息）。这里重放历史时**重新拼出那份内容**，
    保证和上一轮提交给 LLM 的 user message 完全一致，从而前缀缓存能命中。
    """
    turns = get_turns(db, session_id)
    result: list[dict] = []
    for t in turns:
        if t.role not in ("user", "assistant") or not t.content:
            continue
        content = t.content
        if t.role == "user":
            meta = _parse_json(t.meta) or {}
            prefix = meta.get("injected_prefix")
            if prefix:
                content = f"{prefix}\n\n---\n\n{content}"
        result.append({"role": t.role, "content": content})
    return result


def ensure_session_for_question(
    db: Session,
    session_id: str | None,
    question: str,
    mode: str = "agent",
    chat_ids: list[str] | None = None,
) -> ChatSession:
    """若 session_id 存在则取出；否则新建并用 question 派生 title。"""
    if session_id:
        s = get_session(db, session_id)
        if s:
            return s
    title = _derive_title(question)
    return create_session(db, title=title, mode=mode, chat_ids=chat_ids)
