import json
import os
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime

from adapters.base import (
    ProviderAdapter,
    SessionMeta,
    Message,
    SYSTEM_MARKER_PREFIXES,
    ANSI_ESCAPE_RE,
    flatten_messages,
)

# codex 在通用 CLI 标记之外还有环境样板消息
_CODEX_MARKER_PREFIXES = ("<environment_context>",) + SYSTEM_MARKER_PREFIXES

# 转录 envelope 标记（仅用于 kind 分类；不影响 _parse_file 的标题/摘要逻辑）
_CODEX_ENVELOPE_PREFIXES = (
    "<environment_context>",
    "<app-context",
    "<skills_instructions",
    "<permissions",
    "<collaboration_mode",
    "<user_instructions",
) + SYSTEM_MARKER_PREFIXES


def _is_envelope(role: str, text: str) -> bool:
    """developer 角色消息与已知信封前缀 → envelope（干净视图隐藏，完整视图保留）。"""
    if role == "developer":
        return True
    clean = ANSI_ESCAPE_RE.sub("", text).strip()
    return clean.startswith(_CODEX_ENVELOPE_PREFIXES)

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
                                text = ANSI_ESCAPE_RE.sub("", " ".join(texts)).strip()
                                # 跳过系统自动生成的消息（环境样板/命令标记/命令输出），取真实首条用户输入
                                if text.startswith(_CODEX_MARKER_PREFIXES):
                                    continue
                                summary = text[:200] + "..." if len(text) > 200 else text
                    if project_dir and summary:
                        break
        except Exception:
            pass

        stat = path.stat()
        if not created_at:
            created_at = stat.st_ctime
        last_active_at = stat.st_mtime

        # 标题 = 首条真实用户消息截断（稳定）。session_index.jsonl 的 thread_name
        # 为机器生成名，已退役（2026-10-10 用户决定），索引文件不再读取。
        title = summary[:80] + ("..." if len(summary) > 80 else "") if summary else None

        return SessionMeta(
            session_id=session_id,
            provider_id=self.provider_id,
            title=title,
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
                for line_id, line in enumerate(f):
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("type") != "response_item":
                        continue
                    payload = obj.get("payload", {})
                    ts = None
                    # 尝试从外层获取时间戳
                    if "timestamp" in obj:
                        try:
                            ts = datetime.fromisoformat(obj["timestamp"].replace("Z", "+00:00")).timestamp()
                        except Exception:
                            pass
                    pt = payload.get("type")
                    if pt == "message":
                        role = str(payload.get("role", "unknown"))
                        parts = payload.get("content", [])
                        if isinstance(parts, str):
                            # 防御：字符串内容按单文本块处理
                            parts = [{"type": "input_text", "text": parts}]
                        emitted = []
                        for c in parts:
                            if isinstance(c, dict) and isinstance(c.get("text"), str):
                                txt = c["text"]
                                emitted.append(Message(role=role, content=txt, ts=ts, raw=c,
                                                       kind="envelope" if _is_envelope(role, txt) else "text",
                                                       line_id=line_id))
                            elif isinstance(c, str):
                                emitted.append(Message(role=role, content=c, ts=ts, raw=c,
                                                       kind="text", line_id=line_id))
                        if not emitted:
                            # 旧语义：无文本块的消息行仍产出一条空消息
                            emitted.append(Message(role=role, content="", ts=ts, raw=payload,
                                                   kind="text", line_id=line_id))
                        messages.extend(emitted)
                    elif pt == "reasoning":
                        # 思考文本只在 summary[].text（原始思维链已加密，无法恢复）
                        texts = [s.get("text", "") for s in payload.get("summary", [])
                                 if isinstance(s, dict)]
                        txt = "\n".join(t for t in texts if t)
                        if txt:
                            messages.append(Message(role="assistant", content=txt, ts=ts, raw=payload,
                                                    kind="thinking", line_id=line_id))
                    elif pt == "function_call":
                        name = payload.get("name") or ""
                        ns = payload.get("namespace")
                        if ns:
                            # MCP 工具：namespace__name
                            name = f"{ns}__{name}"
                        messages.append(Message(role="assistant", content=payload.get("arguments") or "",
                                                ts=ts, raw=payload, kind="tool_use", name=name,
                                                call_id=payload.get("call_id"), line_id=line_id))
                    elif pt == "function_call_output":
                        messages.append(Message(role="user", content=str(payload.get("output") or ""),
                                                ts=ts, raw=payload, kind="tool_result",
                                                call_id=payload.get("call_id"), line_id=line_id))
        except Exception:
            pass
        return messages

    def flatten_transcript(self, messages: List[Message]) -> List[Dict[str, Any]]:
        # codex 旧归档语义：只保留 message 行（text/envelope），思考与工具调用丢弃
        return flatten_messages([m for m in messages if m.kind in ("text", "envelope")])

    def get_project_dir(self, session_id: str) -> Optional[str]:
        files = self._find_files()
        for f in files:
            if self._make_session_id(f) == session_id:
                meta = self._parse_file(f)
                if meta:
                    return meta.project_dir
        return None
