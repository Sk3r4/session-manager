"""IndexDB 单元测试 — 覆盖所有公开方法。"""

import json
from datetime import datetime

import pytest

from storage.index import IndexDB, SessionRecord


# ── CRUD ──────────────────────────────────────────────────────────

class TestUpsertSessions:
    def test_insert_new(self, clean_db):
        """插入新 session → inserted 列表包含该 ID。"""
        rec = SessionRecord(
            id="p::s1", provider_id="p", session_id="s1",
            title="Test", summary="Summary", project_dir="/tmp/p",
            status="未标注", created_at=1000.0, last_active_at=2000.0,
            source_path="/tmp/s1.jsonl", raw_meta="{}",
        )
        inserted, updated = clean_db.upsert_sessions([rec])
        assert "p::s1" in inserted
        assert updated == []

    def test_update_existing_preserves_summary(self, seeded_db):
        """更新已有 session 时保留旧 summary，更新最后活跃时间。"""
        rec = SessionRecord(
            id="test-provider::session-0", provider_id="test-provider",
            session_id="session-0", title="Updated Title",
            summary=None,  # None 不应覆盖原有 summary
            project_dir="/new/path", status="未标注",
            created_at=None, last_active_at=3000.0,
            source_path="/tmp/updated.jsonl", raw_meta="{}",
        )
        _, updated = seeded_db.upsert_sessions([rec])
        assert "test-provider::session-0" in updated
        row = seeded_db.get_session("test-provider::session-0")
        assert row["title"] == "Session 0"  # 保留旧值：COALESCE(old, new) = old
        assert row["summary"] == "Summary for session 0"  # 保留旧值
        assert row["project_dir"] == "/new/path"  # 新值覆盖
        assert row["last_active_at"] == 3000.0


class TestGetSession:
    def test_get_existing(self, seeded_db):
        row = seeded_db.get_session("test-provider::session-0")
        assert row is not None
        assert row["title"] == "Session 0"

    def test_get_missing(self, clean_db):
        assert clean_db.get_session("nonexistent") is None


class TestUpdateTitle:
    def test_set_title(self, seeded_db):
        seeded_db.update_title("test-provider::session-0", "New Title")
        row = seeded_db.get_session("test-provider::session-0")
        assert row["title"] == "New Title"

    def test_clear_title(self, seeded_db):
        seeded_db.update_title("test-provider::session-0", None)
        row = seeded_db.get_session("test-provider::session-0")
        assert row["title"] is None


class TestUpdateStatus:
    def test_valid_status(self, seeded_db):
        seeded_db.update_status("test-provider::session-0", "已完成")
        row = seeded_db.get_session("test-provider::session-0")
        assert row["status"] == "已完成"

    def test_invalid_status_raises(self, seeded_db):
        with pytest.raises(ValueError, match="Invalid status"):
            seeded_db.update_status("test-provider::session-0", "不存在的状态")


# ── Pin ───────────────────────────────────────────────────────────

class TestPin:
    def test_pin_session(self, seeded_db):
        seeded_db.pin_session("test-provider::session-0", pinned=True)
        row = seeded_db.get_session("test-provider::session-0")
        assert row["pinned"] == 1
        assert row["pinned_at"] is not None

    def test_unpin_session(self, pinned_db):
        pinned_db.pin_session("test-provider::session-0", pinned=False)
        row = pinned_db.get_session("test-provider::session-0")
        assert row["pinned"] == 0
        assert row["pinned_at"] is None

    def test_pinned_order(self, pinned_db):
        """已置顶的 session 排在前面（后置顶的 pinned_at 更大，排更前）。"""
        rows = pinned_db.list_sessions()
        # session-1 最后置顶 → pinned_at 最大 → 排第一
        assert rows[0]["id"] == "test-provider::session-1"
        assert rows[1]["id"] == "test-provider::session-0"
        assert rows[0]["pinned"] == 1
        assert rows[1]["pinned"] == 1


# ── Soft Delete ───────────────────────────────────────────────────

class TestDelete:
    def test_soft_delete(self, seeded_db):
        assert seeded_db.delete_session("test-provider::session-0") is True
        row = seeded_db.get_session("test-provider::session-0")
        assert row["deleted"] == 1

    def test_deleted_not_in_list(self, seeded_db):
        seeded_db.delete_session("test-provider::session-0")
        rows = seeded_db.list_sessions()
        ids = [r["id"] for r in rows]
        assert "test-provider::session-0" not in ids

    def test_delete_missing(self, clean_db):
        assert clean_db.delete_session("nonexistent") is False


# ── List ──────────────────────────────────────────────────────────

class TestListSessions:
    def test_list_all(self, seeded_db):
        rows = seeded_db.list_sessions()
        assert len(rows) == 3

    def test_filter_by_provider(self, seeded_db):
        rows = seeded_db.list_sessions(provider="test-provider")
        assert len(rows) == 3
        rows = seeded_db.list_sessions(provider="other")
        assert len(rows) == 0

    def test_filter_by_status(self, seeded_db):
        rows = seeded_db.list_sessions(status="已完成")
        assert len(rows) == 1
        assert rows[0]["id"] == "test-provider::session-2"

    def test_search(self, seeded_db):
        rows = seeded_db.list_sessions(search="Session 0")
        assert len(rows) == 1
        rows = seeded_db.list_sessions(search="nonexistent")
        assert len(rows) == 0


class TestListByDateRange:
    def test_from_ts(self, seeded_db):
        rows = seeded_db.list_sessions_by_date_range(from_ts=1718999000.0)
        assert len(rows) >= 1

    def test_to_ts(self, seeded_db):
        rows = seeded_db.list_sessions_by_date_range(to_ts=1718990000.0)
        assert len(rows) == 0

    def test_project_dir(self, seeded_db):
        rows = seeded_db.list_sessions_by_date_range(
            project_dir="D:/projects/test-0"
        )
        assert len(rows) == 1
        assert rows[0]["id"] == "test-provider::session-0"


# ── Projects ──────────────────────────────────────────────────────

class TestSyncProjects:
    def test_sync_creates_projects(self, seeded_db):
        seeded_db.sync_projects()
        projects = seeded_db.list_projects()
        assert len(projects) == 3  # 3 个不同 project_dir

    def test_project_get(self, seeded_db):
        seeded_db.sync_projects()
        proj = seeded_db.get_project("D:/projects/test-0")
        assert proj is not None
        assert proj["session_count"] == 1
        assert proj["name"] == "test-0"

    def test_update_project_status(self, seeded_db):
        seeded_db.sync_projects()
        seeded_db.update_project_status("D:/projects/test-0", "已归档")
        proj = seeded_db.get_project("D:/projects/test-0")
        assert proj["status"] == "已归档"

    def test_update_display_name(self, seeded_db):
        seeded_db.sync_projects()
        seeded_db.update_project_display_name("D:/projects/test-0", "自定义名称")
        proj = seeded_db.get_project("D:/projects/test-0")
        assert proj["display_name"] == "自定义名称"


# ── Archive ───────────────────────────────────────────────────────

class TestArchive:
    def test_record_and_check(self, seeded_db):
        assert not seeded_db.is_archived("test-provider::session-0")
        seeded_db.record_archive("test-provider::session-0", "/tmp/archive/s0.md", 42)
        assert seeded_db.is_archived("test-provider::session-0")

    def test_get_archive(self, seeded_db):
        seeded_db.record_archive("test-provider::session-1", "/tmp/a.md", 10)
        row = seeded_db.get_archive("test-provider::session-1")
        assert row["archive_path"] == "/tmp/a.md"
        assert row["message_count"] == 10

    def test_list_unarchived(self, seeded_db):
        seeded_db.record_archive("test-provider::session-0", "/tmp/a.md", 1)
        unarchived = seeded_db.list_unarchived_sessions()
        ids = [r["id"] for r in unarchived]
        assert "test-provider::session-0" not in ids
        assert "test-provider::session-1" in ids


# ── Stats ─────────────────────────────────────────────────────────

class TestStats:
    def test_providers_summary(self, seeded_db):
        summary = seeded_db.get_providers_summary()
        assert len(summary) == 1
        assert summary[0]["provider_id"] == "test-provider"
        assert summary[0]["total"] == 3

    def test_daily_stats(self, seeded_db):
        stats = seeded_db.get_daily_stats()
        assert isinstance(stats, dict)
        # 至少有今天的条目
        assert len(stats) >= 1


# ── Handoff ───────────────────────────────────────────────────────

class TestHandoff:
    def test_insert_and_retrieve(self, clean_db):
        rid = clean_db.insert_handoff(
            project_dir="/tmp/p1",
            summary_text="## Summary\nFound 3 bugs",
            session_id="sid-1",
            source="stop_hook",
            current_phase="impl",
            next_action="fix bug #1",
        )
        assert rid > 0
        ctx = clean_db.get_handoff_context("/tmp/p1", limit=5)
        assert len(ctx) == 1
        assert "Found 3 bugs" in ctx[0]["summary_text"]

    def test_get_latest(self, clean_db):
        clean_db.insert_handoff(
            project_dir="/tmp/p1", summary_text="Old",
        )
        clean_db.insert_handoff(
            project_dir="/tmp/p1", summary_text="New",
        )
        latest = clean_db.get_latest_handoff("/tmp/p1")
        assert latest is not None
        assert "New" in latest["summary_text"]

    def test_list_projects(self, clean_db):
        clean_db.insert_handoff(project_dir="/tmp/a", summary_text="A")
        clean_db.insert_handoff(project_dir="/tmp/b", summary_text="B")
        projects = clean_db.list_handoff_projects()
        assert "/tmp/a" in projects
        assert "/tmp/b" in projects


# ── FTS5 Search ───────────────────────────────────────────────────

class TestFTS5:
    def test_search_finds_session(self, seeded_db):
        results = seeded_db.fts5_search("Session 1", limit=10)
        session_results = [r for r in results if r.get("result_type") == "session"]
        assert len(session_results) >= 1

    def test_search_no_results(self, seeded_db):
        results = seeded_db.fts5_search("zzzz_nonexistent_zzzz", limit=10)
        assert len(results) == 0


# ── Patterns ──────────────────────────────────────────────────────

class TestPatterns:
    def test_upsert_and_list(self, clean_db):
        clean_db.upsert_pattern(
            name="test-pattern", ptype="technique",
            title="Test Pattern", file_path="/tmp/patterns/test-pattern.md",
            status="draft", tags='["test"]', source_sessions='["p::s1"]',
        )
        patterns = clean_db.list_patterns()
        assert len(patterns) == 1
        assert patterns[0]["name"] == "test-pattern"

    def test_filter_by_type(self, clean_db):
        clean_db.upsert_pattern(
            name="p1", ptype="technique", title="T1", file_path="/tmp/p1.md",
        )
        clean_db.upsert_pattern(
            name="p2", ptype="workflow", title="W1", file_path="/tmp/p2.md",
        )
        techs = clean_db.list_patterns(ptype="technique")
        assert len(techs) == 1
        assert techs[0]["name"] == "p1"

    def test_update_status(self, clean_db):
        clean_db.upsert_pattern(
            name="p1", ptype="technique", title="T1", file_path="/tmp/p1.md",
        )
        clean_db.update_pattern_status("p1", "verified")
        pattern = clean_db.get_pattern("p1")
        assert pattern["status"] == "verified"

    def test_bump_use_count(self, clean_db):
        clean_db.upsert_pattern(
            name="p1", ptype="technique", title="T1", file_path="/tmp/p1.md",
        )
        clean_db.bump_pattern_use("p1")
        clean_db.bump_pattern_use("p1")
        pattern = clean_db.get_pattern("p1")
        assert pattern["use_count"] == 2

    def test_delete_pattern(self, clean_db):
        clean_db.upsert_pattern(
            name="p1", ptype="technique", title="T1", file_path="/tmp/p1.md",
        )
        assert clean_db.delete_pattern("p1") is True
        assert clean_db.get_pattern("p1") is None


# ── Edge Cases ────────────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_insert(self, clean_db):
        inserted, updated = clean_db.upsert_sessions([])
        assert inserted == []
        assert updated == []

    def test_special_chars_in_title(self, clean_db):
        rec = SessionRecord(
            id="p::special", provider_id="p", session_id="special",
            title="标题 <>&\"'", summary="摘要 emoji 🎉",
            project_dir="/tmp/p", status="未标注",
            created_at=1000.0, last_active_at=2000.0,
            source_path="/tmp/s.jsonl", raw_meta="{}",
        )
        clean_db.upsert_sessions([rec])
        row = clean_db.get_session("p::special")
        assert row["title"] == "标题 <>&\"'"
        assert "🎉" in row["summary"]
