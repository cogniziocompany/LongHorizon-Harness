# Fleet Reporter Payload Bound — Verification Plan

How to verify, after deploying to CT110, that the bounded fleet heartbeat is
accepted by the fleet plane and that CT110 is registered and visible again.

## What changed (summary)

- `queue_heartbeat` (src/lh_harness/fleet/reporter.py) no longer sends the
  full run list.  `runs[]` carries only non-terminal runs, hard-capped to
  the 200 most recent; the whole store is summarized in `runsTotal` /
  `runsByStatus` / `runsTruncated`.  Field names `{node, runs[], capacity,
  queueLen}` and gzip + HMAC behavior are unchanged.
- Reproduction baseline (audited, 2026-09-22): 532 runs -> 5,469,390 B JSON /
  1,698,083 B gzipped -> HTTP 413 from the fleet plane.  With the bound the
  same store serializes to ~21 KB JSON / ~510 B gzipped (2 active runs).

## Pre-deployment Checks

1. Verify the bound is present in the deployed code:
   - `src/lh_harness/fleet/reporter.py` defines
     `_MAX_ACTIVE_RUNS_PER_HEARTBEAT = 200`.
   - `queue_heartbeat` filters to non-terminal runs and emits
     `runsTotal` / `runsByStatus` / `runsTruncated`.

## Post-deployment Read-Back (for the overseer)

The fleet plane (fleet.easybutt0n.ai) is outside this workspace; run these
checks from an operator context that may reach it.

### 1. Reporter health on CT110

```bash
systemctl status lh-harness
journalctl -u lh-harness -n 200 | grep -i "fleet"
```

Expected: no `fleet reporter rejected /harness/heartbeat` lines after
restart; no repeated `HTTP 413` errors.

### 2. /api/meta read-back (the primary acceptance gate)

```bash
curl -s http://127.0.0.1:8799/api/meta | python3 -m json.tool | grep fleet
```

Expected after the first heartbeat interval (~30 s):

- `fleet_configured`: `true`
- `fleet_ever_succeeded`: **`true`** — must flip to true, this is the gate.
- `fleet_last_ok`: `true`
- `fleet_last_error`: `null` (the previous value was `"HTTP 413"`; it must be
  cleared)

### 3. /api/fleet/nodes read-back

```bash
curl -s https://fleet.easybutt0n.ai/api/fleet/nodes
```

Expected: the response lists **ct110** (previously `[]`) with a fresh
`last_heartbeat` and `capacity`.

### 4. Payload-size confirmation (optional)

- `journalctl -u lh-harness | grep "capping active runs"` — INFO lines only
  appear when more than 200 runs are simultaneously non-terminal; absence is
  normal.
- Aggregate counts (`runsTotal` etc.) can be confirmed in the fleet DB's
  `fleet.harness_nodes` row or via the socket.io `node.heartbeat` deltas.

## Success Criteria

1. `/api/meta` reads `fleet_ever_succeeded: true`, `fleet_last_ok: true`,
   `fleet_last_error` cleared.
2. `/api/fleet/nodes` lists ct110.
3. No 413s in the journal after the deploy; heartbeats land every ~30 s.
4. `runsTotal`/`runsByStatus` still reflect the full 532-run store even
   though `runs[]` is bounded.
5. `pytest tests/fleet` passes, including the bound tests
   (`test_heartbeat_excludes_terminal_runs_and_reports_counts`,
   `test_heartbeat_payload_far_below_failure_envelope`,
   `test_heartbeat_caps_active_runs_with_truncation_flag`,
   `test_heartbeat_counts_unknown_status_as_non_terminal`).

## Troubleshooting

If 413s persist after deploy:
1. Confirm the deployed tree actually contains the bound (check
   `_MAX_ACTIVE_RUNS_PER_HEARTBEAT` in the running code).
2. Confirm the reporter process was restarted after deploy.
3. Escalate to the fleet-plane maintainers: the heartbeat route's exact
   body-size limit is still unknown (the 10 MB `express.json` limit
   documented in `tasks/harness-fleet-window-2026-09-07.md:67` applies to
   `/harness/rounds` only).  Even a 1 MiB limit would accept the bounded
   payload many times over; a persistent 413 with this shape implies a limit
   below ~21 KiB, which would be a fleet-plane regression worth its own
   investigation.

## Open items (overseer)

- Exact heartbeat-route limit and the captured 413 response body from the
  fleet plane (cognizioware-hydra / Caddy) — out of this workspace.
- Rotation of the leaked 48-hex token formerly embedded in the deleted
  `/tmp/measure_real_heartbeat*.py` scripts (scripts removed locally;
  rotation is an operator action).

## References

- Code: `src/lh_harness/fleet/reporter.py` (`queue_heartbeat`,
  `_MAX_ACTIVE_RUNS_PER_HEARTBEAT`)
- Tests: `tests/fleet/test_reporter.py` (heartbeat bound tests)
- Docs: `docs/fleet-reporting.md` (bounded payload shape)
- Incident notes: `tasks/harness-fleet-window-2026-09-07.md`