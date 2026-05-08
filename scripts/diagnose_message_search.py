import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import func, or_, text

from backend.models.database import Import, Message, SessionLocal, TelegramAccount
from backend.routers.import_router import _stable_id_offset
from backend.services import telegram_sync as tg
from backend.services.qa_tools import tool_keyword_search


def _parse_date(value: str | None):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d")


def _short(value: str | None, n: int = 160) -> str:
    value = value or ""
    return value.replace("\n", "\\n")[:n]


def _apply_filters(q, args):
    if args.chat_id:
        q = q.filter(Message.chat_id == args.chat_id)
    if args.sender:
        q = q.filter(Message.sender.like(f"%{args.sender}%"))
    start = _parse_date(args.start_date)
    end = _parse_date(args.end_date)
    if start is not None:
        q = q.filter(Message.date >= start)
    if end is not None:
        q = q.filter(Message.date <= end.replace(hour=23, minute=59, second=59))
    return q


def _print_local_diagnostics(db, args):
    print("\n=== local DB exact LIKE ===")
    like_q = db.query(Message).filter(Message.text_plain.like(f"%{args.keyword}%"))
    like_q = _apply_filters(like_q, args)
    like_count = like_q.count()
    print("count:", like_count)
    for m in like_q.order_by(Message.date.desc()).limit(args.limit).all():
        print(f"- id={m.id} chat={m.chat_id} date={m.date} sender={m.sender!r} text={_short(m.text_plain)}")

    print("\n=== raw FTS MATCH ===")
    try:
        fts_count = db.execute(
            text("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH :kw"),
            {"kw": args.keyword},
        ).scalar()
        rows = db.execute(
            text("SELECT msg_id FROM messages_fts WHERE messages_fts MATCH :kw LIMIT :lim"),
            {"kw": args.keyword, "lim": min(args.limit * 10, 100)},
        ).fetchall()
        ids = [r[0] for r in rows]
        print("raw fts count:", fts_count)
        print("raw fts sample ids:", ids[: args.limit])
        if ids:
            fts_q = db.query(Message).filter(Message.id.in_(ids))
            fts_q = _apply_filters(fts_q, args)
            fts_msgs = fts_q.order_by(Message.date.desc()).limit(args.limit).all()
            print("after SQL filters sample count:", len(fts_msgs))
            for m in fts_msgs:
                print(f"- id={m.id} chat={m.chat_id} date={m.date} sender={m.sender!r} text={_short(m.text_plain)}")
    except Exception as e:
        print("FTS error:", type(e).__name__, str(e))


async def _print_tool_diagnostics(db, args):
    print("\n=== tool_keyword_search ===")
    result = await tool_keyword_search(
        db,
        keyword=args.keyword,
        chat_ids=[args.chat_id] if args.chat_id else None,
        senders=[args.sender] if args.sender else None,
        start_date=args.start_date,
        end_date=args.end_date,
        limit=args.limit,
        include_sender_id=True,
    )
    print("method:", result.get("method"), "count:", result.get("count"), "error:", result.get("error"))
    for item in result.get("results", []):
        print(
            f"- id={item.get('message_id')} chat={item.get('chat_id')} date={item.get('date')} "
            f"sender={item.get('sender')!r} sender_id={item.get('sender_id')!r} text={_short(item.get('text'))}"
        )


async def _print_remote_diagnostics(db, args):
    if not args.remote:
        return
    print("\n=== Telegram remote search ===")
    if not args.chat_id:
        print("remote search requires --chat-id")
        return
    account = db.query(TelegramAccount).first()
    if not account:
        print("no telegram_account row")
        return

    offset = _stable_id_offset(args.chat_id)
    local_max_global = (
        db.query(func.max(Message.id)).filter(Message.chat_id == args.chat_id).scalar() or 0
    )
    local_max_raw = max(0, local_max_global - offset) if local_max_global else 0
    imp = db.query(Import).filter(Import.chat_id == args.chat_id).first()
    print("chat:", args.chat_id, "name:", imp.chat_name if imp else None)
    print("local max raw id:", local_max_raw, "local max global id:", local_max_global)

    client = await tg.get_client(account.api_id, account.api_hash)
    if not client.is_connected():
        await client.connect()
    if not await client.is_user_authorized():
        print("telegram client is not authorized")
        return
    entity = await tg._resolve_entity(client, args.chat_id)

    found = 0
    async for msg in client.iter_messages(entity, search=args.keyword, limit=args.remote_limit):
        found += 1
        raw_id = int(msg.id)
        global_id = offset + raw_id
        remote_text = msg.message or ""
        local = db.get(Message, global_id)
        fts_rows = db.execute(
            text("SELECT count(*) FROM messages_fts WHERE msg_id = :mid"), {"mid": global_id}
        ).scalar()
        if local is None:
            state = "LOCAL_MISSING"
            if raw_id <= local_max_raw:
                state += " id<=local_max_incremental_will_skip"
        else:
            local_has_keyword = args.keyword in (local.text_plain or "")
            remote_has_keyword = args.keyword in remote_text
            state = f"LOCAL_PRESENT local_has_keyword={local_has_keyword} remote_has_keyword={remote_has_keyword} fts_rows={fts_rows}"
            if remote_has_keyword and not local_has_keyword:
                state += " POSSIBLY_EDITED_AFTER_SYNC"
        print(f"- raw_id={raw_id} global_id={global_id} date={msg.date} state={state}")
        print(f"  remote: {_short(remote_text)}")
        if local is not None:
            print(f"  local : {_short(local.text_plain)}")
    print("remote found:", found)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("keyword")
    parser.add_argument("--chat-id")
    parser.add_argument("--sender")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--remote-limit", type=int, default=10)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        _print_local_diagnostics(db, args)
        await _print_tool_diagnostics(db, args)
        await _print_remote_diagnostics(db, args)
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
