# Claude Code Remote (ccr)

在手机上通过飞书（Lark）远程监控和控制本机所有 Claude Code shell 会话。

## 系统架构

```
┌─────────────────────────────────────────────────────┐
│  Lark App (Bot)                                     │
│  • 消息推送: 用户发命令/收到回复                    │
│  • Webhook: Lark 事件 → cloudflared → 本机          │
└──────────┬──────────────────────────┬──────────────┘
           │ Lark Open API (发送消息)   │ Webhook (接收事件)
           ▼                           ▼
┌─────────────────────────────────────────────────────┐
│  Cloudflare Tunnel (cloudflared)                    │
│  • 端口 9998 → 公网 URL                             │
└──────────────────┬──────────────────────────────────┘
                   │ localhost:9998
                   ▼
┌─────────────────────────────────────────────────────┐
│  Daemon (Python aiohttp, 端口 9998)                 │
│  • HTTP API 服务器                                  │
│  • Lark webhook 处理器                              │
│  • Session 注册表 (SQLite)                          │
│  • Screen 会话管理                                  │
│  • 定时健康检查                                     │
└────┬───────────┬──────────┬─────────────────────────┘
     │           │          │
     ▼           ▼          ▼
  screen-1    screen-2    screen-3 (Claude Code sessions)
     │           │          │
     ▼           ▼          ▼
  /tmp/claude-<uuid>.log (script 捕获的输出)
```

## 快速开始

### 前置条件

- Python 3.11+（使用项目 `.venv`）
- macOS（需要 `/usr/bin/screen`）
- [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)（用于公网访问）
- Lark 应用（在 [Lark Open Platform](https://open.feishu.cn) 创建）

### 安装

```bash
# 1. 安装依赖
cd /Users/dp/repo/claude-remote
/Users/dp/repo/.venv/bin/pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2. 设置环境变量
export LARK_APP_ID="cli_xxxxxxxxxxxxx"
export LARK_APP_SECRET="xxxxxxxxxxxxxxxxxxxxx"
export CCR_TUNNEL_DOMAIN="claude-remote.example.com"

# 3. 安装 lcc 包装器
sudo cp lcc /usr/local/bin/lcc

# 4. 启动 daemon
/Users/dp/repo/.venv/bin/python3 daemon.py

# 5. 在另一个终端启动 Cloudflare 隧道
cloudflared tunnel --url http://localhost:9998
```

### 使用

```bash
# 启动带远程监控的 Claude Code 会话
lcc

# 指定会话名称
lcc --name "论文搜索"

# 前台运行（等同直接运行 claude）
lcc --foreground

# 连接到后台 screen 会话
screen -r claude-<id>
```

## Lark 机器人命令

在 Lark 聊天中与机器人对话：

| 命令 | 说明 | 示例 |
|------|------|------|
| `/list` 或 `/ls` | 列出所有 session | `/list` |
| `/status <id>` | 查看 session 详情 | `/status abc12345` |
| `/send <id> <text>` | 发送命令 | `/send abc12345 python train.py` |
| `/confirm <id>` | 确认操作（回车） | `/confirm abc12345` |
| `/select <id> <n>` | 选择第 N 个选项 | `/select abc12345 2` |
| `/interrupt <id>` | 发送 Ctrl+C | `/interrupt abc12345` |
| `/stop <id>` | 终止 session | `/stop abc12345` |
| `/help` | 显示帮助 | `/help` |

> ID 支持模糊匹配：只需输入前 8 位即可，如 `/status abc12345`

## HTTP API

Daemon 在 `http://127.0.0.1:9998` 提供 RESTful API：

### Session 管理

```bash
# 列出所有 session
curl http://localhost:9998/api/sessions

# 获取 session 详情（含实时输出）
curl http://localhost:9998/api/session/<id>

# 注册 session（由 lcc wrapper 调用）
curl -X POST http://localhost:9998/api/session/register \
  -H "Content-Type: application/json" \
  -d '{"id":"uuid","name":"会话名","cwd":"/path"}'

# 发送命令
curl -X POST http://localhost:9998/api/session/<id>/send \
  -H "Content-Type: application/json" \
  -d '{"text":"ls -la"}'

# 确认（回车）
curl -X POST http://localhost:9998/api/session/<id>/confirm

# 选择选项
curl -X POST http://localhost:9998/api/session/<id>/select \
  -H "Content-Type: application/json" \
  -d '{"option":2}'

# 中断（Ctrl+C）
curl -X POST http://localhost:9998/api/session/<id>/interrupt

# 终止 session
curl -X POST http://localhost:9998/api/session/<id>/stop
```

### Lark Webhook

```bash
# Lark URL 验证
curl -X POST http://localhost:9998/lark/webhook \
  -H "Content-Type: application/json" \
  -d '{"challenge":"test","type":"url_verification"}'

# 健康检查
curl http://localhost:9998/health
```

## Session 状态说明

| 状态 | 图标 | 说明 |
|------|------|------|
| `running` | 🟢 | 正在执行命令，持续有输出 |
| `waiting` | 🟡 | 等待用户输入（确认、选择等） |
| `idle` | ⚪ | 进程休眠，无等待模式 |
| `stopped` | 🔴 | 已停止 |
| `error` | ❌ | 异常状态 |

## 配置文件

所有环境变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CCR_DAEMON_PORT` | `9998` | Daemon HTTP 端口 |
| `CCR_DATA_DIR` | `~/.claude-remote` | 数据存储目录 |
| `CCR_CLAUDE_BIN` | `claude` | Claude 二进制路径 |
| `CCR_LOG_PATH` | `/tmp/claude-daemon.log` | 日志文件 |
| `LARK_APP_ID` | - | Lark 应用 ID |
| `LARK_APP_SECRET` | - | Lark 应用 Secret |

## Launchd 服务（开机自启）

```bash
# 安装 daemon 为 launchd 服务
cat > ~/Library/LaunchAgents/com.claude.remote.plist << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude.remote</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/dp/repo/.venv/bin/python3</string>
        <string>/Users/dp/repo/claude-remote/daemon.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/claude-daemon.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/claude-daemon.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>LARK_APP_ID</key>
        <string>$LARK_APP_ID</string>
        <key>LARK_APP_SECRET</key>
        <string>$LARK_APP_SECRET</string>
    </dict>
</dict>
</plist>
PLIST

launchctl load ~/Library/LaunchAgents/com.claude.remote.plist

# 查看日志
tail -f /tmp/claude-daemon.log

# 停止服务
launchctl unload ~/Library/LaunchAgents/com.claude.remote.plist
```

## Lark 应用设置

1. 打开 [Lark Open Platform](https://open.feishu.cn) → 创建企业自建应用
2. 开启 **机器人能力**（Bot）
3. 在权限管理中申请：
   - `im:message:send_as_bot` — 机器人发送消息
   - `im:message:read` — 读取消息
4. 配置事件订阅：`im.message.receive_v1`（接收用户消息）
5. 设置 webhook URL：`https://<your-tunnel-domain>/lark/webhook`
6. 发布应用版本并启用
7. 将 `LARK_APP_ID` 和 `LARK_APP_SECRET` 设为环境变量

## IDEA 插件终端控制（第二阶段）

对于 VS Code / JetBrains IDE 插件中的 Claude Code 终端，由于外部进程无法直接写入 IDE 终端，第二阶段将通过 macOS Accessibility API 实现控制：

- 使用 AppleScript / Swift 定位终端窗口
- 通过 `AXUIElementPostKeyboardEvent` 模拟键盘输入
- 需要系统辅助功能权限授权

## 文件结构

```
/Users/dp/repo/claude-remote/
├── daemon.py           # 主 daemon 进程
├── lcc                 # Bash 包装器脚本
├── lark_bot.py         # Lark 机器人处理器
├── screen_manager.py   # Screen 会话管理
├── registry.py         # SQLite 注册表
├── config.py           # 配置模块
├── data/               # 运行时数据
└── README.md           # 本文档
```

## 开发

```bash
# 测试注册表
/Users/dp/repo/.venv/bin/python3 -c "from registry import SessionRegistry; r = SessionRegistry(':memory:'); r.register('test','screen-test',cwd='/tmp'); print('OK')"

# 测试 screen 管理（需要 macOS）
/Users/dp/repo/.venv/bin/python3 -c "import asyncio; from screen_manager import ScreenManager; m = ScreenManager(); asyncio.run(m.create('test-id','/tmp',command='sleep 10')); print('OK')"
```