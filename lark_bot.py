"""Lark Bot — Event consumption and message handling

Uses lark-cli event consumption instead of webhook:
- Receives messages via `lark-cli event consume im.message.receive_v1 --as bot`
- Sends replies via `lark-cli im send`

No Cloudflare tunnel or public webhook required.
"""

import asyncio
import json
import logging
import shlex
from typing import Optional

from config import config
from registry import SessionRegistry
from screen_manager import ScreenManager
from ide_control import IDEControl

logger = logging.getLogger("lark_bot")

COMMAND_HELP = """
/l list                       — List all sessions (💻=terminal, 🔌=IDE)
/ls                           — Same as /list
/status <id>                  — Show session details
/send <id> <text>             — Send command to session
/confirm <id>                 — Confirm operation (Enter)
/pending                      — List sessions waiting for input
/confirm-all                  — Confirm all waiting sessions at once
/select <id> <n>              — Select option N
/interrupt <id>               — Send Ctrl+C
/stop <id>                    — Stop session
/help                         — Show this help

Remote control types:
💻 terminal — Native terminal (via screen command pipe / AppleScript)
🔌 IDE      — IDE terminal (via macOS Accessibility keyboard simulation)
"""


class LarkBot:
    """Lark bot message handling via lark-cli"""

    def __init__(
        self,
        registry: SessionRegistry,
        screen_mgr: ScreenManager,
        ide_ctrl: Optional[IDEControl] = None,
    ):
        self.registry = registry
        self.screen_mgr = screen_mgr
        self.ide_ctrl = ide_ctrl or IDEControl()

    async def handle_event_line(self, line: dict) -> Optional[dict]:
        """Process a single event from the event consume stream

        lark-cli event consume output format:
        {
            "chat_id": "oc_xxx",
            "chat_type": "p2p",
            "content": "message text",
            "message_id": "om_xxx",
            "message_type": "text",
            "sender_id": "ou_xxx",
        }
        """
        try:
            msg_type = line.get("message_type", "")
            content = line.get("content", "")
            chat_id = line.get("chat_id", "")
            message_id = line.get("message_id", "")
            sender_id = line.get("sender_id", "")

            if not content or not chat_id:
                return None

            response = await self._process_command(content.strip(), sender_id)
            if response:
                await self._reply_message(chat_id, message_id, response)

            return {"code": 0}
        except Exception as e:
            logger.error("Failed to handle event line: %s", e, exc_info=True)
            return None

    async def handle_webhook(self, body: dict) -> Optional[dict]:
        """Handle Lark event callback (webhook mode, retained for compatibility)

        Event types:
        - url_verification: URL challenge
        - im.message.receive_v1: User message received
        """
        event_type = body.get("type", "")

        if event_type == "url_verification":
            return {"challenge": body.get("challenge", "")}

        event = body.get("event", {})
        header = body.get("header", {})

        if header.get("event_type") == "im.message.receive_v1":
            return await self._handle_message_event(event)

        if event_type == "im.message.receive_v1":
            return await self._handle_message_event(event)

        logger.info("Unhandled event type: %s", event_type)
        return None

    async def _handle_message_event(self, event: dict) -> Optional[dict]:
        """Handle received user message"""
        try:
            message = event.get("message", {})
            sender = event.get("sender", {})

            msg_type = message.get("message_type", "")
            content_str = message.get("content", "{}")

            text = self._extract_text(msg_type, content_str)
            if not text:
                return None

            sender_id = (
                sender.get("sender_id", {}).get("open_id", "")
                or sender.get("user_id", "")
            )
            chat_id = message.get("chat_id", "")

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
        """Parse and execute command

        Format: /command [args...]
        """
        if not text.startswith("/"):
            return None

        parts = shlex.split(text[1:])
        if not parts:
            return COMMAND_HELP

        cmd = parts[0].lower()
        args = parts[1:]

        handlers = {
            "list": self._cmd_list,
            "l": self._cmd_list,
            "ls": self._cmd_list,
            "status": self._cmd_status,
            "send": self._cmd_send,
            "confirm": self._cmd_confirm,
            "pending": self._cmd_pending,
            "confirm-all": self._cmd_confirm_all,
            "select": self._cmd_select,
            "interrupt": self._cmd_interrupt,
            "stop": self._cmd_stop,
            "help": self._cmd_help,
        }

        handler = handlers.get(cmd)
        if not handler:
            return f"Unknown command: /{cmd}\n{COMMAND_HELP}"

        return await handler(args)

    async def _cmd_list(self, _args: list[str]) -> str:
        """List all sessions"""
        sessions = self.registry.list()

        if not sessions:
            return "📭 No active Claude Code sessions."

        lines = ["📋 **Claude Code Session List**\n"]
        for s in sessions:
            status_icon = {
                "running": "🟢",
                "waiting": "🟡",
                "idle": "⚪",
                "stopped": "🔴",
                "error": "❌",
            }.get(s.get("status", ""), "⚪")

            stype = s.get("session_type", "screen")
            type_icon = {"screen": "💻", "ide": "🔌", "standalone": "💻", "terminal": "💻"}.get(stype, "💻")

            sid = s["id"][:8]
            name = s.get("name", "") or sid
            cwd = s.get("cwd", "") or "-"
            created = s.get("created_at", 0)
            elapsed = self._format_elapsed(created)
            app = s.get("app_name", "")

            line = f"{status_icon} {type_icon} `{sid}` — **{name}**\n"
            if stype == "ide":
                line += f"  · App: `{app or '-'}`\n"
            else:
                line += f"  · Dir: `{cwd}`\n"
            line += f"  · Running: {elapsed}\n"

            lines.append(line)

        lines.append(f"\n{len(sessions)} session(s) total")
        lines.append("Use `/status <id>` for details")
        lines.append("Use `/help` for all commands")
        return "\n".join(lines)

    async def _cmd_status(self, args: list[str]) -> str:
        """Show session details"""
        if not args:
            return "Usage: `/status <session_id>`\ne.g. `/status abc12345`"

        session_id = self._resolve_id(args[0])
        if not session_id:
            return f"❌ Session not found: `{args[0]}`"

        s = self.registry.get(session_id)
        if not s:
            return f"❌ Session no longer exists"

        stype = s.get("session_type", "screen")
        status_icon = {
            "running": "🟢 Running",
            "waiting": "🟡 Waiting for input",
            "idle": "⚪ Idle",
            "stopped": "🔴 Stopped",
            "error": "❌ Error",
        }.get(s.get("status", ""), s.get("status", "Unknown"))

        lines = [
            f"📊 **Session Details**\n",
            f"  · ID: `{session_id[:12]}...`",
            f"  · Name: {s.get('name', '-')}",
            f"  · Status: {status_icon}",
        ]
        if stype == "ide":
            lines.append(f"  · Type: 🔌 IDE Terminal ({s.get('app_name', '-')})")
        elif stype == "terminal":
            lines.append(f"  · Type: 💻 Native Terminal")
        else:
            lines.append(f"  · Type: 💻 Screen Session")
        lines.append(f"  · Dir: `{s.get('cwd', '-')}`")
        lines.append(f"  · PID: {s.get('pid', '-')}")
        lines.append(f"  · Created: {self._format_elapsed(s.get('created_at', 0))} ago")

        if stype == "ide":
            app_name = s.get("app_name", "")
            output, line_count = self.ide_ctrl.read_output(app_name, 15)
            if output:
                lines.append(f"\n📄 **Latest Output** ({line_count} lines):")
                lines.append(f"```\n{output[-500:]}\n```")
            else:
                lines.append("\n📄 Could not read IDE terminal output.")
                lines.append("   Please ensure Accessibility permission is granted.")
        elif stype == "screen":
            output, line_count = await self.screen_mgr.read_output(session_id, 15)
            if output:
                lines.append(f"\n📄 **Latest Output** ({line_count} lines):")
                lines.append(f"```\n{output[-500:]}\n```")
        else:
            output, line_count = await self.screen_mgr.read_output(session_id, 15)
            if output:
                lines.append(f"\n📄 **Latest Output** ({line_count} lines):")
                lines.append(f"```\n{output[-500:]}\n```")

        if s.get("status") in ("waiting", "running", "idle"):
            lines.append(
                "\n💡 Available actions:\n"
                f"  · `/confirm {session_id[:8]}` — Confirm (Enter)\n"
                f"  · `/send {session_id[:8]} <command>` — Send command\n"
                f"  · `/interrupt {session_id[:8]}` — Ctrl+C\n"
                f"  · `/stop {session_id[:8]}` — Stop"
            )

        return "\n".join(lines)

    async def _cmd_send(self, args: list[str]) -> str:
        """Send command to session"""
        if len(args) < 2:
            return "Usage: `/send <session_id> <text>`\ne.g. `/send abc12345 python train.py`"

        session_id = self._resolve_id(args[0])
        if not session_id:
            return f"❌ Session not found: `{args[0]}`"

        text = " ".join(args[1:])
        s = self.registry.get(session_id)
        if not s:
            return f"❌ Session no longer exists"

        stype = s.get("session_type", "screen")
        if stype == "ide":
            app = s.get("app_name", "")
            ok = self.ide_ctrl.send_keys(app, text)
        elif stype == "terminal":
            app = s.get("app_name") or "Terminal"
            ok = self.ide_ctrl.send_keys(app, text)
        else:
            ok = await self.screen_mgr.send_keys(session_id, text)

        if ok:
            self.registry.update(session_id, status="running")
            type_tag = "🔌 IDE" if stype == "ide" else "💻"
            return f"✅ {type_tag} Sent command to `{session_id[:8]}...`:\n```\n$ {text}\n```"
        else:
            return f"❌ Send failed, session may be stopped"

    async def _cmd_confirm(self, args: list[str]) -> str:
        """Confirm operation (Enter)"""
        if not args:
            return "Usage: `/confirm <session_id>`"

        session_id = self._resolve_id(args[0])
        if not session_id:
            return f"❌ Session not found: `{args[0]}`"

        s = self.registry.get(session_id)
        if not s:
            return f"❌ Session no longer exists"

        stype = s.get("session_type", "screen")
        if stype == "ide":
            ok = self.ide_ctrl.send_enter(s.get("app_name", ""))
        elif stype == "terminal":
            app = s.get("app_name") or "Terminal"
            ok = self.ide_ctrl.send_enter(app)
        else:
            ok = await self.screen_mgr.send_enter(session_id)

        if ok:
            self.registry.update(session_id, status="running")
            return f"✅ Confirmed `{session_id[:8]}...`"
        else:
            return f"❌ Operation failed, session may be stopped"

    async def _cmd_select(self, args: list[str]) -> str:
        """Select option"""
        if len(args) < 2:
            return "Usage: `/select <session_id> <number>`\ne.g. `/select abc12345 2`"

        session_id = self._resolve_id(args[0])
        if not session_id:
            return f"❌ Session not found: `{args[0]}`"

        try:
            option = int(args[1])
        except ValueError:
            return "❌ Option must be a number"

        s = self.registry.get(session_id)
        if not s:
            return f"❌ Session no longer exists"

        stype = s.get("session_type", "screen")
        if stype in ("ide", "terminal"):
            app = s.get("app_name") or ("Terminal" if stype == "terminal" else "")
            ok = self.ide_ctrl.send_text(app, str(option))
            if ok:
                self.ide_ctrl.send_enter(app)
        else:
            ok = await self.screen_mgr.select_option(session_id, option)

        if ok:
            self.registry.update(session_id, status="running")
            return f"✅ Selected option {option} → `{session_id[:8]}...`"
        else:
            return f"❌ Operation failed, session may be stopped"

    async def _cmd_interrupt(self, args: list[str]) -> str:
        """Send Ctrl+C"""
        if not args:
            return "Usage: `/interrupt <session_id>`"

        session_id = self._resolve_id(args[0])
        if not session_id:
            return f"❌ Session not found: `{args[0]}`"

        s = self.registry.get(session_id)
        if not s:
            return f"❌ Session no longer exists"

        stype = s.get("session_type", "screen")
        if stype == "ide":
            ok = self.ide_ctrl.send_ctrl_c(s.get("app_name", ""))
        elif stype == "terminal":
            app = s.get("app_name") or "Terminal"
            ok = self.ide_ctrl.send_ctrl_c(app)
        else:
            ok = await self.screen_mgr.send_ctrl_c(session_id)

        if ok:
            self.registry.update(session_id, status="running")
            return f"⚠️ Sent interrupt to `{session_id[:8]}...`"
        else:
            return f"❌ Operation failed, session may be stopped"

    async def _cmd_stop(self, args: list[str]) -> str:
        """Stop session"""
        if not args:
            return "Usage: `/stop <session_id>`"

        session_id = self._resolve_id(args[0])
        if not session_id:
            return f"❌ Session not found: `{args[0]}`"

        s = self.registry.get(session_id)
        if not s:
            return f"❌ Session no longer exists"

        stype = s.get("session_type", "screen")
        if stype in ("ide", "terminal"):
            app = s.get("app_name") or ("Terminal" if stype == "terminal" else "")
            self.ide_ctrl.send_ctrl_c(app)
        else:
            await self.screen_mgr.send_ctrl_c(session_id)
            await asyncio.sleep(0.5)
            await self.screen_mgr.send_keys(session_id, "exit")
            await asyncio.sleep(1)
            await self.screen_mgr.kill(session_id)

        self.registry.update(session_id, status="stopped")
        return f"⏹️ Stopped session `{session_id[:8]}...`"

    async def _cmd_help(self, _args: list[str]) -> str:
        """Show help"""
        return COMMAND_HELP

    async def _cmd_pending(self, _args: list[str]) -> str:
        """List all sessions waiting for input"""
        sessions = self.registry.list(status_filter="waiting")

        if not sessions:
            return "✅ No sessions waiting for input."

        lines = ["🟡 **Sessions Waiting for Input**\n"]
        for s in sessions:
            sid = s["id"][:8]
            name = s.get("name", "") or sid
            stype = s.get("session_type", "screen")
            icon = {"screen": "💻", "ide": "🔌", "standalone": "💻", "terminal": "💻"}.get(stype, "💻")
            created = s.get("created_at", 0)
            elapsed = self._format_elapsed(created)

            lines.append(
                f"{icon} `{sid}` — **{name}**\n"
                f"  · Running: {elapsed}\n"
            )

        lines.append(f"\n{len(sessions)} session(s) waiting")
        lines.append("Use `/confirm-all` to confirm all at once")
        lines.append("or `/confirm <id>` individually")
        return "\n".join(lines)

    async def _cmd_confirm_all(self, _args: list[str]) -> str:
        """Confirm (Enter) all waiting sessions"""
        sessions = self.registry.list(status_filter="waiting")

        if not sessions:
            return "✅ No sessions waiting for input."

        success = 0
        failed = 0

        for s in sessions:
            ok = await self._confirm_from_bot(s)
            if ok:
                success += 1
            else:
                failed += 1

        parts = []
        if success:
            parts.append(f"✅ Confirmed {success}")
        if failed:
            parts.append(f"❌ Failed {failed}")
        return " — ".join(parts)

    async def _confirm_from_bot(self, s: dict) -> bool:
        """Send Enter to a session (used from bot commands)"""
        session_id = s["id"]
        stype = s.get("session_type", "screen")

        if stype == "ide":
            ok = self.ide_ctrl.send_enter(s.get("app_name", ""))
        elif stype == "terminal":
            app = s.get("app_name") or "Terminal"
            ok = self.ide_ctrl.send_enter(app)
        else:
            ok = await self.screen_mgr.send_enter(session_id)

        if ok:
            self.registry.update(session_id, status="running")
        return ok

    async def _send_message(self, chat_id: str, text: str) -> bool:
        """Send message via lark-cli"""
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
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=10
            )
            if proc.returncode != 0:
                logger.error(
                    "Failed to send Lark message: %s", stderr.decode()
                )
                return False
            return True
        except Exception as e:
            logger.error("Error sending Lark message: %s", e)
            return False

    async def _reply_message(self, chat_id: str, message_id: str, text: str) -> bool:
        """Reply to a message (creates thread)"""
        if not chat_id or not message_id:
            return await self._send_message(chat_id, text)

        try:
            proc = await asyncio.create_subprocess_exec(
                "lark-cli", "im", "+messages-reply",
                "--message-id", message_id,
                "--text", text,
                "--as", "bot",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=10
            )
            if proc.returncode != 0:
                logger.error(
                    "Failed to reply Lark message: %s", stderr.decode()
                )
                return False
            return True
        except Exception as e:
            logger.error("Error replying Lark message: %s", e)
            return False

    def _resolve_id(self, short_id: str) -> Optional[str]:
        """Resolve short ID (first 8 chars) to full session ID"""
        sessions = self.registry.list()
        for s in sessions:
            if s["id"].startswith(short_id):
                return s["id"]
        return None

    @staticmethod
    def _extract_text(msg_type: str, content_str: str) -> Optional[str]:
        """Extract text from message content"""
        try:
            content = json.loads(content_str) if isinstance(content_str, str) else content_str
        except json.JSONDecodeError:
            return str(content_str) if content_str else None

        if msg_type == "text":
            return content.get("text", "")
        return None

    @staticmethod
    def _format_elapsed(timestamp: float) -> str:
        """Format elapsed time"""
        import time
        seconds = time.time() - timestamp
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            return f"{int(seconds // 60)}m"
        elif seconds < 86400:
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            return f"{h}h{m}m"
        else:
            d = int(seconds // 86400)
            h = int((seconds % 86400) // 3600)
            return f"{d}d{h}h"