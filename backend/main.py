import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.models.database import init_db
from backend.routers import (
    import_router,
    qa_router,
    session_router,
    settings_router,
    summary_router,
)
from backend.services.run_registry import periodic_cleanup


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # 后台定时清理过期 run（完成 5 分钟后自动从 registry 中移除）
    cleanup_task = asyncio.create_task(periodic_cleanup(interval_seconds=60))
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except Exception:
            pass


app = FastAPI(
    title="Telegram 群聊智能分析系统",
    description="导入 Telegram 群聊数据，AI 摘要 + RAG 问答",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(import_router.router)
app.include_router(summary_router.router)
app.include_router(qa_router.router)
app.include_router(session_router.router)
app.include_router(settings_router.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
