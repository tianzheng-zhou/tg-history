"""按需查询 Telegram 用户主页 service。

设计要点：
  1. **不全量同步**：仅当 agent 调用 ``tool_get_user_profile`` 时才拉一次
  2. **24h 缓存**：写入 ``TgUserProfileCache`` 表；命中缓存直接返回
  3. **错误用 code 字段标记**：让 agent 能区分 no_login / not_found / privacy_restricted / rate_limited
  4. **限流保护**：进程级 Semaphore + 强制最小间隔，防止 agent 一次性查 50 个触发 FloodWait

工具调用流程：
    sender_id="user6747261966"
    → 解析 → tg_user_id=6747261966
    → cache hit?  → 返回缓存
    → cache miss? → client.get_entity(...) + GetFullUserRequest(...) → 写缓存 → 返回
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session
from telethon.errors import (
    FloodWaitError,
    UserDeactivatedError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import User as TLUser

from backend.models.database import TelegramAccount, TgUserProfileCache
from backend.services import telegram_sync

logger = logging.getLogger(__name__)

# 24 小时 TTL：bio 不会变化太频繁，且按需调用频率低
CACHE_TTL = timedelta(hours=24)

# 限流：进程级最大并发 1（GetFullUser 是敏感 API，串行最稳）+ 最小间隔 1.0s
_concurrency = asyncio.Semaphore(1)
_last_call_ts = 0.0
_MIN_INTERVAL_SEC = 1.0

# FloodWait 上限：等待超过这个秒数就放弃，告诉 agent rate_limited
_FLOOD_WAIT_MAX = 30


def _normalize_sender_id(sender_id: str) -> int | None:
    """从 'user6747261966' / 'user 6747261966' / '6747261966' 提取数字 tg_user_id。

    频道（'channel...'）和群组（'chat...'）不支持（GetFullUser 只用于 User）。
    """
    if not sender_id:
        return None
    s = sender_id.strip()
    m = re.fullmatch(r"user\s*(\d+)", s, re.IGNORECASE)
    if m:
        return int(m.group(1))
    if s.isdigit():
        return int(s)
    return None


def _normalize_username(username: str) -> str:
    """去掉前导 @ 和 t.me/ 前缀。"""
    s = username.strip()
    if s.startswith("@"):
        s = s[1:]
    if "t.me/" in s:
        s = s.split("t.me/", 1)[1].split("/", 1)[0]
    return s


def _row_to_dict(row: TgUserProfileCache, *, cached: bool) -> dict:
    """ORM 行 → API 响应。展平 payload 兜底字段。"""
    extra: dict = {}
    if row.payload:
        try:
            extra = json.loads(row.payload)
        except Exception:
            extra = {}
    return {
        "ok": True,
        "sender_id": row.sender_id,
        "tg_user_id": row.tg_user_id,
        "display_name": row.display_name,
        "username": (f"@{row.username}" if row.username else None),
        "bio": row.bio,
        "is_bot": bool(row.is_bot),
        "is_premium": bool(row.is_premium),
        "common_chats_count": row.common_chats_count or 0,
        "phone": row.phone,
        "deleted": bool(row.deleted),
        "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
        "cached": cached,
        **{k: v for k, v in extra.items() if k not in {
            "sender_id", "tg_user_id", "display_name", "username", "bio",
            "is_bot", "is_premium", "common_chats_count", "phone", "deleted",
            "fetched_at", "cached", "ok",
        }},
    }


def _get_cached(db: Session, sender_id: str | None, username: str | None) -> TgUserProfileCache | None:
    """按 sender_id 或 username 查缓存；返回最新一行或 None。"""
    q = db.query(TgUserProfileCache)
    if sender_id:
        return q.filter(TgUserProfileCache.sender_id == sender_id).first()
    if username:
        return (
            q.filter(TgUserProfileCache.username == username)
             .order_by(TgUserProfileCache.fetched_at.desc())
             .first()
        )
    return None


def _is_fresh(row: TgUserProfileCache) -> bool:
    if not row or not row.fetched_at:
        return False
    return datetime.utcnow() - row.fetched_at < CACHE_TTL


def _save_to_cache(db: Session, sender_id: str, full_user_resp: Any) -> TgUserProfileCache:
    """把 Telethon 的 ``UserFull`` 响应写入缓存。返回新/更新后的行。

    ``full_user_resp`` 是 ``GetFullUserRequest`` 返回的 ``UserFull``：
      - .full_user.about / .common_chats_count / .blocked / .pinned_msg_id ...
      - .users[0] → ``User`` 基本信息
    """
    full_user = full_user_resp.full_user
    base_user: TLUser = full_user_resp.users[0]

    display_name = " ".join(filter(None, [
        getattr(base_user, "first_name", None),
        getattr(base_user, "last_name", None),
    ])).strip() or None

    payload = {
        # 兜底：未来想读的字段先存下来，免得改 schema
        "first_name": getattr(base_user, "first_name", None),
        "last_name": getattr(base_user, "last_name", None),
        "lang_code": getattr(base_user, "lang_code", None),
        "verified": getattr(base_user, "verified", False),
        "scam": getattr(base_user, "scam", False),
        "fake": getattr(base_user, "fake", False),
        "restricted": getattr(base_user, "restricted", False),
        "blocked": getattr(full_user, "blocked", False),
        "pinned_msg_id": getattr(full_user, "pinned_msg_id", None),
    }

    row = db.query(TgUserProfileCache).filter(
        TgUserProfileCache.sender_id == sender_id
    ).first()
    if row is None:
        row = TgUserProfileCache(sender_id=sender_id)
        db.add(row)

    row.tg_user_id = base_user.id
    row.display_name = display_name
    row.username = getattr(base_user, "username", None)
    row.bio = getattr(full_user, "about", None)
    row.is_bot = bool(getattr(base_user, "bot", False))
    row.is_premium = bool(getattr(base_user, "premium", False))
    row.common_chats_count = getattr(full_user, "common_chats_count", 0) or 0
    row.phone = getattr(base_user, "phone", None)
    row.deleted = bool(getattr(base_user, "deleted", False))
    row.payload = json.dumps(payload, ensure_ascii=False)
    row.fetched_at = datetime.utcnow()

    db.commit()
    db.refresh(row)
    return row


async def _get_authorized_client(db: Session):
    """拿到已登录的 TelegramClient。未登录返回 None（让上层翻译成 no_login 错误）。"""
    acc = db.query(TelegramAccount).order_by(TelegramAccount.id.asc()).first()
    if acc is None:
        return None
    client = await telegram_sync.get_client(acc.api_id, acc.api_hash)
    if not client.is_connected():
        try:
            await client.connect()
        except Exception:
            logger.warning("Telegram client connect 失败", exc_info=True)
            return None
    try:
        if not await client.is_user_authorized():
            return None
    except Exception:
        logger.warning("is_user_authorized 检查失败", exc_info=True)
        return None
    return client


async def _throttle():
    """全局最小间隔限速。"""
    global _last_call_ts
    now = time.monotonic()
    elapsed = now - _last_call_ts
    if elapsed < _MIN_INTERVAL_SEC:
        await asyncio.sleep(_MIN_INTERVAL_SEC - elapsed)
    _last_call_ts = time.monotonic()


async def fetch_user_profile(
    db: Session,
    *,
    sender_id: str | None = None,
    username: str | None = None,
    use_cache: bool = True,
) -> dict:
    """获取用户主页（含 bio）。返回字典，错误时含 ``error`` 和 ``code``。

    入参二选一：
        sender_id: 'user6747261966' 或仅数字 '6747261966'
        username: 'cwoiuhwooiv'（可带 '@'）

    错误 code:
        - bad_args: 两个参数都没传
        - no_login: 没登录 Telegram
        - not_found: 用户不存在 / 已注销
        - privacy_restricted: 隐私设置或非好友
        - rate_limited: FloodWait 超过 30 秒
    """
    if not sender_id and not username:
        return {
            "error": "需要提供 sender_id 或 username 中的至少一个",
            "code": "bad_args",
        }

    # 标准化输入
    tg_user_id: int | None = None
    norm_sender_id: str | None = None
    norm_username: str | None = None

    if sender_id:
        tg_user_id = _normalize_sender_id(sender_id)
        if tg_user_id is None:
            return {
                "error": (
                    f"sender_id '{sender_id}' 无法解析。预期格式 'user6747261966' "
                    "或纯数字。注意：频道（channel...）和群组（chat...）不支持本工具"
                ),
                "code": "bad_args",
            }
        norm_sender_id = f"user{tg_user_id}"

    if username:
        norm_username = _normalize_username(username)
        if not norm_username:
            return {"error": "username 为空", "code": "bad_args"}

    # 1. 先查缓存
    if use_cache:
        cached = _get_cached(db, norm_sender_id, norm_username)
        if cached and _is_fresh(cached):
            return _row_to_dict(cached, cached=True)

    # 2. 缓存 miss / 过期 / 强制刷新 → 走 Telegram API（限流保护）
    async with _concurrency:
        await _throttle()

        client = await _get_authorized_client(db)
        if client is None:
            # 没登录时如果有过期缓存，仍返回（标记 cached=true + stale 提示）
            stale = _get_cached(db, norm_sender_id, norm_username)
            if stale:
                resp = _row_to_dict(stale, cached=True)
                resp["stale"] = True
                resp["warning"] = "Telegram 未登录，返回的是过期缓存"
                return resp
            return {
                "error": "Telegram 未登录或会话失效；请到设置里登录后重试",
                "code": "no_login",
            }

        # 拿 entity
        try:
            target = tg_user_id if tg_user_id is not None else norm_username
            entity = await client.get_entity(target)
        except (UsernameInvalidError, UsernameNotOccupiedError, UserDeactivatedError) as e:
            return {
                "error": f"用户不存在或已注销: {e}",
                "code": "not_found",
            }
        except FloodWaitError as e:
            wait = int(getattr(e, "seconds", 0) or 0)
            if wait > _FLOOD_WAIT_MAX:
                return {
                    "error": f"被 Telegram 限流 {wait}s，超过 {_FLOOD_WAIT_MAX}s 上限",
                    "code": "rate_limited",
                    "retry_after_sec": wait,
                }
            await asyncio.sleep(wait + 1)
            try:
                entity = await client.get_entity(target)
            except Exception as e2:
                return {"error": f"重试 get_entity 失败: {e2}", "code": "fetch_failed"}
        except ValueError as e:
            return {
                "error": f"无法解析用户标识符: {e}",
                "code": "not_found",
            }
        except Exception as e:
            logger.exception("get_entity 失败")
            return {"error": f"get_entity 失败: {e}", "code": "fetch_failed"}

        if not isinstance(entity, TLUser):
            return {
                "error": f"目标不是用户（type={type(entity).__name__}）。本工具仅支持 User",
                "code": "not_a_user",
            }

        # 拿 FullUser（含 bio）
        try:
            full = await client(GetFullUserRequest(entity))
        except FloodWaitError as e:
            wait = int(getattr(e, "seconds", 0) or 0)
            if wait > _FLOOD_WAIT_MAX:
                return {
                    "error": f"被 Telegram 限流 {wait}s，超过 {_FLOOD_WAIT_MAX}s 上限",
                    "code": "rate_limited",
                    "retry_after_sec": wait,
                }
            await asyncio.sleep(wait + 1)
            try:
                full = await client(GetFullUserRequest(entity))
            except Exception as e2:
                return {"error": f"重试 GetFullUserRequest 失败: {e2}", "code": "fetch_failed"}
        except Exception as e:
            logger.exception("GetFullUserRequest 失败")
            # bio 拉不到时退化：仍返回基础信息（display_name + username）
            return {
                "error": f"GetFullUserRequest 失败: {e}",
                "code": "privacy_restricted",
                "fallback": {
                    "sender_id": f"user{entity.id}",
                    "tg_user_id": entity.id,
                    "display_name": " ".join(filter(None, [
                        getattr(entity, "first_name", None),
                        getattr(entity, "last_name", None),
                    ])).strip() or None,
                    "username": (
                        f"@{entity.username}" if getattr(entity, "username", None) else None
                    ),
                    "is_bot": bool(getattr(entity, "bot", False)),
                    "is_premium": bool(getattr(entity, "premium", False)),
                },
            }

        sender_id_final = f"user{entity.id}"
        try:
            row = _save_to_cache(db, sender_id_final, full)
        except Exception:
            logger.exception("写缓存失败（不影响返回）")
            db.rollback()
            row = TgUserProfileCache(
                sender_id=sender_id_final,
                tg_user_id=entity.id,
                display_name=" ".join(filter(None, [
                    getattr(entity, "first_name", None),
                    getattr(entity, "last_name", None),
                ])).strip() or None,
                username=getattr(entity, "username", None),
                bio=getattr(full.full_user, "about", None),
                is_bot=bool(getattr(entity, "bot", False)),
                is_premium=bool(getattr(entity, "premium", False)),
                common_chats_count=getattr(full.full_user, "common_chats_count", 0) or 0,
                phone=getattr(entity, "phone", None),
                deleted=bool(getattr(entity, "deleted", False)),
                fetched_at=datetime.utcnow(),
            )
        return _row_to_dict(row, cached=False)
