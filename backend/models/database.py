import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
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
    imported_at = Column(DateTime, default=datetime.utcnow)
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


class QAHistory(Base):
    __tablename__ = "qa_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    question = Column(Text, nullable=False)
    answer = Column(Text)
    sources = Column(Text)  # JSON
    chat_ids = Column(Text)  # JSON
    created_at = Column(DateTime, default=datetime.utcnow)


# ---------- Engine / Session ----------

def _ensure_data_dir():
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)


def get_engine():
    _ensure_data_dir()
    engine = create_engine(
        settings.db_url,
        connect_args={"check_same_thread": False},
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
    """创建所有表 + FTS5 虚拟表"""
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        # 用独立 FTS 表（非 content 同步），避免损坏
        conn.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
            "USING fts5(text_plain, sender, chat_id UNINDEXED, msg_id UNINDEXED)"
        ))
        conn.commit()


def get_db():
    """FastAPI dependency"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
