"""Lark Bot — Event consumption, card actions, and message handling

Uses lark-cli event consumption instead of webhook:
- Receives messages via `lark-cli event consume im.message.receive_v1 --as bot`
- Receives card button clicks via `lark-cli event consume card.action.trigger --as bot`
- Sends replies via `lark-cli im +messages-send` and interactive cards

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
from ide_control import IDEControl, session_tty
from dispatch import send_to_session
from lark_card import (
    session_list_card,
    session_status_card,
    pending_card,
    confirm_all_card,
    done_card,
    InteractiveState,
    interactive_chat_card,
    st_decision_cache,
)

logger = logging.getLogger("lark_bot")

# TUI 斜杠快捷命令（卡片按钮 → 注入 "/<cmd>" 由 Claude Code TUI 执行）
# label 是按钮文案，confirm 是需要用户知晓的后果提示（None = 无害）
_TUI_COMMANDS: dict[str, dict] = {
    "clear": {"label": "🧹 /clear", "confirm": None},
    "compact": {"label": "📦 /compact", "confirm": None},
    "help": {"label": "❓ /help", "confirm": None},
}

# 后台任务强引用集合：防 create_task 的裸任务被 GC 中途取消
_background_tasks: set[asyncio.Task] = set()

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
/new-opencode <path>          — Start a new opencode session in directory
/pending                      — List sessions waiting for input
/confirm-all                  — Confirm all waiting sessions
/select <id|N> <n>            — Select option N
/interrupt [id|N]             — Send Ctrl+C. No arg = last session
/mode <id|N> [mode]           — Show / set permission mode (auto|manual|accept-edits|plan|next)
/setmodel <id|N> [model]       — Show or switch the session's model (no arg = list candidates)
/models                        — List all known models across sessions
/esc [id|N]                   — Send Esc (close dialog / cancel selection)
/stop <id|N>                  — Stop session
/daemon (stop|restart|status) — Control daemon service
/help                         — Show this help

Tips:
  * `/enter 1` then just type messages to chat with session 1
  * `/exit` to leave interactive mode
  * `/ls` to browse directories on the host
  * `/new /path` to start a new claude session
  * `/new-opencode /path` to start a new opencode session
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
            # 图片引用（![Image](img_v3_xxx)）下载到本地并替换为文件路径，
            # 让终端会话里的智能体能真正读到图
            from image_resources import has_image_refs, replace_image_refs
            if has_image_refs(content):
                content = await asyncio.to_thread(
                    replace_image_refs, content, message_id)
            response = await self._process_command(content.strip(), chat_id)
            if response:
                await self._reply_message(chat_id, message_id, response)
            return {"code": 0}
        except Exception as e:
            logger.error("Failed to handle event line: %s", e, exc_info=True)
            return None

    async def handle_card_event(self, event: dict) -> Optional[dict]:
        """Handle a card.action.trigger event — user tapped a button on a card

        lark-cli consume 输出结构未定（可能扁平、可能是 Lark v2 原始结构），
        解析做成防御式，找不到 action 时打日志返回。
        """
        try:
            info = self._parse_card_event(event)
            action, sid = info["action"], info["session_id"]
            chat_id, message_id = info["chat_id"], info["message_id"]
            logger.info("Card action: %s session=%s chat=%s",
                        action, sid[:8] if sid else "-", chat_id or "-")
            if not action or not chat_id:
                logger.warning("Card event missing action/chat: keys=%s", list(event.keys()))
                return None

            text, card = await self._run_card_action(action, sid, chat_id, info.get("extra"))
            if card:
                # 优先原位更新被点击的卡片；失败（或拿不到 message_id）则发新卡
                # （lark-cli subprocess 经 to_thread，避免阻塞事件循环）
                if not (message_id and await asyncio.to_thread(
                        self._update_card_message, message_id, card)):
                    await asyncio.to_thread(self._send_card_to_chat, chat_id, card)
            elif text:
                await self._send_message(chat_id, text)
            return {"code": 0}
        except Exception as e:
            logger.error("Failed to handle card event: %s", e, exc_info=True)
            return None

    async def _run_card_action(self, action: str, sid: str, chat_id: str,
                               extra: dict | None = None) -> tuple[str, str]:
        """执行卡片动作，返回 (text, card_json)，二者最多一个非空"""
        if action in ("list", "refresh"):
            sessions = self._update_list_cache(chat_id)
            models = await self._fetch_session_models(sessions)
            return "", session_list_card(sessions, models)

        if action == "status":
            s = self.registry.get(sid) if sid else None
            if not s:
                return "❌ Session not found.", ""
            output, _ = await self._read_session_output(s)
            decision, mode = await self._read_decision_and_mode(s)
            model = (await asyncio.to_thread(self._read_model, s)
                     if s.get("agent", "claude") == "claude" else "")
            groups = await self._fetch_model_groups() if model else None
            return "", session_status_card(s, output, decision=decision, mode=mode,
                                           model=model, model_groups=groups)

        if action in ("confirm", "interrupt", "stop"):
            s = self.registry.get(sid) if sid else None
            if not s:
                return "❌ Session not found.", ""
            ok = await {"confirm": self._confirm_from_bot,
                        "interrupt": self._interrupt_from_bot,
                        "stop": self._stop_from_bot}[action](s)
            verb = {"confirm": "Confirmed", "interrupt": "Interrupted", "stop": "Stopped"}[action]
            if not ok:
                return f"❌ {action} failed{self._send_fail_hint(s)}", ""
            icon = "⏹️" if action == "stop" else "✅"
            return f"{icon} {verb} `{sid[:8]}`", ""

        if action == "pending":
            waiting = self.registry.list(status_filter="waiting")
            models = await self._fetch_session_models(waiting)
            return "", pending_card(waiting, models)

        if action == "confirm-all":
            waiting = self.registry.list(status_filter="waiting")
            success = failed = 0
            for s in waiting:
                if await self._confirm_from_bot(s):
                    success += 1
                else:
                    failed += 1
            return "", confirm_all_card(success, failed)

        if action == "compose":
            return await self._enter_compose_mode(sid, chat_id)

        if action == "enter":
            if not sid:
                return "❌ Session not found.", ""
            # _cmd_enter 自己发交互卡片，这里只透传其文本提示
            return (await self._cmd_enter([sid], chat_id)) or "", ""
        if action == "exit-interactive":
            # 交互卡"退出交互"按钮：退出交互模式（会话保持运行）
            s = self.registry.get(sid) if sid else None
            if not s:
                return "❌ Session not found.", ""
            return (await self._cmd_exit([], chat_id)) or "", ""

        if action == "irefresh":
            # 交互卡"刷新"：仍处于交互模式的会话读最新输出，原位刷新交互卡
            # （不是状态卡——用 status 会把交互卡替换成状态卡，丢失退出按钮，
            # 看起来像退出了交互模式，实际 ctx["mode"] 还在）
            s = self.registry.get(sid) if sid else None
            if not s:
                return "❌ Session not found.", ""
            if _session_context.get(chat_id, {}).get("mode") != sid:
                # 已不在交互模式：退化为状态卡
                output, _ = await self._read_session_output(s)
                decision, mode = await self._read_decision_and_mode(s)
                model = (await asyncio.to_thread(self._read_model, s)
                         if s.get("agent", "claude") == "claude" else "")
                groups = await self._fetch_model_groups() if model else None
                return "", session_status_card(s, output, decision=decision,
                                               mode=mode, model=model,
                                               model_groups=groups)
            output = await self._read_interactive_output(s)
            card = await self._build_interactive_card(
                s, [("out", output)], done=True)
            return "", card

        if action == "select-option":
            # 决策卡选项直选：裸字符注入选项号（对话框把数字当按键消费，
            # bracketed paste 会被对话框忽略——实测 2026-11-19）
            s = self.registry.get(sid) if sid else None
            if not s or not extra or not extra.get("o"):
                return "❌ Session or option not found.", ""
            ok = await self._dispatch_send(s, "keystroke", str(extra["o"]))
            if not ok:
                return f"❌ Select failed{self._send_fail_hint(s)}", ""
            self.registry.update(sid, status="running")
            return f"✅ Selected option {extra['o']} → `{sid[:8]}`", ""

        if action == "send-key":
            # 决策卡 y/n 等：裸字符注入（同上，对话框场景）
            s = self.registry.get(sid) if sid else None
            if not s or not extra or not extra.get("k"):
                return "❌ Session or key not found.", ""
            ok = await self._dispatch_send(s, "keystroke", str(extra["k"]))
            if not ok:
                return f"❌ Send failed{self._send_fail_hint(s)}", ""
            self.registry.update(sid, status="running")
            return f"✅ Sent `{extra['k']}` → `{sid[:8]}`", ""

        if action == "esc":
            # 关闭对话框 / 取消多选（原始 ESC 注入）
            s = self.registry.get(sid) if sid else None
            if not s:
                return "❌ Session not found.", ""
            ok = await self._dispatch_send(s, "raw_key", "esc")
            if not ok:
                return f"❌ Esc failed{self._send_fail_hint(s)}", ""
            return f"🚫 Esc → `{sid[:8]}`", ""

        if action == "cycle-mode":
            # Claude Code Shift+Tab 循环权限模式（一步）
            s = self.registry.get(sid) if sid else None
            if not s:
                return "❌ Session not found.", ""
            ok = await self._dispatch_send(s, "raw_key", "shift_tab")
            if not ok:
                return f"❌ Mode switch failed{self._send_fail_hint(s)}", ""
            # 稍等 TUI 重绘后读回当前模式
            await asyncio.sleep(0.8)
            _, mode = await self._read_decision_and_mode(s)
            return f"🔁 Mode → {self._mode_label(mode)} (`{sid[:8]}`)", ""

        if action == "set-mode":
            # 直达目标模式：按 Shift+Tab 若干步（循环顺序内计算步数）
            s = self.registry.get(sid) if sid else None
            if not s or not extra or not extra.get("m"):
                return "❌ Session or target mode not found.", ""
            target = str(extra["m"])
            return await self._set_mode(s, target), ""

        if action == "set-model":
            # 状态卡模型按钮：TUI '/model <name>' 直接切换
            s = self.registry.get(sid) if sid else None
            if not s or not extra or not extra.get("m"):
                return "❌ Session or target model not found.", ""
            model = str(extra["m"])
            ok = await self._dispatch_send(s, "send", f"/model {model}")
            if not ok:
                return f"❌ Set model failed{self._send_fail_hint(s)}", ""
            # 稍等 TUI 应用切换后读回当前模型，原位刷新被点击的卡片
            await asyncio.sleep(1.5)
            refreshed = await asyncio.to_thread(self._read_model, s)
            if refreshed:
                model = refreshed
            return (f"✅ Set model → `{model}` (`{sid[:8]}`)\n"
                    f"⚠️ TUI 会把它保存为所有新会话的默认模型"), ""

        if action == "tui-cmd":
            # TUI 斜杠快捷命令（/clear /compact /help /model 等可扩展）：
            # 注入 "/<cmd>" + 回车，TUI 直接执行（实测无需对话框交互）
            s = self.registry.get(sid) if sid else None
            if not s or not extra or not extra.get("c"):
                return "❌ Session or command not found.", ""
            tui_cmd = "/" + str(extra["c"])
            ok = await self._dispatch_send(s, "send", tui_cmd)
            if not ok:
                return f"❌ Command failed{self._send_fail_hint(s)}", ""
            self.registry.update(sid, status="running")
            label = _TUI_COMMANDS.get(str(extra["c"]), {}).get("label", tui_cmd)
            return f"✅ {label} → `{sid[:8]}`", ""

        return f"Unknown card action: {action}", ""

    async def _enter_compose_mode(self, sid: str, chat_id: str) -> tuple[str, str]:
        """进入一次性发送模式：下一条非命令消息直接发到该会话"""
        s = self.registry.get(sid) if sid else None
        if not s:
            return "❌ Session not found.", ""
        _session_context.setdefault(chat_id, {})["compose"] = sid
        _session_context[chat_id]["selected"] = sid
        label = s.get("name") or sid[:8]
        card = done_card(
            f"📤 **Compose mode / 发送模式**\n\n"
            f"**To / 发送到:** {label}\n\n"
            f"直接回复消息内容即可发送（一条一次）。\n"
            f"发送 `/cancel` 退出。"
        )
        return "", card

    @staticmethod
    def _parse_card_event(event: dict) -> dict:
        """防御式解析 card.action.trigger 事件（lark-cli 输出结构未定）

        返回 {"action", "session_id", "chat_id", "message_id", "extra"}
        """
        action, sid, extra = LarkBot._first_action_value(event)
        chat_id, message_id = LarkBot._ids_from_card_event(event)
        return {"action": action, "session_id": sid,
                "chat_id": chat_id, "message_id": message_id, "extra": extra}

    @staticmethod
    def _first_action_value(event: dict) -> tuple[str, str, dict]:
        """在事件的各层结构里找按钮 value（{"a": 动作, "s": 会话 ID, ...附加参数}）

        返回 (action, session_id, extra)；extra 是除 a/s 外的附加参数 dict。
        """
        sources = [event]
        nested = event.get("event")
        if isinstance(nested, dict):
            sources.append(nested)
        for src in sources:
            candidates = [src.get("action_value"), src.get("value")]
            act = src.get("action")
            if isinstance(act, dict):
                candidates.append(act.get("value"))
            for c in candidates:
                if isinstance(c, str):
                    try:
                        c = json.loads(c)
                    except json.JSONDecodeError:
                        continue
                if isinstance(c, dict) and c.get("a"):
                    extra = {k: v for k, v in c.items() if k not in ("a", "s")}
                    return str(c["a"]), str(c.get("s", "")), extra
        return "", "", {}

    @staticmethod
    def _ids_from_card_event(event: dict) -> tuple[str, str]:
        """在事件的各层结构里找 chat_id / message_id（含 v2 的 contexts）"""
        sources = [event]
        nested = event.get("event")
        if isinstance(nested, dict):
            sources.append(nested)
        chat_id = message_id = ""
        for src in sources:
            chat_id = chat_id or src.get("chat_id", "") or src.get("open_chat_id", "")
            message_id = message_id or src.get("message_id", "") or src.get("open_message_id", "")
            ctx = src.get("contexts")
            if isinstance(ctx, dict):
                chat_id = chat_id or ctx.get("open_chat_id", "")
                message_id = message_id or ctx.get("open_message_id", "")
        return chat_id, message_id

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

            ok = await self._dispatch_send(s, "send", text)
            if not ok:
                return (f"❌ Send failed — session tab not found or unavailable. "
                        f"用 `/status {mode_sid[:8]}` 检查{self._send_fail_hint(s)}")
            self.registry.update(mode_sid, status="running")

            # 交互轮询放后台任务：立即返回让事件循环继续消费下一条消息
            # （2026-11-26 事故根因：轮询 await 在 handler 里，最长 5 分钟
            # 阻塞整个消息事件流，/l 等命令全部无响应）
            task = asyncio.create_task(
                self._interactive_round(s, text, chat_id),
                name=f"interactive-{mode_sid[:8]}")
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
            return ""

        # Compose 模式（卡片按钮 "发送…" 进入）：下一条非命令消息直接发到指定会话
        compose_sid = ctx.get("compose")
        if compose_sid and not text.startswith("/"):
            ctx["compose"] = None  # 一次性：无论成败都退出
            s = self.registry.get(compose_sid)
            if not s:
                return "❌ Session no longer exists. Exited compose mode."
            ok = await self._dispatch_send(s, "send", text)
            if not ok:
                return (f"❌ Send failed — session tab not found or unavailable. "
                        f"用 `/status {compose_sid[:8]}` 检查{self._send_fail_hint(s)}")
            self.registry.update(compose_sid, status="running")
            ctx["selected"] = compose_sid
            label = s.get("name") or compose_sid[:8]
            return (f"✅ Sent to {label}:\n```\n$ {text}\n```"
                    f"{await self._busy_note(s)}")

        # Normal command processing
        if not text.startswith("/"):
            return None
        # /cancel 只在 compose 模式下有意义
        if compose_sid and text.strip() == "/cancel":
            ctx["compose"] = None
            return "📤 Compose mode cancelled."
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
            "mode": self._cmd_mode, "esc": self._cmd_esc,
            "setmodel": self._cmd_setmodel, "models": self._cmd_models,
            "enter": self._cmd_enter, "exit": self._cmd_exit,
            "new": self._cmd_new,
            "new-opencode": self._cmd_new_opencode,
            "ls": self._cmd_ls,
            "daemon": self._cmd_daemon,
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
        models = await self._fetch_session_models(sessions)
        await asyncio.to_thread(
            self._send_card_to_chat, chat_id, session_list_card(sessions, models))
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

        # 优先用 agent 的权威状态（claude: per-process 状态文件；opencode: sqlite）
        stype = s.get("session_type", "screen")
        output = s.get("last_output", "")
        status = s.get("status", "running")
        cwd = s.get("cwd", "")
        pid = s.get("pid", 0)
        agent = s.get("agent", "claude")

        from agent_state import read_agent_state
        from ide_control import read_last_assistant_message
        from screen_manager import ScreenManager

        st = await asyncio.to_thread(read_agent_state, agent, pid, cwd)
        used_authoritative = False
        if st and st.session_id:
            used_authoritative = True
            ts_text = st.text
            if ts_text:
                output = ts_text
            # 状态：agent 实时状态
            if st.status:
                status = st.status
            # 待确认补充：终端会话再按 TTY 抓一下，approval 提示未必反映在状态里
            if status != "waiting":
                live_output = await self._read_session_visible(s, stype, agent)
                if live_output and ScreenManager._looks_waiting(live_output, agent=agent):
                    status = "waiting"
                    output = (ts_text + "\n\n—— 待确认 ——\n" + live_output[-400:]) if ts_text else live_output[-600:]
            if output:
                self.registry.update(session_id, status=status, last_output=output[-800:])
            else:
                self.registry.update(session_id, status=status)

        if not used_authoritative:
            # 回退：没有 per-process 状态文件（老版本 claude / 子代理），按类型走老逻辑
            if stype == "ide":
                try:
                    ts_text, ts_iso = await asyncio.to_thread(
                        read_last_assistant_message, cwd, "", 1200)
                    if ts_text:
                        output = ts_text
                    live_output, _ = await asyncio.to_thread(
                        self.ide_ctrl.read_output, s.get("app_name", ""), 50)
                except Exception:
                    live_output = ""
                if live_output and ScreenManager._looks_claude_terminal(live_output) \
                        and ScreenManager._looks_waiting(live_output, agent=agent):
                    status = "waiting"
                    if live_output:
                        output = (ts_text + "\n\n—— 待确认 ——\n" + live_output[-400:]) if ts_text else live_output[-600:]
                elif ts_iso:
                    import time as _time
                    try:
                        from datetime import datetime
                        last = datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).timestamp()
                        status = "executing" if (_time.time() - last < 120) else "idle"
                    except Exception:
                        pass
                if output:
                    self.registry.update(session_id, status=status, last_output=output[-800:])
                else:
                    self.registry.update(session_id, status=status)
            elif stype == "terminal":
                # registry.get 返回的 tags 是 JSON 字符串，session_tty 统一解析
                tty = session_tty(s)
                read_via_tty = False
                if tty:
                    try:
                        live_output, _ = await asyncio.to_thread(
                            self.ide_ctrl.read_terminal_by_tty, tty, 50)
                        if live_output:
                            read_via_tty = True
                            output = live_output
                            if ScreenManager._looks_waiting(live_output, agent=agent):
                                status = "waiting"
                            elif any(x in live_output for x in ("❯", "plan mode", "Claude", "accept edits", "$ ", "# ")):
                                status = "idle"
                            else:
                                status = "executing"
                            self.registry.update(session_id, status=status, last_output=output[-500:])
                    except Exception:
                        pass
                if not read_via_tty:
                    log_path = s.get("log_path", f"/tmp/claude-{session_id}.log")
                    import os
                    if os.path.exists(log_path):
                        live_output = await asyncio.to_thread(
                            self._read_log_output, log_path)
                        if live_output:
                            output = live_output
                            self.registry.update(session_id, last_output=output[-500:])
            elif stype == "screen":
                log_path = s.get("log_path", f"/tmp/claude-{session_id}.log")
                import os
                if os.path.exists(log_path):
                    live_output = await asyncio.to_thread(
                        self._read_log_output, log_path)
                    if live_output:
                        output = live_output

        # Re-fetch to get updated status
        s = self.registry.get(session_id)
        idx = self._find_index(session_id, chat_id)
        decision, mode = await self._read_decision_and_mode(s)
        model = (await asyncio.to_thread(self._read_model, s)
                 if s.get("agent", "claude") == "claude" else "")
        groups = await self._fetch_model_groups() if model else None
        await asyncio.to_thread(
            self._send_card_to_chat, chat_id,
            session_status_card(s, output, idx, decision=decision, mode=mode,
                                model=model, model_groups=groups))
        return ""

    async def _cmd_pending(self, _args: list[str], chat_id: str) -> str:
        """Send pending sessions card"""
        self._update_list_cache(chat_id)
        waiting = self.registry.list(status_filter="waiting")
        models = await self._fetch_session_models(waiting)
        await asyncio.to_thread(
            self._send_card_to_chat, chat_id, pending_card(waiting, models))
        return ""

    async def _cmd_confirm_all(self, _args: list[str], chat_id: str) -> str:
        """Confirm all waiting and show result card"""
        sessions = self.registry.list(status_filter="waiting")
        if not sessions:
            await asyncio.to_thread(
                self._send_card_to_chat, chat_id,
                done_card("✅ No sessions waiting for input."))
            return ""
        success = failed = 0
        for s in sessions:
            ok = await self._confirm_from_bot(s)
            if ok: success += 1
            else: failed += 1
        await asyncio.to_thread(
            self._send_card_to_chat, chat_id, confirm_all_card(success, failed))
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
        # 发交互聊天卡（含操作按钮 + 当前状态）；此后每条消息一张流式卡，
        # 逐张追加在聊天流里（像聊天一样滚动回看历史）
        card = await self._build_interactive_card(s, [], done=True)
        await asyncio.to_thread(self._send_card_to_chat, chat_id, card)
        return ""

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
        # 各轮流式卡保留在聊天流里可回看，这里只发一条退出提示
        if kill:
            return f"⏹️ Exited and stopped **{name}**."
        return (f"✅ Exited interactive mode. **{name}** kept alive.\n"
                f"重新进入: `/enter {mode_sid[:8]}`")

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

        session_id, log_path, pid = await self._create_screen_session(path, command="claude")
        if session_id is None:
            return f"❌ Failed to create session: {pid}"  # pid 复用为错误信息

        self.registry.register(
            session_id, f"claude-{session_id[:12]}",
            name=f"New — {os.path.basename(path)}", pid=pid,
            cwd=path, log_path=log_path,
            session_type="screen", agent="claude",
        )

        # 等待 claude 启动并自动确认 trust prompt（本机启动约 30s）
        started = await self._wait_claude_trust_prompt(session_id, log_path)
        status = "started" if started else "starting (may take a moment)"

        self._enter_interactive_mode(session_id, path, chat_id, started)
        return f"✅ New session in `{path}` ({status}).\nMessages you send will go to this session.\n`/exit` to leave."

    async def _cmd_new_opencode(self, args: list[str], chat_id: str) -> str:
        """Start a new opencode session in a directory and enter interactive mode"""
        path = " ".join(args) if args else os.getcwd()
        if not os.path.isdir(path):
            return f"❌ Not a directory: `{path}`"

        session_id, log_path, pid = await self._create_screen_session(path, command="opencode")
        if session_id is None:
            return f"❌ Failed to create session: {pid}"

        self.registry.register(
            session_id, f"claude-{session_id[:12]}",
            name=f"OpenCode — {os.path.basename(path)}", pid=pid,
            cwd=path, log_path=log_path,
            session_type="screen", agent="opencode",
        )

        # opencode 无 trust prompt，稍等启动；状态由 daemon 从 sqlite 读
        import asyncio as _asyncio
        await _asyncio.sleep(3)

        self._enter_interactive_mode(session_id, path, chat_id, True)
        return f"✅ OpenCode session in `{path}` (started).\nMessages you send will go to this session.\n`/exit` to leave."

    async def _create_screen_session(self, path: str, command: str) -> tuple[Optional[str], str, int]:
        """创建 detached tmux 会话运行指定 agent

        Returns:
            (session_id, log_path, pid)；失败返回 (None, "", 错误码)
        """
        session_id = str(uuid.uuid4())
        log_path = f"/tmp/claude-{session_id}.log"
        try:
            pid = await self.screen_mgr.create(session_id, cwd=path, log_path=log_path, command=command)
            return session_id, log_path, pid
        except Exception as e:
            logger.error("create screen session failed: %s", e)
            return None, "", str(e)

    async def _wait_claude_trust_prompt(self, session_id: str, log_path: str) -> bool:
        """等待 claude 启动并自动确认 trust prompt（claude 专属）"""
        import asyncio as _asyncio
        import re
        for _ in range(35):
            try:
                if os.path.exists(log_path) and os.path.getsize(log_path) > 500:
                    with open(log_path, "rb") as f:
                        raw = f.read()
                    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', raw.decode("utf-8", errors="replace"))
                    if 'enter to confirm' in text.lower():
                        await self.screen_mgr.send_enter(session_id)
                        await _asyncio.sleep(3)
                        return True
                    return True
            except Exception:
                pass
            await _asyncio.sleep(1)
        return False

    def _enter_interactive_mode(self, session_id: str, path: str, chat_id: str, started: bool) -> None:
        """更新列表缓存并进入交互模式（/new 创建后调用）"""
        ctx = _session_context.setdefault(chat_id, {})
        sessions = self.registry.list()
        ctx["sessions"] = [s["id"] for s in sessions]
        ctx["mode"] = session_id
        logger.info("New session + interactive mode: %s %s (ready=%s)", session_id[:8], path, started)

    async def _interactive_round(self, s: dict, text: str, chat_id: str) -> None:
        """交互模式一轮对话：发送 → 流式卡实时刷新 → 稳定后定稿

        每轮发送一张新流式卡（像聊天消息一样追加到聊天流），轮询期间
        卡片原位刷新最新输出（⏳ 执行中），稳定后定稿（✅）。上一轮卡片
        保留在聊天流里，向上滚动即可回看更早的历史。
        由 create_task 后台运行（不阻塞事件消费），异常只记日志。
        """
        try:
            await self._interactive_round_inner(s, text, chat_id)
        except Exception as e:
            logger.error("Interactive round failed: %s", e, exc_info=True)

    async def _interactive_round_inner(self, s: dict, text: str, chat_id: str) -> None:
        """_interactive_round 的实际执行体（异常由外层兜底）"""
        # 发送即建卡（⏳ 状态），后续原位刷新
        card = await self._build_interactive_card(s, [("in", text)], done=False)
        message_id = await asyncio.to_thread(
            self._send_card_to_chat, chat_id, card)
        if not message_id:
            # 卡片发送失败：退化为文本回复（消息已发到会话，不丢）
            output = await self._poll_interactive_output(s)
            await self._send_message(
                chat_id,
                f"📤 Sent to {s.get('name', s['id'][:8])}:\n```\n$ {text}\n```"
                f"\n🤖 Output:\n```\n{output[-800:]}\n```")
            return

        # 轮询输出：有新内容即原位刷新卡片（实时流式效果）
        import time as _time
        deadline = _time.monotonic() + 300  # 最长 5 分钟
        last_output = ""
        stable_rounds = 0
        last_refresh = 0.0
        while _time.monotonic() < deadline:
            await asyncio.sleep(2.0)
            output = await self._read_interactive_output(s)
            if output and output != last_output:
                last_output = output
                stable_rounds = 0
                # 节流：至少 3s 刷一次卡，避免 PATCH 风暴
                now = _time.monotonic()
                if now - last_refresh >= 3.0:
                    await asyncio.to_thread(
                        self._update_card_message, message_id,
                        await self._build_interactive_card(
                            s, [("in", text), ("out", last_output)], done=False))
                    last_refresh = now
            else:
                stable_rounds += 1
            # 输出停更 2 轮（~4s）或出现等待提示 → 稳定
            if last_output and stable_rounds >= 2:
                break
            if last_output and self._looks_stable(last_output):
                break
            # 权威状态回到 waiting/idle → 稳定（claude 状态文件）
            from agent_state import read_agent_state
            st = await asyncio.to_thread(
                read_agent_state,
                s.get("agent", "claude"), s.get("pid", 0), s.get("cwd", ""))
            if st and st.status in ("waiting", "idle"):
                if not last_output and st.text:
                    last_output = st.text
                break

        # 定稿：✅ 状态 + 最终输出（含决策对话框按钮 + 操作按钮）
        final_card = await self._build_interactive_card(
            s, [("in", text), ("out", last_output)], done=True)
        await asyncio.to_thread(self._update_card_message, message_id, final_card)

    async def _build_interactive_card(self, s: dict, history: list[tuple[str, str]],
                                      done: bool) -> str:
        """构建交互聊天卡：读取实时状态/模式/模型 + 决策对话框后渲染

        所有同步 IO（AppleScript 读终端 / 状态文件 / transcript）均经
        to_thread 执行，避免阻塞事件循环。
        """
        decision, mode = await self._read_decision_and_mode(s)
        st_decision_cache[s["id"]] = decision
        model = (await asyncio.to_thread(self._read_model, s)
                 if s.get("agent", "claude") == "claude" else "")
        groups = await self._fetch_model_groups() if model else None
        status = s.get("status", "")
        # 发送后短等状态文件刷新，避免显示上一轮的 executing
        if not done:
            await asyncio.sleep(1.0)
            from agent_state import read_agent_state
            st = await asyncio.to_thread(
                read_agent_state,
                s.get("agent", "claude"), s.get("pid", 0), s.get("cwd", ""))
            if st and st.status:
                status = st.status
        state = InteractiveState(session=s, history=history, status=status,
                                 mode=mode, model=model, model_groups=groups,
                                 done=done)
        return interactive_chat_card(state)

    async def _poll_interactive_output(self, s: dict) -> str:
        """轮询会话输出直到稳定（等待提示 / 输出停更），返回末轮输出

        已被 _interactive_round 的内联轮询取代，仅保留给卡片发送失败的
        退化路径使用。
        """
        import time as _time
        deadline = _time.monotonic() + 90  # 最长 90s
        last_output = ""
        stable_rounds = 0
        while _time.monotonic() < deadline:
            await asyncio.sleep(2.5)
            output = await self._read_interactive_output(s)
            if output and output != last_output:
                last_output = output
                stable_rounds = 0
            else:
                stable_rounds += 1
            # 输出停更 2 轮（~5s）或出现等待提示 → 稳定
            if last_output and stable_rounds >= 2:
                break
            if last_output and self._looks_stable(last_output):
                break
            # 权威状态回到 waiting/idle → 稳定（claude 状态文件）
            from agent_state import read_agent_state
            st = await asyncio.to_thread(
                read_agent_state,
                s.get("agent", "claude"), s.get("pid", 0), s.get("cwd", ""))
            if st and st.status in ("waiting", "idle"):
                if not last_output and st.text:
                    last_output = st.text
                break
        return last_output

    async def _read_interactive_output(self, s: dict) -> str:
        """读取会话当前可见输出（交互模式展示用）

        按会话类型路由：opencode 读 sqlite 最近 assistant 文本（TUI 日志
        是乱码不可用）；screen 读日志文件；terminal 按 TTY 读 tab（无 TTY
        时退回日志文件）。读不到返回占位提示。
        所有同步 IO（AppleScript / 文件读取）均经 to_thread 执行，
        避免阻塞事件循环（2026-11-26 /l 无响应事故根因）。
        """
        agent = s.get("agent", "claude")
        if agent == "opencode":
            return await asyncio.to_thread(self._read_opencode_text, s)
        stype = s.get("session_type", "screen")
        tty = session_tty(s)
        if stype in ("terminal", "ide") and tty:
            out, _ = await asyncio.to_thread(
                self.ide_ctrl.read_terminal_by_tty, tty, 30)
            cleaned = self._clean_terminal_lines(out)
            if cleaned:
                return cleaned
        # screen / 无 TTY 的 terminal：读日志文件
        log_path = s.get("log_path", f"/tmp/claude-{s['id']}.log")
        output = await asyncio.to_thread(self._read_log_output, log_path)
        if output:
            return output
        return "(暂无可读输出，用 /status 查看)"

    def _read_opencode_text(self, s: dict) -> str:
        """OpenCode：读 sqlite 最近一条 assistant 文本"""
        from agent_state import read_agent_state
        st = read_agent_state("opencode", 0, s.get("cwd", ""))
        if st and st.text:
            return st.text
        return "(sent — opencode 输出暂不可读，用 /status 查看)"

    @staticmethod
    def _clean_terminal_lines(out: str) -> str:
        """清理终端可见文本：去掉分隔线/提示符行，保留末尾内容行"""
        if not out:
            return ""
        lines = [l.strip() for l in out.splitlines()
                 if l.strip() and not l.strip().startswith("─")
                 and not l.strip().startswith("❯")
                 and not l.strip().startswith("  ⏵")]
        return "\n".join(lines[-20:])

    async def _read_session_visible(
        self, s: dict, stype: str, agent: str
    ) -> str:
        """读取会话终端可见文本，用于审批提示检测

        terminal 会话按 TTY 抓 Terminal.app tab 内容（非侵入）；
        screen 会话读 tmux pane（capture-pane，非侵入）——仅 opencode，
        claude screen 有 log 文件走原逻辑；
        ide 会话不抢焦点读屏，返回空串。
        AppleScript 经 to_thread 执行，避免阻塞事件循环。
        """
        if stype == "terminal":
            # registry.get 返回的 tags 是 JSON 字符串，session_tty 统一解析
            tty = session_tty(s)
            if not tty:
                return ""
            try:
                live_output, _ = await asyncio.to_thread(
                    self.ide_ctrl.read_terminal_by_tty, tty, 40)
                return live_output
            except Exception:
                return ""
        if stype == "screen" and agent == "opencode":
            try:
                return await self.screen_mgr.read_pane(s["id"], 40)
            except Exception:
                return ""
        return ""

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
        ok = await self._dispatch_send(s, "send", text)
        if ok:
            self.registry.update(session_id, status="running")
            _session_context.setdefault(chat_id, {})["selected"] = session_id
            return (f"✅ Sent to {s.get('name', session_id[:8])}:\n```\n$ {text}\n```"
                    f"{await self._busy_note(s)}")
        # 失败主因是 tab 已关闭（terminal）或 tmux 会话消失（screen），
        # 无头 do script 路径不再依赖焦点，锁屏不会导致此失败
        return (f"❌ Send failed — session tab not found or unavailable. "
                f"用 `/status {session_id[:8]}` 检查{self._send_fail_hint(s)}")

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
        ok = await self._dispatch_send(s, "enter")
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
        # select = 裸字符注入选项号（对话框数字键即选中；bracketed paste
        # 会被对话框忽略），三类会话语义一致（terminal 按 tty 定位 tab）
        ok = await self._dispatch_send(s, "keystroke", str(option))
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
        ok = await self._dispatch_send(s, "ctrl_c")
        if ok:
            self.registry.update(session_id, status="running")
            return f"⚠️ Interrupted `{session_id[:8]}`"
        return "❌ Interrupt failed"

    async def _cmd_mode(self, args: list[str], chat_id: str) -> str:
        """查看或设置 Claude Code 权限模式

        /mode <id>            查看当前模式
        /mode <id> next       Shift+Tab 循环一步
        /mode <id> plan       直达目标模式（auto/manual/accept-edits/plan）
        """
        if not args:
            return ("Usage: `/mode <id|N>` 查看 · `/mode <id|N> next` 循环 · "
                    "`/mode <id|N> (auto|manual|accept-edits|plan)` 直达")
        session_id = await self._resolve_target(args[0], chat_id)
        if not session_id:
            return f"❌ Session not found: `{args[0]}`"
        s = self.registry.get(session_id)
        if not s:
            return "❌ Session no longer exists"
        if s.get("agent") != "claude":
            return "❌ 仅 Claude Code 会话支持模式切换"
        if s.get("session_type") == "ide":
            return "❌ IDE 会话（焦点注入）不支持无头模式切换，请在终端里操作"
        # 无参数：读当前模式
        if len(args) < 2:
            _, mode = await self._read_decision_and_mode(s)
            return f"当前模式: {self._mode_label(mode)} (`{session_id[:8]}`)"
        target = args[1].lower()
        if target in ("next", "cycle"):
            ok = await self._dispatch_send(s, "raw_key", "shift_tab")
            if not ok:
                return f"❌ Mode switch failed{self._send_fail_hint(s)}"
            await asyncio.sleep(0.8)
            _, mode = await self._read_decision_and_mode(s)
            return f"🔁 Mode → {self._mode_label(mode)} (`{session_id[:8]}`)"
        return await self._set_mode(s, target)

    async def _set_mode(self, s: dict, target: str) -> str:
        """把会话切到目标权限模式：按循环顺序算步数连按 Shift+Tab

        每步之间读回实际模式（banner 校验），避免步数错位后盲按。
        读不到 banner 时（对话框覆盖底栏）先 Esc 关掉对话框再读。
        目标不可达（不在循环里）或连按 5 轮仍未命中时报错。
        """
        from decision_detect import MODE_CYCLE, steps_to_mode, KIND_NONE
        sid = s["id"]
        if target not in MODE_CYCLE:
            return (f"❌ 未知模式 `{target}`。可选: {' / '.join(MODE_CYCLE)}")
        for _ in range(5):
            decision, current = await self._read_decision_and_mode(s)
            # 对话框打开时底栏被覆盖、banner 不可读——先 Esc 关掉
            # （只关对话框不影响执行中的任务）
            if current == "" and decision.get("kind", KIND_NONE) != KIND_NONE:
                await self._dispatch_send(s, "raw_key", "esc")
                await asyncio.sleep(1.2)
                decision, current = await self._read_decision_and_mode(s)
            steps = steps_to_mode(current, target)
            if steps == 0:
                return f"✅ Mode = {self._mode_label(target)} (`{sid[:8]}`)"
            for _i in range(steps):
                ok = await self._dispatch_send(s, "raw_key", "shift_tab")
                if not ok:
                    return f"❌ Mode switch failed{self._send_fail_hint(s)}"
                await asyncio.sleep(0.8)
        return f"❌ 切换未收敛（目标 {self._mode_label(target)}），请 `/mode {sid[:8]}` 查看当前状态"

    @staticmethod
    def _mode_label(mode: str) -> str:
        """模式名 → 展示文案（未知模式给 default 提示）"""
        from decision_detect import MODE_LABELS
        return MODE_LABELS.get(mode) or MODE_LABELS["default"]

    async def _cmd_esc(self, args: list[str], chat_id: str) -> str:
        """发送 Esc（关闭对话框 / 取消多选）"""
        if args:
            session_id = await self._resolve_target(args[0], chat_id)
        else:
            session_id = _session_context.get(chat_id, {}).get("selected")
            if not session_id:
                return "Usage: `/esc <id|N>` or `/esc` (last session)"
        if not session_id:
            return "❌ Session not found"
        s = self.registry.get(session_id)
        if not s:
            return "❌ Session no longer exists"
        ok = await self._dispatch_send(s, "raw_key", "esc")
        if ok:
            return f"🚫 Esc → `{session_id[:8]}`"
        return f"❌ Esc failed{self._send_fail_hint(s)}"

    async def _cmd_setmodel(self, args: list[str], chat_id: str) -> str:
        """查看或切换会话模型

        /setmodel <id>          查看当前模型 + 候选列表
        /setmodel <id> <model>  切换（TUI '/model <name>'，切换即保存为默认）
        """
        if not args:
            return "Usage: `/setmodel <id|N>` 查看 · `/setmodel <id|N> <model>` 切换"
        session_id = await self._resolve_target(args[0], chat_id)
        if not session_id:
            return f"❌ Session not found: `{args[0]}`"
        s = self.registry.get(session_id)
        if not s:
            return "❌ Session no longer exists"
        if s.get("agent") != "claude":
            return "❌ 仅 Claude Code 会话支持模型切换"
        current = await asyncio.to_thread(self._read_model, s)
        if len(args) < 2:
            from model_control import collect_known_models
            groups = await self._fetch_model_groups()
            lines = [f"当前模型: `{current or '未知'}` (`{session_id[:8]}`)", ""]
            # 分组展示当前可用模型（实时拉取）
            listed = set()
            for group_name, group_models in groups.items():
                lines.append(f"**{group_name}**")
                for m in group_models:
                    listed.add(m.lower())
                    lines.append(f"  • `{m}`" + (" ← 当前" if m == current else ""))
            # 其他已知模型（settings env + 会话在用）
            others = [m for m in collect_known_models(self.registry.list())
                      if m.lower() not in listed]
            if others:
                lines.append("**其他**")
                lines += [f"  • `{m}`" + (" ← 当前" if m == current else "")
                          for m in others]
            lines.append(f"\n切换: `/setmodel {session_id[:8]} <model>`（支持手动输入任意模型名）")
            return "\n".join(lines)
        # 切换：TUI 斜杠命令直接生效（实测无需对话框交互）
        model = " ".join(args[1:])
        ok = await self._dispatch_send(s, "send", f"/model {model}")
        if not ok:
            return f"❌ Set model failed{self._send_fail_hint(s)}"
        return (f"✅ Set model → `{model}` (`{session_id[:8]}`)\n"
                f"⚠️ 注意：TUI 会把它保存为所有新会话的默认模型")

    async def _cmd_models(self, _args: list[str], chat_id: str) -> str:
        """列出当前可用模型（实时拉取网关，分组展示）"""
        from model_control import collect_known_models
        groups = await self._fetch_model_groups()
        lines = []
        listed = set()
        for group_name, group_models in groups.items():
            lines.append(f"**{group_name}**")
            for m in group_models:
                listed.add(m.lower())
                lines.append(f"  • `{m}`")
        others = [m for m in collect_known_models(self.registry.list())
                  if m.lower() not in listed]
        if others:
            lines.append("**其他**")
            lines += [f"  • `{m}`" for m in others]
        return "\n".join(lines)

    async def _fetch_model_groups(self) -> dict:
        """异步预取模型分组（网关 IO 不阻塞事件循环，5 分钟缓存）"""
        from model_control import fetch_model_groups
        return await asyncio.to_thread(fetch_model_groups)

    async def _fetch_session_models(self, sessions: list[dict]) -> dict[str, str]:
        """批量预取会话当前模型（session_id → model）

        列表卡/待确认卡要展示每个会话在用的模型。读 transcript 是
        subprocess IO，逐个串行会拖慢卡片（10 个会话最多 10×10s），
        这里并发跑在线程池里，整体耗时≈最慢的一个。
        """
        from lark_card import session_model
        claude_sessions = [s for s in sessions if s.get("agent", "claude") == "claude"]
        if not claude_sessions:
            return {}
        results = await asyncio.gather(
            *(asyncio.to_thread(session_model, s) for s in claude_sessions),
            return_exceptions=True,
        )
        models: dict[str, str] = {}
        for s, m in zip(claude_sessions, results):
            if isinstance(m, str) and m:
                models[s["id"]] = m
        return models

    def _read_model(self, s: dict) -> str:
        """读取会话当前模型（transcript 最后一条 assistant 的 model）"""
        from model_control import read_session_model
        pid = s.get("pid", 0)
        if not pid:
            return ""
        try:
            state_fp = os.path.expanduser(f"~/.claude/sessions/{pid}.json")
            with open(state_fp, encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            return ""
        return read_session_model(s.get("cwd", ""), state.get("sessionId", ""))

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
            await self._dispatch_send(s, "ctrl_c")
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

    async def _cmd_daemon(self, args: list[str], chat_id: str) -> str:
        """Control daemon service: stop, restart, status"""
        if not args:
            return "Usage: `/daemon (stop|restart|status)`"
        action = args[0].lower()
        if action == "status":
            import os as _os
            import time as _time
            # 直接返回进程信息，不走 HTTP（避免阻塞事件循环无法响应自身请求）
            pid = _os.getpid()
            # 用 /proc 或 ps 获取进程启动时间
            try:
                import subprocess as _sp
                r = _sp.run(
                    ["ps", "-p", str(pid), "-o", "lstart="],
                    capture_output=True, text=True, timeout=3,
                )
                return f"🟢 Daemon is running\n  PID: {pid}\n  Started: {r.stdout.strip()}"
            except Exception:
                return f"🟢 Daemon is running\n  PID: {pid}"
            import subprocess as _sp
            try:
                _sp.run(
                    ["curl", "-s", "-X", "POST", "http://127.0.0.1:9998/api/daemon/stop",
                     "--connect-timeout", "2", "--max-time", "3"],
                    capture_output=True, text=True, timeout=5,
                )
                return "⏹️ Daemon stopped."
            except Exception:
                return "❌ Failed to stop daemon"
        elif action == "restart":
            import subprocess as _sp
            import os as _os
            daemon_dir = _os.path.dirname(_os.path.abspath(__file__))
            daemon_py = daemon_dir + "/daemon.py"
            # 后台脚本：先 kill 旧进程释放端口，再启动新 daemon
            bg_script = (
                "sleep 0.5 && "
                "kill " + str(_os.getpid()) + " 2>/dev/null; "
                "sleep 2 && "
                "nohup /Users/dp/repo/.venv/bin/python3 " + daemon_py
                + " > /tmp/daemon.log 2>&1 &"
            )
            _sp.Popen(
                ["bash", "-c", bg_script],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )
            return "🔄 Daemon restarting...\nWait a few seconds, then use `/daemon status` to verify."
        return "Usage: `/daemon (stop|restart|status)`"

    # ── Helpers ───────────────────────────────────────

    async def _dispatch_send(self, s: dict, action: str, text: str = "") -> bool:
        """按 session_type 路由发送动作（send=文本+回车 / enter / ctrl_c）

        实际路由逻辑见 dispatch.send_to_session（lark_bot 与 daemon 共用）。
        terminal 类型按 tags.tty 精确定位 Terminal.app 的对应 tab，避免击键落到
        最前面的 tab（修复 /send 把消息发错会话的问题）。
        """
        return await send_to_session(self.ide_ctrl, self.screen_mgr, s, action, text)

    async def _busy_note(self, s: dict) -> str:
        """发送后检查会话是否忙碌，忙碌时提示消息已排队

        Claude Code / OpenCode 均支持生成中提交新消息（自动排队，
        当前任务结束后处理）。忙碌中的提示语与空闲时区分，避免用户
        误以为消息会在本轮立即生效。
        AppleScript 读终端经 to_thread 执行。
        """
        tty = session_tty(s)
        if not tty or s.get("session_type") not in ("terminal", "standalone"):
            return ""
        tail, _ = await asyncio.to_thread(
            self.ide_ctrl.read_terminal_by_tty, tty, 15)
        if tail and IDEControl._tui_busy(tail):
            return "\n\n⏳ 会话正在执行中：消息已排队，将在当前任务结束后处理。"
        return ""

    @staticmethod
    def _send_fail_hint(s: dict) -> str:
        """发送失败的补充提示：IDE 会话锁屏不可注入，给出替代方案"""
        if s.get("session_type") == "ide":
            return ("\n💡 IDE 会话（🔌 内嵌终端）依赖焦点注入，锁屏时不可用、"
                    "未锁屏但终端面板无焦点时也会落错位置。需要锁屏可靠操控的"
                    "会话，请在对应目录用 `lcc` 启动（tmux 后端，完全无头）。")
        return ""

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
        """读取会话输出（状态卡展示用），同步 IO 均经 to_thread 执行"""
        stype = s.get("session_type", "screen")
        session_id = s["id"]
        if stype == "ide":
            # IDE 会话：优先读权威数据（per-pid 状态文件 + transcript 最后一条
            # assistant 消息）——剪贴板抓屏（read_output）依赖 activate+焦点，
            # 多窗口/复制残留时会串台（2026-11-02 状态卡片显示别的会话内容）。
            # 抓屏仅作状态文件缺失时的兜底。
            from ide_control import read_claude_session_state, read_last_assistant_message
            pid = s.get("pid", 0)
            cs = await asyncio.to_thread(read_claude_session_state, pid) if pid else None
            if cs:
                sid = cs.get("sessionId", "")
                text, _ = await asyncio.to_thread(
                    read_last_assistant_message, s.get("cwd", ""), sid, 5000)
                if text:
                    return text, len(text.splitlines())
            out, lc = await asyncio.to_thread(
                self.ide_ctrl.read_output, s.get("app_name", ""), 15)
            return out, lc
        elif stype == "terminal":
            # 按 TTY 精确读取该会话自己的 Terminal tab（非侵入、不抢焦点）。
            # 不能用 read_terminal_full_output —— 它读的是最前台窗口，所有会话会串台
            tty = session_tty(s)
            output, lc = ("", 0)
            if tty:
                output, lc = await asyncio.to_thread(
                    self.ide_ctrl.read_terminal_by_tty, tty, 120)
            if output:
                # 过滤分隔线/提示符行，只保留有意义的内容
                lines = [l for l in output.splitlines()
                         if l.strip() and not l.strip().startswith("─")
                         and not l.strip().startswith("❯")
                         and not l.strip().startswith("  ⏵")]
                if lines:
                    filtered = "\n".join(lines[-60:])
                    return filtered, len(lines)
                return output, lc
            # 兜底：TTY 读不到（tab 已关闭等）时退回日志文件
            output, lc = await self.screen_mgr.read_output(session_id, 15)
            return output, lc
        return await self.screen_mgr.read_output(session_id, 15)

    async def _read_decision_and_mode(self, s: dict) -> tuple[dict, str]:
        """读取会话终端尾部，识别当前决策对话框与权限模式

        Returns:
            (decision, mode)：decision 见 decision_detect.detect_decision，
            mode 为权限模式名（plan / accept-edits / bypass-permissions，
            未知为空串）
        """
        from decision_detect import detect_decision, detect_mode, KIND_NONE
        stype = s.get("session_type", "screen")
        agent = s.get("agent", "claude")
        visible = await self._read_session_visible(s, stype, agent)
        if not visible:
            return {"kind": KIND_NONE, "title": "", "options": [], "hint": ""}, ""
        decision = detect_decision(visible, agent=agent)
        mode = detect_mode(visible) if agent == "claude" else ""
        return decision, mode

    async def _confirm_from_bot(self, s: dict) -> bool:
        session_id = s["id"]
        ok = await self._dispatch_send(s, "enter")
        if ok:
            self.registry.update(session_id, status="running")
        return ok

    async def _interrupt_from_bot(self, s: dict) -> bool:
        session_id = s["id"]
        ok = await self._dispatch_send(s, "ctrl_c")
        if ok:
            self.registry.update(session_id, status="running")
        return ok

    async def _stop_from_bot(self, s: dict) -> bool:
        session_id = s["id"]
        stype = s.get("session_type", "screen")
        pid = s.get("pid", 0)
        if stype in ("ide", "terminal"):
            await self._dispatch_send(s, "ctrl_c")
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
                "lark-cli", "im", "+messages-send", "--chat-id", chat_id, "--msg-type", "text",
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