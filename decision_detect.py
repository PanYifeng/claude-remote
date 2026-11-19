"""TUI 决策识别：从终端可见输出识别审批/选择对话框的类型与选项

数据源是终端可见尾部（Terminal.app tab contents / tmux capture-pane）。
对话框渲染在屏幕底部，因此只看末尾若干行——避免把会话正文里的编号列表
误判成选项（末行必须是选项行或对话框痕迹才认定存在决策）。

支持 claude（Claude Code）与 opencode 两类 TUI 的对话框文本。
"""

import re

# 决策类型常量
KIND_OPTIONS = "options"    # 单选：数字键直选（发送 "<n>" 即选中）
KIND_MULTI = "multi"        # 多选：数字键切换（无回车），Enter 提交
KIND_YN = "yn"              # 是/否：发送 y / n
KIND_CONFIRM = "confirm"    # 泛确认：Enter 确认默认项
KIND_NONE = "none"

# 只看末尾这些行（对话框渲染在屏幕底部）
_TAIL_LINES = 15
# 选项区最多展示的选项数（卡片按钮上限）
_MAX_OPTIONS = 6

# 选项行：容忍框线/选中标记前缀，如 "│ │ 1. Yes"、"❯ 1. Option A"、"[x] 2. foo"
_OPTION_LINE = re.compile(r"^[\s│║❯>◉○\[\]xX]*?(\d{1,2})[.)]\s+(.+?)\s*[│║]?$")
# 对话框提示行（标题/问题文本）
_PROMPT_PATTERNS = (
    re.compile(r"\(y/n\)", re.IGNORECASE),
    re.compile(r"do you want", re.IGNORECASE),
    re.compile(r"would you like", re.IGNORECASE),
    re.compile(r"proceed\?", re.IGNORECASE),
    re.compile(r"requires approval", re.IGNORECASE),
    re.compile(r"\bselect\b", re.IGNORECASE),
    re.compile(r"\bchoose\b", re.IGNORECASE),
)
# 多选提示
_MULTI_HINT = re.compile(r"space to (select|toggle|choose)|select all that apply", re.IGNORECASE)
# 末行可为对话框痕迹：框线 / esc 提示 / 回车提示 / 泛确认问句
_DIALOG_TAIL = re.compile(
    r"[╰╯╭╮┌┐└┘─━═]{2,}|esc to cancel|enter to confirm|space to select"
    r"|proceed\?\s*$|continue\?\s*$", re.IGNORECASE)
# 对话框顶部框线（用于精确圈定选项区）
_BOX_TOP = re.compile(r"[╭┌][─━═]+[╮┐]")
# y/n 问题行（作为末行整体出现）
_YN_LINE = re.compile(r"\(y/n\)\s*[?？]?\s*$", re.IGNORECASE)
# opencode 权限对话框按钮行
_OPENCODE_TAIL = re.compile(r"allow once|allow always|\breject\b|type your own answer", re.IGNORECASE)
# 框线字符（清理选项文本用）
_BOX_CHARS = re.compile(r"[│║╭╮╰╯┌┐└┘─━═]+")

# 权限模式（Claude Code 底栏文案 → 模式名）
# 注意顺序：auto mode 必须在 accept edits 之前匹配（"auto mode on" 不含
# "accept edits"，但 "accept edits on" 行若先被 auto 抢走会误判，反之亦然，
# 按特异性从高到低排列）。manual（默认）与 auto 也纳入——切换后 banner
# 只显示新模式名，读不到时回退 "default"。
_MODE_PATTERNS = (
    (re.compile(r"bypass permissions on", re.IGNORECASE), "bypass-permissions"),
    (re.compile(r"accept edits on", re.IGNORECASE), "accept-edits"),
    (re.compile(r"plan mode on", re.IGNORECASE), "plan"),
    (re.compile(r"auto mode on", re.IGNORECASE), "auto"),
    (re.compile(r"manual mode on", re.IGNORECASE), "manual"),
)
# 模式循环顺序（Shift+Tab 实测：auto → manual → accept edits → plan → auto）
# 与 Claude Code 底栏 banner 一一对应；default = 读不到 banner 时的兜底
MODE_CYCLE = ["auto", "manual", "accept-edits", "plan"]
MODE_LABELS = {
    "auto": "⚡ Auto",
    "manual": "⏸️ Manual",
    "accept-edits": "✏️ Accept Edits",
    "plan": "🧭 Plan",
    "bypass-permissions": "🔓 Bypass Permissions",
    "default": "🔹 Default",
}


def detect_decision(output: str, agent: str = "claude") -> dict:
    """识别终端尾部是否是等待决策的对话框

    Args:
        output: 终端可见尾部文本
        agent: claude | opencode

    Returns:
        {"kind": 类型, "title": 提示文本, "options": [(序号, 文本)], "hint": 操作提示}
    """
    empty = {"kind": KIND_NONE, "title": "", "options": [], "hint": ""}
    if not output or not output.strip():
        return empty
    lines = [l.rstrip() for l in output.strip().splitlines()]
    tail = [l for l in lines[-_TAIL_LINES:] if l.strip()]
    if not tail:
        return empty
    last = tail[-1].strip()

    if agent == "opencode" and _OPENCODE_TAIL.search(last):
        return {"kind": KIND_CONFIRM, "title": "OpenCode 权限确认",
                "options": [], "hint": "Enter 确认默认项（Allow once）"}
    if _YN_LINE.search(last):
        return {"kind": KIND_YN, "title": _clean(last), "options": [], "hint": "发送 y / n 回答"}
    # 末行必须是选项行或对话框痕迹，否则视为无决策（防正文编号列表误判）
    if not (_OPTION_LINE.match(last) or _DIALOG_TAIL.search(last)):
        return empty

    options = _collect_options(tail)
    prompt = _find_prompt(tail)
    if any(_MULTI_HINT.search(l) for l in tail) and options:
        return {"kind": KIND_MULTI, "title": prompt or "多选",
                "options": options, "hint": "数字键切换选项，Enter 提交"}
    if options:
        return {"kind": KIND_OPTIONS, "title": prompt or "选择",
                "options": options, "hint": "点击选项按钮直选"}
    if prompt:
        return {"kind": KIND_CONFIRM, "title": prompt, "options": [], "hint": "Enter 确认"}
    return empty


def detect_mode(output: str) -> str:
    """从终端底栏识别 Claude Code 当前权限模式，未知返回空串

    只扫末尾几行（底栏位置）；模式切换 banner 也会出现在末尾，同样有效。
    多个模式 banner 同时出现时（切换动画残留），取最后出现的——
    从后往前扫，命中即返回。
    """
    if not output:
        return ""
    tail = output.strip().splitlines()[-12:]
    for ln in reversed(tail):
        for pat, mode in _MODE_PATTERNS:
            if pat.search(ln):
                return mode
    return ""


def next_mode(current: str) -> str:
    """返回 Shift+Tab 循环中 current 的下一个模式

    未知/空模式按 auto 起算（实测循环起点）。
    """
    if current not in MODE_CYCLE:
        return MODE_CYCLE[0]
    return MODE_CYCLE[(MODE_CYCLE.index(current) + 1) % len(MODE_CYCLE)]


def steps_to_mode(current: str, target: str) -> int:
    """计算从 current 切到 target 需要按几次 Shift+Tab（0 = 已在目标模式）

    未知/空 current 按 auto 起算。target 不在循环里（如 bypass-permissions）
    返回 -1（不可达）。
    """
    if target not in MODE_CYCLE:
        return -1
    if current not in MODE_CYCLE:
        current = MODE_CYCLE[0]
    cur = MODE_CYCLE.index(current)
    tgt = MODE_CYCLE.index(target)
    return (tgt - cur) % len(MODE_CYCLE)


def _collect_options(tail: list[str]) -> list[tuple[str, str]]:
    """从尾部收集对话框选项

    有框线时取最后一个框顶之后的选项行（精确）；无框线（❯ 列表）时
    从末尾向上取连续选项段，遇到正文行即停。
    """
    box_idx = [i for i, ln in enumerate(tail) if _BOX_TOP.search(ln)]
    if box_idx:
        seg = tail[box_idx[-1] + 1:]
    else:
        seg = []
        for ln in reversed(tail):
            if _OPTION_LINE.match(ln.strip()) or _DIALOG_TAIL.search(ln):
                seg.insert(0, ln)
            else:
                break
    options: list[tuple[str, str]] = []
    seen: set[str] = set()
    for ln in seg:
        m = _OPTION_LINE.match(ln.strip())
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            options.append((m.group(1), _clean(m.group(2))))
    return options[:_MAX_OPTIONS]


def _find_prompt(tail: list[str]) -> str:
    """在尾部找最后一行提示/问题文本（对话框标题）"""
    for ln in reversed(tail):
        if any(p.search(ln) for p in _PROMPT_PATTERNS):
            return _clean(ln.strip())
    return ""


def _clean(text: str) -> str:
    """清理选项/提示文本中的框线字符与多余空白"""
    cleaned = _BOX_CHARS.sub(" ", text)
    return re.sub(r"\s+", " ", cleaned).strip()
