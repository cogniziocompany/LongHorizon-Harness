"""Fleet trios payload: describe queue trios as fleet-admin ingests them.

The launcher keeps the canonical trio state (``queue_trios()``, per-role model
overrides from ``trio_role_overrides``, capacity from ``<name>_max``). This
module materializes that state — plus the live run counts the launcher carries
per trio — into a ``lh-trios/v1`` envelope that fleet-admin can diff cheaply.
The same dictionary shape also feeds :meth:`FleetReporter.send_trios`, so a
slice that wires launcher triggers later does not need a second builder.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from typing import Any

# Roles the queue trios pick agents/models for.  Matches
# ``trio_role_overrides`` in ``src/lh_harness/settings_store.py`` and the
# ``_trio_role_overrides`` walker in ``src/lh_harness/launcher.py``.
_ROLES = ("manager", "executor", "auditor")
_TRIO_CAPACITY_SUFFIX = "_max"

# Schema id of this envelope.  Bumped when the wire format changes; fleet-admin
# pins the version it accepts.
SCHEMA = "lh-trios/v1"


def _now_iso_utc() -> str:
    """ISO 8601 / RFC 3339 UTC timestamp, second precision, ``Z`` suffix.

    ``datetime.now(timezone.utc)`` plus ``strftime`` keeps a stable, sortable
    string (``... 10:11:12Z``) regardless of the runner's locale; ISO format
    without ``Z`` is accepted but ``Z`` is the canonical suffix fleet-admin
    stores.
    """

    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_role_model(
    trio_spec: dict[str, Any],
    role: str,
    trio_model: str | None,
) -> str | None:
    """Per-role override wins over the trio-wide model.

    Keys are ``<role>_model``; missing/blank overrides fall back to the trio
    model so a mixed trio (zaiq: z.ai manager/auditor, local qwen executor)
    still reports a usable model for every role.
    """

    key = f"{role}_model"
    value = trio_spec.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return trio_model


def _trio_model(spec: dict[str, Any]) -> str | None:
    """The trio-wide ``model`` value, stripped or ``None``."""

    value = spec.get("model")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _live_count(live_counts: dict[str, Any], name: str) -> int:
    """Live runs for ``name``, clamped to a non-negative int.

    A missing name counts as zero.  Anything non-int-ish falls back to zero so
    a buggy caller cannot poison the payload with ``None`` / strings.
    """

    value = live_counts.get(name)
    try:
        count = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, count)


def _capacity_for(capacity: dict[str, int], name: str) -> int:
    """Return ``<name>_max`` for the given trio, or 0 when missing / malformed."""

    if not isinstance(capacity, dict):
        return 0
    try:
        value = capacity.get(f"{name}{_TRIO_CAPACITY_SUFFIX}", 0)
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def build_trios_payload(
    config: dict[str, Any],
    node: str,
    live_counts: dict[str, Any],
) -> dict[str, Any]:
    """Project the launcher's current trio state into the ``lh-trios/v1`` wire shape.

    Inputs mirror the launcher's view: ``config`` carries ``trios`` (name →
    spec with ``model`` / ``mcp_profile`` / per-role ``<role>_model`` keys)
    and ``capacity`` (``<name>_max`` keys).  ``live_counts`` is the
    ``{trio_name: live_runs}`` mapping the launcher already maintains for its
    occupancy guard.

    Returned envelope:

    - ``schema`` / ``sentAt`` / ``node`` envelope metadata;
    - ``configSha256`` is the sha256 of the trios-and-capacity view that drove
      this payload, so fleet-admin can short-circuit any client that repeats a
      payload it already has;
    - ``trios`` is one row per trio with its per-role model (override when
      set, else the trio-wide value); ``enabled`` flags rows the launcher has
      marked off so a fleet-admin operator can see them without re-reading
      config;
    - ``drain`` is true when every trio has capacity 0 — a fleet-admin UI
      signal, not an order.
    """

    trios_raw = config.get("trios") if isinstance(config, dict) else None
    trios_view: dict[str, dict[str, Any]] = (
        {str(k): dict(v) for k, v in trios_raw.items() if isinstance(v, dict)}
        if isinstance(trios_raw, dict)
        else {}
    )

    capacity_raw = config.get("capacity") if isinstance(config, dict) else None
    capacity_view: dict[str, int] = {}
    if isinstance(capacity_raw, dict):
        for key, value in capacity_raw.items():
            if not isinstance(key, str) or not key.endswith(_TRIO_CAPACITY_SUFFIX):
                continue
            try:
                capacity_view[key] = max(0, int(value))
            except (TypeError, ValueError):
                continue

    rows: list[dict[str, Any]] = []
    sha_trios: dict[str, dict[str, Any]] = {}
    for name in sorted(trios_view.keys()):
        spec = trios_view[name]
        enabled = bool(spec.get("enabled", True))
        capacity_value = _capacity_for(capacity_view, name)
        trio_model = _trio_model(spec)
        manager = _resolve_role_model(spec, "manager", trio_model)
        executor = _resolve_role_model(spec, "executor", trio_model)
        auditor = _resolve_role_model(spec, "auditor", trio_model)
        rows.append(
            {
                "name": name,
                "enabled": enabled,
                "capacity": capacity_value,
                "live": _live_count(live_counts or {}, name),
                "manager": manager,
                "executor": executor,
                "auditor": auditor,
            }
        )
        sha_trios[name] = {
            "enabled": enabled,
            "manager": manager,
            "executor": executor,
            "auditor": auditor,
        }

    drain = all(row["capacity"] <= 0 for row in rows) if rows else True

    # Sha is over the part that actually drives fleet-admin behavior: the
    # trio row keys and the capacity dict (sorted on key so a re-serialized
    # JSON byte stream never poisons the digest).  ``drain`` and ``live``
    # are intentionally OUT — fleet-admin only needs to compare
    # configuration, not liveness.
    sha_input = {
        "trios": sha_trios,
        "capacity": dict(sorted(capacity_view.items())),
    }
    digest = hashlib.sha256(
        json.dumps(sha_input, sort_keys=True).encode("utf-8")
    ).hexdigest()

    return {
        "schema": SCHEMA,
        "sentAt": _now_iso_utc(),
        "node": node,
        "configSha256": digest,
        "trios": rows,
        "drain": drain,
    }
