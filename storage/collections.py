"""合集与 FS 成员模型域:合集/目录链接/追踪目录/策展(2026-10-15 自 index.py 单体 AST 拆出,方法体原样)。"""
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional
from storage.base import _norm_dir


class CollectionsMixin:
    """合集与 FS 成员模型域:合集/目录链接/追踪目录/策展"""

    def sessions_under_dir(self, directory: str) -> List[Dict[str, Any]]:
        """目录子树时间线:project_dir 等于或位于 directory 之下的可见会话
        ∪ 显式链接(dir_links,符号链接语义:cwd 不在此处的会话也能归入)。

        分隔符归一 + 大小写不敏感;Python 侧比较——路径可能含 LIKE 通配符。
        """
        norm = _norm_dir(directory)
        if not norm:
            return []
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM sessions WHERE deleted = 0 AND project_dir IS NOT NULL AND project_dir != ''"
            ).fetchall()
            linked = conn.execute(
                "SELECT s.* FROM dir_links d JOIN sessions s ON s.id = d.session_id"
                " WHERE d.dir = ? AND s.deleted = 0", (norm,)
            ).fetchall()
        out = {r["id"]: dict(r) for r in linked}
        for r in rows:
            pd = _norm_dir(r["project_dir"])
            if pd == norm or pd.startswith(norm + "/"):
                out[r["id"]] = dict(r)
        return list(out.values())

    def link_to_dir(self, directory: str, session_id: str) -> bool:
        """会话归入目录时间线(符号链接);已在链接返回 False。"""
        norm = _norm_dir(directory)
        if not norm:
            return False
        with self._conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO dir_links (dir, session_id, added_at) VALUES (?, ?, ?)",
                    (norm, session_id, datetime.now().timestamp()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def unlink_from_dir(self, directory: str, session_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM dir_links WHERE dir = ? AND session_id = ?",
                (_norm_dir(directory), session_id),
            )
            return cur.rowcount > 0

    def known_dirs(self, limit: int = 30) -> List[str]:
        """已知目标目录(可见会话的 project_dir 去重,近活跃优先)——AI 归线的候选线。"""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT project_dir, MAX(last_active_at) AS m FROM sessions
                WHERE deleted = 0 AND project_dir IS NOT NULL AND project_dir != ''
                GROUP BY project_dir ORDER BY m DESC NULLS LAST LIMIT ?
            """, (limit,)).fetchall()
        return [r[0] for r in rows]

    def watch_dir(self, directory: str) -> bool:
        """开始追踪目录时间线(幂等);display 保留原始大小写供面板/高亮显示。"""
        norm = _norm_dir(directory)
        if not norm:
            return False
        display = directory.strip().rstrip("\\/")
        with self._conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO watched_dirs (dir, display, created_at) VALUES (?, ?, ?)",
                    (norm, display or directory.strip(), datetime.now().timestamp()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def unwatch_dir(self, directory: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM watched_dirs WHERE dir = ?", (_norm_dir(directory),))
            return cur.rowcount > 0

    def list_watched_dirs(self) -> List[Dict[str, Any]]:
        """全部被追踪的目标目录 + 成员数/最近活跃/最新会话(追踪面板数据源)。"""
        with self._conn() as conn:
            watched = conn.execute(
                "SELECT dir, display FROM watched_dirs ORDER BY created_at"
            ).fetchall()
        out = []
        for d, display in watched:
            members = self.sessions_under_dir(d)  # 子树 ∪ 链接,仅可见
            latest = max(members, key=lambda m: m.get("last_active_at") or 0) if members else None
            out.append({
                "dir": d,
                "display": display or d,
                "count": len(members),
                "last_active_at": latest["last_active_at"] if latest else None,
                "last_session_id": latest["id"] if latest else None,
                "last_title": latest["title"] if latest else None,
                "last_summary": (latest["summary"] or "")[:80] if latest else None,
            })
        return out

    def watched_dir_marks(self) -> Dict[str, List[str]]:
        """watched 目录 → 会话标注(session_id → [display,...]),列表色线高亮用。
        服务端真值:子树前缀 ∪ 链接——不受标题/摘要字面影响(与合集的关键词近似互补)。"""
        with self._conn() as conn:
            watched = conn.execute("SELECT dir, display FROM watched_dirs").fetchall()
            if not watched:
                return {}
            rows = conn.execute("SELECT id, project_dir FROM sessions WHERE deleted = 0").fetchall()
            links: Dict[str, List[str]] = {}
            for sid, d in conn.execute("SELECT session_id, dir FROM dir_links"):
                links.setdefault(sid, []).append(d)
        marks: Dict[str, List[str]] = {}
        for sid, pd in rows:
            npd = _norm_dir(pd or "")
            hit = [display for d, display in watched
                   if (npd and (npd == d or npd.startswith(d + "/"))) or d in links.get(sid, [])]
            if hit:
                marks[sid] = hit
        return marks

    def list_collections(self) -> List[Dict[str, Any]]:
        """全部合集,带可见成员计数(动态合集=自动匹配∪手动)、最近活跃与最新会话。"""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, name, query, watched, created_at, sort_order FROM collections"
                " ORDER BY sort_order, created_at"
            ).fetchall()
            out = []
            for c in rows:
                members = self._collection_members(conn, c["id"])
                latest = members[0] if members else None
                out.append({
                    "id": c["id"], "name": c["name"], "query": c["query"],
                    "watched": bool(c["watched"]), "created_at": c["created_at"],
                    "sort_order": c["sort_order"], "count": len(members),
                    "last_active_at": latest["last_active_at"] if latest else None,
                    "last_session_id": latest["id"] if latest else None,
                    "last_title": latest["title"] if latest else None,
                    "last_summary": (latest["summary"] or "")[:80] if latest else None,
                })
            return out

    def get_collection(self, collection_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM collections WHERE id = ?", (collection_id,)).fetchone()
            return dict(row) if row else None

    def create_collection(self, name: str, query: Optional[str] = None) -> int:
        """创建合集,同名抛 ValueError。query=关键词 → 动态合集(成员=自动匹配∪手动)。"""
        with self._conn() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO collections (name, query, created_at) VALUES (?, ?, ?)",
                    (name, (query or "").strip() or None, datetime.now().timestamp()),
                )
                return cur.lastrowid
            except sqlite3.IntegrityError:
                raise ValueError(f"Collection '{name}' already exists")

    def set_collection_watched(self, collection_id: int, watched: bool) -> bool:
        """切换合集的「追踪」态(面板/列表色线高亮/进度注记的总开关)。"""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE collections SET watched = ? WHERE id = ?",
                (1 if watched else 0, collection_id),
            )
            return cur.rowcount > 0

    def update_collection_query(self, collection_id: int, query: Optional[str]) -> bool:
        """设置/清除动态关键词(空串或 None → 回到静态合集);手动成员不受影响。"""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE collections SET query = ? WHERE id = ?",
                ((query or "").strip() or None, collection_id),
            )
            return cur.rowcount > 0

    def export_curation(self) -> Dict[str, Any]:
        """导出用户策层:手动标题、全部摘要(含扫描默认,不可区分,超集无害)、
        标签、合集(含 query/watched/手动成员)。同机重灌按复合 id 匹配。"""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            sessions: Dict[str, Dict[str, Any]] = {}
            for r in conn.execute("SELECT id, title, summary, title_source FROM sessions"):
                item: Dict[str, Any] = {}
                if r["title_source"] == "manual" and r["title"]:
                    item["title"] = r["title"]
                if r["summary"]:
                    item["summary"] = r["summary"]
                tags = [x[0] for x in conn.execute(
                    "SELECT tag FROM session_tags WHERE session_id = ? ORDER BY added_at, tag",
                    (r["id"],)).fetchall()]
                if tags:
                    item["tags"] = tags
                if item:
                    sessions[r["id"]] = item
            collections = []
            for c in conn.execute("SELECT * FROM collections ORDER BY sort_order, created_at"):
                collections.append({
                    "name": c["name"], "query": c["query"], "watched": bool(c["watched"]),
                    "members": [x[0] for x in conn.execute(
                        "SELECT session_id FROM session_collections WHERE collection_id = ?",
                        (c["id"],)).fetchall()],
                })
            return {"version": 1, "exported_at": datetime.now().isoformat(),
                    "sessions": sessions, "collections": collections}

    def import_curation(self, data: Dict[str, Any]) -> Dict[str, int]:
        """重灌策层(export_curation 的产物)。按复合 id 匹配,不存在的会话跳过;
        合集按名字 get-or-create,query/watched/成员逐一恢复。"""
        stats = {"sessions": 0, "skipped_sessions": 0, "collections": 0}
        for sid, item in (data.get("sessions") or {}).items():
            if not self.get_session(sid):
                stats["skipped_sessions"] += 1
                continue
            if item.get("title"):
                self.update_title(sid, item["title"])
            if item.get("summary"):
                self.update_summary(sid, item["summary"])
            if item.get("tags"):
                self.set_session_tags(sid, item["tags"])
            stats["sessions"] += 1
        for c in (data.get("collections") or []):
            name = c.get("name") or ""
            if not name:
                continue
            cid = next((x["id"] for x in self.list_collections() if x["name"] == name), None)
            if cid is None:
                cid = self.create_collection(name, c.get("query"))
            elif c.get("query"):
                self.update_collection_query(cid, c["query"])
            if c.get("watched"):
                self.set_collection_watched(cid, True)
            for m in (c.get("members") or []):
                if self.get_session(m):
                    self.add_to_collection(cid, m)
            stats["collections"] += 1
        return stats

    def rename_collection(self, collection_id: int, name: str) -> bool:
        with self._conn() as conn:
            try:
                cur = conn.execute("UPDATE collections SET name = ? WHERE id = ?", (name, collection_id))
                return cur.rowcount > 0
            except sqlite3.IntegrityError:
                raise ValueError(f"Collection '{name}' already exists")

    def delete_collection(self, collection_id: int) -> bool:
        """删除合集(成员关系级联清理)。"""
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
            conn.execute("DELETE FROM session_collections WHERE collection_id = ?", (collection_id,))
            return cur.rowcount > 0

    def _collection_members(self, conn: sqlite3.Connection, collection_id: int) -> list:
        """成员计算:手动关系 ∪ 动态匹配(有 query 时:标签精确 + 标题/摘要包含),仅可见会话。"""
        row = conn.execute("SELECT query FROM collections WHERE id = ?", (collection_id,)).fetchone()
        if not row:
            return []
        where = "s.id IN (SELECT session_id FROM session_collections WHERE collection_id = ?)"
        params: List[Any] = [collection_id]
        if row["query"]:
            like = f"%{row['query']}%"
            where += (" OR s.id IN (SELECT session_id FROM session_tags WHERE tag = ?)"
                      " OR s.title LIKE ?"
                      " OR (s.summary IS NOT NULL AND s.summary LIKE ?)")
            params += [row["query"], like, like]
        return conn.execute(f"""
            SELECT DISTINCT s.* FROM sessions s
            WHERE s.deleted = 0 AND ({where})
            ORDER BY s.last_active_at DESC NULLS LAST, s.created_at DESC
        """, params).fetchall()

    def collection_members(self, collection_id: int) -> List[Dict[str, Any]]:
        """合集成员会话(动态合集=自动匹配∪手动;仅 deleted=0),按置顶/最近活跃排序。"""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in self._collection_members(conn, collection_id)]

    def add_to_collection(self, collection_id: int, session_id: str) -> bool:
        """加入合集;已在合集返回 False,新加入返回 True。"""
        with self._conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO session_collections (session_id, collection_id, added_at) VALUES (?, ?, ?)",
                    (session_id, collection_id, datetime.now().timestamp()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def remove_from_collection(self, collection_id: int, session_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM session_collections WHERE collection_id = ? AND session_id = ?",
                (collection_id, session_id),
            )
            return cur.rowcount > 0

    def collections_of_session(self, session_id: str) -> List[Dict[str, Any]]:
        """某会话所属的合集(阅读菜单勾选态)。"""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT c.id, c.name FROM session_collections sc
                JOIN collections c ON c.id = sc.collection_id
                WHERE sc.session_id = ?
                ORDER BY c.sort_order, c.created_at
            """, (session_id,)).fetchall()
            return [dict(r) for r in rows]
