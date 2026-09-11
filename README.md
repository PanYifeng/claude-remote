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
│  • Session management (tmux + AppleScript)          │
│  • Interactive mode (chat with a session directly)  │
│  • Streaming output (real-time card updates)        │
│  • Periodic health check                            │
│  • Lark event consumer (bot identity)               │
└────┬───────────┬──────────┬─────────────────────────┘
     │           │          │
     ▼           ▼          ▼
   tmux        Terminal    IDE (IntelliJ/PyCharm)
  (log file)  (AppleScript) (Accessibility API)
```

## Quick Start / 快速开始

### Prerequisites / 前置条件

- macOS (requires `tmux`, install via `brew install tmux`)
- Python 3.11+
- `lark-cli` (`brew install lark-cli`)
- Claude Code (`claude`) and/or OpenCode (`opencode`, `brew install opencode`) — the agents you want to control
- Lark app (create at [Lark Open Platform](https://open.feishu.cn))

### Install / 安装

```bash
git clone https://github.com/PanYifeng/claude-remote.git
cd claude-remote
pip install aiohttp

# Install tmux (required for new sessions)
brew install tmux

export LARK_APP_ID="cli_xxxxxxxxxxxxx"
export LARK_APP_SECRET="xxxxxxxxxxxxxxxxxxxxx"

# Start daemon
python3 daemon.py

# Register all existing claude processes
python3 scan-existing

# Start heartbeat daemon (keeps sessions alive)
python3 scan-existing --daemon
```

### Start a session / 启动会话

Both **Claude Code** and **OpenCode** sessions are supported. There are two ways to launch a session the daemon can control:

**Option A — `lcc` / `loc` launcher (recommended, full feature support)**

`lcc` wraps `claude` and `loc` wraps `opencode`: each starts a detached tmux session and registers it with the daemon automatically.
- `lcc` (claude) captures output to a log file → streaming output + accurate `/status`.
- `loc` (opencode) reads status from OpenCode's SQLite DB (`~/.local/share/opencode/opencode.db`) → accurate `/status`; no log capture (TUI output is not log-friendly).

```bash
# Install the wrappers somewhere on PATH
sudo cp lcc /usr/local/bin/lcc && chmod +x /usr/local/bin/lcc
sudo cp loc /usr/local/bin/loc && chmod +x /usr/local/bin/loc

# From the project directory you want the agent to work in:
lcc                      # background claude in tmux (control from Lark)
loc                      # background opencode in tmux (control from Lark)
tmux attach -t claude-<id>   # optionally attach locally to watch

lcc --name "论文搜索"     # name the session (shown in /l)
lcc --foreground         # run claude in the current terminal instead of tmux
loc --foreground         # run opencode in the current terminal
```

You can also start a session remotely from Lark: `/new <path>` (claude) or `/new-opencode <path>` (opencode) — both enter interactive mode.

**Option B — `scan-existing` (adopt already-running sessions)**

If you already have `claude` or `opencode` running in a terminal or IDE, `scan-existing` discovers and registers those processes (run above). These native terminal/IDE sessions support `/send`, `/confirm`, `/interrupt` (terminal sessions are targeted by TTY) but have no tmux log file, so no streaming output — use `/status <id>` to read live output on demand.

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
pwd         → sent to session, output returned (streaming card updates)
ls -la      → sent to session
/exit       → leave interactive mode, session kept alive
/exit --kill → leave and stop session (card updated with stop status)
```

> **Streaming output:** Interactive mode sends commands to tmux, then polls the log file every 2 seconds and updates the Lark card in-place via PATCH API. You see output appear progressively as the command executes — no more waiting for completion.

### Control Commands / 控制命令

| Command / 命令 | Description / 说明 |
|---------------|-------------------|
| `/send <id\|N> <text>` | Send command to session |
| `/confirm [id\|N]` | Confirm (Enter). No arg = last session |
| `/interrupt [id\|N]` | Send Ctrl+C. No arg = last session |
| `/stop <id\|N>` | Stop session |
| `/select <id\|N> <n>` | Select option N |
| `/daemon (stop\|restart\|status)` | Control daemon service |

### Host Commands / 主机命令

| Command / 命令 | Description / 说明 | Example / 示例 |
|---------------|-------------------|----------------|
| `/ls [path]` | List directory contents | `/ls ~` or `/ls /Users/dp/repo` |
| `/help` | Show help | `/help` |

### Tips / 提示

- Use number shortcuts: `/confirm 1`, `/status 2`, `/send 3 pwd`
- `/confirm` or `/interrupt` without ID acts on last session
- `/new <path>` creates a tmux session + log file for accurate output reading
- `/send` pastes via the clipboard (supports Chinese and other non-ASCII text; keystroke alone cannot type multibyte characters)
- ID supports fuzzy matching — first 8 chars are sufficient
- `/exit --kill` updates the last interactive card with stop status
- `/status <id>` reads live terminal output for IDE sessions — detects waiting/idle/executing

## Session Types / 会话类型

Sessions are tagged by **agent** (Claude 🟦/💻 vs OpenCode 🟦) and **type** (how it runs):

| Icon | Agent / Type | Output Reading | Interactive Mode | Status Detection |
|:----:|------|:-------------:|:----------------:|:----------------:|
| 💻 | Claude — Tmux (`/new`, `lcc`) | Log file ✅ | ✅ Full support (streaming) | ✅ Auto (log + state file) |
| 💻 | Claude — Terminal/IDE (native) | Live read (`/status`) | ⚠️ Send only | ✅ Auto (state file) |
| 🟦 | OpenCode — Tmux (`/new-opencode`, `loc`) | SQLite ✅ | ✅ (last assistant text) | ✅ Auto (SQLite) |
| 🟦 | OpenCode — Terminal (native) | SQLite (`/status`) | ⚠️ Send only | ✅ Auto (SQLite) |

> **OpenCode status** is read from `~/.local/share/opencode/opencode.db`: `idle` when the last step finished with `reason=stop`, `executing` otherwise (stale >120s → idle). Permission-prompt (`waiting`) auto-detection is best-effort — use `/status` to view live output.

## Status Detection / 状态检测

| Status | Icon | Meaning | When |
|:------:|:----:|---------|------|
| `running` | 🟢 | Alive, unknown status | Default for IDE/terminal sessions without log files |
| `executing` | 🔵 | Task in progress | Log file shows output with no prompt |
| `waiting` | 🟡 | Waiting for confirm/input | Output contains approval prompts |
| `idle` | ⏸️ | At prompt, no task running | Output ends with ❯ / $ prompt |
| `stopped` | 🔴 | Process exited or killed | Process is dead or screen/tmux session gone |

**Note:** IDE sessions (🔌) cannot be auto-detected by health check (would steal focus on every cycle).
Use `/status <id>` to read live terminal output and get the accurate status (waiting, idle, or executing).

Status detection uses multiple strategies:
1. **Log file analysis:** Tmux sessions read log output every 5s; checks for waiting patterns, prompt suffix, or executing content
2. **Live terminal read:** `/status <id>` reads IDE terminal content via AppleScript (Cmd+A → Cmd+C) to detect waiting/idle/executing
3. **Waiting pattern matching:** Scans all lines for "Do you want to proceed?", "requires approval", "Esc to cancel", "Enter to confirm", etc.

## Status Display / 卡片展示

Each session in `/l` shows:
- Status icon (🟢🔵🟡⏸️🔴)
- Type icon (💻🔌)
- Number shortcut `[N]`
- App name + project directory + TTY
- Copyable command line

## Files / 文件结构

```
/Users/dp/repo/claude-remote/
├── daemon.py           # Main daemon — HTTP API + health check + event consume
├── lark_bot.py         # Lark bot — command parsing, interactive mode, streaming card updates
├── lark_card.py        # Card builder — session list, status, streaming, done cards
├── screen_manager.py   # Tmux session lifecycle management (replaces macOS broken screen)
├── registry.py         # SQLite session registry
├── ide_control.py      # IDE terminal control via AppleScript
├── opencode_state.py   # Read OpenCode session state from SQLite
├── agent_state.py      # Unified state reader (dispatches claude vs opencode)
├── dispatch.py         # Shared send/enter/ctrl-c router (used by lark_bot & daemon)
├── config.py           # Configuration from env vars
├── lcc                 # Launcher — start a tmux + daemon-registered claude session
├── loc                 # Launcher — start a tmux + daemon-registered opencode session
├── ide-register        # Register an existing IDE/terminal claude session
├── scan-existing       # Scan & register existing claude/opencode processes with heartbeat daemon
├── LICENSE             # MIT License
└── README.md           # This file
```

## Environment Variables / 环境变量

| Variable | Default | Description |
|----------|---------|-------------|
| `CCR_DAEMON_PORT` | `9998` | Daemon HTTP port |
| `CCR_DATA_DIR` | `~/.claude-remote` | Data storage directory |
| `CCR_LOG_PATH` | `/tmp/claude-daemon.log` | Log file path |
| `CCR_DAEMON_URL` | `http://localhost:9998` | Daemon URL (used by `lcc`/`loc`) |
| `CCR_SESSION_ID` | auto (UUID) | Force a session ID (used by `lcc`/`loc`) |
| `OPENCODE_DB` | `~/.local/share/opencode/opencode.db` | OpenCode SQLite DB path (status reading) |
| `LARK_APP_ID` | - | Lark App ID (required) |
| `LARK_APP_SECRET` | - | Lark App Secret (required) |

## Lark App Setup / Lark 应用配置

1. Open [Lark Open Platform](https://open.feishu.cn) → Create App
2. Enable **Bot** capability
3. Add permissions: `im:message.p2p_msg:readonly`
4. Add event: `im.message.receive_v1` (callback URL not needed, uses event consume)
5. Publish a new version
6. Set `LARK_APP_ID` and `LARK_APP_SECRET` as environment variables

## Technical Notes / 技术说明

### Why tmux instead of screen?

macOS ships with `screen` version 4.00.03 (2006). Its `-X stuff` command returns success (exit code 0) but does not actually inject input into the session. `tmux` (installable via `brew install tmux`) provides reliable `send-keys` and is actively maintained.

### Script flush interval

The `-t 0` flag is passed to `script` to flush output after every character I/O event (default is 30 seconds). This ensures the log file is readable in real-time for streaming output.

### Duplicate session prevention

Each registered session has a `user_stopped` tag when stopped by the user. The `scan-existing` heartbeat daemon skips user-stopped sessions, preventing them from being re-activated.

## License / 许可证

MIT License — see [LICENSE](LICENSE) for details.

---

Built with ❤️ for macOS + Lark + Claude Code