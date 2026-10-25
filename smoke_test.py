"""冒烟测试 — 全部只读，不向任何真实会话注入按键、不发飞书消息"""

import asyncio
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ide_control import IDEControl, session_tty
from lark_bot import LarkBot
from lark_card import session_list_card, session_status_card
from registry import SessionRegistry
from screen_manager import ScreenManager

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


def http_get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)


# 1. Daemon 存活 + API
code, body = http_get("http://localhost:9998/health")
check("daemon /health", code == 200, f"code={code}")
code, body = http_get("http://localhost:9998/api/sessions")
check("daemon /api/sessions", code == 200, f"code={code}")
sessions = json.loads(body)["sessions"] if code == 200 else []
check("sessions non-empty", len(sessions) > 0, f"{len(sessions)} sessions")

# 2. 事件消费循环（消息 + 卡片按钮）仍在运行
ps = subprocess.run(["ps", "auxww"], capture_output=True, text=True).stdout
n_msg = ps.count("event consume im.message.receive_v1")
n_card = ps.count("event consume card.action.trigger")
check("message event consumer", n_msg >= 1, f"{n_msg} process(es)")
check("card event consumer", n_card >= 1, f"{n_card} process(es)")

# 3. daemon 重启后日志无 traceback / error
log_path = Path.home() / ".claude-remote" / "logs" / "daemon.log"
log_text = log_path.read_text(errors="replace") if log_path.exists() else ""
recent = log_text.split("Daemon starting")[-1] if "Daemon starting" in log_text else log_text[-5000:]
check("daemon log clean since restart",
      "Traceback" not in recent and " ERROR " not in recent,
      f"{len(recent)} chars scanned")

# 4. 核心修复：各 terminal 会话输出必须互不相同（读各自的 tty）
reg = SessionRegistry(Path.home() / ".claude-remote" / "sessions.db")
bot = LarkBot(reg, ScreenManager())
term_sessions = [s for s in reg.list() if s.get("session_type") == "terminal"
                 and session_tty(s)]
outputs: dict[str, str] = {}
for s in term_sessions:
    out, _ = asyncio.run(bot._read_session_output(s))
    outputs.setdefault(out.strip()[:100], []).append(s["id"][:8])
distinct = len(outputs)
check("terminal outputs differ per session",
      distinct >= 2 or len(term_sessions) < 2,
      f"{len(term_sessions)} sessions -> {distinct} distinct outputs: "
      f"{[ids[0] for ids in outputs.values()]}")

# 5. IDE 会话读取路径（只读 AppleScript，不抢焦点）
ide_sessions = [s for s in reg.list() if s.get("session_type") == "ide"]
if ide_sessions:
    out, _ = bot.ide_ctrl.read_output(ide_sessions[0].get("app_name", ""), 15)
    check("ide read_output no exception", True, f"app={ide_sessions[0].get('app_name')} len={len(out)}")
else:
    print("ℹ️ no IDE sessions registered, skip ide read test")

# 6. 卡片构建（纯函数，不发飞书）
import json as _json


def _card_ok(card: str) -> bool:
    try:
        d = _json.loads(card)
        return isinstance(d, dict) and "elements" in d
    except (ValueError, TypeError):
        return False


list_card = session_list_card(reg.list())
status_card = session_status_card(term_sessions[0], "smoke test output")
check("session_list_card valid JSON", _card_ok(list_card))
check("session_status_card valid JSON", _card_ok(status_card))

# 7. session_tty 兼容 dict 和 JSON 字符串两种 tags 形态
s0 = term_sessions[0]
tags_dict = {**s0, "tags": json.loads(s0["tags"]) if isinstance(s0.get("tags"), str) else s0.get("tags", {})}
check("session_tty(dict tags)", session_tty(tags_dict) == session_tty(s0),
      f"tty={session_tty(s0)}")
check("session_tty(no tags)", session_tty({"id": "x"}) == "")

# 8. 无 tty 的会话不崩溃，走日志文件兜底
no_tty = {**s0, "id": "smoke000", "tags": "{}"}
out, _ = asyncio.run(bot._read_session_output(no_tty))
check("no-tty fallback no crash", isinstance(out, str))

print(f"\n{'='*50}\n结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)
