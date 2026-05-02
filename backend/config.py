from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # DashScope (阿里云百炼)
    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # Moonshot (Kimi)
    moonshot_api_key: str = ""
    moonshot_base_url: str = "https://api.moonshot.cn/v1"

    # LLM 模型
    llm_model_map: str = "qwen3.5-plus"
    llm_model_reduce: str = "qwen3.6-plus"
    llm_model_qa: str = "qwen3.6-plus"

    # Embedding / Rerank
    embedding_model: str = "text-embedding-v4"
    rerank_model: str = "qwen3-rerank"

    # 数据目录
    data_dir: str = "./data"

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "app.db"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def chroma_dir(self) -> str:
        return str(Path(self.data_dir) / "chroma_db")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
