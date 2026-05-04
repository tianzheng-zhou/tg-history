"""Telegram Desktop 导出 JSON 解析器。

50w+ 条消息的导出 JSON 可能达到 500MB-2GB，stdlib ``json.load`` 会把
全量数据加载到 Python 堆中（AST 放大 3-5x），达到 2-8GB 内存几乎必然 OOM。
这里用 ``ijson`` 做按 chat 流式解析：

- **全量导出** ``{"chats": {"list": [...]}}``：用 ``ijson.items`` 流式迭代各 chat，
  峰值内存降到 O(单 chat 最大 messages)。
- **单群导出** ``{"name": ..., "messages": [...]}``：先事件流拿 header，
  再用 ``ijson.items(f, "messages.item")`` 流式迭代各 message，峰值内存
  正比于 解析后的 parsed list（而非原始 JSON AST）。
底层 iterator 是 ``iter_export_chats``；``parse_export_file`` 是其上的
``list()`` 包装，保留旧接口给不需要 streaming 的调用方。
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import ijson


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


def _finalize_chat(
    chat_name: str, chat_id: str, messages: list[dict]
) -> dict | None:
    """将解析后的 messages 组装成标准的 chat 输出。没有有效消息返回 None。"""
    if not messages:
        return None
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


def _parse_single_chat(data: dict) -> dict | None:
    """解析单个已 load 到内存的群聊数据块（旧同步路径）。"""
    chat_name = data.get("name", "Unknown")
    chat_id = str(data.get("id", ""))
    if not chat_id:
        return None
    raw_messages = data.get("messages", [])
    messages: list[dict] = []
    for raw in raw_messages:
        parsed = parse_message(raw, chat_id)
        if parsed:
            messages.append(parsed)
    return _finalize_chat(chat_name, chat_id, messages)


def _detect_top_format(path: Path) -> str:
    """事件流扫顶层结构，不一次性 load 整份。

    返回值：
        - ``"bulk"``      : ``{"chats": {"list": [...]}}``
        - ``"bulk_list"`` : ``{"chats": [...]}`` 变体
        - ``"single"``    : ``{"name": ..., "messages": [...]}`` 或其他
    """
    with open(path, "rb") as f:
        events = ijson.parse(f)
        # 顶层必须是 start_map
        first = next(events, None)
        if not first or first[1] != "start_map":
            return "single"
        # 按顺序扫顶层 map_key；找到 chats / messages / name 就判决
        for prefix, event, value in events:
            if prefix == "" and event == "map_key":
                if value == "chats":
                    # 下一个 event 决定 chats 是 dict 还是 list
                    nxt = next(events, None)
                    if nxt and nxt[1] == "start_map":
                        return "bulk"
                    if nxt and nxt[1] == "start_array":
                        return "bulk_list"
                    return "bulk"
                if value in ("messages", "name", "id", "type"):
                    return "single"
    return "single"


def _iter_chats_bulk(path: Path, item_prefix: str) -> Iterator[dict]:
    """全量导出：用 ijson.items 流式迭代 chat。每个 chat dict 还是先构建完
    才 yield，但峰值仅为单 chat。调用方处理完后应立即 ``del`` 释放。"""
    with open(path, "rb") as f:
        for chat in ijson.items(f, item_prefix):
            parsed = _parse_single_chat(chat)
            if parsed:
                yield parsed
            # 下一次迭代前明确删除引用，加快 GC
            del chat


def _iter_chats_single(path: Path) -> Iterator[dict]:
    """单群导出：两遍读文件。

    第一遍 ijson.parse 事件流拿顶层 meta（name / id / type），碰到 messages
    数组就停（不继续读）。第二遍 ijson.items(\"messages.item\") 流式迭代
    消息，边 parse 边添加到 batch list。最后一次性 yield 整个 chat。

    对 500k 条/1GB JSON：峰值内存从 stdlib 的 ~3-4GB 降到 ~1-2GB
    （仅保留 parsed messages list，不再保留原始 AST）。
    """
    chat_name = "Unknown"
    chat_id = ""
    # 第一遍：拿 header
    with open(path, "rb") as f:
        for prefix, event, value in ijson.parse(f):
            if prefix == "name" and event == "string":
                chat_name = value or chat_name
            elif prefix == "id" and event in ("number", "string"):
                chat_id = str(value)
            elif prefix == "messages" and event == "start_array":
                break  # 已拿到 header，停止 event 流
    if not chat_id:
        return

    # 第二遍：stream messages
    messages: list[dict] = []
    with open(path, "rb") as f:
        for raw in ijson.items(f, "messages.item"):
            parsed = parse_message(raw, chat_id)
            if parsed:
                messages.append(parsed)
            del raw
    result = _finalize_chat(chat_name, chat_id, messages)
    if result:
        yield result


def iter_export_chats(file_path: str | Path) -> Iterator[dict]:
    """按 chat 流式迭代 Telegram 导出文件。

    与 ``parse_export_file`` 的区别：本函数是 generator，消费方以需求拉取
    每个 chat。适用于 500k 级别导出：全量 export 峰值降到 O(单 chat)；
    单群 export 峰值仅为 parsed messages list（而非原始 JSON AST）。

    调用方每处理完一个 chat 应立即 ``del parsed`` 释放，否则
    累积还是会占满内存。
    """
    path = Path(file_path)
    fmt = _detect_top_format(path)
    if fmt == "bulk":
        yield from _iter_chats_bulk(path, "chats.list.item")
    elif fmt == "bulk_list":
        yield from _iter_chats_bulk(path, "chats.item")
    else:
        yield from _iter_chats_single(path)


def parse_export_file(file_path: str | Path) -> list[dict]:
    """解析 Telegram 导出文件，一次性返回所有 chat。

    **注意：内部仍是流式解析**（调 ``iter_export_chats``），但最后会把所有 chat
    累积到一个 list。对于 50w 级别导出，建议直接用 ``iter_export_chats``
    按 chat 流式处理，否则 list 中的 chat dict 仍会全部占内存。

    保留此接口用于 folder_scan 内的小文件 / 测试代码的向后兼容。

    Returns:
        list[dict] — 每个元素: {"chat_name", "chat_id", "messages", "date_range"}
    """
    return list(iter_export_chats(file_path))
