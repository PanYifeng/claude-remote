"""读取 OpenCode 会话状态（从 SQLite 数据库）

OpenCode（github.com/anomalyco/opencode）把会话/消息/消息片段存在
~/.local/share/opencode/opencode.db（SQLite，WAL 模式）。本模块只读地查询
该库，推断会话状态并取出最近一条 assistant 文本——供 /status 与交互模式展示。

PID→会话匹配：OpenCode 的 session 表无 pid 字段，但 directory 记录工作目录，
按 cwd 匹配最近更新的会话（与 claude 的 cwd 回退启发式一致）。

状态推断（看最后一条 part）：
- step-finish + reason=stop      → idle（本轮结束）
- step-finish + reason=tool-calls → executing（调了工具，仍在跑）
- step-start / text / reasoning  → executing（生成中）
- 最近活动 > 120s                 → idle（陈旧）
"""

import json
import logging
import os
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────
OPENCODE_DB_ENV = "OPENCODE_DB"
OPENCODE_DB_PATH = Path.home() / ".local" / "share" / "opencode" / "opencode.db"

# 最近活动超过此秒数视为 idle（陈旧）
STALE_THRESHOLD_SEC = 120
# 返回文本最多字符数
MAX_TEXT_CHARS = 1200

# OpenCode 时间字段为毫秒
_MS_PER_SEC = 1000

# SQL：按 cwd 匹配最近更新的未归档会话
_SQL_MATCH_SESSION = (
    "SELECT id FROM session "
    "WHERE directory = ? AND time_archived IS NULL "
    "ORDER BY time_updated DESC LIMIT 1"
)
# SQL：最后一条 part（用于状态推断）
_SQL_LAST_PART = (
    "SELECT json_extract(data, '$.type'), json_extract(data, '$.reason'), time_created "
    "FROM part WHERE session_id = ? "
    "ORDER BY time_created DESC, id DESC LIMIT 1"
)
# SQL：最近一条 assistant 文本片段
_SQL_LAST_ASSISTANT_TEXT = (
    "SELECT json_extract(p.data, '$.text') FROM part p "
    "JOIN message m ON p.message_id = m.id "
    "WHERE p.session_id = ? "
    "AND json_extract(m.data, '$.role') = 'assistant' "
    "AND json_extract(p.data, '$.type') = 'text' "
    "ORDER BY p.time_created DESC LIMIT 1"
)


@dataclass
class AgentState:
    """会话状态读取结果（claude 与 opencode 共用）"""
    status: Optional[str]  # "executing" | "idle" | "waiting" | None
    text: str              # 最近一条 assistant 文本
    session_id: str        # 会话 ID


def _resolve_db_path() -> Optional[Path]:
    """解析 opencode.db 路径：$OPENCODE_DB 优先，否则默认路径"""
    env_val = os.environ.get(OPENCODE_DB_ENV, "").strip()
    if env_val:
        return Path(env_val)
    return OPENCODE_DB_PATH


def _connect_ro(db_path: Path) -> Optional[sqlite3.Connection]:
    """以只读模式连接 SQLite（WAL 库需用 mode=ro 避免锁竞争）"""
    if not db_path.exists():
        return None
    uri = "file:" + urllib.parse.quote(str(db_path)) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=3)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        logger.warning("opencode db connect failed: %s", e)
        return None


def _infer_status(part_type: str, part_reason: str, time_created_ms: int) -> str:
    """根据最后一条 part 推断状态"""
    elapsed_sec = time.time() - (time_created_ms / _MS_PER_SEC)
    if elapsed_sec > STALE_THRESHOLD_SEC:
        return "idle"
    if part_type == "step-finish":
        # reason=stop 表示本轮结束；其余（如 tool-calls）表示仍在跑
        return "idle" if part_reason == "stop" else "executing"
    # step-start / text / reasoning 等都表示生成中
    return "executing"


def read_opencode_state(cwd: str) -> Optional[AgentState]:
    """读取指定工作目录对应的 OpenCode 会话状态

    Args:
        cwd: OpenCode 进程工作目录（用于匹配 session.directory）

    Returns:
        AgentState，或 None（库不存在 / 无匹配会话 / 查询失败）
    """
    if not cwd:
        return None
    db_path = _resolve_db_path()
    conn = _connect_ro(db_path) if db_path else None
    if conn is None:
        return None
    try:
        return _query_opencode_state(conn, cwd)
    except sqlite3.Error as e:
        logger.warning("opencode state query failed: %s", e)
        return None
    finally:
        conn.close()


def _query_opencode_state(conn: sqlite3.Connection, cwd: str) -> Optional[AgentState]:
    """执行状态查询：匹配会话 → 状态 → 文本"""
    row = conn.execute(_SQL_MATCH_SESSION, (cwd,)).fetchone()
    if row is None:
        return None
    session_id = row[0]

    status = _read_status(conn, session_id)
    text = _read_last_text(conn, session_id)
    return AgentState(status=status, text=text, session_id=session_id)


def _read_status(conn: sqlite3.Connection, session_id: str) -> Optional[str]:
    """读取并推断会话状态"""
    part = conn.execute(_SQL_LAST_PART, (session_id,)).fetchone()
    if part is None:
        return None
    part_type = part[0] or ""
    part_reason = part[1] or ""
    time_created_ms = part[2] or 0
    return _infer_status(part_type, part_reason, time_created_ms)


def _read_last_text(conn: sqlite3.Connection, session_id: str) -> str:
    """读取最近一条 assistant 文本片段"""
    row = conn.execute(_SQL_LAST_ASSISTANT_TEXT, (session_id,)).fetchone()
    if row is None or row[0] is None:
        return ""
    return str(row[0])[:MAX_TEXT_CHARS]
