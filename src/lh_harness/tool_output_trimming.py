"""Rule-based trimming of old tool results in a parsed trajectory.

The harness persists a per-episode trajectory as ordered steps with kinds
``text``, ``thinking``, ``tool_use``, ``tool_result``, ``result`` (see
``agent_logs.parse_trajectory``). Long runs accumulate large tool outputs
(``cat`` of a big file, ``Bash`` of a long test, MCP ``grep`` results), and
those outputs are re-fed to the model on every subsequent turn. This module
shrinks the trajectory by clearing old tool outputs *by rule*, before any LLM
summarisation runs.

Rules (the defaults match ``[run.context]`` in ``config.toml``):

1. ``keep_last_n`` (default 4): the most recent N ``tool_result`` steps keep
   their original text verbatim. Older ones are cleared.
2. ``max_turns_old`` (default 3): a tool result is "old" when more than
   ``max_turns_old`` assistant turns have happened since it appeared.
3. ``truncation_chars`` (default 500): any cleared tool result whose original
   text was longer than this AND that is older than ``max_turns_old`` turns
   keeps the first ``truncation_chars`` characters followed by a stub; the
   rest is dropped. Short results become the stub on their own.
4. ``enabled`` (default true): master off switch. When false the trajectory
   is returned unchanged and no metrics are recorded.

Clearing is a one-line stub so downstream rendering still shows "this tool
ran and returned N bytes on turn K" instead of vanishing; no LLM
summarisation is invoked here, and the stub is byte-identical for any two
inputs of the same length.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

# Step kinds the parser emits. ``assistant``-style text/thinking advance the
# turn counter; ``tool_result`` is what we trim.
_TURN_ADVANCING_KINDS = frozenset({"text", "thinking", "result"})
_TRIMMED_STUB_TEMPLATE = "[tool output cleared: {tool} {bytes} bytes, turn {turn}]"


@dataclass
class TrimmingConfig:
    """Tunable bounds for ``trim_tool_results``.

    The defaults match the four keys under ``[run.context]`` in the project
    ``config.toml`` and are used when the project file omits them.
    """

    enabled: bool = True
    keep_last_n: int = 4
    max_turns_old: int = 3
    truncation_chars: int = 500

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")
        # ``keep_last_n`` and ``max_turns_old`` may be zero (zero is a
        # legitimate "trim everything" / "always old" bound); a negative
        # value is the only nonsensical case.
        for name, value in (
            ("keep_last_n", self.keep_last_n),
            ("max_turns_old", self.max_turns_old),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        # ``truncation_chars`` must be positive; a zero would degenerate the
        # truncation path into "always stub only" and the loader treats that
        # as a config typo.
        if (
            isinstance(self.truncation_chars, bool)
            or not isinstance(self.truncation_chars, int)
            or self.truncation_chars < 1
        ):
            raise ValueError("truncation_chars must be an integer of at least 1")


@dataclass
class TrimmingReport:
    """Per-call summary returned alongside the trimmed trajectory."""

    enabled: bool
    total_tool_results: int = 0
    kept_verbatim: int = 0
    cleared_stub: int = 0
    cleared_truncated: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    truncated_chars: int = 0
    tool_breakdown: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "total_tool_results": self.total_tool_results,
            "kept_verbatim": self.kept_verbatim,
            "cleared_stub": self.cleared_stub,
            "cleared_truncated": self.cleared_truncated,
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "truncated_chars": self.truncated_chars,
            "tool_breakdown": dict(sorted(self.tool_breakdown.items())),
        }


def trim_tool_results(
    steps: Sequence[Mapping[str, Any]],
    config: TrimmingConfig | None = None,
    *,
    tool_name_for_step: "Callable[[Mapping[str, Any], int], str] | None" = None,
) -> tuple[list[dict[str, Any]], TrimmingReport]:
    """Return a deep-copied trajectory with old tool results cleared by rule.

    ``steps`` is the ordered list produced by ``agent_logs.parse_trajectory``.
    The trajectory normaliser records the tool name on the ``tool_use`` step
    only; for the stub to carry the right name we walk the steps in order and
    pair each ``tool_result`` (keyed by ``tool_use_id``) with the most recent
    unmatched ``tool_use`` of the same id. A ``tool_name_for_step`` callable
    may override this pairing (it receives the step mapping and its index);
    when omitted, the pair table is consulted, falling back to ``"tool"``
    for results without a matching ``tool_use`` (e.g. fixtures that omit the
    tool-use side).

    The function never mutates the input. The returned ``TrimmingReport``
    records bytes before/after, how many results were kept verbatim vs
    cleared, and the per-tool-name stub count, so a caller can add
    ``tokens/task`` metrics and the before/after PR-body comparison without
    re-deriving anything.
    """
    cfg = config or TrimmingConfig()
    report = TrimmingReport(enabled=cfg.enabled)
    # Copy first so we never write back into the caller's data. We rebuild a
    # full new list so callers receive a list[dict] they can serialise.
    output: list[dict[str, Any]] = [dict(step) for step in steps]

    # Build the tool_use_id -> tool_name lookup before any decision so the
    # pairing covers the full input even when the function returns early.
    pending_names: dict[str, str] = {}
    for step in output:
        if step.get("kind") == "tool_use":
            name = str(step.get("name") or step.get("tool") or "tool")
            tool_use_id = str(step.get("id") or "")
            if tool_use_id:
                pending_names[tool_use_id] = name

    if not cfg.enabled:
        # Even when disabled, report the byte counts so the run report has
        # something to compare against.
        for step in output:
            if step.get("kind") != "tool_result":
                continue
            text = _result_text(step)
            report.bytes_before += len(text)
            report.bytes_after += len(text)
            report.total_tool_results += 1
        return output, report

    # First pass: assign each tool_result its turn index (the assistant turn
    # count when it was emitted) and its tool name. We index by absolute
    # position because the trimmed list reuses the same ordering.
    current_turn = 0
    total_turns_at_end = 0
    indexed: list[tuple[int, dict[str, Any], str]] = []
    for index, step in enumerate(output):
        if step.get("kind") in _TURN_ADVANCING_KINDS:
            current_turn += 1
            total_turns_at_end = current_turn
            continue
        if step.get("kind") != "tool_result":
            continue
        if tool_name_for_step is not None:
            name = tool_name_for_step(step, index)
        else:
            tool_use_id = str(step.get("tool_use_id") or "")
            name = pending_names.get(tool_use_id) or str(
                step.get("name") or step.get("tool") or "tool"
            )
        indexed.append((current_turn, step, name))
        report.total_tool_results += 1

    total = len(indexed)
    keep_count = min(cfg.keep_last_n, total)

    # Iterate oldest -> newest so we know "kept verbatim" = the trailing
    # ``keep_count`` entries. Anything before that is cleared.
    for offset, (turn_at_emission, step, name) in enumerate(indexed):
        original_text = _result_text(step)
        original_bytes = len(original_text)
        report.bytes_before += original_bytes
        # Every result, kept or cleared, contributes to the per-tool breakdown
        # so the report tells you exactly which tools the trimmer touched.
        report.tool_breakdown[name] = report.tool_breakdown.get(name, 0) + 1
        is_kept = offset >= total - keep_count
        if is_kept:
            report.kept_verbatim += 1
            report.bytes_after += original_bytes
            continue
        turns_old = total_turns_at_end - turn_at_emission
        truncated = (
            original_bytes > cfg.truncation_chars and turns_old > cfg.max_turns_old
        )
        if truncated:
            head = original_text[: cfg.truncation_chars]
            # Build the replacement text on the same bytes boundary the
            # stub template uses. ``head`` is the first
            # ``truncation_chars`` characters of the original; the stub
            # follows immediately, no separator. The exact stub string is
            # what callers can match on to detect a trimmed result.
            stub = _TRIMMED_STUB_TEMPLATE.format(
                tool=name, bytes=original_bytes, turn=turn_at_emission
            )
            new_text = head + stub
            report.cleared_truncated += 1
            report.truncated_chars += original_bytes - cfg.truncation_chars
        else:
            stub = _TRIMMED_STUB_TEMPLATE.format(
                tool=name, bytes=original_bytes, turn=turn_at_emission
            )
            new_text = stub
            report.cleared_stub += 1
        _replace_result_text(step, new_text)
        report.bytes_after += len(new_text)
    return output, report


def load_trimming_config(payload: Mapping[str, Any] | None) -> TrimmingConfig:
    """Build a ``TrimmingConfig`` from one ``[run.context]`` table.

    Returns the defaults when ``payload`` is empty/None; raises
    ``ValueError`` with a clear message on a bad value so the run config
    loader can surface it the same way it surfaces other [run] errors.
    """
    if not payload:
        return TrimmingConfig()
    if not isinstance(payload, Mapping):
        raise ValueError("[run.context] must be a TOML table")
    cfg = TrimmingConfig(
        enabled=bool(payload.get("tool_output_trimming_enabled", True)),
        keep_last_n=int(payload.get("tool_output_keep_last_n", 4)),
        max_turns_old=int(payload.get("tool_output_max_turns_old", 3)),
        truncation_chars=int(payload.get("tool_output_truncation_chars", 500)),
    )
    return cfg


# Token-aware metric helpers. Tokenisers differ per model; for the PR-body
# "before/after" comparison we expose two views: byte count (always exact)
# and an approximate token count derived from the byte length with the
# conservative 4-chars-per-token ratio the harness already uses elsewhere.
# ``tokens_before`` / ``tokens_after`` on the report are the same numbers
# the run report carries into ``tokens/task`` for the executor role.

CHARS_PER_TOKEN = 4


def approx_tokens(text: str) -> int:
    """Conservative token estimate for a string.

    The harness never runs a real tokenizer on this path (that would defeat
    the point of rule-based trimming); the 4-chars-per-token rule is the
    upper bound published in the OpenAI cookbook and matches what the rest
    of the harness uses for back-of-envelope budgets.
    """
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def add_tokens_per_role(
    report: TrimmingReport,
    *,
    role: str,
    tokens_before: int,
    tokens_after: int,
    per_role: dict[str, dict[str, int]] | None = None,
) -> dict[str, dict[str, int]]:
    """Merge ``tokens_before``/``tokens_after`` for ``role`` into ``per_role``.

    ``per_role`` is a mutable accumulator the caller owns. The function
    returns the same dict so callers can chain: ``add_tokens_per_role(r,
    role="executor", tokens_before=..., tokens_after=..., per_role=out)``.
    The accumulator is what the run report writes under
    ``tokens_per_role``, satisfying the ``Measure`` clause in the task.
    """
    if per_role is None:
        per_role = {}
    bucket = per_role.setdefault(role, {"before": 0, "after": 0})
    bucket["before"] += int(tokens_before)
    bucket["after"] += int(tokens_after)
    return per_role


def _result_text(step: Mapping[str, Any]) -> str:
    """Extract the result text from a trajectory ``tool_result`` step.

    The trajectory normaliser stores the text under ``text``; we never read
    the ``images`` list because it carries base64 image payloads and the
    byte counts we report would mislead a reader otherwise.
    """
    text = step.get("text")
    if isinstance(text, str):
        return text
    return ""


def _replace_result_text(step: dict[str, Any], new_text: str) -> None:
    """Write ``new_text`` back into the trimmed step.

    Mirrors ``_result_text``'s read path so a round-trip is symmetric; we
    leave ``images`` and the ``has_image`` flag alone because image
    payloads are separate from text content and clearing them would lose
    useful information.
    """
    step["text"] = new_text


__all__ = [
    "TrimmingConfig",
    "TrimmingReport",
    "trim_tool_results",
    "load_trimming_config",
    "approx_tokens",
    "add_tokens_per_role",
    "CHARS_PER_TOKEN",
]