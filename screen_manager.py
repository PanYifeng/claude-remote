"""Screen 会话管理 — 封装所有 screen 命令操作"""

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
    """管理 screen 会话的生命周期和交互"""

    SCREEN_PREFIX = "claude-"

    async def create(
        self,
        session_id: str,
        cwd: str = "",
        *,
        log_path: Optional[str] = None,
        command: Optional[str] = None,
    ) -> int:
        """创建新的 detached screen session 并在其中启动 claude

        Args:
            session_id: Session UUID
            cwd: 工作目录（默认为当前目录）
            log_path: 日志文件路径（默认 /tmp/claude-<id>.log）
            command: 要执行的命令（默认 claude）

        Returns:
            进程 PID（可能为 0 如果获取失败）
        """
        screen_name = self._screen_name(session_id)
        log_file = log_path or f"/tmp/claude-{session_id}.log"
        cwd = cwd or os.getcwd()
        cmd = command or "claude"

        # 确保日志目录存在
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)

        # 清空旧日志
        open(log_file, "w").close()

        # 启动 screen: 用 script 捕获输出，以便 tail 读取
        # macOS script 语法: script -q -a <logfile> <command>
        shell_cmd = f"cd {self._escape(cwd)} && script -q -a {self._escape(log_file)} {cmd}"
        proc = await asyncio.create_subprocess_exec(
            "screen",
            "-dmS",
            screen_name,
            "sh",
            "-c",
            shell_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

        # 等待 screen 启动并获取 PID
        await asyncio.sleep(0.3)
        pid = await self._get_pid(screen_name)
        logger.info("Screen session created: %s (PID: %s)", screen_name, pid)
        return pid

    async def send_keys(self, session_id: str, text: str) -> bool:
        """向 session 发送文本（自动追加回车）

        Args:
            session_id: Session UUID
            text: 要发送的文本

        Returns:
            是否成功发送
        """
        screen_name = self._screen_name(session_id)
        if not await self.is_alive(session_id):
            logger.warning("Session not alive, cannot send keys: %s", session_id)
            return False

        # 使用 stuff 命令注入键盘输入
        # shell 特殊字符需要转义
        escaped = text.replace("'", "'\\''")
        proc = await asyncio.create_subprocess_exec(
            "screen",
            "-S",
            screen_name,
            "-X",
            "stuff",
            f"{text}\r",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        logger.info("Sent keys to %s: %s", session_id, text[:50])
        return True

    async def send_text(self, session_id: str, text: str) -> bool:
        """向 session 发送纯文本（不追加回车）"""
        return await self._stuff(session_id, text)

    async def send_enter(self, session_id: str) -> bool:
        """发送回车（确认操作）"""
        return await self._stuff(session_id, "\r")

    async def send_ctrl_c(self, session_id: str) -> bool:
        """发送 Ctrl+C 中断"""
        return await self._stuff(session_id, "\003")  # ^C

    async def send_ctrl_d(self, session_id: str) -> bool:
        """发送 Ctrl+D（EOF）"""
        return await self._stuff(session_id, "\004")

    async def select_option(self, session_id: str, option: int) -> bool:
        """选择数字选项"""
        return await self.send_keys(session_id, str(option))

    async def is_alive(self, session_id: str) -> bool:
        """检查 screen 会话是否存在且运行中

        macOS 自带 screen 版本较旧(4.00.03)，不支持 -Q 参数，
        改用 screen -list 并结合 session name 检查。

        Returns:
            True 如果 screen session 存在
        """
        screen_name = self._screen_name(session_id)
        sessions = await self.list_sessions()
        return screen_name in sessions

    async def kill(self, session_id: str) -> bool:
        """终止 screen 会话

        Returns:
            是否成功终止
        """
        screen_name = self._screen_name(session_id)
        proc = await asyncio.create_subprocess_exec(
            "screen",
            "-S",
            screen_name,
            "-X",
            "quit",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        logger.info("Killed session: %s", session_id)
        return True

    async def read_output(
        self, session_id: str, lines: int = 50
    ) -> tuple[str, int]:
        """读取 session 日志文件的尾部内容

        Args:
            session_id: Session UUID
            lines: 读取行数

        Returns:
            (输出文本, 总行数)
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
        """检测 session 的交互状态

        使用多重策略：
        1. 检查 screen 是否存活
        2. 读取最近输出匹配等待模式
        3. 检查进程状态

        Args:
            session_id: Session UUID
            pid: 进程 PID（可选）
            last_update: 上次更新时间戳（可选）

        Returns:
            'running' | 'waiting' | 'idle' | 'stopped'
        """
        # 1. 检查 screen 存活
        alive = await self.is_alive(session_id)
        if not alive:
            return "stopped"

        # 2. 读取尾部输出判断等待状态
        output, _ = await self.read_output(session_id, lines=20)
        if self._looks_waiting(output):
            return "waiting"

        # 3. 如果提供了 pid，检查进程 CPU 状态
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
                    # 休眠状态 + 有等待模式 = waiting
                    if self._looks_waiting(output):
                        return "waiting"
                    # 休眠 + 最近有输出 = 正在执行（等待外部操作）
                    if last_update and time.time() - last_update < 10:
                        return "running"
                    # 长时间休眠且无等待模式 = idle
                    return "idle"
                elif state in ("R", "D"):
                    return "running"
            except Exception:
                pass

        # 默认
        return "running"

    async def list_sessions(self) -> list[str]:
        """列出所有 claude-* 开头的 screen 会话

        Returns:
            screen 会话名列表
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
            # screen 输出格式: "  PID.ttysxxx.claude-xxxxx (Detached)"
            m = re.search(r"\.({}[^\s]+)".format(self.SCREEN_PREFIX), line)
            if m:
                sessions.append(m.group(1))
        return sessions

    def _screen_name(self, session_id: str) -> str:
        return f"{self.SCREEN_PREFIX}{session_id[:12]}"

    def _escape(self, s: str) -> str:
        """shell 转义（用于 sh -c 参数）"""
        escaped = s.replace("'", "'\\''")
        return f"'{escaped}'"

    async def _stuff(self, session_id: str, text: str) -> bool:
        """低级操作：向 screen 注入原始字符"""
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
        """获取 screen session 的 PID

        从 screen -ls 输出中解析 PID。
        screen 格式: "PID.TTY.HOSTNAME (Detached)"
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
                    # 提取 PID：行首数字
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
        """读取文件尾部 N 行"""
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

    @staticmethod
    def _looks_waiting(output: str) -> bool:
        """判断输出末尾是否呈现"等待输入"的模式

        检测各种 Claude 和 shell 的提示符模式。
        """
        if not output:
            return False

        last_lines = output.strip().splitlines()
        if not last_lines:
            return False

        last_line = last_lines[-1].strip().rstrip()

        # 等待确认模式
        waiting_patterns = [
            r"是否.*[？?]\s*$",
            r"❯\s*$",                        # Claude 提示符
            r"[？?]\s*\([Yy]/[Nn]\).*$",    # ？(Y/n) 结尾
            r"[？?]\s*$",                   # 问号结尾（等待回答）
            r"\$?\s*[Yy]/[Nn]\s*$",         # Y/n 选择
            r"[Cc]ontinue",                  # Continue / continue
            r"[Pp]roceed\s*[？?]?\s*$",     # Proceed / Proceed?
            r"[Ss]elect\s+",                # Select option
            r"[Cc]onfirm\s+",               # Confirm
            r">>>\s*$",                      # Python REPL
            r"请输入.*[:：]\s*$",             # 中文输入提示
            r"请选择.*[:：]\s*$",
            r"选择.*[:：]\s*$",
        ]
        for pat in waiting_patterns:
            if re.search(pat, last_line):
                return True

        # 检查最后是否以提示符结束（但不包含命令后面的输出）
        # Claude Code 在等待时最后一行通常是 "❯ " 或类似
        if last_line.endswith(("$", "#", "❯", ">", ":")):
            return True

        return False


# 单例
screen_manager = ScreenManager()