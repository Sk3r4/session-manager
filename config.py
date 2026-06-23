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
PATTERNS_DIR = DATA_DIR / "patterns"
PATTERNS_DIR.mkdir(exist_ok=True)
for _sub in ["preferences", "workflows", "pitfalls", "techniques"]:
    (PATTERNS_DIR / _sub).mkdir(exist_ok=True)
(PATTERNS_DIR / ".archive").mkdir(exist_ok=True)
STATUS_OPTIONS = ["未标注", "进行中", "已完成", "待跟进", "已归档"]

# 自定义归档目录映射：project_dir -> archive_dir
# 示例：{ "D:\\Webtools\\projects\\active\\Phage": "D:\\Webtools\\projects\\active\\Phage\\sessions" }
# Worktree 会话也聚合到主项目的 archive 目录下
ARCHIVE_MAPPINGS: dict = {}

# LLM Review 配置
# provider: "openai" | "openrouter" | "custom"
# 自定义示例：
#   LLM_CONFIG = {
#       "provider": "custom",
#       "base_url": "http://localhost:11434/v1",
#       "api_key": "ollama",
#       "model": "qwen2.5:14b",
#       "temperature": 0.3,
#       "max_tokens": 4096,
#   }
LLM_CONFIG: Dict[str, Any] = {
    "provider": os.environ.get("SESSION_MGR_LLM_PROVIDER", "openai"),
    "base_url": os.environ.get("SESSION_MGR_LLM_BASE_URL", ""),
    "api_key": os.environ.get("SESSION_MGR_LLM_API_KEY", os.environ.get("OPENAI_API_KEY", "")),
    "model": os.environ.get("SESSION_MGR_LLM_MODEL", "gpt-4o-mini"),
    "temperature": 0.3,
    "max_tokens": 4096,
}
PATTERN_TYPES = ["preference", "workflow", "pitfall", "technique"]
PATTERN_STATUSES = ["draft", "verified", "merged", "archived"]
