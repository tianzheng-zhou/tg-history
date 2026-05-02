"""Map-Reduce 摘要引擎"""

import json
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

CHUNK_SIZE = 80  # 每组消息数


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
    """按话题分组，若话题内消息过多则再按 CHUNK_SIZE 切分"""
    topic_groups: dict[int | None, list[Message]] = {}
    for m in messages:
        key = m.topic_id
        if key not in topic_groups:
            topic_groups[key] = []
        topic_groups[key].append(m)

    chunks = []
    for group in topic_groups.values():
        if len(group) <= CHUNK_SIZE:
            chunks.append(group)
        else:
            for i in range(0, len(group), CHUNK_SIZE):
                chunks.append(group[i : i + CHUNK_SIZE])

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
        max_tokens=2048,
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
        max_tokens=4096,
    )
    return result


async def run_summarize(db: Session, chat_id: str, progress: dict | None = None) -> dict[str, str]:
    """执行完整的 Map-Reduce 摘要流程，可选 progress dict 实时上报进度"""
    messages = (
        db.query(Message)
        .filter(Message.chat_id == chat_id)
        .order_by(Message.date)
        .all()
    )

    if not messages:
        return {}

    # Map 阶段
    chunks = _chunk_messages(messages)
    if progress is not None:
        progress["map_total"] = len(chunks)
        progress["map_done"] = 0
        progress["stage"] = "map"

    chunk_summaries = []
    for chunk in chunks:
        summary = await _map_summarize(chunk)
        chunk_summaries.append(summary)
        if progress is not None:
            progress["map_done"] += 1

    # Reduce 阶段
    if progress is not None:
        progress["stage"] = "reduce"

    full_report = await _reduce_summarize(chunk_summaries)

    # 保存为单个报告（category="full"）
    report = SummaryReport(
        chat_id=chat_id,
        category="full",
        content=full_report,
        generated_at=datetime.utcnow(),
        chunk_summaries=json.dumps(chunk_summaries, ensure_ascii=False),
    )
    db.add(report)
    db.commit()

    return {"full": full_report}
