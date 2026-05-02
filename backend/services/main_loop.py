"""把后台协程调度到 FastAPI 主事件循环上的 helper。

为什么需要这个：
- `llm_adapter` 在模块级缓存了 `asyncio.Semaphore`（_CHAT_SEM / _EMBED_SEM）和
  `AsyncOpenAI` 内部的 `httpx.AsyncClient` 连接池；
- 这些 asyncio 原语首次被 `async with` 时绑定到当时的事件循环，之后再被另一个
  循环触碰会抛 `RuntimeError: <Semaphore ...> is bound to a different event loop`；
- 早期实现通过 `threading.Thread` + `asyncio.run(...)` 起后台任务，每次都会新建
  一个独立循环，必然和 FastAPI 主循环冲突。

解决办法：所有后台协程统一调度到 FastAPI 启动时捕获的主循环上，全应用共用一个
循环，模块级单例就不存在跨循环问题。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Coroutine, Optional

logger = logging.getLogger(__name__)

_main_loop: Optional[asyncio.AbstractEventLoop] = None
# 持有 create_task 返回的 Task 强引用，避免任务被 GC 提前回收
# （asyncio 只保留弱引用 — 见官方文档 asyncio.create_task 注意事项）
_background_tasks: set[asyncio.Task] = set()


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """注册 FastAPI 主事件循环。在 lifespan 启动阶段调用一次。"""
    global _main_loop
    _main_loop = loop


def get_main_loop() -> Optional[asyncio.AbstractEventLoop]:
    return _main_loop


def schedule_on_main_loop(coro: Coroutine) -> None:
    """把协程调度到主循环上，fire-and-forget。

    - 如果调用方就在主循环里（async 端点）→ 用 ``loop.create_task``；
    - 如果调用方在别的线程里（FastAPI 把 sync `def` 端点放线程池跑）
      → 用 ``asyncio.run_coroutine_threadsafe``；
    - 都不走 ``asyncio.run``，避免新建事件循环导致跨循环原语冲突。
    """
    if _main_loop is None or _main_loop.is_closed():
        # 没初始化就被调用通常说明 lifespan 顺序错了；关掉协程避免 "coroutine was never awaited" 警告。
        coro.close()
        raise RuntimeError(
            "main event loop is not initialized; cannot schedule background work"
        )

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    if running is _main_loop:
        task = _main_loop.create_task(coro)
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    else:
        asyncio.run_coroutine_threadsafe(coro, _main_loop)
