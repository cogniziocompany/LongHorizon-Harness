# README — migrated overseer apparatus (task 104 / 104b)

This directory tree is the overseer apparatus migrated off the PTAIT09 workstation
(export snapshot `ptait09-export-20260923`) into the repo, sanitized (env-var NAMES only,
see `docs/SECRETS.md`). It is an ARCHIVE: read it for history and doctrine; do not treat
any of it as live automation.

## Authoritative vs generated

| Path | Status |
|---|---|
| `docs/SECRETS.md` | **Authoritative.** Placeholder registry: every redacted credential, what it stands for, and where the real value lives. Read before touching anything under `tools/`. |
| `docs/LEDGER.md` | **Authoritative.** The overseer tick ledger — the written record of what the scheduled overseer observed and decided, tick by tick, up to the cutover. |
| `docs/LOOP-PROMPT.md` | **Authoritative.** The loop doctrine the scheduled overseer ran under. |
| `docs/handoffs/HANDOFF-*.md` | **Authoritative.** Operator handoffs written during the overseer era; each is a point-in-time statement of fact, NOT a live status. Newer handoffs supersede older ones on the same topic. |
| `tasks/*-task.txt` (and `.md`) | **Authoritative.** Task briefs as filed. The brief is what the run was given; whether the work landed is recorded in the queue records, not here. |
| `queue/STRANDING-DISPOSITION.md` | **Authoritative.** Cause-proven disposition of the 24 stranded runs (2026-09-17). |
| `queue/OPEN-ASKS.md` | **Authoritative.** Open asks register as it stood at export time. |
| `tools/deliver-secret.sh`, `tools/launch_queue.py`, `tools/optimistic_deploy_harness.py`, `tools/spin_watchdog.py`, `tools/validate_queue.py`, `tools/overseer_tick.ps1`, `tools/overseer_tick_hidden.vbs` | **Archived copies** of the scripts that ran on the workstation / PTAIT09. Archived AS-IS (sanitized, not refactored). They are NOT wired to anything in this repo and must not be run as-is. |
| `ship-plane.html` | **Archived** copy of the served Ship Plane page (a point-in-time snapshot, not the live board). |
| `overseer_inventory.txt` | **Generated.** Machine-produced inventory (find + sizes) of the export snapshot. |

## `queue/done/` records are an UPPER BOUND, not completion

Each `queue/done/*.json` / `queue/blocked/*.json` file is the run record **as it was filed
at launch time**: task text, scope, and gates the run was released under. It is an upper
bound on what the task was allowed to do — it is NOT proof that the task completed, landed,
or was merged. Ground truth for what actually landed is the git history / merged PRs of the
repos the brief points at, not the queue record.

## The PC launcher is retired

The PC-based launcher and tick chain (PTAIT09 scheduled task `LH-Overseer-Sweep`,
`tools/overseer_tick.ps1` + `tools/overseer_tick_hidden.vbs`, `tools/launch_queue.py`) are
**retired at cutover 168**: after the cutover there is no PC launcher process; its bearer
returns 401; `C:\tmp\queue` is a read-only archive. The scripts here are the historical
record of that apparatus, kept for post-mortem and audit value — not a runnable system.
The overseer tick itself moving to a CT-hosted home is the NEXT task, filed after this
migration PR lands. It has since landed as TASK 236: the CT-side sweep lives in
`packaging/lh-overseer-sweep.{service,timer}` + `scripts/overseer_ct110/` (READ-ONLY
default), driven by the adapter doc `docs/OVERSEER-TICK-CT110.md` — see those files;
this archive remains the historical record.