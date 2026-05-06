import json
import os
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime

from adapters.base import ProviderAdapter, SessionMeta, Message

def _parse_ts(value) -> Optional[float]:
    """统一将各种时间格式转为秒级时间戳。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # 判断是毫秒还是秒：大于 1e12 认为是毫秒
        if value > 1e12:
            return value / 1000.0
        return float(value)
    if isinstance(value, str):
        # 尝试 ISO 格式
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            pass
        # 尝试纯数字字符串
        try:
            v = float(value)
            return _parse_ts(v)
        except Exception:
            pass
    return None

class ClaudeCodeAdapter(ProviderAdapter):
    """Claude Code CLI 适配器：解析 .claude/projects/{project}/*.jsonl 格式。"""

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
        files = [f for f in files if "subagents" not in str(f).lower().split(os.sep)]
        seen = set()
        uniq = []
        for f in files:
            rp = str(f.resolve())
            if rp not in seen:
                seen.add(rp)
                uniq.append(f)
        return uniq

    def _load_meta(self) -> Dict[str, dict]:
        meta = {}
        for mp in self.config.get("meta_paths", []):
            base = Path(mp)
            if not base.exists():
                continue
            for f in base.glob("*.json"):
                try:
                    with open(f, "r", encoding="utf-8", errors="ignore") as fp:
                        data = json.load(fp)
                    sid = data.get("sessionId")
                    if sid:
                        meta[sid] = data
                except Exception:
                    pass
        return meta

    def _make_session_id(self, path: Path) -> str:
        return path.stem

    def _extract_text_from_content(self, content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for c in content:
                if isinstance(c, dict):
                    if c.get("type") == "text" and "text" in c:
                        texts.append(c["text"])
                    elif c.get("type") == "thinking" and "thinking" in c:
                        texts.append("[思考] " + c["thinking"])
                    elif c.get("type") == "tool_use":
                        name = c.get("name", "")
                        inp = json.dumps(c.get("input", {}), ensure_ascii=False)
                        texts.append(f"[工具调用: {name}] {inp}")
                    elif c.get("type") == "tool_result" and "content" in c:
                        texts.append(f"[工具结果] {c['content']}")
                elif isinstance(c, str):
                    texts.append(c)
            return "\n".join(texts)
        return str(content)

    def _parse_file(self, path: Path, meta_map: Dict[str, dict]) -> Optional[SessionMeta]:
        session_id = self._make_session_id(path)
        meta = meta_map.get(session_id, {})
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
                    msg = obj.get("message")
                    if msg and msg.get("role") == "user":
                        text = self._extract_text_from_content(msg.get("content", ""))
                        if text.strip():
                            summary = text[:300] + "..." if len(text) > 300 else text
                        break
        except Exception:
            pass

        parts = path.parts
        for i, part in enumerate(parts):
            if part.lower() == "projects" and i + 1 < len(parts):
                dir_hint = parts[i + 1]
                if "--" in dir_hint:
                    project_dir = dir_hint.replace("--", ":\\").replace("-", "\\")
                    project_dir = project_dir.replace("\\\\", "\\")
                break

        stat = path.stat()
        raw_created = meta.get("startedAt", stat.st_ctime * 1000)
        created_at = _parse_ts(raw_created)
        last_active_at = stat.st_mtime

        return SessionMeta(
            session_id=session_id,
            provider_id=self.provider_id,
            title=None,
            summary=summary,
            project_dir=project_dir or meta.get("cwd"),
            created_at=created_at,
            last_active_at=last_active_at,
            source_path=str(path.resolve()),
        )

    def scan_sessions(self) -> List[SessionMeta]:
        files = self._find_files()
        meta_map = self._load_meta()
        sessions = []
        for f in files:
            meta = self._parse_file(f, meta_map)
            if meta:
                sessions.append(meta)
        return sessions

    def load_transcript(self, session_id: str) -> List[Message]:
        files = self._find_files()
        for f in files:
            if f.stem == session_id:
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
                    msg = obj.get("message")
                    if not msg:
                        continue
                    role = msg.get("role", "unknown")
                    content = self._extract_text_from_content(msg.get("content", ""))
                    ts = _parse_ts(obj.get("timestamp"))
                    if not content.strip() and role == "assistant":
                        continue
                    messages.append(Message(role=str(role), content=content, ts=ts, raw=msg))
        except Exception:
            pass
        return messages

    def get_project_dir(self, session_id: str) -> Optional[str]:
        files = self._find_files()
        for f in files:
            if f.stem == session_id:
                meta = self._parse_file(f, self._load_meta())
                if meta:
                    return meta.project_dir
        return None
