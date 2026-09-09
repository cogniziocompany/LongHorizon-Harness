# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Experience layer (MSCE Phase 1).** Optional valued L1 trace persistence:
  one redacted JSONL record per managed round written to
  `role_orchestration/experience.jsonl` in the run dir at finalization,
  carrying a deterministic terminal reward split into goal/process/satisfaction
  terms (R = 0.45·goal + 0.30·process + 0.25·satisfaction), a per-round
  reflection weight from the independent audit, and Eq. 2 value backfill
  (γ = 0.9).  Off by default (`LH_HARNESS_EXPERIENCE=1` or
  `[run] experience = true`); when off, runs are byte-identical to previous
  builds.  New package `src/lh_harness/experience/`; documentation in
  [docs/experience-layer.md](docs/experience-layer.md).
- **Experience read-only API.** The three MSCE levels are viewable by the
  fleet surfaces through `GET /api/experience/environment` (L3 — the seeded
  fleet knowledge: hosts, standing constraints, routing backends, with
  `origin`/`seeded_at`/`source`/`superseded_*` per item),
  `GET /api/experience/policies` (L2 — empty but addressable, paginated,
  with the final item schema declared), and
  `GET /api/experience/runs/<run_id>/traces` (L1 — paginated, redacted
  again at serve time, read from the run-dir ledger through the same
  run-boundary checks as every run-scoped route).  Same bearer middleware as
  the rest of `/api/*`; read-only, no write endpoints.
- **Fleet reporting.** Nodes can now stream live telemetry to
  `fleet.easybutt0n.ai` over a single outbound HTTPS path, so NAT'd hosts are
  visible without any inbound firewall rule.  Enabled only when
  `LH_HARNESS_FLEET_URL` is set; when unset the web server and CLI behave
  exactly as before.
  - New reporter at `src/lh_harness/fleet/reporter.py`: one daemon thread,
    bounded queue, 2 s event batching, gzip, HMAC-SHA256 over the raw body with
    `X-Fleet-Host` + `X-Fleet-Signature`, bounded retry then drop, and WARN-once
    persistent-failure behavior.
  - Event hooks feed a public `EventEnvelope` (no transcripts or thinking).
  - Heartbeat every 30 s carries node identity, version, supervised run
    summaries, and capacity.
  - Round content push uploads every round artifact and role trajectory
    (thinking included as stored) capped at 8 MB per round; larger files are
    represented by `{truncated: true, bytes: N}`.
  - `POST /api/runs` accepts an optional `youtrack_issue_id` that is stored in
    control metadata and included in summaries and heartbeats.
  - Docker node deployment passes through `LH_HARNESS_FLEET_URL`,
    `LH_HARNESS_FLEET_NODE`, `LH_HARNESS_FLEET_KEY`, and `LH_HARNESS_FLEET_LABELS`.
  - Documentation: [docs/fleet-reporting.md](docs/fleet-reporting.md) and README
    env table.
- Service-side task queue with durable atomic JSON persistence under the runs root.
  - `POST /api/queue`, `GET /api/queue`, `DELETE /api/queue/{id}`, and priority endpoints.
  - Capacity-gated launcher with trio limits, workspace collision avoidance, and idempotent restarts.
  - Fleet MCP tools exposed under the LiteLLM alias `lhharness` (access group `fleet-runners`).
  - ASCII-only `harness_resolve_gate` validation and `/api/runs/{id}/approvals/{id}/resolve` wiring.
- End-to-end happy-path test for the local queue API.
  - `e2e/happy_path.sh` and `e2e/happy_path.py` start a local service, use a fake `codex` agent, and assert the run reaches `completed`.
  - Covers `queue.launched`, the inter-tick double-launch race, and `base_check` wiring.
- `Makefile` target `e2e-happy` and pytest marker `e2e` so heavy stack tests run separately from the main suite.
- Server-side caching for `/api/runs/{id}/snapshot`, cutting full snapshot p95 latency from ~37 ms to ~5 ms and summary snapshot p95 latency from ~37 ms to ~4 ms on a 16-round synthetic run.
- Documentation: `docs/queue.md`, `docs/ux-performance.md`, and `docs/release-checklist-ct110.md`.

### Changed

- Web API now embeds a service-side supervisor and launcher when run with `lh-harness web`.

### Fixed

- Race where two queue entries for the same workspace could be launched across concurrent ticks is now prevented by a launch lock and active-run re-check.
