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

import logging
import re
import subprocess
from typing import Optional

logger = logging.getLogger("ide_control")

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
        """Send text + Enter to IDE terminal via AppleScript keystroke

        Args:
            app_name: Process name (e.g. "IntelliJ IDEA", "Code")
            text: Text to send (auto-appends Enter)

        Returns:
            True if successful
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
        """Send plain text (no Enter appended)"""
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
        """Send Ctrl+C"""
        script = f"""
        tell application "{app_name}"
            activate
        end tell
        delay 0.1
        tell application "System Events"
            tell process "{app_name}"
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

    def read_terminal_full_output(self, lines: int = 50) -> tuple[str, int]:
        """Read macOS Terminal.app full scrollback via clipboard"""
        saved = self._get_clipboard()
        try:
            script = """
            tell application "Terminal"
                activate
            end tell
            delay 0.1
            tell application "System Events"
                tell process "Terminal"
                    set frontmost to true
                    delay 0.05
                    keystroke "a" using command down
                end tell
            end tell
            delay 0.1
            tell application "System Events"
                tell process "Terminal"
                    keystroke "c" using command down
                end tell
            end tell
            """
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=3,
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

        Single AppleScript: activate -> Cmd+A -> Cmd+C.
        Does NOT press F12/Alt+F12 as those are unreliable across IDE versions.

        The caller must check if the output looks like terminal content
        (via _looks_waiting, presence of ❯, Claude UI patterns) vs editor content.

        Requires Accessibility permission and clipboard access.

        Args:
            app_name: Application display name (e.g. 'IntelliJ IDEA')
            lines: Number of tail lines to return

        Returns:
            (output_text, line_count)
        """
        saved = self._get_clipboard()

        process = self._app_to_process(app_name)
        proc = process or app_name

        # Single AppleScript: activate -> F12 x2 (toggle terminal open) -> Cmd+A -> Cmd+C
        # F12 toggles the terminal panel in IntelliJ-based IDEs. Pressing it twice
        # ensures the terminal is visible regardless of its prior state.
        # For VS Code/Cursor, use Ctrl+` instead of F12.
        if proc in ("idea", "pycharm", "webstorm"):
            # IntelliJ-based: just activate + Cmd+A + Cmd+C.
            # No Alt+F12/F12 — they TOGGLE the terminal panel, so if it's
            # already open they close it, making things worse.
            # If the terminal panel is already visible, this captures it.
            # If the editor is focused, it captures editor content — which
            # _cmd_status detects (no ❯/Claude patterns) and ignores.
            lines_raw = [
                'tell application "' + app_name + '"',
                '    activate',
                'end tell',
                'delay 0.3',
                'tell application "System Events"',
                '    tell process "' + proc + '"',
                '        set frontmost to true',
                '        delay 0.15',
                '        keystroke "a" using command down',
                '    end tell',
                'end tell',
                'delay 0.2',
                'tell application "System Events"',
                '    tell process "' + proc + '"',
                '        keystroke "c" using command down',
                '    end tell',
                'end tell',
            ]
        else:
            lines_raw = [
                'tell application "' + app_name + '"',
                '    activate',
                'end tell',
                'delay 0.3',
                'tell application "System Events"',
                '    tell process "' + proc + '"',
                '        set frontmost to true',
                '        delay 0.15',
                '        keystroke "a" using command down',
                '    end tell',
                'end tell',
                'delay 0.2',
                'tell application "System Events"',
                '    tell process "' + proc + '"',
                '        keystroke "c" using command down',
                '    end tell',
                'end tell',
            ]
        script = '\n'.join(lines_raw)
        try:
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=10,
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