#!/usr/bin/env bash
# TG-History 一键启动脚本 (Linux / macOS)
# 前端: http://localhost:13747
# 后端: http://localhost:13748

set -e
cd "$(dirname "$0")"

# 颜色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "============================================"
echo "  Telegram 群聊智能分析系统 - 一键启动"
echo "  前端: http://localhost:13747"
echo "  后端: http://localhost:13748"
echo "============================================"
echo ""

# 激活虚拟环境
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
    echo -e "${GREEN}[OK]${NC} 已激活虚拟环境"
else
    echo -e "${YELLOW}[WARN]${NC} 未找到 venv，使用系统 Python"
fi

# 清理函数：退出时杀掉子进程
cleanup() {
    echo ""
    echo "正在停止服务..."
    kill $BACKEND_PID 2>/dev/null || true
    kill $FRONTEND_PID 2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM

# 启动后端
echo "[启动] 后端 uvicorn :13748 ..."
uvicorn backend.main:app --host 0.0.0.0 --port 13748 --reload &
BACKEND_PID=$!

sleep 2

# 启动前端
echo "[启动] 前端 vite :13747 ..."
cd frontend && npm run dev &
FRONTEND_PID=$!
cd ..

echo ""
echo -e "${GREEN}[OK]${NC} 前端和后端均已启动，请在浏览器访问:"
echo "     http://localhost:13747"
echo ""
echo "按 Ctrl+C 停止所有服务"

# 等待子进程
wait
