"""HTTP-level smoke test for articles endpoints. run from project root."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from backend.main import app  # noqa: E402
from backend.models.database import SessionLocal, init_db  # noqa: E402
from backend.services import artifact_service, session_service  # noqa: E402


def main():
    init_db()
    client = TestClient(app)

    # 准备 session + artifact
    db = SessionLocal()
    try:
        sess = session_service.create_session(
            db, title="HTTP 联调测试", mode="agent"
        )
        sess_id = sess.id  # 在 db 关闭前捕获，避免 DetachedInstance
        artifact_service.create_artifact(
            db, sess_id, "demo", "演示报告", "# Hello\n\n第一版内容"
        )
    finally:
        db.close()

    # 1. drafts: 应有 1 条，publication_count=0
    r = client.get("/api/articles/drafts")
    r.raise_for_status()
    drafts = r.json()
    print(f"[GET /drafts] 共 {len(drafts)} 条，publication_count={drafts[0]['publication_count']}")
    target_draft = next(
        (d for d in drafts if d["session_id"] == sess_id), None
    )
    assert target_draft is not None, "drafts 缺失刚刚创建的 artifact"
    assert target_draft["publication_count"] == 0
    assert target_draft["session_title"] == "HTTP 联调测试"

    # 2. publications: 空
    r = client.get(f"/api/sessions/{sess_id}/artifacts/demo/publications")
    r.raise_for_status()
    assert r.json() == [], f"应当为空，实际 {r.json()}"
    print("[GET /publications] 空列表 ✓")

    # 3. publish (append)
    r = client.post(
        f"/api/sessions/{sess_id}/artifacts/demo/publish",
        json={"mode": "append"},
    )
    r.raise_for_status()
    article1 = r.json()
    print(f"[POST /publish append] id={article1['id'][:8]} title={article1['title']!r}")
    assert article1["title"] == "演示报告"
    assert article1["source_version_number"] == 1
    assert "Hello" in article1["content"]
    assert article1["source_exists"] is True

    # 4. articles 列表: 1 条
    r = client.get("/api/articles")
    r.raise_for_status()
    articles = r.json()
    article_in_list = next((a for a in articles if a["id"] == article1["id"]), None)
    assert article_in_list is not None
    print(f"[GET /articles] 共 {len(articles)} 条，目标存在 ✓")

    # 5. 再次 publish (overwrite 第一条)
    r = client.post(
        f"/api/sessions/{sess_id}/artifacts/demo/publish",
        json={"mode": "overwrite", "target_article_id": article1["id"]},
    )
    r.raise_for_status()
    article1_updated = r.json()
    assert article1_updated["id"] == article1["id"], "覆盖应保持 id"
    print(f"[POST /publish overwrite] same id ✓ source_version={article1_updated['source_version_number']}")

    # 6. 缺 target_article_id 的 overwrite 应当 400
    r = client.post(
        f"/api/sessions/{sess_id}/artifacts/demo/publish",
        json={"mode": "overwrite"},
    )
    assert r.status_code == 400, f"期望 400，实际 {r.status_code}"
    print(f"[POST /publish overwrite no target] 400 ✓")

    # 7. 不存在的 article_id overwrite 应当 404
    r = client.post(
        f"/api/sessions/{sess_id}/artifacts/demo/publish",
        json={"mode": "overwrite", "target_article_id": "nonexistent-id"},
    )
    assert r.status_code == 404
    print(f"[POST /publish overwrite invalid target] 404 ✓")

    # 8. publish 不存在的 artifact 应当 404
    r = client.post(
        f"/api/sessions/{sess_id}/artifacts/missing-key/publish",
        json={"mode": "append"},
    )
    assert r.status_code == 404
    print(f"[POST /publish unknown artifact] 404 ✓")

    # 9. 不存在的 session 应当 404
    r = client.post(
        "/api/sessions/nonexistent-sid/artifacts/demo/publish",
        json={"mode": "append"},
    )
    assert r.status_code == 404
    print(f"[POST /publish unknown session] 404 ✓")

    # 10. GET /articles/{id}
    r = client.get(f"/api/articles/{article1['id']}")
    r.raise_for_status()
    detail = r.json()
    assert "content" in detail
    print(f"[GET /articles/{{id}}] 详情含正文 ✓")

    # 11. 导出
    r = client.get(f"/api/articles/{article1['id']}/export")
    r.raise_for_status()
    assert "演示报告" in r.text
    assert "demo-v" in r.headers.get("content-disposition", "")
    print(f"[GET /articles/{{id}}/export] 导出 .md ✓")

    # 12. DELETE
    r = client.delete(f"/api/articles/{article1['id']}")
    r.raise_for_status()
    print(f"[DELETE /articles/{{id}}] {r.json()}")

    # 13. 重新 list 不应再有这一条
    r = client.get("/api/articles")
    r.raise_for_status()
    remaining = [a for a in r.json() if a["id"] == article1["id"]]
    assert remaining == []
    print(f"[GET /articles 删除后] 已撤回 ✓")

    # 清理
    db = SessionLocal()
    try:
        session_service.delete_session(db, sess_id)
    finally:
        db.close()

    print("\n[OK] HTTP 联调全部通过。")


if __name__ == "__main__":
    main()
