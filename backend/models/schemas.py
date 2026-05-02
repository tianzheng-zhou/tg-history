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


class QAHistoryItem(BaseModel):
    id: int
    question: str
    answer: str | None
    created_at: datetime


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
    embedding_model: str
    rerank_model: str
    has_api_key: bool
    has_moonshot_key: bool = False
