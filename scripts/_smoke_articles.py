"""Article service smoke test。run: venv\\Scripts\\python scripts/_smoke_articles.py"""

import sys
import time
from pathlib import Path

# 允许从项目根直接运行：把仓库根加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.models.database import PublishedArticle, SessionLocal, init_db  # noqa: E402
from backend.services import (  # noqa: E402
    article_service,
    artifact_service,
    session_service,
)


def main():
    init_db()
    db = SessionLocal()
    try:
        # ---------- 准备：session + 三版 artifact ----------
        s = session_service.create_session(db, title="文章库联调", mode="agent")
        art, ver1 = artifact_service.create_artifact(
            db, s.id, "tech-summary", "技术方案摘要", "# V1\nfirst version"
        )
        print(f"[create] artifact id={art.id} ver1.created_at={ver1.created_at}")
        time.sleep(0.05)
        art, ver2 = artifact_service.update_artifact(
            db, s.id, "tech-summary", "first version", "second version"
        )
        print(f"[update] v2.created_at={ver2.created_at}")
        time.sleep(0.05)
        art, ver3 = artifact_service.rewrite_artifact(
            db, s.id, "tech-summary", "# V3\nbrand new", title="更新后的标题"
        )
        print(
            f"[rewrite] v3.created_at={ver3.created_at}, "
            f"art.current_version={art.current_version}"
        )

        # ---------- 1. append #1 ----------
        a1 = article_service.publish_article(db, s.id, "tech-summary", "append")
        print(
            f"[publish append #1] id={a1.id[:8]} "
            f"content_created_at={a1.content_created_at} "
            f"== v3? {a1.content_created_at == ver3.created_at}"
        )
        assert a1.content_created_at == ver3.created_at, \
            "生成时间应等于源 version 的 created_at"
        assert a1.title == "更新后的标题"
        assert "brand new" in a1.content
        assert a1.source_version_number == 3

        # ---------- 2. 继续 update 原 artifact，a1 应冻结 ----------
        art, ver4 = artifact_service.update_artifact(
            db, s.id, "tech-summary", "brand new", "v4 content"
        )
        db.refresh(a1)
        assert "brand new" in a1.content, "a1 应被冻结"
        assert "v4 content" not in a1.content

        # ---------- 3. append #2 → 新增一条快照 v4 ----------
        a2 = article_service.publish_article(db, s.id, "tech-summary", "append")
        print(
            f"[publish append #2] id={a2.id[:8]} "
            f"source_version={a2.source_version_number}"
        )
        assert a2.source_version_number == 4
        assert a2.content_created_at == ver4.created_at
        assert a1.id != a2.id

        # ---------- 4. rewrite v5 + overwrite a1 ----------
        time.sleep(0.05)
        art, ver5 = artifact_service.rewrite_artifact(
            db, s.id, "tech-summary", "# V5 final", title="V5 title"
        )
        original_a1_published_at = a1.published_at
        a1_again = article_service.publish_article(
            db, s.id, "tech-summary", "overwrite", target_article_id=a1.id
        )
        print(
            f"[overwrite a1] title={a1_again.title!r} "
            f"source_version={a1_again.source_version_number}"
        )
        print(
            f"  published_at unchanged? "
            f"{a1_again.published_at == original_a1_published_at}"
        )
        assert a1_again.id == a1.id, "overwrite 应当更新同一条"
        assert a1_again.source_version_number == 5
        assert "V5 final" in a1_again.content
        assert a1_again.published_at == original_a1_published_at, \
            "published_at 应保持不变"

        # ---------- 5. list_drafts ----------
        drafts = article_service.list_drafts(db)
        print(
            f"[list_drafts] 共 {len(drafts)} 条；"
            f"第一条 publication_count={drafts[0]['publication_count']}"
        )
        assert drafts[0]["publication_count"] == 2
        assert drafts[0]["current_version"] == 5
        assert drafts[0]["session_title"] == "文章库联调"

        # ---------- 6. list_articles ----------
        articles = article_service.list_articles(db)
        print(f"[list_articles] 共 {len(articles)} 条")
        assert len(articles) == 2

        # ---------- 7. list_publications_for_artifact ----------
        pubs = article_service.list_publications_for_artifact(
            db, s.id, "tech-summary"
        )
        print(f"[publications for artifact] 共 {len(pubs)} 条")
        assert len(pubs) == 2

        # ---------- 8. 删源 artifact，文章仍在 ----------
        artifact_service.delete_artifact(db, s.id, "tech-summary")
        db.expire_all()
        a1_after = (
            db.query(PublishedArticle)
            .filter(PublishedArticle.id == a1.id)
            .first()
        )
        print(
            f"[after delete artifact] a1 仍存在={a1_after is not None}, "
            f"source_artifact_id={a1_after.source_artifact_id}, "
            f"source_artifact_key={a1_after.source_artifact_key}"
        )
        assert a1_after is not None
        assert a1_after.source_artifact_id is None, "应 ON DELETE SET NULL"
        assert a1_after.source_artifact_key == "tech-summary"

        # ---------- 9. 删源 session，文章仍在 ----------
        session_title_before = a1_after.source_session_title
        session_service.delete_session(db, s.id)
        db.expire_all()
        a1_after2 = (
            db.query(PublishedArticle)
            .filter(PublishedArticle.id == a1.id)
            .first()
        )
        print(
            f"[after delete session] source_session_id="
            f"{a1_after2.source_session_id}, "
            f"source_session_title={a1_after2.source_session_title!r}"
        )
        assert a1_after2.source_session_id is None
        assert a1_after2.source_session_title == session_title_before

        # ---------- 10. list_articles source_exists ----------
        articles_after = article_service.list_articles(db)
        print(f"[list_articles after delete] source_exists={articles_after[0]['source_exists']}")
        assert articles_after[0]["source_exists"] is False

        # ---------- 11. delete article ----------
        ok = article_service.delete_article(db, a1.id)
        ok2 = article_service.delete_article(db, a2.id)
        print(f"[delete articles] {ok}, {ok2}")
        assert ok and ok2
        remaining = db.query(PublishedArticle).count()
        assert remaining == 0

        print("\n[OK] all smoke tests passed.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
