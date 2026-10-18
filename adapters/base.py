from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
from pathlib import Path
import re

# 系统自动生成消息的前缀标记（CLI 命令标记/输出/系统提醒，不作为标题/摘要来源）
SYSTEM_MARKER_PREFIXES = (
    "<local-command-caveat>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<system-reminder>",
)
# ANSI 转义序列（如 \x1b[1m 加粗），不应进入标题/摘要
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# 旧扁平格式的块前缀（_extract_text_from_content 与 flatten 共用，避免漂移）
THINKING_PREFIX = "[思考] "
TOOL_RESULT_PREFIX = "[工具结果] "
IMAGE_PLACEHOLDER = "[图片]"


def tool_call_segment(name: str, content: str) -> str:
    """工具调用块的旧扁平片段（适配器与 flatten 共用，防漂移）。"""
    return f"[工具调用: {name}] {content}"


@dataclass
class Message:
    role: str
    content: str
    ts: Optional[float] = None
    raw: Optional[Any] = None
    # —— 结构化转录字段（transcript readability，全部向后兼容默认值）——
    kind: str = "text"               # text | thinking | tool_use | tool_result | envelope
    name: Optional[str] = None       # tool_use 的工具名
    call_id: Optional[str] = None    # tool_use.id / tool_result.tool_use_id —— 前端配对键
    is_error: Optional[bool] = None  # tool_result 的错误标记（codex 恒为 None）
    line_id: int = -1                # 原始 JSONL 行号（flatten 归组键；-1 = 无行概念）

    def to_dict(self) -> Dict[str, Any]:
        """转录 API / 清洁导出所需的结构化字段（不含 raw）。"""
        return {"role": self.role, "content": self.content, "ts": self.ts,
                "kind": self.kind, "name": self.name, "call_id": self.call_id,
                "is_error": self.is_error, "line_id": self.line_id}

@dataclass
class SessionMeta:
    session_id: str
    provider_id: str
    title: Optional[str] = None
    summary: Optional[str] = None
    project_dir: Optional[str] = None
    created_at: Optional[float] = None
    last_active_at: Optional[float] = None
    source_path: Optional[str] = None
    # 标题作者:None = AI 生成/派生(claude ai-title、zcode generated、首条消息提取——
    # 会随会话推进漂移);'human' = CLI 侧人工命名(唯一可覆盖 manual 标题的信号)
    title_author: Optional[str] = None


def _default_flat_segment(m: "Message") -> str:
    """单条结构化消息 → 旧扁平文本片段（默认前缀，与 claude 前缀一致）。"""
    if m.kind == "thinking":
        return THINKING_PREFIX + m.content
    if m.kind == "tool_use":
        return tool_call_segment(m.name or "", m.content)
    if m.kind == "tool_result":
        return TOOL_RESULT_PREFIX + m.content
    return m.content


def flatten_messages(messages: List["Message"], *, segment=None, drop_empty: bool = False) -> List[Dict[str, Any]]:
    """按 line_id 将 per-block 结构化消息归组回旧的逐行扁平格式。

    - line_id >= 0：同一行号连续出现 → 归为一行，片段以 \\n 连接；
    - line_id < 0（无行概念，如 generic 适配器）：每条消息独立成行；
    - drop_empty：归组后内容为空则丢弃整行（claude 旧语义；kimi/codex 旧语义保留）。
    返回 [{"role", "content", "ts"}]，与旧 load_transcript 输出同构。
    """
    seg = segment or _default_flat_segment
    out: List[Dict[str, Any]] = []

    def _flush(group: List["Message"]):
        if not group:
            return
        content = "\n".join(seg(m) for m in group)
        if drop_empty and not content.strip():
            return
        out.append({"role": group[0].role, "content": content, "ts": group[0].ts})

    group: List["Message"] = []
    current_line = None
    for m in messages:
        if m.line_id < 0:
            _flush(group)
            group = []
            current_line = None
            _flush([m])
            continue
        if current_line is None or m.line_id != current_line:
            _flush(group)
            group = [m]
            current_line = m.line_id
        else:
            group.append(m)
    _flush(group)
    return out

class ProviderAdapter(ABC):
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.provider_id = config["id"]
        self.provider_name = config["name"]

    @abstractmethod
    def detect(self) -> bool:
        """检测该 provider 是否在本机有数据。"""
        pass

    @abstractmethod
    def scan_sessions(self) -> List[SessionMeta]:
        """扫描所有会话元数据。"""
        pass

    @abstractmethod
    def load_transcript(self, session_id: str) -> List[Message]:
        """加载指定会话的完整对话。"""
        pass

    def flatten_transcript(self, messages: List[Message]) -> List[Dict[str, Any]]:
        """将结构化消息还原为旧扁平 {role, content, ts} 列表（归档/兼容用）。

        默认全类型 + 保留空行；各适配器按旧语义覆写。
        """
        return flatten_messages(messages)

    def get_resume_command(self, session_id: str, project_dir: Optional[str] = None) -> str:
        # project_dir 参数供需要工作目录的 provider（如 Kimi）使用；模板版本忽略它
        template = self.config.get("resume_command_template", "")
        return template.replace("{session_id}", session_id)

    def get_project_dir(self, session_id: str) -> Optional[str]:
        """尝试推断项目目录。子类可覆盖。"""
        return None
