import json
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

from exporter import export_markdown


class SessionArchiver:
    """会话归档器：将原始完整对话导出为 Markdown 备份，防止 CLI compacting 后数据丢失。"""

    def __init__(self, archive_dir: Path, custom_mappings: Optional[Dict[str, str]] = None):
        self.archive_dir = archive_dir
        self.custom_mappings = custom_mappings or {}
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def _get_archive_dir(self, session: dict) -> Path:
        """确定某个会话的归档目录。优先使用自定义映射，否则按 provider/日期 组织。"""
        project_dir = session.get("project_dir")
        if project_dir and project_dir in self.custom_mappings:
            base = Path(self.custom_mappings[project_dir])
        else:
            provider = session.get("provider_id", "unknown")
            date_str = self._ts_to_date(session.get("last_active_at"))
            base = self.archive_dir / provider / date_str
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _ts_to_date(self, ts: Optional[float]) -> str:
        if not ts:
            return "unknown"
        try:
            dt = datetime.fromtimestamp(ts)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return "unknown"

    def archive(self, session: dict, messages: List[Dict[str, Any]]) -> Path:
        """归档单个会话。返回归档后的 Markdown 文件路径。"""
        archive_dir = self._get_archive_dir(session)
        session_id = session["id"]
        safe_name = self._safe_filename(session.get("title") or session.get("session_id", "unknown"))

        md_path = archive_dir / f"{safe_name}_{session_id}.md"
        meta_path = archive_dir / f"{safe_name}_{session_id}.meta.json"

        # 导出 Markdown
        md_text = export_markdown(session, messages)
        md_path.write_text(md_text, encoding="utf-8")

        # 写入元数据
        meta = {
            "session_id": session_id,
            "provider_id": session.get("provider_id"),
            "project_dir": session.get("project_dir"),
            "archived_at": datetime.now().isoformat(),
            "message_count": len(messages),
            "source_path": session.get("source_path"),
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        return md_path

    def get_archive_info(self, session: dict) -> Optional[dict]:
        """检查某个会话是否已归档，返回归档元数据。"""
        archive_dir = self._get_archive_dir(session)
        session_id = session["id"]
        safe_name = self._safe_filename(session.get("title") or session.get("session_id", "unknown"))

        meta_path = archive_dir / f"{safe_name}_{session_id}.meta.json"
        if meta_path.exists():
            try:
                return json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return None

    def load_archived_transcript(self, session: dict) -> Optional[str]:
        """加载已归档的 Markdown 内容。"""
        archive_dir = self._get_archive_dir(session)
        session_id = session["id"]
        safe_name = self._safe_filename(session.get("title") or session.get("session_id", "unknown"))

        md_path = archive_dir / f"{safe_name}_{session_id}.md"
        if md_path.exists():
            try:
                return md_path.read_text(encoding="utf-8")
            except Exception:
                pass
        return None

    def _safe_filename(self, name: str) -> str:
        invalid = '<>:"/\\|?*'
        for ch in invalid:
            name = name.replace(ch, '_')
        return name[:50]
