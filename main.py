import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI, Query, Body
from fastapi.staticfiles import StaticFiles
from urllib.parse import quote

from fastapi.responses import Response, FileResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel

from config import DEFAULT_PROVIDER_CONFIGS, STATUS_OPTIONS, DATA_DIR
from storage.index import IndexDB, SessionRecord
from adapters import build_adapter
from exporter import export_markdown, export_pdf, _safe_filename

app = FastAPI(title="Session Manager", version="0.1.0")

db = IndexDB()

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
        db.upsert_sessions(records)
        db.log_scan(provider_id, len(records))
        return {"provider_id": provider_id, "found": len(records)}
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
def get_transcript(session_id: str):
    row = db.get_session(session_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Session not found"})
    configs = get_configs()
    cfg = next((c for c in configs if c["id"] == row["provider_id"]), None)
    if not cfg:
        return JSONResponse(status_code=404, content={"error": "Provider config not found"})
    adapter = build_adapter(cfg)
    messages = adapter.load_transcript(row["session_id"])
    return {
        "session_id": session_id,
        "provider_id": row["provider_id"],
        "messages": [{"role": m.role, "content": m.content, "ts": m.ts} for m in messages]
    }

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
    cmd = adapter.get_resume_command(row["session_id"])
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
    cmd = adapter.get_resume_command(row["session_id"])
    project_dir = row.get("project_dir") or adapter.get_project_dir(row["session_id"])

    # 构建 PowerShell 命令
    # 先设置 UTF-8 编码避免中文乱码/崩溃，再执行 resume 命令
    # 使用 subprocess.Popen 的 cwd 参数设置工作目录，避免路径中的特殊字符（如 [] " ' 空格等）
    # 在命令字符串中被 PowerShell 通配符或引号解析机制误处理
    ps_cmd = f'chcp 65001 > $null; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; {cmd}'

    popen_kwargs = {}
    if project_dir and Path(project_dir).exists():
        popen_kwargs["cwd"] = str(Path(project_dir).resolve())

    try:
        # 在新 PowerShell 窗口中执行
        subprocess.Popen(
            ["powershell", "-NoExit", "-Command", ps_cmd],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            **popen_kwargs,
        )
        return {"launched": True, "command": ps_cmd, "cwd": popen_kwargs.get("cwd")}
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

@app.get("/api/daily-stats")
def daily_stats():
    return db.get_daily_stats()

@app.get("/api/stats")
def stats():
    return db.get_providers_summary()

# 前端静态文件
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=7821)
