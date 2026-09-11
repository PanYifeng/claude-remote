#!/usr/bin/env python3
"""Claude Code Remote Control Daemon

Integrates:
- aiohttp HTTP API server
- Session registry (SQLite persistence)
- Screen session management
- Lark bot event handler
- Periodic health check

Usage: python3 daemon.py
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from aiohttp import web

from config import config
from lark_bot import LarkBot
from registry import SessionRegistry
from screen_manager import ScreenManager
from ide_control import ide_control
from agent_state import read_agent_state
from dispatch import send_to_session


def setup_logging() -> None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers = [logging.StreamHandler(sys.stdout)]
    log_path = config.log_path
    if log_path:
        handlers.append(logging.FileHandler(log_path))
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.server").setLevel(logging.WARNING)


logger = logging.getLogger("daemon")


class Daemon:
    def __init__(self):
        setup_logging()
        logger.info("Initializing daemon...")
        logger.info("  Data directory: %s", config.data_dir)
        self.registry = SessionRegistry(config.db_path)
        self.screen_mgr = ScreenManager()
        self.ide_ctrl = ide_control
        self.lark_bot = LarkBot(self.registry, self.screen_mgr, self.ide_ctrl)
        self.app = web.Application()
        self._setup_routes()
        self._running = True
        self._health_task: Optional[asyncio.Task] = None
        self._event_task: Optional[asyncio.Task] = None
        self._session_write_locks: dict[str, asyncio.Lock] = {}

    def _setup_routes(self) -> None:
        self.app.router.add_get("/api/sessions", self.handle_list_sessions)
        self.app.router.add_get("/api/session/{session_id}", self.handle_get_session)
        self.app.router.add_post("/api/session/register", self.handle_register_session)
        self.app.router.add_put("/api/session/{session_id}/heartbeat", self.handle_session_heartbeat)
        self.app.router.add_delete("/api/session/{session_id}", self.handle_delete_session)
        self.app.router.add_post("/api/session/{session_id}/send", self.handle_send_command)
        self.app.router.add_post("/api/session/{session_id}/confirm", self.handle_confirm)
        self.app.router.add_post("/api/session/{session_id}/select", self.handle_select)
        self.app.router.add_post("/api/session/{session_id}/interrupt", self.handle_interrupt)
        self.app.router.add_post("/api/session/{session_id}/stop", self.handle_stop)
        self.app.router.add_post("/lark/webhook", self.handle_lark_webhook)
        self.app.router.add_get("/health", self.handle_health)
        self.app.router.add_post("/api/daemon/stop", self.handle_daemon_stop)
        self.app.router.add_post("/api/daemon/restart", self.handle_daemon_restart)

    # ── Handlers ────────────────────────────────
    async def handle_register_session(self, request):
        try:
            data = await request.json()
        except Exception:
            return self._json({"error": "invalid JSON"}, status=400)
        session_id = data.get("id")
        if not session_id:
            return self._json({"error": "missing id"}, status=400)
        session_type = data.get("session_type", "screen")
        screen_name = f"claude-{session_id[:12]}"
        log_path = f"/tmp/claude-{session_id}.log"
        session = self.registry.register(
            session_id, screen_name,
            name=data.get("name", ""), pid=data.get("pid", 0),
            cwd=data.get("cwd", ""), log_path=log_path,
            tags=data.get("tags"), session_type=session_type,
            app_name=data.get("app_name", ""), win_title=data.get("win_title", ""),
            agent=data.get("agent", "claude"),
        )
        logger.info("Session registered: %s (%s) type=%s agent=%s", session_id[:8], data.get("name", ""), session_type, data.get("agent", "claude"))
        return self._json({"ok": True, "session": session})

    async def handle_session_heartbeat(self, request):
        session_id = request.match_info["session_id"]
        try:
            data = await request.json()
        except Exception:
            data = {}
        update = {}
        if data.get("pid"): update["pid"] = data["pid"]
        if data.get("output") is not None: update["last_output"] = data["output"]
        if data.get("status"): update["status"] = data["status"]
        session = self.registry.update(session_id, **update)
        if not session:
            return self._json({"error": "not found"}, status=404)
        return self._json({"ok": True, "session": session})

    async def handle_delete_session(self, request):
        session_id = request.match_info["session_id"]
        self.registry.delete(session_id)
        logger.info("Session deleted: %s", session_id[:8])
        return self._json({"ok": True})

    async def _send_cmd(self, session, text):
        return await send_to_session(self.ide_ctrl, self.screen_mgr, session, "send", text)

    async def _confirm_cmd(self, session):
        return await send_to_session(self.ide_ctrl, self.screen_mgr, session, "enter")

    async def _interrupt_cmd(self, session):
        return await send_to_session(self.ide_ctrl, self.screen_mgr, session, "ctrl_c")

    async def _select_cmd(self, session, option):
        # select = 发"选项号+回车"，三类会话语义一致（terminal 按 tty 定位 tab）
        return await send_to_session(self.ide_ctrl, self.screen_mgr, session, "send", str(option))

    async def _stop_cmd(self, session):
        stype = session.get("session_type", "screen")
        pid = session.get("pid", 0)
        if stype in ("ide", "terminal", "standalone"):
            # 先发 Ctrl+C，再 kill 进程确保退出
            await send_to_session(self.ide_ctrl, self.screen_mgr, session, "ctrl_c")
            if pid:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "kill", "-9", str(pid),
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc.wait()
                except Exception:
                    pass
            return True
        await self.screen_mgr.send_ctrl_c(session["id"])
        await asyncio.sleep(0.5)
        await self.screen_mgr.send_keys(session["id"], "exit")
        await asyncio.sleep(1)
        return await self.screen_mgr.kill(session["id"])

    async def handle_list_sessions(self, request):
        status_filter = request.query.get("status")
        sessions = self.registry.list(status_filter)
        counts = self.registry.count_by_status()
        return self._json({"sessions": sessions, "counts": counts})

    async def handle_get_session(self, request):
        session_id = request.match_info["session_id"]
        session = self.registry.get(session_id)
        if not session:
            return self._json({"error": "not found"}, status=404)
        stype = session.get("session_type", "screen")
        app_name = session.get("app_name", "")
        if stype == "ide":
            try:
                output, line_count = self.ide_ctrl.read_output(app_name, 50)
                session["live_output"] = output
                session["live_line_count"] = line_count
            except Exception:
                session["live_output"] = ""
                session["live_line_count"] = 0
            session["detected_status"] = session.get("status", "running")
        else:
            output, line_count = await self.screen_mgr.read_output(session_id, 50)
            session["live_output"] = output
            session["live_line_count"] = line_count
            detected = await self.screen_mgr.detect_status(session_id, pid=session.get("pid"), last_update=session.get("updated_at"))
            session["detected_status"] = detected
        return self._json({"session": session})

    async def handle_send_command(self, request):
        session_id = request.match_info["session_id"]
        try:
            data = await request.json()
        except Exception:
            return self._json({"error": "invalid JSON"}, status=400)
        text = data.get("text", "")
        if not text:
            return self._json({"error": "missing text"}, status=400)
        session = self.registry.get(session_id)
        if not session:
            return self._json({"error": "not found"}, status=404)
        ok = await self._send_cmd(session, text)
        if ok:
            self.registry.update(session_id, status="running")
            return self._json({"ok": True, "sent": text})
        return self._json({"error": "unavailable"}, status=410)

    async def handle_confirm(self, request):
        session_id = request.match_info["session_id"]
        session = self.registry.get(session_id)
        if not session: return self._json({"error": "not found"}, status=404)
        ok = await self._confirm_cmd(session)
        if ok:
            self.registry.update(session_id, status="running")
            return self._json({"ok": True})
        return self._json({"error": "unavailable"}, status=410)

    async def handle_select(self, request):
        session_id = request.match_info["session_id"]
        try:
            data = await request.json()
            option = int(data.get("option", 0))
        except Exception:
            return self._json({"error": "need option"}, status=400)
        session = self.registry.get(session_id)
        if not session: return self._json({"error": "not found"}, status=404)
        ok = await self._select_cmd(session, option)
        if ok:
            self.registry.update(session_id, status="running")
            return self._json({"ok": True, "option": option})
        return self._json({"error": "unavailable"}, status=410)

    async def handle_interrupt(self, request):
        session_id = request.match_info["session_id"]
        session = self.registry.get(session_id)
        if not session: return self._json({"error": "not found"}, status=404)
        ok = await self._interrupt_cmd(session)
        if ok:
            self.registry.update(session_id, status="running")
            return self._json({"ok": True})
        return self._json({"error": "unavailable"}, status=410)

    async def handle_stop(self, request):
        session_id = request.match_info["session_id"]
        session = self.registry.get(session_id)
        if not session: return self._json({"error": "not found"}, status=404)
        ok = await self._stop_cmd(session)
        self.registry.update(session_id, status="stopped")
        return self._json({"ok": ok})

    async def handle_lark_webhook(self, request):
        try:
            body = await request.json()
        except Exception:
            return self._json({"error": "invalid JSON"}, status=400)
        result = await self.lark_bot.handle_webhook(body)
        if result is not None:
            return self._json(result)
        return self._json({"code": 0})

    async def handle_health(self, request):
        uptime = time.time() - getattr(self, "_start_time", time.time())
        return self._json({"status": "ok", "sessions": self.registry.count_by_status(), "uptime": uptime})

    async def handle_daemon_stop(self, request):
        """Stop the daemon gracefully via API (used by /daemon stop command)"""
        logger.info("Daemon stop requested via API")
        asyncio.create_task(self._shutdown())
        return self._json({"ok": True, "message": "Shutting down..."})

    async def handle_daemon_restart(self, request):
        """Restart the daemon via API (used by /daemon restart command)"""
        logger.info("Daemon restart requested via API")
        asyncio.create_task(self._restart())
        return self._json({"ok": True, "message": "Restarting..."})

    async def _shutdown(self):
        """Graceful shutdown"""
        self._running = False
        if self._health_task:
            self._health_task.cancel()
        if self._event_task:
            self._event_task.cancel()

    async def _restart(self):
        """Shutdown then restart daemon"""
        import subprocess as _sp
        import os as _os
        # 启动新的 daemon 进程（使用 nohup 确保独立于当前进程树）
        log_file = open("/tmp/daemon.log", "a")
        _sp.Popen(
            ["nohup", "/Users/dp/repo/.venv/bin/python3", __file__],
            stdout=log_file,
            stderr=_sp.STDOUT,
            stdin=_os.devnull,
        )
        logger.info("New daemon spawned, shutting down old")
        # 等待 500ms 确保 HTTP 响应已发送，再退出
        await asyncio.sleep(0.5)
        _os._exit(0)

    # ── Health check ────────────────────────────────
    async def health_check_loop(self):
        logger.info("Health check loop started (interval: %.1fs)", config.health_check_interval)
        while self._running:
            try:
                await self._run_health_check()
            except Exception as e:
                logger.error("Health check failed: %s", e)
            await asyncio.sleep(config.health_check_interval)

    async def _run_health_check(self):
        sessions = self.registry.list(status_filter="running")
        sessions += self.registry.list(status_filter="waiting")
        sessions += self.registry.list(status_filter="idle")
        sessions += self.registry.list(status_filter="executing")
        now = time.time()

        # 自动发现并注册新启动的 claude 进程
        await self._auto_discover_claude_processes(sessions)

        # 重新读取 session（可能被 auto_discover 新增了）
        sessions = self.registry.list(status_filter="running")
        sessions += self.registry.list(status_filter="waiting")
        sessions += self.registry.list(status_filter="idle")
        sessions += self.registry.list(status_filter="executing")

        for s in sessions:
            session_id = s["id"]
            stype = s.get("session_type", "screen")
            agent = s.get("agent", "claude")

            if stype in ("ide", "terminal", "standalone"):
                updated_at = s.get("updated_at", 0)
                elapsed = now - updated_at
                pid = s.get("pid", 0)
                alive = True
                if pid:
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            "kill", "-0", str(pid),
                            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                        )
                        await proc.wait()
                        alive = proc.returncode == 0
                    except Exception:
                        alive = False
                if not alive:
                    self.registry.update(session_id, status="stopped")
                    logger.info("Session %s stopped (process dead)", session_id[:8])
                    continue
                if elapsed > 120 and not pid:
                    self.registry.update(session_id, status="stopped")
                    continue

                # Terminal: read output and detect waiting vs executing vs idle
                # NEVER read IDE output here (steals focus via Cmd+A/Cmd+C)
                if stype == "terminal":
                    # 优先用 agent 的权威状态（claude: per-process 状态文件；opencode: sqlite）
                    st = read_agent_state(agent, pid, s.get("cwd", ""))
                    status = st.status if (st and st.session_id) else None
                    if status:
                        # 有 log 文件的会话再补一刀 approval 提示检测（claude status 未必标 waiting）
                        log_path = s.get("log_path", f"/tmp/claude-{session_id}.log")
                        if os.path.exists(log_path):
                            try:
                                output, _ = await self.screen_mgr.read_output(session_id, 15)
                                if output and ScreenManager._looks_waiting(output):
                                    status = "waiting"
                                    self.registry.update(session_id, last_output=output[-500:])
                            except Exception:
                                pass
                    elif os.path.exists(s.get("log_path", f"/tmp/claude-{session_id}.log")):
                        # 无状态文件但有 log：用 log 内容判定
                        output, _ = await self.screen_mgr.read_output(session_id, 15)
                        if output:
                            if ScreenManager._looks_waiting(output):
                                status = "waiting"
                            else:
                                lines = [l.strip().rstrip() for l in output.strip().splitlines() if l.strip()]
                                has_prompt = any(l == "❯" or l.rstrip("\xa0").endswith("❯") for l in lines)
                                last_line = lines[-1] if lines else ""
                                if has_prompt or last_line.endswith(("❯", "$", "#", ">")):
                                    status = "idle"
                                else:
                                    status = "executing"
                                self.registry.update(session_id, last_output=output[-500:])
                        else:
                            status = "running"
                    else:
                        # 无状态文件无 log：无法判定
                        status = "running"

                    if status != s.get("status"):
                        logger.info("Session %s status: %s -> %s", session_id[:8], s.get("status"), status)
                    self.registry.update(session_id, status=status)
                elif stype == "ide":
                    # IDE 会话：用 agent 权威状态判定（非侵入，不抢焦点）
                    st = read_agent_state(agent, pid, s.get("cwd", ""))
                    status = st.status if (st and st.session_id) else None
                    if not status:
                        # 无状态文件：保留原状态（不抢焦点读屏）
                        status = s.get("status", "running")
                    if status != s.get("status"):
                        logger.info("Session %s status: %s -> %s", session_id[:8], s.get("status"), status)
                        self.registry.update(session_id, status=status)
                continue

            # Screen session
            alive = await self.screen_mgr.is_alive(session_id)
            if not alive:
                self.registry.update(session_id, status="stopped")
                logger.info("Session %s stopped (screen dead)", session_id[:8])
                continue
            # OpenCode：状态从 sqlite 读（tmux 日志是 TUI 乱码，不可用）
            if agent == "opencode":
                st = read_agent_state(agent, 0, s.get("cwd", ""))
                status = st.status if (st and st.status) else s.get("status", "running")
                text = st.text if st else ""
                if status != s.get("status"):
                    logger.info("Session %s status: %s -> %s", session_id[:8], s.get("status"), status)
                if text:
                    self.registry.update(session_id, status=status, last_output=text[-800:])
                else:
                    self.registry.update(session_id, status=status)
                continue
            output, _ = await self.screen_mgr.read_output(session_id, 10)
            status = await self.screen_mgr.detect_status(session_id, pid=s.get("pid"), last_update=s.get("updated_at"))
            self.registry.update(session_id, status=status, last_output=output[-300:] if len(output) > 300 else output)

    # ── Event consume ────────────────────────────────
    async def _auto_discover_claude_processes(self, existing_sessions: list[dict]) -> None:
        """自动发现新启动的 claude 进程并注册到 daemon

        每轮健康检查时扫描系统 claude 进程，如果发现未注册的新进程，
        自动注册它。这样即使 scan-existing 没运行，新开的 session 也会被自动发现。
        """
        import uuid as _uuid

        # 获取已注册的 PID 集合（包括 stopped 的 — 防止重复注册已停止的进程）
        registered_pids = set()
        all_sessions = self.registry.list()
        for s in all_sessions:
            pid = s.get("pid", 0)
            if pid:
                registered_pids.add(pid)

        # 扫描当前 agent 进程（claude 与 opencode）
        # 支持的 agent 二进制名
        agent_binaries = {"claude", "opencode"}
        try:
            proc = await asyncio.create_subprocess_exec(
                "ps", "-eo", "pid,tty,comm",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            claude_pids = set()
            pid_agent_map: dict[int, str] = {}  # pid → agent 类型（claude/opencode）
            pid_tty_map: dict[int, str] = {}  # pid → tty，注册时存入 tags，便于按 TTY 定位终端 tab
            for line in stdout.decode().splitlines():
                parts = line.strip().split(None, 2)
                if len(parts) < 3:
                    continue
                pid_str, tty, comm = parts
                if comm in agent_binaries and tty != "??":
                    try:
                        pid = int(pid_str)
                        claude_pids.add(pid)
                        pid_agent_map[pid] = comm
                        pid_tty_map[pid] = tty
                    except ValueError:
                        continue
        except Exception:
            return

        # 排除 daemon 自身的进程和 screen 子进程
        import os as _os
        claude_pids.discard(_os.getpid())

        # 排除 screen 管理的子进程（log 文件由 screen 管理，不是独立 session）
        screen_claude_pids = set()
        try:
            sp = await asyncio.create_subprocess_exec(
                "ps", "-eo", "pid,ppid,comm",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            sp_stdout, _ = await sp.communicate()
            all_procs = sp_stdout.decode().splitlines()
            # 找到所有 SCREEN 进程
            screen_pids = set()
            for line in all_procs:
                parts = line.strip().split(None, 2)
                if len(parts) >= 3 and parts[2] == "SCREEN":
                    screen_pids.add(int(parts[0]))
            # 找到 SCREEN 进程树下的所有子进程（递归）
            all_pids = {}
            for line in all_procs:
                parts = line.strip().split(None, 2)
                if len(parts) >= 3:
                    try:
                        all_pids[int(parts[0])] = int(parts[1])
                    except ValueError:
                        continue
            for pid, ppid in list(all_pids.items()):
                # 向上追溯父进程链，看是否最终属于某个 SCREEN
                cur = pid
                for _ in range(10):
                    if cur in screen_pids:
                        screen_claude_pids.add(pid)
                        break
                    if cur <= 1:
                        break
                    cur = all_pids.get(cur, 0)
        except Exception:
            pass
        claude_pids -= screen_claude_pids

        # 注册新发现的进程
        for pid in sorted(claude_pids):
            if pid in registered_pids:
                continue

            # 获取进程详细信息
            try:
                cwd_proc = await asyncio.create_subprocess_exec(
                    "lsof", "-p", str(pid), "-Fn", "-a", "-d", "cwd",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                cwd_stdout, _ = await cwd_proc.communicate()
                cwd = ""
                for cl in cwd_stdout.decode().splitlines():
                    if cl.startswith("n/"):
                        cwd = cl[1:]
                        break
            except Exception:
                cwd = ""

            # 检测进程类型（IDE vs Terminal）
            kind, app_name = "terminal", ""
            try:
                ppid_proc = await asyncio.create_subprocess_exec(
                    "ps", "-p", str(pid), "-o", "ppid=",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                ppid_stdout, _ = await ppid_proc.communicate()
                ppid = ppid_stdout.decode().strip()

                # 向上查 5 层父进程找 IDE
                found_ide = False
                seen = {int(pid), int(ppid)} if ppid and ppid.isdigit() else {int(pid)}
                current_pid = int(ppid) if ppid and ppid.isdigit() else 0
                for _ in range(5):
                    if current_pid <= 1:
                        break
                    try:
                        p = await asyncio.create_subprocess_exec(
                            "ps", "-p", str(current_pid), "-o", "comm=",
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                        )
                        p_stdout, _ = await p.communicate()
                        comm = p_stdout.decode().strip().lower()
                        ide_map = {"idea": "IntelliJ IDEA", "pycharm": "PyCharm",
                                   "code": "Code", "cursor": "Cursor", "windsurf": "Windsurf"}
                        for key, name in ide_map.items():
                            if key in comm:
                                kind, app_name = "ide", name
                                found_ide = True
                                break
                        if found_ide:
                            break
                        pp = await asyncio.create_subprocess_exec(
                            "ps", "-p", str(current_pid), "-o", "ppid=",
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                        )
                        pp_stdout, _ = await pp.communicate()
                        next_pid = int(pp_stdout.decode().strip()) if pp_stdout.decode().strip().isdigit() else 0
                        if next_pid in seen or next_pid <= 1:
                            break
                        seen.add(next_pid)
                        current_pid = next_pid
                    except Exception:
                        break
            except Exception:
                pass

            # 注册新 session
            session_id = str(_uuid.uuid4())
            stype = "ide" if kind == "ide" else "terminal"
            name = f"{app_name} — {cwd.split('/')[-1] if cwd else '?'}" if app_name else f"Terminal — {cwd.split('/')[-1] if cwd else '?'}"
            log_path = f"/tmp/claude-{session_id}.log"
            screen_name = f"claude-{session_id[:12]}"

            # 把 TTY 存入 tags，便于后续按 TTY 精确定位 Terminal.app 的 tab 读取输出
            session_tags = {"auto_discovered": True}
            tty = pid_tty_map.get(pid, "")
            if tty:
                session_tags["tty"] = tty
            agent = pid_agent_map.get(pid, "claude")

            self.registry.register(
                session_id, screen_name,
                name=name, pid=pid,
                cwd=cwd, log_path=log_path,
                tags=session_tags, session_type=stype, app_name=app_name,
                agent=agent,
            )
            logger.info("Auto-discovered new session: %s (%s) pid=%d type=%s agent=%s", session_id[:8], name, pid, stype, agent)

        # 回填：已注册的 terminal session 如果 tags 里没有 tty，按 pid 补上
        for s in all_sessions:
            if s.get("session_type") != "terminal":
                continue
            tags = s.get("tags", {}) or {}
            if isinstance(tags, dict) and tags.get("tty"):
                continue
            spid = s.get("pid", 0)
            tty = pid_tty_map.get(spid, "")
            if not tty:
                # pid_tty_map 只含本轮发现的 agent 进程；这里是回填，单独查一次 ps
                try:
                    tp = await asyncio.create_subprocess_exec(
                        "ps", "-p", str(spid), "-o", "tty=",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    )
                    ts, _ = await tp.communicate()
                    tty = ts.decode().strip()
                except Exception:
                    tty = ""
            if tty and tty != "??":
                new_tags = dict(tags) if isinstance(tags, dict) else {}
                new_tags["tty"] = tty
                self.registry.update(s["id"], tags=json.dumps(new_tags, ensure_ascii=False))

    async def event_consume_loop(self):
        """Consume im.message.receive_v1 events via lark-cli

        Must keep stdin PIPE open explicitly — lark-cli exits on stdin EOF.
        """
        logger.info("Starting Lark event consume")

        while self._running:
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    "lark-cli", "event", "consume",
                    "im.message.receive_v1", "--as", "bot", "--timeout", "0",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,  # 新进程组 → killpg 杀整棵树（node+lark-cli+_bus），防孤儿泄露
                )
                logger.info("Event consume started (PID: %d)", proc.pid)
                stdin_keepalive = proc.stdin  # 防 GC 关 pipe（lark-cli 遇 stdin EOF 退出）

                assert proc.stdout is not None
                while self._running and proc.returncode is None:
                    try:
                        line = await asyncio.wait_for(proc.stdout.readline(), timeout=600)
                    except asyncio.TimeoutError:
                        continue  # 10min 无事件，继续读同一 proc（旧代码此处抛到外层 except → 重新 spawn 新 proc，旧 proc 孤儿化 → 1818 泄露）
                    if not line:
                        break
                    raw = line.decode("utf-8", errors="replace").strip()
                    if not raw or raw.startswith("["):
                        continue
                    if raw.startswith("{"):
                        try:
                            event_data = json.loads(raw)
                            logger.info("Event received: keys=%s", list(event_data.keys())[:5])
                            await self.lark_bot.handle_event_line(event_data)
                        except json.JSONDecodeError:
                            pass
                        except Exception as e:
                            logger.error("Event handler error: %s", e)

                await proc.wait()
                logger.warning("Event consume exited (code: %d), restarting in 5s...", proc.returncode)
                del stdin_keepalive
            except asyncio.CancelledError:
                await self._kill_proc_tree(proc)  # task 取消（shutdown/restart）——杀整棵树防孤儿
                raise
            except Exception as e:
                logger.error("Event consume error: %s", e)
                await self._kill_proc_tree(proc)
            await asyncio.sleep(5)

    async def _kill_proc_tree(self, proc) -> None:
        """杀整棵进程树（start_new_session 后 proc 是新进程组组长，killpg 杀全组：node+lark-cli+_bus）。"""
        if proc is None or proc.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass

    async def start(self):
        self._start_time = time.time()
        active_screens = await self.screen_mgr.list_sessions()
        recovered = self.registry.recover_sessions(set(active_screens))
        logger.info("Recovered %d active sessions from %d active screens", len(recovered), len(active_screens))

        # 启动时重置 terminal/IDE session 状态，让 health check 重新判定
        # 但保留 waiting 状态（用户通过 /status 检测到的）
        all_sessions = self.registry.list()
        for s in all_sessions:
            if s.get("status") == "waiting":
                continue
            if s.get("session_type") in ("terminal", "ide") and s.get("status") != "stopped":
                self.registry.update(s["id"], status="running")
                logger.info("Reset session %s status -> running (type=%s)", s["id"][:8], s.get("session_type"))

        # 去重：同一 PID 保留最新一条记录，删除旧的
        dedup_pids: dict[int, str] = {}  # pid -> session_id (keep newest)
        dups = []
        for s in sorted(all_sessions, key=lambda x: x.get("updated_at", 0)):
            pid = s.get("pid", 0)
            if pid and s.get("status") != "stopped":
                if pid in dedup_pids:
                    dups.append(s["id"])
                    logger.info("Duplicate non-stopped session for PID %s: keep=%s delete=%s",
                                pid, dedup_pids[pid][:8], s["id"][:8])
                else:
                    dedup_pids[pid] = s["id"]
        for dup_id in dups:
            self.registry.delete(dup_id)

        self._health_task = asyncio.create_task(self.health_check_loop(), name="health-check")

        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", config.daemon_port)
        await site.start()

        self._event_task = asyncio.create_task(self.event_consume_loop(), name="lark-msg")

        logger.info("Daemon started on http://127.0.0.1:%d", config.daemon_port)

        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
            self._running = False
            if self._health_task: self._health_task.cancel()
            if self._event_task: self._event_task.cancel()

    @staticmethod
    def _json(data, status=200):
        return web.json_response(data, status=status, headers={"Access-Control-Allow-Origin": "*"})


def main():
    daemon = Daemon()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(daemon.start())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        loop.close()


if __name__ == "__main__":
    main()