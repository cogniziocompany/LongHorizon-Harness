# scripts/overseer_ct110/ — the CT110 overseer sweep tick (TASK 236)

This directory is the CT-side successor of the archived PTAIT09 tick chain
(`tools/overseer_tick.ps1` + `tools/overseer_tick_hidden.vbs`, scheduled task
`LH-Overseer-Sweep`).  Same concept, one headless `claude -p` invocation
every 5 minutes under the LOOP-PROMPT doctrine (`docs/LOOP-PROMPT.md`,
authoritative per `README-OVERSEER-APPARATUS.md:14`); different home and
different state surface.

| File | Role |
|---|---|
| `tick.sh` | The tick entrypoint: preflight (API reachability), the `claude -p` doctrine invocation, per-tick output log, telemetry handoff. |
| `tick_notify.py` | Per-tick telemetry: one Seq CLEF event (TASK 161 wire contract, `feat/seq-logging` @ edef634) and one hivemind ingest POST (TASK 229 contract, `tasks/overseer-ingest-task.txt`). Fail-open both. |
| `overseer-sweep-secrets.env.example` | Operator template for the CT110 EnvironmentFile — env-var NAMES with empty values; values are filled on the host and never committed. |

## What state it reads/writes (never C:/tmp)

The tick reads and writes the **CT110 harness API + Postgres apparatus
surface** (task 235, branch `feat/task-235` @ 532272b: `overseer_state.py` +
six read-only MCP tools behind `POST /api/mcp/fleet/{tool_name}` and the
`harness.queue` Postgres store on CT103).  Credentials and endpoints are
supplied by env-var **NAME** only through
`/home/harness/.overseer-sweep-secrets.env` (see
`packaging/lh-overseer-sweep.service`, which names every env var it expects).

On CT110 the entrypoint installs self-contained at
`/home/harness/.overseer-sweep/bin/{tick.sh,tick_notify.py}` (the task 224
units stage puts it there); the `claude -p` doctrine run itself happens in
the repo checkout (`LH_OVERSEER_REPO_ROOT`, default
`/home/harness/work/LongHorizon-Harness`) so the doctrine it reads is the
versioned `docs/LOOP-PROMPT.md` in that checkout.

`C:/tmp` is a read-only archive since cutover 168
(`README-OVERSEER-APPARATUS.md`, "The PC launcher is retired"); this tick
never touches it.

## Modes

- **read-only** (default, `LH_OVERSEER_TICK_MODE=read-only` in the unit):
  observe and report.  Reads the fleet state, writes only its own tick
  ledger row and its own hivemind ingest.  Does NOT resolve gates, requeue,
  edit task files, or merge.
- **act**: full LOOP-PROMPT authority.  Requires BOTH
  `LH_OVERSEER_TICK_MODE=act` AND `LH_OVERSEER_TICK_ACT_CONFIRM=YES` in the
  EnvironmentFile, and requires the PTAIT09 `LH-Overseer-Sweep` task to be
  disabled FIRST (reversible with `Enable-ScheduledTask`).  **Never both
  acting at once** — this is the rollout rule from the TASK 236 brief and
  the disconnect checklist D3 ordering.

Rollout (owner: Paxton, post-merge): enable the timer in read-only mode,
run 24h of shadow ticks (>= 288 rows per D3), compare the two tick ledgers,
then flip the mode and disable the PTAIT09 task.  Deploying the units never
starts or enables anything on its own.

## Verification without contacting anything

```
bash -n scripts/overseer_ct110/tick.sh
LH_OVERSEER_TICK_DRY_RUN=1 bash scripts/overseer_ct110/tick.sh --dry-run   # renders, posts nothing
systemd-analyze verify packaging/lh-overseer-sweep.service packaging/lh-overseer-sweep.timer
```

The dry-run renders the claude prompt, the configured sink NAMES and the
telemetry payloads (`--print-only`) and exits; it never executes `claude`
and never POSTs.