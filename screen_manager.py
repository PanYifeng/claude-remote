import asyncio
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("screen_manager")


class ScreenManager:
    """Manage tmux session lifecycle and interaction

    Uses tmux instead of macOS's built-in screen (v4.00.03) because
    screen's 'stuff' command is broken on macOS — it returns success
    but never actually injects input.
    """

    SESSION_PREFIX = "claude-"

    async def create(
        self,
        session_id: str,
        cwd: str = "",
        *,
        log_path: Optional[str] = None,
        command: Optional[str] = None,
    ) -> int:
        """Create a new detached tmux session running claude

        Args:
            session_id: Session UUID
            cwd: Working directory
            log_path: Log file path (default /tmp/claude-<id>.log)
            command: Command to run (default claude)

        Returns:
            Process PID (0 if unobtainable)
        """
        session_name = self._session_name(session_id)
        log_file = log_path or f"/tmp/claude-{session_id}.log"
        cwd = cwd or os.getcwd()
        cmd = command or "claude"

        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        open(log_file, "w").close()

        # Use script to capture output, tmux to manage the session
        shell_cmd = f"cd {self._escape(cwd)} && script -q -a -t 0 {self._escape(log_file)} {cmd}"
        proc = await asyncio.create_subprocess_exec(
            "tmux", "new-session", "-d", "-s", session_name,
            shell_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

        await asyncio.sleep(0.3)
        pid = await self._get_pid(session_name)
        logger.info("Tmux session created: %s (PID: %s)", session_name, pid)
        return pid

    async def send_keys(self, session_id: str, text: str) -> bool:
        """Send text + Enter to session"""
        session_name = self._session_name(session_id)
        if not await self.is_alive(session_id):
            logger.warning("Session not alive, cannot send keys: %s", session_id)
            return False

        proc = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", session_name, text, "Enter",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        logger.info("Sent keys to %s: %s", session_id, text[:50])
        return True

    async def send_text(self, session_id: str, text: str) -> bool:
        """Send plain text (no Enter)"""
        session_name = self._session_name(session_id)
        if not await self.is_alive(session_id):
            return False
        proc = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", session_name, text,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return True

    async def send_enter(self, session_id: str) -> bool:
        """Send Enter key"""
        return await self._send_key(session_id, "Enter")

    async def send_ctrl_c(self, session_id: str) -> bool:
        """Send Ctrl+C"""
        return await self._send_key(session_id, "C-c")

    async def send_ctrl_d(self, session_id: str) -> bool:
        """Send Ctrl+D (EOF)"""
        return await self._send_key(session_id, "C-d")

    async def select_option(self, session_id: str, option: int) -> bool:
        """Select a numeric option"""
        return await self.send_keys(session_id, str(option))

    async def is_alive(self, session_id: str) -> bool:
        """Check if tmux session exists"""
        session_name = self._session_name(session_id)
        sessions = await self.list_sessions()
        return session_name in sessions

    async def kill(self, session_id: str) -> bool:
        """Terminate tmux session"""
        session_name = self._session_name(session_id)
        proc = await asyncio.create_subprocess_exec(
            "tmux", "kill-session", "-t", session_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        logger.info("Killed session: %s", session_id)
        return True

    async def read_output(
        self, session_id: str, lines: int = 50
    ) -> tuple[str, int]:
        """Read tail of session log file

        Args:
            session_id: Session UUID
            lines: Number of lines to read

        Returns:
            (output_text, total_lines)
        """
        log_path = f"/tmp/claude-{session_id}.log"
        return await self._read_tail(log_path, lines)

    async def detect_status(
        self,
        session_id: str,
        *,
        pid: Optional[int] = None,
        last_update: Optional[float] = None,
    ) -> str:
        """Detect session interaction status

        Multi-strategy detection:
        1. Check if tmux session is alive
        2. Match waiting patterns in recent output
        3. Check process CPU state if PID provided

        Returns:
            'running' | 'waiting' | 'idle' | 'stopped'
        """
        alive = await self.is_alive(session_id)
        if not alive:
            return "stopped"

        output, _ = await self.read_output(session_id, lines=20)
        if self._looks_waiting(output):
            return "waiting"

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
                    if self._looks_waiting(output):
                        return "waiting"
                    if last_update and time.time() - last_update < 10:
                        return "running"
                    return "idle"
                elif state in ("R", "D"):
                    return "running"
            except Exception:
                pass

        return "running"

    async def list_sessions(self) -> list[str]:
        """List all claude-* tmux sessions

        Returns:
            List of tmux session names
        """
        proc = await asyncio.create_subprocess_exec(
            "tmux", "list-sessions", "-F", "#{session_name}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        result = stdout.decode()

        sessions = []
        for line in result.splitlines():
            line = line.strip()
            if line.startswith(self.SESSION_PREFIX):
                sessions.append(line)
        return sessions

    def _session_name(self, session_id: str) -> str:
        return f"{self.SESSION_PREFIX}{session_id[:12]}"

    def _escape(self, s: str) -> str:
        """Shell escape for sh -c arguments"""
        escaped = s.replace("'", "'\\''")
        return f"'{escaped}'"

    async def _send_key(self, session_id: str, key: str) -> bool:
        """Send a special key (Enter, C-c, C-d) to tmux session"""
        session_name = self._session_name(session_id)
        if not await self.is_alive(session_id):
            logger.warning("Session not alive: %s", session_id)
            return False
        proc = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", session_name, key,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return True

    async def _get_pid(self, session_name: str) -> int:
        """Get PID of the claude process inside the tmux session"""
        try:
            # Get the pane PID (the script process)
            proc = await asyncio.create_subprocess_exec(
                "tmux", "list-panes", "-t", session_name, "-F", "#{pane_pid}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate(timeout=3)
            pane_pid = stdout.decode().strip()
            if pane_pid and pane_pid.isdigit():
                pid = int(pane_pid)
                # The pane PID is the `script` process (child of sh)
                # The claude process is a child of script
                # Return the pane PID for health check purposes
                if pid > 0:
                    return pid
        except Exception:
            pass
        return 0

    @staticmethod
    async def _read_tail(
        path: str, lines: int = 50
    ) -> tuple[str, int]:
        """Read tail N lines from a file"""
        try:
            if not os.path.exists(path):
                return "", 0
            proc = await asyncio.create_subprocess_exec(
                "tail", "-n", str(lines), path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate(timeout=5)
            text = stdout.decode("utf-8", errors="replace")
            line_count = len(text.splitlines())
            return text, line_count
        except Exception as e:
            logger.warning("Failed to read tail %s: %s", path, e)
            return "", 0

    async def detect_terminal_status(
        self,
        session_id: str,
        *,
        pid: Optional[int] = None,
        last_update: Optional[float] = None,
        log_path: Optional[str] = None,
    ) -> str:
        """Detect status of a standalone terminal session (no tmux)

        Uses process health and log file output analysis instead of tmux.

        Returns:
            'running' | 'waiting' | 'idle' | 'stopped'
        """
        if pid:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "kill", "-0", str(pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                if proc.returncode != 0:
                    return "stopped"
            except Exception:
                return "stopped"
        else:
            return "stopped"

        log_path = log_path or f"/tmp/claude-{session_id}.log"
        output, _ = await self._read_tail(log_path, lines=15)

        if self._looks_waiting(output):
            return "waiting"

        try:
            proc = await asyncio.create_subprocess_exec(
                "ps", "-p", str(pid), "-o", "state=",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            state = stdout.decode().strip()
            if state == "S":
                if self._looks_waiting(output):
                    return "waiting"
                if last_update and time.time() - last_update < 10:
                    return "running"
                return "idle"
            elif state in ("R", "D"):
                return "running"
        except Exception:
            pass

        return "running"

    @staticmethod
    def _looks_waiting(output: str) -> bool:
        """Check if output contains a waiting-for-input pattern"""
        if not output:
            return False

        lines = output.strip().splitlines()
        if not lines:
            return False

        waiting_patterns = [
            r"[？?]\s*\([Yy]/[Nn]\)",
            r"\$?\s*[Yy]/[Nn]\s*$",
            r"[Cc]ontinue",
            r"[Pp]roceed\s*[？?]?\s*$",
            r"requires approval",
            r"[Aa]pprov",
            r"[Ss]elect\s+",
            r"[Cc]onfirm\s+",
            r"Do you want to proceed\?",
            r"Enter to confirm",
            r"Esc to cancel",
            r">>>\s*$",
        ]

        for line in lines:
            stripped = line.strip().rstrip()
            for pat in waiting_patterns:
                if re.search(pat, stripped):
                    return True

        last_line = lines[-1].strip().rstrip()
        if last_line.endswith(("$", "#", "❯", ">", ":")):
            return True
        if re.search(r"❯\s*$", last_line):
            return True
        if re.search(r"^.*[？?]\s*$", last_line):
            return True

        return False


screen_manager = ScreenManager()