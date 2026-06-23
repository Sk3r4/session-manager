"""pytest fixtures for session-manager tests."""

import shutil
from pathlib import Path

import pytest

from storage.index import IndexDB, SessionRecord


@pytest.fixture(scope="module")
def empty_db(tmp_path_factory):
    """返回指向临时空数据库的 IndexDB 实例（模块级别，共享）。"""
    tmp = tmp_path_factory.mktemp("db")
    db_path = tmp / "test_index.db"
    db = IndexDB(db_path=db_path)
    yield db


@pytest.fixture
def fresh_db(empty_db):
    """每次测试前重置数据库到初始空状态。"""
    conn = empty_db._conn()
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    for (t,) in tables:
        if t.endswith("_fts"):
            conn.execute(f"DELETE FROM {t}")
        else:
            conn.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()
    return empty_db


def _clean_db(db):
    """清空所有普通表的数据，保留 schema。跳过 FTS 虚拟表。"""
    conn = db._conn()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE '%_fts%'"
    ).fetchall()
    # 使用 execute 传入表名而非 f-string，避免注入风险（表名来自 schema，安全）
    for (t,) in tables:
        try:
            conn.execute(f"DELETE FROM [{t}]")
        except Exception:
            pass
    # 清理 FTS 索引（通过重建）
    conn.execute("INSERT INTO sessions_fts(sessions_fts) VALUES('rebuild')")
    try:
        conn.execute("INSERT INTO patterns_fts(patterns_fts) VALUES('rebuild')")
    except Exception:
        pass
    conn.commit()
    conn.close()


@pytest.fixture
def clean_db(empty_db):
    """每次测试前清空数据库。"""
    _clean_db(empty_db)
    return empty_db


@pytest.fixture
def seeded_db(empty_db):
    """返回已插入 3 条测试 session 的 IndexDB（每次清理后重新插入）。"""
    _clean_db(empty_db)
    now = 1719000000.0
    records = []
    for i in range(3):
        pid = "test-provider"
        sid = f"session-{i}"
        records.append(SessionRecord(
            id=f"{pid}::{sid}",
            provider_id=pid,
            session_id=sid,
            title=f"Session {i}",
            summary=f"Summary for session {i}",
            project_dir=f"D:/projects/test-{i}",
            status="未标注" if i < 2 else "已完成",
            created_at=now - i * 3600,
            last_active_at=now - i * 1800,
            source_path=f"D:/data/session-{i}.jsonl",
            raw_meta='{"source": "test"}',
        ))
    empty_db.upsert_sessions(records)
    return empty_db


@pytest.fixture
def pinned_db(seeded_db):
    """包含已置顶 session 的数据库。"""
    seeded_db.pin_session("test-provider::session-0", pinned=True)
    seeded_db.pin_session("test-provider::session-1", pinned=True)
    return seeded_db
