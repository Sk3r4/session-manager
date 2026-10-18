"""项目域:projects 同步与查询(含比赛目录聚合)(2026-10-15 自 index.py 单体 AST 拆出,方法体原样)。"""
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


class ProjectsMixin:
    """项目域:projects 同步与查询(含比赛目录聚合)"""

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
                        INSERT INTO projects (id, name, path, created_at, last_active_at, session_count)
                        VALUES (?, ?, ?, ?, ?, ?)
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

    def get_project_sessions(self, project_dir: str, provider: Optional[str] = None) -> List[Dict[str, Any]]:
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
        sql += " ORDER BY last_active_at DESC NULLS LAST, created_at DESC"
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
