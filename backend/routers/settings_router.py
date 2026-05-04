import os

from fastapi import APIRouter

from backend.config import settings
from backend.models.schemas import SettingsResponse, SettingsUpdate
from backend.services import llm_adapter

router = APIRouter(prefix="/api", tags=["settings"])


def _build_response() -> SettingsResponse:
    return SettingsResponse(
        llm_model_map=settings.llm_model_map,
        llm_model_qa=settings.llm_model_qa,
        qa_context_window=llm_adapter.get_context_window(settings.llm_model_qa),
        embedding_model=settings.embedding_model,
        rerank_model=settings.rerank_model,
        has_api_key=bool(settings.dashscope_api_key),
        has_moonshot_key=bool(settings.moonshot_api_key),
    )


@router.get("/settings", response_model=SettingsResponse)
def get_settings():
    """获取当前配置"""
    return _build_response()


@router.put("/settings", response_model=SettingsResponse)
def update_settings(req: SettingsUpdate):
    """更新配置（运行时生效，重启后需写入 .env）"""
    if req.dashscope_api_key is not None:
        settings.dashscope_api_key = req.dashscope_api_key
    if req.moonshot_api_key is not None:
        settings.moonshot_api_key = req.moonshot_api_key
        # 切换 Moonshot key 时重置 client 单例
        from backend.services import llm_adapter
        llm_adapter._moonshot_client = None
    if req.llm_model_map is not None:
        settings.llm_model_map = req.llm_model_map
    if req.llm_model_qa is not None:
        settings.llm_model_qa = req.llm_model_qa
    if req.embedding_model is not None:
        settings.embedding_model = req.embedding_model
    if req.rerank_model is not None:
        settings.rerank_model = req.rerank_model

    # 持久化到 .env
    _persist_env()

    return _build_response()


def _persist_env():
    """将当前配置写入 .env 文件"""
    env_path = os.path.join(os.getcwd(), ".env")
    lines = {
        "DASHSCOPE_API_KEY": settings.dashscope_api_key,
        "DASHSCOPE_BASE_URL": settings.dashscope_base_url,
        "MOONSHOT_API_KEY": settings.moonshot_api_key,
        "MOONSHOT_BASE_URL": settings.moonshot_base_url,
        "LLM_MODEL_MAP": settings.llm_model_map,
        "LLM_MODEL_QA": settings.llm_model_qa,
        "EMBEDDING_MODEL": settings.embedding_model,
        "RERANK_MODEL": settings.rerank_model,
        "DATA_DIR": settings.data_dir,
    }
    with open(env_path, "w", encoding="utf-8") as f:
        for k, v in lines.items():
            f.write(f"{k}={v}\n")
