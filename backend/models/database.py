import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.config import settings


class Base(DeclarativeBase):
    pass


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    chat_id = Column(String, index=True, nullable=False)
    date = Column(DateTime, index=True)
    sender = Column(String, index=True)
    sender_id = Column(String)
    text = Column(Text)
    text_plain = Column(Text)  # 纯文本，用于 FTS
    reply_to_id = Column(Integer)
    forwarded_from = Column(String)
    topic_id = Column(Integer, index=True)
    media_type = Column(String)
    entities = Column(Text)  # JSON: 链接、@提及等
    embedding_id = Column(String)

    def set_entities(self, data: list | dict | None):
        self.entities = json.dumps(data, ensure_ascii=False) if data else None

    def get_entities(self) -> list | dict | None:
        return json.loads(self.entities) if self.entities else None


class Topic(Base):
    __tablename__ = "topics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(String, index=True, nullable=False)
    root_message_id = Column(Integer)
    start_date = Column(DateTime)
    end_date = Column(DateTime)
    participant_count = Column(Integer, default=0)
    message_count = Column(Integer, default=0)
    summary = Column(Text)
    category = Column(String)  # tech / business / resource / general


class Import(Base):
    __tablename__ = "imports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_name = Column(String)
    chat_id = Column(String, unique=True)
    imported_at = Column(DateTime, default=datetime.now)
    message_count = Column(Integer, default=0)
    date_range = Column(String)  # "2024-01-01 ~ 2024-06-30"
    index_built = Column(Boolean, default=False)  # 向量索引是否已构建


class SummaryReport(Base):
    __tablename__ = "summary_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(String, index=True, nullable=False)
    category = Column(String, index=True)  # tech / business / resource / decision / opinion
    content = Column(Text)
    generated_at = Column(DateTime, default=datetime.utcnow)
    chunk_summaries = Column(Text)  # JSON: Map 阶段各段摘要
    stale = Column(Boolean, default=False)  # 新数据导入后标记过期


class ChatSession(Base):
    """智能问答的会话（多轮对话容器）"""

    __tablename__ = "chat_sessions"

    id = Column(String, primary_key=True)  # UUID hex
    title = Column(String, default="新对话")
    mode = Column(String, default="agent")  # "agent" | "rag"
    chat_ids = Column(Text)  # JSON：session 默认群聊过滤
    pinned = Column(Boolean, default=False, index=True)
    archived = Column(Boolean, default=False, index=True)
    turn_count = Column(Integer, default=0)
    last_preview = Column(String)  # 最后一条消息 80 字预览
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, index=True)


class ChatTurn(Base):
    """会话中的一轮消息（user 或 assistant）"""

    __tablename__ = "chat_turns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String, index=True, nullable=False)
    seq = Column(Integer, nullable=False)  # session 内 0 基序号
    role = Column(String, nullable=False)  # "user" | "assistant"
    content = Column(Text)
    sources = Column(Text)  # JSON，仅 assistant
    trajectory = Column(Text)  # JSON，仅 assistant，完整 agent 推理链
    mode = Column(String)  # "agent" | "rag" 本轮实际模式
    meta = Column(Text)  # JSON：usage/confidence/aborted/run_id/...
    created_at = Column(DateTime, default=datetime.utcnow)


class WatchedFolder(Base):
    """绑定的目录：手动触发扫描时递归找 result.json"""

    __tablename__ = "watched_folders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    path = Column(String, unique=True, nullable=False)        # 绝对、规范化后的路径
    alias = Column(String)                                    # 可选别名（默认取路径末段）
    added_at = Column(DateTime, default=datetime.now)
    last_scan_at = Column(DateTime)
    last_scan_total = Column(Integer, default=0)              # 上次扫描发现的 result.json 总数
    last_scan_imported = Column(Integer, default=0)           # 上次扫描成功导入的文件数
    last_scan_skipped = Column(Integer, default=0)            # 上次扫描因 mtime 未变跳过的文件数
    last_scan_failed = Column(Integer, default=0)


class ImportedFile(Base):
    """已扫描/导入过的 result.json：用于路径 + mtime 去重"""

    __tablename__ = "imported_files"

    id = Column(Integer, primary_key=True, autoincrement=True)
    folder_id = Column(Integer, index=True)                   # 软关联 watched_folders.id
    abs_path = Column(String, unique=True, nullable=False)
    mtime = Column(Float)                                     # os.stat().st_mtime
    size = Column(Integer)
    chat_count = Column(Integer, default=0)                   # 该文件解析出多少个群聊
    status = Column(String)                                   # "ok" | "error"
    error = Column(Text)                                      # 错误信息（截断）
    imported_at = Column(DateTime, default=datetime.now)


class TelegramAccount(Base):
    """Telegram 直连同步账号：仅 1 行（singleton），存 api_id/hash 和登录态。

    api_hash 与 .session 文件等价于免密码登录凭证 —— 不要导出到 git，
    不要外传。本表与 data/telegram.session 文件配套使用。
    """

    __tablename__ = "telegram_account"

    id = Column(Integer, primary_key=True, autoincrement=True)
    api_id = Column(Integer, nullable=False)
    api_hash = Column(String, nullable=False)
    phone = Column(String, nullable=False)                    # E.164 格式，含 + 和国家码
    tg_user_id = Column(Integer)                              # 登录后填入
    username = Column(String)                                 # 登录后填入（可能为空）
    first_name = Column(String)
    last_name = Column(String)
    created_at = Column(DateTime, default=datetime.now)
    last_login_at = Column(DateTime)


# ---------- Engine / Session ----------

def _ensure_data_dir():
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)


def get_engine():
    _ensure_data_dir()
    engine = create_engine(
        settings.db_url,
        connect_args={"check_same_thread": False, "timeout": 30},
        pool_size=30,
        max_overflow=50,
        pool_timeout=60,
        pool_recycle=1800,
        echo=False,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


engine = get_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db():
    """创建所有表 + FTS5 虚拟表（trigram tokenizer，对中文友好）"""
    # 丢弃旧 QAHistory 表（已被 chat_sessions + chat_turns 替代）
    with engine.connect() as conn:
        conn.execute(text("DROP TABLE IF EXISTS qa_history"))
        conn.commit()

    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        # 检查现有 FTS 表的 tokenizer：trigram 对中文/CJK 远好于默认 unicode61
        existing_sql_row = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages_fts'"
        )).fetchone()
        existing_sql = (existing_sql_row[0] if existing_sql_row else "") or ""
        needs_rebuild = bool(existing_sql) and "tokenize" not in existing_sql.lower()

        if needs_rebuild:
            # 旧表是 unicode61（默认）—— DROP 重建为 trigram 并重新灌数据
            conn.execute(text("DROP TABLE messages_fts"))
            conn.commit()

        conn.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
            "USING fts5(text_plain, sender, chat_id UNINDEXED, msg_id UNINDEXED, "
            "tokenize='trigram')"
        ))
        conn.commit()

        if needs_rebuild:
            # 把现有 messages 重新灌进新 FTS 表
            conn.execute(text(
                "INSERT INTO messages_fts(text_plain, sender, chat_id, msg_id) "
                "SELECT text_plain, sender, chat_id, id FROM messages "
                "WHERE text_plain IS NOT NULL AND text_plain != ''"
            ))
            conn.commit()


def get_db():
    """FastAPI dependency"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
