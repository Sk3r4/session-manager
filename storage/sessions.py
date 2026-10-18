"""会话域:CRUD/幽灵清理(归档豁免)/归档回退/标注写回/扫描日志/统计(2026-10-15 自 index.py 单体 AST 拆出,方法体原样)。"""
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional
from storage.base import SessionRecord


class SessionsMixin:
    """会话域:CRUD/幽灵清理(归档豁免)/归档回退/标注写回/扫描日志/统计"""

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
                # 标题三方语义:
                #   manual + CLI 侧人工改名(≠最近所见基线 且 title_author='human') → CLI 胜出
                #   manual + AI 标题漂移(作者非 human) → 手动名永远保留(漂移不是改名)
                #   auto(title_source 为 NULL) → 新值优先,自动标题跟随 CLI 演进
                # 基线 cli_title 每次随见随前移(含 AI 漂移),保证"人工改名"的对比始终新鲜。
                conn.execute("""
                    INSERT INTO sessions (id, provider_id, session_id, title, summary, project_dir, created_at, last_active_at, source_path, cli_title, title_author)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        provider_id=excluded.provider_id,
                        session_id=excluded.session_id,
                        title=CASE
                            WHEN sessions.title_source = 'manual'
                                 AND excluded.title IS NOT NULL
                                 AND excluded.title != sessions.cli_title
                                 AND excluded.title_author = 'human'
                                THEN excluded.title
                            WHEN sessions.title_source = 'manual' THEN sessions.title
                            ELSE COALESCE(excluded.title, sessions.title)
                        END,
                        cli_title=COALESCE(excluded.title, sessions.cli_title),
                        title_author=excluded.title_author,
                        summary=COALESCE(sessions.summary, excluded.summary),
                        project_dir=COALESCE(excluded.project_dir, sessions.project_dir),
                        created_at=COALESCE(excluded.created_at, sessions.created_at),
                        last_active_at=COALESCE(excluded.last_active_at, sessions.last_active_at),
                        source_path=excluded.source_path,
                        deleted=COALESCE(sessions.deleted, 0)
                """, (r.id, r.provider_id, r.session_id, r.title, r.summary, r.project_dir,
                      r.created_at, r.last_active_at, r.source_path, r.title, r.title_author))
        return inserted, updated

    def update_title(self, session_id: str, title: Optional[str]):
        with self._conn() as conn:
            # title 非空 → 标记 manual；置空（恢复默认）→ 清除标记，允许下次扫描回填
            conn.execute(
                "UPDATE sessions SET title = ?, title_source = ? WHERE id = ?",
                (title, "manual" if title else None, session_id),
            )

    def soft_delete_absent(self, provider_id: str, live_ids: List[str], *, trust_live_ids: bool = False) -> int:
        """软删该 provider 下不在 live_ids 中的会话。

        同步 CLI 删除/移动会话文件后残留的幽灵行(与手动删除一致,置 deleted=1,可恢复)。
        默认(文件型 provider)双重保险:不在本次扫描结果中 + 源文件在磁盘上确实不存在才删;
        trust_live_ids=True(db 型 provider,如 zcode)扫描结果即权威全集——删会话 = 删库里的行,
        源文件恒在,只按 live_ids 判断。
        有归档导出的行豁免(archives 表存量 + md 在盘)——CLI 保留期清理(如 Claude Code
        cleanupPeriodDays)删的是源文件不是会话内容,不构成隐藏理由(2026-10-14,八月数据事件)。
        """
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            try:
                archived = {r[0] for r in conn.execute(
                    "SELECT session_id, archive_path FROM archives"
                ).fetchall() if r[1] and Path(r[1]).exists()}
            except sqlite3.OperationalError:
                archived = set()  # 全新库无 archives 表(归档功能 2026-09-20 已移除)
            rows = conn.execute(
                "SELECT id, source_path FROM sessions WHERE provider_id = ? AND deleted = 0",
                (provider_id,)
            ).fetchall()
            live = set(live_ids)
            gone = [r["id"] for r in rows
                    if r["id"] not in live and r["id"] not in archived and (
                        trust_live_ids or not Path(r["source_path"] or "").exists())]
            if gone:
                conn.executemany(
                    "UPDATE sessions SET deleted = 1 WHERE id = ?",
                    [(g,) for g in gone]
                )
            return len(gone)

    def archive_of(self, session_id: str) -> Optional[str]:
        """会话的归档 md 路径(archives 表存量数据;无归档/表不存在 → None)。

        归档功能(archiver.py)2026-09-20 已移除,本表只剩历史登记——供转录回退:
        源文件被 CLI 保留期清理后,从归档 md 读内容。
        """
        with self._conn() as conn:
            try:
                row = conn.execute(
                    "SELECT archive_path FROM archives WHERE session_id = ?", (session_id,)
                ).fetchone()
            except sqlite3.OperationalError:
                return None
            return row[0] if row else None

    def list_sessions(
        self,
        provider: Optional[str] = None,
        search: Optional[str] = None,
        from_ts: Optional[float] = None,
        to_ts: Optional[float] = None,
        project_dir: Optional[str] = None,
        tag: Optional[str] = None,
        include_deleted: bool = False,
    ) -> List[Dict[str, Any]]:
        """统一的会话列表查询：所有过滤条件（provider/search/时间范围/项目目录）
        都在 SQL 中完成，不再有"日期范围走 Python 过滤"的第二条路径。

        include_deleted 只返回隐藏(deleted=1);永久移除(deleted=2)对 API 完全不可见。
        search 命中 id(含 session_id/provider)/title/summary/project_dir。
        """
        where = ["deleted IN (0, 1)"] if include_deleted else ["deleted = 0"]
        params: List[Any] = []
        if provider:
            where.append("provider_id = ?")
            params.append(provider)
        if search:
            where.append("(id LIKE ? OR title LIKE ? OR summary LIKE ? OR project_dir LIKE ?"
                         " OR id IN (SELECT session_id FROM session_tags WHERE tag LIKE ?))")
            params.extend([f"%{search}%"] * 5)
        if from_ts is not None:
            where.append("last_active_at >= ?")
            params.append(from_ts)
        if to_ts is not None:
            where.append("last_active_at < ?")
            params.append(to_ts)
        if project_dir is not None:
            where.append("project_dir = ?")
            params.append(project_dir)
        if tag is not None:
            where.append("id IN (SELECT session_id FROM session_tags WHERE tag = ?)")
            params.append(tag)
        sql = "SELECT * FROM sessions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY last_active_at DESC NULLS LAST, created_at DESC"
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            out = [dict(r) for r in rows]
            self._attach_tags(conn, out)
            return out

    def restore_session(self, session_id: str) -> bool:
        """恢复被隐藏的会话（deleted=1 → 0）。永久移除（deleted=2）不可恢复。"""
        with self._conn() as conn:
            cur = conn.execute("UPDATE sessions SET deleted = 0 WHERE id = ? AND deleted = 1", (session_id,))
            return cur.rowcount > 0

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            return dict(row) if row else None

    def log_scan(self, provider_id: str, sessions_found: int, error: Optional[str] = None):
        with self._conn() as conn:
            conn.execute("INSERT INTO scan_log (provider_id, sessions_found, error) VALUES (?, ?, ?)",
                         (provider_id, sessions_found, error))
            # 轮转:kimi 时代曾 26% 全是错误噪音;只保留最近 2000 条
            conn.execute("DELETE FROM scan_log WHERE id NOT IN (SELECT id FROM scan_log ORDER BY id DESC LIMIT 2000)")

    def delete_session(self, session_id: str, permanent: bool = False) -> bool:
        """删除会话。permanent=False → 隐藏（deleted=1，可恢复）；
        permanent=True → 永久移除（deleted=2，任何列表不显示、扫描不复活、不可恢复）。"""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE sessions SET deleted = ? WHERE id = ?",
                (2 if permanent else 1, session_id)
            )
            return cur.rowcount > 0

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
                SELECT provider_id, COUNT(*) as total
                FROM sessions WHERE deleted = 0 GROUP BY provider_id
            """).fetchall()
            return [dict(r) for r in rows]

    def mark_annotated(self, session_id: str):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO annotated_sessions (session_id, annotated_at) VALUES (?, ?)
                ON CONFLICT(session_id) DO UPDATE SET annotated_at = excluded.annotated_at
            """, (session_id, datetime.now().timestamp()))

    def update_summary(self, session_id: str, summary: Optional[str]):
        """直接覆写摘要(AI 标注/人工整理)。扫描 upsert 的 COALESCE 语义
        保证已有摘要不被扫描覆盖;置 None = 恢复"扫描可回填"状态。"""
        with self._conn() as conn:
            conn.execute("UPDATE sessions SET summary = ? WHERE id = ?", (summary, session_id))

    def session_ids_by_source_paths(self, paths: List[str]) -> List[str]:
        """按源文件路径批量查会话 id(变更探测 → 阅读视图重载映射)。"""
        if not paths:
            return []
        with self._conn() as conn:
            placeholders = ",".join("?" * len(paths))
            rows = conn.execute(
                f"SELECT id FROM sessions WHERE source_path IN ({placeholders})", paths
            ).fetchall()
            return [r[0] for r in rows]
