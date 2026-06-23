import time
from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
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
    # 缓存 TTL（秒），30s 避免高频扫描磁盘
    CACHE_TTL = 30.0

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.provider_id = config["id"]
        self.provider_name = config["name"]
        self._files_cache: Any = None
        self._files_cache_at: float = 0.0

    def _cached_find_files(self, finder):
        """通用缓存包装：finder() 结果缓存 CACHE_TTL 秒。"""
        now = time.time()
        if self._files_cache is not None and (now - self._files_cache_at) < self.CACHE_TTL:
            return self._files_cache
        self._files_cache = finder()
        self._files_cache_at = now
        return self._files_cache

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
