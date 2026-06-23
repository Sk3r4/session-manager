"""文件监听增量扫描：watchdog + WebSocket 推送。

设计：
  - 监听各 provider 的 scan_paths
  - 新增/修改 .jsonl → 2s debounce → 解析 → upsert + archive
  - 通过 WebSocket 广播新 session ID 给前端
"""

import asyncio
import logging
import threading
from pathlib import Path
from typing import Dict, Set, Optional, Callable, Any

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

logger = logging.getLogger(__name__)


class DebounceHandler(FileSystemEventHandler):
    """文件系统事件处理器，带 debounce 和路径过滤。"""

    def __init__(self, callback: Callable[[str], None], patterns: Set[str] = None):
        super().__init__()
        self._callback = callback
        self._patterns = patterns or {".jsonl"}
        self._pending: Dict[str, asyncio.Task] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._debounce_seconds = 2.0

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def _should_handle(self, path: str) -> bool:
        """过滤：只处理目标扩展名，排除 subagents 目录。"""
        p = path.lower()
        if "subagents" in p:
            return False
        return any(p.endswith(pat) for pat in self._patterns)

    def _schedule(self, path: str):
        """在当前事件循环中排入 debounce 任务。"""
        if not self._loop or self._loop.is_closed():
            return

        # 取消已存在的同路径任务
        if path in self._pending:
            self._pending[path].cancel()

        async def delayed():
            await asyncio.sleep(self._debounce_seconds)
            try:
                self._callback(path)
            except Exception:
                logger.warning("Watcher callback failed for %s", path, exc_info=True)
            finally:
                self._pending.pop(path, None)

        self._pending[path] = self._loop.create_task(delayed())

    def on_created(self, event: FileSystemEvent):
        if not event.is_directory and self._should_handle(event.src_path):
            self._schedule(event.src_path)

    def on_modified(self, event: FileSystemEvent):
        if not event.is_directory and self._should_handle(event.src_path):
            self._schedule(event.src_path)


class SessionWatcher:
    """管理 watchdog Observer 的生命周期。

    用法:
        watcher = SessionWatcher(watch_paths, on_new_file)
        await watcher.start()
        ...
        await watcher.stop()
    """

    def __init__(self, watch_paths: Dict[str, Set[str]], on_new_file: Callable[[str], None]):
        """
        Args:
            watch_paths: {provider_id: {path1, path2, ...}} 要监听的目录
            on_new_file: 回调，接收新增/修改的文件路径
        """
        self._watch_paths = watch_paths
        self._on_new_file = on_new_file
        self._observer: Optional[Observer] = None
        self._handler: Optional[DebounceHandler] = None

    async def start(self):
        """启动文件监听（在后台线程中运行）。"""
        loop = asyncio.get_running_loop()
        self._handler = DebounceHandler(self._on_new_file)
        self._handler.set_loop(loop)

        self._observer = Observer()
        all_paths: Set[str] = set()
        for paths in self._watch_paths.values():
            all_paths.update(paths)

        for p in all_paths:
            path = Path(p)
            if path.exists() and path.is_dir():
                self._observer.schedule(self._handler, str(path), recursive=True)
                logger.info("Watching: %s", path)

        self._observer.start()
        logger.info("SessionWatcher started (%d directories)", len(all_paths))

    async def stop(self):
        """停止文件监听。"""
        if self._observer and self._observer.is_alive():
            self._observer.stop()
            self._observer.join(timeout=5)
            logger.info("SessionWatcher stopped")


class WebSocketManager:
    """简易 WebSocket 连接管理器。"""

    def __init__(self):
        self._connections: Set[Any] = set()

    async def connect(self, websocket):
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket):
        self._connections.discard(websocket)

    async def broadcast(self, data: dict):
        """向所有连接的客户端广播 JSON。"""
        dead = set()
        for ws in self._connections:
            try:
                await ws.send_json(data)
            except Exception:
                dead.add(ws)
        self._connections -= dead
