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
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from telethon.errors import (
    FloodWaitError,
    SessionPasswordNeededError,
    TakeoutInitDelayError,
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
    "estimating": False,
    "grand_total": 0,
    "grand_fetched": 0,
    "current_chat_id": None,
    "current_chat_name": None,
    "current_chat_total": 0,
    "current_fetched": 0,
    "current_imported": 0,
    "flood_wait_until": None,
    "flood_wait_seconds": 0,
    "takeout_pending": False,
    "takeout_pending_until": None,
    "takeout_pending_seconds": 0,
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
        "estimating": False,
        "grand_total": 0,
        "grand_fetched": 0,
        "current_chat_id": None,
        "current_chat_name": None,
        "current_chat_total": 0,
        "current_fetched": 0,
        "current_imported": 0,
        "flood_wait_until": None,
        "flood_wait_seconds": 0,
        "takeout_pending": False,
        "takeout_pending_until": None,
        "takeout_pending_seconds": 0,
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

    # join 本地 Import + Messages 表（一次性聚合，避免 N+1）
    chat_ids = [d["chat_id"] for d in dialogs]

    def _aggregate_local_state(cids: list[str]) -> tuple[dict, dict]:
        """同步聚合：一次查 Import 表 + 一次 GROUP BY 查所有 max(Message.id)。"""
        if not cids:
            return {}, {}
        imp_rows = (
            db.query(Import.chat_id, Import.message_count)
            .filter(Import.chat_id.in_(cids))
            .all()
        )
        imp_map = {cid: cnt for cid, cnt in imp_rows}

        # 一条 GROUP BY 替代 N 次 max(Message.id)，消除 N+1
        max_rows = (
            db.query(Message.chat_id, func.max(Message.id))
            .filter(Message.chat_id.in_(cids))
            .group_by(Message.chat_id)
            .all()
        )
        max_map = {cid: mid for cid, mid in max_rows if mid is not None}
        return imp_map, max_map

    # db 查询整体派 thread，避免阻塞主循环（即便对几百个 chat 也只是 ~10ms 但仍 IO）
    imported_map, max_id_map = await asyncio.to_thread(_aggregate_local_state, chat_ids)

    out: list[TelegramDialogInfo] = []
    for d in dialogs:
        cid = d["chat_id"]
        local_max = 0
        if cid in imported_map and cid in max_id_map:
            offset = _stable_id_offset(cid)
            local_max = max(0, max_id_map[cid] - offset)

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
        # ---------- 阶段 0：预扫描（Phase H：消息级总进度分母） ----------
        # 让前端主进度条按消息维度显示总体进度（而不是 chat 数维度）。
        # 优化：list_dialogs 一次返回所有对话的 last_message_id，本地 SQL 一次 GROUP BY
        # 拿所有 chat 的 max(Message.id)，整个阶段 0 总计 2 个 IO 操作（~1-2 秒），
        # 不再随 chat 数线性增长。
        _sync_progress["estimating"] = True
        try:
            dialogs = await tg.list_dialogs(api_id, api_hash)
            remote_max_map: dict[str, int] = {
                d["chat_id"]: int(d.get("last_message_id") or 0) for d in dialogs
            }
        except Exception as e:
            logger.warning("预扫描 list_dialogs 失败：%s", e)
            remote_max_map = {}

        def _all_local_max() -> dict[str, int]:
            rows = (
                db.query(Message.chat_id, func.max(Message.id))
                .filter(Message.chat_id.in_(chat_ids))
                .group_by(Message.chat_id)
                .all()
            )
            return {cid: mid for cid, mid in rows if mid is not None}

        local_max_map = await asyncio.to_thread(_all_local_max)

        pre_estimates: dict[str, int] = {}
        for cid in chat_ids:
            if _sync_progress.get("aborting"):
                break
            offset = _stable_id_offset(cid)
            local_min = max(0, local_max_map.get(cid, 0) - offset) if local_max_map.get(cid) else 0
            remote_max = remote_max_map.get(cid, 0)
            est = max(0, remote_max - local_min) if remote_max > local_min else 0
            pre_estimates[cid] = est
        _sync_progress["grand_total"] = sum(pre_estimates.values())
        _sync_progress["estimating"] = False

        # ---------- 阶段 1：实际同步 ----------
        for cid in chat_ids:
            if _sync_progress.get("aborting"):
                break

            # 一次性把该 chat 的状态加载齐全，包含：
            #   - min_id（incremental 拉取下界）
            #   - Import 行
            # 注意：以前这里还会调 _load_existing_ids(db, cid) 一次性加载全 chat 的
            # message.id 集合（50w 群 ~30MB 内存 / 几秒 IO，且阻塞 sync 启动让用户看着空白）。
            # 现在改用 import_messages_for_chat 内部的 batch-scope IN 查询（每 2000 条
            # 消息一次 IN，~10ms 被 commit 时间天然掩盖），把 existing_ids 设 None
            # 即可触发新模式。
            def _load_chat_state():
                offset_local = _stable_id_offset(cid)
                row_local = (
                    db.query(func.max(Message.id))
                    .filter(Message.chat_id == cid)
                    .scalar()
                )
                min_id_local = max(0, (row_local or 0) - offset_local) if row_local else 0
                imp_local = db.query(Import).filter(Import.chat_id == cid).first()
                existing_local = imp_local.chat_name if imp_local else None
                return min_id_local, imp_local, existing_local

            min_id, imp, existing_name = await asyncio.to_thread(_load_chat_state)

            # 群名优先级：
            #   1. 现有 Import.chat_name 且看起来不是 chat_id 数字 → 用它
            #   2. 否则通过 Telethon 拿真实群名
            #   3. 都拿不到 → 退回 cid
            if existing_name and existing_name != cid:
                chat_name = existing_name
            else:
                chat_name = await tg.get_chat_display_name(api_id, api_hash, cid)
                if not chat_name:
                    chat_name = cid
                # 把首次拿到的真实群名回写 DB（fsync 派 thread）
                if imp and imp.chat_name != chat_name:
                    def _persist_name(name=chat_name, target=imp):
                        target.chat_name = name
                        db.commit()
                    await asyncio.to_thread(_persist_name)

            # 复用阶段 0 预扫描的估算（Phase H）：避免对同一 chat 重复调 API
            estimated_total = pre_estimates.get(cid, 0)

            _sync_progress.update({
                "current_chat_id": cid,
                "current_chat_name": chat_name,
                "current_chat_total": estimated_total,
                "current_fetched": 0,
                "current_imported": 0,
                "flood_wait_until": None,
                "flood_wait_seconds": 0,
            })

            # FloodWait 回调：iter_chat_messages 在被限流时调用
            #   wait > 0：开始等待 wait 秒 → 写 flood_wait_until + flood_wait_seconds
            #   wait == 0：等待结束 → 清空
            def _on_flood_wait(wait: int) -> None:
                if wait > 0:
                    # 用 timezone-aware UTC，保证 Pydantic 序列化时带 +00:00 后缀
                    # → 前端 new Date() 能按 UTC 正确解析
                    _sync_progress["flood_wait_until"] = (
                        datetime.now(timezone.utc) + timedelta(seconds=wait)
                    )
                    _sync_progress["flood_wait_seconds"] = wait
                else:
                    _sync_progress["flood_wait_until"] = None
                    _sync_progress["flood_wait_seconds"] = 0

            # Takeout 授权挂起回调：iter_chat_messages 进入 takeout context 失败时调用
            #   wait > 0：用户必须在另一台 Telegram 客户端点"同意导出"，最多等 wait 秒
            #   wait == 0：takeout 已就绪，清空挂起状态
            def _on_takeout_pending(wait: int) -> None:
                if wait > 0:
                    _sync_progress["takeout_pending"] = True
                    _sync_progress["takeout_pending_until"] = (
                        datetime.now(timezone.utc) + timedelta(seconds=wait)
                    )
                    _sync_progress["takeout_pending_seconds"] = wait
                else:
                    _sync_progress["takeout_pending"] = False
                    _sync_progress["takeout_pending_until"] = None
                    _sync_progress["takeout_pending_seconds"] = 0

            batch: list[dict] = []
            min_dt: datetime | None = None
            max_dt: datetime | None = None
            total_new = 0
            error: str | None = None

            try:
                async for converted in tg.iter_chat_messages(
                    api_id, api_hash, cid,
                    min_id=min_id,
                    # 上一阶段预扫描算出的"远端有但本地没的"消息数估计，供 iter_chat_messages
                    # 内部判断是否触发 takeout→普通通道的 fallback（>0 时启用）
                    expected_new=estimated_total,
                    abort_check=lambda: _sync_progress.get("aborting", False),
                    on_flood_wait=_on_flood_wait,
                    on_takeout_pending=_on_takeout_pending,
                ):
                    _sync_progress["current_fetched"] = _sync_progress.get("current_fetched", 0) + 1
                    # 跨 chat 累加（Phase H）：让前端主进度条能按消息维度显示总进度
                    _sync_progress["grand_fetched"] = _sync_progress.get("grand_fetched", 0) + 1
                    batch.append(converted)
                    dt = converted.get("date")
                    if dt:
                        if min_dt is None or dt < min_dt:
                            min_dt = dt
                        if max_dt is None or dt > max_dt:
                            max_dt = dt

                    if len(batch) >= 2000:
                        # batch size 500 → 2000：减少 commit 次数 + 减少 progress 更新频率。
                        # existing_ids=None：启用 import_messages_for_chat 的 batch-scope dedup
                        # （每 batch 一次 IN 查询而不是开局一次性加载全 chat id 集到内存）。
                        # skip_total_count=True：避免每 batch 都跑一次 COUNT(*) 全表扫。
                        partial = await asyncio.to_thread(
                            import_messages_for_chat,
                            db,
                            chat_id=cid,
                            chat_name=chat_name,
                            messages=batch,
                            date_range=tg.date_range_string(min_dt, max_dt),
                            existing_ids=None,
                            skip_total_count=True,
                        )
                        total_new += partial.message_count
                        _sync_progress["current_imported"] = total_new
                        batch.clear()

                # 正常走完：剩余 batch 在下方统一 flush（避免与 except 分支重复）
            except TakeoutInitDelayError as e:
                # Takeout 需用户授权 —— 全局问题（不只影响当前 chat），后续所有 chat 都会一样失败。
                # _on_takeout_pending 已写入挂起状态供前端展示横幅。
                # 标记当前 chat 失败 + 触发 abort，让外层主循环退出（用户授权后重新点同步）
                error = f"Telegram 数据导出需授权：请在另一台 Telegram 客户端确认请求（最多等 {e.seconds} 秒后重试）"
                logger.warning("Takeout 授权挂起（%ds），同步终止待用户操作", e.seconds)
                _sync_progress["aborting"] = True
            except FloodWaitError as e:
                # 兜底：iter_chat_messages 已经会自动 sleep ≤ flood_wait_max（30 分钟）的限流；
                # 走到这里说明等待时间超过上限，让用户知道并继续下一个 chat
                error = f"Telegram 限流，需等待 {e.seconds} 秒（超过自动等待上限）"
                logger.warning("FloodWait on %s: %ds（超过上限放弃）", cid, e.seconds)
            except Exception as e:
                error = str(e)[:300]
                logger.exception("同步 %s 失败", cid)

            # 无论成功/失败，把 batch 里剩下的消息存入 DB —— 保证「部分成功」不会丢
            # 收尾 flush 用 skip_total_count=False，让 Import.message_count 拿到准确值
            #
            # 第三个条件 ``imp is None and error is None``：首次同步一个群拿到 0 条新消息
            # 但没出错的情况下，也走一次 import_messages_for_chat 让它创建 message_count=0 的
            # Import 行 —— 否则该群从未在「已导入的群聊」列表里出现，用户会以为"被静默跳过"。
            # 错误情况（imp is None and error is not None）保持不建 Import 行，免得用户在列表里
            # 看到一行"成功导入 0 条"而把红色错误条目当作误报。
            if batch or total_new > 0 or (imp is None and error is None):
                try:
                    partial = await asyncio.to_thread(
                        import_messages_for_chat,
                        db,
                        chat_id=cid,
                        chat_name=chat_name,
                        messages=batch,
                        date_range=tg.date_range_string(min_dt, max_dt),
                        existing_ids=None,
                        skip_total_count=False,  # 收尾纠正 Import.message_count
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
        # abort 中途退出 for 循环时，被跳过的 chat 一直没机会写 results。
        # 这里补一次差集：让用户能在 UI 上看到"哪些群没跑"，而不是误以为它们被静默成功导入。
        try:
            processed_ids = {r["chat_id"] for r in _sync_progress.get("results", [])}
            for cid in chat_ids:
                if cid in processed_ids:
                    continue
                _sync_progress["results"].append({
                    "chat_id": cid,
                    "chat_name": cid,  # 拿不到 entity 的真实名字（从未进循环），先回退到 chat_id
                    "status": "skipped",
                    "message_count": 0,
                    "error": "中止同步前未处理",
                })
        except Exception:
            logger.exception("补全 skipped results 失败（可忽略）")

        _sync_progress["running"] = False
        _sync_progress["estimating"] = False
        _sync_progress["finished_at"] = datetime.utcnow()
        _sync_progress["current_chat_id"] = None
        _sync_progress["current_chat_name"] = None
        _sync_progress["current_chat_total"] = 0
        _sync_progress["flood_wait_until"] = None
        _sync_progress["flood_wait_seconds"] = 0
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
