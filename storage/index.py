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
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    path TEXT,
                    status TEXT DEFAULT '活跃',
                    created_at REAL,
                    last_active_at REAL,
                    session_count INTEGER DEFAULT 0,
                    archived_count INTEGER DEFAULT 0,
                    display_name TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_project_path ON projects(path)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS archives (
                    session_id TEXT PRIMARY KEY,
                    archive_path TEXT,
                    archived_at REAL,
                    message_count INTEGER,
                    is_compacted INTEGER DEFAULT 0
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_provider ON sessions(provider_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON sessions(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_last_active ON sessions(last_active_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pinned ON sessions(pinned)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_deleted ON sessions(deleted)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_project_dir ON sessions(project_dir)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS handoff_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_dir TEXT NOT NULL,
                    session_id TEXT,
                    source TEXT NOT NULL DEFAULT 'stop_hook',
                    summary_text TEXT NOT NULL,
                    key_findings TEXT,
                    current_phase TEXT,
                    next_action TEXT,
                    volatility_tags TEXT,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_handoff_project ON handoff_entries(project_dir)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_handoff_created ON handoff_entries(created_at)")

            # patterns_idx — 轻量索引，本体在 data/patterns/*.md
            conn.execute("""
                CREATE TABLE IF NOT EXISTS patterns_idx (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    type TEXT NOT NULL DEFAULT 'workflow',
                    title TEXT NOT NULL,
                    category TEXT,
                    project TEXT,
                    tags TEXT,
                    source_sessions TEXT,
                    file_path TEXT NOT NULL,
                    status TEXT DEFAULT 'draft',
                    use_count INTEGER DEFAULT 0,
                    created_at REAL,
                    updated_at REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_patterns_type ON patterns_idx(type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_patterns_status ON patterns_idx(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_patterns_project ON patterns_idx(project)")

            # FTS5 — sessions 搜索
            try:
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts
                    USING fts5(id, title, summary, project_dir,
                               content=sessions, content_rowid='rowid', tokenize='unicode61')
                """)
            except sqlite3.OperationalError:
                pass

            # FTS5 — patterns 搜索
            try:
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS patterns_fts
                    USING fts5(name, title, tags, content=patterns_idx, content_rowid='rowid', tokenize='unicode61')
                """)
            except sqlite3.OperationalError:
                pass

            # FTS5 同步触发器 — sessions
            try:
                conn.executescript("""
                    DROP TRIGGER IF EXISTS sessions_fts_insert;
                    CREATE TRIGGER sessions_fts_insert AFTER INSERT ON sessions BEGIN
                        INSERT INTO sessions_fts(rowid, title, summary, project_dir)
                        VALUES (new.rowid, new.title, new.summary, new.project_dir);
                    END;
                    DROP TRIGGER IF EXISTS sessions_fts_delete;
                    CREATE TRIGGER sessions_fts_delete AFTER DELETE ON sessions BEGIN
                        INSERT INTO sessions_fts(sessions_fts, rowid, title, summary, project_dir)
                        VALUES ('delete', old.rowid, old.title, old.summary, old.project_dir);
                    END;
                    DROP TRIGGER IF EXISTS sessions_fts_update;
                    CREATE TRIGGER sessions_fts_update AFTER UPDATE ON sessions BEGIN
                        INSERT INTO sessions_fts(sessions_fts, rowid, title, summary, project_dir)
                        VALUES ('delete', old.rowid, old.title, old.summary, old.project_dir);
                        INSERT INTO sessions_fts(rowid, title, summary, project_dir)
                        VALUES (new.rowid, new.title, new.summary, new.project_dir);
                    END;
                """)
            except sqlite3.OperationalError:
                pass

            # 回填已有 sessions 到 FTS5 索引
            conn.execute("""
                INSERT OR IGNORE INTO sessions_fts(rowid, title, summary, project_dir)
                SELECT s.rowid, s.title, s.summary, s.project_dir
                FROM sessions s WHERE s.deleted = 0
            """)

            # FTS5 同步触发器 — patterns
            try:
                conn.executescript("""
                    DROP TRIGGER IF EXISTS patterns_fts_insert;
                    CREATE TRIGGER patterns_fts_insert AFTER INSERT ON patterns_idx BEGIN
                        INSERT INTO patterns_fts(rowid, name, title, tags)
                        VALUES (new.rowid, new.name, new.title, new.tags);
                    END;
                    DROP TRIGGER IF EXISTS patterns_fts_delete;
                    CREATE TRIGGER patterns_fts_delete AFTER DELETE ON patterns_idx BEGIN
                        INSERT INTO patterns_fts(patterns_fts, rowid, name, title, tags)
                        VALUES ('delete', old.rowid, old.name, old.title, old.tags);
                    END;
                    DROP TRIGGER IF EXISTS patterns_fts_update;
                    CREATE TRIGGER patterns_fts_update AFTER UPDATE ON patterns_idx BEGIN
                        INSERT INTO patterns_fts(patterns_fts, rowid, name, title, tags)
                        VALUES ('delete', old.rowid, old.name, old.title, old.tags);
                        INSERT INTO patterns_fts(rowid, name, title, tags)
                        VALUES (new.rowid, new.name, new.title, new.tags);
                    END;
                """)
            except sqlite3.OperationalError:
                pass

    def upsert_sessions(self, records: List[SessionRecord]) -> tuple:
        """返回 (inserted_ids, updated_ids)"""
        inserted = []
        updated = []
        with self._conn() as conn:
            for r in records:
                existing = conn.execute("SELECT id FROM sessions WHERE id = ?", (r.id,)).fetchone()
                if existing:
                    updated.append(r.id)
                else:
                    inserted.append(r.id)
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
        return inserted, updated

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

    def sync_projects(self):
        """从 sessions 表聚合 project_dir，同步到 projects 表。

        v0.2: 支持比赛级工作区自动聚合。
        当 project_dir 位于 .../targets/{contest}/challenges/{target}/ 下时，
        向上提取比赛目录作为项目 ID，所有子题目会话聚合到同一项目。
        """
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT project_dir,
                       COUNT(*) as session_count,
                       MAX(last_active_at) as last_active_at,
                       MIN(created_at) as created_at
                FROM sessions
                WHERE deleted = 0 AND project_dir IS NOT NULL AND project_dir != ''
                GROUP BY project_dir
            """).fetchall()

            # 第一步：按项目（单题目或比赛）聚合数据
            project_data = {}
            for r in rows:
                path = r["project_dir"]
                normalized = path.replace("\\", "/")

                if "/challenges/" in normalized:
                    # 比赛级工作区：向上提取比赛目录
                    idx = normalized.find("/challenges/")
                    contest_path = normalized[:idx]
                    pid = contest_path
                    is_contest = True
                else:
                    pid = path
                    is_contest = False

                if pid not in project_data:
                    project_data[pid] = {
                        "name": Path(pid).name,
                        "path": pid,
                        "session_count": 0,
                        "last_active_at": None,
                        "created_at": None,
                        "is_contest": is_contest,
                        "target_count": 0 if is_contest else 1,
                    }

                d = project_data[pid]
                d["session_count"] += r["session_count"]
                if r["last_active_at"]:
                    d["last_active_at"] = max(d["last_active_at"] or 0, r["last_active_at"])
                if r["created_at"]:
                    d["created_at"] = min(d["created_at"] or float('inf'), r["created_at"])
                if is_contest:
                    d["target_count"] += 1

            # 第二步：更新 projects 表
            for pid, data in project_data.items():
                if data["is_contest"]:
                    display = f"{data['name']} ({data['target_count']}题)"
                else:
                    display = data["name"]

                existing = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
                if existing:
                    # 保留用户手动设置的 display_name，只更新 name（作为后备）
                    conn.execute("""
                        UPDATE projects SET
                            session_count = ?,
                            last_active_at = COALESCE(?, last_active_at),
                            name = ?
                        WHERE id = ?
                    """, (data["session_count"], data["last_active_at"], display, pid))
                else:
                    conn.execute("""
                        INSERT INTO projects (id, name, path, status, created_at, last_active_at, session_count)
                        VALUES (?, ?, ?, '活跃', ?, ?, ?)
                    """, (pid, display, data["path"], data["created_at"],
                          data["last_active_at"], data["session_count"]))

    def list_projects(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM projects ORDER BY last_active_at DESC NULLS LAST
            """).fetchall()
            return [dict(r) for r in rows]

    def get_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            return dict(row) if row else None

    def update_project_status(self, project_id: str, status: str):
        with self._conn() as conn:
            conn.execute("UPDATE projects SET status = ? WHERE id = ?", (status, project_id))

    def update_project_display_name(self, project_id: str, display_name: str):
        with self._conn() as conn:
            conn.execute("UPDATE projects SET display_name = ? WHERE id = ?", (display_name, project_id))

    def _is_contest_project_dir(self, project_dir: str) -> bool:
        """检测 project_dir 是否为比赛级工作区目录。"""
        normalized = project_dir.replace("\\", "/")
        # 比赛目录本身不含 /challenges/，但路径下应该有 challenges/ 子目录
        # 简单判断：如果路径以 targets/ 开头且不含 /challenges/，
        # 检查该路径下是否有 contest.json 或 challenges/ 目录
        if "/challenges/" in normalized:
            return False  # 这是子题目目录
        # 检查是否为比赛目录：存在 challenges/ 子目录
        p = Path(project_dir)
        if p.exists() and (p / "challenges").is_dir():
            return True
        return False

    def get_project_sessions(self, project_dir: str, provider: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取项目下的会话。支持比赛级工作区（自动聚合所有子题目会话）。"""
        is_contest = self._is_contest_project_dir(project_dir)

        if is_contest:
            # 比赛目录：匹配所有子题目会话
            # 使用路径前缀匹配，兼容 Windows 和 Unix 路径分隔符
            normalized = project_dir.replace("\\", "/")
            prefix = normalized.rstrip("/") + "/challenges/"
            prefix_win = project_dir.rstrip("\\") + "\\challenges\\"
            sql = """
                SELECT * FROM sessions
                WHERE deleted = 0
                  AND (
                      REPLACE(project_dir, '\\', '/') LIKE ?
                      OR project_dir LIKE ?
                  )
            """
            params = [prefix + "%", prefix_win + "%"]
        else:
            sql = "SELECT * FROM sessions WHERE deleted = 0 AND project_dir = ?"
            params = [project_dir]

        if provider:
            sql += " AND provider_id = ?"
            params.append(provider)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY pinned DESC, pinned_at DESC, last_active_at DESC NULLS LAST, created_at DESC"
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def record_archive(self, session_id: str, archive_path: str, message_count: int):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO archives (session_id, archive_path, archived_at, message_count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    archive_path=excluded.archive_path,
                    archived_at=excluded.archived_at,
                    message_count=excluded.message_count
            """, (session_id, archive_path, datetime.now().timestamp(), message_count))

    def is_archived(self, session_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT 1 FROM archives WHERE session_id = ?", (session_id,)).fetchone()
            return row is not None

    def get_archive(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM archives WHERE session_id = ?", (session_id,)).fetchone()
            return dict(row) if row else None

    def list_unarchived_sessions(self, provider: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = """
            SELECT s.* FROM sessions s
            LEFT JOIN archives a ON s.id = a.session_id
            WHERE s.deleted = 0 AND a.session_id IS NULL
        """
        params = []
        if provider:
            sql += " AND s.provider_id = ?"
            params.append(provider)
        sql += " ORDER BY s.last_active_at DESC"
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def insert_handoff(self, project_dir: str, summary_text: str, session_id: Optional[str] = None,
                       key_findings: Optional[str] = None, current_phase: Optional[str] = None,
                       next_action: Optional[str] = None, volatility_tags: Optional[str] = None,
                       source: str = "stop_hook") -> int:
        with self._conn() as conn:
            cur = conn.execute("""
                INSERT INTO handoff_entries (project_dir, session_id, source, summary_text, key_findings,
                    current_phase, next_action, volatility_tags, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (project_dir, session_id, source, summary_text, key_findings,
                  current_phase, next_action, volatility_tags, datetime.now().timestamp()))
            return cur.lastrowid

    def get_handoff_context(self, project_dir: str, limit: int = 5) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM handoff_entries
                WHERE project_dir = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (project_dir, limit)).fetchall()
            return [dict(r) for r in rows]

    def get_latest_handoff(self, project_dir: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("""
                SELECT * FROM handoff_entries
                WHERE project_dir = ?
                ORDER BY created_at DESC
                LIMIT 1
            """, (project_dir,)).fetchone()
            return dict(row) if row else None

    def list_handoff_projects(self) -> List[str]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT DISTINCT project_dir FROM handoff_entries ORDER BY project_dir
            """).fetchall()
            return [r[0] for r in rows]

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

    # -- FTS5 unified search ------------------------------------------------

    def fts5_search(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Unified FTS5 search across sessions + patterns. Falls back to LIKE."""
        results = []
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("""
                    SELECT s.*, rank FROM sessions_fts f
                    JOIN sessions s ON s.rowid = f.rowid
                    WHERE sessions_fts MATCH ? AND s.deleted = 0
                    ORDER BY rank LIMIT ?
                """, (query, limit)).fetchall()
                for r in rows:
                    d = dict(r)
                    d["result_type"] = "session"
                    d["display_name"] = d.get("title") or d.get("session_id") or "untitled"
                    results.append(d)
        except Exception:
            pass
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("""
                    SELECT p.*, rank FROM patterns_fts f
                    JOIN patterns_idx p ON p.rowid = f.rowid
                    WHERE patterns_fts MATCH ?
                    ORDER BY rank LIMIT ?
                """, (query, limit)).fetchall()
                for r in rows:
                    d = dict(r)
                    d["result_type"] = "pattern"
                    d["display_name"] = d.get("title") or d.get("name")
                    results.append(d)
        except Exception:
            pass
        return sorted(results, key=lambda x: x.get("updated_at") or x.get("last_active_at") or 0, reverse=True)[:limit]

    # -- Pattern CRUD --------------------------------------------------------

    def upsert_pattern(self, name: str, ptype: str, title: str, file_path: str,
                       category: str = "", project: str = "",
                       tags: str = "", source_sessions: str = "",
                       status: str = "draft") -> int:
        now = datetime.now().timestamp()
        with self._conn() as conn:
            cur = conn.execute("""
                INSERT INTO patterns_idx (name, type, title, category, project, tags,
                    source_sessions, file_path, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    type=excluded.type, title=excluded.title, updated_at=excluded.updated_at,
                    file_path=excluded.file_path, tags=excluded.tags,
                    source_sessions=excluded.source_sessions
            """, (name, ptype, title, category, project, tags,
                  source_sessions, file_path, status, now, now))
            return cur.lastrowid

    def list_patterns(self, ptype: Optional[str] = None, status: Optional[str] = None,
                      project: Optional[str] = None, search: Optional[str] = None) -> List[Dict]:
        sql = "SELECT * FROM patterns_idx WHERE 1=1"
        params = []
        if ptype:
            sql += " AND type = ?"; params.append(ptype)
        if status:
            sql += " AND status = ?"; params.append(status)
        if project:
            sql += " AND project = ?"; params.append(project)
        if search:
            sql += " AND (name LIKE ? OR title LIKE ?)"
            params.extend([f"%{search}%"] * 2)
        sql += " ORDER BY updated_at DESC, created_at DESC"
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_pattern(self, name: str) -> Optional[Dict]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM patterns_idx WHERE name = ?", (name,)).fetchone()
            return dict(row) if row else None

    def update_pattern_status(self, name: str, status: str):
        with self._conn() as conn:
            conn.execute("UPDATE patterns_idx SET status = ?, updated_at = ? WHERE name = ?",
                         (status, datetime.now().timestamp(), name))

    def bump_pattern_use(self, name: str):
        with self._conn() as conn:
            conn.execute("""
                UPDATE patterns_idx SET use_count = use_count + 1, updated_at = ?
                WHERE name = ?
            """, (datetime.now().timestamp(), name))

    def delete_pattern(self, name: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM patterns_idx WHERE name = ?", (name,))
            return cur.rowcount > 0

    def list_patterns_by_names(self, names: List[str]) -> List[Dict]:
        if not names:
            return []
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            placeholders = ",".join("?" * len(names))
            rows = conn.execute(
                f"SELECT * FROM patterns_idx WHERE name IN ({placeholders})",
                names
            ).fetchall()
            return [dict(r) for r in rows]
