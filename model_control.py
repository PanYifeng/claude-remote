"""Claude Code 会话模型读取与切换支持

- 读当前模型：transcript .jsonl 里 assistant 消息的 message.model 字段
- 切换模型：TUI 斜杠命令 '/model <name>' 直接切换并保存为默认（实测
  2026-11-19，无需对话框交互）
- 候选模型：每次展示时实时从网关拉取（/v1/models），按家族分组、
  每组取最新版本，失败时回退到内置精选列表
"""

import json
import logging
import os
import re
import subprocess
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

# transcript 尾部扫描行数（找最后一条带 model 的 assistant 消息）
_TAIL_LINES = 500
# 扫描超时（秒）
_TAIL_TIMEOUT = 10
# 网关模型列表缓存（秒）：状态卡可能连点刷新，避免每次都打网关
_LIST_CACHE_TTL = 300
_list_cache: dict = {"ts": 0.0, "models": []}

# 家族分组规则：模型名（去 vendor 前缀、小写）→ 家族名。
# 未匹配的模型不展示（网关列表里带日期后缀的旧版本、qwen 全家桶等
# 噪声太多，只挑主流家族）。
_FAMILY_PATTERNS: list[tuple[str, str]] = [
    (r"^glm", "GLM"),
    (r"^deepseek", "DeepSeek"),
    (r"^kimi", "Kimi"),
    (r"^claude", "Claude"),
    (r"^gpt", "GPT"),
    (r"^gemini", "Gemini"),
    (r"^grok", "Grok"),
    (r"^minimax", "MiniMax"),
    (r"^doubao", "Doubao"),
]
# 每个家族最多展示的模型数（按新鲜度排序后截断）
_MAX_PER_FAMILY = 3
# 已知不可用模型（实测排除：Vendor2/GLM-5.1 走 Anthropic 协议 400）
_MODEL_BLACKLIST = {"vendor2/glm-5.1"}
# 版本号提取（用于组内排序：新版本排前）：glm-5.3-flash → (5, 3)
_VER_RE = re.compile(r"(\d+(?:\.\d+)*)")


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


# 内置精选分组：网关拉取失败时的回退（2026-11-23 实测可用）。
# 注意 GLM-5.3 / GLM-5.3-flash / DeepSeek-V4.1-Flash-1017 不在网关
# /v1/models 列表里但实测可用——网关列表不全，这几个直连模型手动保留。
_CURATED_MODEL_GROUPS: dict[str, list[str]] = {
    "GLM": ["GLM-5.3", "GLM-5.3-flash"],
    "DeepSeek": ["DeepSeek-V4.1-Flash-1017"],
    "Kimi": ["Vendor2/Kimi-k2.6", "Vendor3/kimi-k2.5"],
    "GPT": ["Vendor2/GPT-5.5", "Vendor2/GPT-5.3-codex", "Vendor2/GPT-5-mini"],
    "Claude": ["Vendor2/Claude-4.8-opus", "Vendor2/Claude-4.6-Sonnet"],
}


def fetch_model_groups() -> dict[str, list[str]]:
    """获取当前可用模型分组：实时拉网关，失败回退内置精选

    网关 /v1/models 每次展示时拉取（5 分钟缓存），按家族分组、
    组内按版本号新鲜度排序、每家族取最新几个。网关列表不含的
    直连模型（GLM-5.3 系、DeepSeek）从内置列表补进对应家族。

    Returns:
        {家族名: [模型全名]}，保序（_FAMILY_PATTERNS 顺序）
    """
    import time

    now = time.time()
    if _list_cache["models"] and now - _list_cache["ts"] < _LIST_CACHE_TTL:
        return _list_cache["models"]
    ids = _fetch_gateway_models()
    if ids:
        groups = _group_models(ids)
        # 网关列表不全：补进直连模型（实测可用但不在 /v1/models 里），
        # 合并后整组按新鲜度重排再截断（直连 GLM-5.3 会排到 glm-4.7 前面）
        for family, models in _CURATED_MODEL_GROUPS.items():
            existing = {x.lower() for x in groups.get(family, [])}
            extra = [m for m in models if m.lower() not in existing]
            merged = extra + groups.get(family, [])
            groups[family] = sorted(merged, key=_model_freshness, reverse=True)[:_MAX_PER_FAMILY + 1]
    else:
        groups = {f: list(ms) for f, ms in _CURATED_MODEL_GROUPS.items()}
    # 按 _FAMILY_PATTERNS 顺序重排（DeepSeek 等直连家族不落在末尾）
    order = [fam for _pat, fam in _FAMILY_PATTERNS]
    groups = {fam: groups[fam] for fam in order if fam in groups}
    _list_cache["models"] = groups
    _list_cache["ts"] = now
    return groups


def _fetch_gateway_models() -> list[str]:
    """从网关 /v1/models 拉取模型 ID 列表（失败返回空列表）"""
    base = _settings_env().get("ANTHROPIC_BASE_URL", "")
    key = _settings_env().get("ANTHROPIC_API_KEY", "")
    if not base or not key:
        return []
    try:
        req = urllib.request.Request(
            base.rstrip("/") + "/v1/models",
            headers={"Authorization": "Bearer " + key},
        )
        # venv Python 无系统根证书，用 certifi 的 CA 包
        ctx = None
        try:
            import certifi
            import ssl
            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            pass
        with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
            data = json.loads(r.read())
        return [m.get("id", "") for m in data.get("data", []) if m.get("id")]
    except Exception as e:
        logger.warning("fetch gateway models failed: %s", e)
        return []


def _group_models(ids: list[str]) -> dict[str, list[str]]:
    """把模型 ID 列表按家族分组，组内按版本新鲜度排序并截断

    - 去 vendor 前缀后按 _FAMILY_PATTERNS 匹配家族
    - 组内排序：主版本号降序；同版本时非 flash/mini/lite 变体优先
      （旗舰 > 轻量）；带日期后缀的旧版本排最后
    - 每家族最多 _MAX_PER_FAMILY 个
    """
    families: dict[str, list[str]] = {}
    for mid in ids:
        if mid.lower() in _MODEL_BLACKLIST:
            continue
        short = mid.split("/")[-1]
        for pat, family in _FAMILY_PATTERNS:
            if re.match(pat, short.lower()):
                families.setdefault(family, []).append(mid)
                break
    groups: dict[str, list[str]] = {}
    for _pat, family in _FAMILY_PATTERNS:
        if family not in families:
            continue
        models = sorted(families[family], key=_model_freshness, reverse=True)
        groups[family] = models[:_MAX_PER_FAMILY]
    return groups


def _model_freshness(mid: str) -> tuple:
    """模型新鲜度排序键：版本号降序，旗舰变体优先，日期后缀垫底"""
    short = mid.split("/")[-1].lower()
    # 版本号序列（glm-5.3-flash → (5,3)），无版本号按 (0,)
    vers = tuple(float(v) for v in _VER_RE.findall(short)[:4]) or (0,)
    # 旗舰优先：flash/mini/lite/air 等轻量变体降权
    lite = any(t in short for t in ("flash", "mini", "lite", "air", "omni"))
    # 带日期后缀（qwen-plus-2025-07-14）是旧快照，垫底
    dated = bool(re.search(r"-\d{4}-\d{2}-\d{2}$", short)) or "latest" in short
    return vers, (0 if dated else 1), (0 if lite else 1)


def collect_known_models(active_sessions: list[dict]) -> list[str]:
    """收集候选模型列表（去重、保序）

    来源：精选分组列表 + settings.json env 的模型配置 + 各活跃会话
    当前实际在用的模型。精选列表排最前（真实可用、直接可选）。

    Args:
        active_sessions: 活跃会话列表（registry.list() 的返回值）

    Returns:
        模型名列表，空则返回默认建议
    """
    models: list[str] = [m for group in fetch_model_groups().values() for m in group]
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
