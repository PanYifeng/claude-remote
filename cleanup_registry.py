"""清理注册表中的陈旧 session 记录

规则（只删确定已死的记录，活跃会话绝不碰）：
- terminal/ide 会话：pid 已死 → 删
- screen 会话：对应 tmux session 已不存在 → 删（覆盖 pid=0 的旧记录）

用法:
    python3 cleanup_registry.py           # dry-run，只打印将要删除的记录
    python3 cleanup_registry.py --apply   # 实际删除
"""

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from registry import SessionRegistry


def _pid_alive(pid: int) -> bool:
    """检查进程是否存活"""
    if not pid:
        return False
    return subprocess.run(
        ["ps", "-p", str(pid)], capture_output=True
    ).returncode == 0


def _tmux_alive(screen_name: str) -> bool:
    """检查 tmux session 是否还存在"""
    if not screen_name:
        return False
    return subprocess.run(
        ["tmux", "has-session", "-t", screen_name],
        capture_output=True,
    ).returncode == 0


def _stale(s: dict) -> str:
    """判断记录是否陈旧，返回原因（空串 = 保留）"""
    stype = s.get("session_type", "screen")
    if stype in ("terminal", "ide"):
        if not _pid_alive(s.get("pid", 0)):
            return "pid dead"
        return ""
    # screen / 其他：按 tmux session 判活
    if not _tmux_alive(s.get("screen_name", "")):
        return "tmux session gone"
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="清理陈旧 session 记录")
    parser.add_argument("--apply", action="store_true", help="实际执行删除（默认 dry-run）")
    args = parser.parse_args()

    reg = SessionRegistry(Path.home() / ".claude-remote" / "sessions.db")
    rows = reg.list()
    stale = [(s, _stale(s)) for s in rows]
    to_delete = [(s, why) for s, why in stale if why]

    print(f"total={len(rows)}, stale={len(to_delete)}, keep={len(rows) - len(to_delete)}")
    for s, why in to_delete:
        print(f"  {'DEL' if args.apply else 'DRY'} {s['id'][:8]} [{s.get('session_type')}] "
              f"{s.get('name', '')[:40]} — {why}")

    if not args.apply:
        print("\ndry-run only — add --apply to delete")
        return 0

    for s, _ in to_delete:
        reg.delete(s["id"])
    print(f"\ndeleted {len(to_delete)} stale sessions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
