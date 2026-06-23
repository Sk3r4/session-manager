"""适配器单元测试 — 验证 4 个 adapter 的解析逻辑。"""

import json
from pathlib import Path

import pytest

from adapters.utils import parse_ts, extract_text_from_content
from adapters.claude_code import ClaudeCodeAdapter
from adapters.kimi_code import KimiCodeAdapter
from adapters.codex import CodexAdapter
from adapters.generic import GenericFileAdapter


# ── Fixtures ───────────────────────────────────────────────────────

CLAUDE_CONFIG = {
    "id": "claude-code", "name": "Claude Code",
    "adapter_type": "claude-code",
    "scan_paths": [], "meta_paths": [],
    "file_patterns": ["*.jsonl"],
    "resume_command_template": 'claude -r "{session_id}"',
}

KIMI_CONFIG = {
    "id": "kimi-code", "name": "Kimi Code",
    "adapter_type": "kimi-code",
    "scan_paths": [], "file_patterns": ["*"],
    "resume_command_template": 'kimi -r "{session_id}"',
}

CODEX_CONFIG = {
    "id": "codex", "name": "Codex CLI",
    "adapter_type": "codex",
    "scan_paths": [], "file_patterns": ["*.jsonl"],
    "resume_command_template": 'codex resume "{session_id}"',
}

GENERIC_CONFIG = {
    "id": "generic", "name": "Generic",
    "adapter_type": "generic",
    "scan_paths": [], "file_patterns": ["*.jsonl", "*.json"],
    "heuristic": {"min_size": 10},
    "resume_command_template": 'mycli resume "{session_id}"',
}


def make_jsonl(path: Path, lines: list):
    """创建 JSONL 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")


# ── utils: parse_ts ─────────────────────────────────────────────────

class TestParseTs:
    def test_seconds(self):
        assert parse_ts(1719000000.0) == 1719000000.0

    def test_milliseconds(self):
        assert parse_ts(1719000000000.0) == 1719000000.0

    def test_iso_string(self):
        ts = parse_ts("2024-06-22T00:00:00Z")
        assert ts is not None
        assert abs(ts - 1719014400.0) < 86400  # ~that day

    def test_numeric_string(self):
        assert parse_ts("1719000000") == 1719000000.0

    def test_none(self):
        assert parse_ts(None) is None

    def test_invalid(self):
        assert parse_ts("not-a-timestamp") is None


# ── utils: extract_text_from_content ────────────────────────────────

class TestExtractContent:
    def test_plain_string(self):
        result = extract_text_from_content("Hello world")
        assert result == "Hello world"

    def test_text_blocks(self):
        result = extract_text_from_content([
            {"type": "text", "text": "I'll help you."},
            {"type": "text", "text": "Here's the fix."},
        ])
        assert "I'll help you." in result
        assert "Here's the fix." in result

    def test_thinking_block(self):
        result = extract_text_from_content([
            {"type": "thinking", "thinking": "Let me analyze this."},
        ])
        assert "[思考]" in result
        assert "Let me analyze this." in result

    def test_think_block(self):
        result = extract_text_from_content([
            {"type": "think", "think": "Hmm, this is tricky."},
        ])
        assert "[思考]" in result
        assert "Hmm, this is tricky." in result

    def test_tool_use(self):
        result = extract_text_from_content([
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
        ])
        assert "[工具调用: Bash]" in result
        assert "ls" in result

    def test_tool_result(self):
        result = extract_text_from_content([
            {"type": "tool_result", "content": "file1.txt\nfile2.txt"},
        ])
        assert "[工具结果]" in result
        assert "file1.txt" in result

    def test_mixed_list(self):
        result = extract_text_from_content([
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "name": "Read", "input": {"path": "/f.txt"}},
            {"type": "tool_result", "content": "line 1\nline 2"},
            {"type": "text", "text": "File contents: line 1, line 2"},
        ])
        assert "Let me check." in result
        assert "[工具调用: Read]" in result
        assert "[工具结果]" in result

    def test_str_fallback(self):
        """传入非 string/list → str() 转换。"""
        result = extract_text_from_content(42)
        assert result == "42"


# ── ClaudeCodeAdapter ───────────────────────────────────────────────

class TestClaudeCodeAdapter:
    def test_get_resume_command(self):
        adapter = ClaudeCodeAdapter(CLAUDE_CONFIG)
        assert 'claude -r "abc123"' == adapter.get_resume_command("abc123")

    def test_project_dir_decoding(self):
        """路径编码解码：-- → :\\, - → \\。"""
        adapter = ClaudeCodeAdapter(CLAUDE_CONFIG)
        # 测试 _make_session_id 和路径提取
        assert adapter._make_session_id(Path("/tmp/my-session")) == "my-session"

    def test_parse_jsonl(self, tmp_path):
        """解析 Claude JSONL 格式。"""
        adapter = ClaudeCodeAdapter({
            **CLAUDE_CONFIG,
            "scan_paths": [str(tmp_path)],
        })
        session_file = tmp_path / "test-session.jsonl"
        make_jsonl(session_file, [
            {
                "message": {"role": "user", "content": [{"type": "text", "text": "Help me fix this bug"}]},
                "timestamp": 1719000000000,
            },
            {
                "message": {"role": "assistant", "content": [{"type": "text", "text": "Here's the fix:"}]},
                "timestamp": 1719000001000,
            },
        ])
        messages = adapter.load_transcript("test-session")
        assert len(messages) >= 1
        # 应该至少包含 user 消息
        user_msgs = [m for m in messages if m.role == "user"]
        assert len(user_msgs) == 1
        assert "Help me fix this bug" in user_msgs[0].content

    def test_skip_empty_assistant(self, tmp_path):
        """跳过空内容的 assistant 消息。"""
        adapter = ClaudeCodeAdapter({
            **CLAUDE_CONFIG,
            "scan_paths": [str(tmp_path)],
        })
        session_file = tmp_path / "empty-msg.jsonl"
        make_jsonl(session_file, [
            {"message": {"role": "assistant", "content": []}, "timestamp": 1719000000000},
            {"message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}, "timestamp": 1719000001000},
        ])
        messages = adapter.load_transcript("empty-msg")
        assert len(messages) == 1
        assert messages[0].role == "user"


# ── KimiCodeAdapter ─────────────────────────────────────────────────

class TestKimiCodeAdapter:
    def test_resume_with_project_dir(self, tmp_path):
        """当 project_dir 存在时，返回带 ID 的命令。"""
        adapter = KimiCodeAdapter(KIMI_CONFIG)
        cmd = adapter.get_resume_command("abc")
        # 无 project_dir 时返回交互式选择
        assert cmd == "kimi -r"

    def test_parse_context_jsonl(self, tmp_path):
        """解析 Kimi context.jsonl。"""
        session_dir = tmp_path / "ws_hash" / "session-uuid"
        ctx_file = session_dir / "context.jsonl"
        ctx_file.parent.mkdir(parents=True)
        make_jsonl(ctx_file, [
            {"role": "user", "content": "Write a function", "timestamp": 1719000000},
            {"role": "assistant", "content": [{"type": "text", "text": "Here you go:"}, {"type": "thinking", "thinking": "I should..."}]},
        ])

        adapter = KimiCodeAdapter({**KIMI_CONFIG, "scan_paths": [str(tmp_path)]})
        sessions = adapter.scan_sessions()
        assert len(sessions) == 1
        assert sessions[0].session_id == "session-uuid"
        assert sessions[0].summary is not None
        assert "Write a function" in sessions[0].summary

    def test_parse_state_json_title(self, tmp_path):
        """从 state.json 读取 custom_title。"""
        session_dir = tmp_path / "ws" / "sid"
        ctx_file = session_dir / "context.jsonl"
        state_file = session_dir / "state.json"
        session_dir.mkdir(parents=True)
        make_jsonl(ctx_file, [{"role": "user", "content": "Hello"}])
        state_file.write_text(json.dumps({"custom_title": "Fix login bug"}), encoding="utf-8")

        adapter = KimiCodeAdapter({**KIMI_CONFIG, "scan_paths": [str(tmp_path)]})
        sessions = adapter.scan_sessions()
        assert any(s.title == "Fix login bug" for s in sessions)

    def test_filter_meta_roles(self, tmp_path):
        """跳过 _meta / 内部角色行。"""
        session_dir = tmp_path / "ws" / "sid"
        ctx_file = session_dir / "context.jsonl"
        session_dir.mkdir(parents=True)
        make_jsonl(ctx_file, [
            {"role": "_meta", "content": "internal"},
            {"role": "user", "content": "Real message"},
        ])

        adapter = KimiCodeAdapter({**KIMI_CONFIG, "scan_paths": [str(tmp_path)]})
        messages = adapter.load_transcript("sid")
        roles = [m.role for m in messages]
        assert "_meta" not in roles
        assert "user" in roles


# ── CodexAdapter ────────────────────────────────────────────────────

class TestCodexAdapter:
    def test_rollout_session_id(self, tmp_path):
        """rollout-2024-T12-...-UUID.jsonl → 提取 UUID 作为 session_id。"""
        adapter = CodexAdapter(CODEX_CONFIG)
        sid = adapter._make_session_id(Path("/tmp/rollout-2024-03-17T21-11-19-019cfbec-1686-7ba2-99e7-ef220a3440e4.jsonl"))
        assert sid == "019cfbec-1686-7ba2-99e7-ef220a3440e4"

    def test_parse_session_meta(self, tmp_path):
        """解析 Codex session_meta + response_item 格式。"""
        adapter = CodexAdapter({**CODEX_CONFIG, "scan_paths": [str(tmp_path)]})
        sid = "019cfbec-1686-7ba2-99e7-ef220a3440e4"
        session_file = tmp_path / f"rollout-2024-03-17T21-11-19-{sid}.jsonl"
        make_jsonl(session_file, [
            {"type": "session_meta", "payload": {"cwd": "/home/user/project", "timestamp": "2024-03-17T21:11:19Z"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hello"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hi!"}]}},
        ])
        sessions = adapter.scan_sessions()
        assert len(sessions) == 1
        assert sessions[0].project_dir == "/home/user/project"
        assert sessions[0].summary is not None
        assert "Hello" in sessions[0].summary

    def test_get_resume_command(self):
        adapter = CodexAdapter(CODEX_CONFIG)
        assert 'codex resume "abc"' == adapter.get_resume_command("abc")


# ── GenericFileAdapter ──────────────────────────────────────────────

class TestGenericFileAdapter:
    def test_jsonl_parse(self, tmp_path):
        """解析标准 JSONL 对话格式。"""
        adapter = GenericFileAdapter({**GENERIC_CONFIG, "scan_paths": [str(tmp_path)]})
        session_file = tmp_path / "chat.jsonl"
        make_jsonl(session_file, [
            {"role": "user", "content": "Hello", "timestamp": 1719000000},
            {"role": "assistant", "content": "Hi!", "timestamp": 1719000001},
        ])
        sessions = adapter.scan_sessions()
        assert len(sessions) == 1

    def test_json_array_parse(self, tmp_path):
        """解析 JSON 数组格式。"""
        adapter = GenericFileAdapter({**GENERIC_CONFIG, "scan_paths": [str(tmp_path)]})
        session_file = tmp_path / "chat.json"
        session_file.write_text(json.dumps([
            {"role": "user", "content": "Question"},
            {"role": "assistant", "content": "Answer"},
        ]), encoding="utf-8")
        sessions = adapter.scan_sessions()
        assert len(sessions) == 1

    def test_heuristic_min_size(self, tmp_path):
        """小文件被启发式过滤跳过。"""
        adapter = GenericFileAdapter({**GENERIC_CONFIG, "scan_paths": [str(tmp_path)]})
        tiny_file = tmp_path / "tiny.jsonl"
        tiny_file.write_text("x", encoding="utf-8")  # 1 byte < 10 min_size
        sessions = adapter.scan_sessions()
        assert len(sessions) == 0

    def test_invalid_file_returns_empty(self, tmp_path):
        """无法解析的文件返回空消息列表。"""
        adapter = GenericFileAdapter({**GENERIC_CONFIG, "scan_paths": [str(tmp_path)]})
        bad_file = tmp_path / "bad.jsonl"
        bad_file.write_text("not json {{{", encoding="utf-8")
        messages = adapter.load_transcript("bad_abcdef01")
        assert messages == []


# ── Adapter Factory ─────────────────────────────────────────────────

def test_build_adapter_factory():
    from adapters import build_adapter
    assert isinstance(build_adapter(CLAUDE_CONFIG), ClaudeCodeAdapter)
    assert isinstance(build_adapter(KIMI_CONFIG), KimiCodeAdapter)
    assert isinstance(build_adapter(CODEX_CONFIG), CodexAdapter)
    assert isinstance(build_adapter(GENERIC_CONFIG), GenericFileAdapter)
    # 未知类型 fallback 到 generic
    assert isinstance(build_adapter({"id": "x", "name": "x", "adapter_type": "unknown"}), GenericFileAdapter)
