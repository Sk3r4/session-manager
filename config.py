import os
from pathlib import Path
from typing import List, Dict, Any

USER_PROFILE = Path(os.environ.get("USERPROFILE", ""))
LOCAL_APPDATA = Path(os.environ.get("LOCALAPPDATA", ""))
ROAMING_APPDATA = Path(os.environ.get("APPDATA", ""))

DEFAULT_PROVIDER_CONFIGS: List[Dict[str, Any]] = [
    {
        "id": "claude-code",
        "name": "Claude Code",
        "enabled": True,
        "adapter_type": "claude-code",
        "scan_paths": [str(USER_PROFILE / ".claude" / "projects")],
        "meta_paths": [str(USER_PROFILE / ".claude" / "sessions")],
        "file_patterns": ["*.jsonl"],
        "resume_command_template": "claude -r \"{session_id}\"",
    },
    {
        "id": "codex",
        "name": "Codex CLI",
        "enabled": True,
        "adapter_type": "codex",
        "scan_paths": [str(USER_PROFILE / ".codex" / "sessions")],
        "file_patterns": ["*.jsonl"],
        "resume_command_template": "codex resume \"{session_id}\"",
    },
    {
        "id": "kimi-code",
        "name": "Kimi Code",
        "enabled": True,
        "adapter_type": "kimi-code",
        "scan_paths": [str(USER_PROFILE / ".kimi" / "sessions")],
        "file_patterns": ["*"],
        "heuristic": {"min_size": 50, "content_checks": []},
        "resume_command_template": "kimi -r \"{session_id}\"",
    },
]

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
INDEX_DB_PATH = DATA_DIR / "index.db"
ARCHIVE_DIR = DATA_DIR / "archives"
ARCHIVE_DIR.mkdir(exist_ok=True)
STATUS_OPTIONS = ["未标注", "进行中", "已完成", "待跟进", "已归档"]

# 自定义归档目录映射：project_dir -> archive_dir
# 示例：{ "D:\\Webtools\\10-Projects\\01-活跃\\Phage": "D:\\Webtools\\10-Projects\\01-活跃\\Phage\\sessions" }
ARCHIVE_MAPPINGS: dict = {}
