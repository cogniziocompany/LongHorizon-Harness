# Changelog

## 0.1.8 (Unreleased)

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

## 0.1.7 · 2026-08-20

- A finished run is no longer a dead end: the workbench is now a conversation.
  Read the reply, type a follow-up, and the run continues on its own round
  ledger instead of replanning from scratch.
- `--reasoning-effort` for every role, with per-role overrides.
- The transcript now reads in strict chronological order.
- Graceful stop escalates to force stop only when a worker ignores it.

## 0.1.6 · 2026-08-15

- Added [OpenCode](https://github.com/anomalyco/opencode) CLI support.

## 0.1.5 · 2026-08-14

- Added phase-1 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
  CLI support.

## 0.1.4 · 2026-08-11

- The new Dashboard has landed: a React/FastAPI workbench launched with
  `lh-harness web`.

## 0.1.3 · 2026-08-07

- Every run now ends with a plain-language reply from the verified state.
- Tasks act on the launch directory by default.
