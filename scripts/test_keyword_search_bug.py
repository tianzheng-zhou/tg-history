"""回归测试：tool_keyword_search 的 FTS 路径必须能拿到非空命中。

历史 bug：messages_fts 是独立 FTS5 表（非 content='messages'），其
``rowid`` 是 FTS5 内部自增 id，与 messages.id 无关；真实 message id 存
在 ``msg_id`` 列。原代码 ``SELECT rowid`` 把 FTS 命中映射到不存在的
``messages.id`` 上，所以 FTS 路径几乎 100% 返回空 → agent 看上去
"搜不到聊天里明明有的消息"。

修复后 ``SELECT msg_id``。本脚本走真实 db + 真实 tool 验证。

运行：
    venv\\Scripts\\python scripts\\test_keyword_search_bug.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text

from backend.models.database import SessionLocal
from backend.services.qa_tools import tool_keyword_search


async def main() -> int:
    db = SessionLocal()
    try:
        # 找一个 db 里真实出现过的 trigram-friendly 关键词
        candidates = ["GPU", "OpenAI", "http", "google", "gemini"]
        chosen = None
        for kw in candidates:
            row = db.execute(
                text(
                    "SELECT msg_id FROM messages_fts WHERE messages_fts MATCH :kw LIMIT 1"
                ),
                {"kw": kw},
            ).fetchone()
            if row:
                chosen = kw
                break
        if not chosen:
            print("数据库里都没有这些词，挑一个你自己的关键词改进脚本再试")
            return 1

        result = await tool_keyword_search(db, keyword=chosen, limit=5)
        count = result.get("count", 0)
        method = result.get("method", "?")
        print(f"keyword={chosen!r} method={method} count={count}")
        for r in result.get("results", []):
            print(
                f"  - msg_id={r.get('message_id')} sender={r.get('sender')!r} "
                f"text={(r.get('text') or '')[:60]!r}"
            )

        if count == 0 or "fts5" not in method:
            print("FAIL: FTS 路径仍然没拿到结果（或没走 fts5）")
            return 2
        print("PASS: FTS 路径正常返回结果")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
