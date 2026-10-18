import json
import os
from pathlib import Path
from typing import List, Dict, Any

# 跨平台用户主目录:Windows 用 USERPROFILE,macOS/Linux 用 HOME(Path.home() 兜底)
USER_HOME = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())
USER_PROFILE = USER_HOME
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
        "scan_paths": [
            str(USER_PROFILE / ".codex" / "sessions"),
            # codex 会把旧会话移入 archived_sessions,一并纳入扫描避免成为幽灵行
            str(USER_PROFILE / ".codex" / "archived_sessions"),
        ],
        "file_patterns": ["*.jsonl"],
        "resume_command_template": "codex resume \"{session_id}\"",
    },
    {
        "id": "zcode",
        "name": "ZCode",
        "enabled": True,
        "adapter_type": "zcode",
        # 适配器在 scan_paths 下定位 db/db.sqlite(只读打开)
        "scan_paths": [str(USER_PROFILE / ".zcode" / "cli")],
        # resume 命令模板暂按 codex 风格,待 ZCode 官方文档核对后修正
        "resume_command_template": "zcode resume \"{session_id}\"",
    },
]

# 数据目录:仓库检出模式(含 .git/static,Web 与 MCP 共用 data/) vs 包安装模式(uvx/pip → ~/.session-manager)。
# SM_DATA_DIR 环境变量可强制覆盖。
_REPO_ROOT = Path(__file__).resolve().parent
if os.environ.get("SM_DATA_DIR"):
    DATA_DIR = Path(os.environ["SM_DATA_DIR"])
elif (_REPO_ROOT / ".git").exists() or (_REPO_ROOT / "static").exists():
    DATA_DIR = _REPO_ROOT / "data"
else:
    DATA_DIR = USER_HOME / ".session-manager"
DATA_DIR.mkdir(exist_ok=True)
INDEX_DB_PATH = DATA_DIR / "index.db"

# 用户自定义 provider 配置持久化（不存在时回落到 DEFAULT_PROVIDER_CONFIGS）
CONFIG_PATH = DATA_DIR / "providers.json"


def load_provider_configs() -> list:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            configs = json.load(f)
        # 合并:默认配置里用户文件缺失的 provider 自动补上(版本升级新增 CLI 对老用户可见)。
        # 已存在的条目原样保留;禁用某 provider 用 "enabled": false(直接删条目会在下次启动复活)。
        known = {c.get("id") for c in configs if isinstance(c, dict)}
        configs.extend(c for c in DEFAULT_PROVIDER_CONFIGS if c.get("id") not in known)
        return configs
    return DEFAULT_PROVIDER_CONFIGS


def save_provider_configs(configs: list):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(configs, f, ensure_ascii=False, indent=2)
