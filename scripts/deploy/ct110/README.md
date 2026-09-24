# CT110 CI deploy (TASK 224 / disconnect checklist D5)

`.github/workflows/deploy-ct110.yml` replaces the PTAIT09-pushed deploy
(`C:/tmp/optimistic_deploy_harness.py` streaming `deploy-harness-v2.sh` into
`pct exec 110` over ssh) so CT110 stays upgradeable after PTAIT09 is
disconnected. The workflow is `workflow_dispatch`-only and every job runs on
the LAN runner (CT210, label `lan-deploy`).

## Stage map

| Stage | Where | What |
| --- | --- | --- |
| preflight | runner | `preflight.py`: ref resolves to a sha; a `vX.Y.Z` tag matches `pyproject.toml` version; default-branch tip has no red/in-flight checks |
| build | runner | npm Web bundle + `python -m build --wheel`; wheel version and bundled Web UI verified |
| wait | runner | `wait_zero_active.py`: counted consecutive zero-active polls of `GET /api/runs` |
| deploy | runner → PVE → CT110 | `run_on_ct110.sh` pushes bytes (sha256-verified at both hops) and runs `ct110_deploy.sh deploy` |
| verify | runner / CT110 | `systemctl is-active` == active; installed `lh_harness.__version__` == target; `GET /api/meta` == 200 |
| rollback | runner → CT110 | automatic on any post-deploy failure; loud sentinel if the rollback itself fails |

Greppable terminal markers: `CT110_DEPLOY_OK`, `CT110_DEPLOY_FAILED_ROLLBACK_OK`,
`CT110_DEPLOY_ROLLBACK_FAILED` (page-worthy: host is on an unknown version).

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
4. **DEPLOY-HOLD.** `ct110_deploy.sh` aborts when a hold file exists (its
   contents are the human reason). Rollback deliberately ignores the hold:
   it only runs after a failed deploy and must not be blocked from restoring
   the previous version.

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
- **Rollback source.** The pre-deploy version is recorded to
  `/home/harness/deploy/state/previous_version`, and every deployed wheel is
  archived under `/home/harness/deploy/wheels/` for future rollbacks. The
  very first CI deploy cannot roll back further than the wheel archive it
  finds (today: none) — its rollback would fail loudly by design. Seed the
  archive with the currently-live wheel if a first-run rollback path is
  wanted.
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
```

Until the PR carrying this workflow merges to main, dispatch with
`--ref <pr-branch>`: `workflow_dispatch` resolves the workflow file from the
branch you dispatch on.

## Manual recovery (if CT110_DEPLOY_ROLLBACK_FAILED ever fires)

On the PVE host: `pct exec 110 -- bash`, then inspect
`journalctl -u lh-harness.service -n 200`, reinstall the previous wheel from
`/home/harness/deploy/wheels/` (or rebuild it), `systemctl restart
lh-harness.service`, and confirm `systemctl is-active` +
`curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8799/api/meta`.
