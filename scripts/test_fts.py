import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from backend.models.database import SessionLocal

db = SessionLocal()
sql = db.execute(text("SELECT sql FROM sqlite_master WHERE name='messages_fts'")).fetchone()
print("FTS schema:", sql[0] if sql else None)

for kw in ["GPU", "算力", "租赁", "OpenAI", "Pixel"]:
    rows = db.execute(
        text("SELECT msg_id, sender, substr(text_plain,1,80) FROM messages_fts WHERE messages_fts MATCH :kw LIMIT 3"),
        {"kw": kw},
    ).fetchall()
    print(f"\n关键词 '{kw}' 命中: {len(rows)} 条")
    for r in rows:
        print(f"  - msg={r[0]} sender={r[1]} text={r[2]}")

db.close()
