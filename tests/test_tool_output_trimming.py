"""Tests for the rule-based tool-output trimming (task T-B)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lh_harness.agent_logs import parse_trajectory
from lh_harness.config import (
    ProjectConfigError,
    create_project_config,
    load_run_defaults,
)
from lh_harness.tool_output_trimming import (
    CHARS_PER_TOKEN,
    TrimmingConfig,
    add_tokens_per_role,
    approx_tokens,
    load_trimming_config,
    trim_tool_results,
)

FIXTURE = Path(__file__).parent / "fixtures" / "claude_long_trajectory.jsonl"


def _tool_result(name: str, text: str) -> dict[str, object]:
    return {"kind": "tool_result", "name": name, "tool_use_id": name, "text": text}


def _text(text: str) -> dict[str, object]:
    return {"kind": "text", "text": text}


def _trajectory(*pairs: tuple[dict[str, object], dict[str, object]]) -> list[dict[str, object]]:
    """Build ``[text, tool_result, text, tool_result, ...]`` from (text, result) pairs."""
    out: list[dict[str, object]] = []
    for text_step, result_step in pairs:
        out.append(text_step)
        out.append(result_step)
    return out


# ---------------------------------------------------------------------------
# Boundary tests
# ---------------------------------------------------------------------------


def test_default_config_keeps_last_four_tool_results_verbatim() -> None:
    """N=4 boundary: the trailing 4 tool results stay verbatim regardless of size."""
    big = "X" * 2_000
    small = "Y" * 10
    trajectory = _trajectory(
        (_text("turn 1"), _tool_result("Read", big)),
        (_text("turn 2"), _tool_result("Bash", big)),
        (_text("turn 3"), _tool_result("Read", big)),
        (_text("turn 4"), _tool_result("Bash", big)),
        (_text("turn 5"), _tool_result("Read", big)),
    )

    trimmed, report = trim_tool_results(trajectory, TrimmingConfig())

    assert [step["kind"] for step in trimmed if step["kind"] == "tool_result"] == [
        "tool_result"
    ] * 5
    # First one cleared; remaining four kept verbatim.
    cleared = trimmed[1]
    kept = [trimmed[3], trimmed[5], trimmed[7], trimmed[9]]
    assert "[tool output cleared:" in cleared["text"]
    for step in kept:
        assert step["text"] == big
    assert report.kept_verbatim == 4
    assert report.cleared_stub + report.cleared_truncated == 1


def test_max_turns_old_boundary_uses_stub_only_when_within_window() -> None:
    """M=3 boundary: a result older than M turns AND over 500 chars is truncated;
    a result outside the keep window but with turns_old <= M OR <= 500 chars
    is stub-only.

    With 6 turns and the first tool result emitted on turn 1, the first
    result has turns_old = 5 > M = 3 and 600 chars, so it takes the
    truncation path (first 500 + stub). The second result is also outside
    the keep window but has only 200 chars, so it takes the stub-only path
    (no 500-char head).
    """
    long_text = "Z" * 600
    short_text = "z" * 200

    trajectory = _trajectory(
        (_text("turn 1"), _tool_result("Read", long_text)),  # truncation path
        (_text("turn 2"), _tool_result("Bash", short_text)),  # stub-only path
        (_text("turn 3"), _tool_result("Read", short_text)),
        (_text("turn 4"), _tool_result("Bash", short_text)),
        (_text("turn 5"), _tool_result("Read", short_text)),
        (_text("turn 6"), _tool_result("Bash", short_text)),
    )
    trimmed, report = trim_tool_results(trajectory, TrimmingConfig())

    results = [s for s in trimmed if s["kind"] == "tool_result"]
    # First result: truncation path (first 500 chars + stub).
    assert results[0]["text"].startswith("Z" * 500)
    assert "[tool output cleared:" in results[0]["text"]
    assert results[0]["text"].endswith("]")
    # Second result: stub-only path (no 500-char head, no original chars).
    assert results[1]["text"].startswith("[tool output cleared:")
    assert "Z" not in results[1]["text"]
    assert report.cleared_truncated == 1
    assert report.cleared_stub == 1


def test_truncation_chars_boundary_at_exactly_500_chars() -> None:
    """At exactly 500 chars the result is NOT truncated (rule is "longer than")."""
    exact = "Q" * 500
    one_more = "Q" * 501
    # Six tool results so the oldest is outside the keep window AND old enough
    # in turns to clear.
    trajectory = _trajectory(
        (_text("turn 1"), _tool_result("Read", exact)),
        (_text("turn 2"), _tool_result("Bash", one_more)),
        (_text("turn 3"), _tool_result("Read", one_more)),
        (_text("turn 4"), _tool_result("Bash", one_more)),
        (_text("turn 5"), _tool_result("Read", one_more)),
        (_text("turn 6"), _tool_result("Bash", one_more)),
    )
    trimmed, report = trim_tool_results(trajectory, TrimmingConfig())
    # Find the first (exact=500) result and the second (one_more=501) result.
    results = [s for s in trimmed if s["kind"] == "tool_result"]
    # First is cleared (outside keep window), 500 chars exactly -> stub-only.
    assert report.cleared_stub == 1
    assert report.cleared_truncated == 1
    # 500-char one is stub (length 5 chars stub, since exact=500 is not
    # "longer than" 500 so the truncation rule does not apply).
    assert not results[0]["text"].startswith("Q")
    # 501-char one IS truncated to first 500 + stub.
    assert results[0]["text"].startswith("[tool output cleared:")
    assert results[1]["text"].startswith("Q" * 500)


def test_off_switch_returns_trajectory_unchanged_and_no_metrics() -> None:
    """Disabled trimmer is a no-op: every tool result keeps its text verbatim."""
    trajectory = _trajectory(
        (_text("turn 1"), _tool_result("Read", "A" * 5_000)),
        (_text("turn 2"), _tool_result("Bash", "B" * 5_000)),
        (_text("turn 3"), _tool_result("Read", "C" * 5_000)),
        (_text("turn 4"), _tool_result("Bash", "D" * 5_000)),
        (_text("turn 5"), _tool_result("Read", "E" * 5_000)),
    )
    cfg = TrimmingConfig(enabled=False)
    trimmed, report = trim_tool_results(trajectory, cfg)

    for original, result in zip(trajectory, trimmed):
        if original["kind"] != "tool_result":
            assert original == result
        else:
            assert result["text"] == original["text"]

    assert report.enabled is False
    # Even when disabled the report carries the totals so a downstream metric
    # can still show "we didn't change anything".
    assert report.total_tool_results == 5
    assert report.kept_verbatim == 0
    assert report.bytes_before == 25_000
    assert report.bytes_after == 25_000


def test_keep_last_n_zero_keeps_nothing_verbatim() -> None:
    """Boundary N=0: every tool result is cleared."""
    trajectory = _trajectory(
        (_text("turn 1"), _tool_result("Read", "long " * 200)),
        (_text("turn 2"), _tool_result("Bash", "long " * 200)),
        (_text("turn 3"), _tool_result("Read", "long " * 200)),
    )
    cfg = TrimmingConfig(keep_last_n=0, max_turns_old=3, truncation_chars=500)
    _, report = trim_tool_results(trajectory, cfg)
    assert report.kept_verbatim == 0
    assert report.cleared_stub + report.cleared_truncated == 3


def test_keep_last_n_one_only_keeps_most_recent() -> None:
    """Boundary N=1: only the most recent tool result is verbatim."""
    trajectory = _trajectory(
        (_text("turn 1"), _tool_result("Read", "long " * 200)),
        (_text("turn 2"), _tool_result("Bash", "long " * 200)),
        (_text("turn 3"), _tool_result("Read", "long " * 200)),
    )
    cfg = TrimmingConfig(keep_last_n=1, max_turns_old=3, truncation_chars=500)
    trimmed, report = trim_tool_results(trajectory, cfg)
    assert report.kept_verbatim == 1
    assert report.cleared_stub + report.cleared_truncated == 2


def test_max_turns_old_zero_disables_truncation_path() -> None:
    """Boundary M=0: turns_old > 0 is always true for any cleared result, so
    long cleared results always take the truncation path."""
    trajectory = _trajectory(
        (_text("turn 1"), _tool_result("Read", "X" * 1_000)),
        (_text("turn 2"), _tool_result("Bash", "X" * 1_000)),
        (_text("turn 3"), _tool_result("Read", "X" * 1_000)),
        (_text("turn 4"), _tool_result("Bash", "X" * 1_000)),
        (_text("turn 5"), _tool_result("Read", "X" * 1_000)),
        (_text("turn 6"), _tool_result("Bash", "X" * 1_000)),
    )
    cfg = TrimmingConfig(keep_last_n=4, max_turns_old=0, truncation_chars=500)
    trimmed, report = trim_tool_results(trajectory, cfg)
    # Two results are outside the keep window (positions 1 and 3 by 0-based
    # tool_result order); both are >500 chars and turns_old > 0, so both take
    # the truncation path. The remaining four are kept verbatim.
    assert report.cleared_truncated == 2
    assert report.cleared_stub == 0
    assert report.kept_verbatim == 4
    truncated = [
        s for s in trimmed if s["kind"] == "tool_result" and "[tool output cleared:" in s["text"]
    ]
    assert len(truncated) == 2
    for step in truncated:
        assert step["text"].startswith("X" * 500)
        assert "[tool output cleared:" in step["text"]


def test_stub_carries_tool_name_and_turn_index() -> None:
    """The stub string embeds the tool name, original byte count, and the
    turn the result was emitted on."""
    trajectory = _trajectory(
        (_text("turn 1"), _tool_result("MyTool", "a" * 100)),
        (_text("turn 2"), _tool_result("Bash", "b" * 100)),
        (_text("turn 3"), _tool_result("Read", "c" * 100)),
        (_text("turn 4"), _tool_result("Bash", "d" * 100)),
        (_text("turn 5"), _tool_result("Read", "e" * 100)),
    )
    cleaned, _ = trim_tool_results(trajectory, TrimmingConfig())
    first = cleaned[1]
    assert first["text"] == "[tool output cleared: MyTool 100 bytes, turn 1]"


def test_trim_does_not_mutate_input() -> None:
    trajectory = _trajectory(
        (_text("turn 1"), _tool_result("Read", "long " * 200)),
        (_text("turn 2"), _tool_result("Bash", "long " * 200)),
        (_text("turn 3"), _tool_result("Read", "long " * 200)),
    )
    snapshot = [dict(step) for step in trajectory]
    trim_tool_results(trajectory, TrimmingConfig())
    assert trajectory == snapshot


def test_approx_tokens_and_per_role_accumulator() -> None:
    assert approx_tokens("") == 0
    assert approx_tokens("abcd") == 1
    assert approx_tokens("abcde") == 2
    assert CHARS_PER_TOKEN == 4

    out: dict[str, dict[str, int]] = {}
    add_tokens_per_role(
        TrimmingConfig(), role="executor", tokens_before=1000, tokens_after=600, per_role=out
    )
    add_tokens_per_role(
        TrimmingConfig(), role="executor", tokens_before=500, tokens_after=200, per_role=out
    )
    add_tokens_per_role(
        TrimmingConfig(), role="auditor", tokens_before=200, tokens_after=200, per_role=out
    )
    assert out == {
        "executor": {"before": 1500, "after": 800},
        "auditor": {"before": 200, "after": 200},
    }


def test_load_trimming_config_uses_defaults_and_overrides() -> None:
    assert load_trimming_config(None) == TrimmingConfig()
    assert load_trimming_config({}) == TrimmingConfig()
    cfg = load_trimming_config(
        {
            "tool_output_trimming_enabled": False,
            "tool_output_keep_last_n": 7,
            "tool_output_max_turns_old": 1,
            "tool_output_truncation_chars": 1000,
        }
    )
    assert cfg == TrimmingConfig(
        enabled=False, keep_last_n=7, max_turns_old=1, truncation_chars=1000
    )


def test_load_trimming_config_rejects_non_table() -> None:
    with pytest.raises(ValueError, match=r"\[run\.context\]"):
        load_trimming_config(["not", "a", "dict"])  # type: ignore[arg-type]


def test_trimming_config_rejects_bad_values() -> None:
    with pytest.raises(ValueError):
        TrimmingConfig(keep_last_n=-1)
    with pytest.raises(ValueError):
        TrimmingConfig(max_turns_old=-1)
    with pytest.raises(ValueError):
        TrimmingConfig(truncation_chars=0)  # _positive_int requires >= 1


# ---------------------------------------------------------------------------
# Config-wiring tests
# ---------------------------------------------------------------------------


def test_config_template_carries_context_keys_with_documented_defaults(tmp_path: Path) -> None:
    config_path = create_project_config(tmp_path / "config.toml")
    defaults = load_run_defaults(config_path)
    assert defaults["tool_output_trimming_enabled"] is True
    assert defaults["tool_output_keep_last_n"] == 4
    assert defaults["tool_output_max_turns_old"] == 3
    assert defaults["tool_output_truncation_chars"] == 500


def test_context_overrides_are_parsed_from_run_table(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[run.context]\n"
        "tool_output_trimming_enabled = false\n"
        "tool_output_keep_last_n = 8\n"
        "tool_output_max_turns_old = 2\n"
        "tool_output_truncation_chars = 1024\n",
        encoding="utf-8",
    )
    defaults = load_run_defaults(config_path)
    assert defaults["tool_output_trimming_enabled"] is False
    assert defaults["tool_output_keep_last_n"] == 8
    assert defaults["tool_output_max_turns_old"] == 2
    assert defaults["tool_output_truncation_chars"] == 1024


def test_unknown_context_key_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[run.context]\ntool_output_unknown = 1\n", encoding="utf-8"
    )
    with pytest.raises(ProjectConfigError, match=r"\[run\.context\]"):
        load_run_defaults(config_path)


def test_bad_context_types_are_rejected(tmp_path: Path) -> None:
    bad_truncation = tmp_path / "trunc.toml"
    bad_truncation.write_text(
        "[run.context]\ntool_output_truncation_chars = 0\n", encoding="utf-8"
    )
    with pytest.raises(ProjectConfigError, match=r"tool_output_truncation_chars"):
        load_run_defaults(bad_truncation)

    bad_bool = tmp_path / "bool.toml"
    bad_bool.write_text(
        "[run.context]\ntool_output_trimming_enabled = \"yes\"\n",
        encoding="utf-8",
    )
    with pytest.raises(ProjectConfigError, match=r"tool_output_trimming_enabled"):
        load_run_defaults(bad_bool)


# ---------------------------------------------------------------------------
# Integration test on a fixture transcript
# ---------------------------------------------------------------------------


def _result_names_by_id(steps: list[dict[str, object]]) -> dict[str, str]:
    """Pair each tool_result with the name from the preceding tool_use step.

    The trajectory normaliser leaves the tool name on the ``tool_use`` step
    only; this helper walks the steps in order so the test can assert which
    result came from which tool without re-parsing the raw log.
    """
    pending: dict[str, str] = {}
    out: dict[str, str] = {}
    for step in steps:
        if step["kind"] == "tool_use":
            pending[str(step["id"])] = str(step["name"])
        elif step["kind"] == "tool_result":
            tool_use_id = str(step["tool_use_id"])
            out[tool_use_id] = pending.pop(tool_use_id, "tool")
    return out


def test_fixture_transcript_trim_matches_documented_rules() -> None:
    raw = FIXTURE.read_text(encoding="utf-8")
    steps = parse_trajectory(raw)
    tool_results = [s for s in steps if s["kind"] == "tool_result"]
    assert len(tool_results) == 5, "fixture must contain 5 tool results"

    cfg = TrimmingConfig()  # defaults: N=4, M=3, 500 chars, enabled
    trimmed, report = trim_tool_results(steps, cfg)

    by_name = _result_names_by_id(trimmed)
    trimmed_results = [s for s in trimmed if s["kind"] == "tool_result"]

    # The last N=4 tool results stay verbatim regardless of size.
    assert len(trimmed_results) == 5
    for step in trimmed_results[1:]:
        tool_use_id = str(step["tool_use_id"])
        original = next(s for s in tool_results if str(s["tool_use_id"]) == tool_use_id)
        assert step["text"] == original["text"], (
            f"tool_use_id={tool_use_id} should be verbatim"
        )

    # The first tool result is the only one cleared. It is 8000 chars long
    # and 4 turns older than the last text turn (turns_old > M=3), so it
    # must take the truncation path: first 500 chars + stub.
    first_result = trimmed_results[0]
    tool_use_id = str(first_result["tool_use_id"])
    original = next(s for s in tool_results if str(s["tool_use_id"]) == tool_use_id)
    assert len(original["text"]) == 8000
    assert first_result["text"].startswith(original["text"][:500])
    assert first_result["text"].endswith("]")
    assert "[tool output cleared:" in first_result["text"]
    # Stub includes the original byte count and the turn the result was on.
    assert "8000 bytes" in first_result["text"]
    assert "turn 1]" in first_result["text"]

    # Report: 4 kept verbatim, 1 truncated, 0 stub-only, bytes accounted for.
    assert report.enabled is True
    assert report.total_tool_results == 5
    assert report.kept_verbatim == 4
    assert report.cleared_truncated == 1
    assert report.cleared_stub == 0
    assert report.bytes_before == sum(len(s["text"]) for s in tool_results)
    assert report.bytes_after == sum(len(s["text"]) for s in trimmed_results)
    # Stub includes the tool name read from the preceding tool_use step.
    assert by_name[str(first_result["tool_use_id"])] == "Read"
    # The per-tool breakdown counts every result, kept or cleared. From the
    # fixture: t1 Read (cleared), t2 Bash, t3 Read, t4 Edit, t5 Read.
    assert report.tool_breakdown == {"Read": 3, "Bash": 1, "Edit": 1}


def test_fixture_transcript_off_switch_preserves_all_tool_results() -> None:
    raw = FIXTURE.read_text(encoding="utf-8")
    steps = parse_trajectory(raw)
    original_results = {str(s["tool_use_id"]): s["text"] for s in steps if s["kind"] == "tool_result"}

    cfg = TrimmingConfig(enabled=False)
    trimmed, report = trim_tool_results(steps, cfg)

    for step in trimmed:
        if step["kind"] != "tool_result":
            continue
        assert step["text"] == original_results[str(step["tool_use_id"])]
    assert report.enabled is False
    assert report.kept_verbatim == 0
    assert report.bytes_before == report.bytes_after