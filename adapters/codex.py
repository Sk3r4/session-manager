import json
import os
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime

from adapters.base import ProviderAdapter, SessionMeta, Message

class CodexAdapter(ProviderAdapter):
    """Codex CLI 适配器：解析 .codex/sessions/YYYY/MM/DD/rollout-*.jsonl 格式。"""

    def detect(self) -> bool:
        for p in self.config.get("scan_paths", []):
            if Path(p).exists():
                return True
        return False

    def _find_files(self) -> List[Path]:
        files = []
        for sp in self.config.get("scan_paths", []):
            base = Path(sp)
            if not base.exists():
                continue
            for pat in self.config.get("file_patterns", ["*.jsonl"]):
                try:
                    files.extend(base.rglob(pat))
                except Exception:
                    pass
        # 去重
        seen = set()
        uniq = []
        for f in files:
            rp = str(f.resolve())
            if rp not in seen:
                seen.add(rp)
                uniq.append(f)
        return uniq

    def _make_session_id(self, path: Path) -> str:
        # rollout-2026-03-17T21-11-19-019cfbec-1686-7ba2-99e7-ef220a3440e4.jsonl
        # -> 019cfbec-1686-7ba2-99e7-ef220a3440e4
        stem = path.stem
        if stem.startswith("rollout-"):
            parts = stem.split("-")
            # 找到 UUID 部分（最后5段）
            if len(parts) >= 6:
                return "-".join(parts[-5:])
        return stem

    def _parse_file(self, path: Path) -> Optional[SessionMeta]:
        session_id = self._make_session_id(path)
        project_dir = None
        summary = None
        created_at = None
        last_active_at = None

        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("type") == "session_meta":
                        payload = obj.get("payload", {})
                        project_dir = payload.get("cwd")
                        ts = payload.get("timestamp")
                        if ts:
                            try:
                                created_at = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                            except Exception:
                                pass
                    # 提取第一条用户消息作为摘要
                    if obj.get("type") == "response_item":
                        payload = obj.get("payload", {})
                        if payload.get("type") == "message" and payload.get("role") == "user":
                            content = payload.get("content", [])
                            texts = []
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "input_text":
                                    texts.append(c.get("text", ""))
                            if texts and not summary:
                                text = " ".join(texts)
                                summary = text[:200] + "..." if len(text) > 200 else text
                    if project_dir and summary:
                        break
        except Exception:
            pass

        stat = path.stat()
        if not created_at:
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
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("type") != "response_item":
                        continue
                    payload = obj.get("payload", {})
                    if payload.get("type") != "message":
                        continue
                    role = payload.get("role", "unknown")
                    content_parts = payload.get("content", [])
                    texts = []
                    for c in content_parts:
                        if isinstance(c, dict) and "text" in c:
                            texts.append(c["text"])
                        elif isinstance(c, str):
                            texts.append(c)
                    content = "\n".join(texts)
                    ts = None
                    # 尝试从外层获取时间戳
                    if "timestamp" in obj:
                        ts_str = obj["timestamp"]
                        try:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                        except Exception:
                            pass
                    messages.append(Message(role=str(role), content=content, ts=ts, raw=payload))
        except Exception:
            pass
        return messages

    def get_project_dir(self, session_id: str) -> Optional[str]:
        files = self._find_files()
        for f in files:
            if self._make_session_id(f) == session_id:
                meta = self._parse_file(f)
                if meta:
                    return meta.project_dir
        return None
