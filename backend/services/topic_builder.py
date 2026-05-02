"""话题树构建：回复链 + LLM 语义切分"""

import asyncio
import json
import logging
from collections import defaultdict

from sqlalchemy.orm import Session

from backend.config import settings
from backend.models.database import Message, Topic
from backend.services import llm_adapter

logger = logging.getLogger(__name__)

# LLM 每批处理的消息数上限
BATCH_SIZE = 300
# 批次间重叠消息数（防止话题在边界被截断）
OVERLAP = 50

SPLIT_PROMPT = """你是一个聊天记录分析助手。下面是一段群聊消息，每条消息前有编号 [N]。

请你根据**语义和话题变化**将这些消息分成若干个话题段落。
- 同一个讨论话题的消息归为一组
- 话题切换的地方就是分割点
- 每个话题给一个简短标题（10字以内）

输出格式（JSON数组）：
```json
[{"title": "话题标题", "start": 起始编号, "end": 结束编号}, ...]
```

只输出JSON，不要其他内容。

---
{messages}"""

MERGE_CHECK_PROMPT = """判断以下两段相邻群聊消息是否属于**同一个话题**。

片段A 标题: {title_a}
片段A 最后几条消息:
{tail_a}

---

片段B 标题: {title_b}
片段B 开头几条消息:
{head_b}

这两个片段是否在讨论同一个话题？只回答 "是" 或 "否"，不要其他内容。"""


def _format_for_split(msgs: list[Message]) -> tuple[str, dict[int, Message]]:
    """格式化消息用于 LLM 切分，返回文本和编号→消息映射"""
    lines = []
    idx_map = {}
    idx = 0
    for m in msgs:
        text = (m.text_plain or "").strip()
        if not text:
            continue
        date_str = m.date.strftime("%m-%d %H:%M") if m.date else "?"
        sender = m.sender or "unknown"
        lines.append(f"[{idx}] [{date_str}] {sender}: {text}")
        idx_map[idx] = m
        idx += 1
    return "\n".join(lines), idx_map


async def _llm_split(msgs: list[Message], progress: dict | None = None) -> list[dict]:
    """调用 LLM 对消息进行语义切分，返回 [{title, messages: [Message]}]
    
    采用双向重叠窗口 + 跨批合并：
    - 每批 LLM 看到 BATCH_SIZE 条（含左右 OVERLAP 条上下文）
    - 只认领中间 claim_size 条
    - 跨批边界话题：两批都能看到完整上下文，各自返回的 segment 合并
    """
    text, idx_map = _format_for_split(msgs)
    if not idx_map:
        return []

    all_indices = sorted(idx_map.keys())
    n = len(all_indices)
    claim_size = BATCH_SIZE - 2 * OVERLAP  # 每批实际认领消息数
    if claim_size < 1:
        claim_size = BATCH_SIZE

    batch_starts = list(range(0, n, claim_size))
    total_batches = len(batch_starts)
    if progress is not None:
        progress["topic_total"] = total_batches
        progress["topic_done"] = 0

    # 所有批次并发处理（受全局 semaphore 限流，无需此处再控制）
    async def _process_batch(batch_num: int, claim_start: int) -> list[dict]:
        claim_end = min(claim_start + claim_size, n)
        visible_start = max(0, claim_start - OVERLAP)
        visible_end = min(n, claim_end + OVERLAP)

        batch_indices = all_indices[visible_start:visible_end]
        claim_set = set(all_indices[claim_start:claim_end])

        batch_lines = []
        for i in batch_indices:
            m = idx_map[i]
            text_plain = (m.text_plain or "").strip()
            date_str = m.date.strftime("%m-%d %H:%M") if m.date else "?"
            sender = m.sender or "unknown"
            batch_lines.append(f"[{i}] [{date_str}] {sender}: {text_plain}")

        batch_text = "\n".join(batch_lines)
        prompt = SPLIT_PROMPT.replace("{messages}", batch_text)

        try:
            resp = await llm_adapter.chat(
                messages=[{"role": "user", "content": prompt}],
                model=settings.llm_model_map,
                temperature=0.1,
                enable_thinking=False,
            )
            resp = resp.strip()
            if resp.startswith("```"):
                resp = resp.split("\n", 1)[1] if "\n" in resp else resp[3:]
                resp = resp.rsplit("```", 1)[0]
            segments = json.loads(resp)
        except Exception as e:
            logger.warning(f"LLM 语义切分失败, 回退到整批: {e}")
            segments = [{"title": "未分类", "start": batch_indices[0], "end": batch_indices[-1]}]

        if progress is not None:
            progress["topic_done"] = progress.get("topic_done", 0) + 1

        batch_segments = []
        claimed_in_batch: set[int] = set()
        for seg in segments:
            start = seg.get("start", 0)
            end = seg.get("end", 0)
            claimed_msgs = [idx_map[i] for i in range(start, end + 1)
                            if i in idx_map and i in claim_set]
            if claimed_msgs:
                for i in range(start, end + 1):
                    if i in claim_set:
                        claimed_in_batch.add(i)
                batch_segments.append({
                    "title": seg.get("title", ""),
                    "llm_start": start,
                    "llm_end": end,
                    "messages": claimed_msgs,
                })

        unclaimed = [i for i in sorted(claim_set) if i not in claimed_in_batch]
        if unclaimed:
            unclaimed_msgs = [idx_map[i] for i in unclaimed]
            if batch_segments:
                batch_segments[-1]["messages"].extend(unclaimed_msgs)
            else:
                batch_segments.append({
                    "title": "未分类",
                    "llm_start": unclaimed[0],
                    "llm_end": unclaimed[-1],
                    "messages": unclaimed_msgs,
                })

        return batch_segments

    all_batches = await asyncio.gather(*[
        _process_batch(i, start) for i, start in enumerate(batch_starts)
    ])

    # 跨批合并：批次 N 最后一个 seg 与批次 N+1 第一个 seg 如果 LLM 范围重叠，视为同一话题
    # 未自动合并的批次边界，标记为需要 LLM 检查
    results: list[dict] = []
    check_indices: list[int] = []  # results[i] 与 results[i+1] 是批次边界且未自动合并

    for batch_num, batch_segs in enumerate(all_batches):
        for seg_idx, seg in enumerate(batch_segs):
            is_last_of_batch = seg_idx == len(batch_segs) - 1
            is_first_of_batch = seg_idx == 0

            if (is_first_of_batch and batch_num > 0
                    and results and results[-1].get("_tail")):
                prev = results[-1]
                if seg["llm_start"] <= prev["_llm_end"] and seg["llm_end"] >= prev["_llm_start"]:
                    prev["messages"].extend(seg["messages"])
                    prev["_llm_end"] = max(prev["_llm_end"], seg["llm_end"])
                    prev["_tail"] = is_last_of_batch
                    continue
                else:
                    # 自动合并失败 → 标记为 LLM 检查候选
                    check_indices.append(len(results) - 1)
                    prev.pop("_tail", None)
                    prev.pop("_llm_start", None)
                    prev.pop("_llm_end", None)

            results.append({
                "title": seg["title"],
                "messages": seg["messages"],
                "_llm_start": seg["llm_start"],
                "_llm_end": seg["llm_end"],
                "_tail": is_last_of_batch and batch_num < len(all_batches) - 1,
            })

    # LLM 合并检查：对边界处的相邻话题并发判断是否同一话题
    if check_indices:
        await _llm_merge_check(results, check_indices)

    return [{"title": r["title"], "messages": r["messages"]} for r in results]


def _format_snippet(msgs: list[Message], limit: int = 8) -> str:
    """格式化消息片段（用于 merge check）"""
    lines = []
    for m in msgs[:limit]:
        text = (m.text_plain or "").strip()
        if text:
            sender = m.sender or "?"
            lines.append(f"{sender}: {text[:100]}")
    return "\n".join(lines) if lines else "(无文本)"


async def _llm_merge_check(results: list[dict], check_indices: list[int]) -> None:
    """并发检查相邻话题是否应合并，就地修改 results"""
    async def _check_one(idx: int) -> tuple[int, bool]:
        a = results[idx]
        b = results[idx + 1]
        prompt = MERGE_CHECK_PROMPT.format(
            title_a=a.get("title", ""),
            tail_a=_format_snippet(a["messages"][-8:]),
            title_b=b.get("title", ""),
            head_b=_format_snippet(b["messages"][:8]),
        )
        try:
            resp = await llm_adapter.chat(
                messages=[{"role": "user", "content": prompt}],
                model=settings.llm_model_map,
                temperature=0.0,
                enable_thinking=False,
            )
            return idx, "是" in resp.strip()[:5]
        except Exception as e:
            logger.warning(f"合并检查失败: {e}")
            return idx, False

    tasks = [_check_one(i) for i in check_indices]
    decisions = await asyncio.gather(*tasks)

    # 需要合并的下标（降序处理避免索引变化）
    to_merge = sorted([i for i, should in decisions if should], reverse=True)
    for idx in to_merge:
        if idx + 1 < len(results):
            results[idx]["messages"].extend(results[idx + 1]["messages"])
            del results[idx + 1]


async def build_topics(db: Session, chat_id: str, progress: dict | None = None) -> int:
    """为指定群聊构建话题分组，返回话题数量。"""
    messages = (
        db.query(Message)
        .filter(Message.chat_id == chat_id)
        .order_by(Message.date)
        .all()
    )
    if not messages:
        return 0

    # 1. 基于 reply_to_id 构建回复链
    reply_chains: dict[int, int] = {}
    children: dict[int, list[int]] = defaultdict(list)
    msg_map: dict[int, Message] = {m.id: m for m in messages}

    for m in messages:
        if m.reply_to_id and m.reply_to_id in msg_map:
            children[m.reply_to_id].append(m.id)

    def find_root(mid: int) -> int:
        visited = set()
        cur = mid
        while cur in msg_map and msg_map[cur].reply_to_id and msg_map[cur].reply_to_id in msg_map:
            if cur in visited:
                break
            visited.add(cur)
            cur = msg_map[cur].reply_to_id
        return cur

    for m in messages:
        if m.reply_to_id and m.reply_to_id in msg_map:
            reply_chains[m.id] = find_root(m.id)

    # 2. 按回复链分组
    reply_groups: list[dict] = []
    assigned: set[int] = set()
    reply_topic_map: dict[int, list[Message]] = defaultdict(list)

    for m in messages:
        root = reply_chains.get(m.id)
        if root is not None:
            reply_topic_map[root].append(m)
            assigned.add(m.id)
        elif m.id in children:
            reply_topic_map[m.id].append(m)
            assigned.add(m.id)

    for root_id, group_msgs in reply_topic_map.items():
        reply_groups.append({"title": None, "messages": group_msgs})

    # 3. 未关联消息用 LLM 语义切分
    unassigned = [m for m in messages if m.id not in assigned]
    semantic_groups = await _llm_split(unassigned, progress) if unassigned else []

    all_groups = reply_groups + semantic_groups

    # 4. 删除旧话题记录
    db.query(Topic).filter(Topic.chat_id == chat_id).delete()

    # 5. 写入新话题
    topic_count = 0
    for group in all_groups:
        group_msgs = group["messages"]
        if not group_msgs:
            continue
        dates = [m.date for m in group_msgs if m.date]
        participants = set(m.sender for m in group_msgs if m.sender)

        topic = Topic(
            chat_id=chat_id,
            root_message_id=None,
            start_date=min(dates) if dates else None,
            end_date=max(dates) if dates else None,
            participant_count=len(participants),
            message_count=len(group_msgs),
            summary=group.get("title"),
        )
        db.add(topic)
        db.flush()

        for m in group_msgs:
            m.topic_id = topic.id

        topic_count += 1

    db.commit()
    return topic_count
