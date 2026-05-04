"""话题树构建：回复链 + LLM 语义切分

性能：本模块涉及大量 SQLAlchemy 查询 + Python 循环（reply chain 解析、
话题分组等），对几万条消息需要数秒到几十秒同步 CPU 时间。如果直接在
asyncio main loop 跑，会把所有 HTTP API 拖死（前端进度条都拉不到）。

方案：``build_topics`` / ``build_topics_incremental`` 是 thin async wrapper，
把同步重计算部分（``_build_topics_sync`` / ``_build_topics_incremental_sync``）
派到 thread pool 跑（``asyncio.to_thread``），LLM 调用通过
``asyncio.run_coroutine_threadsafe`` 派回 main loop（必须，``llm_adapter``
的模块级 Semaphore 绑定到 main loop）。
"""

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime

from sqlalchemy.orm import Session

from backend.config import settings
from backend.models.database import Message, SessionLocal, Topic
from backend.services import llm_adapter

logger = logging.getLogger(__name__)


def _await_on_loop(coro, main_loop: asyncio.AbstractEventLoop):
    """从 thread 内派一个协程到 main loop 跑，阻塞 thread 等待结果。"""
    future = asyncio.run_coroutine_threadsafe(coro, main_loop)
    return future.result()

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


MERGE_BOUNDARY_PROMPT = """判断以下两段相邻群聊消息是否属于**同一个话题**。

片段A 标题: {title_a}
片段A 最后几条消息:
{tail_a}

---

片段B 标题: {title_b}
片段B 开头几条消息:
{head_b}

这两个片段是否在讨论同一个话题？只回答 "是" 或 "否"，不要其他内容。"""


async def _check_boundary_merge(
    last_old_topic_msgs: list[Message],
    last_old_title: str,
    first_new_msgs: list[Message],
    first_new_title: str,
) -> bool:
    """判断"最后一个旧话题"与"第一组新消息"是否同一话题（1 次 LLM 调用）。"""
    if not last_old_topic_msgs or not first_new_msgs:
        return False
    prompt = MERGE_BOUNDARY_PROMPT.format(
        title_a=last_old_title or "(无标题)",
        tail_a=_format_snippet(last_old_topic_msgs[-8:]),
        title_b=first_new_title or "(无标题)",
        head_b=_format_snippet(first_new_msgs[:8]),
    )
    try:
        resp = await llm_adapter.chat(
            messages=[{"role": "user", "content": prompt}],
            model=settings.llm_model_map,
            temperature=0.0,
            enable_thinking=False,
        )
        return "是" in resp.strip()[:5]
    except Exception as e:
        logger.warning(f"边界合并检查失败: {e}")
        return False


def _build_topics_incremental_sync(
    chat_id: str, main_loop: asyncio.AbstractEventLoop, progress: dict | None = None
) -> tuple[int, set[int]]:
    """build_topics_incremental 的同步核心 —— 跑在 thread pool 里。

    LLM 调用（``_llm_split`` / ``_check_boundary_merge``）通过 ``_await_on_loop``
    派回 main loop。SQLAlchemy session 独立创建。
    """
    db = SessionLocal()
    try:
        # 0. 没有任何旧 topic → 走全量（也会派到 thread）
        has_existing_topic = (
            db.query(Topic).filter(Topic.chat_id == chat_id).limit(1).first() is not None
        )
        if not has_existing_topic:
            db.close()
            # _build_topics_sync 自己管理 session
            total = _build_topics_sync(chat_id, main_loop, progress)
            # 重新打开 session 拿 all_ids
            db = SessionLocal()
            all_ids = set(
                row[0]
                for row in db.query(Topic.id).filter(Topic.chat_id == chat_id).all()
            )
            return total, all_ids

        # 1. 找新消息（topic_id IS NULL）
        new_msgs = (
            db.query(Message)
            .filter(Message.chat_id == chat_id, Message.topic_id.is_(None))
            .order_by(Message.date)
            .all()
        )
        if not new_msgs:
            # 没有任何变更，调用方应跳过 embedding 步骤
            total = db.query(Topic).filter(Topic.chat_id == chat_id).count()
            return total, set()

        # 加载该 chat 所有 messages 用于 reply chain 解析
        all_msgs = (
            db.query(Message).filter(Message.chat_id == chat_id).all()
        )
        msg_map: dict[int, Message] = {m.id: m for m in all_msgs}

        changed_topic_ids: set[int] = set()

        # 2. 通过 reply chain 把新消息挂到旧 topic
        # 用 memoization 避免对同一长链重复爬：worst-case O(N) 而非 O(N*depth)
        topic_via_reply_cache: dict[int, int | None] = {}

        def _resolve_topic_via_reply(m: Message) -> int | None:
            """沿 reply_to_id 向上找到第一个有 topic_id 的祖先，返回其 topic_id。"""
            chain: list[int] = []
            cur = m
            while True:
                if cur.id in topic_via_reply_cache:
                    result = topic_via_reply_cache[cur.id]
                    break
                if cur.topic_id is not None:
                    result = cur.topic_id
                    break
                if not cur.reply_to_id or cur.reply_to_id not in msg_map or cur.reply_to_id == cur.id:
                    result = None
                    break
                chain.append(cur.id)
                if len(chain) > 64:  # 防御性深度限
                    result = None
                    break
                cur = msg_map[cur.reply_to_id]
            for nid in chain:
                topic_via_reply_cache[nid] = result
            topic_via_reply_cache[cur.id] = result
            return result

        unassigned: list[Message] = []
        topic_dirty: dict[int, dict] = {}  # topic_id → {count_added, max_date}

        for m in new_msgs:
            attached_topic_id = _resolve_topic_via_reply(m)
            if attached_topic_id is not None:
                m.topic_id = attached_topic_id
                changed_topic_ids.add(attached_topic_id)
                d = topic_dirty.setdefault(
                    attached_topic_id, {"count_added": 0, "max_date": None}
                )
                d["count_added"] += 1
                if m.date and (d["max_date"] is None or m.date > d["max_date"]):
                    d["max_date"] = m.date
            else:
                unassigned.append(m)

        # 把挂到旧 topic 的统计写回 Topic 行（一次 IN 查询替代 N 次 PK 查询）
        if topic_dirty:
            dirty_ids = list(topic_dirty.keys())
            topics_to_update = (
                db.query(Topic).filter(Topic.id.in_(dirty_ids)).all()
            )
            for topic in topics_to_update:
                d = topic_dirty.get(topic.id)
                if not d:
                    continue
                topic.message_count = (topic.message_count or 0) + d["count_added"]
                if d["max_date"] and (
                    topic.end_date is None or d["max_date"] > topic.end_date
                ):
                    topic.end_date = d["max_date"]

        # 3. 对孤立新消息内部先做小型 reply-chain 分组（new ↔ new）
        if unassigned:
            new_msg_ids = {m.id for m in unassigned}
            # 预建 reverse 索引：哪些 new msg 的 reply_to_id 指向了集合内的 m.id？
            # 替代下面 O(N²) 的 any(x.reply_to_id == m.id ... for x in unassigned)
            replied_to_in_new: set[int] = {
                x.reply_to_id for x in unassigned
                if x.reply_to_id is not None and x.reply_to_id in new_msg_ids
            }

            # path-compression：每条新消息只走一次到根
            new_reply_root: dict[int, int] = {}

            def _find_new_root(start: Message) -> int | None:
                chain: list[int] = []
                cur = start
                while (
                    cur.reply_to_id
                    and cur.reply_to_id in msg_map
                    and cur.reply_to_id in new_msg_ids
                ):
                    if cur.id in new_reply_root:
                        root = new_reply_root[cur.id]
                        for nid in chain:
                            new_reply_root[nid] = root
                        return root
                    chain.append(cur.id)
                    if len(chain) > 64:
                        break
                    cur = msg_map[cur.reply_to_id]
                root_id = cur.id
                for nid in chain:
                    new_reply_root[nid] = root_id
                return root_id if chain else None

            for m in unassigned:
                root = _find_new_root(m)
                if root is not None and root != m.id:
                    new_reply_root[m.id] = root

            reply_groups: dict[int, list[Message]] = defaultdict(list)
            true_orphan: list[Message] = []
            for m in unassigned:
                root = new_reply_root.get(m.id)
                if root is not None:
                    reply_groups[root].append(m)
                elif m.id in replied_to_in_new:
                    # 至少有一条其它 new msg 回复到 m → m 是这一组的根
                    reply_groups[m.id].append(m)
                else:
                    true_orphan.append(m)

            # 4. 对真正孤立的消息走 _llm_split（仅新消息；派回 main loop）
            semantic_groups = (
                _await_on_loop(_llm_split(true_orphan, progress), main_loop)
                if true_orphan else []
            )

            # 5. last-topic merge_check：把第一个 semantic_group 尝试并入"最后一个旧 topic"
            new_groups_combined: list[dict] = []
            for root_id, msgs in reply_groups.items():
                new_groups_combined.append({"title": None, "messages": msgs})
            new_groups_combined.extend(semantic_groups)

            # 按时间排序，找最早的新 group
            if new_groups_combined:
                new_groups_combined.sort(
                    key=lambda g: min(
                        (m.date for m in g["messages"] if m.date),
                        default=datetime.max,
                    )
                )
                first_new_group = new_groups_combined[0]
                # 找最后一个未被 changed 标记的旧 topic
                old_topic_query = db.query(Topic).filter(Topic.chat_id == chat_id)
                if changed_topic_ids:
                    old_topic_query = old_topic_query.filter(
                        Topic.id.notin_(changed_topic_ids)
                    )
                last_old_topic = old_topic_query.order_by(
                    Topic.end_date.desc().nullslast()
                ).first()

                if last_old_topic is not None:
                    last_old_msgs = (
                        db.query(Message)
                        .filter(Message.topic_id == last_old_topic.id)
                        .order_by(Message.date)
                        .all()
                    )
                    if progress is not None:
                        # 边界 merge_check 也算一次 LLM 步骤
                        progress["topic_total"] = (
                            progress.get("topic_total") or 0
                        ) + 1
                    merged = _await_on_loop(
                        _check_boundary_merge(
                            last_old_msgs,
                            last_old_topic.summary or "",
                            first_new_group["messages"],
                            first_new_group.get("title") or "",
                        ),
                        main_loop,
                    )
                    if progress is not None:
                        progress["topic_done"] = (
                            progress.get("topic_done") or 0
                        ) + 1
                    if merged:
                        first_msg_ids = [m.id for m in first_new_group["messages"]]
                        if first_msg_ids:
                            db.query(Message).filter(Message.id.in_(first_msg_ids)).update(
                                {Message.topic_id: last_old_topic.id},
                                synchronize_session=False,
                            )
                        last_old_topic.message_count = (
                            last_old_topic.message_count or 0
                        ) + len(first_new_group["messages"])
                        dates = [m.date for m in first_new_group["messages"] if m.date]
                        if dates and (
                            last_old_topic.end_date is None
                            or max(dates) > last_old_topic.end_date
                        ):
                            last_old_topic.end_date = max(dates)
                        changed_topic_ids.add(last_old_topic.id)
                        new_groups_combined = new_groups_combined[1:]

            # 6. 创建新 Topic 行
            for group in new_groups_combined:
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
                msg_ids = [m.id for m in group_msgs]
                if msg_ids:
                    db.query(Message).filter(Message.id.in_(msg_ids)).update(
                        {Message.topic_id: topic.id}, synchronize_session=False
                    )
                changed_topic_ids.add(topic.id)

        db.commit()
        total = db.query(Topic).filter(Topic.chat_id == chat_id).count()
        return total, changed_topic_ids
    finally:
        db.close()


async def build_topics_incremental(
    db: Session, chat_id: str, progress: dict | None = None
) -> tuple[int, set[int]]:
    """增量构建话题。

    - 仅对 ``topic_id IS NULL`` 的新消息做处理：
      - 能通过 reply_to_id 挂到旧 topic 的，直接挂上去
      - 剩余孤立新消息走 _llm_split（只切新消息，不动旧消息）
      - 第一批新 topic 与最后一个旧 topic 做 1 次 merge_check，避免边界切断
    - 旧 topic 不删、不重切；只把新增的 message 挂进去并刷新 end_date / message_count

    返回 ``(total_topic_count, changed_topic_ids)``。
    若没有任何旧 topic（首次构建），自动回退到全量 ``build_topics``。

    实际工作派到 thread pool 跑（见 ``_build_topics_incremental_sync``），避免
    阻塞 asyncio main loop。``db`` 参数保留是为了兼容签名，**实际不使用**。
    """
    main_loop = asyncio.get_running_loop()
    return await asyncio.to_thread(
        _build_topics_incremental_sync, chat_id, main_loop, progress
    )


def _build_topics_sync(
    chat_id: str, main_loop: asyncio.AbstractEventLoop, progress: dict | None = None
) -> int:
    """build_topics 的同步核心 —— 跑在 thread pool 里。

    LLM 调用通过 ``_await_on_loop`` 派回 main loop（不阻塞主循环）。
    SQLAlchemy session 在本 thread 内独立创建/关闭。
    """
    db = SessionLocal()
    try:
        messages = (
            db.query(Message)
            .filter(Message.chat_id == chat_id)
            .order_by(Message.date)
            .all()
        )
        if not messages:
            return 0

        # 1. 基于 reply_to_id 构建回复链
        # 使用 path-compression（带 memoization）：每条消息只走一次到根，
        # 总复杂度 O(N)，不再是 O(N*depth)。对 8w 条消息 + 长链群聊从分钟级降到亚秒。
        reply_chains: dict[int, int] = {}
        children: dict[int, list[int]] = defaultdict(list)
        msg_map: dict[int, Message] = {m.id: m for m in messages}

        for m in messages:
            if m.reply_to_id and m.reply_to_id in msg_map:
                children[m.reply_to_id].append(m.id)

        root_cache: dict[int, int] = {}

        def find_root(mid: int) -> int:
            # 沿链走到 cache 命中或到顶；最后回填整条链以便后续 O(1)
            chain: list[int] = []
            cur = mid
            while cur in msg_map and cur not in root_cache:
                parent = msg_map[cur].reply_to_id
                if not parent or parent not in msg_map or parent == cur:
                    break
                chain.append(cur)
                cur = parent
            root = root_cache.get(cur, cur)
            for node in chain:
                root_cache[node] = root
            return root

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

        # 3. 未关联消息用 LLM 语义切分（派回 main loop 跑）
        unassigned = [m for m in messages if m.id not in assigned]
        semantic_groups = (
            _await_on_loop(_llm_split(unassigned, progress), main_loop)
            if unassigned else []
        )

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

            # 用 id 列表批量 update message.topic_id（避免依赖跨 session 对象状态）
            msg_ids = [m.id for m in group_msgs]
            if msg_ids:
                db.query(Message).filter(Message.id.in_(msg_ids)).update(
                    {Message.topic_id: topic.id}, synchronize_session=False
                )

            topic_count += 1

        db.commit()
        return topic_count
    finally:
        db.close()


async def build_topics(db: Session, chat_id: str, progress: dict | None = None) -> int:
    """为指定群聊构建话题分组，返回话题数量。

    实际工作派到 thread pool 跑（见 ``_build_topics_sync``），避免阻塞 asyncio
    main loop。``db`` 参数保留是为了兼容调用方签名，**实际不使用**（thread
    内独立创建 session）。
    """
    main_loop = asyncio.get_running_loop()
    return await asyncio.to_thread(_build_topics_sync, chat_id, main_loop, progress)
