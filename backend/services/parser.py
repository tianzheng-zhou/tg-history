"""Telegram Desktop 导出 JSON 解析器"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def normalize_text(text_field: Any) -> tuple[str, list[dict]]:
    """将 Telegram 的 text 字段统一为 (纯文本, 实体列表)。

    text 字段可能是:
    - 纯字符串: "hello"
    - 混合数组: ["hello ", {"type": "link", "text": "https://..."}, " world"]
    """
    if text_field is None:
        return "", []

    if isinstance(text_field, str):
        return text_field, []

    if isinstance(text_field, list):
        parts: list[str] = []
        entities: list[dict] = []
        offset = 0
        for item in text_field:
            if isinstance(item, str):
                parts.append(item)
                offset += len(item)
            elif isinstance(item, dict):
                t = item.get("text", "")
                parts.append(t)
                entities.append({
                    "type": item.get("type", "unknown"),
                    "text": t,
                    "offset": offset,
                    "length": len(t),
                    "href": item.get("href"),
                })
                offset += len(t)
        return "".join(parts), entities

    return str(text_field), []


def parse_message(raw: dict, chat_id: str) -> dict | None:
    """解析单条消息，返回标准化字段字典。跳过 service 消息。"""
    if raw.get("type") != "message":
        return None

    text_plain, entities = normalize_text(raw.get("text"))

    # 跳过空消息（无文本且无媒体）
    media_type = raw.get("media_type")
    if not text_plain and not media_type:
        return None

    date_str = raw.get("date", "")
    try:
        date = datetime.fromisoformat(date_str) if date_str else None
    except ValueError:
        date = None

    return {
        "id": raw.get("id"),
        "chat_id": chat_id,
        "date": date,
        "sender": raw.get("from", raw.get("actor", "")),
        "sender_id": str(raw.get("from_id", raw.get("actor_id", ""))),
        "text": json.dumps(raw.get("text"), ensure_ascii=False) if raw.get("text") else "",
        "text_plain": text_plain,
        "reply_to_id": raw.get("reply_to_message_id"),
        "forwarded_from": raw.get("forwarded_from"),
        "media_type": media_type,
        "entities": entities or None,
    }


def parse_export_file(file_path: str | Path) -> dict:
    """解析整个 Telegram 导出文件。

    Returns:
        {
            "chat_name": str,
            "chat_id": str,
            "messages": list[dict],
            "date_range": str,
        }
    """
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    chat_name = data.get("name", "Unknown")
    chat_id = str(data.get("id", ""))

    raw_messages = data.get("messages", [])
    messages = []
    for raw in raw_messages:
        parsed = parse_message(raw, chat_id)
        if parsed:
            messages.append(parsed)

    # 计算时间范围
    dates = [m["date"] for m in messages if m.get("date")]
    if dates:
        date_min = min(dates).strftime("%Y-%m-%d")
        date_max = max(dates).strftime("%Y-%m-%d")
        date_range = f"{date_min} ~ {date_max}"
    else:
        date_range = "未知"

    return {
        "chat_name": chat_name,
        "chat_id": chat_id,
        "messages": messages,
        "date_range": date_range,
    }
