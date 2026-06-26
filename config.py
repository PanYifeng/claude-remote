"""Claude Code Shell 远程控制系统 — 配置模块"""

import os
import pathlib
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Config:
    """从环境变量读取配置"""

    # Daemon 端口
    daemon_port: int = int(os.getenv("CCR_DAEMON_PORT", "9998"))

    # 数据目录（SQLite 数据库、运行时文件）
    data_dir: pathlib.Path = pathlib.Path(
        os.getenv("CCR_DATA_DIR", pathlib.Path.home() / ".claude-remote")
    ).expanduser().resolve()

    # Claude 二进制路径
    claude_bin: str = os.getenv("CCR_CLAUDE_BIN", "claude")

    # Lark 应用凭证
    lark_app_id: Optional[str] = os.getenv("LARK_APP_ID")
    lark_app_secret: Optional[str] = os.getenv("LARK_APP_SECRET")

    # Lark 消息接收者（用户 open_id / chat_id）
    lark_user_id: Optional[str] = os.getenv("CCR_LARK_USER_ID")

    # Cloudflare 隧道域名（可选，用于 Lark webhook 验证回调）
    tunnel_domain: Optional[str] = os.getenv("CCR_TUNNEL_DOMAIN")

    # 日志文件路径
    log_path: str = os.getenv("CCR_LOG_PATH", "/tmp/claude-daemon.log")

    # 监控配置
    health_check_interval: float = 5.0   # 健康检查间隔（秒）
    waiting_timeout: float = 5.0         # 判断为 waiting 的空闲超时（秒）
    output_tail_lines: int = 50           # 读取 log 尾部行数

    # log 文件目录
    log_dir: pathlib.Path = field(init=False)

    def __post_init__(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "sessions.db"
        self.log_dir = self.data_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self) -> pathlib.Path:
        return self._db_path

    @db_path.setter
    def db_path(self, value: pathlib.Path):
        self._db_path = value


# 全局单例
config = Config()