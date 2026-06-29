# Claude Code Remote (ccr)

> Remote monitor and control Claude Code shell sessions via Lark (Feishu) on mobile.
> 在手机上通过飞书（Lark）远程监控和控制本机所有 Claude Code shell 会话。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Architecture / 系统架构

```
┌─────────────────────────────────────────────────────┐
│  Lark App (Bot)                                     │
│  • Send/receive commands via Lark chat              │
│  • Event bus (WebSocket) — no public webhook needed │
│  • 通过 Lark 聊天收发命令，无需公网 webhook           │
└─────────────────────┬───────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────┐
│  Daemon (Python aiohttp, port 9998)                 │
│  • HTTP API server                                  │
│  • Session registry (SQLite)                        │
│  • Session management (screen + AppleScript)        │
│  • Periodic health check                            │
│  • Lark event consumer (bot identity)               │
└────┬───────────┬──────────┬─────────────────────────┘
     │           │          │
     ▼           ▼          ▼
  screen-1    screen-2    IDE Terminal (IntelliJ/PyCharm/VS Code)
     │           │          │
     ▼           ▼          ▼
  Log files   Log files  Clipboard capture (Accessibility API)
```

## Quick Start / 快速开始

### Prerequisites / 前置条件

- macOS (requires `/usr/bin/screen`)
- Python 3.11+
- `lark-cli` (`brew install lark-cli`)
- Lark app (create at [Lark Open Platform](https://open.feishu.cn))
- **System Settings → Privacy & Security → Accessibility** — authorize `Python` or your terminal app for IDE terminal control

### Install / 安装

```bash
git clone https://github.com/PanYifeng/claude-remote.git
cd claude-remote

# Install dependencies
pip install aiohttp

# Set Lark credentials
export LARK_APP_ID="cli_xxxxxxxxxxxxx"
export LARK_APP_SECRET="xxxxxxxxxxxxxxxxxxxxx"

# Start the daemon
python3 daemon.py

# Register all existing claude processes
python3 scan-existing

# Start heartbeat daemon (keeps sessions alive)
python3 scan-existing --daemon
```

## Lark Bot Commands / 飞书机器人命令

Send these commands in your Lark chat with the bot:

| Command / 命令 | Description / 说明 | Example / 示例 |
|---------------|-------------------|----------------|
| `/list` or `/ls` | List all sessions | `/list` |
| `/status <id>` | Show session details | `/status abc12345` |
| `/send <id> <text>` | Send command to session | `/send abc12345 pwd` |
| `/confirm <id>` | Confirm (Enter key) | `/confirm abc12345` |
| `/select <id> <n>` | Select option N | `/select abc12345 2` |
| `/interrupt <id>` | Send Ctrl+C | `/interrupt abc12345` |
| `/stop <id>` | Stop session | `/stop abc12345` |
| `/help` | Show help | `/help` |

> ID supports fuzzy matching — first 8 characters are sufficient. / ID 支持模糊匹配，只需输入前 8 位。

## Session Types / 会话类型

| Icon / 图标 | Type / 类型 | Control Method / 控制方式 | Output Reading / 输出读取 |
|:----------:|------------|--------------------------|--------------------------|
| 💻 | Terminal (native) | `screen -X stuff` or AppleScript | Log file tail |
| 🔌 | IDE (IntelliJ/PyCharm/VS Code) | AppleScript keystroke simulation | Select All → Copy → Clipboard |
| 💻 | Standalone | Fallback: screen → AppleScript | Log file tail |

## Status Detection / 状态检测

| Status / 状态 | Icon / 图标 | Meaning / 含义 |
|:------------:|:----------:|---------------|
| `running` | 🟢 | Command is executing, producing output |
| `waiting` | 🟡 | Waiting for user input (confirmation, selection, etc.) |
| `idle` | ⚪ | Process sleeping, no waiting pattern detected |
| `stopped` | 🔴 | Process has exited |

## Files / 文件结构

```
/Users/dp/repo/claude-remote/
├── daemon.py           # Main daemon - HTTP API + health check + event consume
├── lark_bot.py         # Lark bot - command parsing + message reply
├── screen_manager.py   # Screen session lifecycle management
├── registry.py         # SQLite session registry
├── ide_control.py      # IDE terminal control via AppleScript
├── config.py           # Configuration from env vars
├── scan-existing       # Scan & register existing claude processes
├── lcc                 # (deprecated) Screen wrapper for new sessions
├── ide-register        # (deprecated) IDE registration script
├── LICENSE             # MIT License
└── README.md           # This file
```

## Environment Variables / 环境变量

| Variable / 变量 | Default / 默认值 | Description / 说明 |
|----------------|-----------------|-------------------|
| `CCR_DAEMON_PORT` | `9998` | Daemon HTTP port |
| `CCR_DATA_DIR` | `~/.claude-remote` | Data storage directory |
| `CCR_CLAUDE_BIN` | `claude` | Claude binary path |
| `CCR_LOG_PATH` | `/tmp/claude-daemon.log` | Log file path |
| `LARK_APP_ID` | - | Lark App ID (required) |
| `LARK_APP_SECRET` | - | Lark App Secret (required) |

## Lark App Setup / Lark 应用配置

1. Open [Lark Open Platform](https://open.feishu.cn) → Create App
2. Enable **Bot** capability / 开启机器人能力
3. Add permissions / 添加权限: `im:message.p2p_msg:readonly`
4. Add event / 添加事件: `im.message.receive_v1` (callback URL is NOT needed, we use event consume)
5. Publish a new version / 发布新版本
6. Set `LARK_APP_ID` and `LARK_APP_SECRET` as environment variables

## License / 许可证

MIT License — see [LICENSE](LICENSE) for details.

---

Built with ❤️ for macOS + Lark + Claude Code