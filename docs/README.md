# tg-history 聊天记录管理迁移文档

本目录是 `tg-history` 项目的**完整迁移文档**，详细描述聊天记录从原始数据到 Agent 智能问答的全链路设计，旨在让你把这套架构（尤其是 **Agent + 索引** 部分）平移到另一个项目中。

## 文档索引

| 文档 | 内容 | 核心模块 |
|------|------|---------|
| [`01-overview.md`](./01-overview.md) | 总览 + 数据模型 | `database.py`, `config.py` |
| [`02-import-and-topics.md`](./02-import-and-topics.md) | 消息导入 + 话题构建 | `parser.py`, `topic_builder.py` |
| [`03-vector-index.md`](./03-vector-index.md) | 向量索引 + chunk + 增量 | `embedding.py`, `import_router.py` |
| [`04-agent-core.md`](./04-agent-core.md) | **Agent 主循环（最核心）** | `llm_adapter.py`, `qa_agent.py` |
| [`05-tools-and-subagent.md`](./05-tools-and-subagent.md) | **工具集 + 子 Agent + Prompt 全文** | `qa_tools.py`, `sub_agent.py` |
| [`06-runtime-and-artifact.md`](./06-runtime-and-artifact.md) | Run / Session / Artifact + 工程实践 + 迁移 Checklist | `run_registry.py`, `session_service.py`, `artifact_service.py` |

## 阅读建议

### 路径 A：快速理解整体架构（1 小时）

1. `01-overview.md` 总览
2. `04-agent-core.md` 第 8 节"主循环骨架"
3. `06-runtime-and-artifact.md` 第 18 节"端到端时序示例"

### 路径 B：完整迁移（按顺序读）

按文档编号 01 → 06 顺序读完，每篇约 30~60 分钟。

### 路径 C：只取核心 Agent 框架

1. `04-agent-core.md` 全文（LLM Adapter + 主 Agent）
2. `05-tools-and-subagent.md` 全文（工具 + 子 Agent + Prompts）
3. `06-runtime-and-artifact.md` 第 14 节（Run Registry）+ 第 16 节（工程实践）+ 第 17 节（迁移 Checklist）

## 关键认知

> ⚠️ 迁移前必读

### 哪些是不可裁剪的核心

| 模块 | 不可裁剪原因 |
|------|---------|
| **System Prompts** | 多年迭代调出来的 — 决策树/侦察原则/质量门槛/不确定性分级是项目精华 |
| **两层 Agent 架构** | 主 Agent 强模型 + 子 Agent 便宜模型，是成本/质量平衡的核心 |
| **显式缓存** | 多轮对话不开缓存 → 费用 5x↑ |
| **流式 + Run Registry** | SSE 解耦执行/订阅是用户体验关键 |
| **工具错误的 `suggestion`** | LLM 自动纠错全靠它 |
| **`_truncate_tool_output` 智能截断** | 不做的话上下文必爆 |
| **前缀缓存友好的历史重放** | 多轮对话费用直接×几倍 |

### 哪些可以根据场景裁剪

- **导入解析**（仅 Telegram）— 其他数据源换实现
- **话题构建** — 数据天然有边界时（如客服工单）可跳过
- **Telegram User Profile 工具** — 仅 Telegram 场景
- **Artifact 工具** — 不需要"活文档"功能可删
- **RAG 引擎** — 不需要"快问快答"模式可删

## 项目源码位置（参考）

| 关键文件 | 路径 |
|---------|------|
| 数据库模型 | `backend/models/database.py` |
| 配置 | `backend/config.py` |
| LLM Adapter | `backend/services/llm_adapter.py` |
| 主 Agent | `backend/services/qa_agent.py` |
| 子 Agent | `backend/services/sub_agent.py` |
| 工具集 | `backend/services/qa_tools.py` |
| Run Registry | `backend/services/run_registry.py` |
| Session Service | `backend/services/session_service.py` |
| Artifact Service | `backend/services/artifact_service.py` |
| 向量索引 | `backend/services/embedding.py` |
| 话题构建 | `backend/services/topic_builder.py` |
| 消息解析 | `backend/services/parser.py` |
| RAG 引擎 | `backend/services/rag_engine.py` |
| QA Router (SSE) | `backend/routers/qa_router.py` |
| Import Router | `backend/routers/import_router.py` |
