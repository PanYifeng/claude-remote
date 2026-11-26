"""Lark Interactive Card Builder

Cards show session info with commands on separate lines.
Mobile users can long-press a command line to select and copy it.
Bilingual (EN/CN) labels.
"""

import json
import os
import re
from dataclasses import dataclass


def session_name(s: dict) -> str:
    """读取会话的显示名（Claude Code 自动命名，如 patent-chunks-vector-recall）

    来源是 per-process 状态文件 ~/.claude/sessions/<pid>.json 的 name 字段
    （nameSource=auto 时由 Claude Code 按任务内容生成），比 类型—目录
    更能区分同项目的多个会话。opencode / 无状态文件时返回空串。

    Args:
        s: session 记录

    Returns:
        显示名；无则空串
    """
    if s.get("agent", "claude") != "claude":
        return ""
    pid = s.get("pid", 0)
    if not pid:
        return ""
    fp = os.path.join(os.path.expanduser("~"), ".claude", "sessions", f"{pid}.json")
    try:
        with open(fp, encoding="utf-8") as f:
            name = json.load(f).get("name", "")
        return name.strip() if isinstance(name, str) else ""
    except Exception:
        return ""


def _session_headline(s: dict) -> str:
    """会话条目标题行：有自动命名时用名字，否则用 类型—目录

    名字一行过长时截断（卡片窄屏），完整信息在详情卡。
    """
    name = session_name(s)
    label = _session_label(s)
    if not name:
        return label
    if len(name) > 28:
        name = name[:27] + "…"
    return f"{_session_icon(s)} **{name}**"


def _session_icon(s: dict) -> str:
    """会话类型图标（🟦 OpenCode / 🔌 IDE / 💻 Terminal）"""
    if s.get("agent", "claude") == "opencode":
        return "🟦"
    return "🔌" if s.get("session_type") == "ide" else "💻"


def session_list_card(sessions: list[dict]) -> str:
    active = [s for s in sessions if s.get("status") != "stopped"]
    if not active:
        active = sessions[:5]

    # Sort by session ID for stable ordering
    active.sort(key=lambda s: s["id"])

    waiting = [s for s in active if s.get("status") == "waiting"]
    executing = [s for s in active if s.get("status") == "executing"]
    idle = [s for s in active if s.get("status") == "idle"]
    active_others = [s for s in active if s.get("status") not in ("waiting", "executing", "idle")]

    rows = []

    if waiting:
        rows.append(_md(f"🟡 **Waiting / 待确认 ({len(waiting)})**"))
        for s in waiting:
            rows.append(_md(_session_headline(s)))
            rows.append(_md(f"　　`{s['id'][:8]}` · {_session_label(s, with_icon=False)}"))
            rows.append(_btn_row([
                _btn("✅ 确认", "confirm", s["id"], "primary"),
                _btn("📊 详情", "status", s["id"]),
            ]))
        rows.append({"tag": "hr"})

    if executing:
        rows.append(_md(f"🔵 **Executing / 执行中 ({len(executing)})**"))
        for s in executing:
            rows.append(_md(_session_headline(s)))
            rows.append(_md(f"　　`{s['id'][:8]}` · {_session_label(s, with_icon=False)}"))
            rows.append(_btn_row([_btn("📊 详情", "status", s["id"])]))

    if idle:
        rows.append(_md(f"⏸️ **Idle / 空闲 ({len(idle)})**"))
        for s in idle:
            rows.append(_md(_session_headline(s)))
            rows.append(_md(f"　　`{s['id'][:8]}` · {_session_label(s, with_icon=False)}"))
            rows.append(_btn_row([_btn("📊 详情", "status", s["id"])]))

    if active_others:
        rows.append(_md(f"🟢 **Active / 活动 ({len(active_others)})**"))
        for s in active_others:
            rows.append(_md(_session_headline(s)))
            rows.append(_md(f"　　`{s['id'][:8]}` · {_session_label(s, with_icon=False)}"))
            rows.append(_btn_row([_btn("📊 详情", "status", s["id"])]))

    # 提示：IDE 类型 session 需要 /status 查看精确状态
    ide_count = sum(1 for s in active if s.get("session_type") == "ide")
    if ide_count > 0:
        rows.append({"tag": "hr"})
        rows.append(_md("💡 IDE 会话（🔌）需要用 `/status <id>` 查看精确状态。"))

    rows.append({"tag": "hr"})
    rows.append(_btn_row([
        _btn("🔄 刷新", "list", "", "primary"),
        _btn("🟡 待确认", "pending", ""),
        _btn("✅ 确认全部", "confirm-all", ""),
    ]))

    title = f"🤖 {len(active)} sessions"
    parts = []
    if waiting: parts.append(f"🟡{len(waiting)}")
    if executing: parts.append(f"🔵{len(executing)}")
    if idle: parts.append(f"⏸️{len(idle)}")
    if active_others: parts.append(f"🟢{len(active_others)}")
    title += " · " + " ".join(parts) if parts else ""

    card = {
        "config": {"wide_screen_mode": False},
        "header": {"title": {"tag": "lark_md", "content": title}},
        "elements": rows,
    }
    return json.dumps(card, ensure_ascii=False)


def session_status_card(s: dict, output: str = "", idx: int = -1,
                        decision: dict | None = None, mode: str = "",
                        model: str = "", model_groups: dict | None = None) -> str:
    label = _session_label(s)
    status = s.get("status", "unknown")
    cwd = s.get("cwd", "")
    stype = s.get("session_type", "screen")
    status_text = {"running": "🟢 Running / 运行中", "waiting": "🟡 Waiting / 待确认",
                   "executing": "🔵 Executing / 执行中", "idle": "⏸️ Idle / 空闲",
                   "stopped": "🔴 Stopped / 已停止"}.get(status, status)
    sid = s["id"][:8]

    elements = [_md(f"**Status / 状态:** {status_text}\n**Dir / 目录:** `{cwd}`")]

    # 会话名（Claude Code 自动命名）放在最前面，方便一眼确认是哪个任务
    name = session_name(s)
    if name:
        elements.insert(0, _md(f"**Name / 名称:** {name}"))


    if model:
        elements.append(_md(f"**Model / 模型:** `{model}`"))

    if mode:
        from decision_detect import MODE_LABELS
        mode_text = MODE_LABELS.get(mode, mode)
        elements.append(_md(f"**Mode / 模式:** {mode_text}"))

    if stype == "ide" and status == "running":
        elements.append(_md("💡 IDE 会话无法自动检测状态。再次发送 `/status` 可读取终端内容获取精确状态。"))

    if output:
        # 展示末轮完整内容：截断到 5000 字符（约等于一整轮 assistant 输出），
        # 优先保尾部——等待提示/最新结论总在末尾
        display = output[-_MAX_OUTPUT_CHARS:]
        if len(output) > _MAX_OUTPUT_CHARS:
            display = "…（前文截断）\n" + display
        if display.strip():
            elements.append(_md(f"**Output / 输出:**\n```\n{display}\n```"))

    # 决策对话框：按类型给出针对性按钮（选项直选 / y-n / 确认）
    if decision and decision.get("kind") != "none":
        elements.extend(_decision_elements(decision, s["id"]))

    # ── 区 1：会话操作（确认/中断/停止 + 发送/交互/刷新）──
    elements.append({"tag": "hr"})
    elements.append(_md("**▶️ 运行控制**"))
    elements.append(_btn_row([
        _btn("✅ 确认", "confirm", s["id"], "primary"),
        _btn("✋ 中断", "interrupt", s["id"]),
        _btn("⏹️ 停止", "stop", s["id"]),
    ]))
    elements.append(_btn_row([
        _btn("📤 发送…", "compose", s["id"]),
        _btn("💬 交互", "enter", s["id"]),
        _btn("🔄 刷新", "status", s["id"]),
    ]))

    # ── 区 2：TUI 快捷命令（/clear /compact /help）──
    if s.get("agent", "claude") == "claude":
        elements.append({"tag": "hr"})
        elements.append(_md("**🧰 快捷命令**"))
        elements.append(_btn_row(_tui_cmd_buttons(s["id"])))

    # ── 区 3：权限模式（Claude Code：Shift+Tab 循环）+ Esc ──
    if stype != "ide" and s.get("agent", "claude") == "claude":
        elements.append({"tag": "hr"})
        elements.append(_md("**🔑 权限模式**"))
        # 直达四个循环模式（当前模式高亮为 primary，其余 default）
        from decision_detect import MODE_CYCLE, MODE_LABELS
        mode_btns = [
            _btn(MODE_LABELS[m], "set-mode", s["id"],
                 "primary" if m == mode else "default", {"m": m})
            for m in MODE_CYCLE
        ]
        elements.append(_btn_row(mode_btns[:2]))
        elements.append(_btn_row(mode_btns[2:]))
        elements.append(_btn_row([
            _btn("🔁 循环一步", "cycle-mode", s["id"]),
            _btn("🚫 Esc", "esc", s["id"]),
        ]))

    # ── 区 4：模型切换 ────────────────────────
    if s.get("agent", "claude") == "claude":
        elements.append({"tag": "hr"})
        elements.append(_md("**🤖 模型**"))
        # 分组由调用方预取传入（含网络 IO，不在卡片构建里拉）；
        # 未传时回退内置精选
        if model_groups is None:
            from model_control import _CURATED_MODEL_GROUPS as model_groups
        # 卡片只展示前 5 个家族（避免过长），完整列表走 /models 命令
        shown_groups = dict(list(model_groups.items())[:5])
        if len(model_groups) > 5:
            shown_groups.setdefault("…", [])
        # 按分组友好展示，当前模型高亮
        for group_name, group_models in shown_groups.items():
            btns = []
            for m in group_models:
                short = m.split("/")[-1]
                btns.append(_btn(short, "set-model", s["id"],
                                 "primary" if m == model else "default", {"m": m}))
            if btns:
                elements.append(_md(f"**{group_name}**"))
                elements.append(_btn_row(btns[:3]))
                if len(btns) > 3:
                    elements.append(_btn_row(btns[3:]))
        # 手动输入与完整列表入口
        elements.append(_md(
            f"✏️ 其他模型: `/setmodel {sid} <model>` 手动输入"
            f"（不区分大小写）· `/models` 查看全部"))

    elements.append({"tag": "hr"})
    # 保留一条可复制的 /send 命令行（compose 模式之外的手动用法）
    elements.append(_cmd(f"/send {sid} "))


    card = {
        "config": {"wide_screen_mode": False},
        "header": {"title": {"tag": "lark_md", "content": f"📊 [{sid}] {name or label}"}},
        "elements": elements,
    }
    return json.dumps(card, ensure_ascii=False)


# 状态卡输出最多展示的字符数（末轮完整会话优先）
_MAX_OUTPUT_CHARS = 5000

# 交互卡构建参数与构建函数（见文件末尾 InteractiveState / interactive_chat_card）


def _decision_elements(decision: dict, sid: str) -> list[dict]:
    """把识别出的决策对话框渲染成针对性按钮组

    - options/multi：选项全文逐行列在正文（不截断），按钮只放序号短标签
      ——真实选项文本常达 60-70 字符（"Yes, and always allow access to
      /tmp..."），塞进按钮 label 会被砍成碎片，全文放正文可完整阅读
    - yn：Yes / No 按钮（发送 y / n）
    - confirm：单个确认按钮（Enter）
    """
    kind = decision.get("kind", "none")
    title = decision.get("title", "")
    hint = decision.get("hint", "")
    rows: list[dict] = [{"tag": "hr"}]
    if title:
        rows.append(_md(f"🎯 **{title}"))
    if hint:
        rows.append(_md(f"_{hint}_"))

    if kind in ("options", "multi") and decision.get("options"):
        # 选项全文逐行展示（正文行不截断，长选项可完整阅读）
        for num, text in decision["options"]:
            rows.append(_md(f"**{num}.** {text}"))
        # 按钮短标签必须互相可区分：同关键词的选项（1 Yes / 2 Yes / 3 Yes）
        # 按钮上补差异词（always / auto mode...），仍冲突则只放序号
        shorts = _distinct_shorts(decision["options"])
        for i in range(0, len(opts := decision["options"]), 4):
            chunk = opts[i:i + 4]
            rows.append(_btn_row([
                _btn(f"{num} {shorts[num]}", "select-option", sid, "primary", {"o": num})
                for num, _text in chunk
            ]))
    elif kind == "yn":
        rows.append(_btn_row([
            _btn("✅ Yes", "send-key", sid, "primary", {"k": "y"}),
            _btn("❌ No", "send-key", sid, "default", {"k": "n"}),
        ]))
    elif kind == "confirm":
        rows.append(_btn_row([
            _btn("✅ 确认", "confirm", sid, "primary"),
            _btn("🚫 取消", "esc", sid),
        ]))
    return rows


def _option_short(text: str) -> str:
    """选项文本 → 按钮短标签（≤6 字符）

    优先取有辨识度的词：Yes/No/Always/Reject 等关键词直接用；
    否则截前 6 个字符。全文在正文行展示，按钮只需可辨认。
    """
    lower = text.lower()
    for kw in ("yes", "no", "always", "reject", "allow", "deny", "never", "stop", "skip"):
        if kw in lower:
            return kw.capitalize()
    # 无关键词：截前 6 个字符（中文按字符数同样适用）
    return text[:6] if len(text) <= 6 else text[:5] + "…"


def _distinct_shorts(options: list[tuple[str, str]]) -> dict[str, str]:
    """给每个选项生成互相可区分的按钮短标签

    规则（逐级加强直到无冲突）：
    1. 基础短标签（关键词/前缀）
    2. 冲突时从选项全文里挖差异词拼在基础标签后，只拼到能区分为止
       （每轮比较一次，已无冲突即停，避免标签越拼越长）
    3. 仍冲突则放弃文字区分，按钮只放序号（正文行有全文可对照）
    """
    shorts = {num: _option_short(text) for num, text in options}
    words = {num: _distinct_words(text) for num, text in options}

    def _has_conflict() -> bool:
        seen = set()
        for short in shorts.values():
            if short in seen:
                return True
            seen.add(short)
        return False

    # 逐轮拼差异词：每轮每个冲突选项追加下一个差异词，无冲突即停
    for _round in range(4):
        if not _has_conflict():
            break
        by_short: dict[str, list[str]] = {}
        for num, short in shorts.items():
            by_short.setdefault(short, []).append(num)
        for _short, nums in by_short.items():
            if len(nums) < 2:
                continue
            for num in nums:
                extra = words.get(num, [])
                if _round < len(extra):
                    shorts[num] = f"{shorts[num]}·{extra[_round]}"
    # 仍冲突：退化为空标签（按钮只显示序号）
    if _has_conflict():
        seen: dict[str, str] = {}
        for num, short in shorts.items():
            if short in seen.values():
                shorts[num] = ""
            else:
                seen[num] = short
    return shorts


def _distinct_words(text: str) -> list[str]:
    """从选项文本提取有实义的差异词（去掉所有选项共有的常见词）"""
    # 常见虚词/共有词，不具区分度
    _STOP = {"yes", "no", "and", "the", "a", "an", "to", "for", "this",
             "that", "in", "on", "of", "with", "you", "it", "is", "are"}
    words = []
    for w in re.findall(r"[a-zA-Z]{3,}", text.lower()):
        if w not in _STOP and w not in words:
            words.append(w)
    return words[:4]


def pending_card(sessions: list[dict]) -> str:
    if not sessions:
        return done_card("✅ No sessions waiting for input. / 无待确认会话")
    rows = []
    for i, s in enumerate(sessions, 1):
        rows.append(_md(_session_headline(s)))
        rows.append(_md(f"　　`{s['id'][:8]}` · {_session_label(s, with_icon=False)}"))
        rows.append(_btn_row([
            _btn("✅ 确认", "confirm", s["id"], "primary"),
            _btn("📊 详情", "status", s["id"]),
        ]))
    rows.append({"tag": "hr"})
    rows.append(_btn_row([_btn("✅ 一键确认全部", "confirm-all", "", "primary")]))
    card = {
        "config": {"wide_screen_mode": False},
        "header": {"title": {"tag": "lark_md", "content": f"🟡 {len(sessions)} waiting / 待确认"}},
        "elements": rows,
    }
    return json.dumps(card, ensure_ascii=False)


def confirm_all_card(success: int, failed: int) -> str:
    lines = []
    if success: lines.append(f"✅ Confirmed {success}")
    if failed: lines.append(f"❌ Failed {failed}")
    return done_card("\n".join(lines) if lines else "Done. / 完成")


def done_card(text: str) -> str:
    card = {
        "config": {"wide_screen_mode": False},
        "header": {"title": {"tag": "lark_md", "content": "🤖 Claude Remote"}},
        "elements": [_md(text)],
    }
    return json.dumps(card, ensure_ascii=False)


def _md(content: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _cmd(cmd: str) -> dict:
    """Command displayed on its own line for easy selection & copy on mobile"""
    return {"tag": "div", "text": {"tag": "lark_md", "content": cmd}}


def _btn(label: str, action: str, session_id: str = "", btn_type: str = "default",
         extra: dict | None = None) -> dict:
    """交互卡片按钮：点击经 card.action.trigger 事件回调到 bot

    value 里的 a=动作名、s=会话 ID，与 lark_bot.handle_card_event 约定一致；
    extra 里的附加参数（如 o=选项号、k=按键文本）原样并入 value。
    """
    value: dict = {"a": action}
    if session_id:
        value["s"] = session_id
    if extra:
        value.update(extra)
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": btn_type,
        "value": value,
    }


def _btn_row(buttons: list[dict]) -> dict:
    """一行按钮（Lark action 元素，单行最多放 3 个较稳）"""
    return {"tag": "action", "actions": buttons}


# 交互卡输出区最多展示的字符数（优先保尾部——等待提示/最新结论总在末尾）
_INTERACTIVE_MAX_CHARS = 3500


@dataclass
class InteractiveState:
    """交互卡构建参数（lark_bot 交互模式每轮透传给卡片）

    history 是 [(方向, 文本)] 列表：方向 "in" = 用户发送，"out" = 会话输出。
    其余字段用于渲染状态行与操作按钮。
    """

    session: dict
    history: list[tuple[str, str]]
    status: str = ""          # executing / waiting / idle / ...
    mode: str = ""            # 权限模式名（decision_detect.MODE_*）
    model: str = ""           # 当前模型
    model_groups: dict | None = None  # 网关模型分组（按钮快速切换用）
    done: bool = False        # True = 本轮输出已稳定


def _status_line(st: InteractiveState) -> str:
    """交互卡状态行：状态 + 模式 + 模型（有则展示）"""
    from decision_detect import MODE_LABELS
    icons = {"executing": "🔵 执行中", "waiting": "🟡 待确认", "idle": "⏸️ 空闲",
             "running": "🟢 运行中", "stopped": "🔴 已停止"}
    parts = [icons.get(st.status, f"ℹ️ {st.status or '未知'}")]
    if st.mode:
        parts.append(f"模式: {MODE_LABELS.get(st.mode, st.mode)}")
    if st.model:
        parts.append(f"模型: `{st.model}`")
    return " · ".join(parts)


def _history_elements(st: InteractiveState) -> list[dict]:
    """把交互历史渲染成卡片元素：用户消息引用块 + 会话输出代码块

    每轮输出整体截尾到 _INTERACTIVE_MAX_CHARS，历史多轮时只保最近
    3 轮（移动端卡片过长会折叠，最近上下文足够阅读）。
    """
    elements: list[dict] = []
    history = st.history[-3:]
    for i, (direction, text) in enumerate(history):
        if direction == "in":
            elements.append({"tag": "hr"})
            elements.append(_md(f"**📩 你:** {text}"))
        else:
            display = text[-_INTERACTIVE_MAX_CHARS:]
            if len(text) > _INTERACTIVE_MAX_CHARS:
                display = "…（前文截断）\n" + display
            if display.strip():
                elements.append(_md(f"**🤖 会话输出**\n```\n{display}\n```"))
    return elements


# TUI 斜杠快捷命令按钮定义（与 lark_bot._TUI_COMMANDS 保持一致；
# 不直接 import 避免循环依赖——lark_bot import lark_card）
_TUI_COMMANDS: dict[str, str] = {
    "clear": "🧹 /clear",
    "compact": "📦 /compact",
    "help": "❓ /help",
}


def _tui_cmd_buttons(sid: str) -> list[dict]:
    """TUI 斜杠快捷命令按钮（/clear /compact 等）"""
    return [
        _btn(label, "tui-cmd", sid, "default", {"c": cmd})
        for cmd, label in _TUI_COMMANDS.items()
    ]


def interactive_chat_card(st: InteractiveState) -> str:
    """交互模式聊天卡：历史消息 + 实时状态 + Claude Code 操作按钮

    布局按功能分区（操作区 / 会话管理区 / 模式区 / 模型区 / 命令区），
    每区一条 hr 分隔线 + 区标题，避免不同类别的按钮混在一起。
    每次输出更新都整卡原位刷新（PATCH 同一 message_id）。
    """
    s = st.session
    sid = s["id"][:8]
    name = session_name(s) or s.get("name") or sid
    is_claude = s.get("agent", "claude") == "claude"
    elements = [_md(f"**会话:** {name} · `{sid}`")]

    elements.extend(_history_elements(st))
    elements.append({"tag": "hr"})
    icon = "✅" if st.done else "⏳"
    elements.append(_md(f"{icon} {_status_line(st)}"))

    # 决策对话框：待确认时展示针对性按钮（选项直选 / y-n / 确认）
    decision = st_decision_cache.get(s["id"])
    if decision and decision.get("kind") != "none":
        elements.extend(_decision_elements(decision, s["id"]))

    # ── 区 1：运行控制（确认/中断/刷新）────────────
    elements.append({"tag": "hr"})
    elements.append(_md("**▶️ 运行控制**"))
    elements.append(_btn_row([
        _btn("✅ 确认", "confirm", s["id"], "primary"),
        _btn("✋ 中断", "interrupt", s["id"]),
        _btn("🔄 刷新", "irefresh", s["id"]),
    ]))

    # ── 区 2：会话管理（退出/Esc/停止/TUI 命令）─────
    elements.append({"tag": "hr"})
    elements.append(_md("**🗂️ 会话管理**"))
    elements.append(_btn_row([
        _btn("🚪 退出交互", "exit-interactive", s["id"]),
        _btn("🚫 Esc", "esc", s["id"]),
        _btn("⏹️ 停止", "stop", s["id"]),
    ]))
    if is_claude:
        # TUI 斜杠快捷命令（/clear 清空上下文 /compact 压缩等）
        elements.append(_btn_row(_tui_cmd_buttons(s["id"])))

    # ── 区 3：权限模式（Claude Code）──────────────
    if s.get("session_type") != "ide" and is_claude:
        elements.append({"tag": "hr"})
        elements.append(_md("**🔑 权限模式**"))
        from decision_detect import MODE_CYCLE, MODE_LABELS
        mode_btns = [
            _btn(MODE_LABELS[m], "set-mode", s["id"],
                 "primary" if m == st.mode else "default", {"m": m})
            for m in MODE_CYCLE
        ]
        elements.append(_btn_row(mode_btns[:2]))
        elements.append(_btn_row(mode_btns[2:]))

    # ── 区 4：模型切换 ──────────────────────────
    if is_claude:
        elements.append({"tag": "hr"})
        elements.append(_md("**🤖 模型**"))
        groups = st.model_groups
        if groups is None:
            from model_control import _CURATED_MODEL_GROUPS as groups
        for group_name, group_models in list(groups.items())[:3]:
            btns = [
                _btn(m.split("/")[-1], "set-model", s["id"],
                     "primary" if m == st.model else "default", {"m": m})
                for m in group_models[:3]
            ]
            if btns:
                elements.append(_btn_row(btns))

    # ── 底部提示 ───────────────────────────────
    elements.append({"tag": "hr"})
    elements.append(_md(
        "💬 直接回复消息即发送到会话 · `/exit` 退出交互 · `/exit --kill` 停止"))

    card = {
        "config": {"wide_screen_mode": False},
        "header": {"title": {"tag": "lark_md",
                             "content": f"💬 交互 — {name}"}},
        "elements": elements,
    }
    return json.dumps(card, ensure_ascii=False)


# 决策对话框缓存：交互卡渲染时由 lark_bot 塞入（卡片构建是纯函数，
# 不做终端读取；None = 未读取过）
st_decision_cache: dict[str, dict | None] = {}


def interactive_card(cmd: str, output: str) -> str:
    """Deprecated: 旧版交互结果卡，仅保留兼容（现用 interactive_chat_card）"""
    display = output[-500:] if output else "(no output yet)"
    card = {
        "config": {"wide_screen_mode": False},
        "header": {"title": {"tag": "lark_md", "content": f"💬 {cmd}"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**Output / 输出:**\n```\n{display}\n```"}},
            {"tag": "div", "text": {"tag": "lark_md", "content": "⬇️ Continue or exit / 继续或退出\n`/exit`  `/exit --kill`"}},
        ],
    }
    return json.dumps(card, ensure_ascii=False)


def streaming_card(cmd: str, output: str, done: bool = False) -> str:
    """Deprecated: 旧版流式输出卡，仅保留兼容（现用 interactive_chat_card）"""
    icon = "✅" if done else "⏳"
    status = "Done" if done else "Executing..."
    display = output[-500:] if output else "(waiting for output...)"
    card = {
        "config": {"wide_screen_mode": False},
        "header": {"title": {"tag": "lark_md", "content": f"{icon} {cmd}"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**{status} / 执行中:**\n```\n{display}\n```"}},
            {"tag": "div", "text": {"tag": "lark_md", "content": "⬇️ Continue or exit / 继续或退出\n`/exit`  `/exit --kill`"}},
        ],
    }
    return json.dumps(card, ensure_ascii=False)


def _session_label(s: dict, with_icon: bool = True) -> str:
    """会话的类型—目录标签（如 "💻 Terminal — data-infra-search (ttys000)"）

    with_icon=False 用于已有图标前缀的场景（列表卡条目第二行），
    避免同一条目上图标出现两次。
    """
    stype = s.get("session_type", "screen")
    agent = s.get("agent", "claude")
    app = s.get("app_name", "")
    cwd = s.get("cwd", "")
    proj = cwd.split("/")[-1] if cwd else ""
    tags = s.get("tags", {})
    tty = ""
    if isinstance(tags, dict):
        tty = tags.get("tty", "")
    # opencode 用 🟦 区分；claude 用 💻/🔌
    if agent == "opencode":
        icon = "🟦"
    else:
        icon = "🔌" if stype == "ide" else "💻"
    prefix = f"{icon} " if with_icon else ""
    if stype == "ide" and app:
        base = f"{prefix}{app} — {proj}" if proj else f"{prefix}{app}"
    else:
        kind = "OpenCode" if agent == "opencode" else "Terminal"
        base = f"{prefix}{kind} — {proj}" if proj else f"{prefix}{kind}"
    if tty:
        base += f" ({tty})"
    return base