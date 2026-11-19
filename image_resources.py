"""飞书消息图片资源处理：下载 img_v3 资源并转成本地文件路径

飞书消息里的图片是 ![Image](img_v3_xxx) 引用，直接把这段 markdown
发给终端会话没有意义（智能体看不到图）。此模块把消息里的图片下载到
本地，替换成文件路径引用，让 Claude Code 能直接读图。

下载走 lark-cli im +messages-resources-download（--as user 身份，
bot 身份无法下载用户发的图片资源）。
"""

import json
import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# 图片下载目录（按消息分组，避免重名覆盖）
_IMAGE_DIR = Path.home() / ".claude-remote" / "images"

# markdown 图片引用：![Image](img_v3_xxx)
_IMG_REF = re.compile(r"!\[[^\]]*\]\((img_v3_[A-Za-z0-9_-]+)\)")
# 下载超时（秒）
_DOWNLOAD_TIMEOUT = 15


def has_image_refs(text: str) -> bool:
    """文本里是否含飞书图片引用"""
    return bool(_IMG_REF.search(text))


def replace_image_refs(text: str, message_id: str) -> str:
    """把文本里的图片引用替换成本地文件路径

    下载失败时该引用替换为占位说明（不阻断消息发送）。

    Args:
        text: 消息文本（可能含 ![Image](img_v3_xxx)）
        message_id: 飞书消息 ID（om_xxx），用于下载资源

    Returns:
        替换后的文本；无图片引用时原样返回
    """
    if not has_image_refs(text):
        return text
    keys = _IMG_REF.findall(text)
    for key in keys:
        local = _download_image(message_id, key)
        if local:
            text = text.replace(f"![Image]({key})", f"（图片已下载: {local}）")
        else:
            text = text.replace(f"![Image]({key})", f"（图片 {key} 下载失败）")
    return text


def _download_image(message_id: str, file_key: str) -> str:
    """下载单张图片到本地，返回文件路径（失败返回空串）

    下载目录按 message_id 分组；同名文件跳过下载直接复用。
    """
    if not message_id:
        return ""
    out_dir = _IMAGE_DIR / message_id
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["lark-cli", "im", "+messages-resources-download",
             "--message-id", message_id,
             "--file-key", file_key,
             "--type", "image",
             "--as", "user",
             "--output", f"{message_id}/{file_key}"],
            capture_output=True, text=True, timeout=_DOWNLOAD_TIMEOUT,
            cwd=str(_IMAGE_DIR),
        )
        if result.returncode != 0:
            logger.warning("download image %s failed: %s", file_key, result.stderr[:200])
            return ""
        resp = json.loads(result.stdout)
        saved = resp.get("data", {}).get("saved_path", "")
        if saved:
            return str(Path(saved).resolve())
    except Exception as e:
        logger.warning("download image %s error: %s", file_key, e)
    return ""
