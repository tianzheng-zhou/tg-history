# Telegram 群聊智能分析系统

导入 Telegram 群聊导出数据（JSON），自动构建话题索引，支持 Agent / RAG 两种模式的智能问答，并通过 Artifact 机制让 Agent 产出可迭代的结构化文档。

## 功能概览

### 数据导入

- **文件上传**：上传 Telegram Desktop 导出的 `result.json`，支持单群聊和全量导出格式
- **目录绑定**：绑定本地文件夹，手动触发扫描递归发现 `result.json` 文件
- **Telegram 直连同步**：通过 MTProto API 直接登录账号，列出全部对话并增量拉取，免去手动导出（推荐）
- **增量导入**：相同群聊重复导入时自动去重，只插入新消息
- **全文检索索引**：导入时同步构建 SQLite FTS5（trigram tokenizer）全文索引，对中文/CJK 友好
- **消息去重修复**：内置 `/api/admin/dedupe-messages` 管理接口，修复历史 hash 不稳定 bug 造成的重复消息

### 话题构建

- **回复链分组**：基于 `reply_to_id` 自动将消息归入同一话题
- **LLM 语义切分**：对未形成回复链的消息，调用 LLM 按语义变化进行话题分割
- **双向重叠窗口**：LLM 切分采用滑动窗口 + 上下文重叠，避免话题在批次边界被截断
- **跨批合并检查**：批次边界处的相邻话题通过额外 LLM 调用判断是否属于同一话题
- **增量话题构建**：新消息导入后，优先通过回复链挂到旧话题，孤立消息单独做 LLM 切分，旧话题不动

### 向量索引

- **ChromaDB 持久化存储**：基于 cosine 距离的 HNSW 索引
- **话题级 chunk**：每个话题生成一个或多个 chunk（超长话题按 2000 字符自动切分，相邻 chunk 有行级重叠）
- **并发 embedding**：批量并发调用 text-embedding-v4，受全局 Semaphore 限流
- **增量索引**：仅对变更的话题重新 embed，未变话题的向量保持不动
- **全量重建**：支持强制清空重建（`force=true`）
- **多群聊并行**：最多 16 个群聊同时构建索引，实时上报进度（话题构建 → 向量写入）

### 智能问答

#### Agent 模式（推荐）

- **自主工具调用**：LLM 在循环中自主决定调用哪些工具、调用多少次，直到给出最终答案
- **最多 30 步迭代**：超过步数上限后强制基于已收集信息总结
- **7 种内置工具**：
  - `list_chats` — 列出所有已导入群聊
  - `semantic_search` — 向量语义检索（top_k 最高 200）
  - `keyword_search` — FTS5 全文搜索，0 命中时自动回退 LIKE 模糊匹配
  - `fetch_messages` — 按 ID 获取消息完整内容
  - `fetch_topic_context` — 获取整个话题上下文
  - `search_by_sender` — 按发言人查询消息
  - `search_by_date` — 按日期范围查询消息
- **流式输出**：实时推送思考过程、工具调用、工具结果、最终答案
- **Kimi 思考链**：使用 Kimi K2.6 模型时支持 `reasoning_content` 思考链展示

#### RAG 模式

- **经典检索增强生成**：语义检索 → 关键词补充 → 话题上下文扩展 → Rerank 重排序 → LLM 生成
- **Rerank 重排序**：使用 qwen3-rerank 对候选消息按相关性排序，保留 Top 8
- **流式输出**：逐阶段推送进度（语义搜索 → 关键词搜索 → 上下文扩展 → Rerank → 生成）

#### 通用特性

- **多轮对话**：基于 Session 管理对话历史，上下文自动注入
- **Run 架构**：每次提问创建独立 Run，后台异步执行，页面刷新/切换不中断
- **SSE 事件流**：前端通过 `GET /api/runs/{run_id}/events` 订阅，支持 `last_event_id` 断点续播
- **上下文窗口监控**：实时显示 prompt token 用量占模型最大上下文的百分比
- **来源引用**：答案自动附带来源消息（发言人 + 日期 + 预览），按话题去重

### Artifact 协同文档

- **Agent 主动创建**：Agent 在回答中遇到需要交付结构化长文档时（如 “梳理 XX 讨论”、“汇总资源链接”），主动调 `create_artifact` 工具产出 markdown 文档
- **增量编辑**：`update_artifact` 用 `old_str` / `new_str` 精确替换做小改动；`rewrite_artifact` 整体重写做大重构
- **会话内多篇**：一个 Q&A 会话可拥有多篇 artifact，按独立主题分开（如 `tech-summary` / `links-roundup` / `decisions`）
- **版本历史**：每次更新生成新版本，UI 支持版本下拉切换、对比
- **只读展示**：用户侧边面板查看、复制、导出 `.md`；修改通过让 Agent 迭代完成

### 会话管理

- **会话列表**：支持搜索、置顶、归档、分页
- **自动标题**：首轮对话完成后 LLM 自动生成 ≤16 字短标题
- **对话导出**：支持导出为 Markdown 或 JSON 格式
- **完整 Trajectory 记录**：每轮 Agent 的思考过程、工具调用链完整持久化

### 设置管理

- **运行时热更新**：在前端页面修改模型配置，无需重启后端
- **自动持久化**：配置修改后自动写入 `.env` 文件
- **多 Provider 支持**：同时支持阿里云百炼（DashScope）和 Moonshot（Kimi）两个 API

---

## 技术栈

| 层        | 技术                                                             |
|-----------|----------------------------------------------------------------|
| **后端**   | FastAPI · SQLite (WAL) · SQLAlchemy · ChromaDB                  |
| **前端**   | React 19 · Vite 8 · Tailwind CSS 4 · Recharts · React Markdown |
| **LLM**   | 阿里云百炼 DashScope (qwen3.5-flash / qwen3.6-plus) · Moonshot Kimi (kimi-k2.6) |
| **Embedding** | text-embedding-v4 (DashScope)                              |
| **Rerank** | qwen3-rerank (DashScope)                                      |

## 系统架构

```
┌──────────────────────────────────────────────────────────────┐
│                        React 前端                            │
│  Dashboard │ Import │ Index │ QA │ Settings                  │
└────────────────────────────┬─────────────────────────────────┘
                             │ HTTP / SSE
┌────────────────────────────┴─────────────────────────────────┐
│                     FastAPI 后端                              │
│  ┌──────────┐  ┌──────────┐  ┌─────────┐  ┌──────────────┐ │
│  │  Import   │  │   QA     │  │ Artifact│  │  Session     │ │
│  │  Router   │  │  Router  │  │  Router │  │  Router      │ │
│  └─────┬─────┘  └─────┬─────┘  └────┬────┘  └──────┬───────┘ │
│        │              │             │               │         │
│  ┌─────┴─────┐  ┌─────┴─────────────┐   ┌─┴─────────┴──────┐ │
│  │  Parser   │  │  QA Agent │ RAG    │   │ Artifact Service   │ │
│  │  Topic    │  │  Tool Calling │Rerank │   │ (create / update /  │ │
│  │  Builder  │  │  Run Registry     │   │  rewrite / version)│ │
│  └─────┬─────┘  └─────┬─────────────┘   └───────────────────┘ │
│        │              │                                   │
│  ┌─────┴──────────────┴─────────────┴──────────────────────┐ │
│  │                   LLM Adapter                            │ │
│  │  DashScope Client  │  Moonshot Client  │  Semaphore      │ │
│  └─────┬──────────────┴──────────┬────────┘                 │ │
│        │                         │                           │
│  ┌─────┴─────┐            ┌─────┴─────┐                     │
│  │  SQLite   │            │ ChromaDB  │                      │
│  │  + FTS5   │            │ (向量索引) │                      │
│  └───────────┘            └───────────┘                      │
└──────────────────────────────────────────────────────────────┘
```

## 快速开始

### 前置要求

- Python 3.12+
- Node.js 18+
- 阿里云百炼 API Key（必需）— [申请地址](https://bailian.console.aliyun.com/)
- Moonshot API Key（可选，使用 Kimi 模型时需要）— [申请地址](https://platform.kimi.com/console/api-keys)

### 1. 后端

```bash
# 创建虚拟环境
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/Mac

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env，填入 DASHSCOPE_API_KEY（必需）和 MOONSHOT_API_KEY（可选）

# 启动后端
uvicorn backend.main:app --reload --port 8000
```

### 2. 前端

```bash
cd frontend
npm install
npm run dev
```

### 3. 使用

1. 打开浏览器访问 `http://localhost:5173`
2. 在**「设置」**页检查 API Key 是否配置正确
3. 在**「数据导入」**页上传 Telegram 导出的 `result.json`，或绑定本地目录批量扫描
4. 等待后台自动完成话题构建和向量索引（进度可在「索引管理」页查看）
5. 在**「智能问答」**页对聊天记录进行提问，Agent 在复杂梳理类问题时会主动产出 Artifact 侧边文档

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DASHSCOPE_API_KEY` | 阿里云百炼 API Key（必需） | — |
| `DASHSCOPE_BASE_URL` | DashScope 兼容端点 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `MOONSHOT_API_KEY` | Moonshot/Kimi API Key（可选） | — |
| `MOONSHOT_BASE_URL` | Moonshot 兼容端点 | `https://api.moonshot.cn/v1` |
| `LLM_MODEL_MAP` | 话题切分模型（高频调用） | `qwen3.5-flash` |
| `LLM_MODEL_QA` | 问答模型 | `kimi-k2.6` |
| `EMBEDDING_MODEL` | 文本向量化模型 | `text-embedding-v4` |
| `RERANK_MODEL` | 重排序模型 | `qwen3-rerank` |
| `DATA_DIR` | 数据存储目录（SQLite + ChromaDB） | `./data` |

> **成本提示**：`LLM_MODEL_MAP` 是 token 消耗大头（话题切分高频调用），默认使用 `qwen3.5-flash` 以降低成本。

## 项目结构

```
tg-history/
├── backend/
│   ├── models/
│   │   ├── database.py          # SQLAlchemy 模型 + FTS5 初始化
│   │   └── schemas.py           # Pydantic 请求/响应模型
│   ├── prompts/
│   │   └── qa_answer.txt        # RAG 问答 prompt
│   ├── routers/
│   │   ├── import_router.py     # 数据导入 + 目录绑定 + 索引管理
│   │   ├── artifact_router.py   # Artifact CRUD + 版本查询 + 导出
│   │   ├── qa_router.py         # 问答启动 + SSE 订阅 + Run 管理
│   │   ├── session_router.py    # 会话 CRUD + 自动标题 + 导出
│   │   └── settings_router.py   # 配置查看 + 热更新
│   ├── services/
│   │   ├── parser.py            # Telegram JSON 解析
│   │   ├── topic_builder.py     # 话题构建（回复链 + LLM 语义切分）
│   │   ├── embedding.py         # ChromaDB 向量索引
│   │   ├── artifact_service.py  # Artifact CRUD + str_replace + version
│   │   ├── qa_agent.py          # Agent 主循环（LLM + Tool Calling）
│   │   ├── qa_tools.py          # Agent 工具集（检索 + artifact 操作）
│   │   ├── rag_engine.py        # RAG 检索 + Rerank + 生成
│   │   ├── llm_adapter.py       # 多 Provider LLM 客户端 + 并发控制
│   │   ├── run_registry.py      # Run 注册表 + 后台 worker
│   │   ├── session_service.py   # 会话持久化服务
│   │   ├── folder_scanner.py    # 目录扫描 + 增量去重
│   │   └── main_loop.py         # 主事件循环调度
│   ├── config.py                # 配置（Pydantic Settings）
│   └── main.py                  # FastAPI 入口
├── frontend/
│   ├── src/
│   │   ├── pages/
│   │   │   ├── Dashboard.jsx    # 仪表盘（群聊统计概览）
│   │   │   ├── Import.jsx       # 数据导入 + 目录管理
│   │   │   ├── IndexManager.jsx # 索引构建管理
│   │   │   ├── QA.jsx           # 智能问答（Agent/RAG + Artifact 面板）
│   │   │   └── Settings.jsx     # 系统设置
│   │   ├── components/          # 通用组件（Layout, ChatBubble, SourceCard...）
│   │   └── lib/
│   │       ├── api.js           # API 客户端
│   │       └── runsStore.jsx    # Run 状态全局管理
│   └── package.json
├── data/                        # 运行时数据（SQLite + ChromaDB，gitignored）
├── .env.example                 # 环境变量模板
└── requirements.txt             # Python 依赖
```

## API 文档

启动后端后访问 `http://localhost:8000/docs` 查看自动生成的 Swagger API 文档。

### 核心 API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/import` | 上传导入 JSON 文件 |
| `GET` | `/api/chats` | 列出已导入群聊 |
| `GET` | `/api/chats/{chat_id}/stats` | 群聊统计（Top 发言人、每日消息量） |
| `GET` | `/api/messages` | 搜索/浏览消息（分页 + FTS） |
| `POST` | `/api/rebuild-index/{chat_id}` | 重建单群聊索引 |
| `POST` | `/api/rebuild-index-all` | 批量重建索引 |
| `GET` | `/api/index-progress` | 索引构建进度查询 |
| `POST` | `/api/ask/agent` | 启动 Agent 问答 Run |
| `POST` | `/api/ask/stream` | 启动 RAG 问答 Run |
| `GET` | `/api/runs/{run_id}/events` | SSE 订阅 Run 事件流 |
| `POST` | `/api/runs/{run_id}/abort` | 中止 Run |
| `GET/POST/PATCH/DELETE` | `/api/sessions/*` | 会话 CRUD |
| `GET/DELETE` | `/api/sessions/{id}/artifacts/*` | Artifact 列表 / 详情 / 版本 / 导出 / 删除 |
| `GET/PUT` | `/api/settings` | 配置查看/更新 |
| `POST` | `/api/folders` | 绑定监控目录 |
| `POST` | `/api/folders/{id}/scan` | 扫描目录导入 |
| `GET/POST/DELETE` | `/api/telegram/account` | Telegram 账号配置 / 退出登录 |
| `POST` | `/api/telegram/login/send-code` | 发送 Telegram 登录验证码 |
| `POST` | `/api/telegram/login/verify` | 校验验证码 + 完成登录 |
| `GET` | `/api/telegram/dialogs` | 列出账号下所有对话 |
| `POST` | `/api/telegram/sync` | 启动后台增量同步 |
| `GET` | `/api/telegram/sync/progress` | 同步进度 |
| `POST` | `/api/telegram/sync/abort` | 中止同步 |

## 数据导出

### 方式 1：Telegram 直连同步（推荐）

1. 访问 [https://my.telegram.org/apps](https://my.telegram.org/apps) 申请 `api_id` 和 `api_hash`：
   - 用 Telegram 账号登录
   - 填写 App title（含空格，如 `My Chat History`）+ Short name（纯字母数字）+ Platform 选 **Desktop**
   - 提交后保存 `api_id`（数字）和 `api_hash`（32 位 hex）
2. 在 Web UI「数据导入」页 → **Telegram 直连** Tab
3. 填入 `api_id` / `api_hash` / 手机号（E.164 格式，如 `+8613800138000`）→ 点击「发送验证码」
4. 在 Telegram 客户端收到验证码后填入 → 登录（账号开启 2FA 时还需输入云密码）
5. 列出全部对话 → 勾选要同步的群聊/频道 → 点击「开始同步」，后台增量拉取消息

> ⚠️ **安全提示**：`api_hash` 与 `data/telegram.session` 文件等同免密码登录凭证，**不要提交到 git**（已加入 `.gitignore`），不要分享给他人。

### 方式 2：Telegram Desktop 手动导出

1. 打开 Telegram Desktop
2. 进入目标群聊 → 右上角菜单 → **Export chat history**
3. 格式选择 **Machine-readable JSON**
4. 选择需要的时间范围和内容类型
5. 导出完成后得到 `result.json` 文件，上传到 Web UI 或放入「绑定目录」

## License

MIT
