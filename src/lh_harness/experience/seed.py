"""Seeded defaults for the three MSCE levels (Paxton 2026-09-08).

Every harness instance carries all three levels from the start. L1 (trace) is
per run and lives in :mod:`lh_harness.experience.trace`. This module is the
seeded substrate for L2 and L3:

- **L3 (environmental cognition)** is genuinely populated below, as *data*:
  which hosts exist and what each is for, what a head on each can run, the
  standing constraints that already govern the fleet, and the routing
  backends by NAME with their ``max_concurrent`` (never URLs — spec
  ``tasks/msce-experience-layer-2026-09-07.md`` 2026-09-08 routing entry).
- **L2 (policy)** starts empty but is a real, addressable collection keyed by
  stable policy id, with the envelope defined by :data:`POLICY_ITEM_SCHEMA` —
  the same shape a learned policy will take once induction lands (explicitly
  out of Phase 1 scope), so no later change has to redesign it.

Every item carries ``origin`` (``"seeded"`` or ``"learned"``) and
``seeded_at``. A learned (or config-supplied) item *supersedes* a seeded one
without erasing the record of what was seeded: :func:`supersede` marks the
original in place and the replacement lives under its own stable id.

An instance may extend or supersede the built-in L3 seeds through an optional
``[experience]`` table in its own ``.lh-harness/config.toml`` (see
:func:`load_seeded_levels`). Overrides are additive and never touch L1 or the
run flow: the ``[experience]`` table is not a run default and is not applied
to ``HarnessConfig``.

Redaction is structural: every item — built-in or config-supplied — passes
through :func:`lh_harness.experience.redact.redact_value` before it leaves
this module, so hostnames and device ids survive and tokens or keys never do.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Mapping

from ..config import PROJECT_CONFIG_PATH, ProjectConfigError
from .. import config as _config
from .redact import redact_value

# Origin labels: what the instance was *told* vs what it worked out itself.
ORIGIN_SEEDED = "seeded"
ORIGIN_LEARNED = "learned"

# All built-in seeds share one seeded_at: the day the Phase-1 seed set was
# authored (the 2026-09-08 09:45 PT instruction). Precision is the day.
SEEDED_AT = "2026-09-08T00:00:00+00:00"

# Item summaries are one line, JSON-served; bound them like the other
# persistence caps in this package.
SUMMARY_MAX_CHARS = 512

# Kinds a built-in seed or an instance override may declare. "note" covers
# stable knowledge that is neither a host, a constraint, nor a backend.
ENVIRONMENT_KINDS = ("host", "constraint", "routing_backend", "note")

# L2's final shape, as data, so an empty collection still advertises what a
# learned policy will look like and a UI can render against it without
# reverse-engineering later code. device_requirement keeps the KIND of device
# a generalized routine needs ("a linux shell head on a fleet device with
# docker"), never a specific instance (Paxton 2026-09-08).
POLICY_ITEM_SCHEMA: dict[str, str] = {
    "id": "stable quotable id, by convention 'l2.<slug>'",
    "level": "'L2'",
    "kind": "'policy'",
    "origin": "'seeded' or 'learned'",
    "seeded_at": "ISO-8601 timestamp when the policy entered memory",
    "source": "'code' (built-in), 'config' (instance override) or the learner's name",
    "superseded_by": "id of the item replacing this one, or null",
    "superseded_at": "ISO-8601 timestamp of supersession, or null",
    "summary": "one-line statement of the generalized routine (<= 512 chars)",
    "detail.statement": "the generalized routine in full",
    "detail.device_requirement": (
        "{host_kind, shell, requires: [...]} — the kind of device the routine "
        "needs, never a specific instance id"
    ),
    "detail.evidence": "{run_ids: [...], task_context_ids: [...]}",
    "detail.support": "episode count the routine was induced from",
    "detail.value": "value V at induction time, or null",
}


# ---------------------------------------------------------------------------
# L3 seed data: the fleet as stable knowledge (facts from
# cognizioware-how-to.md and docs/handoffs/dynamic-model-routing-handoff-2026-09-08.md).
# Keep these honest: only what is documented, hostnames and ports are fine,
# tokens/keys/URLs on backend items are not.
# ---------------------------------------------------------------------------

_SEED_HOSTS: tuple[dict[str, Any], ...] = (
    {
        "id": "l3.host.ct110",
        "kind": "host",
        "summary": "Primary external lh-harness node (corsairai300 LXC).",
        "detail": {
            "hostname": "ct110",
            "placement": "LXC on corsairai300",
            "purpose": (
                "primary external harness node: runs the `lh-harness web` "
                "supervisor; registered in the Hydra orchestrator as the "
                "`external` primary + fallback harness node"
            ),
            "can_run": [
                "lh-harness web supervisor (systemd lh-harness.service, user harness)",
                "agent CLIs (npm claude under /home/harness) over worker child processes",
                "run workspaces under /home/harness/work (git-clone repos in)",
            ],
            "control_plane": "http://192.168.21.168:8799",
            "notes": [
                "redeploy as root from /home/harness/release-src; the venv at /home/harness/venv is root-owned — check the import path before any harness-dev run",
                "systemctl restart lh-harness kills every in-flight run; recover via POST /api/runs/<id>/resume",
                "bearer token fronted by Caddy at https://harness.lan.easybutt0n.ai",
            ],
        },
    },
    {
        "id": "l3.host.corsairai300",
        "kind": "host",
        "summary": "Hypervisor hosting CT110; home of the Hydra orchestrator and the ingest cron.",
        "detail": {
            "hostname": "corsairai300",
            "placement": "physical",
            "purpose": (
                "Proxmox host for CT110 and other CTs; hosts the Hydra "
                "orchestrator (/opt/cognizioware-hydra) and builds the "
                "lh-harness-node Docker image for managed devices"
            ),
            "can_run": [
                "hydra orchestrator + /fleet/* REST and /fleet-mcp MCP",
                "hivemind-ingest cron (hivemind-ingest.sh, every 5 min) carrying run snapshots to hivemind",
                "docker image builds for lh-harness-node (no source bind mount)",
            ],
            "notes": [
                "HARNESS_NODES_JSON in /opt/cognizioware-hydra/.env registers CT110 by direct IP",
            ],
        },
    },
    {
        "id": "l3.host.ct103",
        "kind": "host",
        "summary": "Hivemind (memory) host: postgres + pgvector agent-db.",
        "detail": {
            "hostname": "ct103",
            "placement": "LXC",
            "purpose": (
                "memory plane: hivemind_sessions.session_memories (pgvector); "
                "the hydra ingester writes lh-harness experience traces here "
                "via memory-mcp with no schema change"
            ),
            "can_run": [
                "memory-mcp remember/recall (gateway alias `memory`)",
            ],
            "notes": [
                "schema changes on CT103 are heavyweight — prefer metadata fields over new columns",
            ],
        },
    },
    {
        "id": "l3.host.ptait01",
        "kind": "host",
        "summary": "Local GPU box serving executor-class models (the local-span backend).",
        "detail": {
            "hostname": "ptait01",
            "placement": "physical",
            "purpose": (
                "local LLM capacity: 2x RTX 3090 behind the cognizioware-ollama-span "
                "container; also runs prisma query-engine, postgres-mcp and speaches"
            ),
            "can_run": [
                "ollama spans for local models (qwen3.8:27b, qwen3.8-nothink, qwen3.6 family)",
            ],
            "notes": [
                "OLLAMA_NUM_PARALLEL=2 — local parallelism is 2 (routing handoff 2026-09-08)",
            ],
        },
    },
    {
        "id": "l3.host.ptait09",
        "kind": "host",
        "summary": "Dev box: WSL harness deployment (pip -e); Fleet Window development seat.",
        "detail": {
            "hostname": "ptait09",
            "placement": "physical, WSL",
            "purpose": (
                "development deployment of lh-harness (editable install, "
                "127.0.0.1:8799, no token) and the fleet-admin/Fleet Window "
                "source-of-truth machine"
            ),
            "can_run": [
                "dev harness runs against /mnt/c workspaces (DrvFS — unstable stat cache, see gotchas)",
                "fleet-admin vendored builds deployed by the cognizioware-mcp-tools lane to CT202",
            ],
            "notes": [
                "dev box — the e2e/QA constraint below applies",
                "never run task execution on native Windows: LocalEnvironment uses os.killpg/SIGHUP (POSIX only)",
            ],
        },
    },
    {
        "id": "l3.host.ct202",
        "kind": "host",
        "summary": "MCP tools host: LiteLLM router's neighbours, fleet-admin, env/vault material.",
        "detail": {
            "hostname": "ct202",
            "placement": "LXC",
            "purpose": (
                "runs cognizioware-mcp-tools (knowledge-base/gateway/backbone MCPs), "
                "hosts fleet-admin, and stores pool material such as the extra "
                "Ollama Cloud key in mcp-tools.env"
            ),
            "can_run": [
                "MCP tool servers accessed via gateway aliases",
            ],
            "notes": [
                "key health probes before kimi launches read its key-health endpoint",
            ],
        },
    },
    {
        "id": "l3.host.ptait-desk03",
        "kind": "host",
        "summary": "Fleet device: Paxton's desk box (Hydra-managed, GUI-capable).",
        "detail": {
            "hostname": "PTAIT-DESK03",
            "placement": "physical, fleet device",
            "purpose": (
                "device-fleet machine: runs the Hydra device agent; can host "
                "lh-harness-node in Docker Desktop/WSL2 and expose terminals, "
                "shells and GUI heads for remote execution"
            ),
            "can_run": [
                "powershell/cmd/bash via its authenticated runner or Hydra",
                "host-local agent CLIs (claude code, cursor, aider, opencode)",
                "GUI heads (screenshots, focus adjustments) via computer-use",
            ],
            "notes": [
                "a desk box is a dev-proximity surface — check the e2e/QA constraint before scheduling suites on it",
            ],
        },
    },
    {
        "id": "l3.host.htpc01",
        "kind": "host",
        "summary": "Fleet device: Hydra-managed living-room box (GUI-capable).",
        "detail": {
            "hostname": "HTPC01",
            "placement": "physical, fleet device",
            "purpose": (
                "device-fleet machine reachable through its authenticated "
                "runner or the Hydra control plane; supports terminal heads "
                "and GUI automation"
            ),
            "can_run": [
                "powershell/cmd/bash via runner or Hydra",
                "GUI heads via computer-use",
            ],
            "notes": [],
        },
    },
    {
        "id": "l3.host.ptait10am5",
        "kind": "host",
        "summary": "Fleet device: Hydra-managed box (GUI-capable).",
        "detail": {
            "hostname": "PTAIT10AM5",
            "placement": "physical, fleet device",
            "purpose": (
                "device-fleet machine reachable through its authenticated "
                "runner or the Hydra control plane; supports terminal heads "
                "and GUI automation"
            ),
            "can_run": [
                "powershell/cmd/bash via runner or Hydra",
                "GUI heads via computer-use",
            ],
            "notes": [],
        },
    },
)

_SEED_CONSTRAINTS: tuple[dict[str, Any], ...] = (
    {
        "id": "l3.constraint.host-change-explicit-go",
        "kind": "constraint",
        "summary": "No host-level change on any fleet machine without an explicit go from the operator.",
        "detail": {
            "statement": (
                "Installs, service edits, firewall or OS changes on any fleet "
                "host require an explicit go from Paxton first; an agent must "
                "stop and ask rather than proceed."
            ),
            "applies_to": ["all hosts"],
        },
    },
    {
        "id": "l3.constraint.runner-restart-from-outside",
        "kind": "constraint",
        "summary": "A runner/supervisor restart is issued from OUTSIDE the runner, never from inside a run it owns.",
        "detail": {
            "statement": (
                "Restarting a supervisor (or systemctl restart lh-harness on "
                "CT110) kills every in-flight run it owns. Restarts are issued "
                "from outside the runner, deliberately, never casually; runs "
                "recover via POST /api/runs/<id>/resume {mode: continue}."
            ),
            "applies_to": ["ct110", "all harness nodes"],
        },
    },
    {
        "id": "l3.constraint.no-e2e-qa-on-dev-box",
        "kind": "constraint",
        "summary": "Never run e2e or QA suites on a dev box.",
        "detail": {
            "statement": (
                "End-to-end and QA suites run on dedicated infra, not on a "
                "development workstation; a dev box holds uncommitted work and "
                "live sessions a suite can destroy."
            ),
            "applies_to": ["dev boxes (e.g. ptait09, desk03)"],
        },
    },
    {
        "id": "l3.constraint.prod-via-lanes",
        "kind": "constraint",
        "summary": "Production deploys go through the lanes, and the gates decide.",
        "detail": {
            "statement": (
                "Prod changes are deployed by the repo's lane pipeline; the "
                "lane's gates (health probes, checks) decide whether the "
                "deploy proceeds. No manual shortcuts onto production."
            ),
            "applies_to": ["all production services"],
        },
    },
    {
        "id": "l3.constraint.some-hosts-no-ipv6-egress",
        "kind": "constraint",
        "summary": "Some hosts have no IPv6 egress; never assume v6 reachability.",
        "detail": {
            "statement": (
                "Parts of the fleet have no IPv6 route to the internet. "
                "Anything that must reach an external service should not "
                "depend on IPv6 alone; test over v4 or fail over cleanly."
            ),
            "applies_to": ["fleet hosts where v6 egress is absent"],
        },
    },
    {
        "id": "l3.constraint.stale-image-tag-rollback",
        "kind": "constraint",
        "summary": "A lane compose resolving a stale image tag silently rolls a service back.",
        "detail": {
            "statement": (
                "When a lane's compose resolves an old/stale image tag, "
                "compose up silently reverts the service to that older image. "
                "Always confirm which tag a lane resolved before the up."
            ),
            "applies_to": ["all lane-deployed services"],
        },
    },
)

_SEED_BACKENDS: tuple[dict[str, Any], ...] = (
    {
        "id": "l3.backend.local-span",
        "kind": "routing_backend",
        "summary": "Local 2x RTX 3090 ollama span; executor-class local models; 2 concurrent.",
        "detail": {
            "name": "local-span",
            "max_concurrent": 2,
            "locality": "local",
            "notes": [
                "OLLAMA_NUM_PARALLEL=2 on the span container — local parallelism is 2",
                "serves the qwen3.8/qwen3.6 local model family used as executors",
            ],
        },
    },
    {
        "id": "l3.backend.synthetic",
        "kind": "routing_backend",
        "summary": "Synthetic subscription packs; 2 concurrent per model per pack.",
        "detail": {
            "name": "synthetic",
            "max_concurrent": 2,
            "locality": "cloud",
            "notes": [
                "subscription packs of 1000 requests / 5 hours, 2 concurrent per model per pack; two packs held",
                "registered model family: glm-5.3-flash, glm-5.2, kimi-k3, qwen3.8-27b (:synthetic)",
                "its /models endpoint lists what is servable; availability must be proven by a cheap completion",
            ],
        },
    },
    {
        "id": "l3.backend.ollama-cloud",
        "kind": "routing_backend",
        "summary": "Ollama Cloud accounts behind the router; no fixed concurrency cap — rate and monthly caps govern.",
        "detail": {
            "name": "ollama-cloud",
            # Documented behavior: failures are per-key hourly rate limits vs
            # monthly account caps; no fixed max_concurrent is documented, so
            # this stays null rather than inventing a number.
            "max_concurrent": None,
            "locality": "cloud",
            "notes": [
                "three Pro accounts shuffled by the router (OLLAMA_CLOUD_KEY_1..3, KEY_1 weighted 3x)",
                "per-key rate limits clear within the hour; a monthly account cap does not clear until the month rolls",
                "pool model groups (:pool) refuse together when all accounts hit the monthly cap",
            ],
        },
    },
)

_SEED_ENVIRONMENT: tuple[dict[str, Any], ...] = (
    _SEED_HOSTS + _SEED_CONSTRAINTS + _SEED_BACKENDS
)


@dataclass(frozen=True)
class SeededLevels:
    """The seeded (and progressively learned) non-per-run levels.

    ``environment`` is the ordered L3 item list (each item addressable by its
    stable ``id``); ``policies`` is the L2 collection keyed by stable policy
    id — empty today, addressable from day one.
    """

    environment: tuple[dict[str, Any], ...]
    policies: dict[str, dict[str, Any]] = field(default_factory=dict)


def empty_policy_collection() -> dict[str, dict[str, Any]]:
    """L2 as it ships: an empty, addressable collection keyed by policy id.

    The shape a learned policy will take is declared in
    :data:`POLICY_ITEM_SCHEMA` so this is the collection's final form, not a
    placeholder a later change must redesign.
    """
    return {}


def supersede(item: Mapping[str, Any], *, replacement_id: str, at: str) -> dict[str, Any]:
    """Return a copy of ``item`` marked superseded by ``replacement_id``.

    The original record is never deleted — what was seeded (or learned) stays
    visible behind the replacement (Paxton 2026-09-08). ``at`` is an ISO-8601
    timestamp string.
    """
    item_id = str(item.get("id", "")).strip()
    if not item_id:
        raise ValueError("supersede: item has no id")
    replacement = str(replacement_id).strip()
    if not replacement:
        raise ValueError("supersede: replacement_id must be non-empty")
    if replacement == item_id:
        raise ValueError("supersede: an item cannot supersede itself")
    stamped = str(at).strip()
    if not stamped:
        raise ValueError("supersede: at must be an ISO-8601 timestamp")
    marked = dict(item)
    marked["superseded_by"] = replacement
    marked["superseded_at"] = stamped
    return marked


def load_seeded_levels(config_path: str | Path | None = None) -> SeededLevels:
    """Load the seeded L2/L3 levels for one instance.

    Always starts from the built-in seeds, then applies the instance's
    optional ``[experience]`` table from ``.lh-harness/config.toml``:

    - ``[[experience.environment]]`` entries append new L3 items; each takes
      ``id``, ``kind`` (one of :data:`ENVIRONMENT_KINDS`), ``summary``, an
      optional ``detail`` table, and an optional ``supersedes`` naming a seed
      it replaces — the seed is kept, marked ``superseded_by``.
    - Override items keep ``origin = "seeded"`` (the instance was *told*,
      it did not learn) with ``source = "config"`` and ``seeded_at`` taken
      from the config file's mtime — when the instance last recorded it.

    L2 is not seeded by configuration: it starts empty per the instruction.
    A missing file yields the pure built-in seeds. A malformed table raises
    ``ProjectConfigError``, the same error family as every other bad
    ``config.toml``.
    """
    path = Path(config_path) if config_path is not None else PROJECT_CONFIG_PATH
    environment: list[dict[str, Any]] = [
        _finalize(raw, seeded_at=SEEDED_AT, source="code") for raw in copy.deepcopy(list(_SEED_ENVIRONMENT))
    ]
    payload = _read_toml_if_present(path)
    if payload is not None:
        experience = payload.get("experience")
        if experience is not None:
            environment = _apply_experience_overrides(environment, experience, path)
    return SeededLevels(environment=tuple(environment), policies=empty_policy_collection())


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _finalize(raw: dict[str, Any], *, seeded_at: str, source: str) -> dict[str, Any]:
    """Wrap a raw seed/overrides dict in the item envelope and redact it.

    Redaction here is the choke point: every item served from this module has
    already passed through ``redact_value``, so a value redaction would strip
    from a trace can never leave this module.
    """
    summary = str(raw.get("summary", "")).strip()
    item = {
        "id": str(raw["id"]).strip(),
        "level": "L3",
        "kind": str(raw["kind"]).strip(),
        "origin": ORIGIN_SEEDED,
        "seeded_at": seeded_at,
        "source": source,
        "superseded_by": raw.get("superseded_by"),
        "superseded_at": raw.get("superseded_at"),
        "summary": summary[:SUMMARY_MAX_CHARS],
        "detail": raw.get("detail", {}),
    }
    return redact_value(item)


def _read_toml_if_present(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        with path.open("rb") as fh:
            payload = _config.tomllib.load(fh)
    except (OSError, _config.tomllib.TOMLDecodeError) as exc:
        raise ProjectConfigError(f"could not read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProjectConfigError(f"{path} must contain a TOML table")
    return payload


def _jsonable(value: Any, name: str) -> Any:
    """Normalize a TOML value to JSON-serialisable equivalents.

    tomllib can produce date/time objects; everything served from this module
    must JSON-round-trip the way a trace does.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item, name) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item, name) for item in value]
    raise ProjectConfigError(f"{name}: unsupported value type {type(value).__name__}")


def _apply_experience_overrides(
    environment: list[dict[str, Any]],
    experience: Any,
    path: Path,
) -> list[dict[str, Any]]:
    """Apply ``[experience]`` from an instance config to the seeded levels."""
    name_prefix = f"[experience] in {path}"
    if not isinstance(experience, Mapping):
        raise ProjectConfigError(f"{name_prefix} must be a TOML table")
    unknown = set(experience) - {"environment"}
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise ProjectConfigError(f"{name_prefix}: unknown key(s): {names}")
    entries = experience.get("environment", [])
    if not isinstance(entries, list):
        raise ProjectConfigError(
            f"{name_prefix}.environment must be an array of tables ([[experience.environment]])"
        )
    if not entries:
        return environment

    seeded_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    by_id = {item["id"]: index for index, item in enumerate(environment)}
    result = list(environment)
    appended: dict[str, int] = {}

    for position, entry in enumerate(entries):
        entry_name = f"experience.environment[{position}]"
        if not isinstance(entry, Mapping):
            raise ProjectConfigError(f"{entry_name} must be a TOML table")
        unknown_keys = set(entry) - {"id", "kind", "summary", "detail", "supersedes"}
        if unknown_keys:
            names = ", ".join(sorted(str(key) for key in unknown_keys))
            raise ProjectConfigError(f"{entry_name}: unknown key(s): {names}")
        item_id = str(entry.get("id", "")).strip()
        if not item_id:
            raise ProjectConfigError(f"{entry_name}.id is required")
        kind = str(entry.get("kind", "")).strip()
        if kind not in ENVIRONMENT_KINDS:
            raise ProjectConfigError(
                f"{entry_name}.kind must be one of: {', '.join(ENVIRONMENT_KINDS)}"
            )
        summary = entry.get("summary", "")
        if not isinstance(summary, str) or not summary.strip():
            raise ProjectConfigError(f"{entry_name}.summary must be a non-empty string")
        if item_id in by_id:
            raise ProjectConfigError(
                f"{entry_name}.id {item_id!r} collides with an existing item; "
                "give it its own id and set supersedes to replace the seeded one"
            )
        raw: dict[str, Any] = {
            "id": item_id,
            "kind": kind,
            "summary": summary,
            "detail": _jsonable(entry.get("detail", {}), f"{entry_name}.detail"),
        }
        item = _finalize(raw, seeded_at=seeded_at, source="config")

        supersedes = str(entry.get("supersedes", "") or "").strip()
        if supersedes:
            target = by_id.get(supersedes)
            if target is None:
                raise ProjectConfigError(
                    f"{entry_name}.supersedes {supersedes!r} does not match any existing item"
                )
            result[target] = supersede(
                result[target], replacement_id=item_id, at=seeded_at
            )
        by_id[item_id] = len(result)
        appended[item_id] = len(result)
        result.append(item)
    return result
