"""标签域:session_tags 多对多与标签面(2026-10-15 自 index.py 单体 AST 拆出,方法体原样)。"""
import sqlite3
from datetime import datetime
from typing import Any, Dict, List


class TagsMixin:
    """标签域:session_tags 多对多与标签面"""

    @staticmethod
    def _attach_tags(conn: sqlite3.Connection, rows: List[Dict[str, Any]]):
        """给会话行批量附上 tags 列表(单次 IN 查询)。"""
        if not rows:
            return
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        tag_rows = conn.execute(
            f"SELECT session_id, tag FROM session_tags WHERE session_id IN ({placeholders})"
            " ORDER BY added_at, tag", ids,
        ).fetchall()
        by_sid: Dict[str, List[str]] = {}
        for sid, tag in tag_rows:
            by_sid.setdefault(sid, []).append(tag)
        for r in rows:
            r["tags"] = by_sid.get(r["id"], [])

    def set_session_tags(self, session_id: str, tags: List[str]) -> List[str]:
        """整组替换会话标签(去空白/去重);标签是集合,恒按字典序返回。"""
        clean: List[str] = []
        seen = set()
        for t in tags:
            t = t.strip()
            if t and t not in seen:
                seen.add(t)
                clean.append(t)
        with self._conn() as conn:
            conn.execute("DELETE FROM session_tags WHERE session_id = ?", (session_id,))
            conn.executemany(
                "INSERT INTO session_tags (session_id, tag, added_at) VALUES (?, ?, ?)",
                [(session_id, t, datetime.now().timestamp()) for t in clean],
            )
        return sorted(clean)

    def get_session_tags(self, session_id: str) -> List[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT tag FROM session_tags WHERE session_id = ? ORDER BY added_at, tag",
                (session_id,),
            ).fetchall()
            return [r[0] for r in rows]

    def list_tags(self) -> List[Dict[str, Any]]:
        """全部标签 + 可见(deleted=0)会话计数,按热度/字典序。"""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT st.tag, COUNT(*) as count
                FROM session_tags st JOIN sessions s ON s.id = st.session_id
                WHERE s.deleted = 0
                GROUP BY st.tag ORDER BY count DESC, st.tag
            """).fetchall()
            return [dict(r) for r in rows]
