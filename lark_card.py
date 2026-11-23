"""Lark Interactive Card Builder

Cards show session info with commands on separate lines.
Mobile users can long-press a command line to select and copy it.
Bilingual (EN/CN) labels.
"""

import json
import re


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
            sid = s["id"][:8]
            label = _session_label(s)
            rows.append(_md(f"🟡 **{sid}** {label}"))
            rows.append(_btn_row([
                _btn("✅ 确认", "confirm", s["id"], "primary"),
                _btn("📊 详情", "status", s["id"]),
            ]))
        rows.append({"tag": "hr"})

    if executing:
        rows.append(_md(f"🔵 **Executing / 执行中 ({len(executing)})**"))
        for s in executing:
            sid = s["id"][:8]
            label = _session_label(s)
            rows.append(_md(f"🔵 **{sid}** {label}"))
            rows.append(_btn_row([_btn("📊 详情", "status", s["id"])]))

    if idle:
        rows.append(_md(f"⏸️ **Idle / 空闲 ({len(idle)})**"))
        for s in idle:
            sid = s["id"][:8]
            label = _session_label(s)
            rows.append(_md(f"⏸️ **{sid}** {label}"))
            rows.append(_btn_row([_btn("📊 详情", "status", s["id"])]))

    if active_others:
        rows.append(_md(f"🟢 **Active / 活动 ({len(active_others)})**"))
        for s in active_others:
            sid = s["id"][:8]
            label = _session_label(s)
            rows.append(_md(f"🟢 **{sid}** {label}"))
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

    elements.append({"tag": "hr"})
    # ── 会话操作 ─────────────────────────────
    elements.append(_md("**操作**"))
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

    # ── 权限模式（Claude Code：Shift+Tab 循环）+ Esc ──
    if stype != "ide" and s.get("agent", "claude") == "claude":
        elements.append({"tag": "hr"})
        elements.append(_md("**权限模式**"))
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

    # ── 模型切换 ────────────────────────────
    if s.get("agent", "claude") == "claude":
        elements.append({"tag": "hr"})
        elements.append(_md("**模型**"))
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
        "header": {"title": {"tag": "lark_md", "content": f"📊 [{sid}] {label}"}},
        "elements": elements,
    }
    return json.dumps(card, ensure_ascii=False)


# 状态卡输出最多展示的字符数（末轮完整会话优先）
_MAX_OUTPUT_CHARS = 5000


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
        label = _session_label(s)
        rows.append(_md(f"🟡 **{label}**"))
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


def interactive_card(cmd: str, output: str) -> str:
    """Card showing the result of an interactive command"""
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
    """Card showing streaming output, updated in-place"""
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


def _session_label(s: dict) -> str:
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
    if stype == "ide" and app:
        base = f"{icon} {app} — {proj}" if proj else f"{icon} {app}"
    else:
        kind = "OpenCode" if agent == "opencode" else "Terminal"
        base = f"{icon} {kind} — {proj}" if proj else f"{icon} {kind}"
    if tty:
        base += f" ({tty})"
    return base