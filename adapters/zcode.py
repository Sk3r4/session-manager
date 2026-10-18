import json
import sqlite3
from pathlib import Path
from typing import List, Optional, Dict, Any

from adapters.base import (
    ProviderAdapter,
    SessionMeta,
    Message,
    SYSTEM_MARKER_PREFIXES,
    ANSI_ESCAPE_RE,
    IMAGE_PLACEHOLDER,
    flatten_messages,
)


def _ms(value) -> Optional[float]:
    """zcode 时间戳统一为毫秒级;容错空值与秒级。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f / 1000.0 if f > 1e12 else f


class ZCodeAdapter(ProviderAdapter):
    """ZCode CLI 适配器:只读 ~/.zcode/cli/db/db.sqlite。

    数据模型(规范化四层):
        session(id/title/directory/time_*) → message(data.role, sequence)
        → part(data.type, sequence)。session_entry 全为 runtime/* 事件,不进转录。

    - 子 agent 会话(parent_id 非空 / task_type='subagent_child')不入扫描;
    - tool part 自带调用与结果,拆为 tool_use + tool_result 两条,以 callID 配对;
    - step-start / step-finish 为流式控制块,跳过;
    - 标题:custom(人工命名,同步进 SM)与 first_input(由首条输入派生一次,稳定)保留;
      generated(AI 生成,随会话漂移)已退役(2026-10-10)→ 首条用户消息截断。
      'custom' → title_author='human'(唯一可覆盖 manual 的标题来源);
    - DB_BACKED:删会话 = 删库里的行(源文件恒在),scan 侧幽灵清理需信任扫描结果全集。
    """

    DB_BACKED = True

    def _db_path(self) -> Optional[Path]:
        for sp in self.config.get("scan_paths", []):
            cand = Path(sp) / "db" / "db.sqlite"
            if cand.exists():
                return cand
        return None

    def detect(self) -> bool:
        return self._db_path() is not None

    def _find_files(self) -> List[Path]:
        """poll 变更探测的 stat 签名源:主库 + WAL/SHM(运行中写入只落 WAL,必须一起盯)。"""
        db = self._db_path()
        if not db:
            return []
        return [p for p in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")) if p.exists()]

    def _connect(self) -> Optional[sqlite3.Connection]:
        db = self._db_path()
        if not db:
            return None
        # as_uri() 做百分号编码(中文/空格路径安全);mode=ro 只读,不干扰运行中的 ZCode
        return sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)

    # ---- 扫描 ----

    def scan_sessions(self) -> List[SessionMeta]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            # 查询错误向上抛(scan_provider 捕获转 500,避免空结果误触发幽灵清理全删)
            rows = conn.execute("""
                SELECT id, title, title_source, directory, time_created, time_updated
                FROM session
                WHERE parent_id IS NULL
                  AND (task_type IS NULL OR task_type != 'subagent_child')
            """).fetchall()
            db_path = str(self._db_path())
            metas = []
            for sid, title, title_source, directory, tc, tu in rows:
                # 标题:custom(人工命名)与 first_input(首条输入派生一次,稳定)保留;
                # generated(AI 生成,随会话漂移)已退役(2026-10-10 用户决定)
                # → 退回首条用户消息截断,与 claude/codex 的兜底一致
                if title_source == "generated":
                    title = None
                if title is None:
                    fb = self._first_user_text(conn, sid)
                    if fb:
                        title = fb[:80] + ("..." if len(fb) > 80 else "")
                metas.append(SessionMeta(
                    session_id=sid,
                    provider_id=self.provider_id,
                    title=title or None,
                    # 'custom' = 用户在 zcode 里命的名 → 人工命名;其余为自动生成
                    title_author="human" if title_source == "custom" else None,
                    summary=self._first_user_text(conn, sid),
                    project_dir=directory or None,
                    created_at=_ms(tc),
                    last_active_at=_ms(tu),
                    source_path=db_path,
                ))
            return metas
        finally:
            conn.close()

    def _first_user_text(self, conn: sqlite3.Connection, session_id: str) -> Optional[str]:
        """首条用户 text part → summary(与其他适配器的首条用户消息约定一致)。"""
        try:
            row = conn.execute("""
                SELECT json_extract(p.data, '$.text')
                FROM part p JOIN message m ON p.message_id = m.id
                WHERE p.session_id = ? AND json_extract(m.data, '$.role') = 'user'
                  AND json_extract(p.data, '$.type') = 'text'
                ORDER BY m.sequence, p.sequence LIMIT 1
            """, (session_id,)).fetchone()
        except sqlite3.Error:
            return None
        if not row or not row[0]:
            return None
        text = ANSI_ESCAPE_RE.sub("", str(row[0])).strip()
        if not text or text.startswith(SYSTEM_MARKER_PREFIXES):
            return None
        return text[:200] + "..." if len(text) > 200 else text

    # ---- 转录 ----

    def load_transcript(self, session_id: str) -> List[Message]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = conn.execute("""
                SELECT m.sequence, m.time_created, json_extract(m.data, '$.role') AS role,
                       p.sequence, p.time_created, p.data
                FROM message m LEFT JOIN part p ON p.message_id = m.id
                WHERE m.session_id = ?
                ORDER BY m.sequence, p.sequence
            """, (session_id,)).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()

        messages: List[Message] = []
        for mseq, mtime, role, pseq, ptime, pdata in rows:
            if role is None:
                continue  # 无法归属角色的消息行(含其内容块)整行跳过
            role = str(role)
            line_id = int(mseq) if mseq is not None else -1
            ts = _ms(ptime) or _ms(mtime)
            if pdata is None:
                # 无内容块的消息:保留一条空消息(旧语义,与 kimi/codex 一致)
                messages.append(Message(role=role, content="", ts=ts, raw=None,
                                        kind="text", line_id=line_id))
                continue
            try:
                part = json.loads(pdata)
            except Exception:
                continue
            if not isinstance(part, dict):
                continue
            messages.extend(self._messages_from_part(role, part, ts, line_id))
        return messages

    def _messages_from_part(self, role: str, part: dict, ts, line_id: int) -> List[Message]:
        pt = part.get("type", "")
        if pt in ("step-start", "step-finish"):
            return []
        if pt == "text":
            text = str(part.get("text", ""))
            kind = "text"
            if role == "user":
                clean = ANSI_ESCAPE_RE.sub("", text).strip()
                if clean.startswith(SYSTEM_MARKER_PREFIXES):
                    kind = "envelope"
            return [Message(role=role, content=text, ts=ts, raw=part, kind=kind, line_id=line_id)]
        if pt == "reasoning":
            text = str(part.get("text", ""))
            if not text:
                return []
            return [Message(role=role, content=text, ts=ts, raw=part,
                            kind="thinking", line_id=line_id)]
        if pt == "tool":
            return self._messages_from_tool_part(role, part, ts, line_id)
        if pt == "file":
            name = part.get("filename") or part.get("mime") or ""
            label = f"{IMAGE_PLACEHOLDER} {name}".strip()
            return [Message(role=role, content=label, ts=ts, raw=part, kind="text", line_id=line_id)]
        if pt == "timeline":
            # 模型切换分隔条:完整视图可见,干净视图隐藏
            to_model = (part.get("toModel") or {}).get("modelID") or ""
            content = f"—— 模型切换: {to_model} ——" if to_model else "—— 模型切换 ——"
            return [Message(role=role, content=content, ts=ts, raw=part,
                            kind="envelope", line_id=line_id)]
        # 兜底:未知类型取首个非 type 的字符串值(kimi 同款 fallback)
        for k, v in part.items():
            if isinstance(v, str) and v.strip() and k != "type":
                return [Message(role=role, content=v, ts=ts, raw=part, kind="text", line_id=line_id)]
        return []

    def _messages_from_tool_part(self, role: str, part: dict, ts, line_id: int) -> List[Message]:
        """tool part 自带调用+结果 → 拆为 tool_use + tool_result 两条,callID 配对。"""
        call_id = part.get("callID")
        name = str(part.get("tool") or "")
        state = part.get("state") or {}
        out: List[Message] = []
        if name:
            out.append(Message(
                role=role,
                content=json.dumps(state.get("input") or {}, ensure_ascii=False),
                ts=ts, raw=part, kind="tool_use", name=name, call_id=call_id, line_id=line_id,
            ))
        output = state.get("output")
        if output is not None:
            out.append(Message(
                role=role, content=str(output), ts=ts, raw=part, kind="tool_result",
                call_id=call_id, is_error=(state.get("status") == "error"), line_id=line_id,
            ))
        return out

    def flatten_transcript(self, messages: List[Message]) -> List[Dict[str, Any]]:
        # zcode 无旧归档格式,采用默认全类型语义(MCP full 模式:思考与工具保留)
        return flatten_messages(messages)

    def get_project_dir(self, session_id: str) -> Optional[str]:
        conn = self._connect()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT directory FROM session WHERE id = ?", (session_id,)
            ).fetchone()
            return row[0] if row else None
        except sqlite3.Error:
            return None
        finally:
            conn.close()
