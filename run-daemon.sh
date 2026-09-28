#!/bin/bash
# launchd 用的 daemon 启动包装脚本
#
# 为什么需要包装：
# 1. launchd 不继承交互 shell 的环境变量（LARK_APP_ID/SECRET 在 ~/.zshrc），
#    这里从 ~/.zshrc 提取，保持单一来源，避免明文复制密钥。
# 2. 日志写入 ~/.claude-remote/logs/ 而非 /tmp —— /tmp 会被 macOS
#    定期清理（文件 3 天未访问即删），2026-09 排查 daemon 消失时
#    /tmp 日志已被清空，无法定位原因。
set -u

# launchd 的 PATH 只有 /usr/bin:/bin，补上 homebrew / sbin 路径
# （tmux、lark-cli 在 /opt/homebrew/bin，lsof 在 /usr/sbin）
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

REPO=/Users/dp/repo/claude-remote
PY=/Users/dp/repo/.venv/bin/python3
LOG_DIR=$HOME/.claude-remote/logs
mkdir -p "$LOG_DIR"

# 从 ~/.zshrc 提取 LARK 凭据（只取这两行，不 source 整个文件）
eval "$(grep -E '^export LARK_APP_(ID|SECRET)=' ~/.zshrc 2>/dev/null)"
if [ -z "${LARK_APP_ID:-}" ] || [ -z "${LARK_APP_SECRET:-}" ]; then
    echo "$(date '+%F %T') ERROR: LARK_APP_ID/LARK_APP_SECRET not found in ~/.zshrc" >&2
    exit 1
fi
export LARK_APP_ID LARK_APP_SECRET

# 持久化日志（daemon 自身的 FileHandler + 崩溃时的 stdout/stderr）
export CCR_LOG_PATH="$LOG_DIR/daemon.log"

# 等待 9998 端口释放（崩溃重启场景下旧进程可能还没释放端口）
for _ in $(seq 1 12); do
    lsof -ti :9998 >/dev/null 2>&1 || break
    sleep 1
done

exec "$PY" "$REPO/daemon.py"
