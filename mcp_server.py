"""Session Manager MCP Server（stdio）。

让 AI 客户端（ZCode / Claude Code / Codex 等）通过 MCP 自动获取本机的
AI CLI 会话历史——"我之前在这个目录/这个项目干过什么"的环境上下文。

复用 Web 应用的同一套模块（storage/adapters/services/config），
读写同一个 data/index.db；stdio 进程按需拉起，不依赖 Web 服务运行。

工具面设计:读为主(环境上下文/搜索/列表/转录/内容全文搜索) + 受控写
(合集归类、摘要与标签标注、会话改名) + 索引刷新。不暴露删除/隐藏等破坏性操作。

接入(任一 AI 客户端的 mcpServers 配置):
    {"sessions": {"command": "<python>", "args": ["D:\\\\session-manager\\\\mcp_server.py"]}}
"""
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP

from config import load_provider_configs
from storage.index import IndexDB
from adapters import build_adapter
from transcript_util import merge_tool_rows, smart_summary

mcp = FastMCP("session-manager")

# 懒初始化:测试可注入临时库(monkeypatch 模块级 db 变量)
db: IndexDB = None


def _get_db() -> IndexDB:
    global db
    if db is None:
        db = IndexDB()
    return db


def _find_adapter(provider_id: str):
    cfg = next((c for c in load_provider_configs() if c["id"] == provider_id), None)
    return build_adapter(cfg) if cfg else None


def _current_session_id():
    """解析"调用方自己"的会话 id。

    Claude Code 给所有子进程(MCP server 亦然)注入 CLAUDE_CODE_SESSION_ID——官方文档化
    契约(code.claude.com/docs/en/env-vars:"Use to correlate scripts and external tools
    with the Claude Code session that launched them"),实测与宿主会话 id 逐字一致。

    文档化边界:MCP server 子进程**保留 spawn 时的 id**——宿主 /clear 或 --continue/
    无显式 id 的 resume 之后,此值可能指向旧会话;此时调用方应显式传 session_id。
    没有该变量(非 claude-code 客户端)同样返回 None——不做 cwd 猜测,猜错会话比多问一句更糟。"""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    return f"claude-code::{sid}" if sid else None


def _fmt_ts(ts) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def _brief(row: dict) -> dict:
    """列表项的紧凑投影。"""
    return {
        "id": row["id"],
        "title": row.get("title") or row.get("display_name") or row.get("session_id"),
        "provider": row["provider_id"],
        "project_dir": row.get("project_dir"),
        "last_active": _fmt_ts(row.get("last_active_at")),
        "summary": (row.get("summary") or "")[:200] or None,
    }


def _norm(p: str) -> str:
    """Windows 路径归一(大小写/分隔符),用于目录前缀匹配。"""
    import os

    return os.path.normcase(str(Path(p).resolve())) if p else ""


def _is_related(project_dir: str, cwd: str) -> bool:
    """目录关联:同目录 / 会话项目在 cwd 之下 / cwd 在会话项目之下。"""
    import os

    p = project_dir.rstrip("\\/")
    c = cwd.rstrip("\\/")
    return p == c or p.startswith(c + os.sep) or c.startswith(p + os.sep)


# ---------------------------------------------------------------- 工具 ----

@mcp.tool()
def get_environment_context(cwd: str = "") -> dict:
    """获取当前工作目录的会话环境上下文——AI 会话开始时调用它即可知道
    "之前在这个目录/项目发生过什么"。返回:该目录(含子目录与祖先项目)的
    相关会话(按最近活跃排序)、今天全局动态、各 CLI 会话量。
    参数 cwd: 当前工作目录,AI 应传自己的工作目录;留空则用本进程 cwd。"""
    cwd = _norm(cwd or str(Path.cwd()))
    if not cwd:
        return {"error": "无法确定工作目录,请显式传 cwd"}

    rows = _get_db().list_sessions()
    related = []
    for r in rows:
        pd = _norm(r.get("project_dir") or "")
        if pd and _is_related(pd, cwd):
            related.append(r)
    related.sort(key=lambda r: r.get("last_active_at") or 0, reverse=True)

    today_key = datetime.now().strftime("%Y-%m-%d")
    today = [r for r in rows if _fmt_ts(r.get("last_active_at")).startswith(today_key)]

    by_provider = {}
    for r in rows:
        by_provider[r["provider_id"]] = by_provider.get(r["provider_id"], 0) + 1

    return {
        "cwd": cwd,
        "current_session_id": _current_session_id(),
        "related_sessions": [_brief(r) for r in related[:8]],
        "related_total": len(related),
        "today_sessions": [_brief(r) for r in today[:3]],
        "today_total": len(today),
        "totals": {"sessions": len(rows), "by_provider": by_provider},
        "hint": "用 get_session_transcript(session_id) 读取某个会话的完整对话;用 search_sessions(query) 跨项目检索。current_session_id 是调用方自己(如已识别)——改名/写摘要/标注直接用它,无需在列表里辨认。",
    }


@mcp.tool()
def search_sessions(query: str, limit: int = 20) -> list:
    """按关键词跨项目检索会话(命中标题/摘要/项目目录/会话 ID)。
    返回紧凑会话列表,按最近活跃排序。"""
    if not query.strip():
        return []
    rows = _get_db().list_sessions(search=query.strip())
    return [_brief(r) for r in rows[: max(1, min(limit, 50))]]


@mcp.tool()
def list_sessions(provider: str = "", days: int = 0, limit: int = 50) -> list:
    """列出会话。provider 可选(claude-code/codex/kimi-code);
    days>0 时只看最近 N 天;默认按最近活跃排序,最多 50 条。"""
    kwargs = {}
    if provider:
        kwargs["provider"] = provider
    if days and days > 0:
        kwargs["from_ts"] = datetime.now().timestamp() - days * 86400
    rows = _get_db().list_sessions(**kwargs)
    return [_brief(r) for r in rows[: max(1, min(limit, 100))]]


@mcp.tool()
def get_session(session_id: str) -> dict:
    """获取单个会话的元数据(标题/摘要/项目目录/时间/源文件路径)。"""
    row = _get_db().get_session(session_id)
    if not row:
        return {"error": f"session not found: {session_id}"}
    out = _brief(row)
    out["source_path"] = row.get("source_path")
    out["collections"] = [c["name"] for c in _get_db().collections_of_session(session_id)]
    return out


@mcp.tool()
def get_session_transcript(session_id: str, mode: str = "clean", last_n: int = 30, max_chars: int = 40000) -> dict:
    """读取会话的对话转录。mode="clean" 只返回用户/AI 正文+工具摘要(推荐),
    mode="full" 返回逐行完整内容(含思考与工具原文)。last_n 只取最近 N 条消息
    (会话结论通常在末尾),max_chars 为总字符上限(超出截断)。"""
    d = _get_db()
    row = d.get_session(session_id)
    if not row:
        return {"error": f"session not found: {session_id}"}
    adapter = _find_adapter(row["provider_id"])
    if not adapter:
        return {"error": f"provider config not found: {row['provider_id']}"}
    messages = adapter.load_transcript(row["session_id"])
    if not messages:
        return {"session_id": session_id, "messages": [], "note": "无对话内容(可能已被 compacting 清除)"}

    if mode == "full":
        flat = adapter.flatten_transcript(messages)
        lines = [f"[{m['role']}] {m.get('content') or ''}" for m in flat]
        msgs = lines[-last_n:] if last_n and last_n > 0 else lines
        text = "\n\n".join(msgs)
        return {
            "session_id": session_id,
            "title": row.get("title") or session_id,
            "mode": "full",
            "message_count": len(lines),
            "transcript": text[:max_chars],
            "truncated": len(text) > max_chars,
        }

    # clean 模式:envelope 丢弃,思考/工具折叠为一行摘要(tool_use 与 tool_result 按 call_id 配对)
    msgs = [m.to_dict() for m in messages if (m.kind or "text") != "envelope"]
    merged = merge_tool_rows(msgs)
    out = []
    for m in merged:
        k = m.get("kind", "text")
        if k == "thinking":
            out.append(f"[thinking] {(m.get('content') or '')[:200]}")
        elif k == "tool_use":
            summary = smart_summary(m.get("name") or "", m.get("content") or "")
            err = " (错误)" if m.get("is_error") else ""
            out.append(f"[tool{err}] {m.get('name')}: {summary}")
        elif k == "tool_result":
            out.append(f"[tool_result] {(m.get('content') or '')[:200]}")
        else:
            content = m.get("content") or ""
            if content.strip():
                out.append(f"[{m.get('role')}] {content}")
    picked = out[-last_n:] if last_n and last_n > 0 else out
    text = "\n\n".join(picked)
    return {
        "session_id": session_id,
        "title": row.get("title") or session_id,
        "mode": "clean",
        "message_count": len(out),
        "transcript": text[:max_chars],
        "truncated": len(text) > max_chars,
    }


@mcp.tool()
def list_collections() -> list:
    """列出全部合集(用户自定义分类)及其会话数。"""
    return [
        {"id": c["id"], "name": c["name"], "count": c["count"]}
        for c in _get_db().list_collections()
    ]


@mcp.tool()
def add_session_to_collection(session_id: str, collection: str) -> dict:
    """把会话加入合集(不存在则自动创建)。用于 AI 在阅读会话后自动归类整理。
    collection: 合集名,如 "Sovell 白转黑"。"""
    d = _get_db()
    if not d.get_session(session_id):
        return {"error": f"session not found: {session_id}"}
    name = collection.strip()
    if not name:
        return {"error": "合集名不能为空"}
    cid = next((c["id"] for c in d.list_collections() if c["name"] == name), None)
    created = False
    if cid is None:
        try:
            cid = d.create_collection(name)
            created = True
        except ValueError:
            return {"error": f"合集「{name}」已存在"}
    added = d.add_to_collection(cid, session_id)
    return {"collection": name, "collection_id": cid, "created": created, "added": added}


@mcp.tool()
def search_content(query: str, limit: int = 20) -> list:
    """按对话内容全文检索会话——不依赖记得标题,记得当时聊过什么就能找到。
    返回命中片段;与 search_sessions(标题/摘要/目录元数据检索)互补。"""
    if not query.strip():
        return []
    rows = _get_db().search_content(query.strip(), limit=max(1, min(limit, 50)))
    return [
        {
            "id": r["id"],
            "title": r.get("title") or r.get("session_id"),
            "provider": r["provider_id"],
            "snippet": r.get("snippet"),
        }
        for r in rows
    ]


@mcp.tool()
def annotate_session(session_id: str = "", summary: str = "", tags: list[str] | None = None) -> dict:
    """给会话写一句话摘要与标签(AI 整理/归类用)。
    session_id 可省略 = 当前会话(自动定位,无需查询)。
    summary 覆写后扫描不会再覆盖(检索句柄,给"不记得聊过什么"的场景用);
    tags 为整组替换;两者都留空则不改动。受控写面——只能丰富索引,不能删改会话。"""
    d = _get_db()
    if not session_id or session_id == "current":
        session_id = _current_session_id() or ""
        if not session_id:
            return {"error": "无法自动定位当前会话(环境缺少 CLAUDE_CODE_SESSION_ID),请显式传 session_id"}
    if not d.get_session(session_id):
        return {"error": f"session not found: {session_id}"}
    if summary:
        d.update_summary(session_id, summary.strip())
    if tags is not None:
        d.set_session_tags(session_id, tags)
    row = d.get_session(session_id)
    return {
        "session_id": session_id,
        "summary": row.get("summary"),
        "tags": d.get_session_tags(session_id),
    }


@mcp.tool()
def rename_session(title: str, session_id: str = "") -> dict:
    """给会话改名(写入 manual 标题:持久、扫描不覆盖;CLI 侧后续改名仍按"谁后改谁赢"同步)。
    session_id 可省略 = 自动定位当前会话——用户说"把标题改成X/把这个会话改名为X"时
    直接调用即可,一次到位,无需先查询自己是哪个会话。也可传完整复合 id 或纯 uuid。
    边界:宿主会话若发生过 /clear 或 resume,自动定位可能指向旧会话——拿不准就显式传 id。"""
    d = _get_db()
    if not session_id or session_id == "current":
        session_id = _current_session_id() or ""
        if not session_id:
            return {"error": "无法自动定位当前会话(环境缺少 CLAUDE_CODE_SESSION_ID),请显式传 session_id"}
    if "::" not in session_id:
        session_id = f"claude-code::{session_id}"
    if not d.get_session(session_id):
        return {"error": f"session not found: {session_id}"}
    title = title.strip()
    if not title:
        return {"error": "标题不能为空(留空恢复默认请在 Web UI 操作)"}
    d.update_title(session_id, title)
    return {"session_id": session_id, "title": title}


@mcp.tool()
def refresh_index() -> dict:
    """扫描全部 provider,把最新的 CLI 会话文件同步进索引。
    如果 Web 服务没在跑(没有实时监控),调用它保证数据新鲜。"""
    from services.scan import scan_all

    results = scan_all(_get_db(), load_provider_configs())
    total = sum(r.get("found", 0) for r in results)
    return {"scanned": [r.get("provider_id") for r in results], "total_sessions": total}


def main():
    """MCP server 入口(pyproject 的 console_scripts 指向这里)。"""
    mcp.run()  # stdio 传输


if __name__ == "__main__":
    main()
