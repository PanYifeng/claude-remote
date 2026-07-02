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
└─────────────────────┬───────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────┐
│  Daemon (Python aiohttp, port 9998)                 │
│  • HTTP API server                                  │
│  • Session registry (SQLite)                        │
│  • Session management (screen + AppleScript)        │
│  • Interactive mode (chat with a session directly)  │
│  • Periodic health check                            │
│  • Lark event consumer (bot identity)               │
└────┬───────────┬──────────┬─────────────────────────┘
     │           │          │
     ▼           ▼          ▼
  screen       Terminal    IDE (IntelliJ/PyCharm)
  (log file)   (AppleScript) (Accessibility API)
```

## Quick Start / 快速开始

### Prerequisites / 前置条件

- macOS (requires `/usr/bin/screen`)
- Python 3.11+
- `lark-cli` (`brew install lark-cli`)
- Lark app (create at [Lark Open Platform](https://open.feishu.cn))

### Install / 安装

```bash
git clone https://github.com/PanYifeng/claude-remote.git
cd claude-remote
pip install aiohttp

export LARK_APP_ID="cli_xxxxxxxxxxxxx"
export LARK_APP_SECRET="xxxxxxxxxxxxxxxxxxxxx"

# Start daemon
python3 daemon.py

# Register all existing claude processes
python3 scan-existing

# Start heartbeat daemon (keeps sessions alive)
python3 scan-existing --daemon
```

## Lark Bot Commands / 飞书机器人命令

### Session Management / 会话管理

| Command / 命令 | Description / 说明 | Example / 示例 |
|---------------|-------------------|----------------|
| `/l` | List all sessions | `/l` |
| `/status <id\|N>` | Show session details | `/status 1` |
| `/pending` | List sessions waiting for input | `/pending` |
| `/confirm-all` | Confirm all waiting sessions | `/confirm-all` |

### Interactive Mode / 交互模式

| Command / 命令 | Description / 说明 | Example / 示例 |
|---------------|-------------------|----------------|
| `/new <path>` | Start a new claude session + enter interactive mode | `/new /Users/dp/repo` |
| `/enter <id\|N>` | Enter interactive mode with a session | `/enter 1` |
| `/exit [--kill]` | Exit interactive mode (`--kill` also stops session) | `/exit` |
| *(any text)* | In interactive mode, send text directly to session | `pwd` |

**Interactive mode flow / 交互模式流程:**
```
/l          → see sessions, note the number
/enter 1    → enter interactive mode with session 1
pwd         → sent to session, output returned
ls -la      → sent to session
/exit       → leave interactive mode, session kept alive
/exit --kill → leave and stop session
```

### Control Commands / 控制命令

| Command / 命令 | Description / 说明 |
|---------------|-------------------|
| `/send <id\|N> <text>` | Send command to session |
| `/confirm [id\|N]` | Confirm (Enter). No arg = last session |
| `/interrupt [id\|N]` | Send Ctrl+C. No arg = last session |
| `/stop <id\|N>` | Stop session |
| `/select <id\|N> <n>` | Select option N |

### Host Commands / 主机命令

| Command / 命令 | Description / 说明 | Example / 示例 |
|---------------|-------------------|----------------|
| `/ls [path]` | List directory contents | `/ls ~` or `/ls /Users/dp/repo` |
| `/help` | Show help | `/help` |

### Tips / 提示

- Use number shortcuts: `/confirm 1`, `/status 2`, `/send 3 pwd`
- `/confirm` or `/interrupt` without ID acts on last session
- `/new <path>` creates a screen session + log file for accurate output reading
- ID supports fuzzy matching — first 8 chars are sufficient

## Session Types / 会话类型

| Icon | Type | Output Reading | Interactive Mode |
|:----:|------|:-------------:|:----------------:|
| 💻 | Screen (`/new`) | Log file ✅ | ✅ Full support |
| 💻 | Terminal (native) | Frontmost window | ⚠️ Send only |
| 🔌 | IDE (IntelliJ/PyCharm) | Clipboard capture | ⚠️ Send only |

## Status Detection / 状态检测

| Status | Icon | Meaning |
|:------:|:----:|---------|
| `running` | 🟢 | Alive, status unknown (no log file) |
| `executing` | 🔵 | Has output → task in progress |
| `waiting` | 🟡 | Waiting for confirm/input |
| `idle` | ⚪ | At prompt, no task running |
| `stopped` | 🔴 | Process exited |

## Status Display / 卡片展示

Each session in `/l` shows:
- Status icon (🟢🔵🟡⚪)
- Type icon (💻🔌)
- Number shortcut `[N]`
- App name + project directory + TTY
- Copyable command line

## Files / 文件结构

```
/Users/dp/repo/claude-remote/
├── daemon.py           # Main daemon — HTTP API + health check + event consume
├── lark_bot.py         # Lark bot — command parsing, interactive mode, card sending
├── lark_card.py        # Card builder — session list, status, interactive cards
├── screen_manager.py   # Screen session lifecycle management
├── registry.py         # SQLite session registry
├── ide_control.py      # IDE terminal control via AppleScript
├── config.py           # Configuration from env vars
├── scan-existing       # Scan & register existing claude processes
├── LICENSE             # MIT License
└── README.md           # This file
```

## Environment Variables / 环境变量

| Variable | Default | Description |
|----------|---------|-------------|
| `CCR_DAEMON_PORT` | `9998` | Daemon HTTP port |
| `CCR_DATA_DIR` | `~/.claude-remote` | Data storage directory |
| `CCR_LOG_PATH` | `/tmp/claude-daemon.log` | Log file path |
| `LARK_APP_ID` | - | Lark App ID (required) |
| `LARK_APP_SECRET` | - | Lark App Secret (required) |

## Lark App Setup / Lark 应用配置

1. Open [Lark Open Platform](https://open.feishu.cn) → Create App
2. Enable **Bot** capability
3. Add permissions: `im:message.p2p_msg:readonly`
4. Add event: `im.message.receive_v1` (callback URL not needed, uses event consume)
5. Publish a new version
6. Set `LARK_APP_ID` and `LARK_APP_SECRET` as environment variables

## License / 许可证

MIT License — see [LICENSE](LICENSE) for details.

---

Built with ❤️ for macOS + Lark + Claude Code