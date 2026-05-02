import asyncio

import httpx
from openai import AsyncOpenAI

from backend.config import settings

# 全局并发控制
_CHAT_SEM = asyncio.Semaphore(30)
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

KIMI_MODELS = {"kimi-k2.6", "kimi-k2.5", "kimi-k2-0905-preview"}


def is_kimi_model(model: str) -> bool:
    return model.startswith("kimi-")


def get_client_for_model(model: str) -> AsyncOpenAI:
    """根据模型名称返回对应 provider 的 client"""
    if is_kimi_model(model):
        return _get_moonshot_client()
    return _get_client()


def _kimi_chat_kwargs(model: str, temperature: float, enable_thinking: bool | None) -> dict:
    """构建 Kimi 模型的特殊参数"""
    kwargs: dict = {}
    # Kimi K2.6/K2.5 温度只能是 1.0（思考）或 0.6（非思考），不能自定义
    if enable_thinking is False:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        kwargs["temperature"] = 0.6
    else:
        # 默认开启思考
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
        kwargs.update(_kimi_chat_kwargs(model, temperature, enable_thinking))
    elif enable_thinking is not None:
        kwargs["extra_body"] = {"enable_thinking": enable_thinking}
    async with _CHAT_SEM:
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
        kwargs.update(_kimi_chat_kwargs(model, temperature, False))
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
