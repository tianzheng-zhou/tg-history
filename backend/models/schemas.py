from datetime import datetime

from pydantic import BaseModel


# ---------- Import ----------

class ImportResult(BaseModel):
    chat_id: str
    chat_name: str
    message_count: int
    date_range: str


class ChatInfo(BaseModel):
    id: int
    chat_name: str
    chat_id: str
    imported_at: datetime
    message_count: int
    date_range: str
    index_built: bool = False


class ChatStats(BaseModel):
    chat_id: str
    chat_name: str
    message_count: int
    date_range: str
    top_senders: list[dict]
    messages_per_day: list[dict]
    topic_count: int


# ---------- Messages ----------

class MessageItem(BaseModel):
    id: int
    chat_id: str
    date: datetime | None
    sender: str | None
    text: str | None
    reply_to_id: int | None
    forwarded_from: str | None
    topic_id: int | None
    media_type: str | None
    entities: list | dict | None = None


class MessageQuery(BaseModel):
    chat_id: str | None = None
    sender: str | None = None
    keyword: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    page: int = 1
    page_size: int = 50


# ---------- Summary ----------

class SummarizeRequest(BaseModel):
    chat_id: str
    force: bool = False  # 强制重新生成


class SummaryItem(BaseModel):
    id: int
    chat_id: str
    category: str
    content: str
    generated_at: datetime
    stale: bool = False


# ---------- QA ----------

class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class AskRequest(BaseModel):
    question: str
    chat_ids: list[str] | None = None
    date_range: list[str] | None = None
    sender: str | None = None
    history: list[ChatMessage] | None = None  # 多轮对话历史


class SourceItem(BaseModel):
    message_ids: list[int]
    sender: str | None
    date: str | None
    preview: str
    topic_id: int | None = None


class AskResponse(BaseModel):
    answer: str
    sources: list[SourceItem]
    confidence: str = "medium"


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    max_context: int = 131072
    percent: float = 0.0
    model: str | None = None


# ---------- Chat Sessions ----------

class SessionCreateRequest(BaseModel):
    title: str | None = None
    mode: str = "agent"  # "agent" | "rag"
    chat_ids: list[str] | None = None


class SessionUpdateRequest(BaseModel):
    title: str | None = None
    pinned: bool | None = None
    archived: bool | None = None
    mode: str | None = None
    chat_ids: list[str] | None = None


class SessionSummary(BaseModel):
    id: str
    title: str
    mode: str
    chat_ids: list[str] | None = None
    pinned: bool = False
    archived: bool = False
    turn_count: int = 0
    last_preview: str | None = None
    created_at: datetime
    updated_at: datetime


class SessionListResponse(BaseModel):
    sessions: list[SessionSummary]
    total: int


class TurnItem(BaseModel):
    id: int
    seq: int
    role: str
    content: str | None = None
    sources: list[SourceItem] | None = None
    trajectory: dict | None = None
    mode: str | None = None
    meta: dict | None = None
    created_at: datetime


class SessionDetailResponse(BaseModel):
    session: SessionSummary
    turns: list[TurnItem]


# ---------- Runs ----------

class RunStartRequest(BaseModel):
    question: str
    session_id: str | None = None
    mode: str = "agent"  # "agent" | "rag"
    chat_ids: list[str] | None = None
    date_range: list[str] | None = None
    sender: str | None = None


class RunStartResponse(BaseModel):
    run_id: str
    session_id: str
    title: str
    already_running: bool = False  # 如果 session 已有进行中的 run，直接返回它


class RunInfo(BaseModel):
    run_id: str
    session_id: str
    mode: str
    question: str
    status: str  # pending | running | completed | aborted | failed | lost
    started_at: datetime
    completed_at: datetime | None = None


# ---------- Settings ----------

class SettingsUpdate(BaseModel):
    dashscope_api_key: str | None = None
    moonshot_api_key: str | None = None
    llm_model_map: str | None = None
    llm_model_reduce: str | None = None
    llm_model_qa: str | None = None
    embedding_model: str | None = None
    rerank_model: str | None = None


class SettingsResponse(BaseModel):
    llm_model_map: str
    llm_model_reduce: str
    llm_model_qa: str
    qa_context_window: int = 131072  # 当前 QA 模型的最大上下文窗口
    embedding_model: str
    rerank_model: str
    has_api_key: bool
    has_moonshot_key: bool = False


# ---------- Watched Folders ----------

class FolderValidateRequest(BaseModel):
    path: str


class FolderValidateResponse(BaseModel):
    valid: bool
    reason: str | None = None
    resolved_path: str | None = None         # 规范化后的绝对路径
    result_json_count: int = 0
    sample_paths: list[str] = []             # 前 5 个相对路径，给用户预览用


class FolderAddRequest(BaseModel):
    path: str
    alias: str | None = None


class WatchedFolderInfo(BaseModel):
    id: int
    path: str
    alias: str | None = None
    added_at: datetime
    last_scan_at: datetime | None = None
    last_scan_total: int = 0
    last_scan_imported: int = 0
    last_scan_skipped: int = 0
    last_scan_failed: int = 0


class ScanFileResult(BaseModel):
    path: str
    status: str                              # "ok" | "skipped" | "error"
    chats: list[ImportResult] = []           # status=ok 时填，每个解析出的群聊增量结果
    error: str | None = None


class ScanResult(BaseModel):
    folder_id: int
    folder_path: str
    total: int                               # 找到的 result.json 总数
    skipped: int                             # mtime 未变跳过的数量
    imported: int                            # 本次成功处理的文件数
    failed: int                              # 解析/导入失败的文件数
    files: list[ScanFileResult] = []


# ---------- Telegram 直连同步 ----------

class TelegramAccountInfo(BaseModel):
    """登录态信息（供前端判断 UI 状态）"""
    configured: bool = False                  # 是否已保存 api_id/hash/phone
    authorized: bool = False                  # 是否已完成验证码登录（session 有效）
    phone: str | None = None
    tg_user_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    last_login_at: datetime | None = None
    needs_password: bool = False              # 上次 sign_in 抛了 SessionPasswordNeededError
    proxy: dict | None = None                 # 当前代理状态：{enabled, source, scheme, host, port}


class TelegramConfigureRequest(BaseModel):
    api_id: int
    api_hash: str
    phone: str                                # E.164 格式，含 + 号，如 +8613800138000


class TelegramSendCodeResponse(BaseModel):
    sent: bool
    phone_code_hash: str | None = None        # 仅作为前端透传字段，verify 时由后端从内存取


class TelegramVerifyRequest(BaseModel):
    code: str
    password: str | None = None               # 2FA 云密码（账号开启了二次验证才需要）


class TelegramDialogInfo(BaseModel):
    chat_id: str
    name: str
    type: str                                 # private / group / supergroup / channel / unknown
    username: str | None = None
    unread_count: int = 0
    last_message_id: int | None = None
    last_message_date: str | None = None
    # 本地导入状态（由后端 join Import 表后填充）
    imported: bool = False
    imported_message_count: int = 0
    local_max_message_id: int = 0             # 本地已存的最大原始 message id（增量基准）


class TelegramSyncRequest(BaseModel):
    chat_ids: list[str]


class TelegramSyncProgress(BaseModel):
    running: bool = False
    aborting: bool = False
    total: int = 0                            # 待同步 chat 数
    completed: int = 0                        # 已完成 chat 数
    current_chat_id: str | None = None
    current_chat_name: str | None = None
    current_fetched: int = 0                  # 当前 chat 已拉取条数
    current_imported: int = 0                 # 当前 chat 累计入库条数
    results: list[dict] = []                  # 每个 chat 完成后追加 {chat_id, chat_name, status, message_count, error?}
    started_at: datetime | None = None
    finished_at: datetime | None = None
