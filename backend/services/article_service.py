"""Article service：管理"文章库"冻结快照的 publish / 列表 / 删除。

设计要点：
- Publish 产生 PublishedArticle 冻结副本，源 artifact 继续在 session 演进，互不干扰
- mode="append"：创建新 Article；mode="overwrite"：更新指定 Article（target_article_id 必填）
- 覆盖时不改 published_at（保留"首次发布时间"），updated_at 由 onupdate 自动变
- content_created_at = 源 ArtifactVersion.created_at（UI 主展示"生成于..."时间）
- 源 artifact / session 被删后 Article 仍保留：source_* FK 是 SET NULL，另冗余保存
  source_session_title / source_artifact_key 防止归属信息丢失
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from backend.models.database import (
    Artifact,
    ArtifactVersion,
    ChatSession,
    PublishedArticle,
)


# ---------- 异常 ----------

class ArticleError(Exception):
    """Article 操作的通用异常基类。"""


class ArticleNotFound(ArticleError):
    """指定的 article 不存在。"""


class SourceArtifactNotFound(ArticleError):
    """要发布的源 artifact 不存在。"""


class InvalidPublishTarget(ArticleError):
    """overwrite 模式下 target_article_id 缺失或不合法。"""


# ---------- 工具函数 ----------

def _preview(s: str, max_len: int = 200) -> str:
    s = s or ""
    if len(s) <= max_len:
        return s
    return s[:max_len] + "..."


# ---------- 查询 ----------

def list_articles(db: Session) -> list[dict]:
    """跨所有 session 列出已发布文章，按 content_created_at 倒序。"""
    rows = (
        db.query(PublishedArticle)
        .order_by(desc(PublishedArticle.content_created_at))
        .all()
    )
    return [_article_to_item_dict(a) for a in rows]


def get_article(db: Session, article_id: str) -> dict | None:
    """取单篇文章（含正文）。"""
    article = (
        db.query(PublishedArticle)
        .filter(PublishedArticle.id == article_id)
        .first()
    )
    if article is None:
        return None
    return _article_to_detail_dict(article)


def list_publications_for_artifact(
    db: Session, session_id: str, artifact_key: str
) -> list[dict]:
    """列出该 artifact 当前已发布过的所有文章，用于 PublishDialog 决定展示形态。

    按 published_at 倒序（最近发布的在前）。artifact 不存在时返回空列表。
    """
    art = (
        db.query(Artifact)
        .filter(
            Artifact.session_id == session_id,
            Artifact.artifact_key == artifact_key,
        )
        .first()
    )
    if art is None:
        return []
    rows = (
        db.query(PublishedArticle)
        .filter(PublishedArticle.source_artifact_id == art.id)
        .order_by(desc(PublishedArticle.published_at))
        .all()
    )
    return [_article_to_item_dict(a) for a in rows]


def list_drafts(db: Session) -> list[dict]:
    """跨 session 列出所有 artifact（= 草稿视图），附带每条的 publication_count。

    用于 Articles 页的"草稿" Tab。按 Artifact.updated_at 倒序。
    """
    # 1. JOIN artifacts + chat_sessions 拿 session 标题（即使 session 已删也要 outerjoin）
    rows = (
        db.query(Artifact, ChatSession)
        .outerjoin(ChatSession, Artifact.session_id == ChatSession.id)
        .order_by(desc(Artifact.updated_at))
        .all()
    )
    if not rows:
        return []

    artifact_ids = [art.id for art, _ in rows]

    # 2. 一次性查 publication_count（grouped）
    pub_count_rows = (
        db.query(
            PublishedArticle.source_artifact_id,
            func.count(PublishedArticle.id),
        )
        .filter(PublishedArticle.source_artifact_id.in_(artifact_ids))
        .group_by(PublishedArticle.source_artifact_id)
        .all()
    )
    pub_counts: dict[int, int] = {aid: cnt for aid, cnt in pub_count_rows}

    # 3. 一次性查每条 artifact 的当前版本正文（用于 content_preview + content_length）
    # ArtifactVersion (artifact_id, version) 唯一，JOIN Artifact 过滤到 current_version
    latest_ver_rows = (
        db.query(
            ArtifactVersion.artifact_id,
            ArtifactVersion.content,
        )
        .join(Artifact, Artifact.id == ArtifactVersion.artifact_id)
        .filter(
            ArtifactVersion.artifact_id.in_(artifact_ids),
            ArtifactVersion.version == Artifact.current_version,
        )
        .all()
    )
    contents: dict[int, str] = {aid: (c or "") for aid, c in latest_ver_rows}

    out: list[dict] = []
    for art, sess in rows:
        content = contents.get(art.id, "")
        out.append({
            "id": art.id,
            "session_id": art.session_id,
            "session_title": (sess.title if sess else None) or "（未命名会话）",
            "artifact_key": art.artifact_key,
            "title": art.title,
            "current_version": art.current_version or 1,
            "content_type": art.content_type or "text/markdown",
            "content_length": len(content),
            "content_preview": _preview(content),
            "publication_count": pub_counts.get(art.id, 0),
            "created_at": art.created_at or datetime.utcnow(),
            "updated_at": art.updated_at or datetime.utcnow(),
        })
    return out


# ---------- 写操作 ----------

def publish_article(
    db: Session,
    session_id: str,
    artifact_key: str,
    mode: str = "append",
    target_article_id: str | None = None,
) -> PublishedArticle:
    """Publish 源 artifact 当前版本为文章库的一条 Article。

    - mode="append": 新建一条 Article（追加）
    - mode="overwrite": 覆盖 target_article_id 指定的 Article 内容 + 源追溯信息，
      published_at 不变（保留首次发布时间），updated_at 自动更新
    """
    if mode not in ("append", "overwrite"):
        raise ArticleError(f"未知 publish mode: {mode!r}")

    # 1. 找源 artifact
    art = (
        db.query(Artifact)
        .filter(
            Artifact.session_id == session_id,
            Artifact.artifact_key == artifact_key,
        )
        .first()
    )
    if art is None:
        raise SourceArtifactNotFound(
            f"artifact '{artifact_key}' 不存在于 session {session_id}"
        )

    # 2. 找当前版本（取 current_version 对应的那行）
    cur_ver = (
        db.query(ArtifactVersion)
        .filter(
            ArtifactVersion.artifact_id == art.id,
            ArtifactVersion.version == (art.current_version or 1),
        )
        .first()
    )
    if cur_ver is None:
        raise SourceArtifactNotFound(
            f"artifact '{artifact_key}' 数据不一致：找不到 version {art.current_version}"
        )

    # 3. 查 session 标题（冗余存入 Article，防源 session 日后被删）
    session_row = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    session_title = (session_row.title if session_row else None) or "（未命名会话）"

    now = datetime.utcnow()
    # 源版本的生成时间，用作 UI 主展示
    content_created_at = cur_ver.created_at or now

    if mode == "append":
        article = PublishedArticle(
            id=uuid.uuid4().hex,
            source_artifact_id=art.id,
            source_session_id=session_id,
            source_session_title=session_title,
            source_artifact_key=artifact_key,
            source_version_number=cur_ver.version,
            title=art.title,
            content=cur_ver.content,
            content_type=art.content_type or "text/markdown",
            content_created_at=content_created_at,
            published_at=now,
            updated_at=now,
        )
        db.add(article)
        db.commit()
        db.refresh(article)
        return article

    # mode == "overwrite"
    if not target_article_id:
        raise InvalidPublishTarget("mode=overwrite 需要提供 target_article_id")
    article = (
        db.query(PublishedArticle)
        .filter(PublishedArticle.id == target_article_id)
        .first()
    )
    if article is None:
        raise ArticleNotFound(f"目标文章 {target_article_id} 不存在")

    # 覆盖内容快照 + 刷新源追溯（源 artifact 可能已经更新了 title / key 不变但版本号变）
    article.source_artifact_id = art.id
    article.source_session_id = session_id
    article.source_session_title = session_title
    article.source_artifact_key = artifact_key
    article.source_version_number = cur_ver.version
    article.title = art.title
    article.content = cur_ver.content
    article.content_type = art.content_type or "text/markdown"
    article.content_created_at = content_created_at
    # published_at 保持不变（这是"首次发布时间"的语义）
    # updated_at 由 SQLAlchemy onupdate=datetime.utcnow 自动更新
    db.commit()
    db.refresh(article)
    return article


def delete_article(db: Session, article_id: str) -> bool:
    """从文章库撤回一篇文章（与源 artifact 无关，源不受影响）。

    返回是否实际删除（false 表示文章本来就不存在）。
    """
    article = (
        db.query(PublishedArticle)
        .filter(PublishedArticle.id == article_id)
        .first()
    )
    if article is None:
        return False
    db.delete(article)
    db.commit()
    return True


# ---------- 序列化 ----------

def _article_to_item_dict(a: PublishedArticle) -> dict:
    """转成 ArticleItem 兼容的 dict（不含正文）。"""
    return {
        "id": a.id,
        "title": a.title,
        "content_type": a.content_type or "text/markdown",
        "source_artifact_id": a.source_artifact_id,
        "source_session_id": a.source_session_id,
        "source_session_title": a.source_session_title,
        "source_artifact_key": a.source_artifact_key,
        "source_version_number": a.source_version_number,
        "source_exists": a.source_artifact_id is not None,
        "content_preview": _preview(a.content or ""),
        "content_length": len(a.content or ""),
        "content_created_at": a.content_created_at,
        "published_at": a.published_at,
        "updated_at": a.updated_at,
    }


def _article_to_detail_dict(a: PublishedArticle) -> dict:
    """转成 ArticleDetail 兼容的 dict（含正文）。"""
    return {
        **_article_to_item_dict(a),
        "content": a.content or "",
    }
