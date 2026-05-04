"""Telegram MTProto 直连同步服务（基于 Telethon）。

核心职责：
    - 维护单例 TelegramClient（绑定到主事件循环）
    - 登录流程：发码 → 校验 → 持久化 session
    - 列出全部对话 + 转换为前端友好结构
    - 按 min_id 增量拉取消息 + 转换为 parser.py 等价格式

设计要点：
    - 客户端由 FastAPI 主循环驱动，所有 async 函数必须在主循环上跑
      （schedule_on_main_loop 已经处理这件事）
    - .session 文件统一放在 data/telegram.session（与 app.db 同目录，已 gitignore）
    - api_hash 与 .session 文件等价于免密码登录凭证 —— 不打日志、不外传
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.custom.dialog import Dialog
from telethon.tl.custom.message import Message as TLMessage
from telethon.tl.types import (
    Channel,
    Chat,
    MessageEntityBold,
    MessageEntityCode,
    MessageEntityEmail,
    MessageEntityHashtag,
    MessageEntityItalic,
    MessageEntityMention,
    MessageEntityMentionName,
    MessageEntityPhone,
    MessageEntityPre,
    MessageEntityStrike,
    MessageEntityTextUrl,
    MessageEntityUnderline,
    MessageEntityUrl,
    PeerChannel,
    PeerChat,
    PeerUser,
    User,
)

from backend.config import settings

logger = logging.getLogger(__name__)


SESSION_NAME = "telegram"   # data/telegram.session


def _session_path() -> str:
    """完整 session 路径（不含 .session 后缀，Telethon 会自动加）"""
    return str(Path(settings.data_dir) / SESSION_NAME)


def session_file() -> Path:
    """实际 .session 文件路径，用于退出登录时清理"""
    return Path(settings.data_dir) / f"{SESSION_NAME}.session"


# ---------- 代理解析 ----------

def _resolve_proxy_url() -> Optional[str]:
    """按优先级返回代理 URL（字符串）：
    1. settings.telegram_proxy（.env 显式配置）
    2. 环境变量 HTTPS_PROXY / ALL_PROXY / HTTP_PROXY
    都没有 → None（直连）
    """
    if settings.telegram_proxy:
        return settings.telegram_proxy.strip()
    for env_key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(env_key)
        if v:
            return v.strip()
    return None


def _parse_proxy(url: str) -> Optional[tuple]:
    """把代理 URL 解析为 Telethon 的 proxy 元组。

    支持格式：
        socks5://host:port
        socks5://user:pass@host:port
        socks4://host:port
        http://host:port            (HTTP CONNECT 隧道，可走 Clash/V2Ray 的 7890 端口)

    返回 (proxy_type_str, host, port[, rdns, username, password])，
    与 ``TelegramClient(proxy=...)`` 兼容。
    """
    try:
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        host = parsed.hostname
        port = parsed.port
        if not host or not port:
            logger.warning("代理 URL 缺少 host 或 port: %s", url)
            return None

        if scheme in ("socks5", "socks5h"):
            proxy_type = "socks5"
        elif scheme == "socks4":
            proxy_type = "socks4"
        elif scheme in ("http", "https"):
            proxy_type = "http"
        else:
            logger.warning("不支持的代理 scheme: %s（仅支持 socks5/socks4/http）", scheme)
            return None

        # rdns=True：让代理解析远端域名，避免本地 DNS 污染
        if parsed.username or parsed.password:
            return (proxy_type, host, port, True, parsed.username or "", parsed.password or "")
        return (proxy_type, host, port, True)
    except Exception as e:
        logger.warning("解析代理 URL 失败 %s: %s", url, e)
        return None


def _get_proxy_tuple() -> Optional[tuple]:
    url = _resolve_proxy_url()
    if not url:
        return None
    parsed = _parse_proxy(url)
    if parsed:
        # 不打印 username/password
        logger.info("Telegram 客户端将使用代理: %s://%s:%s", parsed[0], parsed[1], parsed[2])
    return parsed


def proxy_status() -> dict:
    """供前端展示当前代理状态"""
    url = _resolve_proxy_url()
    if not url:
        return {"enabled": False, "source": None, "scheme": None, "host": None, "port": None}
    source = "settings" if settings.telegram_proxy else "env"
    parsed = _parse_proxy(url)
    if not parsed:
        return {"enabled": False, "source": source, "error": "URL 格式不正确，仅支持 socks5/socks4/http"}
    return {
        "enabled": True,
        "source": source,
        "scheme": parsed[0],
        "host": parsed[1],
        "port": parsed[2],
    }


# ---------- 单例客户端 ----------

_client: Optional[TelegramClient] = None
# 同一进程里 send_code 返回的 phone_code_hash，verify 时需要
_pending_phone_code_hash: Optional[str] = None
_pending_phone: Optional[str] = None

# is_authorized 结果缓存：避免每次 /account 都走一次 Telegram 服务器往返
# key=(api_id, api_hash) → (value: bool, expires_at: float, last_checked: float)
# True 状态缓存 5 分钟（authorized 几乎不变；用户主动 logout/login 时会清空缓存）；
#   设得比前端轮询间隔（2 分钟）长，让大部分前端调用都命中缓存
# False 状态缓存 10 秒（让用户登录后尽快看到状态变化）
_AUTH_CACHE_TTL_OK = 300.0
_AUTH_CACHE_TTL_FAIL = 10.0
_authorized_cache: dict[tuple, tuple[bool, float]] = {}


def _invalidate_auth_cache() -> None:
    _authorized_cache.clear()


async def get_client(api_id: int, api_hash: str) -> TelegramClient:
    """获取/初始化单例 TelegramClient（不做 connect/start）。

    如果已有 client 但 api_id/hash 变了 → 断开旧的、新建。
    """
    global _client
    if _client is not None:
        # 已有 client：如果凭据没变就直接复用
        try:
            if (
                getattr(_client, "_api_id", None) == api_id
                and getattr(_client, "_api_hash", None) == api_hash
            ):
                return _client
        except Exception:
            pass
        # 凭据变了：断开旧的
        try:
            if _client.is_connected():
                await _client.disconnect()
        except Exception:
            logger.warning("旧 TelegramClient 断开失败", exc_info=True)
        _client = None

    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    proxy = _get_proxy_tuple()
    # connect_retries=2 + timeout=20：代理慢/不稳时少等待
    # receive_updates=False：本工具只主动拉历史 + 列 dialogs，**不需要实时 update**。
    #   不关掉的话 connect() 会自动调 GetDifferenceRequest 拉所有积压的离线 update —
    #   长时间不连 Telegram 时这会阻塞 asyncio 主循环 N 分钟，把所有 HTTP API 拖死。
    _client = TelegramClient(
        _session_path(),
        api_id,
        api_hash,
        proxy=proxy,
        connection_retries=2,
        timeout=20,
        receive_updates=False,
    )
    return _client


async def is_authorized(
    api_id: int,
    api_hash: str,
    *,
    connect_timeout: float = 8.0,
    use_cache: bool = True,
) -> bool:
    """检查 session 是否已登录（不会触发交互式登录）。

    ``connect_timeout`` 控制 connect() 阶段的最大等待时间（秒）。代理慢/不通时
    避免无限阻塞 — 超时直接返回 False，调用方会展示「未登录」状态。

    ``use_cache=True`` 时优先返回 module 级缓存（True 60s / False 5s），
    避免前端切换页面时反复触发 Telegram 服务器握手（每次 ~2-3s）。
    """
    import asyncio as _asyncio
    import time as _time

    cache_key = (api_id, api_hash)
    if use_cache:
        cached = _authorized_cache.get(cache_key)
        if cached is not None:
            value, expires_at = cached
            if _time.time() < expires_at:
                return value

    client = await get_client(api_id, api_hash)
    if not client.is_connected():
        try:
            await _asyncio.wait_for(client.connect(), timeout=connect_timeout)
        except _asyncio.TimeoutError:
            logger.warning("Telegram connect 超时 %.1fs（代理慢或不通）", connect_timeout)
            _authorized_cache[cache_key] = (False, _time.time() + _AUTH_CACHE_TTL_FAIL)
            return False
        except Exception as e:
            logger.warning("Telegram connect 失败: %s", e)
            _authorized_cache[cache_key] = (False, _time.time() + _AUTH_CACHE_TTL_FAIL)
            return False

    result = await client.is_user_authorized()
    ttl = _AUTH_CACHE_TTL_OK if result else _AUTH_CACHE_TTL_FAIL
    _authorized_cache[cache_key] = (result, _time.time() + ttl)
    return result


async def disconnect_client() -> None:
    """断开当前 client（不删 session 文件）"""
    global _client
    _invalidate_auth_cache()
    if _client is not None:
        try:
            if _client.is_connected():
                await _client.disconnect()
        except Exception:
            logger.warning("disconnect 失败", exc_info=True)
        _client = None


async def send_code(api_id: int, api_hash: str, phone: str) -> str:
    """发送验证码到 Telegram 客户端。返回 phone_code_hash（verify 时要用）"""
    global _pending_phone_code_hash, _pending_phone
    client = await get_client(api_id, api_hash)
    if not client.is_connected():
        await client.connect()

    try:
        sent = await client.send_code_request(phone)
    except PhoneNumberInvalidError as e:
        raise ValueError(f"手机号格式不正确（需 E.164 格式如 +8613800138000）: {e}")

    _pending_phone_code_hash = sent.phone_code_hash
    _pending_phone = phone
    return sent.phone_code_hash


async def sign_in(
    api_id: int,
    api_hash: str,
    phone: str,
    code: str,
    password: Optional[str] = None,
) -> dict:
    """提交验证码完成登录。返回登录后的用户信息 dict。

    若账号开启了 2FA 且未传 password，抛 ``SessionPasswordNeededError``，
    前端应提示用户输入云密码后再次调用本函数（带 password 参数）。
    """
    global _pending_phone_code_hash, _pending_phone
    client = await get_client(api_id, api_hash)
    if not client.is_connected():
        await client.connect()

    # 优先用本进程内缓存的 phone_code_hash
    phone_code_hash = _pending_phone_code_hash
    phone_to_use = _pending_phone or phone

    try:
        if password:
            # 2FA 流程：先 sign_in 拿到 code 错误（或直接走 password 路径）
            try:
                await client.sign_in(
                    phone=phone_to_use,
                    code=code,
                    phone_code_hash=phone_code_hash,
                )
            except SessionPasswordNeededError:
                pass
            # 无论是否走到 SessionPasswordNeededError，都用 password 完成
            await client.sign_in(password=password)
        else:
            try:
                await client.sign_in(
                    phone=phone_to_use,
                    code=code,
                    phone_code_hash=phone_code_hash,
                )
            except SessionPasswordNeededError:
                # 上层捕获，转换为更友好的错误
                raise
    except PhoneCodeInvalidError as e:
        raise ValueError(f"验证码不正确: {e}")
    except PhoneCodeExpiredError as e:
        raise ValueError(f"验证码已过期，请重新发送: {e}")

    me = await client.get_me()
    _pending_phone_code_hash = None
    _pending_phone = None
    _invalidate_auth_cache()  # 登录成功 → 让下次 is_authorized 真实查询返回 True 并续期缓存
    return _user_to_dict(me)


async def logout() -> None:
    """登出并删除 .session 文件"""
    global _client, _pending_phone_code_hash, _pending_phone
    _pending_phone_code_hash = None
    _pending_phone = None
    _invalidate_auth_cache()

    if _client is not None:
        try:
            if not _client.is_connected():
                await _client.connect()
            if await _client.is_user_authorized():
                await _client.log_out()
        except Exception:
            logger.warning("log_out 调用失败", exc_info=True)
        try:
            if _client.is_connected():
                await _client.disconnect()
        except Exception:
            pass
        _client = None

    # 兜底：log_out 通常会清掉 session，但有时会残留
    sf = session_file()
    try:
        if sf.exists():
            sf.unlink()
    except OSError:
        logger.warning("无法删除 session 文件 %s", sf, exc_info=True)
    # journal 文件也清掉
    journal = sf.with_suffix(".session-journal")
    try:
        if journal.exists():
            journal.unlink()
    except OSError:
        pass


def _user_to_dict(user: User) -> dict:
    return {
        "tg_user_id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "phone": user.phone,
    }


# ---------- 对话列表 ----------

def _dialog_type(dialog: Dialog) -> str:
    """把 Telethon dialog 分类为 user/group/supergroup/channel"""
    entity = dialog.entity
    if isinstance(entity, User):
        return "private"
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, Channel):
        if getattr(entity, "megagroup", False):
            return "supergroup"
        if getattr(entity, "gigagroup", False):
            return "supergroup"
        return "channel"
    return "unknown"


def _normalize_chat_id(entity: Any) -> str:
    """把 Telethon entity 的 id 规范化为字符串。

    Telegram 内部对群/频道使用负数 id（兼容 Bot API 风格）。
    Telethon 1.40 起 entity.id 已经是正数 raw id；为了和 Telegram Desktop
    导出的 result.json 里的 chat id 对得上，需要按类型加前缀：
        - 普通 group: 转成 -<id>
        - supergroup/channel: 转成 -100<id>
        - 私聊: 直接 <id>
    实际上 result.json 里 ``"id": 1234567890`` 是不带 -100 前缀的 raw id；
    为了与现有数据库行为兼容，我们直接用 raw id 字符串。
    """
    return str(getattr(entity, "id", ""))


async def list_dialogs(api_id: int, api_hash: str) -> list[dict]:
    """列出全部对话。返回前端友好的字典列表。"""
    client = await get_client(api_id, api_hash)
    if not client.is_connected():
        await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("未登录，请先完成 Telegram 登录")

    dialogs: list[dict] = []
    async for d in client.iter_dialogs():
        entity = d.entity
        chat_id_str = _normalize_chat_id(entity)
        if not chat_id_str:
            continue

        name = d.name or getattr(entity, "title", None) or getattr(entity, "first_name", "") or chat_id_str
        # 优先取 message_count（Telethon 不直接给，但可从 dialog.message 推断 last_id）
        last_msg_id = d.message.id if d.message else None
        last_msg_date = d.message.date.isoformat() if (d.message and d.message.date) else None

        dialogs.append({
            "chat_id": chat_id_str,
            "name": name,
            "type": _dialog_type(d),
            "username": getattr(entity, "username", None),
            "unread_count": d.unread_count or 0,
            "last_message_id": last_msg_id,
            "last_message_date": last_msg_date,
        })
    return dialogs


# ---------- 消息转换 ----------

# Telethon entity 类型 → result.json entity type 字符串
_ENTITY_TYPE_MAP: dict[type, str] = {
    MessageEntityUrl: "link",
    MessageEntityTextUrl: "text_link",
    MessageEntityMention: "mention",
    MessageEntityMentionName: "mention_name",
    MessageEntityHashtag: "hashtag",
    MessageEntityEmail: "email",
    MessageEntityPhone: "phone",
    MessageEntityBold: "bold",
    MessageEntityItalic: "italic",
    MessageEntityUnderline: "underline",
    MessageEntityStrike: "strikethrough",
    MessageEntityCode: "code",
    MessageEntityPre: "pre",
}


def _entity_to_dict(text: str, ent: Any) -> dict:
    cls = type(ent)
    et_type = _ENTITY_TYPE_MAP.get(cls, cls.__name__.replace("MessageEntity", "").lower())
    offset = getattr(ent, "offset", 0)
    length = getattr(ent, "length", 0)
    sub = text[offset:offset + length] if text else ""
    href = None
    if isinstance(ent, MessageEntityTextUrl):
        href = getattr(ent, "url", None)
    elif isinstance(ent, MessageEntityUrl):
        href = sub
    return {
        "type": et_type,
        "text": sub,
        "offset": offset,
        "length": length,
        "href": href,
    }


def _media_type(msg: TLMessage) -> Optional[str]:
    """把 msg.media 映射成与 Telegram Desktop 导出一致的简短字符串"""
    if msg.photo:
        return "photo"
    if msg.video:
        return "video"
    if msg.audio:
        return "audio"
    if msg.voice:
        return "voice_message"
    if msg.video_note:
        return "video_message"
    if msg.sticker:
        return "sticker"
    if msg.gif:
        return "animation"
    if msg.contact:
        return "contact"
    if msg.poll:
        return "poll"
    if msg.geo or msg.venue:
        return "location"
    if msg.document:
        return "file"
    if msg.media:
        return "other"
    return None


def _sender_display_name(msg: TLMessage) -> tuple[str, str]:
    """从 Telethon 消息提取 (sender_display_name, sender_id_str)"""
    sender = msg.sender
    sender_id = msg.sender_id
    sender_id_str = str(sender_id) if sender_id is not None else ""

    if sender is None:
        return ("", sender_id_str)

    if isinstance(sender, User):
        first = sender.first_name or ""
        last = sender.last_name or ""
        full = (first + " " + last).strip() or sender.username or sender_id_str
        return (full, f"user{sender.id}")
    if isinstance(sender, Channel):
        return (sender.title or sender_id_str, f"channel{sender.id}")
    if isinstance(sender, Chat):
        return (sender.title or sender_id_str, f"chat{sender.id}")
    return (str(sender_id_str), sender_id_str)


def _forwarded_from(msg: TLMessage) -> Optional[str]:
    fw = getattr(msg, "forward", None) or getattr(msg, "fwd_from", None)
    if not fw:
        return None
    # Telethon 的 forward 对象有 sender / chat / from_name
    name = (
        getattr(fw, "from_name", None)
        or getattr(getattr(fw, "sender", None), "first_name", None)
        or getattr(getattr(fw, "chat", None), "title", None)
    )
    return name


def convert_message(msg: TLMessage, chat_id: str) -> Optional[dict]:
    """把一条 Telethon Message 转成 parser.parse_message 等价的 dict。

    返回 None 表示这条消息应跳过（service message / 空消息）。
    """
    # 跳过 service messages（入群/改名/置顶等）
    if msg.action is not None:
        return None

    text_plain = msg.message or ""
    media_type = _media_type(msg)

    # 空消息 + 无 media 跳过（与 parser 行为对齐）
    if not text_plain and not media_type:
        return None

    sender_display, sender_id_str = _sender_display_name(msg)

    # entities：Telethon 的 msg.entities 已经是 list[MessageEntity*]
    entities_dicts: list[dict] = []
    if msg.entities:
        for ent in msg.entities:
            try:
                entities_dicts.append(_entity_to_dict(text_plain, ent))
            except Exception:
                # entity 转换失败不应阻断整条消息
                pass

    # text 字段：与 Telegram Desktop 导出格式对齐 —— 纯字符串，
    # 或 ["...", {"type":"link","text":"..."}, ...] 混合数组；
    # 这里走"纯字符串 + entities 单独存"的简化路径，与 parser.normalize_text(str)
    # 输出一致（entities=[] 也兼容下游）。
    text_field_raw = text_plain

    return {
        "id": msg.id,
        "chat_id": chat_id,
        "date": msg.date.replace(tzinfo=None) if msg.date else None,
        "sender": sender_display,
        "sender_id": sender_id_str,
        "text": json.dumps(text_field_raw, ensure_ascii=False) if text_field_raw else "",
        "text_plain": text_plain,
        "reply_to_id": msg.reply_to_msg_id,
        "forwarded_from": _forwarded_from(msg),
        "media_type": media_type,
        "entities": entities_dicts or None,
    }


# ---------- 增量拉取 ----------

async def iter_chat_messages(
    api_id: int,
    api_hash: str,
    chat_id: str,
    *,
    min_id: int = 0,
    on_progress: Optional[Callable[[int], Awaitable[None] | None]] = None,
    on_flood_wait: Optional[Callable[[int], Awaitable[None] | None]] = None,
    abort_check: Optional[Callable[[], bool]] = None,
    max_retries: int = 10,
    flood_wait_max: int = 1800,
):
    """异步生成器：迭代某个 chat 的消息（id > min_id），按 id 升序。

    每条 yield 的是 ``convert_message`` 输出的 dict（已跳过 None）。

    回调：
      - on_progress(fetched_count) 每 50 条调一次。
      - on_flood_wait(seconds) 在被 Telegram 限流时调一次（>0 表示开始等待，0 表示等完）。
      - abort_check() 返回 True 时立刻停止。

    自动恢复策略：
      1. **FloodWait（限流）**：自动 sleep 服务器要求的秒数 + 2 秒缓冲，然后续传。
         不消耗 retry 预算（限流是 Telegram 正常节流，不算错误）。
         若要求等待 > ``flood_wait_max`` 秒（默认 30 分钟）则放弃并抛出。
      2. **ConnectionError / OSError / TimeoutError**：把 client 主动断开后指数退避重连
         （2/4/8/16/32/60 秒，最多 ``max_retries`` 次）。

    断点续传：记录已 yield 的最大 last_id，每次重新 ``iter_messages(min_id=last_id)``，
    既不会重复也不会跳过。
    """
    import asyncio as _asyncio

    fetched = 0
    last_id = min_id  # 已成功 yield 的最大 message id（断线/限流续传基准）
    attempt = 0
    client: TelegramClient | None = None

    async def _maybe_call(cb, *args):
        if cb is None:
            return
        res = cb(*args)
        if hasattr(res, "__await__"):
            await res

    while True:
        try:
            client = await get_client(api_id, api_hash)
            if not client.is_connected():
                await client.connect()
            if not await client.is_user_authorized():
                raise RuntimeError("未登录")

            # entity 解析：Telethon get_entity 接受 int id（带 -100 前缀） / username / Peer
            entity = await _resolve_entity(client, chat_id)

            # reverse=True → id 升序；min_id 严格大于 last_id
            async for tl_msg in client.iter_messages(entity, min_id=last_id, reverse=True):
                if abort_check and abort_check():
                    return
                converted = convert_message(tl_msg, chat_id)
                fetched += 1
                last_id = tl_msg.id  # 记录进度（即使 converted is None 也推进，避免回退重拉）
                if converted is not None:
                    yield converted
                if on_progress and fetched % 50 == 0:
                    await _maybe_call(on_progress, fetched)
            return  # 正常走完
        except FloodWaitError as e:
            # Telegram 限流 → 自动等待（不计入 attempt）
            wait = max(int(getattr(e, "seconds", 0) or 0), 1) + 2
            if wait > flood_wait_max:
                logger.error(
                    "iter_messages(%s) FloodWait 要求等待 %ds，超过上限 %ds，放弃",
                    chat_id, wait, flood_wait_max,
                )
                raise
            logger.warning(
                "iter_messages(%s) FloodWait %ds @ msg_id=%d，自动等待后续传...",
                chat_id, wait, last_id,
            )
            await _maybe_call(on_flood_wait, wait)
            try:
                # FloodWait 期间也要响应 abort（按秒分片 sleep）
                slept = 0
                while slept < wait:
                    if abort_check and abort_check():
                        return
                    step = min(1, wait - slept)
                    await _asyncio.sleep(step)
                    slept += step
            finally:
                # 通知 UI 等待结束
                await _maybe_call(on_flood_wait, 0)
            # 不 disconnect 也不 reset auth cache —— 继续 while 循环重启 iter_messages(min_id=last_id)
            continue
        except (ConnectionError, OSError, _asyncio.TimeoutError) as e:
            attempt += 1
            if attempt > max_retries:
                logger.error(
                    "iter_messages(%s) 连接 %d 次重试后仍失败：%s",
                    chat_id, max_retries, e,
                )
                raise
            delay = min(2 ** attempt, 60)
            logger.warning(
                "iter_messages(%s) 断开于 msg_id=%d（%s），%ds 后第 %d/%d 次重连续传...",
                chat_id, last_id, str(e)[:100], delay, attempt, max_retries,
            )
            # 主动断开旧连接让下次循环重建（避免 telethon 内部状态卡住）
            try:
                if client is not None and client.is_connected():
                    await client.disconnect()
            except Exception:
                pass
            _invalidate_auth_cache()
            await _asyncio.sleep(delay)


async def _resolve_entity(client: TelegramClient, chat_id: str) -> Any:
    """把字符串形式的 chat_id 解析为 Telethon entity。

    优先尝试 int —— 大多数情况都是 raw id（来自 list_dialogs）。
    """
    try:
        return await client.get_entity(int(chat_id))
    except (ValueError, TypeError):
        return await client.get_entity(chat_id)


def _entity_display_name(entity: Any) -> str:
    """从 Telethon entity 提取展示用群名（与 list_dialogs 中 d.name 等价）"""
    title = getattr(entity, "title", None)
    if title:
        return title
    parts = [
        (getattr(entity, "first_name", "") or "").strip(),
        (getattr(entity, "last_name", "") or "").strip(),
    ]
    full = " ".join(p for p in parts if p)
    if full:
        return full
    return getattr(entity, "username", None) or ""


async def get_chat_display_name(api_id: int, api_hash: str, chat_id: str) -> str:
    """通过 Telethon 拿群聊/频道/用户的展示名。失败时返回 chat_id 字符串。

    Telethon 内部对 entity 有缓存，list_dialogs 之后再调用通常不会走网络。
    """
    client = await get_client(api_id, api_hash)
    if not client.is_connected():
        try:
            await client.connect()
        except Exception:
            return chat_id
    if not await client.is_user_authorized():
        return chat_id
    try:
        entity = await _resolve_entity(client, chat_id)
        return _entity_display_name(entity) or chat_id
    except Exception as e:
        logger.warning("get_chat_display_name(%s) 失败: %s", chat_id, e)
        return chat_id


async def get_chat_remote_max_id(api_id: int, api_hash: str, chat_id: str) -> int:
    """取对端 chat 的最新一条消息 id。失败/无消息返回 0。

    用途：同步前估算"待拉取条数 = remote_max - local_max"，给前端进度条提供分母。
    一次 API call（``iter_messages(limit=1, reverse=False)``），网络开销 ~200ms，
    被 FloodWait 时直接失败回 0（让进度按未知处理，不阻塞同步主流程）。
    """
    try:
        client = await get_client(api_id, api_hash)
        if not client.is_connected():
            await client.connect()
        if not await client.is_user_authorized():
            return 0
        entity = await _resolve_entity(client, chat_id)
        # iter_messages 默认按时间倒序，取第一条 = 最新
        async for tl_msg in client.iter_messages(entity, limit=1):
            return int(getattr(tl_msg, "id", 0) or 0)
        return 0
    except FloodWaitError:
        # 被限流就不估算了，让 UI 显示"未知"分母而不是阻塞主同步
        return 0
    except Exception as e:
        logger.warning("get_chat_remote_max_id(%s) 失败: %s", chat_id, e)
        return 0


def date_range_string(min_dt: Optional[datetime], max_dt: Optional[datetime]) -> str:
    """生成与 Import.date_range 字段一致的 'YYYY-MM-DD ~ YYYY-MM-DD' 格式"""
    if not min_dt or not max_dt:
        return "未知"
    return f"{min_dt.strftime('%Y-%m-%d')} ~ {max_dt.strftime('%Y-%m-%d')}"
