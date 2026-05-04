"""Article（文章库）REST API。

两个挂载点：
- `/api/articles/*`           跨 session 的"文章库"视图 + 草稿总览
- `/api/sessions/{sid}/artifacts/{key}/publish | publications`
                              session 内的发布动作 + 查询该 artifact 的发布历史
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from backend.models.database import get_db
from backend.models.schemas import (
    ArticleDetail,
    ArticleItem,
    DraftItem,
    PublishRequest,
)
from backend.services import article_service, session_service


# ============================================================================
# Router 1: /api/articles —— 全局视图
# ============================================================================

router = APIRouter(prefix="/api/articles", tags=["articles"])


@router.get("/drafts", response_model=list[DraftItem])
def list_drafts(db: Session = Depends(get_db)):
    """跨 session 列出所有 artifact（= 草稿视图），带 publication_count。"""
    return article_service.list_drafts(db)


@router.get("", response_model=list[ArticleItem])
def list_articles(db: Session = Depends(get_db)):
    """跨 session 列出所有已发布文章，按 content_created_at 倒序。"""
    return article_service.list_articles(db)


@router.get("/{article_id}", response_model=ArticleDetail)
def get_article(article_id: str, db: Session = Depends(get_db)):
    """取单篇文章完整内容。"""
    article = article_service.get_article(db, article_id)
    if article is None:
        raise HTTPException(404, f"文章 {article_id} 不存在")
    return article


@router.delete("/{article_id}")
def delete_article(article_id: str, db: Session = Depends(get_db)):
    """从文章库撤回一篇文章（不影响源 artifact）。"""
    ok = article_service.delete_article(db, article_id)
    if not ok:
        raise HTTPException(404, f"文章 {article_id} 不存在")
    return {"ok": True}


@router.get("/{article_id}/export")
def export_article(article_id: str, db: Session = Depends(get_db)):
    """导出文章为 .md 文件下载。"""
    article = article_service.get_article(db, article_id)
    if article is None:
        raise HTTPException(404, f"文章 {article_id} 不存在")

    filename = f"{article['source_artifact_key']}-v{article['source_version_number']}.md"
    ts = article["content_created_at"]
    ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
    body = (
        f"<!-- article: {article['id']} | source: "
        f"{article['source_session_title']} / {article['source_artifact_key']} "
        f"v{article['source_version_number']} | generated: {ts_str} -->\n"
        f"# {article['title']}\n\n"
        f"{article['content']}\n"
    )
    return PlainTextResponse(
        body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ============================================================================
# Router 2: /api/sessions/{sid}/artifacts/{key}/(publish|publications)
# ============================================================================

publish_router = APIRouter(
    prefix="/api/sessions/{session_id}/artifacts/{artifact_key}",
    tags=["articles"],
)


def _ensure_session(db: Session, session_id: str) -> None:
    if session_service.get_session(db, session_id) is None:
        raise HTTPException(404, "会话不存在")


@publish_router.post("/publish", response_model=ArticleDetail)
def publish_artifact(
    session_id: str,
    artifact_key: str,
    req: PublishRequest,
    db: Session = Depends(get_db),
):
    """将指定 artifact 的当前版本发布/覆盖到文章库。"""
    _ensure_session(db, session_id)
    try:
        article = article_service.publish_article(
            db,
            session_id=session_id,
            artifact_key=artifact_key,
            mode=req.mode.value,
            target_article_id=req.target_article_id,
        )
    except article_service.SourceArtifactNotFound as e:
        raise HTTPException(404, str(e))
    except article_service.ArticleNotFound as e:
        raise HTTPException(404, str(e))
    except article_service.InvalidPublishTarget as e:
        raise HTTPException(400, str(e))
    except article_service.ArticleError as e:
        raise HTTPException(400, str(e))

    return article_service._article_to_detail_dict(article)


@publish_router.get("/publications", response_model=list[ArticleItem])
def list_publications(
    session_id: str,
    artifact_key: str,
    db: Session = Depends(get_db),
):
    """列出该 artifact 当前已发布过的所有文章。

    用于前端 PublishDialog：
    - 返回空列表 → 首次发布，直接走 append
    - 返回非空 → 展示列表，让用户选"追加新文章"或"覆盖某一条"
    """
    _ensure_session(db, session_id)
    return article_service.list_publications_for_artifact(db, session_id, artifact_key)
