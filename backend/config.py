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
    # 注意：map 模型在话题切分/Map 摘要里被高频调用，是 token 消耗大头。
    # qwen3.5-flash 比 qwen3.5-plus 输入便宜 4x、输出便宜 2.4x，限流也更宽松。
    # 当前所有调用 llm_model_map 的位置都已经传 enable_thinking=False，避免 flash
    # 默认开思考模式吃额外的思考链 token。
    llm_model_map: str = "qwen3.5-flash"
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
