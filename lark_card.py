"""Lark Interactive Card Builder

Cards show session info with commands on separate lines.
Mobile users can long-press a command line to select and copy it.
Bilingual (EN/CN) labels.
"""

import json


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


def session_status_card(s: dict, output: str = "", idx: int = -1) -> str:
    label = _session_label(s)
    status = s.get("status", "unknown")
    cwd = s.get("cwd", "")
    stype = s.get("session_type", "screen")
    status_text = {"running": "🟢 Running / 运行中", "waiting": "🟡 Waiting / 待确认",
                   "executing": "🔵 Executing / 执行中", "idle": "⏸️ Idle / 空闲",
                   "stopped": "🔴 Stopped / 已停止"}.get(status, status)
    sid = s["id"][:8]

    elements = [_md(f"**Status / 状态:** {status_text}\n**Dir / 目录:** `{cwd}`")]

    if stype == "ide" and status == "running":
        elements.append(_md("💡 IDE 会话无法自动检测状态。再次发送 `/status` 可读取终端内容获取精确状态。"))

    if output:
        # 直接取末尾内容展示。等待提示（"Do you want to proceed?"、选项列表、
        # "Esc to cancel"）通常就在末尾几行，不能像之前那样砍掉最后 4 行。
        display = output[-500:]
        if display.strip():
            elements.append(_md(f"**Output / 输出:**\n```\n{display}\n```"))

    elements.append({"tag": "hr"})
    # 快捷操作按钮：点击直接执行，无需手动复制命令（value 用完整会话 ID）
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
    # 保留一条可复制的 /send 命令行（compose 模式之外的手动用法）
    elements.append(_cmd(f"/send {sid} "))


    card = {
        "config": {"wide_screen_mode": False},
        "header": {"title": {"tag": "lark_md", "content": f"📊 [{sid}] {label}"}},
        "elements": elements,
    }
    return json.dumps(card, ensure_ascii=False)


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


def _btn(label: str, action: str, session_id: str = "", btn_type: str = "default") -> dict:
    """交互卡片按钮：点击经 card.action.trigger 事件回调到 bot

    value 里的 a=动作名、s=会话 ID，与 lark_bot.handle_card_event 约定一致。
    """
    value: dict = {"a": action}
    if session_id:
        value["s"] = session_id
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