"""IDE 终端控制 — 通过 macOS Accessibility API 控制 IntelliJ/PyCharm/VS Code 终端

提供：
- find_ide_terminal(): 定位匹配的 IDE 终端窗口
- send_keys(): 向终端发送文本+回车
- send_text(): 发送纯文本
- send_enter(): 回车
- send_ctrl_c(): Ctrl+C
- list_terminals(): 列出所有可控制的 IDE 终端

需要「系统设置 → 隐私与安全性 → 辅助功能」授权 python3（或 Terminal/IDE）。
"""

import logging
import re
import subprocess
from typing import Optional

logger = logging.getLogger("ide_control")

# 已知的 IDE 进程名（macOS 上的 app process name）
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

# IDEA 类 IDE 内部终端所在窗口（AppCode 等基于 IntelliJ 的 IDE）
# 这些 IDE 的终端在 tool window 中，窗口标题通常是项目名或 wildcard


class IDEControl:
    """IDE 终端操作封装"""

    def __init__(self):
        self._cached_windows: list[dict] = []
        self._cache_time: float = 0

    def find_ide_terminal(self, keyword: str = "") -> list[dict]:
        """查找运行 Claude Code 的 IDE 终端窗口

        通过 AppleScript 获取系统事件中所有窗口信息，
        匹配 IDE 进程名或标题中的 Claude 关键词。

        Args:
            keyword: 额外关键词过滤

        Returns:
            窗口信息列表，每个包含 {app, pid, title, win_id}
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

        # 过滤 IDE 进程 + 关键词
        ide_names = set(IDE_PROCESS_NAMES.values())
        matched = []
        for w in windows:
            proc = w.get("app", "")
            title = w.get("title", "")

            # 必须在 IDE 列表中的进程
            if proc not in ide_names and proc.lower() not in {n.lower() for n in ide_names}:
                continue

            # 如果有关键词，过滤标题
            if keyword and keyword.lower() not in title.lower():
                continue

            matched.append(w)

        self._cached_windows = matched
        return matched

    def register_session(self, session_id: str, app_name: str,
                          win_title: str, pid: int) -> bool:
        """通过 AppleScript 注册 IDE session（验证窗口是否存在）"""
        windows = self.find_ide_terminal()
        for w in windows:
            if w["app"].lower() == app_name.lower():
                logger.info("IDE terminal found: %s (PID: %s)", app_name, pid)
                return True
        logger.warning("IDE terminal %s not found", app_name)
        return False

    def send_keys(self, app_name: str, text: str) -> bool:
        """向指定 IDE 的终端窗口发送文本+回车

        通过 AppleScript keystroke 模拟键盘输入到目标应用。

        Args:
            app_name: 应用进程名（如 "IntelliJ IDEA", "Code"）
            text: 要发送的文本（自动追加回车）

        Returns:
            是否成功
        """
        escaped = text.replace('"', '\\"')
        script = f"""
        tell application "{app_name}"
            activate
        end tell
        delay 0.15
        tell application "System Events"
            tell process "{app_name}"
                set frontmost to true
                delay 0.1
                keystroke "{escaped}"
                keystroke return
            end tell
        end tell
        """
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

    def send_text(self, app_name: str, text: str) -> bool:
        """发送纯文本（不追加回车）"""
        escaped = text.replace('"', '\\"')
        script = f"""
        tell application "{app_name}"
            activate
        end tell
        delay 0.15
        tell application "System Events"
            tell process "{app_name}"
                set frontmost to true
                delay 0.1
                keystroke "{escaped}"
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

    def send_enter(self, app_name: str) -> bool:
        """发送回车"""
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
        """发送 Ctrl+C"""
        script = f"""
        tell application "{app_name}"
            activate
        end tell
        delay 0.1
        tell application "System Events"
            tell process "{app_name}"
                -- 在 IntelliJ 类 IDE 中, Ctrl+C 被映射到复制,
                -- 需要发送 Ctrl+Shift+C 或使用 Esc 退出当前模式
                -- 先尝试标准 Ctrl+C
                key code 8 using command down
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

    def list_terminals(self) -> list[dict]:
        """列出所有可控制的终端窗口（供调试用）"""
        return self.find_ide_terminal()

    @staticmethod
    def _parse_ascript_records(text: str) -> list[dict]:
        """解析 AppleScript 返回的记录列表

        AppleScript 返回格式: {app:XXX, pid:123, title:YYY, winID:456}
        每个记录占一行。
        """
        windows = []
        for line in text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            # 解析键值对
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


# 单例
ide_control = IDEControl()