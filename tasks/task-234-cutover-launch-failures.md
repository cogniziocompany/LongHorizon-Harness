# TASK 234 — Cutover launch failures: read-only auditor profile, pre-burn refusal, launch smoke test

Measured on CT110 2026-09-24 ~04:25Z by interactive session [61855af8] during the
chat.easybutt0n.ai 'new overseer' test. This file is the PR notes/draft carrier for
the task; the PR body should be assembled from it. DO NOT MERGE. Env-var NAMES only
(no values anywhere in this repo).

## What failed (measured, CT110)

Two failure classes around the 2026-09-23 ~23:53 cutover restart:

1. **mcp_profile refusals** — launches failed with
   `roles.auditor.mcp_profile 'default' is not read-only; auditor roles require a read-only MCP profile`.
   The raise lives in `supervisor/service.py::_normalise_role_configs` (auditor roles
   must resolve to a read-only MCP profile; `default` is read-write because it adds
   github/youtrack/ssh). On the queue path the launcher caught that `ValueError` in
   `Launcher._launch` and called `queue_store.mark_failed(..., "launch failed: ...")`
   BEFORE retry classification — the refusal matched neither retry signature, so the
   entry died on a burned attempt with no successor and no skip reason.

2. **`worker exited with status 2`** — five runs around 23:50–23:52Z died within
   seconds of start: `20260923T235013Z_312781db`, `20260923T235030Z_9c347f23`,
   `20260923T235047Z_769c09e4`, `20260923T235103Z_70ed3e28`, `20260923T235120Z_7164fbff`
   (both workspaces, LongHorizon-Harness and cognizioware-hydra, affected; only the
   227 relaunch survived). Each `worker.log` is exactly two lines:
   `Cannot start run: supervised run role configuration does not match its reservation`
   followed by `Using config: ...`. The raise is `src/lh_harness/cli.py:329` (sibling
   checks 320–373): the reservation stored one role configuration while the worker
   re-derived another from the workspace config, transiently around the cutover; the
   23:54 retries started normally. Remedy tracked as D2 (separate commit round): the
   mismatch error must print BOTH the reservation-stored (expected) and the recomputed
   (actual) role configuration so the next occurrence is self-explaining.

## D1 — read-only auditor profile + pre-burn refusal (this commit)

Code (`src/lh_harness/launcher.py`, `src/lh_harness/supervisor/service.py`):

- New public helper `supervisor.service.auditor_read_only_violation(role_configs, *,
  agent, model, mcp_profile=None)`: re-runs the same `_normalise_role_configs`
  validation the supervisor performs at `create_run` time and returns the exact
  `ValueError` text an auditor profile violation would raise, or `None` when the
  launch validates. Unrelated validation errors are left for `create_run`'s own
  handling.
- `Launcher._launch` now refuses BEFORE `prepare_workspace_base`/`create_run` when the
  auditor's effective profile would violate the read-only rule. The refusal is
  appended to the entry's `skip_reasons` via the existing pre-burn mechanism
  (`queue_store.record_skip` — the same path the workspace-base guard uses): status
  stays `pending`, attempt count unchanged, `mark_failed` is never reached, and the
  `queue.skipped` service event carries the reason.
- The check resolves the auditor's effective profile with the worker's real
  precedence: an explicit `[run.roles.auditor] mcp_profile` from the project config
  wins over the trio's run-wide `mcp_profile`, then env web defaults
  (`LH_HARNESS_WEB_DEFAULT_AUDITOR_MCP_PROFILE` / `LH_HARNESS_WEB_DEFAULT_MCP_PROFILE`
  — NAMES only; values are supplied out-of-band), then the built-in default
  (`audit` for the auditor). The launch itself still strips `mcp_profile` from every
  role spec, so a passing launch cannot regress into the cutover-168
  reservation-mismatch death mode.
- Original refusal string no longer burns an attempt: correctly configured launches
  pass the check and are sent to `create_run`; the misconfigured combination is
  refused pre-burn with the clear reason.

Tests (`tests/webapi/test_launcher.py`, `tests/webapi/test_auditor_read_only_refusal.py`):

- `test_launcher_skips_non_read_only_auditor_profile_without_burning_attempt` — the
  CT110 shape (trio profile `default`, no auditor binding): status pending, attempt
  unchanged, refusal recorded in `skip_reasons`, no `create_run` call, survives a
  second tick.
- `test_launcher_skips_unknown_profile_with_read_only_requirement` — same refusal for
  an explicit auditor role override; launched specs stay stripped.
- `test_auditor_read_only_violation_matches_service_refusal` and friends — the helper
  returns exactly the supervisor's `ValueError` text and `None` for read-only/unset
  profiles and unrelated validation errors.
- `tests/webapi/test_auditor_read_only_refusal.py` — end-to-end pinning of the refusal
  text, the pre-burn no-attempt-consumption path, the config-binding precedence that
  lifts the refusal, and the worker-side precedence.

## Consumed live config (CT110, applied 2026-09-24)

`/home/harness/work/.lh-harness/config.toml` (the config the live service consumes;
cwd of the service process is `/home/harness/work`) gained:

```toml
[run.roles.auditor]
model = "kimi-k2.7-code:cloud"
mcp_profile = "audit"
```

- Backup taken before the edit: `config.toml.bak-20260924-task234` (same directory).
- Validated after the edit with the harness's own validation paths (no service
  restart, no launches, live service untouched): `config.load_run_defaults` parses the
  file, `resolve_profile("auditor", role_profile="audit", gateway_key="validation-only")`
  resolves read-only, and the launcher pre-burn check accepts both trios.
- **The repo default is non-authoritative for CT110**: this repository's example
  blocks (docs/queue.md, docs/release-checklist-ct110.md) describe other deployments
  and are NOT the config the CT110 service consumes; the consumed file lives outside
  this repo at `/home/harness/work/.lh-harness/config.toml` and was edited in place.
  The repo-mirrored snippet in `docs/queue.md` ("Example `config.toml` block") is the
  documented equivalent for other deployments.

## D3 — launch smoke test (planned, this PR)

A standalone smoke-test script + pytest that starts and stops a 1-round no-op run
(the only permitted real launch) will land in this PR, based on `main`. Note for the
PR body: `.github/workflows/deploy-ct110.yml` (task 224, PR #49 — unmerged at the time
of writing, lives only on `feat/task-224-ci-deploy-ct110`) should invoke the smoke
script once both PRs merge, as a step between the `GET /api/meta` verification and the
`DEPLOY OK` marker, gated `if: ${{ !inputs.dry_run }}`.

## Deliverable status

- D1 (this commit round): implemented + tested + applied to the consumed config.
- D2 (next round): `cli.py` reservation-mismatch error prints expected vs actual role
  configuration; PR documents the root cause with the five run IDs above.
- D3 (following round): standalone smoke script + test; PR body notes the PR #49
  wiring.
- Final round: push branch + open PR against `main`. DO NOT MERGE.