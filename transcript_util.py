"""转录工具纯函数层:无第三方依赖,Web 导出(exporter.py)与 MCP(mcp_server.py)共用。

- one_line / smart_summary: 工具调用的单行智能摘要
  (与前端 frontend/src/lib/smartSummary.ts 用同一份夹具对拍锁定,
   见 frontend/src/lib/smartSummary.cases.json 与 tests/test_smart_summary_parity.py)
- merge_tool_rows: tool_result 按 call_id 并入配对的 tool_use;孤立结果保留独立行
"""
import json
import re
from typing import Any, Dict, List


def one_line(t, n: int = 140) -> str:
    """压平空白并截断的单行摘要。"""
    s = re.sub(r"\s+", " ", str(t or "")).strip()
    return s[:n] + "…" if len(s) > n else s


def smart_summary(name: str, input_json: str) -> str:
    """工具调用的智能一行摘要(与前端 smartSummary 保持同步,夹具对拍锁定)。"""
    try:
        inp = json.loads(input_json) if input_json else {}
    except Exception:
        inp = {}
    if not isinstance(inp, dict):
        inp = {}
    if name == "Bash":
        return one_line(inp.get("command"))
    if name == "Read":
        rng = ""
        if inp.get("offset") is not None:
            rng = f":{inp['offset']}"
            if inp.get("limit"):
                rng = f":{inp['offset']}-{inp['offset'] + inp['limit']}"
        return (inp.get("file_path") or "") + rng
    if name in ("Edit", "Write", "NotebookEdit"):
        return one_line(inp.get("file_path"))
    if name == "Grep":
        parts = []
        if inp.get("pattern"):
            parts.append(f'"{inp["pattern"]}"')
        if inp.get("path"):
            parts.append(inp["path"])
        return " in ".join(parts)
    if name == "Glob":
        return inp.get("pattern") or ""
    if name in ("WebFetch", "WebSearch"):
        return one_line(inp.get("url") or inp.get("query"))
    if name and name.startswith("Task"):
        return one_line(inp.get("subject") or inp.get("description"))
    if name == "exec_command":  # codex
        return one_line(inp.get("cmd"))
    for v in inp.values():
        if isinstance(v, (str, int, float)):
            return one_line(v)
    return one_line(input_json)


def merge_tool_rows(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """tool_result 按 call_id 并入配对的 tool_use;孤立结果保留为独立行。"""
    outputs: Dict[str, Dict[str, Any]] = {}
    for m in messages:
        if m.get("kind") == "tool_result" and m.get("call_id"):
            slot = outputs.setdefault(m["call_id"], {"output": [], "is_error": False})
            if m.get("content"):
                slot["output"].append(m["content"])
            if m.get("is_error"):
                slot["is_error"] = True
    use_ids = {m.get("call_id") for m in messages
               if m.get("kind") == "tool_use" and m.get("call_id")}
    items = []
    for m in messages:
        k = m.get("kind", "text")
        if k == "tool_use":
            out = outputs.get(m.get("call_id"))
            items.append({**m,
                          "output": "\n".join(out["output"]) if out and out["output"] else None,
                          "is_error": out["is_error"] if out else None})
        elif k == "tool_result":
            if not m.get("call_id") or m["call_id"] not in use_ids:
                items.append({**m, "name": "tool_result"})
        else:
            items.append(dict(m))
    return items
