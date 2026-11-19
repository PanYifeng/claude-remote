"""Claude Code 会话模型读取与切换支持

- 读当前模型：transcript .jsonl 里 assistant 消息的 message.model 字段
- 切换模型：TUI 斜杠命令 '/model <name>' 直接切换并保存为默认（实测
  2026-11-19，无需对话框交互）
- 候选模型：各活跃会话 transcript 里实际出现过的模型 + settings.json
  env 里配置的模型（ANTHROPIC_MODEL / *_SONNET / *_OPUS / *_HAIKU）
"""

import json
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# transcript 尾部扫描行数（找最后一条带 model 的 assistant 消息）
_TAIL_LINES = 500
# 扫描超时（秒）
_TAIL_TIMEOUT = 10


def read_session_model(cwd: str, session_id: str) -> str:
    """读取会话当前使用的模型（transcript 最后一条 assistant 的 model）

    Args:
        cwd: 会话工作目录（定位 transcript 项目目录）
        session_id: Claude Code 会话 ID（transcript 文件名）

    Returns:
        模型名（如 "GLM-5.3-flash"），读不到返回空串
    """
    if not cwd or not session_id:
        return ""
    proj_dir = Path.home() / ".claude" / "projects" / cwd.replace("/", "-")
    fp = proj_dir / f"{session_id}.jsonl"
    if not fp.exists():
        return ""
    try:
        r = subprocess.run(
            ["tail", "-n", str(_TAIL_LINES), str(fp)],
            capture_output=True, text=True, timeout=_TAIL_TIMEOUT,
        )
    except Exception as e:
        logger.warning("read_session_model tail failed: %s", e)
        return ""
    for ln in reversed(r.stdout.splitlines()):
        try:
            o = json.loads(ln)
        except Exception:
            continue
        msg = o.get("message")
        if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("model"):
            return str(msg["model"])
    return ""


# 实测可用模型（2026-11-19 网关探测：Anthropic 协议全部 OK）。
# 旧的 Vendor3/glm-5.1 已 404 下线；Vendor2/GLM-5.1 走 Anthropic 协议 400。
_CURATED_MODELS = [
    "GLM-5.3-flash",
    "DeepSeek-V4.1-Flash-1017",
    "Vendor3/glm-5",
    "Vendor3/MiniMax-M2.5",
    "Vendor2/Claude-4.8-opus",
]


def collect_known_models(active_sessions: list[dict]) -> list[str]:
    """收集候选模型列表（去重、保序）

    来源：精选实测可用列表 + settings.json env 的模型配置 + 各活跃会话
    当前实际在用的模型。精选列表排最前（真实可用、直接可选）。

    Args:
        active_sessions: 活跃会话列表（registry.list() 的返回值）

    Returns:
        模型名列表，空则返回默认建议
    """
    models: list[str] = list(_CURATED_MODELS)
    # settings.json env 里的模型配置
    for env_key in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
        val = _settings_env().get(env_key, "")
        if val and val not in models:
            models.append(val)
    # 各会话实际在用的模型
    for s in active_sessions:
        if s.get("agent", "claude") != "claude":
            continue
        pid = s.get("pid", 0)
        if not pid:
            continue
        try:
            state_fp = Path.home() / ".claude" / "sessions" / f"{pid}.json"
            state = json.loads(state_fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        m = read_session_model(s.get("cwd", ""), state.get("sessionId", ""))
        if m and m not in models:
            models.append(m)
    # 大小写归一化去重（网关实测不区分大小写；GLM-5.3-Flash 与 GLM-5.3-flash 同模型）
    seen: dict[str, str] = {}
    for m in models:
        seen.setdefault(m.lower(), m)
    models = list(seen.values())
    return models


def _settings_env() -> dict:
    """读 ~/.claude/settings.json 的 env 段（读失败返回空 dict）"""
    fp = Path.home() / ".claude" / "settings.json"
    try:
        return json.loads(fp.read_text(encoding="utf-8")).get("env", {}) or {}
    except Exception:
        return {}
