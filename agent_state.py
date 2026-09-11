"""统一的会话状态读取调度

按 agent 类型（claude | opencode）把"读取会话状态 + 最近一条 assistant 文本"
派发到对应后端。lark_bot 与 daemon 的状态判定均走此函数，避免 claude/opencode
判断逻辑散落各处。

- claude（默认）：读 ~/.claude/sessions/<pid>.json 拿 sessionId + status，
  再读 transcript .jsonl 取最近 assistant 文本
- opencode：读 ~/.local/share/opencode/opencode.db 取状态与文本
"""

import logging
from typing import Optional

from ide_control import read_claude_session_state, map_claude_status, read_last_assistant_message
from opencode_state import AgentState, read_opencode_state

logger = logging.getLogger(__name__)

# agent 类型常量
AGENT_CLAUDE = "claude"
AGENT_OPENCODE = "opencode"

# 返回文本最多字符数（claude 路径）
_MAX_TEXT_CHARS = 1200


def read_agent_state(agent: str, pid: int, cwd: str) -> Optional[AgentState]:
    """读取会话状态（按 agent 派发）

    Args:
        agent: AGENT_CLAUDE 或 AGENT_OPENCODE
        pid: 进程 pid（claude 路径用于定位 ~/.claude/sessions/<pid>.json）
        cwd: 进程工作目录（opencode 路径用于匹配 session.directory；claude 路径用于定位 transcript）

    Returns:
        AgentState，或 None（无状态可读）
    """
    if agent == AGENT_OPENCODE:
        return read_opencode_state(cwd)
    return _read_claude_state(pid, cwd)


def _read_claude_state(pid: int, cwd: str) -> Optional[AgentState]:
    """读取 claude 会话状态：per-process 状态文件 + transcript"""
    if not pid:
        return None
    cs = read_claude_session_state(pid)
    if not cs:
        return None
    session_id = cs.get("sessionId", "")
    text = ""
    if session_id:
        text, _ = read_last_assistant_message(cwd, session_id, _MAX_TEXT_CHARS)
    status = map_claude_status(cs.get("status", ""))
    return AgentState(status=status, text=text, session_id=session_id)
