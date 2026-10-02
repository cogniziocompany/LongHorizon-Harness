"""Compact per-episode statistics derived from a Claude Code stream-json log.

The fleet event pipeline (``fleet/reporter.py`` -> ``/harness/events``) carries
only summary fields, never trajectories.  These counters are the summary of one
episode that sizing and stall analysis need: how many turns it took, how much
it edited, and how long it sat waiting on the model.  They ride inside
``episode_status`` on every role done/failed event, so the fleet plane can
aggregate them per trio without anyone reading run directories.

A "gap" is the wait between a tool result going back to the model and the next
assistant message arriving.  A gap at or above ``STALL_GAP_SECONDS`` is a
stall: on the local lanes (one request at a time per lane) that is a request
queued behind another one, or a single response that ran away.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .agent_logs import _json_records

STALL_GAP_SECONDS = 120.0
_EDIT_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})
_PATH_KEYS = ("file_path", "notebook_path")


def summarize_episode(log: str) -> dict[str, Any] | None:
    """Return episode counters, or ``None`` when ``log`` has no assistant turns.

    Only the Claude Code stream-json format carries the per-record timestamps
    and message ids this needs; other formats yield ``None`` rather than a
    misleading row of zeros.
    """

    message_ids: set[str] = set()
    files: set[str] = set()
    model = ""
    tool_calls = edits = tool_errors = tool_result_chars = 0
    thinking_tokens = turn_thinking = max_turn_thinking = 0
    api_retries = compactions = stalls = 0
    max_gap = stall_seconds = 0.0
    last_tool_result_at: float | None = None

    for record in _json_records(log):
        record_type = record.get("type")
        if record_type == "system":
            subtype = record.get("subtype")
            if subtype == "thinking_tokens":
                delta = _as_int(record.get("estimated_tokens_delta"))
                thinking_tokens += delta
                turn_thinking += delta
            elif subtype == "api_retry":
                api_retries += 1
            elif subtype == "compact_boundary":
                compactions += 1
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        at = _timestamp(record.get("timestamp"))
        blocks = message.get("content")
        blocks = blocks if isinstance(blocks, list) else []
        if record_type == "assistant":
            model = model or _real_model(message.get("model"))
            message_id = str(message.get("id") or "")
            if message_id not in message_ids:
                # Claude Code prints one record per content block; the message
                # id is what marks a new model turn.
                message_ids.add(message_id)
                max_turn_thinking = max(max_turn_thinking, turn_thinking)
                turn_thinking = 0
                if at is not None and last_tool_result_at is not None:
                    gap = max(0.0, at - last_tool_result_at)
                    max_gap = max(max_gap, gap)
                    if gap >= STALL_GAP_SECONDS:
                        stalls += 1
                        stall_seconds += gap
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_calls += 1
                if block.get("name") in _EDIT_TOOLS:
                    edits += 1
                    tool_input = block.get("input")
                    if isinstance(tool_input, dict):
                        for key in _PATH_KEYS:
                            if isinstance(tool_input.get(key), str):
                                files.add(tool_input[key])
        elif record_type == "user":
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_result_chars += _content_chars(block.get("content"))
                if block.get("is_error"):
                    tool_errors += 1
            if at is not None:
                last_tool_result_at = at

    if not message_ids:
        return None
    return {
        "model": model,
        "turns": len(message_ids),
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "edits": edits,
        "files_touched": len(files),
        "tool_result_chars": tool_result_chars,
        "thinking_tokens": thinking_tokens,
        "max_turn_thinking_tokens": max(max_turn_thinking, turn_thinking),
        "api_retries": api_retries,
        "compactions": compactions,
        "max_gap_seconds": round(max_gap, 1),
        "stalls": stalls,
        "stall_seconds": round(stall_seconds, 1),
    }


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _real_model(value: Any) -> str:
    # Claude Code labels its own error placeholders "<synthetic>".
    return value if isinstance(value, str) and not value.startswith("<") else ""


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _content_chars(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return 0
