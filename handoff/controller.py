"""Handoff controller: archive and query handoff summaries."""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

from storage.index import IndexDB


class HandoffController:
    def __init__(self, db: IndexDB):
        self.db = db

    def archive_summary(
        self,
        project_dir: str,
        summary_text: str,
        session_id: Optional[str] = None,
        source: str = "stop_hook",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """Archive a handoff summary from AI response or PreCompact hook.

        The summary_text is the raw markdown/text from the AI's flagged handoff section.
        We attempt to parse structured fields from it, but always store the full text.
        """
        meta = metadata or {}
        key_findings = meta.get("key_findings")
        current_phase = meta.get("current_phase")
        next_action = meta.get("next_action")
        volatility_tags = meta.get("volatility_tags")

        # If no structured metadata provided, attempt lightweight extraction
        if not current_phase:
            current_phase = self._extract_field(summary_text, "phase")
        if not next_action:
            next_action = self._extract_field(summary_text, "next_action")
        if not key_findings:
            key_findings = self._extract_field(summary_text, "findings")

        row_id = self.db.insert_handoff(
            project_dir=project_dir,
            session_id=session_id,
            source=source,
            summary_text=summary_text,
            key_findings=key_findings,
            current_phase=current_phase,
            next_action=next_action,
            volatility_tags=volatility_tags,
        )
        return {"id": row_id, "project_dir": project_dir, "source": source}

    def get_context(self, project_dir: str, limit: int = 5) -> dict:
        """Return recent handoff summaries for a project, formatted for AI consumption."""
        entries = self.db.get_handoff_context(project_dir, limit=limit)
        if not entries:
            return {
                "project_dir": project_dir,
                "entries": [],
                "summary": "No previous handoff summaries found for this project.",
            }

        # Build a compact context block suitable for AI reading
        parts = []
        for i, e in enumerate(entries):
            created = datetime.fromtimestamp(e["created_at"]).strftime("%Y-%m-%d %H:%M") if e["created_at"] else "unknown"
            parts.append(f"## Handoff #{len(entries) - i} ({created})\n"
                         f"**Phase**: {e['current_phase'] or 'unknown'}\n"
                         f"**Source**: {e['source']}\n\n"
                         f"{e['summary_text']}")

        return {
            "project_dir": project_dir,
            "entry_count": len(entries),
            "latest_phase": entries[0].get("current_phase") if entries else None,
            "latest_next_action": entries[0].get("next_action") if entries else None,
            "markdown": "\n\n---\n\n".join(parts),
            "entries": [
                {
                    "id": e["id"],
                    "created_at": e["created_at"],
                    "source": e["source"],
                    "current_phase": e["current_phase"],
                    "next_action": e["next_action"],
                    "summary_text": e["summary_text"][:500],
                }
                for e in entries
            ],
        }

    def get_latest(self, project_dir: str) -> Optional[dict]:
        """Return only the most recent handoff summary."""
        entry = self.db.get_latest_handoff(project_dir)
        if not entry:
            return None
        created = datetime.fromtimestamp(entry["created_at"]).strftime("%Y-%m-%d %H:%M") if entry["created_at"] else "unknown"
        return {
            "created_at": created,
            "source": entry["source"],
            "current_phase": entry["current_phase"],
            "next_action": entry["next_action"],
            "summary_text": entry["summary_text"],
            "key_findings": entry["key_findings"],
        }

    def archive_full_transcript(self, project_dir: str, transcript_path: str, session_id: Optional[str] = None) -> dict:
        """Archive a full transcript file before compaction.

        Reads the transcript JSONL, generates a structured summary using lightweight
        extraction (no LLM call — we tag and store, the AI reads it later).
        """
        path = Path(transcript_path)
        if not path.exists():
            return {"error": f"Transcript file not found: {transcript_path}"}

        # Read the transcript and extract the last N assistant messages as a rough summary
        try:
            messages = []
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

            # Extract last few assistant responses as context snapshot
            assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
            recent = assistant_msgs[-3:] if len(assistant_msgs) >= 3 else assistant_msgs

            summary_parts = []
            for m in recent:
                content = m.get("content", "")
                if isinstance(content, str) and content.strip():
                    # Take first 300 chars of each response as summary
                    summary_parts.append(content[:300])

            summary_text = "\n\n---\n\n".join(summary_parts) if summary_parts else "(No assistant messages found)"
            summary_text = f"[Pre-compaction archive: {len(messages)} messages, {len(assistant_msgs)} assistant responses]\n\n{summary_text}"

        except Exception as e:
            summary_text = f"[Error reading transcript: {e}]"

        return self.archive_summary(
            project_dir=project_dir,
            session_id=session_id,
            source="pre_compact",
            summary_text=summary_text,
        )

    @staticmethod
    def _extract_field(text: str, field: str) -> Optional[str]:
        """Lightweight extraction of tagged fields from handoff summary text."""
        markers = {
            "phase": ["当前阶段:", "Phase:", "current_phase:", "阶段:"],
            "next_action": ["下一步:", "Next:", "next_action:", "待办:"],
            "findings": ["关键发现:", "Findings:", "key_findings:", "已确认:"],
            "volatility": ["易变项:", "Volatile:", "volatility:", "需重新验证:"],
        }
        for marker in markers.get(field, []):
            if marker in text:
                idx = text.find(marker) + len(marker)
                line_end = text.find("\n", idx)
                if line_end == -1:
                    line_end = len(text)
                value = text[idx:line_end].strip()
                if value and len(value) < 200:
                    return value
        return None
