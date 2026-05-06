import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
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

    __table_args__ = (
        # 热查询：按 chat 过滤 + 按时间排序（qa_tools / _sync_runner 均涉及）
        Index("ix_messages_chat_date", "chat_id", "date"),
        # 话题内按时间拉取（embedding._collect_topic_chunks 、qa_tools.tool_fetch_topic_context）
        Index("ix_messages_topic_date", "topic_id", "date"),
    )

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

    __table_args__ = (
        # topic_builder.build_topics_incremental 查 last_old_topic、embedding 全量重建拉取
        Index("ix_topics_chat_end", "chat_id", "end_date"),
    )


class Import(Base):
    __tablename__ = "imports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_name = Column(String)
    chat_id = Column(String, unique=True)
    imported_at = Column(DateTime, default=datetime.now)
    message_count = Column(Integer, default=0)
    date_range = Column(String)  # "2024-01-01 ~ 2024-06-30"
    index_built = Column(Boolean, default=False)  # 向量索引是否已构建


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


class Artifact(Base):
    """Agent 在会话中产出的"活文档"。session 内可有多篇，按 artifact_key 区分。

    替代旧的一次性 SummaryReport：Agent 通过 create_artifact / update_artifact /
    rewrite_artifact 工具持续迭代，每次更新生成新 ArtifactVersion。
    """

    __tablename__ = "artifacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        String,
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # Agent 自定义 slug（如 "tech-summary"），session 内 unique
    artifact_key = Column(String, nullable=False)
    title = Column(String, nullable=False)
    content_type = Column(String, default="text/markdown")
    current_version = Column(Integer, default=1)  # 当前最新版本号（=ArtifactVersion.version 最大值）
    # 预留字段：未来升级到 chat-scoped 知识库时使用，现阶段始终 None
    chat_id = Column(String, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("session_id", "artifact_key", name="uq_artifact_session_key"),
    )


class ArtifactVersion(Base):
    """Artifact 的某一次版本快照。每次 create / update / rewrite 都新增一行。

    保留全量内容（而非 diff），方便 UI 任意切换版本浏览。
    """

    __tablename__ = "artifact_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    artifact_id = Column(
        Integer,
        ForeignKey("artifacts.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    version = Column(Integer, nullable=False)  # 1, 2, 3, ...
    content = Column(Text, nullable=False)
    op = Column(String, nullable=False)  # "create" | "update" | "rewrite"
    # JSON：update 时 {"old_str_preview": "...", "new_str_preview": "..."}；
    # create 时 None；rewrite 时 {"prev_length": N, "new_length": M}
    op_meta = Column(Text)
    # 哪次 assistant turn 产出（trace 用），turn 被删时置 NULL
    turn_id = Column(
        Integer,
        ForeignKey("chat_turns.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("artifact_id", "version", name="uq_artifact_version"),
    )


class PublishedArticle(Base):
    """已发布到"文章库"的冻结快照。

    由用户在 UI 上手动 publish artifact 时创建；内容取自 artifacts.content 当前快照，
    但**脱钩保存** —— 即使源 artifact / session 被删，文章仍保留。
    """

    __tablename__ = "published_articles"

    id = Column(String, primary_key=True)  # UUID hex

    # 源追溯（ON DELETE SET NULL —— 源被删后保留文章）
    source_artifact_id = Column(
        Integer,
        ForeignKey("artifacts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_session_id = Column(
        String,
        ForeignKey("chat_sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # 冗余备份字段：防 session / artifact 被删后丢失归属显示
    source_session_title = Column(String, nullable=False)
    source_artifact_key = Column(String, nullable=False)
    source_version_number = Column(Integer, nullable=False)

    # 内容快照
    title = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    content_type = Column(String, default="text/markdown")

    # 时间
    # ↓ 用户关心的"生成时间" = 源 ArtifactVersion.created_at，UI 主展示字段
    content_created_at = Column(DateTime, nullable=False, index=True)
    # 发布动作的时间（首次 publish 时 & overwrite 不改），仅用于内部追溯
    published_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # 覆盖模式下会变，用来做"最近改动"排序辅助
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


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


class TgUserProfileCache(Base):
    """按需调 Telegram API 拉到的用户主页缓存。

    仅当 agent 调 ``tool_get_user_profile`` 时才会写入；不会全量同步。
    """

    __tablename__ = "tg_user_profile_cache"

    sender_id = Column(String, primary_key=True)              # "user6747261966"，与 messages.sender_id 同源
    tg_user_id = Column(Integer, index=True)                  # 6747261966
    display_name = Column(String)                             # first_name + last_name
    username = Column(String, index=True)                     # 不带 @ 的用户名（可能为空）
    bio = Column(Text)                                        # FullUser.about
    is_bot = Column(Boolean, default=False)
    is_premium = Column(Boolean, default=False)
    common_chats_count = Column(Integer, default=0)
    phone = Column(String)
    deleted = Column(Boolean, default=False)                  # User.deleted（账号注销/封禁）
    payload = Column(Text)                                    # 完整 JSON 兜底（含未来字段）
    fetched_at = Column(DateTime, default=datetime.utcnow, index=True)


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
        # WAL: 多连接读不阻写
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        # WAL 下官方推荐：仅在 checkpoint 时 fsync，平常 commit 不阻塞
        cursor.execute("PRAGMA synchronous=NORMAL")
        # 64MB 页缓存（负数表示 KB）— 大幅减少热表磁盘读
        cursor.execute("PRAGMA cache_size=-65536")
        # 临时表/排序在内存 — 加速 GROUP BY / ORDER BY
        cursor.execute("PRAGMA temp_store=MEMORY")
        # 256MB mmap — 让大表走 page cache、减少 read 系统调用
        cursor.execute("PRAGMA mmap_size=268435456")
        # 写锁冲突时最多等 30s，避免 burst 并发下出现 SQLITE_BUSY
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


engine = get_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db():
    """创建所有表 + FTS5 虚拟表（trigram tokenizer，对中文友好）"""
    # 丢弃旧 QAHistory 表（已被 chat_sessions + chat_turns 替代）
    # 丢弃旧 SummaryReport 表（已被 Artifact 机制替代）
    with engine.connect() as conn:
        conn.execute(text("DROP TABLE IF EXISTS qa_history"))
        conn.execute(text("DROP TABLE IF EXISTS summary_reports"))
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
