import asyncio
import base64
import io
import json
import logging
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).parent))

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Body, WebSocket
from fastapi.staticfiles import StaticFiles
from urllib.parse import quote

from fastapi.responses import Response, FileResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel

from config import DEFAULT_PROVIDER_CONFIGS, STATUS_OPTIONS, DATA_DIR, ARCHIVE_DIR, ARCHIVE_MAPPINGS, PATTERNS_DIR
from storage.index import IndexDB, SessionRecord
from adapters import build_adapter
from exporter import export_markdown, export_pdf, _safe_filename
from archiver import SessionArchiver
from handoff import HandoffController
from watcher import SessionWatcher, WebSocketManager

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    global watcher
    # 启动文件监听
    configs = get_configs()
    watch_paths: dict = {}
    for cfg in configs:
        if cfg.get("enabled", True):
            paths = set()
            for sp in cfg.get("scan_paths", []):
                p = Path(sp)
                if p.exists():
                    paths.add(str(p))
            if paths:
                watch_paths[cfg["id"]] = paths
    watcher = SessionWatcher(watch_paths, _on_file_changed)
    await watcher.start()
    logger.info("SessionWatcher started")
    yield
    # 关闭文件监听
    if watcher:
        await watcher.stop()
        logger.info("SessionWatcher stopped")


app = FastAPI(title="Session Manager", version="0.1.0", lifespan=lifespan)
START_TIME = time.time()

db = IndexDB()
archiver = SessionArchiver(ARCHIVE_DIR, ARCHIVE_MAPPINGS)
handoff = HandoffController(db)
ws_manager = WebSocketManager()
watcher: Optional[SessionWatcher] = None

# 加载/持久化用户自定义 provider 配置
CONFIG_PATH = DATA_DIR / "providers.json"

def load_provider_configs() -> list:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return DEFAULT_PROVIDER_CONFIGS

def save_provider_configs(configs: list):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(configs, f, ensure_ascii=False, indent=2)

def get_configs() -> list:
    return load_provider_configs()


def _on_file_changed(file_path: str):
    """watchdog 回调：新文件或修改文件 → 解析 → upsert → archive → 广播。"""
    configs = get_configs()
    path = Path(file_path)
    for cfg in configs:
        if not cfg.get("enabled", True):
            continue
        # 检查路径是否属于该 provider 的 scan_paths
        belongs = False
        for sp in cfg.get("scan_paths", []):
            try:
                if Path(sp).resolve() in path.resolve().parents or Path(sp).resolve() == path.resolve().parent:
                    belongs = True
                    break
            except Exception:
                pass
        if not belongs:
            continue

        adapter = build_adapter(cfg)
        if not adapter.detect():
            continue
        try:
            sessions = adapter.scan_sessions()
            for s in sessions:
                # 检查该 session 是否匹配此文件路径
                if s.source_path and Path(s.source_path).resolve() == path.resolve():
                    sid = f"{s.provider_id}::{s.session_id}"
                    record = SessionRecord(
                        id=sid, provider_id=s.provider_id, session_id=s.session_id,
                        title=s.title, summary=s.summary, project_dir=s.project_dir,
                        status="未标注", created_at=s.created_at,
                        last_active_at=s.last_active_at, source_path=s.source_path,
                        raw_meta=json.dumps({"source_path": s.source_path, "title": s.title}, ensure_ascii=False),
                    )
                    inserted, _ = db.upsert_sessions([record])
                    db.sync_projects()

                    # 自动归档
                    for new_sid in inserted:
                        if db.is_archived(new_sid):
                            continue
                        row = db.get_session(new_sid)
                        if not row:
                            continue
                        try:
                            messages = adapter.load_transcript(row["session_id"])
                            archiver.archive(row, [{"role": m.role, "content": m.content, "ts": m.ts} for m in messages])
                            db.record_archive(new_sid, str(archiver.archive_dir), len(messages))
                        except Exception:
                            logger.debug("Auto-archive failed for %s", new_sid, exc_info=True)

                    # WebSocket 广播
                    for sid_key in (inserted + [sid] if sid not in inserted else inserted):
                        async def _broadcast():
                            await ws_manager.broadcast({
                                "type": "new_session",
                                "session_id": sid_key,
                                "title": s.title or s.session_id,
                                "provider_id": s.provider_id,
                            })
                        try:
                            loop = asyncio.get_running_loop()
                            loop.create_task(_broadcast())
                        except RuntimeError:
                            pass  # 无运行中的事件循环
                    break  # 已匹配到正确的 adapter，跳出
        except Exception:
            logger.debug("Watcher: failed to handle %s", file_path, exc_info=True)


@app.get("/api/providers")
def list_providers():
    configs = get_configs()
    result = []
    for c in configs:
        adapter = build_adapter(c)
        detected = adapter.detect()
        result.append({
            "id": c["id"],
            "name": c["name"],
            "enabled": c.get("enabled", True),
            "detected": detected,
            "scan_paths": c.get("scan_paths", []),
        })
    return result

@app.post("/api/providers/{provider_id}/scan")
def scan_provider(provider_id: str):
    configs = get_configs()
    cfg = next((c for c in configs if c["id"] == provider_id), None)
    if not cfg:
        return JSONResponse(status_code=404, content={"error": "Provider not found"})
    if not cfg.get("enabled", True):
        return JSONResponse(status_code=400, content={"error": "Provider disabled"})

    adapter = build_adapter(cfg)
    if not adapter.detect():
        db.log_scan(provider_id, 0, "Provider not detected on this machine")
        return {"provider_id": provider_id, "found": 0, "message": "Provider not detected"}

    try:
        sessions = adapter.scan_sessions()
        records = []
        for s in sessions:
            sid = f"{s.provider_id}::{s.session_id}"
            records.append(SessionRecord(
                id=sid,
                provider_id=s.provider_id,
                session_id=s.session_id,
                title=s.title,
                summary=s.summary,
                project_dir=s.project_dir,
                status="未标注",
                created_at=s.created_at,
                last_active_at=s.last_active_at,
                source_path=s.source_path,
                raw_meta=json.dumps({"source_path": s.source_path, "title": s.title}, ensure_ascii=False),
            ))
        inserted, updated = db.upsert_sessions(records)
        db.sync_projects()

        # 自动归档新插入的会话
        archived_count = 0
        for sid in inserted:
            if db.is_archived(sid):
                continue
            row = db.get_session(sid)
            if not row:
                continue
            try:
                messages = adapter.load_transcript(row["session_id"])
                # 即使 messages 为空（compacted）也执行元数据归档，保留 session 存在记录
                path = archiver.archive(row, [{"role": m.role, "content": m.content, "ts": m.ts} for m in messages])
                db.record_archive(sid, str(path), len(messages))
                archived_count += 1
            except Exception as e:
                logger.warning("Auto-archive failed for %s: %s", sid, e)

        db.log_scan(provider_id, len(records))
        return {"provider_id": provider_id, "found": len(records), "archived": archived_count}
    except Exception as e:
        db.log_scan(provider_id, 0, str(e))
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/scan-all")
def scan_all():
    configs = get_configs()
    results = []
    for cfg in configs:
        if not cfg.get("enabled", True):
            continue
        res = scan_provider(cfg["id"])
        if isinstance(res, JSONResponse):
            body = json.loads(res.body)
            results.append({"provider_id": cfg["id"], **body})
        else:
            results.append(res)
    db.sync_projects()
    return results

@app.get("/api/sessions")
def list_sessions(
    provider: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    from_ts: Optional[float] = Query(None, alias="from"),
    to_ts: Optional[float] = Query(None, alias="to"),
    project_dir: Optional[str] = Query(None)
):
    if from_ts is not None or to_ts is not None or project_dir is not None:
        rows = db.list_sessions_by_date_range(from_ts=from_ts, to_ts=to_ts, project_dir=project_dir)
        # 再应用 provider/status/search 过滤
        if provider:
            rows = [r for r in rows if r["provider_id"] == provider]
        if status:
            rows = [r for r in rows if r["status"] == status]
        if search:
            s = search.lower()
            rows = [r for r in rows if s in (r.get("display_name") or r["session_id"]).lower() or s in (r.get("summary") or "").lower() or s in (r.get("project_dir") or "").lower()]
    else:
        rows = db.list_sessions(provider=provider, status=status, search=search)
    for r in rows:
        r["display_name"] = r["title"] or r["session_id"]
    return rows

class UpdateSessionReq(BaseModel):
    title: Optional[str] = None
    status: Optional[str] = None

@app.put("/api/sessions/{session_id}")
def update_session(session_id: str, req: UpdateSessionReq):
    if req.title is not None:
        db.update_title(session_id, req.title if req.title.strip() else None)
    if req.status is not None:
        db.update_status(session_id, req.status)
    row = db.get_session(session_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Session not found"})
    return row

@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    row = db.get_session(session_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Session not found"})
    return row

@app.get("/api/sessions/{session_id}/transcript")
def get_transcript(session_id: str, use_archive: bool = Query(False)):
    row = db.get_session(session_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Session not found"})

    # 如果请求归档版本且已归档，返回归档的 Markdown
    if use_archive:
        archive_row = db.get_archive(session_id)
        if archive_row:
            md_text = archiver.load_archived_transcript(row)
            if md_text:
                return {
                    "session_id": session_id,
                    "provider_id": row["provider_id"],
                    "from_archive": True,
                    "markdown": md_text,
                }

    configs = get_configs()
    cfg = next((c for c in configs if c["id"] == row["provider_id"]), None)
    if not cfg:
        return JSONResponse(status_code=404, content={"error": "Provider config not found"})
    adapter = build_adapter(cfg)
    messages = adapter.load_transcript(row["session_id"])
    return {
        "session_id": session_id,
        "provider_id": row["provider_id"],
        "from_archive": False,
        "messages": [{"role": m.role, "content": m.content, "ts": m.ts} for m in messages]
    }

@app.get("/api/sessions/{session_id}/archive-status")
def get_archive_status(session_id: str):
    row = db.get_session(session_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Session not found"})
    archive_row = db.get_archive(session_id)
    info = archiver.get_archive_info(row)
    return {
        "session_id": session_id,
        "is_archived": archive_row is not None,
        "archive_path": archive_row["archive_path"] if archive_row else None,
        "archived_at": archive_row["archived_at"] if archive_row else None,
        "message_count": archive_row["message_count"] if archive_row else None,
        "file_exists": info is not None,
    }

@app.post("/api/sessions/{session_id}/archive")
def archive_session(session_id: str):
    row = db.get_session(session_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Session not found"})
    if db.is_archived(session_id):
        return {"archived": True, "session_id": session_id, "message": "Already archived"}

    configs = get_configs()
    cfg = next((c for c in configs if c["id"] == row["provider_id"]), None)
    if not cfg:
        return JSONResponse(status_code=404, content={"error": "Provider config not found"})
    adapter = build_adapter(cfg)
    messages = adapter.load_transcript(row["session_id"])
    # 即使 messages 为空（compacted）也执行元数据归档
    path = archiver.archive(row, [{"role": m.role, "content": m.content, "ts": m.ts} for m in messages])
    db.record_archive(session_id, str(path), len(messages))
    return {"archived": True, "session_id": session_id, "path": str(path), "message_count": len(messages)}

class BatchArchiveReq(BaseModel):
    session_ids: Optional[List[str]] = None
    provider: Optional[str] = None
    project_dir: Optional[str] = None

@app.post("/api/archive/batch")
def batch_archive(req: BatchArchiveReq):
    if req.session_ids:
        targets = [db.get_session(sid) for sid in req.session_ids]
        targets = [t for t in targets if t]
    elif req.provider or req.project_dir:
        targets = db.list_sessions_by_date_range(project_dir=req.project_dir)
        if req.provider:
            targets = [t for t in targets if t["provider_id"] == req.provider]
        targets = [t for t in targets if not db.is_archived(t["id"])]
    else:
        targets = db.list_unarchived_sessions()

    configs = get_configs()
    archived = []
    failed = []
    for row in targets:
        if db.is_archived(row["id"]):
            continue
        cfg = next((c for c in configs if c["id"] == row["provider_id"]), None)
        if not cfg:
            failed.append({"id": row["id"], "reason": "Provider config not found"})
            continue
        adapter = build_adapter(cfg)
        try:
            messages = adapter.load_transcript(row["session_id"])
            path = archiver.archive(row, [{"role": m.role, "content": m.content, "ts": m.ts} for m in messages])
            db.record_archive(row["id"], str(path), len(messages))
            archived.append(row["id"])
        except Exception as e:
            failed.append({"id": row["id"], "reason": str(e)})

    return {"archived": archived, "failed": failed, "total": len(targets)}

@app.get("/api/sessions/{session_id}/copy-command")
def copy_command(session_id: str):
    row = db.get_session(session_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Session not found"})
    configs = get_configs()
    cfg = next((c for c in configs if c["id"] == row["provider_id"]), None)
    if not cfg:
        return JSONResponse(status_code=404, content={"error": "Provider config not found"})
    adapter = build_adapter(cfg)
    raw_sid = row["session_id"]
    cmd = adapter.get_resume_command(raw_sid)
    return {"command": cmd}

@app.post("/api/sessions/{session_id}/launch")
def launch_session(session_id: str):
    row = db.get_session(session_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Session not found"})
    configs = get_configs()
    cfg = next((c for c in configs if c["id"] == row["provider_id"]), None)
    if not cfg:
        return JSONResponse(status_code=404, content={"error": "Provider config not found"})
    adapter = build_adapter(cfg)
    raw_sid = row["session_id"]
    cmd = adapter.get_resume_command(raw_sid)
    project_dir = row.get("project_dir") or adapter.get_project_dir(raw_sid)

    # 构建 PowerShell 命令
    # 先设置 UTF-8 编码避免中文乱码/崩溃，再执行 resume 命令
    # 使用 subprocess.Popen 的 cwd 参数设置工作目录，避免路径中的特殊字符（如 [] " ' 空格等）
    # 在命令字符串中被 PowerShell 通配符或引号解析机制误处理
    # 使用 -EncodedCommand + Base64 防止 session_id 中的特殊字符注入
    ps_script = f'chcp 65001 > $null; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; {cmd}'
    encoded_cmd = base64.b64encode(ps_script.encode('utf-16-le')).decode('ascii')

    popen_kwargs = {}
    if project_dir and Path(project_dir).exists():
        popen_kwargs["cwd"] = str(Path(project_dir).resolve())

    try:
        # 在新 PowerShell 窗口中执行
        subprocess.Popen(
            ["powershell", "-NoExit", "-EncodedCommand", encoded_cmd],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            **popen_kwargs,
        )
        return {"launched": True, "command": cmd, "cwd": popen_kwargs.get("cwd")}
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"无法启动终端: {str(e)}", "command": cmd}
        )

@app.get("/api/sessions/{session_id}/export")
def export_session(session_id: str, format: str = Query("md")):
    try:
        row = db.get_session(session_id)
        if not row:
            return JSONResponse(status_code=404, content={"error": "Session not found"})
        configs = get_configs()
        cfg = next((c for c in configs if c["id"] == row["provider_id"]), None)
        if not cfg:
            return JSONResponse(status_code=404, content={"error": "Provider config not found"})
        adapter = build_adapter(cfg)
        messages = adapter.load_transcript(row["session_id"])

        safe_name = _safe_filename(row.get("title") or row["session_id"])

        if format == "pdf":
            data = export_pdf(row, [{"role": m.role, "content": m.content, "ts": m.ts} for m in messages])
            filename = f"{safe_name}.pdf"
            media_type = "application/pdf"
        else:
            text = export_markdown(row, [{"role": m.role, "content": m.content, "ts": m.ts} for m in messages])
            data = text.encode("utf-8")
            filename = f"{safe_name}.md"
            media_type = "text/markdown; charset=utf-8"

        encoded_name = quote(filename)
        return Response(
            content=data,
            media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"}
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str):
    row = db.get_session(session_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Session not found"})
    db.delete_session(session_id)
    return {"deleted": True}

@app.get("/api/status-options")
def status_options():
    return STATUS_OPTIONS

@app.post("/api/sessions/{session_id}/pin")
def pin_session(session_id: str):
    db.pin_session(session_id, pinned=True)
    row = db.get_session(session_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Session not found"})
    return row

@app.post("/api/sessions/{session_id}/unpin")
def unpin_session(session_id: str):
    db.pin_session(session_id, pinned=False)
    row = db.get_session(session_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Session not found"})
    return row

# ═══════════════════════════════════════════════════════════════
# Handoff API — 跨会话上下文保持
# ═══════════════════════════════════════════════════════════════

class ArchiveHandoffReq(BaseModel):
    project_dir: str
    summary_text: str
    session_id: Optional[str] = None
    source: str = "stop_hook"
    metadata: Optional[dict] = None

@app.post("/api/handoff/archive")
def handoff_archive(req: ArchiveHandoffReq):
    """归档 AI 回复末尾的 handoff 摘要，或 PreCompact 时的完整 transcript。

    Stop hook 调用此端点存储 AI 的带标志位摘要。
    PreCompact hook 调用此端点存储完整 transcript 快照。
    """
    try:
        result = handoff.archive_summary(
            project_dir=req.project_dir,
            summary_text=req.summary_text,
            session_id=req.session_id,
            source=req.source,
            metadata=req.metadata,
        )
        return result
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


class ArchiveTranscriptReq(BaseModel):
    project_dir: str
    transcript_path: str
    session_id: Optional[str] = None

@app.post("/api/handoff/archive-transcript")
def handoff_archive_transcript(req: ArchiveTranscriptReq):
    """PreCompact hook 专用：直接传 transcript 文件路径，由服务端读取并归档。"""
    try:
        result = handoff.archive_full_transcript(
            project_dir=req.project_dir,
            transcript_path=req.transcript_path,
            session_id=req.session_id,
        )
        return result
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/handoff/context")
def handoff_context(project_dir: str = Query(...), limit: int = Query(5)):
    """获取项目的最近 handoff 摘要，返回 Markdown 格式供 AI 直接阅读。

    AI 在新会话或 compaction 后调用此端点恢复上下文。
    返回 compact markdown，可直接作为上下文注入。
    """
    try:
        result = handoff.get_context(project_dir, limit=limit)
        return result
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/handoff/latest")
def handoff_latest(project_dir: str = Query(...)):
    """获取最近一条 handoff 摘要。轻量级查询，适合频繁调用。"""
    try:
        result = handoff.get_latest(project_dir)
        if not result:
            return {"project_dir": project_dir, "found": False, "message": "No handoff entries yet"}
        return {"project_dir": project_dir, "found": True, **result}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/daily-stats")
def daily_stats():
    return db.get_daily_stats()

from datetime import datetime, timedelta

@app.get("/api/daily-archive/{date}")
def get_daily_archive(date: str):
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Invalid date format, expected YYYY-MM-DD"})
    
    start_ts = dt.timestamp()
    end_ts = (dt + timedelta(days=1)).timestamp()
    rows = db.list_sessions_by_date_range(from_ts=start_ts, to_ts=end_ts)
    
    projects = {}
    orphan_sessions = []
    for r in rows:
        r["display_name"] = r["title"] or r["session_id"]
        if r.get("project_dir"):
            pid = r["project_dir"]
            if pid not in projects:
                # 尝试获取项目的显示名
                proj = db.get_project(pid)
                display_name = proj.get("display_name") or proj.get("name") or pid if proj else pid
                projects[pid] = {
                    "display_name": display_name,
                    "path": pid,
                    "sessions": [],
                    "summaries": [],
                    "session_count": 0,
                    "last_active": None,
                }
            projects[pid]["sessions"].append(r["id"])
            if r.get("summary"):
                projects[pid]["summaries"].append(r["summary"][:200])
            projects[pid]["session_count"] += 1
            if r.get("last_active_at"):
                if projects[pid]["last_active"] is None or r["last_active_at"] > projects[pid]["last_active"]:
                    projects[pid]["last_active"] = r["last_active_at"]
        else:
            orphan_sessions.append({
                "id": r["id"],
                "display_name": r["display_name"],
                "provider_id": r["provider_id"],
                "summary": r.get("summary"),
                "last_active_at": r.get("last_active_at"),
            })
    
    # 转换 last_active 为 ISO 字符串
    for p in projects.values():
        if p["last_active"]:
            p["last_active"] = datetime.fromtimestamp(p["last_active"]).isoformat()
    
    return {
        "date": date,
        "projects": projects,
        "orphan_sessions": orphan_sessions,
        "total_sessions": len(rows),
    }

@app.get("/api/daily-archive/{date}/export")
def export_daily_archive(date: str):
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Invalid date format, expected YYYY-MM-DD"})
    
    start_ts = dt.timestamp()
    end_ts = (dt + timedelta(days=1)).timestamp()
    rows = db.list_sessions_by_date_range(from_ts=start_ts, to_ts=end_ts)
    configs = get_configs()
    
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in rows:
            cfg = next((c for c in configs if c["id"] == r["provider_id"]), None)
            if not cfg:
                continue
            adapter = build_adapter(cfg)
            messages = adapter.load_transcript(r["session_id"])
            text = export_markdown(r, [{"role": m.role, "content": m.content, "ts": m.ts} for m in messages])
            safe_name = _safe_filename(r.get("title") or r["session_id"])
            # 按项目分组存放
            project_name = _safe_filename(r.get("project_dir") or "未归类")
            zf.writestr(f"{project_name}/{safe_name}.md", text.encode("utf-8"))
    
    buf.seek(0)
    filename = f"daily_archive_{date}.zip"
    encoded_name = quote(filename)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"}
    )

@app.get("/api/projects")
def list_projects():
    rows = db.list_projects()
    for r in rows:
        r["display_name"] = r.get("display_name") or r.get("name") or r["id"]
    return rows

@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    row = db.get_project(project_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Project not found"})
    row["display_name"] = row.get("display_name") or row.get("name") or row["id"]
    return row

class UpdateProjectReq(BaseModel):
    status: Optional[str] = None
    display_name: Optional[str] = None

@app.put("/api/projects/{project_id}")
def update_project(project_id: str, req: UpdateProjectReq):
    if req.status is not None:
        db.update_project_status(project_id, req.status)
    if req.display_name is not None:
        db.update_project_display_name(project_id, req.display_name)
    row = db.get_project(project_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Project not found"})
    row["display_name"] = row.get("display_name") or row.get("name") or row["id"]
    return row

@app.get("/api/projects/{project_id}/sessions")
def get_project_sessions(project_id: str, provider: Optional[str] = Query(None), status: Optional[str] = Query(None)):
    row = db.get_project(project_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Project not found"})
    sessions = db.get_project_sessions(project_dir=row["path"], provider=provider, status=status)
    for s in sessions:
        s["display_name"] = s["title"] or s["session_id"]
    return sessions

@app.post("/api/projects/{project_id}/export")
def export_project(project_id: str):
    row = db.get_project(project_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Project not found"})

    sessions = db.get_project_sessions(project_dir=row["path"])
    configs = get_configs()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for s in sessions:
            cfg = next((c for c in configs if c["id"] == s["provider_id"]), None)
            if not cfg:
                continue
            adapter = build_adapter(cfg)
            messages = adapter.load_transcript(s["session_id"])
            text = export_markdown(s, [{"role": m.role, "content": m.content, "ts": m.ts} for m in messages])
            safe_name = _safe_filename(s.get("title") or s["session_id"])
            zf.writestr(f"{safe_name}.md", text.encode("utf-8"))

    buf.seek(0)
    safe_project = _safe_filename(row.get("display_name") or row.get("name") or project_id)
    filename = f"{safe_project}_sessions.zip"
    encoded_name = quote(filename)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"}
    )

@app.get("/api/stats")
def stats():
    return db.get_providers_summary()

# ═══════════════════════════════════════════════════════════════
# Session Review API — 人为触发复盘
# ═══════════════════════════════════════════════════════════════

class ReviewReq(BaseModel):
    session_ids: List[str]

@app.post("/api/sessions/review")
def review_sessions(req: ReviewReq):
    """选择一个或多个 session 执行 LLM Review，提取 Pattern。"""
    from reviewer import run_review
    from patterns import title_similarity

    configs = get_configs()
    all_results = []
    for sid in req.session_ids:
        row = db.get_session(sid)
        if not row:
            all_results.append({"session_id": sid, "error": "Session not found", "patterns": []})
            continue
        cfg = next((c for c in configs if c["id"] == row["provider_id"]), None)
        if not cfg:
            all_results.append({"session_id": sid, "error": "Provider not found", "patterns": []})
            continue
        try:
            adapter = build_adapter(cfg)
            messages = adapter.load_transcript(row["session_id"])
            msgs = [{"role": m.role, "content": m.content, "ts": m.ts} for m in messages]
            patterns = run_review(sid, row["provider_id"], msgs,
                                  project_dir=row.get("project_dir") or "")
            for p in patterns:
                # ── 去重：FTS5 搜索已有 Pattern，相似度 > 80% 则合并 ──
                existing = db.list_patterns(search=p["name"].split("-")[0])
                merged = False
                for ex in existing:
                    if title_similarity(p["title"], ex["title"]) > 0.8:
                        # 追加 source_session 到已有 Pattern
                        try:
                            old_sessions = json.loads(ex.get("source_sessions", "[]"))
                        except Exception:
                            old_sessions = []
                        new_sessions = p.get("source_sessions", [])
                        combined = sorted(set(old_sessions + new_sessions))
                        db.upsert_pattern(
                            name=ex["name"], ptype=ex["type"], title=ex["title"],
                            file_path=ex["file_path"], project=ex.get("project", ""),
                            tags=ex.get("tags", "[]"),
                            source_sessions=json.dumps(combined, ensure_ascii=False),
                            status=ex.get("status", "verified"),
                        )
                        db.bump_pattern_use(ex["name"])
                        p["merged_into"] = ex["name"]
                        merged = True
                        break

                if not merged:
                    source_json = json.dumps(p.get("source_sessions", []), ensure_ascii=False)
                    tags_json = json.dumps(p.get("tags", []), ensure_ascii=False)
                    db.upsert_pattern(
                        name=p["name"], ptype=p["type"], title=p["title"],
                        file_path=p["file_path"], project=row.get("project_dir") or "",
                        tags=tags_json, source_sessions=source_json,
                        status="draft",
                    )
            all_results.append({
                "session_id": sid,
                "patterns": patterns,
            })
        except Exception as e:
            import traceback
            traceback.print_exc()
            all_results.append({"session_id": sid, "error": str(e), "patterns": []})
    return {"results": all_results}

# ═══════════════════════════════════════════════════════════════
# Patterns API — CRUD + 聚合
# ═══════════════════════════════════════════════════════════════

class UpdatePatternReq(BaseModel):
    status: Optional[str] = None

class ConsolidateReq(BaseModel):
    prefix: str
    umbrella_name: str

@app.get("/api/patterns")
def list_patterns(
    type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    project: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
):
    return db.list_patterns(ptype=type, status=status, project=project, search=search)

@app.get("/api/patterns/consolidate")
def find_clusters():
    """扫描现有 Pattern，发现可合并的前缀聚类。"""
    from patterns import find_prefix_clusters
    return find_prefix_clusters(db)

@app.post("/api/patterns/consolidate/apply")
def apply_consolidation(req: ConsolidateReq):
    """执行合并：将同一前缀的 Pattern 合并为 Umbrella Pattern。"""
    from patterns import consolidate_cluster
    members = consolidate_cluster(db, req.prefix, req.umbrella_name)
    return {"consolidated": True, "umbrella": req.umbrella_name, "members": members}

@app.get("/api/patterns/{name}")
def get_pattern(name: str):
    row = db.get_pattern(name)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Pattern not found"})
    from patterns import read_pattern_file
    fm_data = read_pattern_file(name, row["type"])
    if fm_data:
        row["body"] = fm_data.get("body", "")
        row["frontmatter"] = fm_data.get("frontmatter", {})
    db.bump_pattern_use(name)
    return row

@app.put("/api/patterns/{name}")
def update_pattern(name: str, req: UpdatePatternReq):
    row = db.get_pattern(name)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Pattern not found"})
    if req.status is not None:
        db.update_pattern_status(name, req.status)
    return db.get_pattern(name)

@app.delete("/api/patterns/{name}")
def delete_pattern(name: str):
    row = db.get_pattern(name)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Pattern not found"})
    from patterns import archive_pattern
    archive_pattern(name, row["type"])
    db.delete_pattern(name)
    return {"deleted": True, "name": name}

@app.post("/api/patterns/{name}/restore")
def restore_pattern(name: str):
    from patterns import restore_pattern as _restore
    path = _restore(name)
    if not path:
        return JSONResponse(status_code=404, content={"error": "Not found in archive"})
    from patterns import _parse_frontmatter
    fm = _parse_frontmatter(path)
    ptype = fm.get("type", "technique")
    title = fm.get("title", name)
    db.upsert_pattern(name=name, ptype=ptype, title=title,
                      file_path=str(path),
                      source_sessions=json.dumps(fm.get("source_sessions", []), ensure_ascii=False),
                      status="verified")
    return {"restored": True, "name": name, "file_path": str(path)}

@app.get("/api/patterns/{name}/file")
def read_pattern_file_content(name: str):
    """直接返回 Pattern Markdown 文件内容，供前端渲染。"""
    row = db.get_pattern(name)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Pattern not found"})
    fm_data = None
    try:
        from patterns import read_pattern_file as _rpf
        fm_data = _rpf(name, row["type"])
    except Exception:
        pass
    return {
        "name": name,
        "title": row["title"],
        "type": row["type"],
        "status": row["status"],
        "frontmatter": fm_data.get("frontmatter", {}) if fm_data else {},
        "body": fm_data.get("body", "") if fm_data else "",
        "file_path": row["file_path"],
    }


# ═══════════════════════════════════════════════════════════════
# Unified Search API — FTS5
# ═══════════════════════════════════════════════════════════════

@app.get("/api/search")
def unified_search(q: str = Query(...), limit: int = Query(20)):
    return db.fts5_search(q, limit=limit)

# 前端静态文件
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/api/health")
def health():
    db_ok = True
    db_error = None
    try:
        db.list_sessions()
    except Exception as e:
        db_ok = False
        db_error = str(e)
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "connected" if db_ok else f"error: {db_error}",
        "uptime_seconds": int(time.time() - START_TIME),
        "version": "0.1.0",
    }


@app.get("/")
def root():
    content = static_dir.joinpath("index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=content, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.get("/{path:path}")
def catch_all(path: str):
    if path.startswith("api/"):
        return JSONResponse(status_code=404, content={"error": "Not found"})
    content = static_dir.joinpath("index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=content, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.websocket("/ws")
async def websocket_endpoint(ws):
    await ws_manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # 保持连接，只接收心跳
    except Exception:
        ws_manager.disconnect(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=7821)
