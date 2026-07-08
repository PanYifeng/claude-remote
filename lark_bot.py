"""Lark Bot — Event consumption, card actions, and message handling

Uses lark-cli event consumption instead of webhook:
- Receives messages via `lark-cli event consume im.message.receive_v1 --as bot`
- Receives card button clicks via `lark-cli event consume card.action.trigger --as bot`
- Sends replies via `lark-cli im send` and interactive cards

No Cloudflare tunnel or public webhook required.
"""

import asyncio
import json
import logging
import os
import shlex
import subprocess
import uuid
from typing import Optional, List

from config import config
from registry import SessionRegistry
from screen_manager import ScreenManager
from ide_control import IDEControl
from lark_card import (
    session_list_card,
    session_status_card,
    pending_card,
    confirm_all_card,
    done_card,
    interactive_card,
    streaming_card,
)

logger = logging.getLogger("lark_bot")

_session_context: dict[str, dict] = {}

COMMAND_HELP = """
/l list                       — List all sessions
/status <id|N>                — Show session details
/send <id|N> <text>           — Send command to session
/confirm [id|N]               — Confirm (Enter). No arg = last session
/enter <id|N>                 — Enter interactive mode with a session
/exit [--kill]                — Exit interactive mode (--kill also stops session)
/ls [path]                    — List directory contents on host
/new <path>                   — Start a new claude session in directory
/pending                      — List sessions waiting for input
/confirm-all                  — Confirm all waiting sessions
/select <id|N> <n>            — Select option N
/interrupt [id|N]             — Send Ctrl+C. No arg = last session
/stop <id|N>                  — Stop session
/help                         — Show this help

Tips:
  * `/enter 1` then just type messages to chat with session 1
  * `/exit` to leave interactive mode
  * `/ls` to browse directories on the host
  * `/new /path` to start a new claude session
"""


class LarkBot:
    """Lark bot handling messages via lark-cli"""

    def __init__(
        self,
        registry: SessionRegistry,
        screen_mgr: ScreenManager,
        ide_ctrl: Optional[IDEControl] = None,
    ):
        self.registry = registry
        self.screen_mgr = screen_mgr
        self.ide_ctrl = ide_ctrl or IDEControl()

    # ── Event handlers ────────────────────────────────

    async def handle_event_line(self, line: dict) -> Optional[dict]:
        """Handle an im.message.receive_v1 event from the consume stream"""
        try:
            content = line.get("content", "")
            chat_id = line.get("chat_id", "")
            message_id = line.get("message_id", "")
            logger.info("handle_event_line: chat_id=%s content=%s keys=%s",
                        chat_id[:15] if chat_id else "NONE",
                        repr(content[:50]) if content else "NONE",
                        list(line.keys()))
            if not content or not chat_id:
                logger.warning("Skipping event: content=%s chat_id=%s", bool(content), bool(chat_id))
                return None
            response = await self._process_command(content.strip(), chat_id)
            if response:
                await self._reply_message(chat_id, message_id, response)
            return {"code": 0}
        except Exception as e:
            logger.error("Failed to handle event line: %s", e, exc_info=True)
            return None

    async def handle_card_action(self, data: dict) -> Optional[dict]:
        """Handle a card.action.trigger event — user tapped a button"""
        try:
            chat_id = data.get("chat_id", "")
            message_id = data.get("message_id", "")
            token = data.get("token", "")
            action_value_raw = data.get("action_value", "{}")

            try:
                action_value = json.loads(action_value_raw) if isinstance(action_value_raw, str) else action_value_raw
            except json.JSONDecodeError:
                action_value = {}

            action = action_value.get("a", "")
            session_id = action_value.get("s", "")

            logger.info("Card action: %s session=%s", action, session_id[:8] if session_id else "-")

            response_card = None

            if action == "list":
                sessions = self._update_list_cache(chat_id)
                response_card = session_list_card(sessions)

            elif action == "status":
                s = self.registry.get(session_id) if session_id else None
                if s:
                    output, _ = await self._read_session_output(s)
                    idx = self._find_index(session_id, chat_id)
                    response_card = session_status_card(s, output, idx)
                else:
                    response_card = done_card("❌ Session not found.")

            elif action == "confirm":
                s = self.registry.get(session_id) if session_id else None
                if s:
                    await self._confirm_from_bot(s)
                    sessions = self._update_list_cache(chat_id)
                    response_card = session_list_card(sessions)
                else:
                    response_card = done_card("❌ Session not found.")

            elif action == "interrupt":
                s = self.registry.get(session_id) if session_id else None
                if s:
                    await self._interrupt_from_bot(s)
                    sessions = self._update_list_cache(chat_id)
                    response_card = session_list_card(sessions)
                else:
                    response_card = done_card("❌ Session not found.")

            elif action == "stop":
                s = self.registry.get(session_id) if session_id else None
                if s:
                    await self._stop_from_bot(s)
                    sessions = self._update_list_cache(chat_id)
                    response_card = session_list_card(sessions)
                else:
                    response_card = done_card("❌ Session not found.")

            elif action == "pending":
                waiting = self.registry.list(status_filter="waiting")
                response_card = pending_card(waiting)

            elif action == "confirm-all":
                waiting = self.registry.list(status_filter="waiting")
                success = failed = 0
                for s in waiting:
                    ok = await self._confirm_from_bot(s)
                    if ok: success += 1
                    else: failed += 1
                response_card = confirm_all_card(success, failed)

            elif action == "refresh":
                sessions = self._update_list_cache(chat_id)
                response_card = session_list_card(sessions)

            if response_card:
                self._update_card(token, response_card)

            return {"code": 0}
        except Exception as e:
            logger.error("Failed to handle card action: %s", e, exc_info=True)
            return None

    async def handle_webhook(self, body: dict) -> Optional[dict]:
        """Handle Lark event callback (webhook compatibility)"""
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
        """Handle received user message (webhook path)"""
        try:
            message = event.get("message", {})
            msg_type = message.get("message_type", "")
            content_str = message.get("content", "{}")
            text = self._extract_text(msg_type, content_str)
            if not text:
                return None
            chat_id = message.get("chat_id", "")
            response = await self._process_command(text.strip(), chat_id)
            if response:
                await self._send_message(chat_id, response)
            return {"code": 0}
        except Exception as e:
            logger.error("Failed to handle message event: %s", e, exc_info=True)
            return None

    # ── Command processing ────────────────────────────

    async def _process_command(self, text: str, chat_id: str) -> Optional[str]:
        """Parse and execute /command

        In interactive mode (ctx["mode"] is set), messages NOT starting
        with "/" are sent directly to the interactive session.
        """
        ctx = _session_context.setdefault(chat_id, {"sessions": []})
        mode_sid = ctx.get("mode")

        # Interactive mode: non-command messages go to the session
        if mode_sid and not text.startswith("/"):
            s = self.registry.get(mode_sid)
            if not s:
                ctx["mode"] = None
                return "❌ Session no longer exists. Exited interactive mode."

            stype = s.get("session_type", "screen")

            # Only screen sessions (created via /new) support interactive mode
            if stype != "screen":
                return "❌ Interactive mode only works with screen sessions.\nUse `/new <path>` to create one, or `/send <id> <text>` to send a single command."

            ok = await self.screen_mgr.send_keys(mode_sid, text)
            if not ok:
                return "❌ Send failed, session may be stopped"

            # Streaming output: send initial card, then poll and update in-place
            log_path = s.get("log_path", f"/tmp/claude-{mode_sid}.log")
            initial_card = streaming_card(text, "")
            message_id = self._send_card_to_chat(chat_id, initial_card)
            if not message_id:
                return "❌ Failed to send card"

            # Save message_id so /exit can update the card
            ctx["last_msg_id"] = message_id

            # Poll log file and update card in-place
            last_size = 0
            if os.path.exists(log_path):
                try:
                    last_size = os.path.getsize(log_path)
                except OSError:
                    pass
            no_change = 0
            for _ in range(30):  # Up to ~60 seconds
                await asyncio.sleep(2)
                current_size = 0
                if os.path.exists(log_path):
                    try:
                        current_size = os.path.getsize(log_path)
                    except OSError:
                        pass
                if current_size > last_size:
                    last_size = current_size
                    output = self._read_log_output(log_path)
                    if output:
                        self._update_card_message(message_id, streaming_card(text, output))
                    no_change = 0
                else:
                    no_change += 1

                # Check if output has stabilized (prompt or waiting pattern)
                if output and self._looks_stable(output):
                    break
                # No change for 2 rounds (4 seconds) -> stable
                if no_change >= 2:
                    break

            # Final update
            output = self._read_log_output(log_path) if os.path.exists(log_path) else ""
            self._update_card_message(message_id, streaming_card(text, output, done=True))
            return ""  # Card sent, no text reply

        # Normal command processing
        if not text.startswith("/"):
            return None
        parts = shlex.split(text[1:])
        if not parts:
            return COMMAND_HELP
        cmd = parts[0].lower()
        args = parts[1:]

        handlers = {
            "list": self._cmd_list, "l": self._cmd_list,
            "status": self._cmd_status, "send": self._cmd_send,
            "confirm": self._cmd_confirm, "pending": self._cmd_pending,
            "confirm-all": self._cmd_confirm_all, "select": self._cmd_select,
            "interrupt": self._cmd_interrupt, "stop": self._cmd_stop,
            "help": self._cmd_help,
            "enter": self._cmd_enter, "exit": self._cmd_exit,
            "new": self._cmd_new,
            "ls": self._cmd_ls,
        }
        handler = handlers.get(cmd)
        if not handler:
            return f"Unknown command: /{cmd}\n{COMMAND_HELP}"
        return await handler(args, chat_id)

    # ── Card-sending commands (these send interactive cards) ──

    async def _cmd_list(self, _args: list[str], chat_id: str) -> str:
        """Send interactive session list card"""
        sessions = self._update_list_cache(chat_id)
        if not sessions:
            return "📭 No active Claude Code sessions."
        self._send_card_to_chat(chat_id, session_list_card(sessions))
        return ""

    async def _cmd_status(self, args: list[str], chat_id: str) -> str:
        """Send session detail card"""
        if not args:
            return "Usage: `/status <id|N>`"
        session_id = await self._resolve_target(args[0], chat_id)
        if not session_id:
            return f"❌ Session not found: `{args[0]}`"
        s = self.registry.get(session_id)
        if not s:
            return "❌ Session no longer exists"

        # Use cached last_output only — never read clipboard here.
        # Clipboard reads during /status processing capture whatever is
        # frontmost (Lark chat, Terminal, etc.) and return wrong data.
        output = s.get("last_output", "")
        idx = self._find_index(session_id, chat_id)
        self._send_card_to_chat(chat_id, session_status_card(s, output, idx))
        return ""

    async def _cmd_pending(self, _args: list[str], chat_id: str) -> str:
        """Send pending sessions card"""
        self._update_list_cache(chat_id)
        waiting = self.registry.list(status_filter="waiting")
        self._send_card_to_chat(chat_id, pending_card(waiting))
        return ""

    async def _cmd_confirm_all(self, _args: list[str], chat_id: str) -> str:
        """Confirm all waiting and show result card"""
        sessions = self.registry.list(status_filter="waiting")
        if not sessions:
            self._send_card_to_chat(chat_id, done_card("✅ No sessions waiting for input."))
            return ""
        success = failed = 0
        for s in sessions:
            ok = await self._confirm_from_bot(s)
            if ok: success += 1
            else: failed += 1
        self._send_card_to_chat(chat_id, confirm_all_card(success, failed))
        return ""

    # ── Text-reply commands (keep text for simple ops) ──

    async def _cmd_enter(self, args: list[str], chat_id: str) -> str:
        """Enter interactive mode with a session"""
        if not args:
            return "Usage: `/enter <id|N>`\ne.g. `/enter 1`"
        session_id = await self._resolve_target(args[0], chat_id)
        if not session_id:
            return f"❌ Session not found: `{args[0]}`"
        s = self.registry.get(session_id)
        if not s:
            return "❌ Session no longer exists"
        ctx = _session_context.setdefault(chat_id, {})
        ctx["mode"] = session_id
        logger.info("Interactive mode: chat=%s session=%s", chat_id[:12], session_id[:8])
        return f"🔵 Interactive mode: **{s.get('name', session_id[:8])}**\n\nType any message to send to this session.\n`/exit` to leave (session kept alive).\n`/exit --kill` to leave and stop."

    async def _cmd_exit(self, args: list[str], chat_id: str) -> str:
        """Exit interactive mode"""
        ctx = _session_context.get(chat_id, {})
        mode_sid = ctx.get("mode")
        if not mode_sid:
            return "⚪ Not in interactive mode."
        kill = "--kill" in args or "-k" in args
        s = self.registry.get(mode_sid)
        name = s.get("name", mode_sid[:8]) if s else mode_sid[:8]
        if kill and s:
            await self._stop_from_bot(s)
        ctx["mode"] = None
        logger.info("Exit interactive mode: chat=%s kill=%s", chat_id[:12], kill)

        # 更新上一次交互卡片为已停止状态
        last_msg_id = ctx.get("last_msg_id", "")
        if kill:
            done = done_card(f"⏹️ **{name}** 已停止。")
        else:
            done = done_card(f"✅ 已退出交互模式。**{name}** 保持运行。")
        if last_msg_id:
            self._update_card_message(last_msg_id, done)
            return ""  # 卡片已更新，无需文本回复
        # 没有交互卡片可更新时，返回文本反馈
        if kill:
            return f"⏹️ Exited and stopped **{name}**."
        return f"✅ Exited interactive mode. **{name}** kept alive."

    async def _cmd_ls(self, args: list[str], chat_id: str) -> str:
        """List directory contents"""
        path = " ".join(args) if args else "."
        # Expand ~ to home directory
        path = os.path.expanduser(path)
        try:
            result = subprocess.run(
                ["ls", "-la", path],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                output = result.stdout
                if len(output) > 1500:
                    output = output[-1500:]
                return f"📂 `{path}`:\n```\n{output}\n```"
            else:
                return f"❌ `{path}`: {result.stderr.strip()}"
        except subprocess.TimeoutExpired:
            return f"❌ `ls` timed out on `{path}`"
        except Exception as e:
            return f"❌ {e}"

    async def _cmd_new(self, args: list[str], chat_id: str) -> str:
        """Start a new claude session in a directory and enter interactive mode"""
        path = " ".join(args) if args else os.getcwd()
        if not os.path.isdir(path):
            return f"❌ Not a directory: `{path}`"

        session_id = str(uuid.uuid4())
        screen_name = f"claude-{session_id[:12]}"
        log_path = f"/tmp/claude-{session_id}.log"
        name = os.path.basename(path)

        try:
            pid = await self.screen_mgr.create(session_id, cwd=path, log_path=log_path)
        except Exception as e:
            return f"❌ Failed to create session: {e}"

        self.registry.register(
            session_id, screen_name,
            name=f"New — {name}", pid=pid,
            cwd=path, log_path=log_path,
            session_type="screen",
        )

        # Wait for claude to start and auto-confirm trust prompt
        # Claude takes ~30s to start on this machine
        import asyncio as _asyncio
        started = False
        for i in range(35):
            try:
                if os.path.exists(log_path) and os.path.getsize(log_path) > 500:
                    with open(log_path, "rb") as f:
                        raw = f.read()
                    import re
                    text = raw.decode("utf-8", errors="replace")
                    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
                    # Auto-confirm trust prompt
                    if 'enter to confirm' in text.lower():
                        await self.screen_mgr.send_enter(session_id)
                        await _asyncio.sleep(3)
                        started = True
                        break
                    started = True
                    break
            except Exception:
                pass
            await _asyncio.sleep(1)

        status = "started" if started else "starting (may take a moment)"

        # Update list cache and enter interactive mode
        ctx = _session_context.setdefault(chat_id, {})
        sessions = self.registry.list()
        ctx["sessions"] = [s["id"] for s in sessions]
        ctx["mode"] = session_id
        logger.info("New session + interactive mode: %s %s (ready=%s)", session_id[:8], path, started)

        return f"✅ New session in `{path}` ({status}).\nMessages you send will go to this session.\n`/exit` to leave."

    async def _read_interactive_output(self, s: dict) -> str:
        """Read session output for interactive mode display

        Tries log file first (screen sessions via /new), then falls back
        to Terminal.app clipboard capture (reads frontmost window).
        """
        session_id = s["id"]
        log_path = s.get("log_path", f"/tmp/claude-{session_id}.log")

        # Try log file (screen sessions created via /new)
        if os.path.exists(log_path):
            try:
                initial_size = os.path.getsize(log_path)
            except OSError:
                initial_size = 0

            for attempt in range(10):
                try:
                    size = os.path.getsize(log_path)
                    if size > initial_size or (size > 50 and attempt == 0):
                        with open(log_path, "rb") as f:
                            raw = f.read()
                        if len(raw) > 50:
                            import re
                            text = raw.decode("utf-8", errors="replace")
                            text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
                            text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
                            lines = [l.strip() for l in text.splitlines() if l.strip()]
                            clean = [l for l in lines if len(l) > 5]
                            if clean:
                                return "\n".join(clean[-25:])
                            return text[-500:]
                except (OSError, IOError):
                    pass
                await asyncio.sleep(1)

        # Fallback: Terminal.app clipboard (reads frontmost window)
        # This may show the wrong session if multiple terminals are open
        try:
            for attempt in range(4):
                await asyncio.sleep(1)
                out, _ = self.ide_ctrl.read_terminal_full_output(25)
                if out and len(out) > 50:
                    lines = [l.strip() for l in out.splitlines()
                             if l.strip() and not l.strip().startswith("─")
                             and not l.strip().startswith("❯")
                             and not l.strip().startswith("  ⏵")]
                    if lines:
                        return "\n".join(lines[-20:])
        except Exception:
            pass

        return "(sent — view output in the terminal directly)"

    @staticmethod
    def _read_log_output(log_path: str) -> str:
        """Read and clean log file output"""
        try:
            with open(log_path, "rb") as f:
                raw = f.read()
            if len(raw) < 10:
                return ""
            import re
            text = raw.decode("utf-8", errors="replace")
            text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
            text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            clean = [l for l in lines if len(l) > 5]
            if clean:
                return "\n".join(clean[-25:])
            return text[-500:]
        except (OSError, IOError):
            return ""

    @staticmethod
    def _looks_stable(output: str) -> bool:
        """Check if output has stabilized (prompt or waiting pattern)"""
        if not output:
            return False
        from screen_manager import ScreenManager
        return ScreenManager._looks_waiting(output)

    async def _cmd_send(self, args: list[str], chat_id: str) -> str:
        if len(args) < 2:
            return "Usage: `/send <id|N> <text>`"
        session_id = await self._resolve_target(args[0], chat_id)
        if not session_id:
            return f"❌ Session not found: `{args[0]}`"
        text = " ".join(args[1:])
        s = self.registry.get(session_id)
        if not s:
            return "❌ Session no longer exists"
        stype = s.get("session_type", "screen")
        if stype == "ide":
            ok = self.ide_ctrl.send_keys(s.get("app_name", ""), text)
        elif stype == "terminal":
            ok = self.ide_ctrl.send_keys(s.get("app_name", "") or "Terminal", text)
        else:
            ok = await self.screen_mgr.send_keys(session_id, text)
        if ok:
            self.registry.update(session_id, status="running")
            _session_context.setdefault(chat_id, {})["selected"] = session_id
            return f"✅ Sent to {s.get('name', session_id[:8])}:\n```\n$ {text}\n```"
        return "❌ Send failed, session may be stopped"

    async def _cmd_confirm(self, args: list[str], chat_id: str) -> str:
        if args:
            session_id = await self._resolve_target(args[0], chat_id)
        else:
            session_id = _session_context.get(chat_id, {}).get("selected")
            if not session_id:
                return "Usage: `/confirm <id|N>` or `/confirm` (last session)"
        if not session_id:
            return "❌ Session not found"
        s = self.registry.get(session_id)
        if not s:
            return "❌ Session no longer exists"
        stype = s.get("session_type", "screen")
        if stype == "ide":
            ok = self.ide_ctrl.send_enter(s.get("app_name", ""))
        elif stype == "terminal":
            ok = self.ide_ctrl.send_enter(s.get("app_name", "") or "Terminal")
        else:
            ok = await self.screen_mgr.send_enter(session_id)
        if ok:
            self.registry.update(session_id, status="running")
            return f"✅ Confirmed `{session_id[:8]}`"
        return "❌ Confirm failed"

    async def _cmd_select(self, args: list[str], chat_id: str) -> str:
        if len(args) < 2:
            return "Usage: `/select <id|N> <n>`"
        session_id = await self._resolve_target(args[0], chat_id)
        if not session_id:
            return f"❌ Session not found: `{args[0]}`"
        try:
            option = int(args[1])
        except ValueError:
            return "❌ Option must be a number"
        s = self.registry.get(session_id)
        if not s:
            return "❌ Session no longer exists"
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
            return f"✅ Selected option {option} → `{session_id[:8]}`"
        return "❌ Select failed"

    async def _cmd_interrupt(self, args: list[str], chat_id: str) -> str:
        if args:
            session_id = await self._resolve_target(args[0], chat_id)
        else:
            session_id = _session_context.get(chat_id, {}).get("selected")
            if not session_id:
                return "Usage: `/interrupt <id|N>` or `/interrupt` (last session)"
        if not session_id:
            return "❌ Session not found"
        s = self.registry.get(session_id)
        if not s:
            return "❌ Session no longer exists"
        stype = s.get("session_type", "screen")
        if stype == "ide":
            ok = self.ide_ctrl.send_ctrl_c(s.get("app_name", ""))
        elif stype == "terminal":
            ok = self.ide_ctrl.send_ctrl_c(s.get("app_name", "") or "Terminal")
        else:
            ok = await self.screen_mgr.send_ctrl_c(session_id)
        if ok:
            self.registry.update(session_id, status="running")
            return f"⚠️ Interrupted `{session_id[:8]}`"
        return "❌ Interrupt failed"

    async def _cmd_stop(self, args: list[str], chat_id: str) -> str:
        if not args:
            return "Usage: `/stop <id|N>`"
        session_id = await self._resolve_target(args[0], chat_id)
        if not session_id:
            return f"❌ Session not found: `{args[0]}`"
        s = self.registry.get(session_id)
        if not s:
            return "❌ Session no longer exists"
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
        return f"⏹️ Stopped `{session_id[:8]}`"

    async def _cmd_help(self, _args: list[str], chat_id: str) -> str:
        return COMMAND_HELP

    # ── Helpers ───────────────────────────────────────

    async def _resolve_target(self, arg: str, chat_id: str) -> Optional[str]:
        ctx = _session_context.get(chat_id)
        if not ctx:
            ctx = _session_context.setdefault(chat_id, {"sessions": []})

        # If it's all digits, prefer short ID over numeric index
        # (session IDs are 8+ chars, user session numbers are 1-2 digits)
        if arg.isdigit():
            # Try short ID first (UUID first 8 chars)
            resolved = self._resolve_id(arg)
            if resolved:
                return resolved
            # Fall back to numeric index
            idx = int(arg) - 1
            cached = ctx.get("sessions", [])
            if 0 <= idx < len(cached):
                return cached[idx]
            return None

        return self._resolve_id(arg)

    def _update_list_cache(self, chat_id: str) -> list:
        sessions = self.registry.list()
        ctx = _session_context.setdefault(chat_id, {})
        ctx["sessions"] = [s["id"] for s in sessions]
        return sessions

    def _find_index(self, session_id: str, chat_id: str) -> int:
        ctx = _session_context.get(chat_id, {})
        for i, sid in enumerate(ctx.get("sessions", []), 1):
            if sid == session_id:
                return i
        return -1

    async def _read_session_output(self, s: dict) -> tuple[str, int]:
        stype = s.get("session_type", "screen")
        session_id = s["id"]
        if stype == "ide":
            return self.ide_ctrl.read_output(s.get("app_name", ""), 15)
        elif stype == "terminal":
            # Try full scrollback first (clipboard capture) for richer output
            output, lc = self.ide_ctrl.read_terminal_full_output(50)
            if output:
                # Filter out separator/prompt lines to show meaningful content
                lines = [l for l in output.splitlines()
                         if l.strip() and not l.strip().startswith("─")
                         and not l.strip().startswith("❯")
                         and not l.strip().startswith("  ⏵")]
                if lines:
                    filtered = "\n".join(lines[-30:])
                    return filtered, len(lines)
                return output, lc
            # Fallback: log file
            output, lc = await self.screen_mgr.read_output(session_id, 15)
            return output, lc
        return await self.screen_mgr.read_output(session_id, 15)

    async def _confirm_from_bot(self, s: dict) -> bool:
        session_id = s["id"]
        stype = s.get("session_type", "screen")
        if stype == "ide":
            ok = self.ide_ctrl.send_enter(s.get("app_name", ""))
        elif stype == "terminal":
            ok = self.ide_ctrl.send_enter(s.get("app_name", "") or "Terminal")
        else:
            ok = await self.screen_mgr.send_enter(session_id)
        if ok:
            self.registry.update(session_id, status="running")
        return ok

    async def _interrupt_from_bot(self, s: dict) -> bool:
        session_id = s["id"]
        stype = s.get("session_type", "screen")
        if stype == "ide":
            ok = self.ide_ctrl.send_ctrl_c(s.get("app_name", ""))
        elif stype == "terminal":
            ok = self.ide_ctrl.send_ctrl_c(s.get("app_name", "") or "Terminal")
        else:
            ok = await self.screen_mgr.send_ctrl_c(session_id)
        if ok:
            self.registry.update(session_id, status="running")
        return ok

    async def _stop_from_bot(self, s: dict) -> bool:
        session_id = s["id"]
        stype = s.get("session_type", "screen")
        pid = s.get("pid", 0)
        if stype in ("ide", "terminal"):
            app = s.get("app_name") or ("Terminal" if stype == "terminal" else "")
            self.ide_ctrl.send_ctrl_c(app)
            # 实际杀掉进程 — send_ctrl_c 只发送 Ctrl+C，进程可能不退出
            if pid:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "kill", "-9", str(pid),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc.wait()
                except Exception:
                    pass
        else:
            await self.screen_mgr.send_ctrl_c(session_id)
            await asyncio.sleep(0.5)
            await self.screen_mgr.send_keys(session_id, "exit")
            await asyncio.sleep(1)
            await self.screen_mgr.kill(session_id)
        # 标记为用户主动停止，防止 scan-existing 重新激活
        tags = s.get("tags", {})
        if isinstance(tags, str):
            try:
                import json
                tags = json.loads(tags)
            except json.JSONDecodeError:
                tags = {}
        tags["user_stopped"] = True
        self.registry.update(session_id, status="stopped", tags=tags)
        return True

    # ── Message sending ──────────────────────────────

    async def _send_message(self, chat_id: str, text: str) -> bool:
        if not chat_id:
            return False
        try:
            content = json.dumps({"text": text}, ensure_ascii=False)
            proc = await asyncio.create_subprocess_exec(
                "lark-cli", "im", "send", "--chat-id", chat_id, "--msg-type", "text",
                "--content", content, "--as", "bot",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode != 0:
                logger.error("Send msg failed: %s", stderr.decode()[:200])
                return False
            return True
        except Exception as e:
            logger.error("Send msg error: %s", e)
            return False

    async def _reply_message(self, chat_id: str, message_id: str, text: str) -> bool:
        if not chat_id or not message_id:
            return await self._send_message(chat_id, text)
        try:
            proc = await asyncio.create_subprocess_exec(
                "lark-cli", "im", "+messages-reply", "--message-id", message_id,
                "--text", text, "--as", "bot",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode != 0:
                logger.error("Reply msg failed: %s", stderr.decode()[:200])
                return False
            return True
        except Exception as e:
            logger.error("Reply msg error: %s", e)
            return False

    def _send_card_to_chat(self, chat_id: str, card_json: str) -> str:
        """Send a new interactive card to a chat

        Returns:
            message_id (str) on success, empty string on failure
        """
        if not chat_id:
            logger.warning("_send_card_to_chat: no chat_id")
            return ""
        try:
            import subprocess
            logger.info("Sending card to %s (len=%d)", chat_id[:15], len(card_json))
            result = subprocess.run(
                ["lark-cli", "im", "+messages-send",
                 "--chat-id", chat_id,
                 "--msg-type", "interactive",
                 "--content", card_json,
                 "--as", "bot"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                full_err = result.stderr[:500] if result.stderr else result.stdout[:500]
                logger.warning("Send card failed (exit=%d): %s", result.returncode, full_err)
                return ""
            logger.info("Card sent OK to %s", chat_id[:15])
            # Parse message_id from response
            try:
                resp = json.loads(result.stdout)
                msg_id = resp.get("data", {}).get("message_id", "")
                if msg_id:
                    return msg_id
            except (json.JSONDecodeError, AttributeError):
                pass
            return ""
        except Exception as e:
            logger.error("Send card error: %s", e)
            return ""

    def _update_card_message(self, message_id: str, card_json: str) -> bool:
        """Update an existing interactive card message in-place via PATCH"""
        if not message_id:
            return False
        try:
            import subprocess
            result = subprocess.run(
                ["lark-cli", "api", "PATCH",
                 f"/open-apis/im/v1/messages/{message_id}",
                 "--as", "bot",
                 "--data", json.dumps({
                     "msg_type": "interactive",
                     "content": card_json,
                 })],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                logger.warning("Update card failed (exit=%d): %s", result.returncode, result.stderr[:200])
                return False
            return True
        except Exception as e:
            logger.error("Update card error: %s", e)
            return False

    def _reply_card(self, chat_id: str, message_id: str, card_json: str) -> bool:
        """Reply with a card to an existing message"""
        try:
            import subprocess
            result = subprocess.run(
                ["lark-cli", "im", "+messages-reply",
                 "--message-id", message_id,
                 "--msg-type", "interactive",
                 "--content", card_json,
                 "--as", "bot"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                logger.warning("Reply card failed: %s", result.stderr[:200])
                return False
            return True
        except Exception as e:
            logger.error("Reply card error: %s", e)
            return False

    def _update_card(self, token: str, card_json: str) -> bool:
        """Update an existing interactive card in-place"""
        if not token:
            return False
        try:
            import subprocess
            result = subprocess.run(
                ["lark-cli", "api", "POST", "/open-apis/interactive/v1/card/update",
                 "--data", json.dumps({"token": token, "card": json.loads(card_json)}, ensure_ascii=False)],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                logger.warning("Card update failed: %s", result.stderr[:200])
                return False
            return True
        except Exception as e:
            logger.error("Card update error: %s", e)
            return False

    def _resolve_id(self, short_id: str) -> Optional[str]:
        sessions = self.registry.list()
        for s in sessions:
            if s["id"].startswith(short_id):
                return s["id"]
        return None

    @staticmethod
    def _extract_text(msg_type: str, content_str: str) -> Optional[str]:
        try:
            content = json.loads(content_str) if isinstance(content_str, str) else content_str
        except json.JSONDecodeError:
            return str(content_str) if content_str else None
        if msg_type == "text":
            return content.get("text", "")
        return None

    @staticmethod
    def _format_elapsed(timestamp: float) -> str:
        import time
        seconds = time.time() - timestamp
        if seconds < 60: return f"{int(seconds)}s"
        elif seconds < 3600: return f"{int(seconds // 60)}m"
        elif seconds < 86400: return f"{int(seconds // 3600)}h"
        else: return f"{int(seconds // 86400)}d"