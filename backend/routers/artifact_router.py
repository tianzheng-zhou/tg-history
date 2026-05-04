"""Artifact 的 REST API。

挂在 /api/sessions/{session_id}/artifacts 下，与 session 强绑。

- GET    /                      列出该 session 下所有 artifact 元信息
- GET    /{key}                 取最新版完整内容（?version=N 取历史版本）
- GET    /{key}/versions        列出全部版本元信息
- DELETE /{key}                 删除 artifact + 全部版本
- GET    /{key}/export          导出为 .md 文件
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from backend.models.database import get_db
from backend.models.schemas import (
    ArtifactDetail,
    ArtifactSummary,
    ArtifactVersionItem,
)
from backend.services import artifact_service, session_service

router = APIRouter(prefix="/api/sessions/{session_id}/artifacts", tags=["artifacts"])


def _ensure_session(db: Session, session_id: str) -> None:
    if session_service.get_session(db, session_id) is None:
        raise HTTPException(404, "会话不存在")


@router.get("", response_model=list[ArtifactSummary])
def list_session_artifacts(session_id: str, db: Session = Depends(get_db)):
    """列出 session 下所有 artifact（不含正文）。"""
    _ensure_session(db, session_id)
    arts = artifact_service.list_artifacts(db, session_id)
    return [artifact_service.artifact_to_summary_dict(a) for a in arts]


@router.get("/{artifact_key}", response_model=ArtifactDetail)
def get_session_artifact(
    session_id: str,
    artifact_key: str,
    version: int | None = Query(None, ge=1, description="历史版本号；省略时返回最新"),
    db: Session = Depends(get_db),
):
    """取 artifact 完整内容；version=None 返回最新，否则返回指定历史版本。"""
    _ensure_session(db, session_id)
    art = artifact_service.get_artifact(db, session_id, artifact_key)
    if art is None:
        raise HTTPException(404, f"artifact '{artifact_key}' 不存在")
    ver = artifact_service.get_version(db, art.id, version)
    if ver is None:
        raise HTTPException(404, f"artifact '{artifact_key}' 没有版本 {version}")
    return artifact_service.artifact_to_detail_dict(art, ver)


@router.get("/{artifact_key}/versions", response_model=list[ArtifactVersionItem])
def list_artifact_versions(
    session_id: str,
    artifact_key: str,
    db: Session = Depends(get_db),
):
    """列出 artifact 的全部版本（不含正文）。"""
    _ensure_session(db, session_id)
    art = artifact_service.get_artifact(db, session_id, artifact_key)
    if art is None:
        raise HTTPException(404, f"artifact '{artifact_key}' 不存在")
    versions = artifact_service.list_versions(db, art.id)
    return [artifact_service.version_to_item_dict(v) for v in versions]


@router.delete("/{artifact_key}")
def delete_session_artifact(
    session_id: str,
    artifact_key: str,
    db: Session = Depends(get_db),
):
    """删除 artifact + 全部版本。"""
    _ensure_session(db, session_id)
    ok = artifact_service.delete_artifact(db, session_id, artifact_key)
    if not ok:
        raise HTTPException(404, f"artifact '{artifact_key}' 不存在")
    return {"ok": True}


@router.get("/{artifact_key}/export")
def export_artifact(
    session_id: str,
    artifact_key: str,
    version: int | None = Query(None, ge=1),
    db: Session = Depends(get_db),
):
    """导出 artifact 为 .md 文件下载。"""
    _ensure_session(db, session_id)
    art = artifact_service.get_artifact(db, session_id, artifact_key)
    if art is None:
        raise HTTPException(404, f"artifact '{artifact_key}' 不存在")
    ver = artifact_service.get_version(db, art.id, version)
    if ver is None:
        raise HTTPException(404, f"artifact '{artifact_key}' 没有版本 {version}")

    # 文件名：用 artifact_key 而不是 title，避免中文/标点编码问题
    suffix = f"-v{ver.version}" if version is not None else ""
    filename = f"{art.artifact_key}{suffix}.md"

    # 头部加一行 frontmatter 风格的元信息，方便用户离线查看上下文
    body = (
        f"<!-- artifact: {art.artifact_key} | version: {ver.version} | "
        f"updated: {(art.updated_at or '').isoformat() if art.updated_at else ''} -->\n"
        f"# {art.title}\n\n"
        f"{ver.content}\n"
    )
    return PlainTextResponse(
        body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
