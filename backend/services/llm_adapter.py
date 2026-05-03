import asyncio

import httpx
from openai import AsyncOpenAI

from backend.config import settings

# 分 Provider 并发控制
# DashScope RPM 30,000（含百炼直供 kimi/），官方 Moonshot 并发限制 = 3
_DASHSCOPE_CHAT_SEM = asyncio.Semaphore(10)
_MOONSHOT_CHAT_SEM = asyncio.Semaphore(3)
_EMBED_SEM = asyncio.Semaphore(20)

# ---------- 多 Provider 单例 client ----------

_dashscope_client: AsyncOpenAI | None = None
_moonshot_client: AsyncOpenAI | None = None


def _make_http_client(timeout: float = 180.0) -> httpx.AsyncClient:
    """创建禁用系统代理的 httpx client（国内 API 走代理只会变慢）"""
    return httpx.AsyncClient(
        trust_env=False,
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=50,
            keepalive_expiry=30.0,
        ),
        timeout=httpx.Timeout(timeout, connect=10.0),
    )


def _get_client() -> AsyncOpenAI:
    """DashScope client（兼容旧调用）"""
    global _dashscope_client
    if _dashscope_client is None:
        _dashscope_client = AsyncOpenAI(
            api_key=settings.dashscope_api_key,
            base_url=settings.dashscope_base_url,
            http_client=_make_http_client(),
        )
    return _dashscope_client


def _get_moonshot_client() -> AsyncOpenAI:
    """Moonshot/Kimi client"""
    global _moonshot_client
    if _moonshot_client is None:
        _moonshot_client = AsyncOpenAI(
            api_key=settings.moonshot_api_key,
            base_url=settings.moonshot_base_url,
            http_client=_make_http_client(),
        )
    return _moonshot_client


# ---------- Provider 路由 ----------

KIMI_MODELS = {"kimi-k2.6", "kimi-k2.5", "kimi-k2-0905-preview",
               "kimi/kimi-k2.6", "kimi/kimi-k2.5"}


def is_kimi_model(model: str) -> bool:
    """判断是否为 Kimi 系列模型（含官方 Moonshot 和百炼直供）"""
    return model.startswith("kimi-") or model.startswith("kimi/")


def _is_dashscope_kimi(model: str) -> bool:
    """判断是否为百炼直供 Kimi（kimi/ 前缀，走 DashScope client）"""
    return model.startswith("kimi/")


def get_client_for_model(model: str) -> AsyncOpenAI:
    """根据模型名称返回对应 provider 的 client"""
    if is_kimi_model(model) and not _is_dashscope_kimi(model):
        return _get_moonshot_client()  # 官方 Moonshot API
    return _get_client()  # DashScope（含 kimi/ 百炼直供）


def get_chat_semaphore(model: str) -> asyncio.Semaphore:
    """获取模型对应 provider 的 chat 并发 semaphore"""
    if is_kimi_model(model) and not _is_dashscope_kimi(model):
        return _MOONSHOT_CHAT_SEM  # 官方 Moonshot 并发 = 3
    return _DASHSCOPE_CHAT_SEM  # DashScope（含 kimi/ 百炼直供）RPM 30,000


# ---------- 模型上下文窗口 ----------
#
# 数据来源：阿里云百炼 / Moonshot 官方模型列表（2026-04 时点）
# https://help.aliyun.com/zh/model-studio/models
# https://platform.moonshot.cn/docs/intro
#
# 注：百炼对很多模型实际给出"上下文长度 = 输入 + 输出"，输入上限通常略小于
# 上下文长度（如 qwen3.6-plus 1,000,000 上下文，最大输入 ~983K）。这里取
# 上下文长度作为"window"，因为我们只用 prompt_tokens 算占比。

MODEL_CONTEXT_WINDOW: dict[str, int] = {
    # ----- DashScope / Qwen 旗舰 -----
    # qwen3.6-plus: 上下文 1M（思考/非思考），输入 2 元/百万 token（≤256K）
    "qwen3.6-plus": 1_000_000,
    # qwen3.5-plus: 上下文 1M
    "qwen3.5-plus": 1_000_000,
    # qwen-plus: Qwen3 系列，上下文 ~1M（995,904 / 997,952，简化按 1M 显示）
    "qwen-plus": 1_000_000,
    # qwen3-max / qwen-max: 上下文 262,144（256K）
    "qwen3-max": 262_144,
    "qwen-max": 262_144,
    # ----- DashScope / Qwen Flash -----
    # qwen3.5-flash: 上下文 1M
    "qwen3.5-flash": 1_000_000,
    # qwen-flash: 上下文 ~1M（995,904 / 997,952）
    "qwen-flash": 1_000_000,
    # qwen-turbo: 旧名，已被 qwen-flash 替代，但仍可调用
    "qwen-turbo": 1_000_000,
    # ----- DashScope / Qwen Omni -----
    "qwen3.5-omni-plus": 262_144,
    "qwen3.5-omni-flash": 262_144,
    "qwen3-omni-flash": 65_536,
    # ----- Moonshot / Kimi（官方 + 百炼直供）-----
    # Kimi K2/K2.5/K2.6: 256K = 262,144 上下文
    "kimi-k2.6": 262_144,
    "kimi-k2.5": 262_144,
    "kimi-k2-0905-preview": 262_144,
    # 百炼直供（kimi/ 前缀）
    "kimi/kimi-k2.6": 262_144,
    "kimi/kimi-k2.5": 262_144,
}
DEFAULT_CONTEXT_WINDOW = 131_072  # 未知模型回落到 128K（保守）


def get_context_window(model: str) -> int:
    """获取模型的最大上下文窗口（tokens）。未知模型回落到默认 128K。

    匹配策略：
    1. 精确匹配
    2. prefix 匹配（处理 qwen3.6-plus-2026-04-02 这种带快照日期的）
    """
    if model in MODEL_CONTEXT_WINDOW:
        return MODEL_CONTEXT_WINDOW[model]
    # 按 key 长度倒序，优先匹配更长的 prefix（避免 qwen-plus 误匹配 qwen-plus-...）
    for key in sorted(MODEL_CONTEXT_WINDOW.keys(), key=len, reverse=True):
        if model.startswith(key):
            return MODEL_CONTEXT_WINDOW[key]
    return DEFAULT_CONTEXT_WINDOW


# ---------- 模型定价（元 / 百万 token） ----------
#
# 数据来源：https://help.aliyun.com/zh/model-studio/model-pricing（2026-05 时点）
# (input, output, cached_input)  —— cached_input 为 0 表示不支持/无折扣
MODEL_PRICING: dict[str, tuple[float, float, float]] = {
    # 百炼直供 Kimi（隐式缓存）
    "kimi/kimi-k2.6": (6.5, 27.0, 6.5 * 0.169),   # 缓存 16.9%
    "kimi/kimi-k2.5": (4.0, 21.0, 4.0 * 0.175),    # 缓存 17.5%
    # 阿里云部署 Kimi
    "kimi-k2.6": (6.5, 27.0, 0),
    "kimi-k2.5": (4.0, 21.0, 0),
    # Qwen Plus
    "qwen3.6-plus": (2.0, 12.0, 0),
    "qwen3.5-plus": (0.8, 4.8, 0),
    "qwen-plus": (0.8, 2.0, 0),
    # Qwen Flash
    "qwen3.5-flash": (0.0, 0.0, 0),   # 免费
    "qwen-flash": (0.0, 0.0, 0),
    # Embedding / Rerank
    "text-embedding-v4": (0.5, 0, 0),
    "text-embedding-v3": (0.5, 0, 0),
    "qwen3-rerank": (0.5, 0, 0),
    "gte-rerank-v2": (0.8, 0, 0),
}


def _match_pricing(model: str) -> tuple[float, float, float]:
    """获取模型定价，支持精确匹配和 prefix 匹配。未知模型返回 (0,0,0)。"""
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    for key in sorted(MODEL_PRICING.keys(), key=len, reverse=True):
        if model.startswith(key):
            return MODEL_PRICING[key]
    return (0, 0, 0)


def parse_usage(usage) -> dict:
    """从 OpenAI usage 对象提取 token 计数（含 cached_tokens）。

    兼容 DashScope 的 prompt_tokens_details.cached_tokens。
    usage 可以是对象（有属性）或 dict。
    """
    if usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0}

    def _get(obj, key, default=0):
        if isinstance(obj, dict):
            return obj.get(key, default) or default
        return getattr(obj, key, default) or default

    prompt = _get(usage, "prompt_tokens")
    completion = _get(usage, "completion_tokens")
    total = _get(usage, "total_tokens")

    # cached_tokens 藏在 prompt_tokens_details 里
    cached = 0
    details = _get(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = _get(details, "cached_tokens")

    return {
        "prompt_tokens": int(prompt),
        "completion_tokens": int(completion),
        "total_tokens": int(total),
        "cached_tokens": int(cached),
    }


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int,
                  cached_tokens: int = 0) -> float:
    """根据模型定价估算费用（元）。

    cached_tokens 是 prompt_tokens 中命中缓存的部分，
    按缓存价计费，剩余按正常输入价。
    """
    input_price, output_price, cache_price = _match_pricing(model)
    per_m = 1_000_000

    uncached_input = max(prompt_tokens - cached_tokens, 0)
    cost = (uncached_input / per_m) * input_price
    cost += (cached_tokens / per_m) * cache_price
    cost += (completion_tokens / per_m) * output_price
    return round(cost, 6)


def kimi_chat_kwargs(model: str, enable_thinking: bool | None) -> dict:
    """构建 Kimi 模型的特殊参数（同时支持官方 Moonshot 和百炼直供）

    - 官方 Moonshot: thinking: {type: "enabled"/"disabled"}
    - 百炼直供 kimi/: enable_thinking: true/false
    - 温度固定：思考=1.0，非思考=0.6
    """
    kwargs: dict = {}
    dashscope = _is_dashscope_kimi(model)
    if enable_thinking is False:
        if dashscope:
            kwargs["extra_body"] = {"enable_thinking": False}
        else:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        kwargs["temperature"] = 0.6
    else:
        if dashscope:
            kwargs["extra_body"] = {"enable_thinking": True}
        else:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        kwargs["temperature"] = 1.0
    return kwargs


# ---------- 统一对话接口 ----------

async def chat(
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    enable_thinking: bool | None = None,
) -> str:
    """统一对话接口，返回纯文本"""
    model = model or settings.llm_model_qa
    client = get_client_for_model(model)
    kwargs = dict(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if is_kimi_model(model):
        kwargs.update(kimi_chat_kwargs(model, enable_thinking))
    elif enable_thinking is not None:
        kwargs["extra_body"] = {"enable_thinking": enable_thinking}
    async with get_chat_semaphore(model):
        resp = await client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


async def chat_stream(
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
):
    """流式对话，yield 每个 token"""
    model = model or settings.llm_model_qa
    client = get_client_for_model(model)
    kwargs = dict(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
    )
    if is_kimi_model(model):
        kwargs.update(kimi_chat_kwargs(model, False))
    async with get_chat_semaphore(model):
        stream = await client.chat.completions.create(**kwargs)
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content


async def embed(texts: list[str], model: str | None = None) -> list[list[float]]:
    """批量文本向量化（始终用 DashScope）"""
    client = _get_client()
    model = model or settings.embedding_model
    async with _EMBED_SEM:
        resp = await client.embeddings.create(model=model, input=texts)
    return [item.embedding for item in resp.data]


async def rerank(
    query: str,
    documents: list[str],
    top_n: int = 5,
    model: str | None = None,
) -> list[dict]:
    """调用 DashScope Rerank API（HTTP 直接调用，非 OpenAI 兼容）"""
    model = model or settings.rerank_model
    url = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
    headers = {
        "Authorization": f"Bearer {settings.dashscope_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "input": {
            "query": query,
            "documents": documents,
        },
        "parameters": {
            "top_n": top_n,
            "return_documents": True,
        },
    }
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    results = data.get("output", {}).get("results", [])
    return [
        {
            "index": r["index"],
            "relevance_score": r["relevance_score"],
            "text": r.get("document", {}).get("text", ""),
        }
        for r in results
    ]
