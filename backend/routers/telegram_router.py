"""Telegram 直连同步 HTTP 路由。

所有路径前缀 /api/telegram。

| 路径                    | 方法 | 说明                                                  |
|-------------------------|------|-------------------------------------------------------|
| /account                | GET  | 当前登录态                                            |
| /account                | POST | 保存 api_id/hash/phone（不发码）                      |
| /account                | DELETE | 退出登录 + 清 session + 清 DB                       |
| /login/send-code        | POST | 触发 Telegram 客户端发码                              |
| /login/verify           | POST | 提交验证码（+ 可选 2FA 密码）                         |
| /dialogs                | GET  | 列出全部对话 + 本地导入状态                           |
| /sync                   | POST | 后台增量同步选中 chat_ids                             |
| /sync/progress          | GET  | 查询同步进度                                          |
| /sync/abort             | POST | 中止当前同步                                          |
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from telethon.errors import (
    FloodWaitError,
    SessionPasswordNeededError,
)

from backend.models.database import (
    Import,
    Message,
    SessionLocal,
    TelegramAccount,
    get_db,
)
from backend.models.schemas import (
    TelegramAccountInfo,
    TelegramConfigureRequest,
    TelegramDialogInfo,
    TelegramSendCodeResponse,
    TelegramSyncProgress,
    TelegramSyncRequest,
    TelegramVerifyRequest,
)
from backend.routers.import_router import (
    _enqueue_index,
    _stable_id_offset,
    import_messages_for_chat,
)
from backend.services import telegram_sync as tg
from backend.services.main_loop import schedule_on_main_loop

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/telegram", tags=["telegram"])


# ---------- 进度状态（全局单例） ----------

_sync_progress: dict[str, Any] = {
    "running": False,
    "aborting": False,
    "total": 0,
    "completed": 0,
    "current_chat_id": None,
    "current_chat_name": None,
    "current_fetched": 0,
    "current_imported": 0,
    "results": [],
    "started_at": None,
    "finished_at": None,
}


def _reset_progress(total: int) -> None:
    _sync_progress.update({
        "running": True,
        "aborting": False,
        "total": total,
        "completed": 0,
        "current_chat_id": None,
        "current_chat_name": None,
        "current_fetched": 0,
        "current_imported": 0,
        "results": [],
        "started_at": datetime.utcnow(),
        "finished_at": None,
    })


# ---------- 公共工具 ----------

def _get_account(db: Session) -> TelegramAccount | None:
    """singleton：永远只有 1 行（id 最小的）"""
    return db.query(TelegramAccount).order_by(TelegramAccount.id.asc()).first()


def _account_to_info(
    acc: TelegramAccount | None,
    *,
    authorized: bool = False,
) -> TelegramAccountInfo:
    proxy = tg.proxy_status()
    if acc is None:
        return TelegramAccountInfo(configured=False, authorized=False, proxy=proxy)
    return TelegramAccountInfo(
        configured=True,
        authorized=authorized,
        phone=acc.phone,
        tg_user_id=acc.tg_user_id,
        username=acc.username,
        first_name=acc.first_name,
        last_name=acc.last_name,
        last_login_at=acc.last_login_at,
        proxy=proxy,
    )


# ---------- /account ----------

@router.get("/account", response_model=TelegramAccountInfo)
async def get_account(db: Session = Depends(get_db)):
    acc = _get_account(db)
    if acc is None:
        return _account_to_info(None)
    try:
        authorized = await tg.is_authorized(acc.api_id, acc.api_hash)
    except Exception as e:
        logger.warning("is_authorized 失败: %s", e)
        authorized = False
    return _account_to_info(acc, authorized=authorized)


@router.post("/account", response_model=TelegramAccountInfo)
def upsert_account(req: TelegramConfigureRequest, db: Session = Depends(get_db)):
    """保存 api_id/api_hash/phone（singleton）。不触发发码。

    覆盖已有行（如果用户改了 phone 等）。
    """
    if req.api_id <= 0:
        raise HTTPException(400, "api_id 必须是正整数")
    if not req.api_hash or len(req.api_hash) < 16:
        raise HTTPException(400, "api_hash 格式不正确（应为 32 位 hex）")
    phone = req.phone.strip()
    if not phone.startswith("+"):
        raise HTTPException(400, "手机号需 E.164 格式（含国家码 + 号），如 +8613800138000")

    acc = _get_account(db)
    if acc is None:
        acc = TelegramAccount(
            api_id=req.api_id,
            api_hash=req.api_hash,
            phone=phone,
        )
        db.add(acc)
    else:
        acc.api_id = req.api_id
        acc.api_hash = req.api_hash
        acc.phone = phone
    db.commit()
    db.refresh(acc)
    return _account_to_info(acc, authorized=False)


@router.delete("/account")
async def delete_account(db: Session = Depends(get_db)):
    """退出登录 + 清 session + 清 DB 行"""
    acc = _get_account(db)
    if acc is None:
        return {"status": "noop"}

    try:
        await tg.logout()
    except Exception as e:
        logger.warning("Telegram logout 失败: %s", e)

    db.delete(acc)
    db.commit()
    return {"status": "ok"}


# ---------- /login ----------

@router.post("/login/send-code", response_model=TelegramSendCodeResponse)
async def login_send_code(db: Session = Depends(get_db)):
    """触发 Telegram 给已配置的 phone 发验证码。"""
    acc = _get_account(db)
    if acc is None:
        raise HTTPException(400, "请先 POST /api/telegram/account 配置 api_id/hash/phone")

    try:
        phone_code_hash = await tg.send_code(acc.api_id, acc.api_hash, acc.phone)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("send_code 失败")
        raise HTTPException(500, f"发送验证码失败: {e}")

    return TelegramSendCodeResponse(sent=True, phone_code_hash=phone_code_hash)


@router.post("/login/verify", response_model=TelegramAccountInfo)
async def login_verify(req: TelegramVerifyRequest, db: Session = Depends(get_db)):
    """用验证码（+ 可选 2FA 密码）完成登录。"""
    acc = _get_account(db)
    if acc is None:
        raise HTTPException(400, "请先配置 api_id/hash/phone 并发送验证码")

    code = req.code.strip().replace(" ", "")
    if not code:
        raise HTTPException(400, "请输入验证码")

    try:
        info = await tg.sign_in(
            acc.api_id, acc.api_hash, acc.phone, code, password=req.password
        )
    except SessionPasswordNeededError:
        # 账号开启了 2FA，前端要补 password 再调一次
        return TelegramAccountInfo(
            configured=True,
            authorized=False,
            phone=acc.phone,
            needs_password=True,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("Telegram sign_in 失败")
        raise HTTPException(500, f"登录失败: {e}")

    # 写回用户信息
    acc.tg_user_id = info.get("tg_user_id")
    acc.username = info.get("username")
    acc.first_name = info.get("first_name")
    acc.last_name = info.get("last_name")
    acc.last_login_at = datetime.utcnow()
    db.commit()
    db.refresh(acc)

    return _account_to_info(acc, authorized=True)


# ---------- /dialogs ----------

@router.get("/dialogs", response_model=list[TelegramDialogInfo])
async def list_dialogs(db: Session = Depends(get_db)):
    """列出 Telegram 全部对话，附加本地导入状态。"""
    acc = _get_account(db)
    if acc is None:
        raise HTTPException(400, "未配置 Telegram 账号")
    try:
        authorized = await tg.is_authorized(acc.api_id, acc.api_hash)
    except Exception as e:
        raise HTTPException(500, f"连接 Telegram 失败: {e}")
    if not authorized:
        raise HTTPException(401, "未登录，请先完成验证码登录")

    try:
        dialogs = await tg.list_dialogs(acc.api_id, acc.api_hash)
    except FloodWaitError as e:
        raise HTTPException(429, f"Telegram 限流，请等待 {e.seconds} 秒后重试")
    except Exception as e:
        logger.exception("list_dialogs 失败")
        raise HTTPException(500, f"列出对话失败: {e}")

    # join 本地 Import + Messages 表
    chat_ids = [d["chat_id"] for d in dialogs]
    if chat_ids:
        imp_rows = (
            db.query(Import.chat_id, Import.message_count)
            .filter(Import.chat_id.in_(chat_ids))
            .all()
        )
        imported_map = {cid: cnt for cid, cnt in imp_rows}
    else:
        imported_map = {}

    out: list[TelegramDialogInfo] = []
    for d in dialogs:
        cid = d["chat_id"]
        local_max = 0
        if cid in imported_map:
            offset = _stable_id_offset(cid)
            row = (
                db.query(func.max(Message.id))
                .filter(Message.chat_id == cid)
                .scalar()
            )
            if row:
                local_max = max(0, row - offset)

        out.append(TelegramDialogInfo(
            chat_id=cid,
            name=d["name"],
            type=d["type"],
            username=d.get("username"),
            unread_count=d.get("unread_count", 0),
            last_message_id=d.get("last_message_id"),
            last_message_date=d.get("last_message_date"),
            imported=cid in imported_map,
            imported_message_count=imported_map.get(cid, 0),
            local_max_message_id=local_max,
        ))
    return out


# ---------- /sync ----------

async def _sync_runner(api_id: int, api_hash: str, chat_ids: list[str]) -> None:
    """后台同步 worker（在主事件循环上跑）"""
    db = SessionLocal()
    new_chat_ids: list[str] = []
    try:
        for cid in chat_ids:
            if _sync_progress.get("aborting"):
                break

            # 计算 min_id（增量基准）
            offset = _stable_id_offset(cid)
            row = (
                db.query(func.max(Message.id))
                .filter(Message.chat_id == cid)
                .scalar()
            )
            min_id = max(0, (row or 0) - offset) if row else 0

            # 群名优先级：
            #   1. 现有 Import.chat_name 且看起来不是 chat_id 数字 → 用它（用户/历史可能有正确名字）
            #   2. 否则通过 Telethon 拿真实群名（list_dialogs 后通常走本地缓存）
            #   3. 都拿不到 → 退回 cid
            imp = db.query(Import).filter(Import.chat_id == cid).first()
            existing_name = imp.chat_name if imp else None
            if existing_name and existing_name != cid:
                chat_name = existing_name
            else:
                chat_name = await tg.get_chat_display_name(api_id, api_hash, cid)
                if not chat_name:
                    chat_name = cid
                # 把首次拿到的真实群名回写 DB（修复历史脏数据）
                if imp and imp.chat_name != chat_name:
                    imp.chat_name = chat_name
                    db.commit()

            _sync_progress.update({
                "current_chat_id": cid,
                "current_chat_name": chat_name,
                "current_fetched": 0,
                "current_imported": 0,
            })

            batch: list[dict] = []
            min_dt: datetime | None = None
            max_dt: datetime | None = None
            total_new = 0
            error: str | None = None

            try:
                async for converted in tg.iter_chat_messages(
                    api_id, api_hash, cid,
                    min_id=min_id,
                    abort_check=lambda: _sync_progress.get("aborting", False),
                ):
                    _sync_progress["current_fetched"] = _sync_progress.get("current_fetched", 0) + 1
                    batch.append(converted)
                    dt = converted.get("date")
                    if dt:
                        if min_dt is None or dt < min_dt:
                            min_dt = dt
                        if max_dt is None or dt > max_dt:
                            max_dt = dt

                    if len(batch) >= 500:
                        # 群名兜底：从 dialog 拿不到就用第一条 sender 推断 — 这里直接用 chat_name
                        partial = await asyncio.to_thread(
                            import_messages_for_chat,
                            db,
                            chat_id=cid,
                            chat_name=chat_name,
                            messages=batch,
                            date_range=tg.date_range_string(min_dt, max_dt),
                        )
                        total_new += partial.message_count
                        _sync_progress["current_imported"] = total_new
                        batch.clear()

                # 正常走完：剩余 batch 在下方统一 flush（避免与 except 分支重复）
            except FloodWaitError as e:
                # Telethon 不会自动 sleep > 60s 的等待 —— 直接报错给前端
                error = f"Telegram 限流，需等待 {e.seconds} 秒"
                logger.warning("FloodWait on %s: %ds", cid, e.seconds)
            except Exception as e:
                error = str(e)[:300]
                logger.exception("同步 %s 失败", cid)

            # 无论成功/失败，把 batch 里剩下的消息存入 DB —— 保证「部分成功」不会丢
            if batch:
                try:
                    partial = await asyncio.to_thread(
                        import_messages_for_chat,
                        db,
                        chat_id=cid,
                        chat_name=chat_name,
                        messages=batch,
                        date_range=tg.date_range_string(min_dt, max_dt),
                    )
                    total_new += partial.message_count
                    _sync_progress["current_imported"] = total_new
                except Exception as flush_err:
                    logger.warning("收尾 flush %s 失败: %s", cid, flush_err)
                    if error is None:
                        error = f"收尾保存失败: {flush_err}"
                batch.clear()

            status = "ok" if error is None else "error"
            _sync_progress["results"].append({
                "chat_id": cid,
                "chat_name": chat_name,
                "status": status,
                "message_count": total_new,
                "error": error,
            })
            if total_new > 0 and error is None:
                new_chat_ids.append(cid)

            _sync_progress["completed"] = _sync_progress.get("completed", 0) + 1

    finally:
        _sync_progress["running"] = False
        _sync_progress["finished_at"] = datetime.utcnow()
        _sync_progress["current_chat_id"] = None
        _sync_progress["current_chat_name"] = None
        db.close()

    # 触发后台索引构建（去重）
    if new_chat_ids:
        seen: set[str] = set()
        unique_ids: list[str] = []
        for c in new_chat_ids:
            if c not in seen:
                seen.add(c)
                unique_ids.append(c)
        try:
            _enqueue_index(unique_ids)
        except Exception:
            logger.exception("入队索引构建失败")


@router.post("/sync")
async def start_sync(req: TelegramSyncRequest, db: Session = Depends(get_db)):
    """后台启动 Telegram 增量同步。立即返回，进度查 /sync/progress。"""
    acc = _get_account(db)
    if acc is None:
        raise HTTPException(400, "未配置 Telegram 账号")
    if not req.chat_ids:
        raise HTTPException(400, "请至少选择一个 chat")

    if _sync_progress.get("running"):
        raise HTTPException(409, "已有同步任务在进行中，请先等待或调用 /sync/abort")

    try:
        authorized = await tg.is_authorized(acc.api_id, acc.api_hash)
    except Exception as e:
        raise HTTPException(500, f"连接 Telegram 失败: {e}")
    if not authorized:
        raise HTTPException(401, "未登录，请先完成验证码登录")

    _reset_progress(len(req.chat_ids))
    schedule_on_main_loop(_sync_runner(acc.api_id, acc.api_hash, list(req.chat_ids)))
    return {"status": "started", "total": len(req.chat_ids)}


@router.get("/sync/progress", response_model=TelegramSyncProgress)
def get_sync_progress():
    return TelegramSyncProgress(**_sync_progress)


@router.post("/sync/abort")
def abort_sync():
    if not _sync_progress.get("running"):
        return {"status": "noop"}
    _sync_progress["aborting"] = True
    return {"status": "aborting"}


# ---------- /refresh-names ----------

@router.post("/refresh-names")
async def refresh_chat_names(db: Session = Depends(get_db)):
    """一次性回填群名 — 把所有 chat_name 等于 chat_id 的 Import 行通过 Telethon 拿真实名字写回。

    用于修复历史首次同步时 fallback 到 chat_id 字符串的脏数据。
    """
    acc = _get_account(db)
    if acc is None or not acc.api_id:
        raise HTTPException(400, "未配置 Telegram 账号")
    if not await tg.is_authorized(acc.api_id, acc.api_hash):
        raise HTTPException(401, "未登录 Telegram，请先完成登录")

    # 找出所有「chat_name 像 chat_id」的 Import 行
    rows = (
        db.query(Import)
        .filter(Import.chat_name == Import.chat_id)
        .all()
    )
    if not rows:
        return {"updated": 0, "checked": 0, "items": []}

    items: list[dict] = []
    updated = 0
    for imp in rows:
        cid = imp.chat_id
        try:
            name = await tg.get_chat_display_name(acc.api_id, acc.api_hash, cid)
        except Exception as e:
            items.append({"chat_id": cid, "status": "error", "error": str(e)[:200]})
            continue
        if name and name != cid and name != imp.chat_name:
            imp.chat_name = name
            updated += 1
            items.append({"chat_id": cid, "status": "ok", "new_name": name})
        else:
            items.append({"chat_id": cid, "status": "unchanged"})
    db.commit()
    return {"checked": len(rows), "updated": updated, "items": items}
