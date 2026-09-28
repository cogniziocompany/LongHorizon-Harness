"""Read-only API for the three MSCE experience levels (Paxton 2026-09-08).

Three GET endpoints let the fleet surfaces — the Hydra right-hand panel and
the fleet wallboard — render the experience layer without reverse-engineering
storage:

- ``GET /api/experience/environment`` — L3, this instance's environmental
  cognition: which hosts exist and what each is for, the standing
  constraints that govern the fleet, and the routing backends by name.
  Seeded today from :mod:`lh_harness.experience.seed` plus the instance's
  optional ``[experience]`` table; learned items supersede seeded ones when
  induction lands (not Phase 1). Every item is addressable by a stable id
  and carries ``origin`` / ``seeded_at`` / ``source`` / ``superseded_*``.
- ``GET /api/experience/policies`` — L2, paginated. Empty but addressable
  from day one; the response carries :data:`POLICY_ITEM_SCHEMA` so a UI can
  render against the collection's final shape instead of a placeholder.
- ``GET /api/experience/runs/{run_id}/traces`` — L1 for one run, paginated.
  Reads the run's own ``role_orchestration/experience.jsonl`` through the
  same ``StateRegistry.state_for`` run-boundary checks as every other
  run-scoped route; device fields (``device_id`` / ``terminal_id`` /
  ``hydra_node``) are served exactly as recorded — identical strings, never
  re-keyed or truncated — and stay absent for rounds that did no remote
  execution.

Read-only in Phase 1: no write endpoints, no editing from the UI. The bearer
boundary is the shared ``/api/*`` auth middleware in :mod:`.server`; these
routes add nothing to it. Redaction runs again at serve time on every item of
every response (:func:`redact_value`), so a value redaction would strip from
a trace can never be returned — even from a ledger written by another tool.
Reads never write to any workspace or run dir.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Query

from ..config import PROJECT_CONFIG_PATH, ProjectConfigError
from ..experience import (
    EXPERIENCE_FILENAME,
    POLICY_ITEM_SCHEMA,
    experience_ledger_path,
    load_seeded_levels,
    read_trace_records,
    redact_value,
)
from ..utils.run_boundary import (
    CANONICAL_ROLE_DIR,
    LEGACY_LOG_DIR,
    LEGACY_ROLE_DIR,
)

if TYPE_CHECKING:  # pragma: no cover - typing only; avoids the server import cycle
    from .server import StateRegistry

_PAGE_DEFAULT = 50
_PAGE_MAX = 500


def _experience_config_path(runs_root: Path | None) -> Path:
    """Where this instance's ``[experience]`` override table may live.

    Same resolution rule the queue config uses: the project config next to
    the runs root first, then the process working directory (see
    ``server._runs_root_config_path``). A missing file yields the pure
    built-in seeds; a malformed table surfaces as a 500 below, the same error
    family every other bad ``config.toml`` produces.
    """
    if runs_root is not None:
        candidate = runs_root / ".lh-harness" / "config.toml"
        if candidate.is_file():
            return candidate
    return PROJECT_CONFIG_PATH


def register_experience_api(
    app: FastAPI,
    registry: "StateRegistry",
    *,
    runs_root: Path | None = None,
) -> None:
    """Register the read-only ``/api/experience/*`` routes on ``app``."""

    def _levels() -> Any:
        try:
            return load_seeded_levels(_experience_config_path(runs_root))
        except ProjectConfigError as exc:
            raise HTTPException(
                status_code=500, detail=f"invalid [experience] config: {exc}"
            ) from exc

    @app.get("/api/experience/environment")
    def experience_environment() -> dict[str, Any]:
        """L3 for this instance: the seeded (and later learned) fleet knowledge."""
        levels = _levels()
        items = [redact_value(dict(item)) for item in levels.environment]
        return {
            "level": "L3",
            "kind": "environment",
            "count": len(items),
            "items": items,
        }

    @app.get("/api/experience/policies")
    def experience_policies(
        offset: int = Query(0, ge=0),
        limit: int = Query(_PAGE_DEFAULT, ge=1, le=_PAGE_MAX),
    ) -> dict[str, Any]:
        """L2, paginated: empty today, addressable in its final shape from day one."""
        levels = _levels()
        ordered = [
            redact_value(dict(levels.policies[policy_id]))
            for policy_id in sorted(levels.policies)
        ]
        return {
            "level": "L2",
            "kind": "policy",
            "schema": POLICY_ITEM_SCHEMA,
            "total": len(ordered),
            "offset": offset,
            "limit": limit,
            "items": ordered[offset : offset + limit],
        }

    @app.get("/api/experience/runs/{run_id}/traces")
    def experience_traces(
        run_id: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(_PAGE_DEFAULT, ge=1, le=_PAGE_MAX),
    ) -> dict[str, Any]:
        """L1 for one run, paginated and redacted, in ledger (round) order."""
        state = registry.state_for(run_id)
        if state is None:
            raise HTTPException(status_code=404, detail="run not found")
        log_dir = Path(state.log_dir)
        role_name = (
            LEGACY_ROLE_DIR if log_dir.name == LEGACY_LOG_DIR else CANONICAL_ROLE_DIR
        )
        ledger = experience_ledger_path(log_dir / role_name)
        # Records are redacted at write time by the capture pipeline; they are
        # redacted again here so the serve boundary never depends on who wrote
        # the ledger.
        records = [redact_value(record) for record in read_trace_records(ledger)]
        return {
            "run_id": run_id,
            "level": "L1",
            "kind": "trace",
            "ledger": f"{role_name}/{EXPERIENCE_FILENAME}",
            "captured": ledger.is_file() and not ledger.is_symlink(),
            "total": len(records),
            "offset": offset,
            "limit": limit,
            "items": records[offset : offset + limit],
        }
