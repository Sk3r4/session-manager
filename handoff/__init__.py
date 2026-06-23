"""Handoff module: cross-session context preservation.

Flow:
  Stop hook   → POST /api/handoff/archive  → handoff_entries table
  AI on resume → GET /api/handoff/context   → returns recent summaries
  PreCompact  → POST /api/handoff/archive  → full transcript archival
"""

from .controller import HandoffController

__all__ = ["HandoffController"]
