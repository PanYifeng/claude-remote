"""Lark 机器人 — 消息收发与命令处理"""

import json
import logging
import shlex
from typing import Optional

from config import config
from registry import SessionRegistry
from screen_manager import ScreenManager
from ide_control import IDEControl

logger = logging.getLogger("lark_bot")

# 可用的命令及说明
COMMAND_HELP = """
/l list                       — 列出所有 session（💻=终端, 🔌=IDE）
/ls                           — 同 /list
/status <id>                  — 查看 session 详情
/send <id> <text>             — 向 session 发送命令
/confirm <id>                 — 确认操作（回车）
/select <id> <n>              — 选择第 N 个选项
/interrupt <id>               — 发送 Ctrl+C
/stop <id>                    — 终止 session
/help                         — 显示此帮助

远程控制类型:
💻 screen — 独立终端（通过 screen 命令管道控制）
🔌 IDE    — IDE 终端（通过 macOS 辅助功能模拟键盘）
📡 支持独立终端 / IntelliJ / PyCharm / VS Code / Cursor
"""


class LarkBot:
    """通过 lark-cli 和 Lark Open API 实现机器人消息处理"""

    def __init__(
        self,
        registry: SessionRegistry,
        screen_mgr: ScreenManager,
        ide_ctrl: Optional[IDEControl] = None,
    ):
        self.registry = registry
        self.screen_mgr = screen_mgr
        self.ide_ctrl = ide_ctrl or IDEControl()

    async def handle_webhook(self, body: dict) -> Optional[dict]:
        """处理 Lark 事件回调

        Lark 事件类型:
        - url_verification: URL 验证挑战
        - im.message.receive_v1: 收到用户消息
        """
        event_type = body.get("type", "")

        # URL 验证
        if event_type == "url_verification":
            return {"challenge": body.get("challenge", "")}

        # 事件回调
        event = body.get("event", {})
        header = body.get("header", {})

        # Lark v2 事件格式
        if header.get("event_type") == "im.message.receive_v1":
            return await self._handle_message_event(event)

        # Lark v1 事件格式
        if event_type == "im.message.receive_v1":
            return await self._handle_message_event(event)

        logger.info("Unhandled event type: %s", event_type)
        return None

    async def _handle_message_event(self, event: dict) -> Optional[dict]:
        """处理收到的用户消息"""
        try:
            message = event.get("message", {})
            sender = event.get("sender", {})

            # 获取消息内容
            msg_type = message.get("message_type", "")
            content_str = message.get("content", "{}")

            # 提取文本内容
            text = self._extract_text(msg_type, content_str)
            if not text:
                return None

            # 获取发送者信息
            sender_id = (
                sender.get("sender_id", {}).get("open_id", "")
                or sender.get("user_id", "")
            )
            chat_id = message.get("chat_id", "")

            # 处理命令
            response = await self._process_command(text.strip(), sender_id)
            if response:
                await self._send_message(chat_id, response)

            return {"code": 0}
        except Exception as e:
            logger.error("Failed to handle message event: %s", e, exc_info=True)
            return None

    async def _process_command(
        self, text: str, sender_id: str
    ) -> Optional[str]:
        """解析并执行命令

        格式: /命令 [参数...]
        """
        # 去掉 / 前缀
        if not text.startswith("/"):
            return None

        # 解析命令
        parts = shlex.split(text[1:])
        if not parts:
            return COMMAND_HELP

        cmd = parts[0].lower()
        args = parts[1:]

        # 命令分发
        handlers = {
            "list": self._cmd_list,
            "l": self._cmd_list,
            "ls": self._cmd_list,
            "status": self._cmd_status,
            "send": self._cmd_send,
            "confirm": self._cmd_confirm,
            "select": self._cmd_select,
            "interrupt": self._cmd_interrupt,
            "stop": self._cmd_stop,
            "help": self._cmd_help,
        }

        handler = handlers.get(cmd)
        if not handler:
            return f"未知命令: /{cmd}\n{COMMAND_HELP}"

        return await handler(args)

    async def _cmd_list(self, _args: list[str]) -> str:
        """列出所有 session"""
        sessions = self.registry.list()

        if not sessions:
            return "📭 当前没有活跃的 Claude Code session。"

        lines = ["📋 **Claude Code Session 列表**\n"]
        for s in sessions:
            status_icon = {
                "running": "🟢",
                "waiting": "🟡",
                "idle": "⚪",
                "stopped": "🔴",
                "error": "❌",
            }.get(s.get("status", ""), "⚪")

            # 会话类型图标
            stype = s.get("session_type", "screen")
            type_icon = "💻" if stype == "screen" else "🔌"

            sid = s["id"][:8]  # 短 ID
            name = s.get("name", "") or sid
            cwd = s.get("cwd", "") or "-"
            created = s.get("created_at", 0)
            elapsed = self._format_elapsed(created)
            app = s.get("app_name", "")

            line = f"{status_icon} {type_icon} `{sid}` — **{name}**\n"
            if stype == "ide":
                line += f"  · 应用: `{app or '-'}`\n"
            else:
                line += f"  · 目录: `{cwd}`\n"
            line += f"  · 运行: {elapsed}\n"

            lines.append(line)

        lines.append(f"\n共 {len(sessions)} 个 session")
        lines.append("输入 `/status <id>` 查看详情")
        lines.append("输入 `/help` 查看所有命令")
        return "\n".join(lines)

    async def _cmd_status(self, args: list[str]) -> str:
        """查看 session 详情"""
        if not args:
            return "用法: `/status <session_id>`\n例: `/status abc12345`"

        session_id = self._resolve_id(args[0])
        if not session_id:
            return f"❌ 未找到 session: `{args[0]}`"

        s = self.registry.get(session_id)
        if not s:
            return f"❌ session 已不存在"

        stype = s.get("session_type", "screen")
        status_icon = {
            "running": "🟢 运行中",
            "waiting": "🟡 等待输入",
            "idle": "⚪ 空闲",
            "stopped": "🔴 已停止",
            "error": "❌ 错误",
        }.get(s.get("status", ""), s.get("status", "未知"))

        lines = [
            f"📊 **Session 详情**\n",
            f"  · ID: `{session_id[:12]}...`",
            f"  · 名称: {s.get('name', '-')}",
            f"  · 状态: {status_icon}",
        ]
        if stype == "ide":
            lines.append(f"  · 类型: 🔌 IDE 终端 ({s.get('app_name', '-')})")
        else:
            lines.append(f"  · 类型: 💻 独立终端 (screen)")
        lines.append(f"  · 目录: `{s.get('cwd', '-')}`")
        lines.append(f"  · PID: {s.get('pid', '-')}")
        lines.append(f"  · 创建: {self._format_elapsed(s.get('created_at', 0))}前")

        # 获取最新输出
        if stype == "screen":
            output, line_count = await self.screen_mgr.read_output(session_id, 15)
            if output:
                lines.append(f"\n📄 **最新输出** ({line_count} 行):")
                lines.append(f"```\n{output[-500:]}\n```")
        else:
            lines.append("\n📄 IDE 终端的输出无法自动读取，")
            lines.append("   请直接查看 IDE 中的终端窗口。")

        # 操作提示
        if s.get("status") in ("waiting", "running", "idle"):
            lines.append(
                "\n💡 可执行操作:\n"
                f"  · `/confirm {session_id[:8]}` — 确认\n"
                f"  · `/send {session_id[:8]} <命令>` — 发送命令\n"
                f"  · `/interrupt {session_id[:8]}` — 中断\n"
                f"  · `/stop {session_id[:8]}` — 停止"
            )

        return "\n".join(lines)

    async def _cmd_send(self, args: list[str]) -> str:
        """发送命令到 session"""
        if len(args) < 2:
            return "用法: `/send <session_id> <text>`\n例: `/send abc12345 python train.py`"

        session_id = self._resolve_id(args[0])
        if not session_id:
            return f"❌ 未找到 session: `{args[0]}`"

        text = " ".join(args[1:])
        s = self.registry.get(session_id)
        if not s:
            return f"❌ session 已不存在"

        stype = s.get("session_type", "screen")
        if stype == "ide":
            app = s.get("app_name", "")
            ok = self.ide_ctrl.send_keys(app, text)
        else:
            ok = await self.screen_mgr.send_keys(session_id, text)

        if ok:
            self.registry.update(session_id, status="running")
            type_tag = "🔌 IDE" if stype == "ide" else "💻"
            return f"✅ {type_tag} 已发送命令到 `{session_id[:8]}...`:\n```\n$ {text}\n```"
        else:
            return f"❌ 发送失败，session 可能已停止"

    async def _cmd_confirm(self, args: list[str]) -> str:
        """确认操作（回车）"""
        if not args:
            return "用法: `/confirm <session_id>`"

        session_id = self._resolve_id(args[0])
        if not session_id:
            return f"❌ 未找到 session: `{args[0]}`"

        s = self.registry.get(session_id)
        if not s:
            return f"❌ session 已不存在"

        stype = s.get("session_type", "screen")
        if stype == "ide":
            ok = self.ide_ctrl.send_enter(s.get("app_name", ""))
        else:
            ok = await self.screen_mgr.send_enter(session_id)

        if ok:
            self.registry.update(session_id, status="running")
            return f"✅ 已确认 `{session_id[:8]}...`"
        else:
            return f"❌ 操作失败，session 可能已停止"

    async def _cmd_select(self, args: list[str]) -> str:
        """选择选项"""
        if len(args) < 2:
            return "用法: `/select <session_id> <数字>`\n例: `/select abc12345 2`"

        session_id = self._resolve_id(args[0])
        if not session_id:
            return f"❌ 未找到 session: `{args[0]}`"

        try:
            option = int(args[1])
        except ValueError:
            return "❌ 选项必须为数字"

        s = self.registry.get(session_id)
        if not s:
            return f"❌ session 已不存在"

        stype = s.get("session_type", "screen")
        if stype == "ide":
            app = s.get("app_name", "")
            ok = self.ide_ctrl.send_text(app, str(option))
            if ok:
                self.ide_ctrl.send_enter(app)
        else:
            ok = await self.screen_mgr.select_option(session_id, option)

        if ok:
            self.registry.update(session_id, status="running")
            return f"✅ 已选择选项 {option} → `{session_id[:8]}...`"
        else:
            return f"❌ 操作失败，session 可能已停止"

    async def _cmd_interrupt(self, args: list[str]) -> str:
        """中断（Ctrl+C）"""
        if not args:
            return "用法: `/interrupt <session_id>`"

        session_id = self._resolve_id(args[0])
        if not session_id:
            return f"❌ 未找到 session: `{args[0]}`"

        s = self.registry.get(session_id)
        if not s:
            return f"❌ session 已不存在"

        stype = s.get("session_type", "screen")
        if stype == "ide":
            ok = self.ide_ctrl.send_ctrl_c(s.get("app_name", ""))
        else:
            ok = await self.screen_mgr.send_ctrl_c(session_id)

        if ok:
            self.registry.update(session_id, status="running")
            return f"⚠️ 已发送中断信号到 `{session_id[:8]}...`"
        else:
            return f"❌ 操作失败，session 可能已停止"

    async def _cmd_stop(self, args: list[str]) -> str:
        """停止 session"""
        if not args:
            return "用法: `/stop <session_id>`"

        session_id = self._resolve_id(args[0])
        if not session_id:
            return f"❌ 未找到 session: `{args[0]}`"

        s = self.registry.get(session_id)
        if not s:
            return f"❌ session 已不存在"

        stype = s.get("session_type", "screen")
        if stype == "ide":
            app = s.get("app_name", "")
            self.ide_ctrl.send_ctrl_c(app)
        else:
            await self.screen_mgr.send_ctrl_c(session_id)
            await asyncio.sleep(0.5)
            await self.screen_mgr.send_keys(session_id, "exit")
            await asyncio.sleep(1)
            await self.screen_mgr.kill(session_id)

        self.registry.update(session_id, status="stopped")
        return f"⏹️ 已停止 session `{session_id[:8]}...`"

    async def _cmd_help(self, _args: list[str]) -> str:
        """显示帮助"""
        return COMMAND_HELP

    async def _send_message(self, chat_id: str, text: str) -> bool:
        """通过 lark-cli 发送消息

        使用 lark-cli im send 命令发送文本消息。
        """
        if not chat_id:
            logger.warning("No chat_id for sending message")
            return False

        try:
            content = json.dumps({"text": text}, ensure_ascii=False)
            proc = await asyncio.create_subprocess_exec(
                "lark-cli", "im", "send",
                "--chat-id", chat_id,
                "--msg-type", "text",
                "--content", content,
                "--as", "bot",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate(timeout=10)
            if proc.returncode != 0:
                logger.error(
                    "Failed to send Lark message: %s", stderr.decode()
                )
                return False
            return True
        except Exception as e:
            logger.error("Error sending Lark message: %s", e)
            return False

    def _resolve_id(self, short_id: str) -> Optional[str]:
        """将短 ID（前 8 位）解析为完整 session ID"""
        sessions = self.registry.list()
        for s in sessions:
            if s["id"].startswith(short_id):
                return s["id"]
        return None

    @staticmethod
    def _extract_text(msg_type: str, content_str: str) -> Optional[str]:
        """从消息 content 中提取文本"""
        try:
            content = json.loads(content_str) if isinstance(content_str, str) else content_str
        except json.JSONDecodeError:
            return str(content_str) if content_str else None

        if msg_type == "text":
            return content.get("text", "")
        return None

    @staticmethod
    def _format_elapsed(timestamp: float) -> str:
        """格式化运行时长"""
        import time
        seconds = time.time() - timestamp
        if seconds < 60:
            return f"{int(seconds)}秒"
        elif seconds < 3600:
            return f"{int(seconds // 60)}分"
        elif seconds < 86400:
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            return f"{h}时{m}分"
        else:
            d = int(seconds // 86400)
            h = int((seconds % 86400) // 3600)
            return f"{d}天{h}时"