"""全文索引域:content_fts(FTS5 trigram)增量同步与搜索(2026-10-15 自 index.py 单体 AST 拆出,方法体原样)。"""
import sqlite3
from typing import Any, Dict, List, Optional
from storage.base import SessionRecord


class FtsMixin:
    """全文索引域:content_fts(FTS5 trigram)增量同步与搜索"""

    def fts_stale_ids(self, records: List[SessionRecord]) -> List[str]:
        """需要重建内容索引的会话 id(无基线,或 last_active_at 较上次有变化)。"""
        with self._conn() as conn:
            rows = conn.execute("SELECT session_id, last_active_at FROM fts_state").fetchall()
            state = {r[0]: r[1] for r in rows}
        return [r.id for r in records
                if r.id not in state or state[r.id] != r.last_active_at]

    def fts_upsert(self, session_id: str, last_active_at: Optional[float], text: str):
        """重建单个会话的内容索引并前移基线(空文本只记基线,不占索引)。"""
        with self._conn() as conn:
            conn.execute("DELETE FROM content_fts WHERE sid = ?", (session_id,))
            if text:
                conn.execute("INSERT INTO content_fts (content, sid) VALUES (?, ?)",
                             (text, session_id))
            conn.execute("""
                INSERT INTO fts_state (session_id, last_active_at) VALUES (?, ?)
                ON CONFLICT(session_id) DO UPDATE SET last_active_at = excluded.last_active_at
            """, (session_id, last_active_at))

    def fts_orphan_cleanup(self):
        """清掉 sessions 表已不存在的索引行(会话被彻底移除后)。"""
        with self._conn() as conn:
            conn.execute("DELETE FROM content_fts WHERE sid NOT IN (SELECT id FROM sessions)")
            conn.execute("DELETE FROM fts_state WHERE session_id NOT IN (SELECT id FROM sessions)")

    def search_content(self, q: str, limit: int = 50) -> List[Dict[str, Any]]:
        """内容全文搜索,只返回可见(deleted=0)会话。
        ≥3 字符走 MATCH 索引(bm25 相关性排序 + snippet 高亮);
        更短的词 trigram 无条目 → LIKE 全扫兜底(实测百 ms 级),Python 侧截片段。"""
        q = q.strip()
        if not q:
            return []
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            if len(q) >= 3:
                phrase = '"' + q.replace('"', '""') + '"'
                rows = conn.execute("""
                    SELECT s.id, s.title, s.provider_id, s.session_id,
                           snippet(content_fts, 0, '「', '」', '…', 12) as snippet,
                           bm25(content_fts) as rank
                    FROM content_fts f JOIN sessions s ON s.id = f.sid
                    WHERE f.content MATCH ? AND s.deleted = 0
                    ORDER BY rank
                    LIMIT ?
                """, (phrase, limit)).fetchall()
                return [dict(r) for r in rows]
            rows = conn.execute("""
                SELECT s.id, s.title, s.provider_id, s.session_id, f.content
                FROM content_fts f JOIN sessions s ON s.id = f.sid
                WHERE f.content LIKE ? AND s.deleted = 0
                LIMIT ?
            """, (f"%{q}%", limit)).fetchall()
            out = []
            for r in rows:
                text = r["content"]
                i = text.find(q)
                start = max(0, i - 16) if i >= 0 else 0
                frag = text[start:start + 42].replace("\n", " ")
                out.append({
                    "id": r["id"], "title": r["title"], "provider_id": r["provider_id"],
                    "session_id": r["session_id"],
                    "snippet": ("…" if start > 0 else "") + frag + "…",
                })
            return out
