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
    # 注意：map 模型在话题切分里被高频调用，是 token 消耗大头。
    # qwen3.5-flash 比 qwen3.5-plus 输入便宜 4x、输出便宜 2.4x，限流也更宽松。
    # 当前所有调用 llm_model_map 的位置都已经传 enable_thinking=False，避免 flash
    # 默认开思考模式吃额外的思考链 token。
    llm_model_map: str = "qwen3.5-flash"
    llm_model_qa: str = "qwen3.6-plus"
    # 子 Agent 模型（research 工具委派给的 sub-agent 用的模型）。
    # 空字符串 = 跟随 llm_model_qa；推荐用便宜模型如 qwen3.5-plus 或 qwen3.5-flash。
    # 主 Agent 给 research 写详细 task，弱模型也能完成。
    llm_model_sub_agent: str = ""

    # 显式缓存开关（仅 qwen 系列生效，kimi 走自家缓存机制不受影响）
    # qwen 隐式缓存实测命中率近 0%（DashScope 路由策略保守），
    # 改成显式 cache_control 后命中率 99%+，命中价 10%。
    enable_qwen_explicit_cache: bool = True

    # Embedding / Rerank
    embedding_model: str = "text-embedding-v4"
    rerank_model: str = "qwen3-rerank"

    # 数据目录
    data_dir: str = "./data"

    # Telegram 直连同步代理
    # 国内访问 Telegram 服务器（149.154.0.0/16 等）需要代理
    # 格式：socks5://127.0.0.1:7891 或 http://127.0.0.1:7890
    # 留空时回退读取 HTTPS_PROXY / ALL_PROXY 环境变量；都没有则直连
    telegram_proxy: str = ""

    @property
    def effective_sub_agent_model(self) -> str:
        """子 Agent 实际使用的模型：未显式配置时跟随主 QA 模型"""
        return self.llm_model_sub_agent or self.llm_model_qa

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
