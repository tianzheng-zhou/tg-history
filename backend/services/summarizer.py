"""Map-Reduce 摘要引擎"""

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from backend.config import settings
from backend.models.database import Message, SummaryReport, Topic
from backend.services import llm_adapter

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

CATEGORIES = ["tech", "business", "resource", "decision", "opinion"]
CATEGORY_LABELS = {
    "tech": "技术信息",
    "business": "商业信息",
    "resource": "资源与链接",
    "decision": "关键决策与待办",
    "opinion": "重要观点与讨论",
}

CHUNK_SIZE = 200  # 每组消息数（合并小话题以减少 LLM 调用）


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _format_messages(messages: list[Message]) -> str:
    """将消息列表格式化为可读文本"""
    lines = []
    for m in messages:
        date_str = m.date.strftime("%Y-%m-%d %H:%M") if m.date else "未知时间"
        sender = m.sender or "未知"
        text = m.text_plain or ""
        if text:
            lines.append(f"[{date_str}] {sender}: {text}")
    return "\n".join(lines)


def _chunk_messages(messages: list[Message]) -> list[list[Message]]:
    """按话题分组，并把多个小话题合并成接近 CHUNK_SIZE 的组以减少 LLM 调用次数。
    
    - 大话题（>= CHUNK_SIZE）单独切分
    - 小话题按时间顺序累积，达到 CHUNK_SIZE 才输出
    """
    # 按话题分组（保持时间顺序）
    topic_groups: dict[int | None, list[Message]] = {}
    topic_order: list[int | None] = []
    for m in messages:
        key = m.topic_id
        if key not in topic_groups:
            topic_groups[key] = []
            topic_order.append(key)
        topic_groups[key].append(m)

    chunks: list[list[Message]] = []
    buffer: list[Message] = []

    def flush_buffer():
        if buffer:
            chunks.append(list(buffer))
            buffer.clear()

    for key in topic_order:
        group = topic_groups[key]
        if len(group) >= CHUNK_SIZE:
            # 大话题：先冲掉小话题缓冲区，再切分大话题
            flush_buffer()
            for i in range(0, len(group), CHUNK_SIZE):
                chunks.append(group[i : i + CHUNK_SIZE])
        else:
            buffer.extend(group)
            if len(buffer) >= CHUNK_SIZE:
                flush_buffer()

    flush_buffer()
    return chunks


async def _map_summarize(chunk: list[Message]) -> str:
    """Map 阶段：对每组消息生成摘要"""
    prompt_template = _load_prompt("map_summary.txt")
    formatted = _format_messages(chunk)
    if not formatted.strip():
        return "无重要信息"

    prompt = prompt_template.replace("{messages_chunk}", formatted)
    result = await llm_adapter.chat(
        messages=[{"role": "user", "content": prompt}],
        model=settings.llm_model_map,
        temperature=0.2,
        enable_thinking=False,
    )
    return result


async def _reduce_summarize(summaries: list[str]) -> str:
    """Reduce 阶段：合并各段摘要为结构化报告"""
    prompt_template = _load_prompt("reduce_summary.txt")
    combined = "\n\n---\n\n".join(
        f"### 摘要片段 {i + 1}\n{s}" for i, s in enumerate(summaries) if s != "无重要信息"
    )
    if not combined.strip():
        return "该群聊暂无有价值的信息。"

    prompt = prompt_template.replace("{summaries}", combined)
    result = await llm_adapter.chat(
        messages=[{"role": "user", "content": prompt}],
        model=settings.llm_model_reduce,
        temperature=0.3,
        # 防御性关思考：用户若切到 flash 系列做 reduce，避免被默认开思考吃额外 token
        enable_thinking=False,
    )
    return result


async def run_summarize(db: Session, chat_id: str, progress: dict | None = None) -> dict[str, str]:
    """执行完整的 Map-Reduce 摘要流程，可选 progress dict 实时上报进度"""

    # 大群聊几十万条消息，全部拉到内存 + 切 chunk 都是 CPU/IO 同步活，派 thread
    def _load_and_chunk_sync():
        messages = (
            db.query(Message)
            .filter(Message.chat_id == chat_id)
            .order_by(Message.date)
            .all()
        )
        if not messages:
            return []
        return _chunk_messages(messages)

    chunks = await asyncio.to_thread(_load_and_chunk_sync)
    if not chunks:
        return {}
    if progress is not None:
        progress["map_total"] = len(chunks)
        progress["map_done"] = 0
        progress["stage"] = "map"

    # 并发 Map（受 llm_adapter 全局 semaphore 限流）
    async def _map_one(idx: int, chunk: list[Message]) -> tuple[int, str]:
        summary = await _map_summarize(chunk)
        if progress is not None:
            progress["map_done"] = progress.get("map_done", 0) + 1
        return idx, summary

    map_results = await asyncio.gather(*[_map_one(i, c) for i, c in enumerate(chunks)])
    map_results.sort(key=lambda x: x[0])
    chunk_summaries = [s for _, s in map_results]

    # Reduce 阶段
    if progress is not None:
        progress["stage"] = "reduce"

    full_report = await _reduce_summarize(chunk_summaries)

    # 解析 full 报告，按分类切分（与 reduce_summary.txt 中的标题一一对应）
    sections = _split_by_category(full_report)
    now = datetime.utcnow()
    summaries_json = json.dumps(chunk_summaries, ensure_ascii=False)

    # 保存到 db（commit fsync 是同步 IO，派 thread）
    def _save_sync():
        db.add(SummaryReport(
            chat_id=chat_id, category="full", content=full_report,
            generated_at=now, chunk_summaries=summaries_json,
        ))
        for cat_key, cat_content in sections.items():
            db.add(SummaryReport(
                chat_id=chat_id, category=cat_key, content=cat_content,
                generated_at=now,
            ))
        db.commit()

    await asyncio.to_thread(_save_sync)

    return {"full": full_report, **sections}


# 标题 → category key 映射
_CATEGORY_HEADERS = {
    "技术信息": "tech",
    "商业信息": "business",
    "资源与链接": "resource",
    "关键决策与待办": "decision",
    "重要观点与讨论": "opinion",
}


def _split_by_category(markdown: str) -> dict[str, str]:
    """把 reduce 阶段输出的 full markdown 按 5 个分类标题切分。
    
    匹配多种 markdown 标题格式，允许前置 emoji，例如：
      ## 🔧 技术信息
      ## 技术信息
      **技术信息**
      ### 💼 商业信息：
    """
    headers_pattern = "|".join(re.escape(h) for h in _CATEGORY_HEADERS.keys())
    # 匹配 ## / ### / **...** 标题，允许中间有任意非中文字符（emoji、空格、符号）
    pattern = re.compile(
        rf"^\s*(?:#{{1,4}}\s+|\*\*\s*)[^\u4e00-\u9fa5\n]*?({headers_pattern})[^\n]*$",
        re.MULTILINE,
    )

    matches = list(pattern.finditer(markdown))
    result = {v: "暂无内容" for v in _CATEGORY_HEADERS.values()}

    if not matches:
        return result

    for i, m in enumerate(matches):
        header_zh = m.group(1)
        cat_key = _CATEGORY_HEADERS.get(header_zh)
        if not cat_key:
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        body = markdown[start:end].strip()
        # 去掉末尾的 "---" 分隔线
        body = re.sub(r"\n*-{3,}\s*$", "", body).strip()
        if body and body not in ("暂无", "暂无内容", "无"):
            result[cat_key] = body
    return result
