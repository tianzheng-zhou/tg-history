"""Artifact service：管理会话内"活文档"的 CRUD + str_replace 编辑 + 版本历史。

设计要点：
- artifact_key 是 session 内 unique 的 slug，由 Agent 自定（如 "tech-summary"）
- 每次 create / update / rewrite 都新增一行 ArtifactVersion，正文全量保留（不存 diff）
- update 走 str_replace 协议：old_str 必须在当前最新版本中**唯一**出现一次，否则抛 StrReplaceError
- rewrite 整体替换最新版本内容
- 所有写操作返回 (Artifact, ArtifactVersion) 元组，方便上层生成事件 payload
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from backend.models.database import Artifact, ArtifactVersion


# ---------- 异常 ----------

class ArtifactError(Exception):
    """Artifact 操作的通用异常基类。"""


class ArtifactKeyConflict(ArtifactError):
    """artifact_key 在该 session 内已存在。"""


class ArtifactNotFound(ArtifactError):
    """指定的 artifact 不存在。"""


class StrReplaceError(ArtifactError):
    """update_artifact 的 old_str 在内容中匹配数 != 1。

    属性：
        match_count: 实际匹配数（0 表示未找到，>=2 表示不唯一）
        old_str_preview: 用户提交的 old_str 截断预览
        nearby_snippets: 当 match_count == 0 时，给出当前内容里"看起来相似"的片段
                          帮助 Agent 判断是否拼写/换行/缩进错误
    """

    def __init__(self, match_count: int, old_str: str, nearby_snippets: list[str] | None = None):
        self.match_count = match_count
        self.old_str_preview = old_str[:200] + ("..." if len(old_str) > 200 else "")
        self.nearby_snippets = nearby_snippets or []
        msg = f"old_str matched {match_count} times (expected exactly 1)"
        super().__init__(msg)


# ---------- 工具函数 ----------

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")


def _validate_key(artifact_key: str) -> None:
    """artifact_key 必须是 64 字符内的小写 slug。

    Agent 偶尔会用中文/空格/大写 → 提前拒绝，强迫它换名。
    """
    if not artifact_key or not _SLUG_RE.match(artifact_key):
        raise ArtifactError(
            f"artifact_key 必须是 1~64 字符的小写 slug（字母/数字/下划线/短横线，"
            f"开头不得是符号），收到：{artifact_key!r}"
        )


def _dump_op_meta(meta: dict | None) -> str | None:
    if meta is None:
        return None
    return json.dumps(meta, ensure_ascii=False)


def _parse_op_meta(s: str | None) -> dict | None:
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def _preview(s: str, max_len: int = 200) -> str:
    s = s or ""
    if len(s) <= max_len:
        return s
    return s[:max_len] + "..."


def _find_nearby_snippets(content: str, old_str: str, max_snippets: int = 3) -> list[str]:
    """当 old_str 0 命中时，截取若干"形似"片段帮助 Agent 修正。

    粗暴策略：取 old_str 第一行（去首尾空格），用它做子串匹配；
    每个命中位置取 ±60 字符的片段。
    """
    first_line = old_str.strip().split("\n", 1)[0].strip()
    if not first_line or len(first_line) < 4:
        return []
    snippets: list[str] = []
    seen: set[int] = set()
    start = 0
    while True:
        idx = content.find(first_line, start)
        if idx < 0:
            break
        # 去重：避免同一位置反复加
        if idx in seen:
            break
        seen.add(idx)
        s_from = max(idx - 60, 0)
        s_to = min(idx + len(first_line) + 60, len(content))
        snippet = content[s_from:s_to]
        if s_from > 0:
            snippet = "..." + snippet
        if s_to < len(content):
            snippet = snippet + "..."
        snippets.append(snippet)
        if len(snippets) >= max_snippets:
            break
        start = idx + len(first_line)
    return snippets


# ---------- 查询 ----------

def list_artifacts(db: Session, session_id: str) -> list[Artifact]:
    """列出 session 下所有 artifact，按 updated_at 降序。"""
    return (
        db.query(Artifact)
        .filter(Artifact.session_id == session_id)
        .order_by(desc(Artifact.updated_at))
        .all()
    )


def get_artifact(db: Session, session_id: str, artifact_key: str) -> Artifact | None:
    """按 (session_id, artifact_key) 取出 artifact 行。"""
    return (
        db.query(Artifact)
        .filter(Artifact.session_id == session_id, Artifact.artifact_key == artifact_key)
        .first()
    )


def get_version(
    db: Session, artifact_id: int, version: int | None = None
) -> ArtifactVersion | None:
    """取指定版本；version=None 时取最新版。"""
    q = db.query(ArtifactVersion).filter(ArtifactVersion.artifact_id == artifact_id)
    if version is None:
        return q.order_by(desc(ArtifactVersion.version)).first()
    return q.filter(ArtifactVersion.version == version).first()


def list_versions(db: Session, artifact_id: int) -> list[ArtifactVersion]:
    """列出某 artifact 的所有版本，按 version 升序。"""
    return (
        db.query(ArtifactVersion)
        .filter(ArtifactVersion.artifact_id == artifact_id)
        .order_by(ArtifactVersion.version)
        .all()
    )


# ---------- 写操作 ----------

def create_artifact(
    db: Session,
    session_id: str,
    artifact_key: str,
    title: str,
    content: str,
    *,
    content_type: str = "text/markdown",
    turn_id: int | None = None,
) -> tuple[Artifact, ArtifactVersion]:
    """创建一篇新的 artifact + v1 版本。"""
    _validate_key(artifact_key)
    if not title.strip():
        raise ArtifactError("title 不能为空")

    existing = get_artifact(db, session_id, artifact_key)
    if existing is not None:
        raise ArtifactKeyConflict(
            f"artifact_key '{artifact_key}' 在该 session 内已存在；"
            f"请改用 update_artifact / rewrite_artifact 修改它，或换一个 key 新建"
        )

    now = datetime.now(timezone.utc)
    art = Artifact(
        session_id=session_id,
        artifact_key=artifact_key,
        title=title.strip(),
        content_type=content_type,
        current_version=1,
        chat_id=None,
        created_at=now,
        updated_at=now,
    )
    db.add(art)
    db.flush()  # 拿到 art.id

    ver = ArtifactVersion(
        artifact_id=art.id,
        version=1,
        content=content,
        op="create",
        op_meta=None,
        turn_id=turn_id,
        created_at=now,
    )
    db.add(ver)
    db.commit()
    db.refresh(art)
    db.refresh(ver)
    return art, ver


def update_artifact(
    db: Session,
    session_id: str,
    artifact_key: str,
    old_str: str,
    new_str: str,
    *,
    turn_id: int | None = None,
) -> tuple[Artifact, ArtifactVersion]:
    """str_replace 风格的增量编辑。old_str 必须在当前最新版本中唯一出现。

    成功后 bump current_version，新增一行 ArtifactVersion。
    """
    if not old_str:
        raise ArtifactError("old_str 不能为空字符串")

    art = get_artifact(db, session_id, artifact_key)
    if art is None:
        raise ArtifactNotFound(f"artifact '{artifact_key}' 不存在")

    latest = get_version(db, art.id)
    if latest is None:
        # 数据不一致：有 Artifact 行但没有任何版本。视为 not found 让 Agent 重建
        raise ArtifactNotFound(f"artifact '{artifact_key}' 没有任何版本，无法 update")

    cur = latest.content
    match_count = cur.count(old_str)
    if match_count != 1:
        nearby = _find_nearby_snippets(cur, old_str) if match_count == 0 else []
        raise StrReplaceError(match_count, old_str, nearby)

    new_content = cur.replace(old_str, new_str, 1)
    next_version = (art.current_version or 0) + 1
    now = datetime.now(timezone.utc)

    ver = ArtifactVersion(
        artifact_id=art.id,
        version=next_version,
        content=new_content,
        op="update",
        op_meta=_dump_op_meta({
            "old_str_preview": _preview(old_str),
            "new_str_preview": _preview(new_str),
        }),
        turn_id=turn_id,
        created_at=now,
    )
    db.add(ver)

    art.current_version = next_version
    art.updated_at = now

    db.commit()
    db.refresh(art)
    db.refresh(ver)
    return art, ver


def rewrite_artifact(
    db: Session,
    session_id: str,
    artifact_key: str,
    content: str,
    *,
    title: str | None = None,
    turn_id: int | None = None,
) -> tuple[Artifact, ArtifactVersion]:
    """整体重写：保留旧版本、新增一行 version。可选同步改 title。"""
    art = get_artifact(db, session_id, artifact_key)
    if art is None:
        raise ArtifactNotFound(f"artifact '{artifact_key}' 不存在")

    latest = get_version(db, art.id)
    prev_length = len(latest.content) if latest else 0

    next_version = (art.current_version or 0) + 1
    now = datetime.now(timezone.utc)

    ver = ArtifactVersion(
        artifact_id=art.id,
        version=next_version,
        content=content,
        op="rewrite",
        op_meta=_dump_op_meta({
            "prev_length": prev_length,
            "new_length": len(content),
        }),
        turn_id=turn_id,
        created_at=now,
    )
    db.add(ver)

    art.current_version = next_version
    if title and title.strip():
        art.title = title.strip()
    art.updated_at = now

    db.commit()
    db.refresh(art)
    db.refresh(ver)
    return art, ver


def delete_artifact(db: Session, session_id: str, artifact_key: str) -> bool:
    """删除 artifact + 全部版本。返回是否实际删除（false 表示不存在）。"""
    art = get_artifact(db, session_id, artifact_key)
    if art is None:
        return False
    db.query(ArtifactVersion).filter(ArtifactVersion.artifact_id == art.id).delete(
        synchronize_session=False
    )
    db.delete(art)
    db.commit()
    return True


# ---------- 序列化 ----------

def artifact_to_summary_dict(art: Artifact) -> dict:
    """转成 ArtifactSummary 兼容的 dict。"""
    return {
        "id": art.id,
        "session_id": art.session_id,
        "artifact_key": art.artifact_key,
        "title": art.title,
        "content_type": art.content_type or "text/markdown",
        "current_version": art.current_version or 1,
        "created_at": art.created_at or datetime.now(timezone.utc),
        "updated_at": art.updated_at or datetime.now(timezone.utc),
    }


def artifact_to_detail_dict(art: Artifact, ver: ArtifactVersion) -> dict:
    """转成 ArtifactDetail 兼容的 dict（带正文）。"""
    return {
        **artifact_to_summary_dict(art),
        "content": ver.content,
        "version": ver.version,
    }


def version_to_item_dict(ver: ArtifactVersion) -> dict:
    """转成 ArtifactVersionItem 兼容的 dict（不含正文）。"""
    return {
        "version": ver.version,
        "op": ver.op,
        "op_meta": _parse_op_meta(ver.op_meta),
        "turn_id": ver.turn_id,
        "created_at": ver.created_at or datetime.now(timezone.utc),
    }
