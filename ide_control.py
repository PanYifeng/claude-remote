"""IDE terminal control — Control IntelliJ/PyCharm/VS Code terminals via macOS Accessibility API

Provides:
- find_ide_terminal(): Locate matching IDE terminal windows
- send_keys(): Send text + Enter to terminal
- send_text(): Send plain text
- send_enter(): Send Enter
- send_ctrl_c(): Send Ctrl+C
- list_terminals(): List all controllable IDE terminals
- read_output(): Read terminal output via Select All -> Copy -> Clipboard

Requires: System Settings -> Privacy & Security -> Accessibility -> authorize python3
"""

import json
import logging
import os
import re
import subprocess
from typing import Optional

logger = logging.getLogger("ide_control")


def read_claude_session_state(pid: int) -> Optional[dict]:
    """读取 Claude Code 的 per-process 状态文件 ~/.claude/sessions/<pid>.json

    Claude Code 会把每个进程的当前状态写到这个文件，包含：
    - sessionId: 会话 ID（对应 transcript 文件名，精确匹配，不会张冠李戴）
    - status: 实时状态（busy / shell / idle / 等，比抓屏可靠）
    - cwd, startedAt, name, version 等

    Args:
        pid: claude 进程 pid

    Returns:
        状态 dict，或 None（文件不存在 / 读失败）
    """
    if not pid:
        return None
    p = os.path.join(os.path.expanduser("~"), ".claude", "sessions", f"{pid}.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# Claude Code 的 status 字段 → 我们的会话状态
CLAUDE_STATUS_MAP = {
    "busy": "executing",
    "shell": "idle",
    "idle": "idle",
    "waiting": "waiting",
    "waiting_for_input": "waiting",
    "input_required": "waiting",
    "prompt": "waiting",
}


def map_claude_status(claude_status: str) -> Optional[str]:
    """把 Claude Code 的 status 映射为我们的状态，未知返回 None"""
    if not claude_status:
        return None
    return CLAUDE_STATUS_MAP.get(claude_status)


def session_tty(session: dict) -> str:
    """从 session 记录中提取 TTY 设备名（用于按 TTY 精确定位 Terminal.app 的 tab）

    session 的 tags 可能是 dict（register 返回）或 JSON 字符串（get/list 返回），
    此函数统一处理。返回如 "ttys005"；无则空串。
    """
    tags = session.get("tags", {})
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except (json.JSONDecodeError, ValueError):
            return ""
    if isinstance(tags, dict):
        return tags.get("tty", "") or ""
    return ""


def _read_last_assistant_from_file(fp: str, max_chars: int) -> tuple[str, str]:
    """从单个 transcript .jsonl 读取最后一条 assistant 文本消息

    Returns: (text, timestamp_iso)
    """
    try:
        r = subprocess.run(
            ["tail", "-n", "500", fp],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return "", ""
    last_ts = ""
    last_txt = ""
    for ln in r.stdout.splitlines():
        try:
            o = json.loads(ln)
        except Exception:
            continue
        msg = o.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        txt = ""
        if isinstance(content, str):
            txt = content
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    txt += c.get("text", "")
        if txt.strip():
            last_ts = o.get("timestamp", "")
            last_txt = txt
    if not last_txt:
        return "", ""
    return last_txt[:max_chars], last_ts


def read_last_assistant_message(cwd: str, session_id: str = "", max_chars: int = 1200) -> tuple[str, str]:
    """从 Claude Code 本地 transcript 读取最近一条 assistant 文本消息

    - 传入 session_id 时：精确读取该会话的 transcript 文件（推荐，配合
      read_claude_session_state 拿到的 sessionId 使用，不会张冠李戴）。
    - 不传 session_id 时：扫描项目目录所有 .jsonl，取"最近一条 assistant
      消息时间戳最大"的文件（启发式，多会话同项目时可能选错，仅作回退）。

    Args:
        cwd: claude 进程工作目录
        session_id: 精确会话 ID（可选）
        max_chars: 最多返回字符数

    Returns:
        (text, timestamp_iso)，失败返回 ("", "")
    """
    if not cwd:
        return "", ""
    home = os.path.expanduser("~")
    encoded = cwd.replace("/", "-")
    proj_dir = os.path.join(home, ".claude", "projects", encoded)
    if not os.path.isdir(proj_dir):
        return "", ""

    if session_id:
        fp = os.path.join(proj_dir, f"{session_id}.jsonl")
        return _read_last_assistant_from_file(fp, max_chars)

    # 启发式回退：扫描所有 .jsonl，取 last-assistant-timestamp 最大的
    best_ts = ""
    best_txt = ""
    try:
        files = [f for f in os.listdir(proj_dir) if f.endswith(".jsonl")]
    except Exception:
        return "", ""
    for fn in files:
        fp = os.path.join(proj_dir, fn)
        txt, ts = _read_last_assistant_from_file(fp, max_chars)
        if ts and ts > best_ts:
            best_ts = ts
            best_txt = txt
    return best_txt, best_ts

# Known IDE process names on macOS
IDE_PROCESS_NAMES = {
    "IntelliJ IDEA": "idea",
    "PyCharm": "pycharm",
    "WebStorm": "webstorm",
    "VS Code": "Code",
    "Cursor": "Cursor",
    "Windsurf": "Windsurf",
    "Terminal": "Terminal",
    "iTerm2": "iTerm2",
}


class IDEControl:
    """IDE terminal operations wrapper"""

    def __init__(self):
        self._cached_windows: list[dict] = []
        self._cache_time: float = 0

    def find_ide_terminal(self, keyword: str = "") -> list[dict]:
        """Find IDE terminal windows running Claude Code

        Uses AppleScript to list all windows via System Events,
        filters by IDE process names and optional keyword.

        Args:
            keyword: Optional keyword to filter window titles

        Returns:
            List of window info dicts: {app, pid, title, win_id}
        """
        script = """
        tell application "System Events"
            set results to {}
            set appList to every process whose background only is false
            repeat with proc in appList
                set procName to name of proc
                set procPID to unix id of proc
                try
                    set winList to every window of proc
                    repeat with win in winList
                        set winTitle to title of win
                        set end of results to {app:procName, pid:procPID, title:winTitle, winID:id of win}
                    end repeat
                end try
            end repeat
            return results
        end tell
        """
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=15,
            )
            windows = self._parse_ascript_records(result.stdout)
        except subprocess.TimeoutExpired:
            logger.warning("AppleScript timed out listing windows")
            return []
        except Exception as e:
            logger.error("Failed to list windows: %s", e)
            return []

        ide_names = set(IDE_PROCESS_NAMES.values())
        matched = []
        for w in windows:
            proc = w.get("app", "")
            title = w.get("title", "")

            if proc not in ide_names and proc.lower() not in {n.lower() for n in ide_names}:
                continue

            if keyword and keyword.lower() not in title.lower():
                continue

            matched.append(w)

        self._cached_windows = matched
        return matched

    def register_session(self, session_id: str, app_name: str,
                          win_title: str, pid: int) -> bool:
        """Verify IDE session window exists via AppleScript"""
        windows = self.find_ide_terminal()
        for w in windows:
            if w["app"].lower() == app_name.lower():
                logger.info("IDE terminal found: %s (PID: %s)", app_name, pid)
                return True
        logger.warning("IDE terminal %s not found", app_name)
        return False

    def send_keys(self, app_name: str, text: str) -> bool:
        """Send text + Enter to IDE terminal (clipboard paste, supports CJK)

        Args:
            app_name: Process name (e.g. "IntelliJ IDEA", "Code")
            text: Text to send (auto-appends Enter)

        Returns:
            True if successful
        """
        script = f"""
        tell application "{app_name}"
            activate
        end tell
        delay 0.15
        tell application "System Events"
            tell process "{app_name}"
                set frontmost to true
                delay 0.1
                keystroke "v" using command down
                keystroke return
            end tell
        end tell
        """

        def paste() -> bool:
            try:
                proc = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True, text=True, timeout=5,
                )
                if proc.returncode != 0:
                    logger.warning("send_keys failed: %s", proc.stderr.strip())
                    return False
                return True
            except Exception as e:
                logger.error("send_keys error: %s", e)
                return False

        return self._with_clipboard_text(text, paste)

    def send_text(self, app_name: str, text: str) -> bool:
        """Send plain text, no Enter appended (clipboard paste for CJK support)"""
        script = f"""
        tell application "{app_name}"
            activate
        end tell
        delay 0.15
        tell application "System Events"
            tell process "{app_name}"
                set frontmost to true
                delay 0.1
                keystroke "v" using command down
            end tell
        end tell
        """

        def paste() -> bool:
            try:
                subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True, text=True, timeout=5,
                )
                return True
            except Exception:
                return False

        return self._with_clipboard_text(text, paste)

    def send_enter(self, app_name: str) -> bool:
        """Send Enter key"""
        script = f"""
        tell application "{app_name}"
            activate
        end tell
        delay 0.1
        tell application "System Events"
            tell process "{app_name}"
                keystroke return
            end tell
        end tell
        """
        try:
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=5,
            )
            return True
        except Exception:
            return False

    def send_ctrl_c(self, app_name: str) -> bool:
        """Send Ctrl+C (SIGINT, 中断前台进程)"""
        script = f"""
        tell application "{app_name}"
            activate
        end tell
        delay 0.1
        tell application "System Events"
            tell process "{app_name}"
                key code 8 using control down
            end tell
        end tell
        """
        try:
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=5,
            )
            return True
        except Exception:
            return False

    def _send_to_terminal_tty(self, tty: str, keystroke_lines: str) -> bool:
        """向 Terminal.app 中匹配 tty 的 tab 发送击键（单次 osascript）

        复用 read_terminal_by_tty 的 tab 定位循环：找到匹配 tab 后选定该 tab、把
        所在窗口提到最前、激活 Terminal.app，再执行击键——确保击键落到目标会话
        而非任意最前面的 tab。找不到匹配 tab 时不发送任何击键（返回 False），避免
        误发到其它会话。单次 osascript 是必须的：分次调用会在调用间丢焦点。

        Args:
            tty: TTY 设备名（如 "ttys005"）
            keystroke_lines: 击键 AppleScript 片段，在 'tell process "Terminal"'
                块内执行，如 'keystroke "ls"' 'keystroke return'

        Returns:
            True 已发送；False tab 未找到或发送失败
        """
        if not tty:
            return False
        tty_esc = tty.replace('"', '\\"')
        script = f"""
        set targetTTY to "{tty_esc}"
        set didFind to false
        tell application "Terminal"
            set wCount to count of windows
            repeat with wi from 1 to wCount
                try
                    set w to window wi
                    set tCount to count of tabs of w
                    repeat with ti from 1 to tCount
                        set theTab to tab ti of w
                        set tabTTY to tty of theTab
                        if tabTTY starts with "/dev/" then
                            set tabTTY to text 6 thru -1 of tabTTY
                        end if
                        if tabTTY is targetTTY then
                            set selected tab of w to theTab
                            set didFind to true
                            try
                                set index of w to 1
                            end try
                            exit repeat
                        end if
                    end repeat
                end try
                if didFind then exit repeat
            end repeat
        end tell
        if not didFind then
            return "CCR_TAB_NOT_FOUND"
        end if
        delay 0.25
        tell application "Terminal" to activate
        delay 0.15
        tell application "System Events"
            tell process "Terminal"
                set frontmost to true
                delay 0.1
                {keystroke_lines}
            end tell
        end tell
        return "CCR_OK"
        """
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=8,
            )
        except Exception as e:
            logger.warning("send_to_terminal_tty(%s) failed: %s", tty, e)
            return False
        if result.returncode != 0:
            logger.warning("send_to_terminal_tty(%s) osascript error: %s", tty, result.stderr.strip())
            return False
        if result.stdout.strip() == "CCR_TAB_NOT_FOUND":
            logger.warning("send_to_terminal_tty(%s): tab not found", tty)
            return False
        return True

    def send_keys_to_tty(self, tty: str, text: str) -> bool:
        """向 tty 对应的 Terminal tab 发送文本 + 回车

        走剪贴板 Cmd+V 粘贴：keystroke 无法输入中文等多字节字符，会打成乱码，
        必须用剪贴板绕过。
        """
        paste = "delay 0.1\n            keystroke \"v\" using command down\n            keystroke return"
        return self._with_clipboard_text(text, lambda: self._send_to_terminal_tty(tty, paste))

    def send_enter_to_tty(self, tty: str) -> bool:
        """向 tty 对应的 Terminal tab 发送回车"""
        return self._send_to_terminal_tty(tty, "keystroke return")

    def send_ctrl_c_to_tty(self, tty: str) -> bool:
        """向 tty 对应的 Terminal tab 发送 Ctrl+C（中断，SIGINT）"""
        return self._send_to_terminal_tty(tty, "key code 8 using control down")

    def read_terminal_output(self, lines: int = 50) -> tuple[str, int]:
        """Read macOS Terminal.app visible content via AppleScript

        Quick, non-invasive read of visible terminal content.
        Used by health check (runs every 5s, cannot steal focus).

        Args:
            lines: Number of tail lines to return

        Returns:
            (output_text, line_count)
        """
        script = """
        tell application "Terminal"
            try
                set allContent to contents of selected tab of front window
                return allContent
            on error
                return ""
            end try
        end tell
        """
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=5,
            )
            text = result.stdout.strip()
            if text:
                line_count = len(text.splitlines())
                if line_count > lines:
                    text = "\n".join(text.splitlines()[-lines:])
                    line_count = lines
                return text, line_count
        except Exception:
            pass
        return "", 0

    def read_terminal_by_app(self, app_name: str, lines: int = 50) -> tuple[str, int]:
        """Read terminal content, activating the target app first"""
        import time
        try:
            activate_script = f"""
            tell application "{app_name}"
                activate
            end tell
            delay 0.2
            """
            subprocess.run(["osascript", "-e", activate_script], capture_output=True, timeout=3)
            time.sleep(0.3)

            if app_name == "Terminal":
                return self.read_terminal_output(lines)
            else:
                return self.read_output(app_name, lines)
        except Exception as e:
            logger.warning("read_terminal_by_app(%s) failed: %s", app_name, e)
            return "", 0

    def read_terminal_by_tty(self, tty: str, lines: int = 50) -> tuple[str, int]:
        """Read Terminal.app tab content by matching its TTY

        Iterates all Terminal windows/tabs and returns the `contents` of the
        tab whose `tty` matches. This is non-invasive: no activation, no
        clipboard, no focus stealing — and it reads the EXACT tab for this
        session, not whichever terminal happens to be frontmost.

        Args:
            tty: TTY device name (e.g. "ttys005")
            lines: Number of tail lines to return

        Returns:
            (output_text, line_count)
        """
        if not tty:
            return "", 0
        # AppleScript 中字符串转义
        tty_esc = tty.replace('"', '\\"')
        # 注意：`contents of t` 会与 AppleScript 保留字 contents（解引用运算符）冲突，
        # 返回 tab 对象引用而非文本。必须用 `tell tab <i> of window <j> to set c to contents`
        # 这种显式索引引用形式才能取到 tab 的 contents 文本属性。
        # tty of t 返回 "/dev/ttys007"，需去掉 "/dev/" 前缀与 ps 输出对齐。
        script = f"""
        set targetTTY to "{tty_esc}"
        tell application "Terminal"
            set wCount to count of windows
            repeat with wi from 1 to wCount
                try
                    set w to window wi
                    set tCount to count of tabs of w
                    repeat with ti from 1 to tCount
                        set theTab to tab ti of w
                        set tabTTY to tty of theTab
                        if tabTTY starts with "/dev/" then
                            set tabTTY to text 6 thru -1 of tabTTY
                        end if
                        if tabTTY is targetTTY then
                            tell tab ti of window wi to set c to contents
                            return c as text
                        end if
                    end repeat
                end try
            end repeat
            return ""
        end tell
        """
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=8,
            )
        except Exception as e:
            logger.warning("read_terminal_by_tty(%s) failed: %s", tty, e)
            return "", 0
        if result.returncode != 0:
            logger.warning("read_terminal_by_tty(%s) osascript error: %s", tty, result.stderr.strip())
            return "", 0
        text = result.stdout
        if not text:
            return "", 0
        # Terminal 的 contents 末尾常带一个空行，去掉
        text = text.rstrip("\n")
        line_count = len(text.splitlines())
        if line_count > lines:
            text = "\n".join(text.splitlines()[-lines:])
            line_count = lines
        return text, line_count

    def read_terminal_full_output(self, lines: int = 50) -> tuple[str, int]:
        """Read macOS Terminal.app full scrollback via clipboard

        Uses a single AppleScript call: activate -> Cmd+A -> Cmd+C.
        Split osascript calls lose focus between invocations.

        Only call on user demand (/status), NOT in health check loop.
        """
        saved = self._get_clipboard()
        script = """tell application "Terminal"
    activate
end tell
delay 0.4
tell application "System Events"
    tell process "Terminal"
        set frontmost to true
        delay 0.2
        keystroke "a" using command down
    end tell
end tell
delay 0.3
tell application "System Events"
    tell process "Terminal"
        keystroke "c" using command down
    end tell
end tell"""
        try:
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass
        output = self._get_clipboard()
        if saved:
            self._set_clipboard(saved)
        if not output:
            return "", 0
        text = output
        line_count = len(text.splitlines())
        if line_count > lines:
            text = "\n".join(text.splitlines()[-lines:])
            line_count = lines
        return text, line_count

    @staticmethod
    def _app_to_process(app_name: str) -> str:
        """Map display name (e.g. 'IntelliJ IDEA') to actual process name (e.g. 'idea')"""
        return IDE_PROCESS_NAMES.get(app_name, app_name)

    def read_output(self, app_name: str, lines: int = 50) -> tuple[str, int]:
        """Read IDE terminal output via Select All -> Copy -> Clipboard

        流程：activate -> 聚焦终端区域 -> Cmd+A -> Cmd+C -> 读剪贴板。

        聚焦策略（解决之前抓到编辑器内容/抓空的问题）：
        1. 优先找"Claude Code 工具窗口"组（Claude Code 插件），set focused 聚焦它。
           —— 已验证：set focused of <group> to true 能让插件面板获得焦点，
           不会像 Alt+F12 那样在已打开时反关闭。
        2. 兜底：点击左工具栏的"终端/Terminal"按钮（内置终端场景），AXPress 打开/聚焦。
        3. 都找不到就退化为只 activate + Cmd+A + Cmd+C。

        调用方需自行判断输出是否为终端内容（含 ❯、Claude UI 等标记），
        否则可能是编辑器内容，应丢弃。

        Args:
            app_name: Application display name (e.g. 'IntelliJ IDEA')
            lines: Number of tail lines to return

        Returns:
            (output_text, line_count)
        """
        saved = self._get_clipboard()

        process = self._app_to_process(app_name)
        proc = process or app_name

        # AppleScript 里先清空剪贴板，便于判断 Cmd+C 是否真的复制到了新内容
        script = (
            'tell application "' + app_name + '"\n'
            '    activate\n'
            'end tell\n'
            'delay 0.3\n'
            'tell application "System Events"\n'
            '    tell process "' + proc + '"\n'
            '        set frontmost to true\n'
            '        delay 0.15\n'
            '        -- 1. 尝试聚焦 Claude Code 工具窗口组（插件场景）\n'
            '        set ccFound to false\n'
            '        try\n'
            '            set w to front window\n'
            '            set root to UI element 1 of w\n'
            '            repeat with ui in UI elements of root\n'
            '                try\n'
            '                    if (description of ui) contains "Claude Code" then\n'
            '                        set focused of ui to true\n'
            '                        set ccFound to true\n'
            '                        exit repeat\n'
            '                    end if\n'
            '                end try\n'
            '            end repeat\n'
            '        end try\n'
            '        -- 2. 兜底：点击左工具栏的终端按钮（内置终端场景）\n'
            '        if not ccFound then\n'
            '            try\n'
            '                repeat with ui in UI elements of root\n'
            '                    if (description of ui) contains "工具栏" or (description of ui) contains "toolbar" then\n'
            '                        repeat with b in UI elements of ui\n'
            '                            try\n'
            '                                set bd to (description of b)\n'
            '                                if bd contains "终端" or bd contains "Terminal" then\n'
            '                                    perform action "AXPress" of b\n'
            '                                    exit repeat\n'
            '                                end if\n'
            '                            end try\n'
            '                        end repeat\n'
            '                        exit repeat\n'
            '                    end if\n'
            '                end repeat\n'
            '            end try\n'
            '        end if\n'
            '        delay 0.3\n'
            '        keystroke "a" using command down\n'
            '    end tell\n'
            'end tell\n'
            'delay 0.2\n'
            'tell application "System Events"\n'
            '    tell process "' + proc + '"\n'
            '        keystroke "c" using command down\n'
            '    end tell\n'
            'end tell\n'
            'delay 0.2\n'
        )
        try:
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=12,
            )
        except Exception:
            pass

        output = self._get_clipboard()

        if saved:
            self._set_clipboard(saved)

        if not output:
            return "", 0

        text = output
        line_count = len(text.splitlines())

        if line_count > lines:
            text = "\n".join(text.splitlines()[-lines:])
            line_count = lines

        return text, line_count

    @staticmethod
    def _get_clipboard() -> str:
        """Read system clipboard content"""
        try:
            result = subprocess.run(
                ["pbpaste", "-Prefer", "txt"],
                capture_output=True, text=True, timeout=3,
            )
            return result.stdout or ""
        except Exception:
            return ""

    @staticmethod
    def _set_clipboard(text: str) -> None:
        """Set system clipboard content"""
        try:
            subprocess.run(
                ["pbcopy"],
                input=text, text=True, timeout=3,
            )
        except Exception:
            pass

    def _with_clipboard_text(self, text: str, paste_action) -> bool:
        """把 text 放到剪贴板、执行 paste_action() 粘贴、再还原剪贴板

        用剪贴板 Cmd+V 粘贴代替 keystroke 文本：keystroke 无法对中文等多字节
        字符生成键盘事件，会打成乱码（ASCII 正常）。paste_action 须在调用前
        已聚焦目标窗口/tab，仅负责 Cmd+V 粘贴（和回车）。

        Args:
            text: 要粘贴的文本
            paste_action: 无参可调用对象，执行实际粘贴，返回是否成功

        Returns:
            paste_action 的返回值
        """
        saved = self._get_clipboard()
        self._set_clipboard(text)
        try:
            return paste_action()
        finally:
            self._set_clipboard(saved)

    @staticmethod
    def _read_tail(path: str, lines: int = 50) -> tuple[str, int]:
        """Read tail of a file (used for Terminal log files only)"""
        import os
        try:
            if not os.path.exists(path):
                return "", 0
            result = subprocess.run(
                ["tail", "-n", str(lines), path],
                capture_output=True, text=True, timeout=5,
            )
            text = result.stdout
            line_count = len(text.splitlines())
            return text, line_count
        except Exception as e:
            logger.warning("Failed to read tail %s: %s", path, e)
            return "", 0

    def list_terminals(self) -> list[dict]:
        """List all controllable terminal windows (for debugging)"""
        return self.find_ide_terminal()

    @staticmethod
    def _parse_ascript_records(text: str) -> list[dict]:
        """Parse AppleScript record list output"""
        windows = []
        for line in text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.search(
                r"\{?\s*app:(\S+?),\s*pid:(\d+),\s*title:(.+?),\s*winID:(\d+)\s*\}?",
                line,
            )
            if m:
                windows.append({
                    "app": m.group(1),
                    "pid": int(m.group(2)),
                    "title": m.group(3).strip(),
                    "win_id": int(m.group(4)),
                })
        return windows


# Singleton
ide_control = IDEControl()