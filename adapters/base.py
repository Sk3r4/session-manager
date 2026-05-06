from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
from pathlib import Path

@dataclass
class Message:
    role: str
    content: str
    ts: Optional[float] = None
    raw: Optional[Any] = None

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

    def get_resume_command(self, session_id: str) -> str:
        template = self.config.get("resume_command_template", "")
        return template.replace("{session_id}", session_id)

    def get_project_dir(self, session_id: str) -> Optional[str]:
        """尝试推断项目目录。子类可覆盖。"""
        return None
