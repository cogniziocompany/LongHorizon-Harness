"""Rule-based redaction (MSCE B.11) applied to every text field before write.

The sink for experience traces is shared: run dirs are volumes on every node,
the fleet ingester walks them, and (post-ingest) trace text lands in
``hivemind_sessions.session_memories.metadata``. Everything under that sink
must be unreadable to a secret, so this module is the single choke point the
store calls before any write: :func:`redact_trace` covers every text field of
a ``TraceUnit`` (trace texts, rationales, role identities, error signatures,
repo/branch/workspace, tags, tool names, device identifiers).

Rules are deliberately regex-only and deterministic — no LLM call, no
network. The pattern set (per the task spec):

* bearer credentials — the ``Bearer`` authorization scheme followed by a
  token-length string (the lowercase phrase "bearer tokens rotate" is held to
  the same charset/length bar, so prose survives);
* ``sk-*`` style API keys (``sk-ant-…``, ``sk-proj-…``, ``sk-…``);
* GitHub token families (``ghp_*`` and siblings ``gho_/ghu_/ghs_/ghr_``, plus
  fine-grained ``github_pat_…``);
* PEM ``PRIVATE KEY`` blocks (body replaced, markers kept so a reader still
  sees a key was there);
* ``password=``/``passwd=`` assignments (key kept, value masked);
* unbroken hex strings of 32+ characters (API hashes, tokens, key material);
* known secret env var NAMES — whenever one of those names appears with an
  assignment (``NAME=value`` or ``NAME: value``), the value is masked.

Specific patterns run before the generic hex catch-all so a masked value is
never re-consumed. Redaction is idempotent: applying it twice changes
nothing extra. The generic env-name shapes in
``lh_harness.adapters.cli_agent.redact_secrets`` predate this module; this one
carries the MSCE-specific rules and the *known-names* list and is the one the
experience layer calls.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .trace import TraceUnit

REDACTED = "***REDACTED***"

# Known secret env var NAMES (spec: the "known env-var NAMES list"). These are
# credentials this fleet's tooling legitimately puts in the environment; when
# log or report text echoes them as assignments, only the value is masked —
# the name itself is not secret.
SECRET_ENV_VAR_NAMES: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_AUTH_TOKEN",
    "CODEX_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "HF_TOKEN",
    "HUGGINGFACE_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "LH_HARNESS_WEB_TOKEN",
    "LH_HARNESS_MCP_GATEWAY_KEY",
    "MEMORY_MCP_TOKEN",
    "LITELLM_MASTER_KEY",
)

_QUOTED_OR_BARE_VALUE = r"(?:'[^'\n]*'|\"[^\"\n]*\"|[^\s]+)"

# PEM private key blocks: body replaced, BEGIN/END markers kept.
_PRIVATE_KEY_BLOCK_RE = re.compile(
    r"(-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----)(.*?)(-----END (?:[A-Z0-9]+ )?PRIVATE KEY-----)",
    re.DOTALL,
)

# Bearer scheme + token. The 8-char minimum and the token charset keep prose
# ("bearer tokens rotate daily" — "tokens" is 6 lowercase chars) from being
# mauled; real credentials are far longer and always match.
_BEARER_RE = re.compile(
    r"(?i)(\bbearer\b[ \t]*[:=]?[ \t]*[\"']?)([A-Za-z0-9][A-Za-z0-9._~+/=\"'-]{7,})"
)

_SK_KEY_RE = re.compile(r"(?<![\w-])sk-[A-Za-z0-9][A-Za-z0-9_-]{15,}")

_GITHUB_TOKEN_RE = re.compile(
    r"(?<![\w])(?:gh[pousr]_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{20,})"
)

_PASSWORD_ASSIGNMENT_RE = re.compile(
    rf"(?i)(\b[a-z0-9_.-]*(?:password|passwd)[a-z0-9_.-]*\s*[=:]\s*){_QUOTED_OR_BARE_VALUE}"
)

_ENV_NAME_VALUE_RE = re.compile(
    r"(\b(?:"
    + "|".join(re.escape(name) for name in SECRET_ENV_VAR_NAMES)
    + rf")\b\s*[=:]\s*){_QUOTED_OR_BARE_VALUE}"
)

# Generic catch-all: unbroken hex of 32+ chars. Runs last.
_LONG_HEX_RE = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{32,}(?![0-9A-Za-z])")


def redact_text(text: str) -> str:
    """Mask secrets in one string. Idempotent; never raises on str input."""
    value = str(text)
    if not value:
        return value
    value = _PRIVATE_KEY_BLOCK_RE.sub(
        lambda match: f"{match.group(1)}\n{REDACTED}\n{match.group(3)}",
        value,
    )
    value = _BEARER_RE.sub(lambda match: f"{match.group(1)}{REDACTED}", value)
    value = _SK_KEY_RE.sub(REDACTED, value)
    value = _GITHUB_TOKEN_RE.sub(REDACTED, value)
    value = _PASSWORD_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{REDACTED}", value
    )
    value = _ENV_NAME_VALUE_RE.sub(lambda match: f"{match.group(1)}{REDACTED}", value)
    value = _LONG_HEX_RE.sub(REDACTED, value)
    return value


def redact_value(value: Any) -> Any:
    """Recursively redact strings inside mappings/sequences; scalars pass through."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            redact_text(key) if isinstance(key, str) else key: redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        cleaned = [redact_value(item) for item in value]
        return tuple(cleaned) if isinstance(value, tuple) else cleaned
    return value


def redact_trace(unit: TraceUnit) -> TraceUnit:
    """Return a copy of ``unit`` with every text field redacted.

    Serialising through :meth:`TraceUnit.to_dict` and rebuilding keeps the
    absent/empty contract intact: fields the round never recorded stay absent
    rather than materialising as empty strings, and role/rationale/device
    fields are covered by the recursive value pass.
    """
    payload = redact_value(unit.to_dict())
    return TraceUnit(**payload)


def redact_texts(values: Sequence[str]) -> tuple[str, ...]:
    """Small batch helper for callers holding raw field text."""
    return tuple(redact_text(value) for value in values)
