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

Gate state changes (`approval_created`, `approval_resolved`) and supervisor
`run.status` updates are also emitted as events.

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
    "uiBaseUrl": ""
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
      "summary": {}
    }
  ],
  "capacity": {"active": 1, "cap": 1},
  "queueLen": 0
}
```

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
