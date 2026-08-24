"""统一的会话发送路由

按 session_type 把 send（文本+回车）/ enter / ctrl_c 派发到对应后端：
- terminal：用 tags.tty 精确定位 Terminal.app 的对应 tab（避免击键落到最前面的
  tab，修复 /send 把消息发错会话的问题）
- ide：按 app_name（回退 win_title）经 AppleScript 发送
- screen：经 tmux 发送

lark_bot 与 daemon 共用此函数，避免路由逻辑重复。
"""

from ide_control import session_tty


def _app_for_ide(session: dict) -> str:
    """IDE 会话的目标 app 名：app_name 优先，回退 win_title"""
    return session.get("app_name", "") or session.get("win_title", "") or ""


async def send_to_session(ide_ctrl, screen_mgr, session: dict, action: str, text: str = "") -> bool:
    """向指定 session 发送一个动作

    Args:
        ide_ctrl: IDEControl 实例
        screen_mgr: ScreenManager 实例
        session: 会话记录（registry.get/list 返回的 dict）
        action: "send"（文本+回车）/ "enter" / "ctrl_c"
        text: action=="send" 时的文本

    Returns:
        是否发送成功
    """
    stype = session.get("session_type", "screen")
    sid = session["id"]

    if stype in ("terminal", "standalone"):
        # terminal 优先按 tty 精确定位 tab；无 tty 才退化为发到最前 tab
        tty = session_tty(session)
        if tty:
            if action == "send":
                return ide_ctrl.send_keys_to_tty(tty, text)
            if action == "enter":
                return ide_ctrl.send_enter_to_tty(tty)
            if action == "ctrl_c":
                return ide_ctrl.send_ctrl_c_to_tty(tty)
            return False
        app = session.get("app_name", "") or "Terminal"
    elif stype == "ide":
        app = _app_for_ide(session)
    else:
        # screen 走 tmux
        if action == "send":
            return await screen_mgr.send_keys(sid, text)
        if action == "enter":
            return await screen_mgr.send_enter(sid)
        if action == "ctrl_c":
            return await screen_mgr.send_ctrl_c(sid)
        return False

    # ide 或无 tty 的 terminal：发到 app
    if action == "send":
        return ide_ctrl.send_keys(app, text)
    if action == "enter":
        return ide_ctrl.send_enter(app)
    if action == "ctrl_c":
        return ide_ctrl.send_ctrl_c(app)
    return False
