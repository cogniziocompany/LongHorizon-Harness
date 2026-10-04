# Fleet reporting

LongHorizon-Harness can push live telemetry from every node to the shared fleet
admin service at `fleet.easybutt0n.ai`.  This lets NAT'd or headless nodes report
their state over a single outbound HTTPS path, without any inbound firewall rule
or VPN.  Reporting is **opt-in and off by default**; it activates only when
`LH_HARNESS_FLEET_URL` is set.

## Enabling fleet reporting

Set these environment variables before starting the web workbench (`lh-harness
web`) or a supervised node (`docker/compose.node.yml`):

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `LH_HARNESS_FLEET_URL` | yes | *(none)* | Origin of the fleet-admin ingest service, e.g. `https://fleet.easybutt0n.ai`. |
| `LH_HARNESS_FLEET_KEY` | yes | *(none)* | Per-host read key used to HMAC-sign every POST body. |
| `LH_HARNESS_FLEET_NODE` | no | `socket.gethostname()` | Unique node identity reported as `X-Fleet-Host`. |
| `LH_HARNESS_FLEET_LABELS` | no | *(none)* | Comma-separated `key=value` labels such as `kind=ct110,repo=LongHorizon-Harness`. |
| `LH_HARNESS_FLEET_UI_BASE_URL` | no | *(none)* | Public base URL of this node's own Web console, reported as `node.uiBaseUrl` in the heartbeat so fleet-admin can deep-link to the node's dashboard. The trailing slash is stripped; unset reports `""`. Environment-only — the config loader has no `[fleet]` table. |

When `LH_HARNESS_FLEET_URL` is unset, the reporter is not created and the system
behaves exactly as before.

## What is pushed

All traffic is sent by a single daemon thread with a bounded queue.  Bodies are
gzip-compressed and signed with HMAC-SHA256 over the raw (compressed) bytes,
using the same headers that the device `/checkin` endpoint uses:

- `X-Fleet-Host`: node identity.
- `X-Fleet-Signature`: hex-encoded HMAC-SHA256 of the raw body.
- `X-Fleet-Body-Original-Length`: uncompressed JSON length.
- `Content-Encoding: gzip`

### 1. Events

Manager events from `logs/role_orchestration/events.jsonl` are converted to a
public `EventEnvelope` containing `run_id`, `type`, `ts`, `round`, `role`,
`status`, and a trimmed `payload`.  Transcripts, trajectories, thinking blocks,
prompts, and other large nested objects are stripped before transmission.  The
reporter batches events for two seconds and POSTs them to
`{LH_HARNESS_FLEET_URL}/harness/events`.

Gate state changes (`approval_created`, `approval_resolved`), supervisor
`run.status` updates, and the launcher's queue ledger events —
`queue.launched`, `queue.done`, `queue.failed` (per-run ledgers) and
`queue.requeued`, `queue.skipped` (service ledger) — are also emitted as
events.  The local JSONL ledgers remain the durable record; the fleet push is
a fail-open side-car that never alters launcher behaviour when fleet is
unconfigured.

#### Episode stats

Every role done/failed event (`manager_round_done`, `executor_role_done`,
`auditor_role_done`, `agent_runtime_failed`, ...) carries
`payload.episode_status.stats`, a set of counters computed from the episode's
Claude Code stream-json log by `src/lh_harness/episode_stats.py`:

| Field | Meaning |
| --- | --- |
| `model` | Model that actually served the episode (e.g. `ornith-1.5:9b-256k`). |
| `turns` | Model turns (distinct assistant message ids). |
| `tool_calls`, `tool_errors` | Tool calls made, and tool results flagged as errors. |
| `edits`, `files_touched` | `Edit`/`Write`/`NotebookEdit` calls, and distinct files they named. |
| `tool_result_chars` | Characters of tool output fed back to the model. |
| `thinking_tokens`, `max_turn_thinking_tokens` | Estimated thinking tokens: episode total and largest single turn. |
| `api_retries`, `compactions` | Claude Code API retries and context compactions. |
| `max_gap_seconds` | Longest wait between a tool result and the next model turn. |
| `stalls`, `stall_seconds` | Waits of 120 s or more, and their total. |

`stats` is `null` for agents that do not emit stream-json.  The counters hold
no prompt, thinking or tool content.  They exist so sizing questions ("how many
turns and files does an episode that finishes on the local lane have?") and
stall questions ("how much executor time is spent waiting on a busy lane?")
can be answered from `fleet.harness_events` instead of from run directories.

### 2. Heartbeats

Every 30 seconds the reporter POSTs one heartbeat to
`{LH_HARNESS_FLEET_URL}/harness/heartbeat`:

```json
{
  "node": {
    "name": "ct110-lab",
    "version": "0.1.7",
    "kind": "ct110",
    "labels": {"kind": "ct110", "repo": "LongHorizon-Harness"},
    "uiBaseUrl": "https://ct110-lab.example.ai"
  },
  "runs": [
    {
      "runId": "20250907T120000Z_abcd1234",
      "run_id": "20250907T120000Z_abcd1234",
      "status": "running",
      "round": 3,
      "activeRole": "executor",
      "model": "kimi-k2.7-code:cloud",
      "repo": "LongHorizon-Harness",
      "workspace": "/home/harness/work/LongHorizon-Harness",
      "youtrackIssueId": "MCP-123",
      "lastEvent": {"id": "20250907T120000Z_abcd1234:000042", "type": "round.executor.started", "ts": 1757170800.0},
      "lastEventAgeSeconds": 17.5,
      "summary": {}
    }
  ],
  "capacity": {"active": 1, "cap": 1},
  "queueLen": 0,
  "runsTotal": 532,
  "runsByStatus": {"running": 1, "completed": 530, "failed": 1},
  "runsTruncated": false
}
```

**Run identity from the event tail**: run summaries carry no active-round or
active-role fields of their own, so `round`, `activeRole`, and `lastEvent`
(`{id, type, ts}` plus `lastEventAgeSeconds`) are derived from the run's last
durable event, read with the same bounded seek-from-end
`EventTailer.read_last` tail read that the `/api/runs/{id}/latest` liveness
route uses (a 64 KiB tail chunk in the common case, geometric growth only for
oversized final records).  The read happens at most once per serialized run
row and the projection is a fixed small shape regardless of ledger size.
When a run has no readable event ledger all four fields are `null`.
`node.uiBaseUrl` comes from `LH_HARNESS_FLEET_UI_BASE_URL` (trailing slash
stripped, `""` when unset).

**Payload bounding**: A long-lived node accumulates hundreds of completed
runs whose per-run summaries dominate the heartbeat (measured on CT110,
2026-09-22: ~10 KB per run; a 532-run store produced a 5.47 MB JSON body /
1.62 MB gzipped that the fleet plane rejected with HTTP 413).  The heartbeat
therefore never carries the full run list:

- `runs[]` contains only **non-terminal** runs (the ones a remote operator
  can act on; terminal = `completed`, `failed`, `cancelled`, `blocked`,
  `incomplete` per `supervisor.lifecycle.TERMINAL_STATUSES`).
- `runsTotal` and `runsByStatus` aggregate over the **entire** run store, so
  the fleet still sees the full picture.  Unknown/blank statuses
  canonicalize (via `canonical_lifecycle_status`) to `idle` or the raw
  value, are counted, and are treated as non-terminal — never silently
  dropped.
- `runs[]` itself is hard-capped to the **200 most recent active runs** (by
  `updated_at`, falling back to the registry `mtime`).  Live runs are
  bounded by node capacity in practice; the cap only guards a pathological
  store.  When the cap engages, `runsTruncated` is `true` and an INFO log
  line is emitted:

  ```
  fleet reporter heartbeat: capping active runs from X to 200 most recent; aggregate counts still cover every run
  ```

With this shape a 532-run store serializes to ~21 KB JSON / ~510 B gzipped —
roughly 0.03% of the rejected body.  The exact request-size limit for the
`/harness/heartbeat` route on the fleet plane (cognizioware-hydra fleet API
behind Caddy) is not known to this repo; per
`tasks/harness-fleet-window-2026-09-07.md:67` the **10 MB** `express.json`
limit belongs to the `/harness/rounds` route only.  The bounding here is
designed to stay far below any plausible limit; the heartbeat route's limit
is tracked as an open question with the fleet-plane maintainers.

### 3. Round content

After each round is recorded, the reporter reads the validated round directory
`logs/role_orchestration/rounds/round_NNN/` and POSTs its contents to
`{LH_HARNESS_FLEET_URL}/harness/rounds`:

- every artifact file the dashboard exposes via `/api/runs/{id}/rounds/{n}/artifacts/{name}`;
- every role trajectory file exposed via `/api/runs/{id}/rounds/{n}/trajectory/{role}`;
- a run-level `logs/report.json` push after the final durable write.

Round payloads are capped at **8 MB per round**.  Larger files are never dropped
silently: they are replaced with an explicit marker `{truncated: true, bytes: N}`
that records the original size.

### 4. YouTrack issue id

`POST /api/runs` accepts an optional `youtrack_issue_id` string.  It is stored in
`control/owner.json` and `control/status.json` metadata and included in run
summaries and heartbeats.

## Privacy and access control

Role trajectories include the agent's raw thinking blocks and intermediate
outputs exactly as stored on disk.  The fleet read key therefore gates access to
round content just as `LH_HARNESS_WEB_TOKEN` gates the local dashboard.  If you
do not want thinking content to leave the node, leave `LH_HARNESS_FLEET_URL`
unset.

Event payloads are intentionally trimmed to summary fields only; they never
contain transcripts or thinking.

## Fleet channel (fc-H3): fleet-admin reaches the node

fleet-admin cannot open connections to harness nodes, so the web service
dials out instead. It opens one TLS WebSocket to
`wss://<host of LH_HARNESS_FLEET_URL>/harness/channel` and serves allow-listed
API calls over it. The server side is fleet-admin's `src/channel.js` (task
fc-F1). The client is `src/lh_harness/fleet/channel.py`.

- **Off by default.** It starts only in the web service (`lh-harness web`),
  only when `LH_HARNESS_FLEET_URL` and `LH_HARNESS_FLEET_KEY` are set, and only
  when `LH_HARNESS_FLEET_CHANNEL` is not `0`. The node name is the reporter's
  (`LH_HARNESS_FLEET_NODE`, else the hostname).
- **Handshake:** the headers are `X-Fleet-Host`, `X-Fleet-Ts` and
  `X-Fleet-Signature` (hex HMAC-SHA256 of the device key over
  `"<host>.<ts>"`). Every connect signs a fresh timestamp. fleet-admin allows
  60 s of clock skew, so keep the node clock in NTP sync.
- **Served calls:** GET `/api/meta`, `/api/queue`, `/api/runs`,
  `/api/runs/latest`, `/api/runs/{id}/{snapshot,events,status,latest}`; POST
  `/api/runs/{id}/{stop,resume,abort,instructions,time_limit}`. Anything else
  answers 403 `{"error":"not allowed"}`. Calls go to the service's own
  loopback address with its bearer (`LH_HARNESS_WEB_TOKEN`). The request
  timeout is 30 s (504) and the response cap is 4 MB (413).
- **Caller scoping:** when a `[callers]` table is configured, the run-control
  POSTs also need a caller identity. Set `LH_HARNESS_FLEET_CHANNEL_CALLER` to
  the `[callers]` entry the channel signs as. That entry needs
  `rest_run_control = true`, and its secret comes from its own `secret_env`.
  Without this, proxied POSTs answer 401 or 403 while GETs still work.
- **Keep-alive and reconnect:** the node answers `{"kind":"ping"}` with a
  pong and sends its own ping every 20 s. After 60 s without any frame it
  reconnects. Reconnects back off exponentially from 1 s to 60 s, with jitter.
- **Visibility:** `/api/meta` carries `fleet_channel_connected`,
  `fleet_channel_since` (ISO time of the last connect or disconnect) and
  `fleet_channel_last_error`.
