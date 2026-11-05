# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **The auditor can independently verify a pull request, read-only.** The
  harness is a validation chain: each step validates the last in its process
  to achieve its tasks, so a step may only claim success if the step before
  it actually validated the thing being claimed.  The auditor is the last
  independent verification step, and until now it could not verify anything
  about the outside world.  Its MCP profile (`audit`) lists only servers
  whose tools are inherently non-mutating — the gateway cannot scope a
  server to read-only, so adding the `github` server there would have handed
  the auditor WRITE-capable tools — and its tool deny-list refused
  `Bash(gh *)` wholesale, taking out the local `gh` CLI's READ verbs along
  with the write ones.  (That blanket deny has since been replaced by
  per-spelling write denies; see below.)

  The consequence was structural.  An auditor with no independent evidence
  could only RESTATE an executor's claims or refuse them; it could never
  CONFIRM or REFUTE them.  A run whose auditor could not reach GitHub
  produced an audit saying "any executor claim about the PR is untrusted in
  its entirety" — and that run was then recorded with no distinction from
  one that had been delivered and audited cleanly.

  New module `lh_harness/auditor_verify.py` gives the auditor a read-only
  reach of its own:

  * `ReadOnlyGitHubClient` reads a pull request's existence, number, state,
    base branch, head sha and diff stat.  Read-only is a property of the
    code, not a convention: every request is a `GET`, the write verbs have no
    code path at all, and the one dynamic entry point
    (`ReadOnlyGitHubClient.request`) refuses anything that is not a read.
  * `assert_read_only_command` is the enforcement layer for the `gh` / `git`
    command surface.  It refuses `gh pr merge`, `gh pr comment`,
    `gh pr close`, the `gh api` write methods (`--method=POST`, `-X PATCH`,
    `-X PUT`, `-X DELETE`) and the field-supplying `gh api` bodies (`-f`,
    `-F`, `--input`) that a bare `Bash(gh *)` deny would leave unmatched,
    plus the write leaves that hide under read groups (`gh pr review
    --approve`, `gh secret set`, `gh workflow run`).
  * `resolve_auditor_credential` resolves the auditor's OWN fine-grained
    read-only token from `LH_HARNESS_AUDITOR_GITHUB_TOKEN` and refuses to
    proceed when the resolved value collides with the executor's `GH_TOKEN` /
    `GITHUB_TOKEN` or with a fleet admin token.  The value is never printed,
    logged, or committed; the credential's own `repr` renders presence, not
    value.

  Fail-closed by construction: any read failure — network error, missing PR,
  permission error, unexpected status — yields an explicit `unverifiable`
  verification result that BLOCKS a delivered claim.  It never becomes a
  silent pass and never becomes a confirmed value.

  New CLI command `lh-harness verify-pr --repo <owner/name> --number <n>
  [--reviewed-sha <sha>]` runs the verification and prints a verdict of
  `confirmed`, `refuted`, or `unverifiable`.  Exit code 0 means CONFIRMED;
  a refutation or an unreachable read exits non-zero, because an auditor that
  could not reach the evidence has no basis to call the claim true.

  **The auditor's reach is now REACHABLE, not merely present.**  The command
  the auditor's role instructions named — `lh-harness verify-pr` — did not
  resolve in the agent environment: the console script is only on PATH inside
  the project virtualenv, and where it did resolve it resolved to the
  *installed* package, which has no `verify-pr` subcommand at all.  A
  verification surface the verifier cannot invoke is not a verification
  surface.  The auditor's instructions now name
  `python3 scripts/auditor_verify_pr.py --repo <owner/name> --number <n>
  [--reviewed-sha <sha>]`, a script that resolves THIS checkout's own `src`
  tree ahead of anything installed in site-packages, so the auditor verifies
  against the code in the working tree it is auditing.  Exit code 0 means
  confirmed; any other exit means NOT confirmed.

  **The blanket `Bash(gh *)` deny is replaced by per-spelling write denies.**
  Denying `gh` wholesale took out the local `gh` CLI's READ verbs along with
  its write ones.  `AUDITOR_GH_WRITE_DENY` now denies the write spellings one
  by one — `gh pr merge`, `gh pr comment`, `gh pr close`, `gh pr ready`,
  `gh pr edit`, `gh issue comment`, the `gh api` write methods
  (`--method=*`, `-X *`), the field-supplying `gh api` bodies (`-f`, `-F`,
  `--field`, `--raw-field`, `--input`), `gh secret set`, `gh workflow run`,
  `git push` and the rest — while a `gh` read is no longer blocked by the
  same rule that blocks a write.  The in-process enforcement layer
  (`assert_read_only_command`) is the belt to that deny list's braces: it
  refuses a write that arrives by any other route, including a direct call
  into the harness's GitHub client.

  **Every write spelling is refused INDIVIDUALLY and asserted individually.**
  `tests/test_auditor_read_only_spelling_coverage.py` gives each spelling its
  own named test rather than one generic parametrized case, so a spelling
  cannot be lost from a list or moved to the wrong group without a test
  failing.  Each refusal is asserted to name the attempted operation and to
  carry no credential value.

  **The read-only enforcement is now proven at BOTH layers, and the
  permission layer had six real holes.**  The auditor's read-only reach is
  enforced in two places: the in-process layer
  (`assert_read_only_command`), which refuses a write by the SHAPE of the
  invocation, and the permission layer — the `Bash(...)` rules handed to
  Claude Code as `--disallowedTools` — which stops the auditor shelling out
  to a write at all.  Only the first was proven.  The permission layer was
  checked by string MEMBERSHIP: the tests asserted that the literal rule
  `"Bash(gh pr merge*)"` was IN the deny list, which proves the rule exists
  and nothing about whether it DENIES the command it is written for.  A rule
  can sit in the list and match nothing, and the shell-out is then refused
  by nothing at all.

  Six write spellings were in exactly that state — refused in-process, matched
  by no deny rule: `gh issue reopen`, `gh issue delete`, `gh release delete`,
  `gh secret delete`, `gh variable delete`, and the joined
  `gh api <path> --input=payload.json` body spelling.  All six now have a
  deny rule, and `uncovered_auditor_write_spellings()` computes the
  permission layer's coverage instead of assuming it.

  New in `lh_harness/auditor_verify.py`:
  * `write_intent_reason(argv)` is the SHAPE classifier the whole surface is
    built on: it decides by program, subcommand and leaf, HTTP method, and
    the presence of a request body — never by matching one literal command
    string — so a renamed or parameterised write is refused the same way.
    `assert_read_only_command` now sits on top of it.
  * `deny_rule_matches(command, rule)` and
    `is_denied_by_auditor_policy(command, rules)` are the permission layer's
    matcher.  They make a refusal at that layer PROVABLE in-repo instead of
    asserted by string membership.
  * `AUDITOR_WRITE_SPELLINGS` is the canonical list of write spellings, as
    argv lists, and is the single source of truth for the per-spelling tests
    and for the deny list's coverage invariant.

  New `tests/test_auditor_write_spelling_refusal.py` checks every spelling
  against BOTH layers, with each spelling in its own named test, and adds
  the HTTP-layer proof that no write request is ever issued: the client's
  transport records every request and the test asserts that the number of
  non-GET requests is ZERO.  Fail-closed behaviour is unchanged — an
  unreachable read still yields an explicit `unverifiable` that blocks a
  delivered claim, never a silent pass.

  The auditor's contract header is UNCHANGED: still exactly `Status` /
  `Integrity` / `Contract audit`.  The `confirmed` / `refuted` /
  `unverifiable` values are this module's internal verification result, not a
  contract-header vocabulary.  The close-out outcome vocabulary belongs to the
  sibling task `yt:easybutt0n-17:harness-closeout-contract` and is neither
  defined nor renamed here.  PR #147 (auditor isolation) and PR #159 (queue
  outcomes) are referenced as design references, not re-implemented.

- **The launcher enforces a node-level active-run ceiling.** `[queue] node_max_runs`
  is the maximum number of runs the launcher will let be active on THIS node at
  once — the value the fleet registry advertises as `maxConcurrentRuns`. Until
  now the launcher enforced only the per-trio caps in `[queue.capacity]`, and
  because those sum past the node maximum a node could run well over its own
  advertised limit (ct110 on 2026-10-22: 11 active runs against an advertised
  maximum of 5, with per-trio caps summing to 13).  `_run_pass` now counts the
  active runs from the same `_active_runs()` set it already counts per-trio
  occupancy from, and once the node maximum is reached it skips the entry
  through the existing pending path with
  `node at capacity: <n> active runs, node max <m>`.  The per-trio caps keep
  working unchanged and the tighter of the two wins.  The key is a NEW,
  documented config key (the harness had no node-level value before; the
  advertised number lived only in the Hydra node registry): it defaults to
  `5`, matching what ct110 advertises today, and a node that advertises
  something else (ct111: `2`) sets it explicitly.  No existing per-trio cap,
  node maximum value or default was changed.

- **Queue entries carry a go-to-market manager (`gtm_manager`).** A new
  optional enqueue field names the overseer that owns the task: `overseer1` or
  `overseer1-op` (GTM-MANAGER-G1-a, 2026-10-22). It is separate from the
  creator fields (`requested_by`, `requester`): other agents create tasks, so
  the creator is not the manager. Absent means the default manager
  (`overseer1`), any other value is refused by the enqueue path's own
  validation error (422 on REST, `code: 422` on MCP), and the value is stored
  on the entry, returned by `GET /api/queue` / `GET /api/queue/{id}`, and
  carried onto a requeued successor. Migration
  `007_harness_queue_gtm_manager.sql` adds the Postgres column
  (`VARCHAR(32) NOT NULL DEFAULT 'overseer1'`); an entry written before the
  field existed reads back as the default manager.

- **Postgres queue: retry and backoff columns.** Migration
  `005_harness_queue_retry_backoff.sql` adds `retry_of`, `attempt`,
  `failure_cause`, `not_before` and `wait_reason` to `harness.queue`, and
  `PgQueueStore` writes and reads them like the file store: a requeued entry
  keeps its lineage (the `max_retries` cap now trips through re-reads) and its
  provider-quota wait (the launcher's `waiting:` skip works on Postgres). Also
  fixed so the backend runs on a real psycopg 3 server at all: the store's
  `with self._txn()` blocks now run on a cursor with commit/rollback (a
  psycopg 3 connection closes at the end of `with conn:` and has no
  `fetchall`), JSONB `skip_reasons` decoded by the driver are kept, a dedup hit
  returns the full row, and a requeue carries `branch`/`continue_branch`.

- **Run list failure fields.** `GET /api/runs?fields=summary` rows of terminal
  runs now carry `outcome` (the terminal lifecycle status, else report.json
  `status`), `abort_reason` and `failure_reason` (`<abort_reason> |
  <failure_reason>` from report.json plus the supervisor's `failure_reason`,
  else a supervisor-generated report's `error`, else the worker's `Stopped:`
  line). Both reasons are ASCII-only and at most 200 characters. Live rows are
  unchanged (the keys are absent), so existing readers see the same shape.
  report.json is parsed once per finished run (memoized on mtime/size).
  Hydra's `list_fleet_runs` can now show why a run created outside the queue
  failed. New module `src/lh_harness/run_outcome.py`.

- **Experience layer (MSCE Phase 1).** Optional valued L1 trace persistence:
  one redacted JSONL record per managed round written to
  `role_orchestration/experience.jsonl` in the run dir at finalization,
  carrying a deterministic terminal reward split into goal/process/satisfaction
  terms (R = 0.45·goal + 0.30·process + 0.25·satisfaction), a per-round
  reflection weight from the independent audit, and Eq. 2 value backfill
  (γ = 0.9).  Off by default (`LH_HARNESS_EXPERIENCE=1` or
  `[run] experience = true`); when off, runs are byte-identical to previous
  builds.  New package `src/lh_harness/experience/`; documentation in
  [docs/experience-layer.md](docs/experience-layer.md).
- **Experience read-only API.** The three MSCE levels are viewable by the
  fleet surfaces through `GET /api/experience/environment` (L3 — the seeded
  fleet knowledge: hosts, standing constraints, routing backends, with
  `origin`/`seeded_at`/`source`/`superseded_*` per item),
  `GET /api/experience/policies` (L2 — empty but addressable, paginated,
  with the final item schema declared), and
  `GET /api/experience/runs/<run_id>/traces` (L1 — paginated, redacted
  again at serve time, read from the run-dir ledger through the same
  run-boundary checks as every run-scoped route).  Same bearer middleware as
  the rest of `/api/*`; read-only, no write endpoints.
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
- Service-side task queue with durable atomic JSON persistence under the runs root.
  - `POST /api/queue`, `GET /api/queue`, `DELETE /api/queue/{id}`, and priority endpoints.
  - Capacity-gated launcher with trio limits, workspace collision avoidance, and idempotent restarts.
  - Fleet MCP tools exposed under the LiteLLM alias `lhharness` (access group `fleet-runners`).
  - ASCII-only `harness_resolve_gate` validation and `/api/runs/{id}/approvals/{id}/resolve` wiring.
- End-to-end happy-path test for the local queue API.
  - `e2e/happy_path.sh` and `e2e/happy_path.py` start a local service, use a fake `codex` agent, and assert the run reaches `completed`.
  - Covers `queue.launched`, the inter-tick double-launch race, and `base_check` wiring.
- `Makefile` target `e2e-happy` and pytest marker `e2e` so heavy stack tests run separately from the main suite.
- Server-side caching for `/api/runs/{id}/snapshot`, cutting full snapshot p95 latency from ~37 ms to ~5 ms and summary snapshot p95 latency from ~37 ms to ~4 ms on a 16-round synthetic run.
- Documentation: `docs/queue.md`, `docs/ux-performance.md`, and `docs/release-checklist-ct110.md`.

### Changed

- Web API now embeds a service-side supervisor and launcher when run with `lh-harness web`.

### Fixed

- The auditor's read-only enforcement no longer depends on the ORDER of a
  `gh api` command's flags.  `gh api` parses its flags with Go's flag
  package, so the endpoint is not position-locked:
  `gh api -f body=x repos/o/r/issues/1/comments` is the same write as
  `gh api repos/o/r/issues/1/comments -f body=x`.  The deny rules for a
  field-supplying body were anchored on the path coming first, so in the
  flag-first ordering they matched NOTHING and the permission layer would
  have let the write through — the in-process classifier refused it, but the
  shell-out was refused by no rule at all.  Every body flag (`-f`, `-F`,
  `--field`, `--raw-field`, `--input`, `--input=`, `-i`) now has a rule for
  both orderings, and each spelling has its own named test asserting BOTH
  layers refuse it.
- The auditor's permission layer now covers the write verbs it had missed
  entirely: `gh pr lock`/`gh pr unlock` and `gh issue lock`/`gh issue
  unlock` (locking a conversation is a write), `gh gist rename`/`view`/
  `list`/`clone` (the gist group is a write group, so the permission layer
  denies all of it and the two layers agree), `gh project create`/`edit`/
  `close`/`delete`, `gh cache delete`, `gh config set`, `gh gpg-key delete`,
  and the write-only groups `gh alias`, `gh extension`, `gh org`, `gh team`
  and `gh sso` — denied as a GROUP, because a verb-by-verb list is a list
  that goes stale.  The canonical write-spelling list grows from 90 to 125
  entries, each with its own named test at both layers.
- Two invariant tests now guard the enforcement instead of asserting it once:
  one walks every body flag through every position it can take relative to
  the endpoint and requires both layers to refuse each, and one requires the
  in-process classifier and the permission layer to AGREE about which
  commands are writes — a write the classifier refuses but no deny rule
  matches is a hole in the permission layer, and a command a deny rule
  matches that the classifier calls a read is a read taken away.
- The `graphify` code-graph tool's local index (`graphify-out/`) is now
  ignored, so a local analysis run cannot commit it.

- The auditor's role instruction now names BOTH halves of the invocation an
  auditor needs: the command that resolves in the agent environment
  (`python3 scripts/auditor_verify_pr.py --repo <owner/name> --number <n>
  --reviewed-sha <sha>`) and the credential variable the verifier reads
  (`LH_HARNESS_AUDITOR_GITHUB_TOKEN`).  The instruction previously said
  "the auditor's dedicated read-only token" without saying which variable
  that was, so an auditor could not set it — and an auditor that guessed the
  name would have picked up the executor's write-capable `GH_TOKEN` instead.
  The variable NAME is not a secret and is stated in both the `en` and `zh`
  instructions; the token VALUE appears nowhere.  The executor's variables
  are named in the instruction only to forbid them.
- The auditor's read-only enforcement is now proven **per spelling, at both
  layers**, for the spellings a read allowlist would otherwise let through:
  `gh pr merge --admin`, `gh pr merge --rebase`, `gh pr merge --auto`,
  `gh api --method POST|PATCH|PUT|DELETE` (the space-separated long-flag
  spelling), and `gh api` with a write method AND a field-supplying body.
  Each has its own named test asserting the in-process layer refuses it AND
  the permission layer's deny rule MATCHES it.
- The G4 reachability tests no longer measure the test harness instead of the
  surface: they ran the verifier with a hard-coded `HOME` pointing at the
  repository root, which moved the user site-packages directory and made the
  harness's own dependencies unimportable, so the named invocation failed
  with `ModuleNotFoundError` before it ever reached the verifier.  The
  subprocess environment is now derived from the real one with the harness's
  own runtime variables stripped.
- Queue launches no longer burn an attempt on an ineligible auditor MCP
  profile (task 234).  An auditor role that would resolve to a non-read-only
  profile is refused on the pre-burn path: the exact supervisor reason
  (`roles.auditor.mcp_profile '<name>' is not read-only; auditor roles
  require a read-only MCP profile`) is appended to the queue entry's
  `skip_reasons`, the entry stays pending with its attempt count unchanged,
  and `mark_failed` is never reached.  The check honours the worker's
  precedence — an explicit `[run.roles.auditor] mcp_profile` in the project
  config wins over the trio's run-wide profile — so a deployment binds the
  auditor to a read-only profile (e.g. built-in `audit`) while the
  manager/executor keep a richer one.
- Race where two queue entries for the same workspace could be launched across concurrent ticks is now prevented by a launch lock and active-run re-check.
- `migrations/001_harness_queue.sql` now applies cleanly to a fresh database as
  a non-superuser role owner. It previously failed under `ON_ERROR_STOP=1` with
  `ERROR: default for column "status" cannot be cast automatically to type
  queue_status` because it created `status` as `VARCHAR NOT NULL DEFAULT
  'pending'` and only converted the column afterwards. The `queue_status` enum
  is now created first (guarded by a `pg_type` check so re-running is a no-op)
  and `status` is declared as that enum with the `'pending'` default directly
  in `CREATE TABLE`. `002_harness_queue_events.sql`'s foreign key is likewise
  guarded, so re-running either file on an already-migrated schema is a clean
  no-op. Covered by `tests/webapi/test_pg_migrations.py` (fresh apply +
  double-apply) and documented in `docs/queue.md` (Postgres backend and
  migration order).
