"""Per-caller identity and tool scoping for the harness API (task 174).

Section 3.4 of the migration doc is explicit that the old single-bearer
authorization ("enforceable only at the gateway") is not enough: a leaked
bearer token must not be able to resolve operator gates.  Every harness-facing
caller therefore presents its name -- the ``X-Harness-Caller`` header on REST
routes, the ``caller`` field on MCP dispatch -- plus an HMAC-SHA256 over
``"<caller>:<ts>"`` keyed with the per-caller secret read from the environment
variable named by the caller's ``secret_env`` config key.  The config holds the
env variable NAME only; the secret value never appears in code or config.

A request with a missing name, an unknown name, a missing/expired timestamp, or
a bad signature resolves to the reserved ``"anon"`` caller, whose allowlist is
empty: 401 everywhere.  A verified caller invoking a tool outside its
allowlist gets 403; gate resolution refusals additionally emit an audit event
``{caller, tool, run_id, decision}``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import time
from typing import Any

from .config import load_caller_configs

# Reserved caller synthesized for missing/bad identity; its allowlist is empty.
ANON_CALLER = "anon"
# The MCP tool that unlocks the REST resolve route (POST
# /api/runs/{run_id}/approvals/{approval_id}/resolve).
RESOLVE_TOOL = "harness_resolve_gate"
# Pseudo-tool naming the overseer-only REST run-control routes (POST /api/runs,
# POST /api/runs/{id}/abort, /stop, /resume).  Gated on the caller's
# ``rest_run_control`` config flag instead of the ``tools`` allowlist.
REST_RUN_CONTROL_TOOL = "rest_run_control"

_CALLER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_SIGNATURE_SKEW_SECONDS = 300
_MAX_TS_CHARS = 20
_MAX_SIGNATURE_CHARS = 128

_REST_CALLER_HEADER = "x-harness-caller"
_REST_TIMESTAMP_HEADER = "x-harness-timestamp"
_REST_SIGNATURE_HEADER = "x-harness-signature"


def canonical_timestamp(value: Any) -> str | None:
    """Canonicalize the identity timestamp to whole-epoch-seconds text.

    Both surfaces (REST headers and MCP arguments) MAC the same canonical
    string -- ``str(int(seconds))`` -- so a client either presents the integer
    directly or a float that lands on a whole second.  A fractional stamp
    whose canonical form differs from what was signed fails the HMAC, so the
    canonicalization cannot be abused to smuggle an unsigned value past the
    signature: only the signed text is ever verified.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or len(text) > _MAX_TS_CHARS:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        if not math.isfinite(number) or number != int(number):
            return None
        return str(int(number))
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number) or number != int(number):
            return None
        return str(int(number))
    return None


def caller_configs_or_defaults(
    caller_configs: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Return an effective caller table, always containing ``anon``.

    ``anon`` is synthesized here (never configurable -- config.py rejects the
    name) with an empty allowlist so refusals are a plain allowlist miss.
    """
    # An empty table behaves like "not configured": the migration-doc defaults
    # apply (the loader itself returns pure defaults for an absent file, so
    # ``{}`` cannot be distinguished from "no [callers] section").
    specs = dict(caller_configs or load_caller_configs())
    specs[ANON_CALLER] = {
        "secret_env": "",
        "tools": [],
        "max_entries_per_hour": None,
        "max_rounds_clamp": None,
        "rest_run_control": False,
    }
    return specs


def _signature_message(caller: str, ts: str) -> bytes:
    """Canonical bytes the HMAC is computed over: ``<caller>:<ts>``."""
    return f"{caller}:{ts}".encode("utf-8")


def compute_signature(caller: str, ts: str, secret: str) -> str:
    """Return the hex HMAC-SHA256 clients must present for ``(caller, ts)``."""
    return hmac.new(
        secret.encode("utf-8"), _signature_message(caller, ts), hashlib.sha256
    ).hexdigest()


def _signature_matches(caller: str, ts: str, supplied: str, secret: str | None) -> bool:
    if not secret or not supplied:
        return False
    expected = compute_signature(caller, ts, secret)
    return hmac.compare_digest(expected, supplied.strip().lower())


def _secret_for(spec: dict[str, Any] | None) -> str | None:
    """Read the caller's secret from the env var NAMED in config.

    Only the name comes from config; the value is read from the environment at
    verification time and never logged or echoed.
    """
    if not spec:
        return None
    env_name = str(spec.get("secret_env") or "").strip()
    if not env_name:
        return None
    value = os.environ.get(env_name)
    return value if value else None


def _fresh(ts_text: str | None) -> str | None:
    """Validate the timestamp and return the canonical text, or None.

    The signature covers ``(caller, ts)``, so a stale or far-future stamp is
    refused: signatures older than the skew window are rejected outright,
    which keeps a sniffed header pair from being replayed indefinitely.
    """
    if ts_text is None:
        return None
    try:
        value = float(ts_text)
    except ValueError:
        return None
    if value <= 0:
        return None
    if abs(time.time() - value) > _SIGNATURE_SKEW_SECONDS:
        return None
    return ts_text


def _verified_caller(
    caller: Any, ts: Any, signature: Any, specs: dict[str, dict[str, Any]]
) -> str:
    """Return the caller name only when name+ts+signature all check out."""
    if not isinstance(caller, str) or not caller:
        return ANON_CALLER
    caller = caller.strip()
    if caller == ANON_CALLER or not _CALLER_NAME_RE.match(caller):
        return ANON_CALLER
    ts_text = _fresh(canonical_timestamp(ts))
    if ts_text is None or not isinstance(signature, str) or not signature:
        return ANON_CALLER
    spec = specs.get(caller)
    if spec is None:
        # An unknown name cannot hold a valid signature (no secret is
        # configured for it), so it is anon rather than a deny-listed caller.
        return ANON_CALLER
    if not _signature_matches(caller, ts_text, signature, _secret_for(spec)):
        return ANON_CALLER
    return caller


def resolve_rest_caller(
    headers: Any, specs: dict[str, dict[str, Any]] | None = None
) -> str:
    """Resolve the caller from REST headers, falling back to ``anon``."""
    if specs is None:
        specs = caller_configs_or_defaults(None)
    return _verified_caller(
        headers.get(_REST_CALLER_HEADER),
        headers.get(_REST_TIMESTAMP_HEADER),
        headers.get(_REST_SIGNATURE_HEADER),
        specs,
    )


def resolve_mcp_caller(
    arguments: dict[str, Any], specs: dict[str, dict[str, Any]] | None = None
) -> str:
    """Resolve the caller from MCP dispatch arguments, falling back to ``anon``."""
    if not isinstance(arguments, dict):
        return ANON_CALLER
    if specs is None:
        specs = caller_configs_or_defaults(None)
    return _verified_caller(
        arguments.get("caller"),
        arguments.get("caller_ts"),
        arguments.get("caller_sig"),
        specs,
    )


CALLER_ARGUMENT_KEYS = ("caller", "caller_ts", "caller_sig")


def strip_caller_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return tool arguments without the caller-identity fields."""
    return {key: value for key, value in arguments.items() if key not in CALLER_ARGUMENT_KEYS}


def tool_allowed(caller: str, tool: str, specs: dict[str, dict[str, Any]]) -> bool:
    """Return True only when ``tool`` is on ``caller``'s configured allowlist."""
    if caller == ANON_CALLER:
        return False
    tools = specs.get(caller, {}).get("tools") or []
    return tool in tools


def run_control_allowed(caller: str, specs: dict[str, dict[str, Any]]) -> bool:
    """Return True only for callers whose ``rest_run_control`` flag is True."""
    if caller == ANON_CALLER:
        return False
    return bool(specs.get(caller, {}).get("rest_run_control", False))


def emit_caller_audit(runs_root: Any, event: dict[str, Any]) -> bool:
    """Append one audit event to ``<runs_root>/queue/caller_audit.jsonl``.

    Durable-but-simple emit mechanism for task 174 refusals (the pg store's
    ``queue_events`` table is the richer model this can graduate to).  Best
    effort: an audit write failure must never turn into a crash or mask the
    HTTP refusal it accompanies.
    """
    if not runs_root:
        return False
    path = os.path.join(str(runs_root), "queue", "caller_audit.jsonl")
    line = json.dumps({"ts": time.time(), **event}, sort_keys=True)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        return False
    return True


def emit_refusal_audit(
    runs_root: Any, *, caller: str, tool: str, run_id: str | None
) -> bool:
    """Emit the task-174 refusal event ``{caller, tool, run_id, decision}``."""
    return emit_caller_audit(
        runs_root,
        {
            "caller": caller,
            "tool": tool,
            "run_id": run_id,
            "decision": "denied",
        },
    )


# ---------------------------------------------------------------- ceilings --
# Section 3.5 budget ceilings (task 174). ``max_rounds_clamp`` is a hard
# refusal (422), never a silent truncation: an entry launched with fewer
# rounds than the client asked for would fail its task half-way through and
# nobody would know why. ``max_entries_per_hour`` is a rolling one-hour
# window counted from the durable queue (entries this caller created), so the
# ceiling survives an API restart instead of resetting with the process.


_ENQUEUE_WINDOW_SECONDS = 3600


def hourly_entry_count(queue_store: Any, caller: str, *, now: float | None = None) -> int:
    """Count queue entries ``caller`` created inside the rolling hour window.

    Entries carry ``requested_by`` stamped from the verified caller at enqueue
    time (including requeue successors, which inherit it), so the durable
    queue is the ledger for the hourly ceiling.
    """
    if queue_store is None:
        return 0
    current = time.time() if now is None else now
    try:
        entries = queue_store.list()
    except Exception:
        return 0
    cutoff = current - _ENQUEUE_WINDOW_SECONDS
    return sum(
        1
        for entry in entries
        if entry.requested_by == caller and entry.created_at >= cutoff
    )


def enqueue_rate_violation(
    spec: dict[str, Any] | None,
    queue_store: Any,
    caller: str,
    *,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Return a 429 refusal when ``caller`` is over ``max_entries_per_hour``.

    The result carries ``code``/``error`` for the MCP shape and
    ``retry_after`` seconds (time until the oldest in-window entry leaves the
    window) for the REST ``Retry-After`` header.  ``None`` means unlimited or
    under the ceiling.
    """
    if caller == ANON_CALLER or not spec:
        return None
    limit = spec.get("max_entries_per_hour")
    if limit is None:
        return None
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        # Malformed config degrades to unlimited rather than locking callers
        # out; the config loader rejects these shapes long before runtime.
        return None
    current = time.time() if now is None else now
    if queue_store is None:
        return None
    try:
        entries = queue_store.list()
    except Exception:
        return None
    recent = [
        entry
        for entry in entries
        if entry.requested_by == caller
        and entry.created_at >= current - _ENQUEUE_WINDOW_SECONDS
    ]
    if len(recent) < limit:
        return None
    if recent:
        oldest = min(entry.created_at for entry in recent)
        retry_after = max(1, int(math.ceil(oldest + _ENQUEUE_WINDOW_SECONDS - current)))
    else:
        # A zero cap with an empty window simply waits out the full hour.
        retry_after = _ENQUEUE_WINDOW_SECONDS
    return {
        "code": 429,
        "error": (
            f"caller {caller!r} reached max_entries_per_hour ({limit}); "
            f"retry after {retry_after}s"
        ),
        "retry_after": retry_after,
    }


def rounds_clamp_violation(spec: dict[str, Any] | None, max_rounds: Any) -> str | None:
    """Return a 422 reason when ``max_rounds`` exceeds ``max_rounds_clamp``.

    An omitted ``max_rounds`` is checked against the store's default (25), so
    a clamp below the default cannot be bypassed by simply not sending the
    field.  The request is refused, never truncated: a run silently launched
    with fewer rounds than requested would fail mid-task.
    """
    if not spec:
        return None
    clamp = spec.get("max_rounds_clamp")
    if clamp is None:
        return None
    if not isinstance(clamp, int) or isinstance(clamp, bool) or clamp < 1:
        return None
    if max_rounds is None:
        from .types import DEFAULT_MAX_ROUNDS

        max_rounds = DEFAULT_MAX_ROUNDS
    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int):
        # Type violations are the queue store's validation error (422 there);
        # the clamp only bounds integers.
        return None
    if max_rounds <= clamp:
        return None
    return (
        f"max_rounds {max_rounds} exceeds the caller's max_rounds_clamp of "
        f"{clamp}; the request is refused, not truncated"
    )


def may_delete_entry(
    caller: str, spec: dict[str, Any] | None, entry: Any
) -> bool:
    """DELETE /api/queue/{id} ownership rule (task 174, scope item 5).

    Callers trusted with gate resolution (operator class -- their allowlist
    contains ``harness_resolve_gate``) manage the whole queue.  Every other
    caller may delete only entries it created, proven by the ``requested_by``
    stamp the harness wrote from its verified identity at enqueue time.
    """
    if caller == ANON_CALLER:
        return False
    if spec and RESOLVE_TOOL in (spec.get("tools") or []):
        return True
    return bool(entry is not None and entry.requested_by == caller)


def header_names() -> dict[str, str]:
    """Expose the REST header names for callers of this module."""
    return {
        "caller": _REST_CALLER_HEADER,
        "timestamp": _REST_TIMESTAMP_HEADER,
        "signature": _REST_SIGNATURE_HEADER,
    }


__all__ = [
    "ANON_CALLER",
    "CALLER_ARGUMENT_KEYS",
    "REST_RUN_CONTROL_TOOL",
    "RESOLVE_TOOL",
    "caller_configs_or_defaults",
    "canonical_timestamp",
    "compute_signature",
    "emit_caller_audit",
    "emit_refusal_audit",
    "enqueue_rate_violation",
    "header_names",
    "hourly_entry_count",
    "may_delete_entry",
    "resolve_mcp_caller",
    "resolve_rest_caller",
    "rounds_clamp_violation",
    "run_control_allowed",
    "strip_caller_arguments",
    "tool_allowed",
]