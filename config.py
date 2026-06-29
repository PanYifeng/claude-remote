"""Claude Code Remote Control System — Configuration module"""

import os
import pathlib
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Config:
    """Read configuration from environment variables"""

    daemon_port: int = int(os.getenv("CCR_DAEMON_PORT", "9998"))
    data_dir: pathlib.Path = pathlib.Path(
        os.getenv("CCR_DATA_DIR", pathlib.Path.home() / ".claude-remote")
    ).expanduser().resolve()
    claude_bin: str = os.getenv("CCR_CLAUDE_BIN", "claude")
    lark_app_id: Optional[str] = os.getenv("LARK_APP_ID")
    lark_app_secret: Optional[str] = os.getenv("LARK_APP_SECRET")
    lark_user_id: Optional[str] = os.getenv("CCR_LARK_USER_ID")
    tunnel_domain: Optional[str] = os.getenv("CCR_TUNNEL_DOMAIN")
    log_path: str = os.getenv("CCR_LOG_PATH", "/tmp/claude-daemon.log")
    health_check_interval: float = 5.0
    waiting_timeout: float = 5.0
    output_tail_lines: int = 50
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


config = Config()