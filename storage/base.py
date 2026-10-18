"""存储基座:SessionRecord、连接事务管理、目录归一化——全存储域模块共用。"""
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from config import INDEX_DB_PATH
from storage.schema import ensure_schema


def _norm_dir(directory: str) -> str:
    """目录归一(小写/正斜杠/去尾分隔)——目录时间线与链接的统一键。"""
    return (directory or "").replace("\\", "/").rstrip("/").lower()



@dataclass
class SessionRecord:
    id: str
    provider_id: str
    session_id: str
    title: Optional[str]
    summary: Optional[str]
    project_dir: Optional[str]
    created_at: Optional[float]
    last_active_at: Optional[float]
    source_path: Optional[str]
    # 标题作者:None=自动生成/派生(AI 标题会随会话推进漂移);'human'=CLI 侧人工命名
    title_author: Optional[str] = None



class StorageBase:
    """连接与事务基座;各域混入经 IndexDB 组合后共享 db_path/_conn。"""

    def __init__(self, db_path: Path = INDEX_DB_PATH):
        self.db_path = db_path
        with self._conn() as conn:
            ensure_schema(conn)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """事务 + 确保关闭（sqlite3 连接的上下文管理器只管事务不管 close）。"""
        conn = sqlite3.connect(str(self.db_path))
        try:
            with conn:
                yield conn
        finally:
            conn.close()
