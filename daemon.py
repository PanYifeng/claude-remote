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
        )
        logger.info("Session registered: %s (%s) type=%s", session_id[:8], data.get("name", ""), session_type)
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

    def _get_app_name(self, session):
        return session.get("app_name") or session.get("win_title", "")

    async def _send_cmd(self, session, text):
        stype = session.get("session_type", "screen")
        if stype == "ide":
            return self.ide_ctrl.send_keys(self._get_app_name(session), text)
        elif stype == "terminal":
            return self.ide_ctrl.send_keys(session.get("app_name") or "Terminal", text)
        elif stype == "screen":
            return await self.screen_mgr.send_keys(session["id"], text)
        else:
            ok = await self.screen_mgr.send_keys(session["id"], text)
            if ok: return True
            app = self._get_app_name(session)
            return self.ide_ctrl.send_keys(app, text) if app else False

    async def _confirm_cmd(self, session):
        stype = session.get("session_type", "screen")
        if stype == "ide":
            return self.ide_ctrl.send_enter(self._get_app_name(session))
        elif stype == "terminal":
            return self.ide_ctrl.send_enter(session.get("app_name") or "Terminal")
        elif stype == "screen":
            return await self.screen_mgr.send_enter(session["id"])
        else:
            ok = await self.screen_mgr.send_enter(session["id"])
            if ok: return True
            app = self._get_app_name(session)
            return self.ide_ctrl.send_enter(app) if app else False

    async def _interrupt_cmd(self, session):
        stype = session.get("session_type", "screen")
        if stype == "ide":
            return self.ide_ctrl.send_ctrl_c(self._get_app_name(session))
        elif stype == "terminal":
            return self.ide_ctrl.send_ctrl_c(session.get("app_name") or "Terminal")
        elif stype == "screen":
            return await self.screen_mgr.send_ctrl_c(session["id"])
        else:
            ok = await self.screen_mgr.send_ctrl_c(session["id"])
            if ok: return True
            app = self._get_app_name(session)
            return self.ide_ctrl.send_ctrl_c(app) if app else False

    async def _select_cmd(self, session, option):
        stype = session.get("session_type", "screen")
        if stype in ("ide", "terminal"):
            app = session.get("app_name") or ("Terminal" if stype == "terminal" else "")
            ok = self.ide_ctrl.send_text(app, str(option))
            if ok: self.ide_ctrl.send_enter(app)
            return ok
        return await self.screen_mgr.select_option(session["id"], option)

    async def _stop_cmd(self, session):
        stype = session.get("session_type", "screen")
        if stype in ("ide", "terminal"):
            app = session.get("app_name") or ("Terminal" if stype == "terminal" else "")
            self.ide_ctrl.send_ctrl_c(app)
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
        now = time.time()

        for s in sessions:
            session_id = s["id"]
            stype = s.get("session_type", "screen")

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
                    # Only use session-specific log file for status detection.
                    # DO NOT use read_terminal_output() — it reads the frontmost
                    # Terminal window, causing ALL sessions to show the same status.
                    output = ""
                    output, _ = await self.screen_mgr.read_output(session_id, 15)

                    if output and ScreenManager._looks_waiting(output):
                        status = "waiting"
                    elif output:
                        lines = [l.strip().rstrip() for l in output.strip().splitlines() if l.strip()]
                        last_line = lines[-1] if lines else ""
                        if last_line.endswith(("❯", "$", "#", ">")):
                            status = "idle"
                        else:
                            status = "executing"
                        self.registry.update(session_id, last_output=output[-500:])
                    else:
                        # No log file — process is alive, status unknown
                        status = "running"

                    if status != s.get("status"):
                        logger.info("Session %s status: %s -> %s", session_id[:8], s.get("status"), status)
                    self.registry.update(session_id, status=status)
                elif stype == "ide":
                    # IDE sessions: no log file, can't read output without focus steal.
                    # Mark as "running" (alive, unknown activity).
                    status = "running"
                    if status != s.get("status"):
                        self.registry.update(session_id, status=status)

                self.registry.update(session_id, updated_at=now)
                continue

            # Screen session
            alive = await self.screen_mgr.is_alive(session_id)
            if not alive:
                self.registry.update(session_id, status="stopped")
                logger.info("Session %s stopped (screen dead)", session_id[:8])
                continue
            output, _ = await self.screen_mgr.read_output(session_id, 10)
            status = await self.screen_mgr.detect_status(session_id, pid=s.get("pid"), last_update=s.get("updated_at"))
            self.registry.update(session_id, status=status, last_output=output[-300:] if len(output) > 300 else output)

    # ── Event consume ────────────────────────────────
    async def event_consume_loop(self):
        """Consume im.message.receive_v1 events via lark-cli

        Reads stdout for NDJSON events. stdin kept open via PIPE.
        """
        logger.info("Starting Lark event consume")

        while self._running:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "lark-cli", "event", "consume",
                    "im.message.receive_v1", "--as", "bot", "--timeout", "0",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                logger.info("Event consume started (PID: %d)", proc.pid)

                assert proc.stdout is not None
                while self._running and proc.returncode is None:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=600)
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
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error("Event consume error: %s", e)
            await asyncio.sleep(5)

    async def start(self):
        self._start_time = time.time()
        active_screens = await self.screen_mgr.list_sessions()
        recovered = self.registry.recover_sessions(set(active_screens))
        logger.info("Recovered %d active sessions from %d active screens", len(recovered), len(active_screens))

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