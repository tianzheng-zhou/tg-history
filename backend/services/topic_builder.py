"""话题树构建：基于 reply_to_id 和时间窗口将消息分组"""

from collections import defaultdict
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.models.database import Message, Topic

# 时间窗口：30 分钟内的连续消息视为同一段对话
TIME_WINDOW = timedelta(minutes=30)


def build_topics(db: Session, chat_id: str) -> int:
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
    reply_chains: dict[int, int] = {}  # message_id → root_id
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
    topic_groups: dict[int, list[Message]] = defaultdict(list)
    assigned: set[int] = set()

    for m in messages:
        root = reply_chains.get(m.id)
        if root is not None:
            topic_groups[root].append(m)
            assigned.add(m.id)
        elif m.id in children:
            topic_groups[m.id].append(m)
            assigned.add(m.id)

    # 3. 未关联消息按时间窗口分组
    unassigned = [m for m in messages if m.id not in assigned]
    current_group: list[Message] = []
    time_group_id = -1

    for m in unassigned:
        if not current_group:
            time_group_id -= 1
            current_group = [m]
        elif m.date and current_group[-1].date and (m.date - current_group[-1].date) <= TIME_WINDOW:
            current_group.append(m)
        else:
            if current_group:
                topic_groups[time_group_id] = current_group
            time_group_id -= 1
            current_group = [m]

    if current_group:
        topic_groups[time_group_id] = current_group

    # 4. 删除旧话题记录
    db.query(Topic).filter(Topic.chat_id == chat_id).delete()

    # 5. 写入新话题
    topic_count = 0
    for root_id, group_msgs in topic_groups.items():
        if not group_msgs:
            continue
        dates = [m.date for m in group_msgs if m.date]
        participants = set(m.sender for m in group_msgs if m.sender)

        topic = Topic(
            chat_id=chat_id,
            root_message_id=root_id if root_id > 0 else None,
            start_date=min(dates) if dates else None,
            end_date=max(dates) if dates else None,
            participant_count=len(participants),
            message_count=len(group_msgs),
        )
        db.add(topic)
        db.flush()

        for m in group_msgs:
            m.topic_id = topic.id

        topic_count += 1

    db.commit()
    return topic_count
