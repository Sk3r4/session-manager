import sqlite3
import json
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, asdict
from datetime import datetime

from config import INDEX_DB_PATH, STATUS_OPTIONS

@dataclass
class SessionRecord:
    id: str
    provider_id: str
    session_id: str
    title: Optional[str]
    summary: Optional[str]
    project_dir: Optional[str]
    status: str
    created_at: Optional[float]
    last_active_at: Optional[float]
    source_path: Optional[str]
    raw_meta: str

class IndexDB:
    def __init__(self, db_path: Path = INDEX_DB_PATH):
        self.db_path = db_path
        self._ensure_tables()

    def _conn(self):
        return sqlite3.connect(str(self.db_path))

    def _ensure_tables(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    provider_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    title TEXT,
                    summary TEXT,
                    project_dir TEXT,
                    status TEXT DEFAULT '未标注',
                    created_at REAL,
                    last_active_at REAL,
                    source_path TEXT,
                    raw_meta TEXT,
                    pinned INTEGER DEFAULT 0,
                    pinned_at REAL,
                    deleted INTEGER DEFAULT 0
                )
            """)
            # 兼容旧表
            try:
                conn.execute("ALTER TABLE sessions ADD COLUMN deleted INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scan_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scanned_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    provider_id TEXT,
                    sessions_found INTEGER,
                    error TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_provider ON sessions(provider_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON sessions(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_last_active ON sessions(last_active_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pinned ON sessions(pinned)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_deleted ON sessions(deleted)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_project_dir ON sessions(project_dir)")

    def upsert_sessions(self, records: List[SessionRecord]):
        with self._conn() as conn:
            for r in records:
                conn.execute("""
                    INSERT INTO sessions (id, provider_id, session_id, title, summary, project_dir, status, created_at, last_active_at, source_path, raw_meta)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        provider_id=excluded.provider_id,
                        session_id=excluded.session_id,
                        summary=COALESCE(sessions.summary, excluded.summary),
                        project_dir=COALESCE(excluded.project_dir, sessions.project_dir),
                        created_at=COALESCE(excluded.created_at, sessions.created_at),
                        last_active_at=COALESCE(excluded.last_active_at, sessions.last_active_at),
                        source_path=excluded.source_path,
                        raw_meta=excluded.raw_meta,
                        deleted=COALESCE(sessions.deleted, 0)
                """, (r.id, r.provider_id, r.session_id, r.title, r.summary, r.project_dir, r.status,
                      r.created_at, r.last_active_at, r.source_path, r.raw_meta))

    def update_title(self, session_id: str, title: Optional[str]):
        with self._conn() as conn:
            conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))

    def update_status(self, session_id: str, status: str):
        if status not in STATUS_OPTIONS:
            raise ValueError(f"Invalid status: {status}. Must be one of {STATUS_OPTIONS}")
        with self._conn() as conn:
            conn.execute("UPDATE sessions SET status = ? WHERE id = ?", (status, session_id))

    def pin_session(self, session_id: str, pinned: bool = True):
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET pinned = ?, pinned_at = ? WHERE id = ?",
                (1 if pinned else 0, datetime.now().timestamp() if pinned else None, session_id)
            )

    def list_sessions(self, provider: Optional[str] = None, status: Optional[str] = None, search: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM sessions WHERE deleted = 0"
        params = []
        if provider:
            sql += " AND provider_id = ?"
            params.append(provider)
        if status:
            sql += " AND status = ?"
            params.append(status)
        if search:
            sql += " AND (id LIKE ? OR title LIKE ? OR summary LIKE ? OR project_dir LIKE ?)"
            params.extend([f"%{search}%"] * 4)
        sql += " ORDER BY pinned DESC, pinned_at DESC, last_active_at DESC NULLS LAST, created_at DESC"
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            return dict(row) if row else None

    def log_scan(self, provider_id: str, sessions_found: int, error: Optional[str] = None):
        with self._conn() as conn:
            conn.execute("INSERT INTO scan_log (provider_id, sessions_found, error) VALUES (?, ?, ?)",
                         (provider_id, sessions_found, error))

    def delete_session(self, session_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("UPDATE sessions SET deleted = 1 WHERE id = ?", (session_id,))
            return cur.rowcount > 0

    def list_sessions_by_date_range(self, from_ts: Optional[float] = None, to_ts: Optional[float] = None, project_dir: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM sessions WHERE deleted = 0"
        params = []
        if from_ts is not None:
            sql += " AND last_active_at >= ?"
            params.append(from_ts)
        if to_ts is not None:
            sql += " AND last_active_at < ?"
            params.append(to_ts)
        if project_dir is not None:
            sql += " AND project_dir = ?"
            params.append(project_dir)
        sql += " ORDER BY pinned DESC, pinned_at DESC, last_active_at DESC NULLS LAST, created_at DESC"
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_daily_stats(self) -> Dict[str, int]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT DATE(datetime(last_active_at, 'unixepoch', 'localtime')) as day, COUNT(*) as cnt
                FROM sessions WHERE deleted = 0 AND last_active_at IS NOT NULL
                GROUP BY day ORDER BY day DESC
            """).fetchall()
            return {r["day"]: r["cnt"] for r in rows}

    def get_providers_summary(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT provider_id, COUNT(*) as total,
                       SUM(CASE WHEN status != '未标注' THEN 1 ELSE 0 END) as labeled,
                       SUM(CASE WHEN pinned = 1 THEN 1 ELSE 0 END) as pinned
                FROM sessions WHERE deleted = 0 GROUP BY provider_id
            """).fetchall()
            return [dict(r) for r in rows]
