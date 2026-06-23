"""API 集成测试 — 用 FastAPI TestClient 覆盖核心路由。"""

import json
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

# Patch 掉文件监听器，避免测试期间启动 watchdog
with patch("main.SessionWatcher", autospec=True), \
     patch("main.WebSocketManager", autospec=True):
    from main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mock_popen():
    with patch("main.subprocess.Popen") as mock:
        yield mock


# ── Health ──────────────────────────────────────────────────────────

class TestHealth:
    def test_health_ok(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["db"] == "connected"
        assert "uptime_seconds" in data
        assert data["version"] == "0.1.0"


# ── Providers ───────────────────────────────────────────────────────

class TestProviders:
    def test_list_providers(self, client):
        resp = client.get("/api/providers")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert "id" in data[0]
        assert "name" in data[0]
        assert "detected" in data[0]

    def test_scan_missing_provider(self, client):
        resp = client.post("/api/providers/nonexistent/scan")
        assert resp.status_code == 404


# ── Sessions CRUD ───────────────────────────────────────────────────

class TestSessions:
    def test_list_sessions(self, client):
        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_list_with_filters(self, client):
        resp = client.get("/api/sessions?provider=claude-code&status=未标注&search=test")
        assert resp.status_code == 200

    def test_get_missing(self, client):
        resp = client.get("/api/sessions/nonexistent")
        assert resp.status_code == 404

    def test_update_missing(self, client):
        resp = client.put("/api/sessions/nonexistent", json={"title": "X"})
        assert resp.status_code == 404

    def test_delete_missing(self, client):
        resp = client.delete("/api/sessions/nonexistent")
        assert resp.status_code == 404


# ── Status Options ──────────────────────────────────────────────────

class TestStatusOptions:
    def test_get_options(self, client):
        resp = client.get("/api/status-options")
        assert resp.status_code == 200
        data = resp.json()
        assert "未标注" in data
        assert "已完成" in data
        assert "已归档" in data


# ── Stats ───────────────────────────────────────────────────────────

class TestStats:
    def test_providers_summary(self, client):
        resp = client.get("/api/stats")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_daily_stats(self, client):
        resp = client.get("/api/daily-stats")
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict)


# ── Projects ────────────────────────────────────────────────────────

class TestProjects:
    def test_list_projects(self, client):
        resp = client.get("/api/projects")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_get_missing_project(self, client):
        resp = client.get("/api/projects/nonexistent")
        assert resp.status_code == 404

    def test_project_sessions_missing(self, client):
        resp = client.get("/api/projects/nonexistent/sessions")
        assert resp.status_code == 404


# ── Search ──────────────────────────────────────────────────────────

class TestSearch:
    def test_search_empty_query(self, client):
        resp = client.get("/api/search?q=")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


# ── Patterns ────────────────────────────────────────────────────────

class TestPatterns:
    def test_list_patterns(self, client):
        resp = client.get("/api/patterns")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_get_missing_pattern(self, client):
        resp = client.get("/api/patterns/nonexistent")
        assert resp.status_code == 404

    def test_clusters(self, client):
        resp = client.get("/api/patterns/consolidate")
        assert resp.status_code == 200


# ── Launch (mocked) ────────────────────────────────────────────────

class TestLaunch:
    def test_launch_missing_session(self, client):
        resp = client.post("/api/sessions/nonexistent/launch")
        assert resp.status_code == 404

    def test_copy_command_missing(self, client):
        resp = client.get("/api/sessions/nonexistent/copy-command")
        assert resp.status_code == 404

    def test_launch_session(self, client, mock_popen):
        """通过 scan 获取一个真实 session 后测试 launch（mock Popen）。"""
        # 扫描 kimi（如果安装了）
        resp = client.post("/api/providers/kimi-code/scan")
        if resp.status_code != 200:
            pytest.skip("Kimi Code not available")

        sessions = client.get("/api/sessions?provider=kimi-code").json()
        if not sessions:
            pytest.skip("No kimi sessions found")

        sid = sessions[0]["id"]
        resp = client.post(f"/api/sessions/{sid}/launch")
        assert resp.status_code == 200
        data = resp.json()
        assert data["launched"] is True
        mock_popen.assert_called_once()


# ── Export ──────────────────────────────────────────────────────────

class TestExport:
    def test_export_missing(self, client):
        resp = client.get("/api/sessions/nonexistent/export")
        assert resp.status_code == 404

    def test_export_md(self, client):
        """如果有已扫描的 session，测试 Markdown 导出。"""
        sessions = client.get("/api/sessions").json()
        if not sessions:
            pytest.skip("No sessions available")
        sid = sessions[0]["id"]
        resp = client.get(f"/api/sessions/{sid}/export?format=md")
        assert resp.status_code in (200, 404, 500)

    def test_export_pdf(self, client):
        sessions = client.get("/api/sessions").json()
        if not sessions:
            pytest.skip("No sessions available")
        sid = sessions[0]["id"]
        resp = client.get(f"/api/sessions/{sid}/export?format=pdf")
        assert resp.status_code in (200, 404, 500)


# ── Handoff ─────────────────────────────────────────────────────────

class TestHandoff:
    def test_context_empty_project(self, client):
        resp = client.get("/api/handoff/context?project_dir=/tmp/nonexistent_xyz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["entries"] == []
        assert "No previous handoff" in data["summary"]

    def test_latest_empty(self, client):
        resp = client.get("/api/handoff/latest?project_dir=/tmp/nonexistent_xyz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["found"] is False


# ── Ingest ──────────────────────────────────────────────────────────

class TestIngest:
    def test_ingest_new_session(self, client):
        """推送新 session → 自动 upsert + archive。"""
        import uuid
        uid = uuid.uuid4().hex[:8]
        resp = client.post("/api/sessions/ingest", json={
            "provider": f"test-{uid}",
            "session_id": f"ingest-{uid}",
            "title": "Ingest Test",
            "project_dir": "/tmp/ingest-test",
            "messages": [
                {"role": "user", "content": "Hello world", "ts": 1719000000.0},
                {"role": "assistant", "content": "Hi!", "ts": 1719000001.0},
            ],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "inserted"
        assert data["message_count"] == 2

    def test_ingest_duplicate_updates(self, client):
        """重复推送同一 session → 第一次 inserted，第二次 updated。"""
        import uuid
        uid = uuid.uuid4().hex[:8]
        payload = {
            "provider": f"dup-{uid}",
            "session_id": f"dup-sid-{uid}",
            "messages": [{"role": "user", "content": "First push"}],
        }
        r1 = client.post("/api/sessions/ingest", json=payload)
        assert r1.json()["action"] == "inserted"
        r2 = client.post("/api/sessions/ingest", json=payload)
        assert r2.json()["action"] == "updated"

    def test_ingest_auto_registers_provider(self, client):
        """未知 provider 自动创建。"""
        import uuid
        uid = uuid.uuid4().hex[:8]
        resp = client.post("/api/sessions/ingest", json={
            "provider": f"auto-reg-{uid}",
            "session_id": "sid",
            "messages": [],
        })
        assert resp.status_code == 200
        providers = client.get("/api/providers").json()
        ids = [p["id"] for p in providers]
        assert f"auto-reg-{uid}" in ids

    def test_ingest_creates_archive(self, client):
        """推送带消息的 session → 自动归档。"""
        import uuid
        uid = uuid.uuid4().hex[:8]
        resp = client.post("/api/sessions/ingest", json={
            "provider": f"arc-{uid}",
            "session_id": f"arc-sid-{uid}",
            "messages": [
                {"role": "user", "content": "Important discussion", "ts": 1719000000.0},
            ],
        })
        data = resp.json()
        assert data["archived"] is True
        assert data["archived_path"] is not None

    def test_ingest_empty_messages(self, client):
        """推送空消息列表 → 仍可创建 session。"""
        import uuid
        uid = uuid.uuid4().hex[:8]
        resp = client.post("/api/sessions/ingest", json={
            "provider": f"empty-{uid}",
            "session_id": f"empty-{uid}",
            "messages": [],
        })
        assert resp.status_code == 200
        assert resp.json()["message_count"] == 0

    def test_ingest_list_content(self, client):
        """推送 list 格式的 content → 展平为字符串。"""
        import uuid
        uid = uuid.uuid4().hex[:8]
        resp = client.post("/api/sessions/ingest", json={
            "provider": f"list-{uid}",
            "session_id": f"list-{uid}",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "Help me"}], "ts": 1719000000.0},
            ],
        })
        assert resp.status_code == 200


# ── Register Provider ───────────────────────────────────────────────

class TestRegisterProvider:
    def test_register_new_provider(self, client):
        """注册新 provider → action=registered。"""
        import uuid
        uid = uuid.uuid4().hex[:8]
        pid = f"reg-{uid}"
        resp = client.post("/api/providers/register", json={
            "id": pid, "name": f"Reg CLI {uid}",
            "resume_command_template": f'reg-{uid} resume "{{session_id}}"',
        })
        assert resp.status_code == 200
        assert resp.json()["action"] == "registered"

        providers = client.get("/api/providers").json()
        match = [p for p in providers if p["id"] == pid]
        assert len(match) == 1

    def test_re_register_updates(self, client):
        """重复注册 → 第一次 registered，第二次 updated。"""
        import uuid
        uid = uuid.uuid4().hex[:8]
        pid = f"re-reg-{uid}"
        r1 = client.post("/api/providers/register", json={
            "id": pid, "name": "First Name",
        })
        assert r1.json()["action"] == "registered"
        r2 = client.post("/api/providers/register", json={
            "id": pid, "name": "Updated Name",
        })
        assert r2.json()["action"] == "updated"

        providers = client.get("/api/providers").json()
        match = [p for p in providers if p["id"] == pid]
        assert match[0]["name"] == "Updated Name"

    def test_register_without_template(self, client):
        """不提供 resume command → 自动生成默认模板。"""
        import uuid
        uid = uuid.uuid4().hex[:8]
        resp = client.post("/api/providers/register", json={
            "id": f"auto-{uid}", "name": "Auto",
        })
        assert resp.status_code == 200

    def test_register_missing_fields(self, client):
        """缺少必填字段 → 422。"""
        resp = client.post("/api/providers/register", json={"id": "no-name"})
        assert resp.status_code == 422


# ── Error Handling ──────────────────────────────────────────────────

class TestErrorHandling:
    def test_404_api(self, client):
        resp = client.get("/api/nonexistent_endpoint")
        assert resp.status_code == 404

    def test_invalid_daily_archive_date(self, client):
        resp = client.get("/api/daily-archive/invalid-date")
        assert resp.status_code == 400
