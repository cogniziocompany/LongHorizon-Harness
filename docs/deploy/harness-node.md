# Deploying a harness node from a git ref

This is the operator runbook for the node deploy pipeline:
`scripts/deploy-node.sh` (the in-node half) and
`.github/workflows/deploy-harness-node.yml` (the runner-side half).

It installs a node's code **straight from a git ref** into the node's existing
virtualenv, and leaves a **deploy receipt** on the node so the deployed commit
is readable over the API without a shell.

Status: **the pipeline ships in a PR; the first real deploy is a separate,
Paxton-approved step.** Nothing in this document has been run against a live
node yet.

## What already exists (this does not replace it)

| Path | What it owns |
| --- | --- |
| `.github/workflows/deploy-ct110.yml` | the wheel deploy: build a wheel on the runner, push it to the node as BYTES, install it, restart, verify, roll back |
| `scripts/deploy/ct110/ct110_deploy.sh` | the in-node half of that deploy (idle recheck, install, restart, verify, rollback) |
| `scripts/deploy/ct110/run_on_ct110.sh` | the runner-side transfer: `runner --scp--> PVE host --pct push--> node`, sha256-verified at both hops |
| `scripts/deploy/node/bootstrap-node.sh` | the FIRST install of a brand new node (container, user, venv, unit, secrets file) |
| `scripts/deploy/nodes.json` | the node inventory (names only, never values) |

The ref deploy is the OTHER half of the same story. It reuses the runner, the
SSH path, the transfer discipline, the venv layout and the unit; it differs
only in where the code comes from: **a git ref** instead of **a CI-built
wheel**. That means a deploy can be run for any commit in the repository
without a CI build first.

## The node layout this deploy assumes

| Fact | Value | Where it comes from |
| --- | --- | --- |
| service unit | `lh-harness.service` | `packaging/lh-harness.service`, `scripts/deploy/node/templates/lh-harness-node.service` |
| runs it | `/home/harness/venv/bin/python -m lh_harness web` | the same two files |
| user | `harness` | the same two files |
| virtualenv | `/home/harness/venv` (root-owned, non-editable install) | `scripts/deploy/node/bootstrap-node.sh` |
| settings store | `/home/harness/.lh-harness/settings.db` | `src/lh_harness/settings_store.py` (`DEFAULT_DB_PATH`) |
| run state | `<runs_root>/<run_id>/control/status.json` | `src/lh_harness/supervisor/control_bus.py` |
| credentials | `/home/harness/.lh-harness-secrets.env`, mode 600 | the unit's `EnvironmentFile=` |

The deploy never reads the secrets file. It talks to the local API with the
bearer token it is given in its **environment** (`LH_HARNESS_WEB_TOKEN`), the
same way `scripts/deploy/ct110/verify_meta.py` takes `CT110_API_TOKEN` from
the runner's environment. No credential value is printed, copied into a
receipt or written into a backup.

## Who runs it (and the privilege preflight)

The deploy is described as running **as the `harness` user**, and that is the
user that owns the state this deploy writes: the receipt and the backups live
under `/home/harness/.lh-harness/` (mode 700, owner `harness`), and so does
`settings.db` (mode 600).

The **venv is root-owned**, though: `scripts/deploy/node/bootstrap-node.sh`
creates `/home/harness/venv` as root and never chowns it, because the wheel
in it is installed by root. That is why the repository's own node deploy
(`scripts/deploy/ct110/ct110_deploy.sh`) runs as root inside the container.
The two facts cannot both be satisfied by a single uid.

So the deploy does not insist on one uid. It runs as whatever uid invokes it
and **preflights** that every path it writes is writable:

| Path | Written by the deploy as |
| --- | --- |
| `/home/harness/venv` (root-owned) | needs privilege `harness` does not have |
| `/home/harness/.lh-harness/` (owner `harness`) | `harness` |
| `/home/harness/.lh-harness/settings.db` (owner `harness`) | `harness` |

If any of them is not writable the deploy stops with rc=1 **before it touches
anything**, naming the path and the user it is running as - not with an opaque
pip error half way through the install. In the CI path (the workflow) the
script reaches the node through `pct exec`, which runs it as root, so all
three are writable.

## The drain rule

**A deploy restarts `lh-harness.service`, and a restart kills every in-flight
run.** So the deploy refuses to touch a node that has a live run.

The deploy reads the run state directly: for every run directory under the
runs root it reads `control/status.json` and looks at the `status` field. A
run is **alive** when its status is one of the supervisor's ACTIVE statuses
(`src/lh_harness/supervisor/lifecycle.py`):

```
creating   starting   running   waiting_approval   stopping
```

Everything else (`completed`, `failed`, `cancelled`, `blocked`, `incomplete`)
is terminal, and a run with no `status.json` at all never opened its run
directory, so it is not an alive run either.

Two ways to satisfy the rule:

* **wait it out** - `--drain-wait N` polls every 5 seconds for up to `N`
  seconds. If the runs finish, the deploy continues. If they are still alive
  when the wait expires, the deploy aborts (rc=2) and touches nothing.
* **stop the queue first** - the existing CT110 workflow's approach: set the
  queue drain flag (`POST /api/queue/drain`) so no NEW runs are launched,
  then wait for the live ones to finish. `scripts/deploy/ct110/set_drain.py`
  and `scripts/deploy/ct110/wait_zero_active.py` do this.

Without `--drain-wait`, a node with a live run is **refused** (rc=2), not
deployed over.

The rule is checked **first**, before anything on the node is touched: no
backup, no install, no restart.

## Operator steps

### 1. Decide the ref

The ref is a branch, a tag or a commit sha. The default is `origin/main`.

The ref is resolved to a commit **on the node** and checked out DETACHED at
that commit, so the installed code is exactly the ref and not "whatever the
branch pointed at later".

### 2. Run it from CI (the normal path)

```bash
gh workflow run deploy-harness-node.yml --ref <branch-with-the-workflow> \
  -f node=ct110 -f ref=origin/main
gh workflow run deploy-harness-node.yml --ref <branch-with-the-workflow> \
  -f node=ct111 -f ref=<sha> -f drain_wait_seconds=600
gh workflow run deploy-harness-node.yml --ref <branch-with-the-workflow> \
  -f node=ct110 -f ref=origin/main -f dry_run=true   # resolves the ref, touches nothing
```

`workflow_dispatch` resolves the workflow file from the branch you dispatch
on, so a workflow that is still in a PR is dispatched with `--ref <pr-branch>`.

The workflow needs the same prerequisites the CT110 deploy already has: the
self-hosted runner labelled `lan-deploy` on CT210, and the GitHub environment
`ct110-prod` / `ct111-prod` with `CT110_API_TOKEN` / `CT111_API_TOKEN` and
`CT110_PVE_SSH_KEY` / `CT111_PVE_SSH_KEY` (and the `CT110_WEB_URL` /
`CT111_WEB_URL` variables). See `scripts/deploy/nodes.json` for the inventory
and `scripts/deploy/node/README.md` for creating a node.

### 3. Run it on the node directly (a node in trouble)

The script runs **on the node**, in the container, as the user that can write
the venv (root) or, where the venv is group-writable, as `harness`:

```bash
# in the container, as a user that can write the venv
scripts/deploy-node.sh --ref origin/main
scripts/deploy-node.sh --ref <sha> --drain-wait 900
scripts/deploy-node.sh --ref origin/main --dry-run
```

If the invoking user cannot write the venv, the state directory or the
settings store, the deploy stops with rc=1 and names the path - see
[Who runs it](#who-runs-it-and-the-privilege-preflight).

The script clones the repository itself into `/home/harness/deploy/repo`, so
the node needs outbound git access to the repository, and the venv needs `pip`
and a build backend (`hatchling`).

### 4. Read the result back

A successful deploy prints exactly one line:

```
DEPLOY_OK node=ct110 commit=27ad8820f0e0f0e0f0e0f0e0f0e0f0e0f0e0f0e version=0.1.7 at=2026-10-26T18:40:11Z by=harness previous=none
```

`previous=none` means there was no earlier receipt; otherwise it names the
commit that was live before this deploy.

From the API (no shell on the node needed):

```bash
curl -s http://<node>:8799/api/meta | python3 -c 'import json,sys; print(json.load(sys.stdin)["build"])'
```

`build.commit` is the deployed commit and `build.version` the package
version. `GET /api/health` is a liveness probe that answers `200` with
`{"status": "ok", "service": "lh-harness"}` and nothing else; it is the one
API path that is not behind the bearer token, so the deploy can read it
without handling a credential.

## The receipt format

The receipt is written to `/home/harness/.lh-harness/deployed.json`:

```json
{
  "commit": "27ad8820f0e0f0e0f0e0f0e0f0e0f0e0f0e0f0e0f",
  "version": "0.1.7",
  "at": "2026-10-26T18:40:11Z",
  "by": "harness",
  "previous": {
    "commit": "b6f51f95fe8758176162b7d7c6750527b5e4928f",
    "version": "0.1.7",
    "at": "2026-10-20T09:12:03Z",
    "by": "harness"
  }
}
```

| Field | Meaning |
| --- | --- |
| `commit` | the commit that was installed and that the running service reports at `/api/meta` |
| `version` | the installed package version (every build is 0.1.7 today, so this alone never identifies a deploy) |
| `at` | when the deploy finished, in UTC |
| `by` | who ran it (the `harness` user, or the operator named in `LH_DEPLOYED_BY`) |
| `previous` | the receipt of the deploy before this one, or `null` for the first one |

The receipt is written **before** the service is restarted, because
`/api/meta` reports the receipt's commit: that is what makes the deployed
commit readable over the API without a shell. If the read-back then fails,
the rollback restores the previous receipt, so a receipt never names a commit
the node does not run.

`GET /api/meta` reports the receipt's commit when the receipt is present and
the package's own build commit otherwise (a CI-built wheel carries
`lh_harness/_build_info.json`). A missing or malformed receipt is ignored:
the package value is reported unchanged.

## The backups

Before the install the deploy copies three things aside, each with a **UTC**
suffix (`YYYYMMDDThhmmssZ`), into
`/home/harness/.lh-harness/backups/`:

| Backup | What it is |
| --- | --- |
| `package-metadata.<stamp>/` | the installed package's metadata directory (the dist-info pip owns) |
| `package-files.<stamp>/` | the installed package's code directory |
| `settings.db.<stamp>` | the settings store, copied (never moved) |

The package FILES are backed up, not just the metadata, so a rollback restores
the exact pre-deploy bytes. That is the lesson from the 2026-10-03 rollback
that "restored" the new code: every build is version 0.1.7, so reinstalling
"the version" can silently put the new code back.

## The rollback

A deploy is a success only when all three read-backs agree:

1. `systemctl is-active lh-harness.service` says `active`;
2. `GET /api/health` answers `200`;
3. `GET /api/meta` reports the commit that was deployed.

If any of them fails, the deploy **rolls back by itself**:

* the installed package files and metadata are restored from the backups;
* the previous receipt is restored (or the failed deploy's receipt is
  removed, when there was no previous one);
* the service is restarted on the pre-deploy code.

The deploy then exits rc=4 (read-back failed). The rollback itself fails
loudly with rc=3 when there is no usable backup - the node is then in a state
a human must look at, and the deploy says so rather than claiming success.

`--dry-run` resolves the ref and reports the live-run state, and touches
nothing: no install, no restart, no receipt, no backup.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success: installed, active, read-back agreed, receipt written |
| 1 | unexpected error (bad usage, the ref does not resolve, the privilege preflight failed, the install failed) |
| 2 | operator abort: a run was alive and no `--drain-wait` was given, or the drain wait expired |
| 3 | the rollback was impossible (no usable backup) |
| 4 | the read-back failed (the rollback was attempted) |

## What this deploy never does

* it never reads, prints or copies a secret;
* it never merges anything and never touches `main`;
* it never changes a lane cap;
* it never deploys more than one node per dispatch;
* it never runs automatically: `workflow_dispatch` is the only trigger.
