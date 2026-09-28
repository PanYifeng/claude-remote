#!/bin/bash
# Restart daemon with interactive card support
set -e

echo "=== 停掉旧进程 ==="
lsof -ti :9998 2>/dev/null | xargs kill -9 2>/dev/null || true
lark-cli event stop --force 2>/dev/null || true
sleep 2

echo "=== 启动 daemon（卡片模式）==="
source ~/.zshrc
export LARK_APP_ID LARK_APP_SECRET
nohup /Users/dp/repo/.venv/bin/python3 /Users/dp/repo/claude-remote/daemon.py > /tmp/daemon.log 2>&1 &
DPID=$!
echo "Daemon PID: $DPID"
sleep 6

echo "=== 注册 session ==="
/Users/dp/repo/.venv/bin/python3 /Users/dp/repo/claude-remote/scan-existing 2>&1

echo "=== 启动心跳 ==="
nohup /Users/dp/repo/.venv/bin/python3 /Users/dp/repo/claude-remote/scan-existing --daemon > /tmp/heartbeat.log 2>&1 &

echo "=== event bus ==="
lark-cli event status 2>&1 | grep -E "Bus|im\.message|card"

echo ""
echo "✅ 完成！发送 /l 到飞书机器人查看交互卡片"