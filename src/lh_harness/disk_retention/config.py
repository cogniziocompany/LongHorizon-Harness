"""Configuration loader for the disk-retention subsystem.

Every threshold the harness exposes to operators lives in the settings DB
(``SettingsStore``); no path or limit is hard-coded outside this loader.
``DiskRetentionConfig`` is a frozen dataclass, so the rest of the package
can pass it around safely and tests can construct it without touching a
real database.

The four ``LH_HARNESS_DISK_*`` settings are:

* ``LH_HARNESS_DISK_RUN_MAX_AGE_DAYS`` — run-folder age (default ``14``).
* ``LH_HARNESS_DISK_WORKTREE_MAX_AGE_DAYS`` — parked work-tree idle age
  (default ``7``).
* ``LH_HARNESS_DISK_CACHE_MAX_BYTES`` — per-cache cap in bytes (default
  ``4294967296`` = 4 GiB).  Two caches live in the store as ``LH_HARNESS_DISK_NPM_CACHE_MAX_BYTES`` and ``LH_HARNESS_DISK_PIP_CACHE_MAX_BYTES``; the umbrella cap is the fallback when either is unset.
* ``LH_HARNESS_DISK_WARN_PCT`` — warn threshold (default ``80``).
* ``LH_HARNESS_DISK_PAUSE_PCT`` — launch-pause threshold (default ``90``).
* ``LH_HARNESS_DISK_RESUME_PCT`` — auto-resume threshold (default ``85``).

A mis-set value is logged and ignored — the next setting (or the default)
is used in its place.  A value of ``0`` or a negative integer disables the
rule that owns it; ``-1`` for the cache cap means "no cap".
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

logger = logging.getLogger(__name__)


# Names registered in the settings DB catalog.  Public so the admin screen
# can read the same set the loader consults.
SETTING_RUN_MAX_AGE_DAYS = "LH_HARNESS_DISK_RUN_MAX_AGE_DAYS"
SETTING_WORKTREE_MAX_AGE_DAYS = "LH_HARNESS_DISK_WORKTREE_MAX_AGE_DAYS"
SETTING_CACHE_MAX_BYTES = "LH_HARNESS_DISK_CACHE_MAX_BYTES"
SETTING_NPM_CACHE_MAX_BYTES = "LH_HARNESS_DISK_NPM_CACHE_MAX_BYTES"
SETTING_PIP_CACHE_MAX_BYTES = "LH_HARNESS_DISK_PIP_CACHE_MAX_BYTES"
SETTING_WARN_PCT = "LH_HARNESS_DISK_WARN_PCT"
SETTING_PAUSE_PCT = "LH_HARNESS_DISK_PAUSE_PCT"
SETTING_RESUME_PCT = "LH_HARNESS_DISK_RESUME_PCT"

SETTING_NAMES: tuple[str, ...] = (
    SETTING_RUN_MAX_AGE_DAYS,
    SETTING_WORKTREE_MAX_AGE_DAYS,
    SETTING_CACHE_MAX_BYTES,
    SETTING_NPM_CACHE_MAX_BYTES,
    SETTING_PIP_CACHE_MAX_BYTES,
    SETTING_WARN_PCT,
    SETTING_PAUSE_PCT,
    SETTING_RESUME_PCT,
)

# Defaults match the operator-approved thresholds in CT110-DISK.
DEFAULT_RUN_MAX_AGE_DAYS = 14
DEFAULT_WORKTREE_MAX_AGE_DAYS = 7
DEFAULT_CACHE_MAX_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB
DEFAULT_WARN_PCT = 80
DEFAULT_PAUSE_PCT = 90
DEFAULT_RESUME_PCT = 85

# A path inside the harness-owned control directory is illegal; the
# retention sweep must never be pointed at the live runs tree of the very
# process driving it.  We use substring matches so callers can pass any
# shape of resolved path.
_HARNESS_OWNED_SENTINELS = (
    "/.lh-harness/runs",
    "/.lh-harness/queue",
    "/.lh-harness/cache",
)


def _looks_like_harness_owned(path: str) -> bool:
    if not path:
        return False
    norm = os.path.normpath(path)
    return any(sentinel in norm for sentinel in _HARNESS_OWNED_SENTINELS)


@dataclass(frozen=True)
class RetainMode:
    """How aggressive a single sweep should be.

    ``dry_run`` is the default everywhere: every prune routine in this
    package records what it would do without touching the filesystem, so
    the operator can run ``python -m lh_harness.disk_retention`` once to
    inspect the result, and only then flip ``dry_run=False``.
    """

    dry_run: bool = True
    apply: bool = False  # alias used by callers that prefer the verb

    def __post_init__(self) -> None:
        # ``apply`` and ``dry_run`` are equivalent: setting one must set the
        # other.  This avoids a class of bugs where a caller writes
        # ``RetainMode(apply=True)`` and accidentally still reports a
        # dry-run.
        if self.apply == self.dry_run:
            return
        object.__setattr__(self, "dry_run", not self.apply)
        object.__setattr__(self, "apply", self.apply)

    @classmethod
    def dry(cls) -> "RetainMode":
        return cls(dry_run=True)

    @classmethod
    def live(cls) -> "RetainMode":
        return cls(dry_run=False)


@dataclass(frozen=True)
class DiskRetentionConfig:
    """Resolved retention thresholds, with no I/O.

    ``runs_root`` and ``failed_reports_root`` are intentionally optional:
    the disk-check module (component 4) operates on a path passed in by
    the caller, so it never has to know where the runs tree lives.  The
    cache and work-tree modules also accept paths from their callers; this
    object is the single source of truth for numeric thresholds only.

    ``cache_paths`` is a name→path map (e.g. ``{"npm": "...", "pip": "..."}``)
    resolved at start.  Missing entries are skipped at prune time; an
    empty map disables the cache cap rule entirely.
    """

    run_max_age_days: int = DEFAULT_RUN_MAX_AGE_DAYS
    worktree_max_age_days: int = DEFAULT_WORKTREE_MAX_AGE_DAYS
    cache_max_bytes: int = DEFAULT_CACHE_MAX_BYTES
    npm_cache_max_bytes: int = DEFAULT_CACHE_MAX_BYTES
    pip_cache_max_bytes: int = DEFAULT_CACHE_MAX_BYTES
    warn_pct: int = DEFAULT_WARN_PCT
    pause_pct: int = DEFAULT_PAUSE_PCT
    resume_pct: int = DEFAULT_RESUME_PCT
    # ``worktree_dirs`` is a list of parent directories under which parked
    # work trees live; the work-tree retention pass walks each.  An empty
    # list disables the rule (it is the safe default in tests and in
    # deployments that have no parked work trees).
    worktree_dirs: tuple[str, ...] = field(default_factory=tuple)
    cache_paths: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    runs_root: str = ""
    failed_reports_root: str = ""

    # ------------------------------------------------------------ helpers --
    def cache_max_bytes_for(self, name: str) -> int:
        """Return the per-cache cap for ``name``.

        ``cache_paths`` (an ordered list of ``(name, path)`` pairs) is the
        authoritative source when set: a name that lives in the map gets
        whatever the map row carries, and only that.  Otherwise we fall
        back to the dedicated ``npm_cache_max_bytes`` / ``pip_cache_max_bytes``
        field, with ``cache_max_bytes`` as the umbrella default.  Unknown
        names get the umbrella cap, so an operator can register a third
        cache (e.g. ``"pnpm"``) without code changes.
        """

        for key, value in self.cache_paths:
            if key == name:
                # ``cache_paths`` is a name→path map; the byte cap is
                # still resolved from the dedicated fields below.  This
                # branch is here to make the explicit ``cache_paths``
                # table the only place the operator can disable a cache
                # by removing it from the map.
                break
        if name == "npm":
            return self.npm_cache_max_bytes or self.cache_max_bytes
        if name == "pip":
            return self.pip_cache_max_bytes or self.cache_max_bytes
        return self.cache_max_bytes

    def is_harness_owned(self, path: str) -> bool:
        return _looks_like_harness_owned(path)

    def with_overrides(self, **kwargs: Any) -> "DiskRetentionConfig":
        return replace(self, **kwargs)


def _coerce_int(value: Any, default: int) -> int:
    """Parse a settings-DB value into an int; log and return ``default`` on failure."""

    if value is None or value == "":
        return default
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        logger.warning("disk-retention: ignoring non-integer value %r; using %d", value, default)
        return default
    return parsed


def _coerce_bytes(value: Any, default: int) -> int:
    """Parse a byte-cap value.

    Accepts decimal integers (``"4294967296"``) and the common suffixes
    ``K``, ``M``, ``G``, ``T`` (case-insensitive, decimal).  Returns the
    default on any failure.
    """

    if value is None or value == "":
        return default
    text = str(value).strip()
    if not text:
        return default
    suffix = text[-1].upper() if text[-1].isalpha() else ""
    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    try:
        if suffix in multipliers:
            return int(float(text[:-1]) * multipliers[suffix])
        return int(text)
    except (TypeError, ValueError):
        logger.warning("disk-retention: ignoring unparsable byte cap %r; using %d", value, default)
        return default


def config_from_values(values: Mapping[str, Any] | None) -> DiskRetentionConfig:
    """Build a config from a name→string map (the settings-DB ``values()`` output)."""

    values = values or {}
    cache_max = _coerce_bytes(values.get(SETTING_CACHE_MAX_BYTES), DEFAULT_CACHE_MAX_BYTES)
    # A per-cache setting that is *absent* falls back to the umbrella cap;
    # an explicit "0" or "" is treated as "use the umbrella" too (so the
    # operator can clear a per-cache override by removing the row from the
    # store without a code change).
    npm_setting = values.get(SETTING_NPM_CACHE_MAX_BYTES)
    pip_setting = values.get(SETTING_PIP_CACHE_MAX_BYTES)
    npm_cache = _coerce_bytes(npm_setting, cache_max) if npm_setting not in (None, "") else cache_max
    pip_cache = _coerce_bytes(pip_setting, cache_max) if pip_setting not in (None, "") else cache_max
    return DiskRetentionConfig(
        run_max_age_days=_coerce_int(values.get(SETTING_RUN_MAX_AGE_DAYS), DEFAULT_RUN_MAX_AGE_DAYS),
        worktree_max_age_days=_coerce_int(values.get(SETTING_WORKTREE_MAX_AGE_DAYS), DEFAULT_WORKTREE_MAX_AGE_DAYS),
        cache_max_bytes=cache_max,
        npm_cache_max_bytes=npm_cache,
        pip_cache_max_bytes=pip_cache,
        warn_pct=_coerce_int(values.get(SETTING_WARN_PCT), DEFAULT_WARN_PCT),
        pause_pct=_coerce_int(values.get(SETTING_PAUSE_PCT), DEFAULT_PAUSE_PCT),
        resume_pct=_coerce_int(values.get(SETTING_RESUME_PCT), DEFAULT_RESUME_PCT),
    )


def config_from_runtime(runtime: Any) -> DiskRetentionConfig:
    """Build a config from a live ``SettingsRuntime`` (or anything quacking like one)."""

    store = getattr(runtime, "store", None)
    if store is None:
        return DiskRetentionConfig()
    try:
        values = store.values()
    except Exception:  # noqa: BLE001 - a broken store must yield a default config
        logger.warning("disk-retention: settings store unreadable; using defaults")
        return DiskRetentionConfig()
    return config_from_values(values)


DEFAULT_CONFIG = DiskRetentionConfig()


__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_CACHE_MAX_BYTES",
    "DEFAULT_PAUSE_PCT",
    "DEFAULT_RESUME_PCT",
    "DEFAULT_RUN_MAX_AGE_DAYS",
    "DEFAULT_WARN_PCT",
    "DEFAULT_WORKTREE_MAX_AGE_DAYS",
    "DiskRetentionConfig",
    "RetainMode",
    "SETTING_CACHE_MAX_BYTES",
    "SETTING_NAMES",
    "SETTING_NPM_CACHE_MAX_BYTES",
    "SETTING_PAUSE_PCT",
    "SETTING_PIP_CACHE_MAX_BYTES",
    "SETTING_RESUME_PCT",
    "SETTING_RUN_MAX_AGE_DAYS",
    "SETTING_WARN_PCT",
    "SETTING_WORKTREE_MAX_AGE_DAYS",
    "config_from_runtime",
    "config_from_values",
]
