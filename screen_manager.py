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
    """Manage screen session lifecycle and interaction"""

    SCREEN_PREFIX = "claude-"

    async def create(
        self,
        session_id: str,
        cwd: str = "",
        *,
        log_path: Optional[str] = None,
        command: Optional[str] = None,
    ) -> int:
        """Create a new detached screen session running claude

        Args:
            session_id: Session UUID
            cwd: Working directory
            log_path: Log file path (default /tmp/claude-<id>.log)
            command: Command to run (default claude)

        Returns:
            Process PID (0 if unobtainable)
        """
        screen_name = self._screen_name(session_id)
        log_file = log_path or f"/tmp/claude-{session_id}.log"
        cwd = cwd or os.getcwd()
        cmd = command or "claude"

        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        open(log_file, "w").close()

        # macOS script syntax: script -q -a <logfile> <command>
        shell_cmd = f"cd {self._escape(cwd)} && script -q -a {self._escape(log_file)} {cmd}"
        proc = await asyncio.create_subprocess_exec(
            "screen", "-dmS", screen_name, "sh", "-c", shell_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

        await asyncio.sleep(0.3)
        pid = await self._get_pid(screen_name)
        logger.info("Screen session created: %s (PID: %s)", screen_name, pid)
        return pid

    async def send_keys(self, session_id: str, text: str) -> bool:
        """Send text + Enter to session"""
        screen_name = self._screen_name(session_id)
        if not await self.is_alive(session_id):
            logger.warning("Session not alive, cannot send keys: %s", session_id)
            return False

        escaped = text.replace("'", "'\\''")
        proc = await asyncio.create_subprocess_exec(
            "screen", "-S", screen_name, "-X", "stuff", f"{text}\r",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        logger.info("Sent keys to %s: %s", session_id, text[:50])
        return True

    async def send_text(self, session_id: str, text: str) -> bool:
        """Send plain text (no Enter)"""
        return await self._stuff(session_id, text)

    async def send_enter(self, session_id: str) -> bool:
        """Send Enter key"""
        return await self._stuff(session_id, "\r")

    async def send_ctrl_c(self, session_id: str) -> bool:
        """Send Ctrl+C"""
        return await self._stuff(session_id, "\003")

    async def send_ctrl_d(self, session_id: str) -> bool:
        """Send Ctrl+D (EOF)"""
        return await self._stuff(session_id, "\004")

    async def select_option(self, session_id: str, option: int) -> bool:
        """Select a numeric option"""
        return await self.send_keys(session_id, str(option))

    async def is_alive(self, session_id: str) -> bool:
        """Check if screen session exists

        macOS screen (4.00.03) doesn't support -Q flag,
        so we use screen -list and match session name.

        Returns:
            True if screen session exists
        """
        screen_name = self._screen_name(session_id)
        sessions = await self.list_sessions()
        return screen_name in sessions

    async def kill(self, session_id: str) -> bool:
        """Terminate screen session"""
        screen_name = self._screen_name(session_id)
        proc = await asyncio.create_subprocess_exec(
            "screen", "-S", screen_name, "-X", "quit",
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
        1. Check if screen is alive
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
        """List all claude-* screen sessions

        Returns:
            List of screen session names
        """
        proc = await asyncio.create_subprocess_exec(
            "screen", "-ls",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        result = stdout.decode()

        sessions = []
        for line in result.splitlines():
            m = re.search(r"\.({}[^\s]+)".format(self.SCREEN_PREFIX), line)
            if m:
                sessions.append(m.group(1))
        return sessions

    def _screen_name(self, session_id: str) -> str:
        return f"{self.SCREEN_PREFIX}{session_id[:12]}"

    def _escape(self, s: str) -> str:
        """Shell escape for sh -c arguments"""
        escaped = s.replace("'", "'\\''")
        return f"'{escaped}'"

    async def _stuff(self, session_id: str, text: str) -> bool:
        """Low-level operation: inject raw characters into screen"""
        screen_name = self._screen_name(session_id)
        if not await self.is_alive(session_id):
            logger.warning("Session not alive: %s", session_id)
            return False
        proc = await asyncio.create_subprocess_exec(
            "screen", "-S", screen_name, "-X", "stuff", text,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return True

    async def _get_pid(self, screen_name: str) -> int:
        """Get screen session PID from screen -ls output

        screen format: "PID.TTY.HOSTNAME (Detached)"
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "screen", "-ls",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate(timeout=3)
            result = stdout.decode()
            for line in result.splitlines():
                if screen_name in line:
                    m = re.match(r"\s*(\d+)", line)
                    if m:
                        return int(m.group(1))
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
        """Detect status of a standalone terminal session (no screen)

        Uses process health and log file output analysis instead of screen.

        Returns:
            'running' | 'waiting' | 'idle' | 'stopped'
        """
        # Check if process is alive
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

        # Read output from log file if available
        log_path = log_path or f"/tmp/claude-{session_id}.log"
        output, _ = await self._read_tail(log_path, lines=15)

        if self._looks_waiting(output):
            return "waiting"

        # Check process state via ps
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
        """Check if output ends with a waiting-for-input pattern

        Detects various Claude and shell prompt patterns.
        """
        if not output:
            return False

        last_lines = output.strip().splitlines()
        if not last_lines:
            return False

        last_line = last_lines[-1].strip().rstrip()

        waiting_patterns = [
            r"^.*[？?]\s*$",
            r"❯\s*$",
            r"[？?]\s*\([Yy]/[Nn]\).*$",
            r"\$?\s*[Yy]/[Nn]\s*$",
            r"[Cc]ontinue",
            r"[Pp]roceed\s*[？?]?\s*$",
            r"[Ss]elect\s+",
            r"[Cc]onfirm\s+",
            r">>>\s*$",
        ]
        for pat in waiting_patterns:
            if re.search(pat, last_line):
                return True

        if last_line.endswith(("$", "#", "❯", ">", ":")):
            return True

        return False


screen_manager = ScreenManager()