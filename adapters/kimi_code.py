import json
import os
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


def _extract_text_from_content(content) -> str:
    """提取 Kimi content 中的文本。支持字符串和列表格式。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                t = c.get("type", "")
                if t == "text":
                    parts.append(c.get("text", ""))
                elif t == "think":
                    parts.append(c.get("think", ""))
                elif t == "thinking":
                    parts.append(f"[思考] {c.get('thinking', '')}")
                elif t == "tool_use":
                    parts.append(f"[工具调用: {c.get('name', '')}] {json.dumps(c.get('input', {}), ensure_ascii=False)}")
                elif t == "tool_result":
                    parts.append(f"[工具结果] {c.get('content', '')}")
                else:
                    # 兜底：取所有字符串值
                    for k, v in c.items():
                        if isinstance(v, str) and v.strip() and k not in ("type", "encrypted"):
                            parts.append(v)
                            break
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(parts)
    return ""


def _read_project_dir_from_kimi_json(session_id: str) -> Optional[str]:
    """从 ~/.kimi/kimi.json 的 work_dirs 中匹配 session_id 获取项目目录。"""
    kimi_json = Path.home() / ".kimi" / "kimi.json"
    if not kimi_json.exists():
        return None
    try:
        with open(kimi_json, "r", encoding="utf-8", errors="ignore") as f:
            data = json.load(f)
        for wd in data.get("work_dirs", []):
            if wd.get("last_session_id") == session_id:
                path = wd.get("path", "")
                if path and os.path.isdir(path):
                    return path
        return None
    except Exception:
        return None


def _read_project_dir_from_state(session_dir: Path, session_id: str) -> Optional[str]:
    """从 state.json 的 custom_title 推断项目目录，失败时回退到 kimi.json。"""
    # 优先从 state.json 的 custom_title 推断
    state_path = session_dir / "state.json"
    if state_path.exists():
        try:
            with open(state_path, "r", encoding="utf-8", errors="ignore") as f:
                data = json.load(f)
            custom_title = data.get("custom_title", "")
            if custom_title:
                # 如果是路径，直接返回
                if os.path.isdir(custom_title):
                    return custom_title
                # 如果是文件路径，取目录
                dirname = os.path.dirname(custom_title)
                if dirname and os.path.isdir(dirname):
                    return dirname
                # 尝试提取路径中的目录部分（处理中文乱码导致的截断）
                for sep in ("\\", "/"):
                    if sep in custom_title:
                        candidate = custom_title.rsplit(sep, 1)[0]
                        if candidate and os.path.isdir(candidate):
                            return candidate
        except Exception:
            pass

    # 回退：从 kimi.json 的 work_dirs 查找
    return _read_project_dir_from_kimi_json(session_id)


class KimiCodeAdapter(ProviderAdapter):
    """Kimi Code CLI 适配器。

    目录结构：
        .kimi/sessions/{workspace_hash}/{session_uuid}/context.jsonl
        .kimi/sessions/{workspace_hash}/context_{hash}/context.jsonl   (旧格式)
    """

    def detect(self) -> bool:
        for p in self.config.get("scan_paths", []):
            if Path(p).exists():
                return True
        return False

    def _find_sessions(self) -> List[tuple]:
        """返回 (session_id, context_jsonl_path, session_dir) 列表。"""
        results = []
        for sp in self.config.get("scan_paths", []):
            base = Path(sp)
            if not base.exists():
                continue
            # 遍历 workspace hash 目录
            for ws_dir in base.iterdir():
                if not ws_dir.is_dir():
                    continue
                # 遍历 session 目录（可能是 UUID 或 context_xxx）
                for session_dir in ws_dir.iterdir():
                    if not session_dir.is_dir():
                        continue
                    ctx_file = session_dir / "context.jsonl"
                    if ctx_file.exists():
                        results.append((session_dir.name, ctx_file, session_dir))
        # 去重（按文件路径）
        seen = set()
        uniq = []
        for sid, path, sdir in results:
            rp = str(path.resolve())
            if rp not in seen:
                seen.add(rp)
                uniq.append((sid, path, sdir))
        return uniq

    def _parse_file(self, session_id: str, path: Path, session_dir: Path) -> Optional[SessionMeta]:
        summary = None
        title = None

        # 尝试从 state.json 读取标题和项目目录
        project_dir = _read_project_dir_from_state(session_dir, session_id)
        state_path = session_dir / "state.json"
        if state_path.exists():
            try:
                with open(state_path, "r", encoding="utf-8", errors="ignore") as f:
                    data = json.load(f)
                title = data.get("custom_title")
                # 如果 custom_title 是路径，用它作为 title 但不覆盖 project_dir
                if title and os.path.exists(title):
                    title = os.path.basename(title) or title
            except Exception:
                pass

        # 从 context.jsonl 提取第一条有效 user 消息作为 summary
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    role = obj.get("role", "")
                    if role == "user":
                        content = obj.get("content", "")
                        text = _extract_text_from_content(content)
                        if text.strip():
                            summary = text[:200] + "..." if len(text) > 200 else text
                        break
        except Exception:
            pass

        stat = path.stat()
        created_at = stat.st_ctime
        last_active_at = stat.st_mtime

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
        sessions = []
        for sid, path, sdir in self._find_sessions():
            meta = self._parse_file(sid, path, sdir)
            if meta:
                sessions.append(meta)
        return sessions

    def load_transcript(self, session_id: str) -> List[Message]:
        for sid, path, sdir in self._find_sessions():
            if sid == session_id:
                return self._load_messages_from_file(path)
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
                    role = obj.get("role", "unknown")
                    content = obj.get("content", "")
                    text = _extract_text_from_content(content)
                    ts = _parse_ts(obj.get("timestamp") or obj.get("ts") or obj.get("created_at"))
                    # 跳过内部元数据行，除非在完整模式下
                    if role.startswith("_"):
                        continue
                    messages.append(Message(role=str(role), content=text, ts=ts, raw=obj))
        except Exception:
            pass
        return messages

    def get_project_dir(self, session_id: str) -> Optional[str]:
        for sid, path, sdir in self._find_sessions():
            if sid == session_id:
                meta = self._parse_file(sid, path, sdir)
                if meta:
                    return meta.project_dir
        return None

    def get_resume_command(self, session_id: str) -> str:
        """生成 Kimi 恢复命令。

        Kimi CLI 的 resume 必须在正确的工作目录下才能找到 session。
        如果知道 project_dir，返回带 ID 的命令（launch 时会自动 cd）；
        否则降级为交互式选择（不带 ID），让 Kimi 自己处理目录切换。
        """
        project_dir = self.get_project_dir(session_id)
        if project_dir and Path(project_dir).exists():
            return f'kimi -r "{session_id}"'
        # project_dir 未知时，使用交互式选择（不带 ID）
        # 这样 Kimi 会显示所有 session 列表，用户选择后自动切换到正确目录
        return 'kimi -r'
