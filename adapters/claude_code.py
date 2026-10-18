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
    THINKING_PREFIX,
    TOOL_RESULT_PREFIX,
    IMAGE_PLACEHOLDER,
    tool_call_segment,
    flatten_messages,
)

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

def _flat_segment(m: Message) -> str:
    """claude 旧扁平前缀片段（与 _extract_text_from_content 输出一致）。"""
    if m.kind == "thinking":
        return THINKING_PREFIX + m.content
    if m.kind == "tool_use":
        return tool_call_segment(m.name or "", m.content)
    if m.kind == "tool_result":
        return TOOL_RESULT_PREFIX + m.content
    return m.content

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
                        texts.append(THINKING_PREFIX + c["thinking"])
                    elif c.get("type") == "tool_use":
                        name = c.get("name", "")
                        inp = json.dumps(c.get("input", {}), ensure_ascii=False)
                        texts.append(tool_call_segment(name, inp))
                    elif c.get("type") == "tool_result":
                        # content 缺失或为空时不输出，避免产生 "[工具结果] " 空片段
                        result_content = c.get("content")
                        if isinstance(result_content, list):
                            part_texts = [
                                TOOL_RESULT_PREFIX + rc.get("text", "")
                                for rc in result_content
                                if isinstance(rc, dict) and rc.get("type") == "text"
                            ]
                            if part_texts:
                                texts.extend(part_texts)
                            elif result_content:
                                # 列表内无文本块（如纯图片结果），输出占位符
                                texts.append(TOOL_RESULT_PREFIX + IMAGE_PLACEHOLDER)
                        elif isinstance(result_content, str) and result_content:
                            texts.append(TOOL_RESULT_PREFIX + result_content)
                elif isinstance(c, str):
                    texts.append(c)
            return "\n".join(texts)
        return str(content)

    def _decode_project_dir(self, dir_hint: str) -> Optional[str]:
        """将 Claude Code 编码的目录名解码回实际路径。

        Windows: 第一个 '--' 分隔驱动器号和路径（D--foo-bar → D:\\foo\\bar）
        Unix(macOS/Linux): 前导 '-' 即根,- 为路径分隔（-Users-foo-bar → /Users/foo/bar;
        含连字符的目录名有歧义,由存在性校验过滤）
        """
        if "--" in dir_hint:
            # 只替换第一个 -- 作为驱动器分隔符
            parts = dir_hint.split("--", 1)
            drive = parts[0] + ":"
            rest = parts[1].replace("-", "\\") if len(parts) > 1 else ""
            decoded = drive + "\\" + rest if rest else drive + "\\"
            decoded = str(Path(decoded))
            if Path(decoded).exists():
                return decoded
            return None
        if dir_hint.startswith("-"):
            decoded = "/" + dir_hint[1:].replace("-", "/")
            if Path(decoded).exists():
                return decoded
        return None

    def _parse_file(self, path: Path, meta_map: Dict[str, dict]) -> Optional[SessionMeta]:
        session_id = self._make_session_id(path)
        meta = meta_map.get(session_id, {})
        project_dir = None
        summary = None
        title = None
        created_at = None
        last_active_at = None
        jsonl_cwd = None  # JSONL 顶层 cwd 字段（权威项目目录）
        fallback_title = None  # 首条用户消息截断（AI 生成标题退役后的唯一标题来源）

        # 从 JSONL 单遍提取：首条用户消息（摘要与标题）+ 顶层 cwd。
        # AI 生成标题（ai-title 行）已退役（2026-10-10 用户决定）：随会话漂移且质量
        # 不稳，web/MCP 手动改名才是权威——标题行直接跳过，不再解析。
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if not line.strip():
                        continue
                    # 廉价预筛：摘要与 cwd 都已拿到后，其余行直接跳过
                    if summary is not None and jsonl_cwd is not None:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    # 顶层 cwd 是权威的项目目录（历史路径同样有效，不要求存在）
                    if not jsonl_cwd and obj.get("cwd"):
                        jsonl_cwd = obj["cwd"]
                    if summary is None:
                        msg = obj.get("message")
                        if msg and msg.get("role") == "user":
                            text = self._extract_text_from_content(msg.get("content", ""))
                            if not text.strip():
                                # 空消息（如纯图片 tool_result）不算首条用户消息，继续向后找
                                continue
                            clean = ANSI_ESCAPE_RE.sub("", text).strip()
                            # 跳过系统自动生成的命令/系统标记
                            if clean.startswith(SYSTEM_MARKER_PREFIXES):
                                continue
                            summary = clean[:300] + "..." if len(clean) > 300 else clean
                            # 用前 80 字符作为降级标题（无 aiTitle / 注册表 auto 名时）
                            fallback_title = clean[:80] + ("..." if len(clean) > 80 else "")
        except Exception:
            pass

        # 标题 = 首条用户消息截断（稳定，永不漂移）。aiTitle / 注册表自动名 / 机器代号
        # 均为机器生成，已全部退役（2026-10-10）；claude 在 CLI 侧没有人工命名通道，
        # 人工名只来自 web/MCP 改名（title_source='manual'，扫描永不覆盖）。
        title = fallback_title

        # 项目目录优先级：JSONL 顶层 cwd > meta cwd > 路径名解码（最后手段）
        project_dir = jsonl_cwd or (meta.get("cwd") if meta else None)
        if not project_dir:
            parts = path.parts
            for i, part in enumerate(parts):
                if part.lower() == "projects" and i + 1 < len(parts):
                    dir_hint = parts[i + 1]
                    project_dir = self._decode_project_dir(dir_hint)
                    break

        stat = path.stat()
        raw_created = meta.get("startedAt", stat.st_ctime * 1000)
        created_at = _parse_ts(raw_created)
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

    def _messages_from_line(self, msg: dict, line_id: int, ts) -> List[Message]:
        """将一条 JSONL 消息按 content 块拆为结构化 Message 列表（保持顺序）。"""
        out: List[Message] = []
        role = str(msg.get("role", "unknown"))
        content = msg.get("content", "")

        def _kind_for_text(text: str) -> str:
            # 系统标记（命令/提醒信封）不作为用户真实输入
            if role == "user":
                clean = ANSI_ESCAPE_RE.sub("", text).strip()
                if clean.startswith(SYSTEM_MARKER_PREFIXES):
                    return "envelope"
            return "text"

        if isinstance(content, str):
            if content.strip():
                out.append(Message(role=role, content=content, ts=ts, raw=content,
                                   kind=_kind_for_text(content), line_id=line_id))
            return out
        if not isinstance(content, list):
            return out
        for c in content:
            if not isinstance(c, dict):
                if isinstance(c, str) and c.strip():
                    out.append(Message(role=role, content=c, ts=ts, raw=c,
                                       kind=_kind_for_text(c), line_id=line_id))
                continue
            t = c.get("type")
            if t == "text":
                txt = c.get("text", "")
                if txt.strip():
                    out.append(Message(role=role, content=txt, ts=ts, raw=c,
                                       kind=_kind_for_text(txt), line_id=line_id))
            elif t == "thinking":
                think = c.get("thinking", "")
                if think.strip():
                    out.append(Message(role=role, content=think, ts=ts, raw=c,
                                       kind="thinking", line_id=line_id))
            elif t == "tool_use":
                # content 为紧凑 JSON（与旧扁平格式逐字节一致，前端展示时再 pretty-print）
                out.append(Message(role=role, content=json.dumps(c.get("input", {}), ensure_ascii=False),
                                   ts=ts, raw=c, kind="tool_use", name=c.get("name", ""),
                                   call_id=c.get("id"), line_id=line_id))
            elif t == "tool_result":
                rc = c.get("content")
                call_id = c.get("tool_use_id")
                is_error = c.get("is_error")
                if isinstance(rc, list):
                    texts = [x.get("text", "") for x in rc
                             if isinstance(x, dict) and x.get("type") == "text"]
                    if texts:
                        # 多个文本子块 → 多条同 call_id 的 tool_result
                        for txt in texts:
                            out.append(Message(role=role, content=txt, ts=ts, raw=c, kind="tool_result",
                                               call_id=call_id, is_error=is_error, line_id=line_id))
                    elif rc:
                        # 纯图片结果 → 占位符
                        out.append(Message(role=role, content=IMAGE_PLACEHOLDER, ts=ts, raw=c,
                                           kind="tool_result", call_id=call_id,
                                           is_error=is_error, line_id=line_id))
                elif isinstance(rc, str) and rc:
                    out.append(Message(role=role, content=rc, ts=ts, raw=c, kind="tool_result",
                                       call_id=call_id, is_error=is_error, line_id=line_id))
        return out

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
                    msg = obj.get("message")
                    if not msg:
                        continue
                    ts = _parse_ts(obj.get("timestamp"))
                    # 按块拆分；无块产出的行（空内容）自然被跳过
                    messages.extend(self._messages_from_line(msg, line_id, ts))
        except Exception:
            pass
        return messages

    def flatten_transcript(self, messages: List[Message]) -> List[Dict[str, Any]]:
        # claude 旧归档语义：空行丢弃
        return flatten_messages(messages, segment=_flat_segment, drop_empty=True)

    def get_project_dir(self, session_id: str) -> Optional[str]:
        files = self._find_files()
        meta_map = self._load_meta()
        for f in files:
            if f.stem == session_id:
                meta = self._parse_file(f, meta_map)
                if meta:
                    return meta.project_dir
        return None
