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
            rows.append(_cmd(f"/confirm {sid}"))
        rows.append({"tag": "hr"})

    if executing:
        rows.append(_md(f"🔵 **Executing / 执行中 ({len(executing)})**"))
        for s in executing:
            sid = s["id"][:8]
            label = _session_label(s)
            rows.append(_md(f"🔵 **{sid}** {label}"))
            rows.append(_cmd(f"/status {sid}"))

    if idle:
        rows.append(_md(f"⏸️ **Idle / 空闲 ({len(idle)})**"))
        for s in idle:
            sid = s["id"][:8]
            label = _session_label(s)
            rows.append(_md(f"⏸️ **{sid}** {label}"))
            rows.append(_cmd(f"/status {sid}"))

    if active_others:
        rows.append(_md(f"🟢 **Active / 活动 ({len(active_others)})**"))
        for s in active_others:
            sid = s["id"][:8]
            label = _session_label(s)
            rows.append(_md(f"🟢 **{sid}** {label}"))
            rows.append(_cmd(f"/status {sid}"))

    # 提示：IDE 类型 session 需要 /status 查看精确状态
    ide_count = sum(1 for s in active if s.get("session_type") == "ide")
    if ide_count > 0:
        rows.append({"tag": "hr"})
        rows.append(_md("💡 IDE 会话（🔌）需要用 `/status <id>` 查看精确状态。"))

    rows.append({"tag": "hr"})
    rows.append(_md("⬇️ Select & copy a command below / 长按选择复制命令"))
    rows.append(_cmd("/confirm-all"))

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
        # Filter: remove separator lines, prompt lines, and self-echo of commands
        lines = [l for l in output.splitlines()
                 if l.strip()
                 and not l.strip().startswith("─")
                 and not l.strip().startswith("❯")
                 and not l.strip().startswith("  ⏵")
                 and not l.strip().startswith("/status")]
        meaningful = "\n".join(lines[-15:]) if lines else output
        if meaningful.strip():
            elements.append(_md(f"**Output / 输出:**\n```\n{meaningful[-300:]}\n```"))

    elements.append({"tag": "hr"})
    elements.append(_md("⬇️ Select & copy / 长按选择复制"))

    elements.append(_md("✅ Confirm / 确认（继续执行）"))
    elements.append(_cmd(f"/confirm {sid}"))

    elements.append(_md("✋ Interrupt / 中断（Ctrl+C，停止当前命令，会话保留）"))
    elements.append(_cmd(f"/interrupt {sid}"))

    elements.append(_md("⏹️ Stop / 终止（关闭整个会话，不可恢复）"))
    elements.append(_cmd(f"/stop {sid}"))

    elements.append(_md("📤 Send / 发送命令（末尾补上具体命令）"))
    elements.append(_cmd(f"/send {sid} "))

    elements.append(_md("💬 Interactive / 交互模式（直接对话）"))
    elements.append(_cmd(f"/enter {sid}"))

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
        rows.append(_cmd(f"/confirm {i}"))
    rows.append({"tag": "hr"})
    rows.append(_md("⬇️ Confirm all / 一键确认全部"))
    rows.append(_cmd("/confirm-all"))
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
    app = s.get("app_name", "")
    cwd = s.get("cwd", "")
    proj = cwd.split("/")[-1] if cwd else ""
    tags = s.get("tags", {})
    tty = ""
    if isinstance(tags, dict):
        tty = tags.get("tty", "")
    icon = "🔌" if stype == "ide" else "💻"
    if stype == "ide" and app:
        base = f"{icon} {app} — {proj}" if proj else f"{icon} {app}"
    else:
        base = f"{icon} Terminal — {proj}" if proj else f"{icon} Terminal"
    if tty:
        base += f" ({tty})"
    return base