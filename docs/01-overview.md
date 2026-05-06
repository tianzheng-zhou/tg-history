# 聊天记录分析与管理：完整迁移文档（1/6）总览与数据模型

> 本文档系列详细描述 `tg-history` 项目中**聊天记录从原始数据到 Agent 智能问答**的全链路设计，旨在让你把这套架构（尤其是 **Agent + 索引** 部分）平移到另一个项目中。

## 目录（系列）

1. **`01-overview.md`** — 总览与数据模型（本文）
2. **`02-import-and-topics.md`** — 消息导入解析 + 话题构建
3. **`03-vector-index.md`** — 向量索引 chunk + embedding + 增量
4. **`04-agent-core.md`** — **Agent 主循环（最核心）**
5. **`05-tools-and-subagent.md`** — **工具集 + 子 Agent + Prompt 全文**
6. **`06-runtime-and-artifact.md`** — Run/Session/Artifact + 工程实践 + 迁移 Checklist

---

## 1. 分层架构

```
┌────────────────────── 前端 React ──────────────────────┐
│ Dashboard │ Import │ Index │ QA + Artifact │ Settings │
└────────────────────────┬───────────────────────────────┘
                         │ HTTP / SSE
┌────────────────────────┴────────────────────── FastAPI ─┐
│ ┌─────────┐ ┌──────────┐ ┌─────────────┐ ┌──────────┐  │
│ │ Import  │ │ QA Run   │ │ Session     │ │ Artifact │  │
│ │ Router  │ │ Router   │ │ Router      │ │ Router   │  │
│ └────┬────┘ └────┬─────┘ └──────┬──────┘ └────┬─────┘  │
│      ▼           ▼               ▼              ▼       │
│ ┌──────────┐ ┌─────────────────┐ ┌──────────────────┐  │
│ │  Parser  │ │ RunRegistry     │ │ artifact_service │  │
│ │  Topic   │ │  ↳ run_agent    │ │ session_service  │  │
│ │  Builder │ │  ↳ rag_engine   │ └──────────────────┘  │
│ │ Embedding│ └────┬────────────┘                       │
│ └──────────┘      ▼                                    │
│            ┌──────────────────────────────┐            │
│            │ qa_agent (Orchestrator 主循环) │            │
│            │   ↳ qa_tools (10+ 工具)       │            │
│            │   ↳ sub_agent (research)      │            │
│            └──────────┬───────────────────┘            │
│                       ▼                                │
│            ┌──────────────────────────┐                │
│            │  llm_adapter（统一封装）   │                │
│            │  · DashScope (qwen系列)   │                │
│            │  · Moonshot (kimi系列)    │                │
│            │  · 显式缓存 / 并发控制     │                │
│            └──────────┬───────────────┘                │
│                       ▼                                │
│   ┌────────────────────────┐  ┌────────────────────┐   │
│   │ SQLite (WAL + FTS5)    │  │ ChromaDB (HNSW)    │   │
│   │ · messages / topics    │  │ · 话题级 chunk 向量 │   │
│   │ · sessions / turns     │  │ · cosine 距离      │   │
│   │ · artifacts / versions │  │                    │   │
│   └────────────────────────┘  └────────────────────┘   │
└────────────────────────────────────────────────────────┘
```

**核心抽象**：

| 抽象 | 含义 | 数据库表 |
|------|------|---------|
| **Message** | 一条聊天消息 | `messages` |
| **Topic** | 一段语义连续的话题 | `topics` |
| **Chunk** | 给向量检索用的话题切片（≤2000 字符） | ChromaDB |
| **Session** | 一次多轮问答的容器 | `chat_sessions` |
| **Turn** | session 内的一条消息（user/assistant） | `chat_turns` |
| **Artifact** | Agent 产出的"活文档"，可迭代版本 | `artifacts` + `artifact_versions` |
| **Run** | 一次问答的执行实例（异步任务，可订阅） | 内存 `RunRegistry` |

**两层 Agent 架构**：
- **主 Agent (Orchestrator)** — 强模型（kimi-k2.6 / qwen3.6-plus），规划/拆任务/合成，**不亲自做大量检索**
- **子 Agent (research)** — 便宜模型（qwen3.5-plus），独立上下文窗口执行具体搜索任务

---

## 2. 数据模型（SQLite + ChromaDB）

### 2.1 SQLite 引擎配置

```python
# backend/models/database.py
engine = create_engine(
    settings.db_url,
    connect_args={"check_same_thread": False, "timeout": 30},
    pool_size=30, max_overflow=50, pool_timeout=60, pool_recycle=1800,
)

# 关键 PRAGMA（每条新连接都要跑一遍，通过 connect 事件钩子）
@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")          # 多连接读不阻写
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")        # WAL 推荐
    cursor.execute("PRAGMA cache_size=-65536")         # 64MB 页缓存
    cursor.execute("PRAGMA temp_store=MEMORY")
    cursor.execute("PRAGMA mmap_size=268435456")       # 256MB mmap
    cursor.execute("PRAGMA busy_timeout=30000")        # 写锁等 30s
```

**关键点**：`pool_size=30` + `max_overflow=50` 让 Agent 模式下子 Agent 并发 + chromadb 后台 thread + 主端点同时读不会瓶颈。

### 2.2 核心表结构

#### `messages`

```python
class Message(Base):
    id = Column(Integer, primary_key=True)         # 全局唯一 = chat_id_offset + raw_id
    chat_id = Column(String, index=True)
    date = Column(DateTime, index=True)
    sender = Column(String, index=True)
    sender_id = Column(String)                     # 如 "user6747261966"
    text = Column(Text)                            # 原始 JSON（带 entities）
    text_plain = Column(Text)                      # 纯文本（用于 FTS）
    reply_to_id = Column(Integer)                  # 已加 chat_id_offset
    forwarded_from = Column(String)
    topic_id = Column(Integer, index=True)         # NULL = 尚未分组
    media_type = Column(String)
    entities = Column(Text)                        # JSON: 链接/@提及

    __table_args__ = (
        Index("ix_messages_chat_date", "chat_id", "date"),
        Index("ix_messages_topic_date", "topic_id", "date"),
    )
```

**关键设计 — 跨 chat 全局唯一 ID**：

```python
def _stable_id_offset(chat_id: str) -> int:
    """SHA-256 取前 8 字节 → 偏移量，跨进程稳定"""
    digest = hashlib.sha256(chat_id.encode("utf-8")).digest()
    return (int.from_bytes(digest[:8], "big") % (10**9)) * 1000000

# 真正存的 message.id = id_offset + raw_telegram_id
```

> ⚠️ **必须用稳定哈希（SHA-256），不要用 Python 内置 `hash()`**：内置 hash 是 PYTHONHASHSEED 加盐的（重启后变），导致旧消息会被当作新消息再次插入（出现 `message_count` 翻倍 + 索引整库重建）。

#### `topics`

```python
class Topic(Base):
    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(String, index=True)
    root_message_id = Column(Integer)
    start_date = Column(DateTime)
    end_date = Column(DateTime)
    participant_count = Column(Integer)
    message_count = Column(Integer)
    summary = Column(Text)                         # LLM 生成的话题标题
    category = Column(String)                      # tech/business/resource/general

    __table_args__ = (Index("ix_topics_chat_end", "chat_id", "end_date"),)
```

#### `imports`

```python
class Import(Base):
    chat_id = Column(String, unique=True)
    chat_name = Column(String)
    imported_at = Column(DateTime)
    message_count = Column(Integer)
    date_range = Column(String)
    index_built = Column(Boolean, default=False)   # 索引是否最新
```

#### `chat_sessions` / `chat_turns`

```python
class ChatSession(Base):
    id = Column(String, primary_key=True)          # UUID hex
    title = Column(String, default="新对话")
    mode = Column(String, default="agent")         # agent | rag
    chat_ids = Column(Text)                        # JSON
    pinned = Column(Boolean)
    archived = Column(Boolean)
    turn_count = Column(Integer)
    last_preview = Column(String)
    created_at / updated_at

class ChatTurn(Base):
    session_id = Column(String, index=True)
    seq = Column(Integer)                          # session 内 0 基序号
    role = Column(String)                          # user | assistant
    content = Column(Text)
    sources = Column(Text)                         # JSON, 仅 assistant
    trajectory = Column(Text)                      # JSON, 完整 agent 推理链
    mode = Column(String)
    meta = Column(Text)                            # JSON: usage/run_id/injected_prefix
```

#### `artifacts` / `artifact_versions`

```python
class Artifact(Base):
    session_id = Column(String, ForeignKey(ondelete="CASCADE"))
    artifact_key = Column(String)                  # session 内 unique 的 slug
    title = Column(String)
    current_version = Column(Integer)
    UniqueConstraint("session_id", "artifact_key")

class ArtifactVersion(Base):
    artifact_id = Column(Integer, ForeignKey)
    version = Column(Integer)
    content = Column(Text)                         # 全量内容（不存 diff）
    op = Column(String)                            # create | update | rewrite
    op_meta = Column(Text)                         # JSON
    turn_id = Column(Integer, FK ondelete="SET NULL")
    UniqueConstraint("artifact_id", "version")
```

> **设计关键**：每次 update / rewrite 都新增一行（保留全量内容，不存 diff）。这样 UI 可以任意切换版本浏览。

### 2.3 FTS5 全文索引

```python
def init_db():
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        # trigram tokenizer 对中文/CJK 远好于默认 unicode61
        conn.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
            "USING fts5(text_plain, sender, chat_id UNINDEXED, msg_id UNINDEXED, "
            "tokenize='trigram')"
        ))
```

**为什么用 trigram**：
- 默认 `unicode61` 对中文按字切，搜 "便宜" 容易漏命中
- `trigram` 把 "便宜" 拆成 trigram 索引，模糊匹配更稳
- 代价：索引体积约 3x（可接受）

### 2.4 配置（`backend/config.py`）

```python
class Settings(BaseSettings):
    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    moonshot_api_key: str = ""
    moonshot_base_url: str = "https://api.moonshot.cn/v1"

    llm_model_map: str = "qwen3.5-flash"           # 高频调用（话题切分）
    llm_model_qa: str = "qwen3.6-plus"             # 主 Agent
    llm_model_sub_agent: str = ""                  # 空 = 跟随 qa；推荐 qwen3.5-plus
    enable_qwen_explicit_cache: bool = True

    embedding_model: str = "text-embedding-v4"
    rerank_model: str = "qwen3-rerank"

    data_dir: str = "./data"

    @property
    def db_path(self): return Path(self.data_dir) / "app.db"
    @property
    def db_url(self): return f"sqlite:///{self.db_path}"
    @property
    def chroma_dir(self): return str(Path(self.data_dir) / "chroma_db")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
```

**依赖（`requirements.txt`）**：

```
fastapi==0.115.9
uvicorn==0.34.2
sqlalchemy==2.0.40
pydantic==2.11.2
pydantic-settings==2.9.1
httpx==0.28.1
openai==1.78.1                # AsyncOpenAI 客户端
chromadb==1.0.7
chroma-hnswlib==0.7.6
ijson==3.3.0                  # 流式 JSON 解析
sse-starlette==2.2.1
python-dotenv==1.1.0
aiofiles==24.1.0
```

> 下一篇：`02-import-and-topics.md` — 消息导入解析 + 话题构建（reply chain + LLM 语义切分）。
