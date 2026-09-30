"""Spec size measurement and the learned expected-size range.

A queued task's spec (see ``templates/task-spec.md``) is measured, not policed:
there is no fixed token budget. Instead every spec that reaches
``ready-for-dev`` contributes its estimated token count to a rolling window,
and a robust range (median +/- k * scaled MAD) is recomputed after each one.
A spec outside that range is *flagged* to the operator; it is never blocked.

Median/MAD rather than mean/stdev because spec sizes are right-skewed and the
history is small; a couple of bloated specs must not drag the range with them.

The only I/O here is one JSON file, ``<runs_root>/queue/spec_stats.json``.
The live file queue (``C:/tmp/stage_specs.py``) feeds the same file through
the ``/api/queue/spec_stats`` routes so both queues share one distribution.
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .supervisor.control_bus import _atomic_bytes_write

SPEC_STATS_FILENAME = "spec_stats.json"
DEFAULT_SPEC_STATS_CONFIG: dict[str, Any] = {"window": 50, "min_samples": 5, "k": 3.0}
# Consistency constant that makes MAD comparable to a standard deviation for
# normally distributed data.
_MAD_SCALE = 1.4826
# Rough chars-per-token for English prose/markdown when no tokenizer is available.
_CHARS_PER_TOKEN = 4

RangeState = str  # "in_range" | "below" | "above" | "unestablished"
SPEC_STATUS_READY = "ready-for-dev"
SPEC_STATUS_DRAFT = "draft"

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)


@dataclass
class SpecMeasure:
    chars: int
    tokens_est: int
    exact: bool
    measured_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SpecStats:
    n: int = 0
    median: float | None = None
    mad: float | None = None
    lower: float | None = None
    upper: float | None = None
    updated_at: float | None = None
    established: bool = False
    samples: dict[str, int] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        """Shape returned by the API (samples omitted)."""

        data = asdict(self)
        data.pop("samples", None)
        return data


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a ``---`` YAML-ish frontmatter block from a markdown body.

    Only ``key: value`` scalars and ``key: [a, b]`` inline lists are parsed;
    that is all the spec template uses, and it keeps this module free of a
    YAML dependency.
    """

    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    meta: dict[str, Any] = {}
    for raw in match.group(1).splitlines():
        line = raw.split("#", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            meta[key] = [item.strip().strip("'\"") for item in inner.split(",") if item.strip()] if inner else []
        else:
            meta[key] = value.strip("'\"")
    return meta, text[match.end():]


def spec_status_from_text(text: str) -> str | None:
    meta, _ = parse_frontmatter(text)
    status = meta.get("status")
    return str(status).strip().lower() if status is not None else None


def estimate_tokens(text: str) -> int:
    return int(math.ceil(len(text) / _CHARS_PER_TOKEN)) if text else 0


def _exact_token_count(text: str) -> int | None:
    """Count tokens with the Anthropic client when it is importable and keyed.

    Any failure falls back to the estimate; a spec measurement must never make
    the queue API depend on network reachability.
    """

    if not os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LH_HARNESS_SPEC_EXACT_TOKENS", "1") == "0":
        return None
    try:  # pragma: no cover - exercised only with a live key
        import anthropic  # type: ignore

        client = anthropic.Anthropic()
        response = client.messages.count_tokens(
            model=os.environ.get("LH_HARNESS_SPEC_TOKEN_MODEL", "claude-sonnet-5"),
            messages=[{"role": "user", "content": text}],
        )
        value = getattr(response, "input_tokens", None)
        return int(value) if isinstance(value, int) else None
    except Exception:
        return None


def measure_text(text: str, *, exact: bool = True) -> SpecMeasure:
    count = _exact_token_count(text) if exact else None
    if count is None:
        return SpecMeasure(chars=len(text), tokens_est=estimate_tokens(text), exact=False, measured_at=time.time())
    return SpecMeasure(chars=len(text), tokens_est=count, exact=True, measured_at=time.time())


def measure_spec(path: str | Path, *, exact: bool = True) -> SpecMeasure:
    text = Path(path).read_text(encoding="utf-8")
    return measure_text(text, exact=exact)


def spec_stats_path(runs_root: str | Path) -> Path:
    return Path(runs_root).expanduser().resolve() / "queue" / SPEC_STATS_FILENAME


def _normalize_config(config: dict[str, Any] | None) -> tuple[int, int, float]:
    cfg = dict(DEFAULT_SPEC_STATS_CONFIG)
    if isinstance(config, dict):
        cfg.update({k: v for k, v in config.items() if k in cfg})
    window = max(1, int(cfg["window"]))
    min_samples = max(1, int(cfg["min_samples"]))
    k = float(cfg["k"])
    if k <= 0:
        raise ValueError("spec_stats.k must be positive")
    return window, min_samples, k


def compute_stats(samples: dict[str, int], *, config: dict[str, Any] | None = None) -> SpecStats:
    """Recompute the range from ``samples`` (insertion order = finish order)."""

    window, min_samples, k = _normalize_config(config)
    kept = list(samples.items())[-window:]
    values = [int(v) for _, v in kept]
    stats = SpecStats(n=len(values), samples=dict(kept), updated_at=time.time())
    if len(values) < min_samples:
        return stats
    median = float(statistics.median(values))
    mad = float(statistics.median(abs(v - median) for v in values))
    spread = k * _MAD_SCALE * mad
    stats.median = median
    stats.mad = mad
    stats.lower = max(0.0, median - spread)
    stats.upper = median + spread
    stats.established = True
    return stats


def load_stats(path: str | Path) -> SpecStats:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SpecStats()
    if not isinstance(data, dict):
        return SpecStats()
    samples = data.get("samples")
    clean: dict[str, int] = {}
    if isinstance(samples, dict):
        for key, value in samples.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                clean[str(key)] = int(value)
    stats = SpecStats(samples=clean)
    for key in ("n", "median", "mad", "lower", "upper", "updated_at", "established"):
        if key in data:
            setattr(stats, key, data[key])
    return stats


def save_stats(path: str | Path, stats: SpecStats) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(stats), ensure_ascii=False, sort_keys=True, indent=2)
    _atomic_bytes_write(target, payload.encode("utf-8"))


def record_finished(
    path: str | Path,
    key: str,
    tokens_est: int,
    *,
    config: dict[str, Any] | None = None,
) -> SpecStats:
    """Add (or overwrite) one finished spec's size and recompute the range."""

    if not isinstance(key, str) or not key.strip():
        raise ValueError("key is required")
    if isinstance(tokens_est, bool) or not isinstance(tokens_est, int) or tokens_est < 0:
        raise ValueError("tokens_est must be a non-negative integer")
    current = load_stats(path)
    samples = dict(current.samples)
    # A re-marked spec replaces its earlier sample rather than duplicating it.
    samples.pop(key, None)
    samples[key] = tokens_est
    stats = compute_stats(samples, config=config)
    save_stats(path, stats)
    return stats


def classify(tokens_est: int | None, stats: SpecStats | None) -> RangeState:
    if tokens_est is None or stats is None or not stats.established:
        return "unestablished"
    if stats.lower is not None and tokens_est < stats.lower:
        return "below"
    if stats.upper is not None and tokens_est > stats.upper:
        return "above"
    return "in_range"


def spec_stats_config_from_config(config: dict[str, Any]) -> dict[str, Any]:
    """Extract ``[queue.spec_stats]`` with defaults applied."""

    queue = config.get("queue", {}) if isinstance(config, dict) else {}
    section = queue.get("spec_stats", {}) if isinstance(queue, dict) else {}
    result = dict(DEFAULT_SPEC_STATS_CONFIG)
    if isinstance(section, dict):
        for key in result:
            if key in section and isinstance(section[key], (int, float)) and not isinstance(section[key], bool):
                result[key] = section[key]
    return result
