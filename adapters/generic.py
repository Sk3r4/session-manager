import json
import os
import hashlib
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime

from adapters.base import ProviderAdapter, SessionMeta, Message

def _parse_ts(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value > 1e12:
            return value / 1000.0
        return float(value)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            pass
        try:
            v = float(value)
            return _parse_ts(v)
        except Exception:
            pass
    return None

class GenericFileAdapter(ProviderAdapter):
    """通用文件扫描适配器：基于配置扫描目录中的 JSON/JSONL 文件。"""

    def detect(self) -> bool:
        for p in self.config.get("scan_paths", []):
            if Path(p).exists():
                return True
        return False

    def _find_files(self) -> List[Path]:
        files = []
        patterns = self.config.get("file_patterns", ["*.json", "*.jsonl"])
        for sp in self.config.get("scan_paths", []):
            base = Path(sp)
            if not base.exists():
                continue
            for pat in patterns:
                try:
                    files.extend(base.rglob(pat))
                except Exception:
                    pass
        seen = set()
        uniq = []
        for f in files:
            rp = str(f.resolve())
            if rp not in seen:
                seen.add(rp)
                uniq.append(f)
        return uniq

    def _is_transcript(self, path: Path) -> bool:
        heuristic = self.config.get("heuristic", {})
        min_size = heuristic.get("min_size", 50)
        if path.stat().st_size < min_size:
            return False
        checks = heuristic.get("content_checks", [])
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                head = f.read(8192)
            for c in checks:
                if c not in head:
                    return False
            return True
        except Exception:
            return False

    def _make_session_id(self, path: Path) -> str:
        stem = path.stem
        path_hash = hashlib.md5(str(path.resolve()).encode()).hexdigest()[:8]
        return f"{stem}_{path_hash}"

    def _parse_file(self, path: Path) -> Optional[SessionMeta]:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                if path.suffix.lower() == ".jsonl":
                    lines = [json.loads(line) for line in f if line.strip()]
                else:
                    data = json.load(f)
                    lines = data if isinstance(data, list) else [data]
        except Exception:
            return None

        if not lines:
            return None

        has_messages = any(
            isinstance(item, dict) and "role" in item and "content" in item
            for item in lines[:20]
        )
        if not has_messages:
            return None

        session_id = self._make_session_id(path)
        project_dir = None
        summary = None
        created_at = None
        last_active_at = None

        for item in lines[:10]:
            if isinstance(item, dict) and item.get("role") == "user":
                content = item.get("content", "")
                if isinstance(content, str):
                    summary = content[:200] + "..." if len(content) > 200 else content
                break

        stat = path.stat()
        created_at = stat.st_ctime
        last_active_at = stat.st_mtime

        return SessionMeta(
            session_id=session_id,
            provider_id=self.provider_id,
            title=None,
            summary=summary,
            project_dir=project_dir,
            created_at=created_at,
            last_active_at=last_active_at,
            source_path=str(path.resolve()),
        )

    def scan_sessions(self) -> List[SessionMeta]:
        files = self._find_files()
        sessions = []
        for f in files:
            if not self._is_transcript(f):
                continue
            meta = self._parse_file(f)
            if meta:
                sessions.append(meta)
        return sessions

    def load_transcript(self, session_id: str) -> List[Message]:
        files = self._find_files()
        for f in files:
            if self._make_session_id(f) == session_id:
                return self._load_messages_from_file(f)
        return []

    def _load_messages_from_file(self, path: Path) -> List[Message]:
        messages = []
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                if path.suffix.lower() == ".jsonl":
                    items = [json.loads(line) for line in f if line.strip()]
                else:
                    data = json.load(f)
                    items = data if isinstance(data, list) else data.get("messages", [data])
        except Exception:
            return []

        for item in items:
            if not isinstance(item, dict):
                continue
            role = item.get("role", "unknown")
            content = item.get("content", "")
            if isinstance(content, list):
                texts = []
                for c in content:
                    if isinstance(c, dict) and "text" in c:
                        texts.append(c["text"])
                    elif isinstance(c, str):
                        texts.append(c)
                content = "\n".join(texts)
            ts = _parse_ts(item.get("timestamp") or item.get("ts") or item.get("created_at"))
            messages.append(Message(role=str(role), content=str(content), ts=ts, raw=item))
        return messages

    def get_project_dir(self, session_id: str) -> Optional[str]:
        files = self._find_files()
        for f in files:
            if self._make_session_id(f) == session_id:
                meta = self._parse_file(f)
                if meta:
                    return meta.project_dir
        return None
