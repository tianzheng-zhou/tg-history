"""绑定目录递归扫描 + 路径/mtime 去重。

- `validate_folder` 用于前端"校验"按钮：检查路径合法性、统计 result.json 数量
- `find_result_jsons` 递归收集所有 result.json
- `diff_pending` 用一次性 SELECT IN (...) 拿全部已记录文件，对比 mtime 决定是否需要重新解析
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy.orm import Session

from backend.models.database import ImportedFile

logger = logging.getLogger(__name__)

# 单次扫描结果上限：避免误绑根目录把进程卡死
MAX_RESULT_JSONS = 10000

# mtime 比较容差（秒）：跨文件系统/Windows 上 mtime 精度有差异
_MTIME_TOLERANCE = 0.5


def find_result_jsons(folder: str | Path) -> list[Path]:
    """递归扫描 folder 下所有 result.json。

    - 跳过不可读文件 / symlink loop
    - 收集到 > MAX_RESULT_JSONS 时提前返回（防误绑根目录）
    """
    base = Path(folder)
    if not base.exists() or not base.is_dir():
        return []

    found: list[Path] = []
    try:
        for p in base.rglob("result.json"):
            # 不解析 symlink，避免 loop
            try:
                if p.is_symlink():
                    continue
                if not p.is_file():
                    continue
                if not os.access(p, os.R_OK):
                    continue
            except OSError:
                continue
            found.append(p)
            if len(found) >= MAX_RESULT_JSONS:
                logger.warning(
                    "find_result_jsons: hit MAX_RESULT_JSONS=%d under %s",
                    MAX_RESULT_JSONS, folder,
                )
                break
    except OSError as e:
        logger.warning("rglob 出错 (%s): %s", folder, e)

    return sorted(found)


def validate_folder(path: str) -> dict:
    """校验路径是否可用作绑定目录。

    Returns:
        {valid, reason, resolved_path, result_json_count, sample_paths}
    """
    if not path or not path.strip():
        return {
            "valid": False,
            "reason": "路径不能为空",
            "resolved_path": None,
            "result_json_count": 0,
            "sample_paths": [],
        }

    try:
        p = Path(path).expanduser().resolve()
    except (OSError, RuntimeError) as e:
        return {
            "valid": False,
            "reason": f"路径解析失败: {e}",
            "resolved_path": None,
            "result_json_count": 0,
            "sample_paths": [],
        }

    if not p.exists():
        return {
            "valid": False,
            "reason": "目录不存在",
            "resolved_path": str(p),
            "result_json_count": 0,
            "sample_paths": [],
        }
    if not p.is_dir():
        return {
            "valid": False,
            "reason": "路径不是一个目录",
            "resolved_path": str(p),
            "result_json_count": 0,
            "sample_paths": [],
        }
    if not os.access(p, os.R_OK):
        return {
            "valid": False,
            "reason": "目录不可读（权限不足）",
            "resolved_path": str(p),
            "result_json_count": 0,
            "sample_paths": [],
        }

    files = find_result_jsons(p)
    sample_paths = []
    for f in files[:5]:
        try:
            sample_paths.append(str(f.relative_to(p)))
        except ValueError:
            sample_paths.append(str(f))

    return {
        "valid": True,
        "reason": None,
        "resolved_path": str(p),
        "result_json_count": len(files),
        "sample_paths": sample_paths,
    }


def resolve_path(path: str) -> str:
    """规范化用户输入路径（绝对、解析 symlink、去掉 .. 等），失败返回原值。"""
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError):
        return path


def diff_pending(
    db: Session,
    folder_id: int,
    files: list[Path],
) -> tuple[list[dict], int]:
    """对比 ImportedFile 表，返回 (pending, skipped_count)。

    pending: 未记录或 mtime 已变化的文件，结构 [{path, mtime, size, prev_mtime}]
    skipped_count: mtime 未变跳过的文件数（包括上次失败的，因为只要文件没变就不重试）
    """
    if not files:
        return [], 0

    abs_paths = [str(f) for f in files]
    rows = (
        db.query(ImportedFile)
        .filter(ImportedFile.abs_path.in_(abs_paths))
        .all()
    )
    by_path = {r.abs_path: r for r in rows}

    pending: list[dict] = []
    skipped = 0
    for f in files:
        try:
            st = f.stat()
        except OSError as e:
            # 取不到 stat 当作变更，让上层尝试解析以便记录错误
            pending.append({
                "path": str(f),
                "mtime": 0.0,
                "size": 0,
                "prev_mtime": None,
                "stat_error": str(e),
            })
            continue

        prev = by_path.get(str(f))
        if prev is not None and prev.mtime is not None:
            if abs(prev.mtime - st.st_mtime) <= _MTIME_TOLERANCE:
                skipped += 1
                continue

        pending.append({
            "path": str(f),
            "mtime": st.st_mtime,
            "size": st.st_size,
            "prev_mtime": prev.mtime if prev else None,
        })

    return pending, skipped


def upsert_imported_file(
    db: Session,
    *,
    folder_id: int,
    abs_path: str,
    mtime: float,
    size: int,
    chat_count: int,
    status: str,
    error: str | None = None,
) -> None:
    """写入或更新 imported_files 行。调用方负责 commit。"""
    from datetime import datetime

    err = (error[:500] if error else None)
    row = db.query(ImportedFile).filter(ImportedFile.abs_path == abs_path).first()
    if row is None:
        row = ImportedFile(
            folder_id=folder_id,
            abs_path=abs_path,
            mtime=mtime,
            size=size,
            chat_count=chat_count,
            status=status,
            error=err,
            imported_at=datetime.now(),
        )
        db.add(row)
    else:
        row.folder_id = folder_id
        row.mtime = mtime
        row.size = size
        row.chat_count = chat_count
        row.status = status
        row.error = err
        row.imported_at = datetime.now()
