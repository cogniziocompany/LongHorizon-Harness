from __future__ import annotations

import json

from lh_harness.episode_stats import summarize_episode
from lh_harness.fleet.reporter import _record_to_envelope
from lh_harness.manager import _episode_status
from lh_harness.types import EpisodeResult


def _jsonl(*records: dict[str, object]) -> str:
    return "\n".join(json.dumps(record) for record in records) + "\n"


def _assistant(message_id: str, at: str, *blocks: dict[str, object]) -> dict[str, object]:
    return {
        "type": "assistant",
        "timestamp": at,
        "message": {"id": message_id, "model": "ornith-1.5:9b-256k", "content": list(blocks)},
    }


def _tool_use(name: str, **tool_input: object) -> dict[str, object]:
    return {"type": "tool_use", "id": "t", "name": name, "input": tool_input}


def _tool_result(at: str, content: str, *, is_error: bool = False) -> dict[str, object]:
    return {
        "type": "user",
        "timestamp": at,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "content": content, "is_error": is_error}],
        },
    }


def _thinking(delta: int) -> dict[str, object]:
    return {"type": "system", "subtype": "thinking_tokens", "estimated_tokens_delta": delta}


_LOG = _jsonl(
    {"type": "system", "subtype": "init", "model": "qwen3.8"},
    _thinking(40),
    # One model turn printed as two records: same message id, counted once.
    _assistant("m1", "2026-10-01T03:45:40.000Z", {"type": "text", "text": "reading"}),
    _assistant("m1", "2026-10-01T03:45:40.010Z", _tool_use("Read", file_path="/w/a.py")),
    _tool_result("2026-10-01T03:45:41.000Z", "x" * 100),
    _thinking(10),
    _assistant("m2", "2026-10-01T03:45:46.000Z", _tool_use("Edit", file_path="/w/a.py")),
    _tool_result("2026-10-01T03:45:46.500Z", "boom", is_error=True),
    {"type": "system", "subtype": "api_retry", "attempt": 1, "error": "unknown"},
    _thinking(30),
    # 695.5 s between the tool result and the next turn: a queued request.
    _assistant("m3", "2026-10-01T03:57:22.000Z", _tool_use("Write", file_path="/w/b.py")),
    _tool_result("2026-10-01T03:57:23.000Z", "ok"),
    _assistant("m4", "2026-10-01T03:57:26.000Z", {"type": "text", "text": "done"}),
)


def test_counts_turns_edits_and_stalls_from_a_claude_stream() -> None:
    assert summarize_episode(_LOG) == {
        "model": "ornith-1.5:9b-256k",
        "turns": 4,
        "tool_calls": 3,
        "tool_errors": 1,
        "edits": 2,
        "files_touched": 2,
        "tool_result_chars": 106,
        "thinking_tokens": 80,
        "max_turn_thinking_tokens": 40,
        "api_retries": 1,
        "compactions": 0,
        "max_gap_seconds": 695.5,
        "stalls": 1,
        "stall_seconds": 695.5,
    }


def test_a_log_without_assistant_turns_has_no_stats() -> None:
    assert summarize_episode("") is None
    assert summarize_episode("plain text\nAGENT_EXIT=1\n") is None
    assert summarize_episode(_jsonl({"type": "thread.started", "thread_id": "t"})) is None


def test_stats_survive_the_fleet_event_payload_trim() -> None:
    stats = summarize_episode(_LOG)
    result = EpisodeResult(
        status="timeout",
        actions_log=_LOG,
        error="Episode timed out after 1800s.",
        duration_ms=1_800_000,
        metadata={"episode_stats": stats},
    )
    envelope = _record_to_envelope(
        {
            "event_id": "20261001T032509Z_ee39ebc1:000009",
            "event": "agent_runtime_failed",
            "round": 2,
            "phase": "executor",
            "status": "failed",
            "episode_status": _episode_status(result),
        }
    )
    assert envelope.to_dict()["payload"]["episode_status"]["stats"] == stats
