# Telegram 群聊智能分析系统

导入 Telegram 群聊导出数据（JSON），通过 AI 实现全局摘要和 RAG 问答。

## 快速开始

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
# 编辑 .env，填入你的 DASHSCOPE_API_KEY

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
2. 在「数据导入」页上传 Telegram 导出的 `result.json`
3. 在「摘要报告」页查看 AI 生成的分类摘要
4. 在「智能问答」页对聊天记录提问

## 技术栈

- **后端**：FastAPI + SQLite + ChromaDB
- **前端**：React + Vite + Tailwind CSS + shadcn/ui
- **LLM**：阿里云百炼 DashScope（qwen3.5-plus / qwen3.6-plus）
- **Embedding**：text-embedding-v4
- **Rerank**：qwen3-rerank

## API 文档

启动后端后访问 `http://localhost:8000/docs` 查看自动生成的 API 文档。
