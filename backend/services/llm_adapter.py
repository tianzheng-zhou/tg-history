import httpx
from openai import AsyncOpenAI

from backend.config import settings


def _get_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=settings.dashscope_api_key,
        base_url=settings.dashscope_base_url,
    )


async def chat(
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
) -> str:
    """统一对话接口，返回纯文本"""
    client = _get_client()
    model = model or settings.llm_model_qa
    resp = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content or ""


async def chat_stream(
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
):
    """流式对话，yield 每个 token"""
    client = _get_client()
    model = model or settings.llm_model_qa
    stream = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
    )
    async for chunk in stream:
        delta = chunk.choices[0].delta
        if delta.content:
            yield delta.content


async def embed(texts: list[str], model: str | None = None) -> list[list[float]]:
    """批量文本向量化"""
    client = _get_client()
    model = model or settings.embedding_model
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
    async with httpx.AsyncClient(timeout=30) as client:
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
