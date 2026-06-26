"""Session 注册表 — SQLite 持久化存储"""

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional


class SessionRegistry:
    """管理所有 Claude Code shell session 的注册信息"""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库表结构"""
        with self._lock:
            conn = sqlite3.connect(str(self._db_path))
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        id           TEXT PRIMARY KEY,
                        name         TEXT DEFAULT '',
                        screen_name  TEXT NOT NULL,
                        pid          INTEGER DEFAULT 0,
                        cwd          TEXT DEFAULT '',
                        log_path     TEXT DEFAULT '',
                        status       TEXT DEFAULT 'running',
                        created_at   REAL NOT NULL,
                        updated_at   REAL NOT NULL,
                        last_output  TEXT DEFAULT '',
                        tags         TEXT DEFAULT '{}',
                        session_type TEXT DEFAULT 'screen',
                        app_name     TEXT DEFAULT '',
                        win_title    TEXT DEFAULT ''
                    )
                """)
                # 数据库迁移：为旧版数据库添加新列（安全幂等）
                for col, col_type in [
                    ("session_type", "TEXT DEFAULT 'screen'"),
                    ("app_name", "TEXT DEFAULT ''"),
                    ("win_title", "TEXT DEFAULT ''"),
                ]:
                    try:
                        conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {col_type}")
                    except sqlite3.OperationalError:
                        pass  # 列已存在，忽略
                conn.commit()
            finally:
                conn.close()

    def register(
        self,
        session_id: str,
        screen_name: str,
        *,
        name: str = "",
        pid: int = 0,
        cwd: str = "",
        log_path: str = "",
        tags: Optional[dict] = None,
        session_type: str = "screen",
        app_name: str = "",
        win_title: str = "",
    ) -> dict:
        """注册一个新的 session

        Args:
            session_id: UUID
            screen_name: screen 会话名（如 claude-xxxx）
            name: 用户可读名称
            pid: 进程 ID
            cwd: 工作目录
            log_path: 输出日志路径
            tags: 额外标签
            session_type: 会话类型 ('screen' | 'ide' | 'standalone')
            app_name: 应用名称（IDE 终端时：IntelliJ IDEA、PyCharm 等）
            win_title: 窗口标题

        Returns:
            完整 session 记录
        """
        now = time.time()
        row = {
            "id": session_id,
            "name": name,
            "screen_name": screen_name,
            "pid": pid,
            "cwd": cwd,
            "log_path": log_path,
            "status": "running",
            "created_at": now,
            "updated_at": now,
            "last_output": "",
            "tags": json.dumps(tags or {}, ensure_ascii=False),
            "session_type": session_type,
            "app_name": app_name,
            "win_title": win_title,
        }
        with self._lock:
            conn = sqlite3.connect(str(self._db_path))
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO sessions
                       (id, name, screen_name, pid, cwd, log_path,
                        status, created_at, updated_at, last_output, tags,
                        session_type, app_name, win_title)
                       VALUES (:id, :name, :screen_name, :pid, :cwd, :log_path,
                               :status, :created_at, :updated_at, :last_output, :tags,
                               :session_type, :app_name, :win_title)""",
                    row,
                )
                conn.commit()
            finally:
                conn.close()
        return self.row_to_dict(row)

    def get(self, session_id: str) -> Optional[dict]:
        """根据 id 获取 session"""
        with self._lock:
            conn = sqlite3.connect(str(self._db_path))
            try:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    "SELECT * FROM sessions WHERE id = ?", (session_id,)
                )
                row = cur.fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def update(self, session_id: str, **kwargs) -> Optional[dict]:
        """更新 session 字段（仅更新传入的 key）

        Args:
            session_id: Session ID
            **kwargs: 要更新的字段（如 status、pid、last_output 等）

        Returns:
            更新后的完整 session，或 None（不存在）
        """
        if not kwargs:
            return self.get(session_id)

        kwargs["updated_at"] = time.time()
        sets = ", ".join(f"{k} = :{k}" for k in kwargs)
        params = kwargs | {"id": session_id}

        with self._lock:
            conn = sqlite3.connect(str(self._db_path))
            try:
                conn.execute(
                    f"UPDATE sessions SET {sets} WHERE id = :id", params
                )
                conn.commit()
            finally:
                conn.close()
        return self.get(session_id)

    def list(self, status_filter: Optional[str] = None) -> list[dict]:
        """列出所有 session，按创建时间降序

        Args:
            status_filter: 可选，按状态过滤（running、waiting、stopped）

        Returns:
            session 记录列表
        """
        with self._lock:
            conn = sqlite3.connect(str(self._db_path))
            try:
                conn.row_factory = sqlite3.Row
                if status_filter:
                    cur = conn.execute(
                        "SELECT * FROM sessions WHERE status = ? ORDER BY created_at DESC",
                        (status_filter,),
                    )
                else:
                    cur = conn.execute(
                        "SELECT * FROM sessions ORDER BY created_at DESC"
                    )
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

    def delete(self, session_id: str) -> bool:
        """删除 session 记录

        Returns:
            是否找到了并删除了
        """
        with self._lock:
            conn = sqlite3.connect(str(self._db_path))
            try:
                cur = conn.execute(
                    "DELETE FROM sessions WHERE id = ?", (session_id,)
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def update_status(self, session_id: str, status: str) -> Optional[dict]:
        """快捷方法：更新 session 状态"""
        return self.update(session_id, status=status)

    def count_by_status(self) -> dict[str, int]:
        """按状态统计 session 数量"""
        with self._lock:
            conn = sqlite3.connect(str(self._db_path))
            try:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    "SELECT status, COUNT(*) as cnt FROM sessions GROUP BY status"
                )
                counts = {"running": 0, "waiting": 0, "stopped": 0, "error": 0}
                for row in cur.fetchall():
                    counts[row["status"]] = row["cnt"]
                return counts
            finally:
                conn.close()

    def recover_sessions(self, active_screen_names: set[str]) -> list[dict]:
        """Daemon 启动时恢复：将 DB 中 running 但 screen 不存在的标记为 stopped

        Args:
            active_screen_names: 当前活跃的 screen 会话名集合

        Returns:
            所有活跃 session 列表
        """
        recovered = []
        with self._lock:
            conn = sqlite3.connect(str(self._db_path))
            try:
                conn.row_factory = sqlite3.Row

                # 查出所有标记为 running 但 screen 已死的 session
                cur = conn.execute(
                    "SELECT * FROM sessions WHERE status IN ('running', 'waiting')"
                )
                for row in cur.fetchall():
                    if row["screen_name"] not in active_screen_names:
                        conn.execute(
                            "UPDATE sessions SET status = 'stopped', updated_at = ? WHERE id = ?",
                            (time.time(), row["id"]),
                        )

                # 返回所有未停止的 session
                cur = conn.execute(
                    "SELECT * FROM sessions WHERE status IN ('running', 'waiting') ORDER BY created_at DESC"
                )
                recovered = [dict(r) for r in cur.fetchall()]
                conn.commit()
            finally:
                conn.close()
        return recovered

    @staticmethod
    def row_to_dict(row: dict) -> dict:
        """将 SQLite row 转为字典（JSON 兼容）"""
        result = dict(row)
        if "tags" in result and isinstance(result["tags"], str):
            try:
                result["tags"] = json.loads(result["tags"])
            except json.JSONDecodeError:
                result["tags"] = {}
        # 时间戳转为可读字符串
        return result