#!/bin/bash
# Restart daemon with interactive card support
set -e

echo "=== 停掉旧进程 ==="
lsof -ti :9998 2>/dev/null | xargs kill -9 2>/dev/null || true
# 注意：不要 stop event bus！bus 是常驻进程（跨 daemon 重启存活是设计意图）。
# 实测（2026-11-26）：stop --force 后立刻重建的 bus，飞书 WebSocket 虽然连接
# 成功但事件不推送（旧连接在飞书侧未释放，订阅被忽略），盲区长达 43 分钟，
# 期间 /l 等命令全部无响应。只有 consumer 挂了才需要动 bus。
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