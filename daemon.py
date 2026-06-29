#!/usr/bin/env python3
"""Claude Code Remote Control Daemon

Integrates:
- aiohttp HTTP API server
- Session registry (SQLite persistence)
- Screen session management
- Lark bot webhook handler
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
from typing import Optional

from aiohttp import web

from config import config
from lark_bot import LarkBot
from registry import SessionRegistry
from screen_manager import ScreenManager
from ide_control import ide_control

import asyncio
import json
import logging
import os
import signal
import sys
import time
from typing import Optional

from aiohttp import web

from config import config
from lark_bot import LarkBot
from registry import SessionRegistry
from screen_manager import ScreenManager
from ide_control import ide_control

# ── 日志配置 ──────────────────────────────────────────────
def setup_logging() -> None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers = []

    # 总是输出到 stdout
    handlers.append(logging.StreamHandler(sys.stdout))

    # 也配置日志文件
    log_path = config.log_path
    if log_path:
        handlers.append(logging.FileHandler(log_path))

    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=handlers,
    )
    # 降低 aiohttp 内部日志级别
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.server").setLevel(logging.WARNING)


logger = logging.getLogger("daemon")


# ── Daemon 主类 ──────────────────────────────────────────
class Daemon:
    """管理所有 Claude Code shell session 的 daemon"""

    def __init__(self):
        setup_logging()

        # 初始化各组件
        logger.info("Initializing daemon...")
        logger.info("  Data directory: %s", config.data_dir)

        self.registry = SessionRegistry(config.db_path)
        self.screen_mgr = ScreenManager()
        self.ide_ctrl = ide_control
        self.lark_bot = LarkBot(self.registry, self.screen_mgr, self.ide_ctrl)

        # aiohttp app
        self.app = web.Application()
        self._setup_routes()

        # 运行状态
        self._running = True
        self._health_task: Optional[asyncio.Task] = None
        self._event_task: Optional[asyncio.Task] = None
        self._session_write_locks: dict[str, asyncio.Lock] = {}

    # ── HTTP 路由 ────────────────────────────────────────────
    def _setup_routes(self) -> None:
        """注册所有 HTTP 路由"""
        self.app.router.add_get("/api/sessions", self.handle_list_sessions)
        self.app.router.add_get(
            "/api/session/{session_id}", self.handle_get_session
        )
        self.app.router.add_post(
            "/api/session/register", self.handle_register_session
        )
        self.app.router.add_put(
            "/api/session/{session_id}/heartbeat",
            self.handle_session_heartbeat,
        )
        self.app.router.add_delete(
            "/api/session/{session_id}", self.handle_delete_session
        )
        self.app.router.add_post(
            "/api/session/{session_id}/send", self.handle_send_command
        )
        self.app.router.add_post(
            "/api/session/{session_id}/confirm",
            self.handle_confirm,
        )
        self.app.router.add_post(
            "/api/session/{session_id}/select",
            self.handle_select,
        )
        self.app.router.add_post(
            "/api/session/{session_id}/interrupt",
            self.handle_interrupt,
        )
        self.app.router.add_post(
            "/api/session/{session_id}/stop", self.handle_stop
        )
        self.app.router.add_post("/lark/webhook", self.handle_lark_webhook)
        self.app.router.add_get("/health", self.handle_health)

    # ── Handler: 内部 API ──────────────────────────────────
    async def handle_register_session(
        self, request: web.Request
    ) -> web.Response:
        """注册新 session（由 lcc wrapper 或 ide-register 脚本调用）"""
        try:
            data = await request.json()
        except Exception:
            return self._json(
                {"error": "无效的 JSON body"}, status=400
            )

        session_id = data.get("id")
        if not session_id:
            return self._json({"error": "缺少 id 字段"}, status=400)

        session_type = data.get("session_type", "screen")
        screen_name = f"claude-{session_id[:12]}"
        log_path = f"/tmp/claude-{session_id}.log"

        # 注册 session
        session = self.registry.register(
            session_id,
            screen_name,
            name=data.get("name", ""),
            pid=data.get("pid", 0),
            cwd=data.get("cwd", ""),
            log_path=log_path,
            tags=data.get("tags"),
            session_type=session_type,
            app_name=data.get("app_name", ""),
            win_title=data.get("win_title", ""),
        )

        logger.info(
            "Session registered: %s (%s) type=%s",
            session_id[:8], data.get("name", ""), session_type,
        )
        return self._json({"ok": True, "session": session})

    async def handle_session_heartbeat(
        self, request: web.Request
    ) -> web.Response:
        """Session 心跳（由 lcc wrapper 或 scan-existing 定期调用）"""
        session_id = request.match_info["session_id"]

        try:
            data = await request.json()
        except Exception:
            data = {}

        # 更新 session
        update = {}
        if data.get("pid"):
            update["pid"] = data["pid"]
        if data.get("output") is not None:
            update["last_output"] = data["output"]
        if data.get("status"):
            # 允许通过心跳恢复 session 状态
            update["status"] = data["status"]

        session = self.registry.update(session_id, **update)
        if not session:
            return self._json({"error": "session 不存在"}, status=404)

        return self._json({"ok": True, "session": session})

    async def handle_delete_session(
        self, request: web.Request
    ) -> web.Response:
        """删除 session（由 lcc wrapper 退出时调用）"""
        session_id = request.match_info["session_id"]
        self.registry.delete(session_id)
        logger.info("Session deleted: %s", session_id[:8])
        return self._json({"ok": True})

    # ── 命令分发辅助 ──────────────────────────────────────────
    def _get_app_name(self, session: dict) -> str:
        """获取给 IDE 控制用的 app 名称"""
        return session.get("app_name") or session.get("win_title", "")

    async def _send_cmd(self, session: dict, text: str) -> bool:
        """Dispatch send command based on session type"""
        stype = session.get("session_type", "screen")
        if stype == "ide":
            app = self._get_app_name(session)
            return self.ide_ctrl.send_keys(app, text)
        elif stype in ("terminal",):
            app = session.get("app_name") or "Terminal"
            return self.ide_ctrl.send_keys(app, text)
        elif stype == "screen":
            return await self.screen_mgr.send_keys(session["id"], text)
        else:
            ok = await self.screen_mgr.send_keys(session["id"], text)
            if ok:
                return True
            app = self._get_app_name(session)
            if app:
                return self.ide_ctrl.send_keys(app, text)
            return False

    async def _confirm_cmd(self, session: dict) -> bool:
        """Send Enter confirmation"""
        stype = session.get("session_type", "screen")
        if stype == "ide":
            app = self._get_app_name(session)
            return self.ide_ctrl.send_enter(app)
        elif stype in ("terminal",):
            app = session.get("app_name") or "Terminal"
            return self.ide_ctrl.send_enter(app)
        elif stype == "screen":
            return await self.screen_mgr.send_enter(session["id"])
        else:
            ok = await self.screen_mgr.send_enter(session["id"])
            if ok:
                return True
            app = self._get_app_name(session)
            if app:
                return self.ide_ctrl.send_enter(app)
            return False

    async def _interrupt_cmd(self, session: dict) -> bool:
        """Send Ctrl+C"""
        stype = session.get("session_type", "screen")
        if stype == "ide":
            app = self._get_app_name(session)
            return self.ide_ctrl.send_ctrl_c(app)
        elif stype in ("terminal",):
            app = session.get("app_name") or "Terminal"
            return self.ide_ctrl.send_ctrl_c(app)
        elif stype == "screen":
            return await self.screen_mgr.send_ctrl_c(session["id"])
        else:
            ok = await self.screen_mgr.send_ctrl_c(session["id"])
            if ok:
                return True
            app = self._get_app_name(session)
            if app:
                return self.ide_ctrl.send_ctrl_c(app)
            return False

    async def _select_cmd(self, session: dict, option: int) -> bool:
        """Select an option number"""
        stype = session.get("session_type", "screen")
        if stype in ("ide", "terminal"):
            app = session.get("app_name") or ("Terminal" if stype == "terminal" else self._get_app_name(session))
            ok = self.ide_ctrl.send_text(app, str(option))
            if ok:
                self.ide_ctrl.send_enter(app)
            return ok
        else:
            return await self.screen_mgr.select_option(session["id"], option)

    async def _stop_cmd(self, session: dict) -> bool:
        """Stop a session"""
        stype = session.get("session_type", "screen")
        if stype in ("ide", "terminal"):
            app = session.get("app_name") or ("Terminal" if stype == "terminal" else self._get_app_name(session))
            self.ide_ctrl.send_ctrl_c(app)
            return True
        else:
            await self.screen_mgr.send_ctrl_c(session["id"])
            await asyncio.sleep(0.5)
            await self.screen_mgr.send_keys(session["id"], "exit")
            await asyncio.sleep(1)
            return await self.screen_mgr.kill(session["id"])

    # ── Handler: 命令 API ──────────────────────────────────
    async def handle_list_sessions(
        self, request: web.Request
    ) -> web.Response:
        """列出所有 session"""
        status_filter = request.query.get("status")
        sessions = self.registry.list(status_filter)
        counts = self.registry.count_by_status()
        return self._json({"sessions": sessions, "counts": counts})

    async def handle_get_session(
        self, request: web.Request
    ) -> web.Response:
        """获取单个 session 详情"""
        session_id = request.match_info["session_id"]
        session = self.registry.get(session_id)
        if not session:
            return self._json({"error": "session 不存在"}, status=404)

        stype = session.get("session_type", "screen")
        app_name = session.get("app_name", "")

        if stype == "ide":
            # IDE session: 通过 Accessibility API 全选→复制读取终端内容
            try:
                output, line_count = self.ide_ctrl.read_output(app_name, 50)
                session["live_output"] = output
                session["live_line_count"] = line_count
            except Exception as e:
                logger.warning("Failed to read IDE terminal output: %s", e)
                session["live_output"] = ""
                session["live_line_count"] = 0
            session["detected_status"] = session.get("status", "running")
        else:
            # Screen/terminal session: 读取 log 尾部
            output, line_count = await self.screen_mgr.read_output(session_id, 50)
            session["live_output"] = output
            session["live_line_count"] = line_count
            detected = await self.screen_mgr.detect_status(
                session_id,
                pid=session.get("pid"),
                last_update=session.get("updated_at"),
            )
            session["detected_status"] = detected

        return self._json({"session": session})

    async def handle_send_command(
        self, request: web.Request
    ) -> web.Response:
        """向 session 发送命令"""
        session_id = request.match_info["session_id"]

        try:
            data = await request.json()
        except Exception:
            return self._json({"error": "无效的 JSON body"}, status=400)

        text = data.get("text", "")
        if not text:
            return self._json({"error": "缺少 text 字段"}, status=400)

        session = self.registry.get(session_id)
        if not session:
            return self._json({"error": "session 不存在"}, status=404)

        ok = await self._send_cmd(session, text)
        if ok:
            self.registry.update(session_id, status="running")
            return self._json({"ok": True, "sent": text})
        else:
            return self._json({"error": "session 不可用"}, status=410)

    async def handle_confirm(
        self, request: web.Request
    ) -> web.Response:
        """发送回车确认"""
        session_id = request.match_info["session_id"]
        session = self.registry.get(session_id)
        if not session:
            return self._json({"error": "session 不存在"}, status=404)

        ok = await self._confirm_cmd(session)
        if ok:
            self.registry.update(session_id, status="running")
            return self._json({"ok": True})
        else:
            return self._json({"error": "session 不可用"}, status=410)

    async def handle_select(
        self, request: web.Request
    ) -> web.Response:
        """选择数字选项"""
        session_id = request.match_info["session_id"]

        try:
            data = await request.json()
            option = int(data.get("option", 0))
        except Exception:
            return self._json({"error": "需要 option 字段（数字）"}, status=400)

        session = self.registry.get(session_id)
        if not session:
            return self._json({"error": "session 不存在"}, status=404)

        ok = await self._select_cmd(session, option)
        if ok:
            self.registry.update(session_id, status="running")
            return self._json({"ok": True, "option": option})
        else:
            return self._json({"error": "session 不可用"}, status=410)

    async def handle_interrupt(
        self, request: web.Request
    ) -> web.Response:
        """发送 Ctrl+C"""
        session_id = request.match_info["session_id"]
        session = self.registry.get(session_id)
        if not session:
            return self._json({"error": "session 不存在"}, status=404)

        ok = await self._interrupt_cmd(session)
        if ok:
            self.registry.update(session_id, status="running")
            return self._json({"ok": True})
        else:
            return self._json({"error": "session 不可用"}, status=410)

    async def handle_stop(
        self, request: web.Request
    ) -> web.Response:
        """停止 session"""
        session_id = request.match_info["session_id"]
        session = self.registry.get(session_id)
        if not session:
            return self._json({"error": "session 不存在"}, status=404)

        ok = await self._stop_cmd(session)
        self.registry.update(session_id, status="stopped")
        return self._json({"ok": ok})

    # ── Handler: Lark webhook ──────────────────────────────
    async def handle_lark_webhook(
        self, request: web.Request
    ) -> web.Response:
        """处理 Lark 事件回调"""
        try:
            body = await request.json()
        except Exception:
            return self._json({"error": "无效的 JSON"}, status=400)

        result = await self.lark_bot.handle_webhook(body)
        if result is not None:
            return self._json(result)
        return self._json({"code": 0})

    # ── Handler: 健康检查 ──────────────────────────────────
    async def handle_health(
        self, _request: web.Request
    ) -> web.Response:
        """健康检查端点"""
        uptime = time.time() - getattr(self, "_start_time", time.time())
        return self._json(
            {
                "status": "ok",
                "sessions": self.registry.count_by_status(),
                "uptime": uptime,
            }
        )

    # ── 后台任务 ────────────────────────────────────────────
    async def health_check_loop(self) -> None:
        """定时健康检查：清理孤儿 session、更新状态

        每 5 秒检查一次：
        1. 对每个 running/waiting 的 session 检测 screen 是否存活
        2. 读取最新输出、检测等待状态
        3. 清理已死的 session
        """
        logger.info("Health check loop started (interval: %.1fs)", config.health_check_interval)

        while self._running:
            try:
                await self._run_health_check()
            except Exception as e:
                logger.error("Health check failed: %s", e)

            await asyncio.sleep(config.health_check_interval)

    async def _run_health_check(self) -> None:
        """Run a single health check cycle"""
        sessions = self.registry.list(status_filter="running")
        sessions += self.registry.list(status_filter="waiting")
        sessions += self.registry.list(status_filter="idle")

        now = time.time()

        for s in sessions:
            session_id = s["id"]
            stype = s.get("session_type", "screen")

            # For all non-screen types (ide, terminal, standalone),
            # check process health via kill -0 and detect waiting state
            if stype in ("ide", "terminal", "standalone"):
                updated_at = s.get("updated_at", 0)
                elaped = now - updated_at
                pid = s.get("pid", 0)
                liv = True
                if pid:
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            "kill", "-0", str(pid),
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        await proc.wait()
                        liv = proc.returncode == 0
                    except Exception:
                        liv = False

                if not liv:
                    self.registry.update(session_id, status="stopped")
                    logger.info(
                        "Session %s stopped (process dead)", session_id[:8]
                    )
                    continue

                # Only mark as stopped if no heartbeat for very long (>120s)
                # This lets the heartbeat daemon keep it alive
                if elaped > 120 and not pid:
                    self.registry.update(session_id, status="stopped")
                    logger.info(
                        "Session %s stopped (no heartbeat for %.0fs)",
                        session_id[:8], elaped,
                    )
                    continue

                # For terminal sessions: try log file first, then AppleScript Terminal reading
                if stype == "terminal":
                    output, _ = await self.screen_mgr.read_output(session_id, 10)

                    # If log file doesn't exist (session not started via lcc),
                    # read Terminal window content directly via AppleScript
                    if not output:
                        try:
                            output, _ = self.ide_ctrl.read_terminal_output(15)
                        except Exception:
                            pass

                    if output:
                        self.registry.update(
                            session_id,
                            last_output=output[-300:] if len(output) > 300 else output,
                        )

                    log_status = ScreenManager._looks_waiting(output) if output else False
                    if log_status:
                        status = "waiting"
                    elif not output:
                        status = "running"
                    else:
                        if pid:
                            try:
                                proc = await asyncio.create_subprocess_exec(
                                    "ps", "-p", str(pid), "-o", "state=",
                                    stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.DEVNULL,
                                )
                                stdout, _ = await proc.communicate()
                                state = stdout.decode().strip()
                                if state == "S":
                                    status = "idle"
                                elif state in ("R", "D"):
                                    status = "running"
                                else:
                                    status = "running"
                            except Exception:
                                status = "running"
                        else:
                            status = "running"

                    if status != s.get("status"):
                        logger.info(
                            "Session %s status: %s -> %s",
                            session_id[:8], s.get("status"), status,
                        )
                        self.registry.update(session_id, status=status)
                    self.registry.update(session_id, updated_at=now)
                else:
                    # ide/standalone: just keep alive via heartbeat freshness
                    self.registry.update(session_id, updated_at=now)

                continue

            # Screen session: check screen alive
            liv = await self.screen_mgr.is_alive(session_id)

            if not liv:
                self.registry.update(session_id, status="stopped")
                logger.info(
                    "Session %s stopped (screen no longer alive)",
                    session_id[:8],
                )
                continue

            # Read latest output and detect status
            output, _ = await self.screen_mgr.read_output(session_id, 10)
            status = await self.screen_mgr.detect_status(
                session_id,
                pid=s.get("pid"),
                last_update=s.get("updated_at"),
            )
            self.registry.update(
                session_id,
                status=status,
                last_output=output[-300:] if len(output) > 300 else output,
            )

    # ── 启动 / 关闭 ────────────────────────────────────────
    async def event_consume_loop(self) -> None:
        """通过 lark-cli event consume 实时接收飞书消息

        替代 webhook 方案，使用长连接事件消费模式。
        lark-cli event consume 输出说明：
        - stderr: [event] 日志 + NDJSON 格式的消息事件
        - stdout: 通常为空
        - 必须保持 stdin 打开，否则 consume 进程立即退出
        """
        logger.info("Starting Lark event consume loop...")

        while self._running:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "lark-cli", "event", "consume",
                    "im.message.receive_v1",
                    "--as", "bot",
                    "--timeout", "0",  # 无限等待
                    stdin=asyncio.subprocess.PIPE,  # 必须保持打开，否则 consume 因 stdin EOF 退出
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                logger.info("Lark event consume started (PID: %d)", proc.pid)

                # 事件输出到 stdout（NDJSON），[event] 日志在 stderr
                assert proc.stdout is not None
                while self._running and proc.returncode is None:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=600
                    )
                    if not line:
                        break

                    raw = line.decode("utf-8", errors="replace").strip()
                    if not raw:
                        continue

                    # 尝试解析 JSON — 消息事件
                    try:
                        event_data = json.loads(raw)
                        await self.lark_bot.handle_event_line(event_data)
                    except json.JSONDecodeError:
                        logger.debug("Skipping non-JSON: %s", raw[:80])
                    except Exception as e:
                        logger.error("Error handling event: %s", e)
                # 如果进程正常退出，等待退出码
                await proc.wait()
                logger.warning(
                    "Lark event consume exited (code: %d), restarting in 3s...",
                    proc.returncode,
                )

            except asyncio.TimeoutError:
                # 超时重置（keepalive 信号）
                continue
            except Exception as e:
                logger.error("Lark event consume error: %s", e)

            # 重启前等待
            await asyncio.sleep(3)

    async def start(self) -> None:
        """启动 daemon"""
        self._start_time = time.time()

        # 恢复 session（清理已死但 DB 中标记为 running 的）
        active_screens = await self.screen_mgr.list_sessions()
        recovered = self.registry.recover_sessions(set(active_screens))
        logger.info(
            "Recovered %d active sessions from %d active screens",
            len(recovered),
            len(active_screens),
        )

        # 启动健康检查
        self._health_task = asyncio.create_task(self.health_check_loop(), name="health-check")

        # 启动 HTTP 服务器
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", config.daemon_port)
        await site.start()

        # 启动事件消费（替代 webhook）
        self._event_task = asyncio.create_task(self.event_consume_loop(), name="lark-event")

        logger.info(
            "Daemon started on http://127.0.0.1:%d",
            config.daemon_port,
        )

        # 保持运行
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
            self._running = False
            if self._health_task:
                self._health_task.cancel()
            if self._event_task:
                self._event_task.cancel()

    # ── 工具方法 ──────────────────────────────────────────
    async def _with_lock(
        self, session_id: str, coro_fn, *args
    ) -> bool:
        """对同一个 session 的操作加锁，避免并发问题"""
        if session_id not in self._session_write_locks:
            self._session_write_locks[session_id] = asyncio.Lock()
        lock = self._session_write_locks[session_id]

        async with lock:
            return await coro_fn(session_id, *args)

    @staticmethod
    def _json(data: dict, status: int = 200) -> web.Response:
        return web.json_response(
            data, status=status,
            headers={"Access-Control-Allow-Origin": "*"},
        )


# ── 入口 ────────────────────────────────────────────────
def main() -> None:
    if sys.platform != "darwin":
        # 允许非 macOS 进行测试
        logger.warning("Not running on macOS, screen commands may fail")

    daemon = Daemon()

    # 处理 SIGTERM / SIGINT
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