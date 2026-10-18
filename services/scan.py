"""扫描编排：单 provider 扫描、全量扫描与幽灵行清理、轻量变更探测（实时监控）。"""
from typing import Dict, List, Tuple

from errors import ApiError
from adapters import build_adapter
from services.annotate import auto_annotate_async
from storage.index import SessionRecord


def scan_provider(db, configs: list, provider_id: str) -> dict:
    cfg = next((c for c in configs if c["id"] == provider_id), None)
    if not cfg:
        raise ApiError(404, "Provider not found")
    if not cfg.get("enabled", True):
        raise ApiError(400, "Provider disabled")

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
                created_at=s.created_at,
                last_active_at=s.last_active_at,
                source_path=s.source_path,
                title_author=s.title_author,
            ))
        inserted, _ = db.upsert_sessions(records)
        db.sync_projects()
        # 同步清理:CLI 删除/归档会话文件后残留的幽灵行 → 软删(源文件确实不存在的才删)。
        # db 型适配器(zcode)删会话 = 删库里的行,源文件恒在 → 信任扫描结果全集
        removed_count = db.soft_delete_absent(
            provider_id, [r.id for r in records],
            trust_live_ids=getattr(adapter, "DB_BACKED", False),
        )

        # 内容全文索引增量同步(FTS5;失败不阻断扫描)
        _sync_content_index(db, adapter, records)

        # AI 自动标注:仅新增会话,后台线程执行(复用 ~/.claude/settings.json 配置;
        # 未配置密钥 = 功能关闭)。poll 携带完成计数驱动前端刷新。
        if inserted:
            inserted_ids = set(inserted)
            auto_annotate_async(db, adapter, [r for r in records if r.id in inserted_ids])

        db.log_scan(provider_id, len(records))
        return {"provider_id": provider_id, "found": len(records), "removed": removed_count}
    except ApiError:
        raise
    except Exception as e:
        db.log_scan(provider_id, 0, str(e))
        raise ApiError(500, str(e))


def _sync_content_index(db, adapter, records):
    """内容索引增量同步:只重建 last_active_at 有变化(或首次)的会话,
    只索引对话文本(text/thinking,工具输出不进索引)。
    失败不阻断扫描——基线未前移,下次扫描自动重试。"""
    try:
        by_id = {r.id: r for r in records}
        stale = db.fts_stale_ids(records)
        for rid in stale:
            r = by_id[rid]
            msgs = adapter.load_transcript(r.session_id)
            text = "\n".join(m.content for m in msgs
                             if m.content and m.kind in ("text", "thinking"))
            db.fts_upsert(rid, r.last_active_at, text)
        if stale:
            db.fts_orphan_cleanup()
    except Exception:
        pass


def scan_all(db, configs: list) -> list:
    results = []
    for cfg in configs:
        if not cfg.get("enabled", True):
            continue
        try:
            results.append(scan_provider(db, configs, cfg["id"]))
        except ApiError as e:
            # 单个 provider 失败不中断全量扫描,错误并入结果(与旧 API 返回结构一致)
            results.append({"provider_id": cfg["id"], "error": e.message})
    return results


# ---- 轻量变更探测(SSE 事件流与 /api/poll 共用的签名对比)----
# 会话文件签名: path -> (mtime, size)。仅 stat 不读内容,毫秒级。
# /api/poll 用模块级 _last_sig;SSE 每连接私有 state——互不抢事件。
_last_sig: Dict[str, Tuple[float, int]] = {}


def collect_signatures(configs: list) -> Dict[str, Tuple[float, int]]:
    """当前全部 provider 会话文件的 stat 签名。"""
    current: Dict[str, Tuple[float, int]] = {}
    for cfg in configs:
        if not cfg.get("enabled", True):
            continue
        adapter = build_adapter(cfg)
        if not adapter.detect():
            continue
        try:
            files = adapter._find_files()
        except Exception:
            continue
        for f in files:
            try:
                key = str(f.resolve())
                st = f.stat()
            except OSError:
                continue
            current[key] = (st.st_mtime, st.st_size)
    return current


def diff_signatures(db, prev: Dict[str, Tuple[float, int]],
                    current: Dict[str, Tuple[float, int]]) -> dict:
    """对比两份签名表 → {changed, changed_session_ids, changed_count}。

    changed_session_ids 为 DB 中已有会话里源文件发生变化的 id,
    用于正在阅读的会话更新(贴底跟随/更新提示条)。
    """
    changed_paths = [k for k in current if k not in prev or prev[k] != current[k]]
    removed = [p for p in prev if p not in current]
    changed = bool(changed_paths or removed)
    changed_session_ids: List[str] = []
    if changed and changed_paths:
        changed_session_ids = db.session_ids_by_source_paths(changed_paths)
    return {
        "changed": changed,
        "changed_session_ids": changed_session_ids,
        "changed_count": len(changed_paths) + len(removed),
    }


def poll_changes(db, configs: list) -> dict:
    """/api/poll:对比模块级签名表并前移。
    服务重启后首次调用必然 changed=True(顺带完成启动时的初始同步)。"""
    global _last_sig
    current = collect_signatures(configs)
    result = diff_signatures(db, _last_sig, current)
    _last_sig.clear()
    _last_sig.update(current)
    return result


def next_diff(db, configs: list, state: Dict[str, Tuple[float, int]]) -> dict:
    """SSE 每 tick:对比连接私有签名表并前移——多连接互不抢事件。"""
    current = collect_signatures(configs)
    result = diff_signatures(db, state, current)
    state.clear()
    state.update(current)
    return result
