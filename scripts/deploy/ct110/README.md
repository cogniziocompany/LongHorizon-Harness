# CT110 CI deploy (TASK 224 / disconnect checklist D5)

`.github/workflows/deploy-ct110.yml` replaces the PTAIT09-pushed deploy
(`C:/tmp/optimistic_deploy_harness.py` streaming `deploy-harness-v2.sh` into
`pct exec 110` over ssh) so CT110 stays upgradeable after PTAIT09 is
disconnected. The workflow is `workflow_dispatch`-only and every job runs on
the LAN runner (CT210, label `lan-deploy`).

## Targets (one pipeline, several nodes)

The workflow keeps its name and file (`deploy-ct110.yml`) but takes a `target` input
(`ct110` default, `ct111`). The scripts in this directory were already parameterised by
`CT_ID`, `PVE_HOST`, `--url` and so on, so they are shared unchanged; only the workflow chooses
per target:

| | `ct110` (default) | `ct111` |
| --- | --- | --- |
| GitHub environment | `ct110-prod` | `ct111-prod` |
| Secrets | `CT110_API_TOKEN`, `CT110_PVE_SSH_KEY` | `CT111_API_TOKEN`, `CT111_PVE_SSH_KEY` |
| Variables | `CT110_WEB_URL`, `CT110_PVE_HOST`, `CT110_PVE_SSH_USER`, `CT110_CT_ID` | `CT111_WEB_URL` (**required**, no default), `CT111_PVE_HOST`, `CT111_PVE_SSH_USER`, `CT111_CT_ID` (default `111`) |
| Concurrency group | `deploy-ct110` | `deploy-ct111` |
| Wheel artifact | `ct110-wheel` | `ct111-wheel` |
| `overseer-units` job | runs | skipped |

A dispatch without `target` is the CT110 deploy exactly as before. Secret and variable names
are built from a literal prefix (`secrets[format('{0}_API_TOKEN', 'CT110' | 'CT111')]`), so an
unset secret resolves to empty and fails the run instead of falling through to the other node's
credential. Inside the job the env names (`CT110_API_TOKEN`, `CT110_WEB_URL`, …) and the
`CT110_*` log markers are the same for both targets because the helper scripts read those
names; the deployment record states the target node.

The inventory is `scripts/deploy/nodes.json` (names only), kept in agreement with the workflow
by `tests/test_deploy_node_targets.py`. Creating a node from nothing — container, first
install, credentials, registration — is `scripts/deploy/node/README.md`.

## Stage map

| Stage | Where | What |
| --- | --- | --- |
| preflight | runner | `preflight.py`: ref resolves to a sha; a `vX.Y.Z` tag matches `pyproject.toml` version; default-branch tip has no red/in-flight checks |
| build | runner | npm Web bundle + the target sha stamped into `src/lh_harness/_build_info.json` + `uv build --wheel`; wheel version, build commit and bundled Web UI verified |
| drain | runner | `set_drain.py --enable`: POST `/api/queue/drain` stops NEW queue launches (TASK 242); live runs finish untouched, and the flag survives the restart so a failed verify leaves CT110 drained rather than refilling the slots |
| suspend | runner | `suspend_runs.py` (fc-H4b, `active_runs=suspend` only): POST `/api/maintenance/suspend` stops every ACTIVE run and parks it in a persisted manifest |
| wait | runner | `wait_zero_active.py`: counted consecutive zero-active polls of `GET /api/runs` — with `active_runs=suspend` a fixed 10-minute timeout (only the suspend stops have to land); with `active_runs=wait`, `wait_timeout_minutes` as before |
| deploy | runner → PVE → CT110 | `run_on_ct110.sh` pushes bytes (sha256-verified at both hops) and runs `ct110_deploy.sh deploy` |
| verify | runner / CT110 | `systemctl is-active` == active; installed `lh_harness.__version__` == target AND installed build commit (`python -m lh_harness.build_info`) == target sha; `verify_meta.py`: `GET /api/meta` polled up to 90 s in 5 s steps until 200, then `meta.build.commit` == target sha (the RESTARTED process is the new code); `meta.drain.enabled == true` (the restarted service must come back drained) |
| rollback | runner → CT110 | automatic on any failure AFTER the deploy step has started (a skipped deploy step — wheel download, SSH key staging, zero-active wait — aborts the run with NO service restart). Reinstalls the pre-deploy wheel identified BY SHA256 (copied aside when the deploy started), then verifies like the deploy: wheel sha256 and (if the old build has one) build commit inside CT110, and the bounded `/api/meta` wait with `meta.build.commit` == the pre-deploy commit. Runs BEFORE resume and drain clear, so its restart never kills resumed runs or fresh launches. Loud sentinel if the rollback or its verify fails |
| resume runs | runner | `resume_runs.py` (fc-H4b, `active_runs=suspend` only): POST `/api/maintenance/resume` puts every parked run back (`mode=continue`) and polls `GET /api/runs` until each is ACTIVE again — runs BEFORE the drain clears, on the success path and (after the rollback) on the failure path alike (fails by name if a run never comes back) |
| resume | runner | `set_drain.py --disable`: clears the drain and verifies `meta.drain.enabled == false` — runs whenever the flag was set (success or failure path), so a failed deploy never leaves the queue frozen |

Greppable terminal markers: `CT110_DEPLOY_OK`; on a failed deploy one of
`CT110_DEPLOY_FAILED_ROLLBACK_OK` (the service reports the pre-deploy commit),
`CT110_DEPLOY_FAILED_ROLLBACK_COMMIT_UNVERIFIED` (pre-deploy wheel bytes
restored and the service answers, but that build predates build info, so its
commit cannot be read back), `CT110_DEPLOY_FAILED_NOTHING_INSTALLED` (the
deploy aborted before installing); and `CT110_DEPLOY_ROLLBACK_FAILED`
(page-worthy: the pre-deploy build was NOT restored or did not verify).

**Why the build commit, not the version (2026-10-03, run 37162090599,
b6f51f9).** Every build is version 0.1.7. The old verify called `/api/meta`
once, 1.3 s after the restart, got curl exit 7 (nothing listening yet) and
failed; the rollback then reinstalled `wheels/lh_harness-0.1.7-py3-none-any.whl`
— which the same deploy had just overwritten with the NEW wheel — and
reported `CT110_DEPLOY_FAILED_ROLLBACK_OK`. The new code kept running.

## Paid-for lessons encoded here — do not strip them

1. **Bytes, not text.** On 2026-09-18 a text-mode pipe added CRLFs and bash
   died with `$'do\r'`. Payloads move via scp + `pct push` (both binary) and
   are sha256-verified at each hop before execution. `.gitattributes` pins
   `scripts/deploy/**` to `eol=lf`.
2. **Exit code 2 is overloaded** — it means BOTH "inner idle-recheck/hold
   abort" AND "bash syntax error". `run_on_ct110.sh` prints captured remote
   stderr before interpreting any nonzero rc; rc=2 additionally prints an
   `::error::` explaining the ambiguity.
3. **A deploy restarts the service and kills in-flight runs.** Deploys only
   happen inside a counted zero-active window (runner-side), and
   `ct110_deploy.sh` re-checks idleness on-host immediately before
   `systemctl restart` — a run launched between the two checks aborts the
   deploy with rc=2 instead of being killed.
   TASK 242: with a normal backlog that window may never open on its own
   (the kimi slots refill the moment one frees). The runner now sets the
   queue drain flag first (`POST /api/queue/drain`, operator bearer token)
   so the backlog stops launching; the zero-active wait then only has to
   outlive the already-running runs. The flag persists in
   `runs_root/queue/drain.json` across the restart, the verify step proves
   the new service came back still drained, and `set_drain.py --disable`
   resumes launching once everything is verified.
4. **DEPLOY-HOLD.** `ct110_deploy.sh` aborts when a hold file exists (its
   contents are the human reason). Rollback deliberately ignores the hold:
   it only runs after a failed deploy and must not be blocked from restoring
   the previous version. The workflow gates rollback on the deploy step
   having actually started (`steps.deploy.outcome` in `success`/`failure`),
   so a pre-deploy failure can never reach the rollback's service restart.
5. **Long runs can outwait the zero-active window, so the default is to stop
   and resume them (fc-H4b).** `active_runs=suspend` (the default) drains the
   queue, stops every ACTIVE run with POST `/api/maintenance/suspend` (the API
   refuses with 409 unless the drain is already set), waits at most 10 minutes
   for the stops to land, deploys, verifies, then puts every parked run back
   with POST `/api/maintenance/resume` (`mode=continue`) and only clears the
   drain once each resumed run is ACTIVE again. The resume step is
   `always()`-gated on the suspend step having succeeded, so it also fires on
   the automatic-rollback path — a suspended run is never left behind, and the
   resume step fails by name if a run does not come back within
   `--timeout-minutes` (default 5). `active_runs=wait` is exactly the lesson-3
   behaviour. **Cost:** the round a suspended run had in progress is redone
   after resume.

## ACTIVE runs during the deploy: `active_runs=suspend` (default) or `wait`

A deploy restarts `lh-harness.service`, which kills every in-flight run, so an
ACTIVE run must be gone before the deploy touches the host. The
`active_runs` input chooses how:

- **`suspend` (default, fc-H4b).** After the drain stops new launches,
  `suspend_runs.py` POSTs `/api/maintenance/suspend`: the service stops every
  ACTIVE run and parks it in `runs_root/queue/maintenance_manifest.json`.
  A short zero-active wait (fixed 10-minute timeout — only the stops have to
  land) confirms idleness, the deploy runs and verifies, then
  `resume_runs.py` POSTs `/api/maintenance/resume`, which restarts every
  parked run with `mode=continue`, and polls `GET /api/runs` until each one
  reports an ACTIVE status again (5-minute default; the step fails naming any
  run that does not come back). Only then is the drain cleared. The resume
  step is `always()`-gated on the suspend step having succeeded, so parked
  runs also come back when the deploy fails and automatically rolls back —
  a suspended run is never left behind.
- **`wait`.** Exactly the pre-fc-H4b behaviour: no suspend, no resume, and
  the zero-active wait simply outlives the live runs for up to
  `wait_timeout_minutes` (default 30).

**Cost of `suspend`:** the round a suspended run had in progress is redone
after resume. Choose `wait` for a deploy that must not disturb a run's
in-flight round at all.

## Assumptions (mechanics that live only on PTAIT09 and could not be read)

- **Hold-file path.** Candidates: `$LH_DEPLOY_HOLD_FILE`, then
  `/home/harness/work/LongHorizon-Harness/DEPLOY-HOLD`, then
  `/home/harness/DEPLOY-HOLD`. If the 09-18 mechanism used another path, set
  `LH_DEPLOY_HOLD_FILE` to it (or move the file).
- **Install shape.** The live unit (`systemctl cat lh-harness.service`) runs
  `/home/harness/venv/bin/python -m lh_harness web` — a NON-editable pip
  install in the root-owned `/home/harness/venv`. The deploy therefore
  installs a built wheel into that venv (`pip install --upgrade`), not a git
  checkout, and the in-repo `packaging/lh-harness.service` (which points at a
  repo-local `.venv`) does NOT match the live unit. Reconcile that drift in
  its own change; this deploy preserves the live shape.
- **Rollback source.** Before installing, the deploy records the live
  build to `/home/harness/deploy/state/` (`previous_version`,
  `previous_commit` — empty for builds that predate build info —
  `previous_wheel_sha256` from pip's `direct_url.json`) and copies the
  archived wheel whose sha256 matches into `state/rollback/`. Deployed wheels
  are archived per build under `/home/harness/deploy/wheels/by-commit/<sha>/`,
  so a same-version build can never overwrite an older one (the legacy flat
  `wheels/lh_harness-<version>-py3-none-any.whl` files are still searched by
  sha256). If no archived wheel matches the live install, the deploy warns and
  its rollback fails loudly (rc=3) instead of installing something else.
  `state/install_started` marks that the install began; without it rollback
  exits 5 ("nothing to roll back").
- **Host naming.** CT110 is LXC 110 on the PVE host `corsairai300`, web API
  on `http://192.168.21.168:8799` (docs + live host match). Overridable via
  environment variables below.

## Human-owned prerequisites (cannot be created from code)

1. **Runner registration (template lesson 2026-09-06).** Self-hosted runners
   are per-repo: register one for `cogniziocompany/LongHorizon-Harness` on
   CT210 as user `runner` with `--labels lan-deploy` (recipe in
   `tasks/TEMPLATE-overseer-hierarchy.md`). The runner needs `python3`,
   `curl`, and `unzip`.
2. **GitHub environment `ct110-prod`** holding these secrets (referenced by
   name only, never committed):
   - `CT110_API_TOKEN` — bearer token for the CT110 web API
     (`LH_HARNESS_WEB_TOKEN`; read on CT110, do not copy into git).
   - `CT110_PVE_SSH_KEY` — private key allowed to ssh `$PVE_USER@$PVE_HOST`
     (the same identity CT210 already keeps at
     `/home/runner/.ssh/id_ed25519_proxmox` for other deploys).
   Optional environment variables (defaults in the workflow match today):
   `CT110_WEB_URL`, `CT110_PVE_HOST` (`corsairai300`),
   `CT110_PVE_SSH_USER` (`root`), `CT110_CT_ID` (`110`).

## Known host-side hygiene issue (do NOT fix from this change)

The live unit at `/etc/systemd/system/lh-harness.service` has
`LH_HARNESS_WEB_TOKEN` inline in an `Environment=` line (world-readable via
`systemctl cat`). `ct110_deploy.sh` treats the unit file only as a fallback
token source and never echoes it. Rotating the token into
`/home/harness/.lh-harness-secrets.env` alone is a separate, operator-run
change.

## Dispatching

```
gh workflow run deploy-ct110.yml --ref <branch-with-workflow> \
  -f target_ref=v0.1.8
gh workflow run deploy-ct110.yml --ref <branch-with-workflow> \
  -f target_ref=v0.1.8 -f dry_run=true   # preflight+build only, never touches CT110
gh workflow run deploy-ct110.yml --ref <branch-with-workflow> \
  -f target=ct111 -f target_ref=v0.1.8   # the finance node; omit target for CT110
gh workflow run deploy-ct110.yml --ref <branch-with-workflow> \
  -f target_ref=v0.1.8 -f active_runs=wait  # never stops runs; waits them out (pre-fc-H4b behaviour)
```

Until the PR carrying this workflow merges to main, dispatch with
`--ref <pr-branch>`: `workflow_dispatch` resolves the workflow file from the
branch you dispatch on.

## Manual recovery (if CT110_DEPLOY_ROLLBACK_FAILED ever fires)

On the PVE host: `pct exec 110 -- bash`, then inspect
`journalctl -u lh-harness.service -n 200`, reinstall the previous wheel from
`/home/harness/deploy/state/rollback/` (the pre-deploy bytes; its sha256 is
in `state/previous_wheel_sha256`) or `/home/harness/deploy/wheels/by-commit/<sha>/`
with `pip install --force-reinstall --no-deps` (or rebuild it), `systemctl restart
lh-harness.service`, and confirm `systemctl is-active` +
`curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8799/api/meta`
(its `build.commit` names the running commit).
