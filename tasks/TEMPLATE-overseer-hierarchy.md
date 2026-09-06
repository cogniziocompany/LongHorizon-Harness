# TASK TEMPLATE — Overseer hierarchy (supervisor ⇄ repo-session agents ⇄ harness runs)

The three-tier supervision pattern proven live on 2026-08-30 across five concurrent
workstreams (hydra, fleet-runner, mcp-tools/LiteLLM, hivemind, powerplatform). Companion to
`TEMPLATE-litellm-qwen-local.md` (which templates a single run; this templates who watches it).

## The hierarchy

```
Paxton (human)
  └── OVERSEER session (LongHorizon-Harness repo — one per fleet)
        ├── repo-session agent A (owns repo A) ── creates + monitors repo A's harness runs
        ├── repo-session agent B (owns repo B) ── creates + monitors repo B's harness runs
        └── unassigned runs ─────────────────── monitored directly by the overseer
```

- **Every harness run is monitored directly by its repo's Claude session agent** (the session
  the human designates for that repo). The overseer monitors the *sessions*, not the rounds.
- Runs in repos with **no designated session** stay under direct overseer monitoring until
  the human assigns one.
- The overseer keeps its own run-monitor only as a backstop **until the repo session ACKs its
  monitoring plan**, then retires the duplicate.

## Tier responsibilities

| Tier | Does | Never does |
|---|---|---|
| Overseer | Cross-repo deconfliction, ordering, deploy windows, GPU/router capacity, gates that affect other repos, session audits, backfill-run creation for unowned gaps | Build in a repo that has an owner session; resolve another repo's *deploy* gates unilaterally |
| Repo session | Creates runs in ITS repo, polls their snapshots, injects pacing, resolves routine/loop-protection gates with rationale, reports completions with audit evidence | Resolve deploy/rollout gates (escalate); build large features by hand when a harness run was directed; touch another session's reserved paths |
| Harness run | One contract, one workspace, audited rounds | Deploy, reboot hosts, push, touch reserved paths |

## Addressing runs via the Hydra fleet plane (preferred since 2026-09-05)

Run creation/monitoring/intervention now has a single API instead of per-node
base URLs: Hydra's `/fleet/*` REST and the `hydrafleet` MCP alias
(`/fleet-mcp`, access group `fleet-runners`). It encodes this template's rules
as invariants: top-level `model` required, one run per working tree (409),
`cancelReasonAck` before resuming a cancelled run, a `rationale` on every
mutation (logged to the per-run activity feed the web UI shows), migrate =
the Step-6 successor pattern (stop-before-create, CONTINUATION task text),
device power = 501. MCP prompts `overseer-briefing`,
`run-management-playbook`, and `assign-repo-session` serve this doctrine at
call time. `claude --resume` (below) remains the channel for talking to
*sessions*; the fleet plane is the channel for runs.

## Addressing agents (hard-won)

- **`claude --resume <UUID> -p "<directive>"` from the repo's own directory is the reliable
  channel.** Find the UUID under `~/.claude/projects/<repo-slug>/*.jsonl` (newest = current).
  `SendMessage` by list-name can misdeliver — never assert a name↔session mapping you haven't
  verified; multiple sessions can work one repo.
- Headless resumes need the workspace **trusted**:
  `projects["<path>"].hasTrustDialogAccepted: true` in `~/.claude.json` — set it on EVERY
  capitalization variant of the path or it silently stalls with zero output.
- Long work inside a resume: have the agent `nohup` it to a log and exit; resume again to
  collect. A resume pass should stay under ~8 minutes of its own activity.
- A `--resume -p` fork runs CONCURRENTLY with any interactive instance of the same session:
  countermand/kill the fork if the human redirects the work (check the tree for stray edits
  after killing an `acceptEdits` fork).

## Assignment directive (send to the repo session, fill the ⟨⟩)

```
Supervisor directive (supervision hierarchy): YOU directly monitor the harness runs in your
repo; I monitor you.
1. MONITOR NOW: run ⟨run_id⟩ on ⟨workbench URL⟩. Poll GET /api/runs/⟨run_id⟩/snapshot;
   watch rounds, audit verdicts, gates. Handle yourself: pacing instructions
   (POST .../instructions), loop-protection gates (resolve continue + rationale when the
   cause is environmental). ESCALATE to me: deploy/rollout gates, contract drift, repeated
   organic failures.
2. WHEN IT COMPLETES: create the next run yourself in your repo workspace — POST /api/runs
   with top-level "model" set (REQUIRED or the workspace config leaks and the worker dies),
   roles ⟨trio⟩, the task text carrying: authoritative spec file to read FIRST, BUILD-ONLY /
   no-deploy/no-reboot boundaries, reserved paths of other workstreams, frozen contracts,
   branch name, pacing + auditor-no-fetch lines. Then monitor it the same way.
3. REPORT to me: completions with audit evidence, unresolvable gates, readiness for any
   batched deploy window. Acknowledge with your monitoring plan.
```

## Task-text rule (learned twice on 2026-09-05)
The harness worker sees ONLY the `task` string — never the task file's header, tables, or
"spec at the path above". Put the authoritative spec's absolute path (WSL form for WSL runs)
INSIDE the task text as its first sentence, plus the workspace path and branch. A run that
cannot find its spec will gate on "spec not found" and burn a round.

## Overseer duties that stay central

- **One run per working tree.** Two runs sharing a checkout collide on the git index; a
  second run for the same repo waits or the human explicitly approves a separate clone.
- **Deconfliction before launch**: ask the owner session what it has "in plan" — cancel one
  side explicitly (in writing) before a backfill run starts.
- **Deploy windows**: batch every action that restarts shared infrastructure (orchestrator
  netns/tunnel couplings, routers, runner services) into announced windows; list blast radius
  per host. Never let any tier reboot the host that carries the overseer/workbench.
- **Capacity routing**: local-GPU executors (qwen3.8) for the runs that justify it; cloud
  executors for backfill/overflow so runs don't starve each other.
- **Standard trio (2026-08-30, Paxton)**: manager `glm-5.3:cloud`; executor
  `kimi-k2.7-code:cloud`; auditor `kimi-k3:cloud`. glm-5.3-flash:cloud is live in the router
  as the pooled fallback/chat workhorse but CANNOT hold the Claude Code executor seat --
  deterministic "Content block is not a thinking block" (2026-08-31). minimax-m3 is retired from the manager seat: it repeatedly ignored
  operator gate resolves (asked the same question 4x through 4 consumed answers).
- **Session audits**: periodically resume each repo session for a status-vs-tasking report
  (per-item, with commit evidence); reconcile against the repo (`git log`, gap-list docs)
  rather than trusting prose; honest gaps get new runs, not blame.
- **Memory**: record assignments, reserved paths, ordering constraints, and deconfliction
  outcomes immediately — the hierarchy only works if the overseer's map is current.

## Escalation ladder (any tier → up)

1. Routine (pacing, env fixes, streak resets) — handle at the lowest tier that can.
2. Cross-repo impact (contract changes, shared-infra restarts, agent-fleet redeploys) — overseer.
3. Human-gated (prod deploys, host reboots, credentials, secret rotation, architecture
   decisions) — the human, with the evidence assembled and a recommendation.

## Run-management playbook (the escalation ladder, as executed live 2026-08-30)

Work each step in order; each is cheaper than the next. Record the step you're on in the run's
gate rationales so the next reader sees the history. Declare your escalation point BEFORE you
reach it ("if the next organic failure happens under X, I stop auto-continuing") — and honor it.

### Step 0 — Diagnose before touching anything
A timeout, a gate, or a "violation" each have several causes that pattern-match to the same
symptom. Read the round's trajectory (steps done, last tool call, surviving disk state),
`harness_feedback`, and the auditor's wrapped report before choosing a remedy. Examples of
same-symptom/different-cause caught live: a "cancelled" run that was a deliberate replacement
by its owner (do NOT resume it); an audit "violation" that was the auditor's own `git fetch`
writing `.git/objects`; a CI "advisory suite failure" that was an external cancel at 5 minutes
against a 25-minute budget; a config raise that silently no-oped over a hung SSH session
(verify remote edits by reading the file back).

### Step 1 — Pacing instruction (cheapest)
`POST /api/runs/{id}/instructions`: "one component per round, inventory surviving disk work
first, each round completable well under 45 minutes." Reaches the manager at the NEXT round
boundary — a round already dispatched still runs under old guidance.

### Step 2 — Hard step budget with stop-early semantics
Wall-clock pacing fails when throughput collapses (GPU contention: 60–120s/step). Convert to:
"under N tool steps; at ~N-10 STOP EARLY, commit what is coherent by explicit path, report
where you stopped. An honest partial PASSES the round; running out the clock FAILS it and
loses the transcript." Give managers the measured s/step so they size slices in steps.

### Step 3 — Raise the round timeout + cycle
Workspace `config.toml` `[run.timeouts]` up (3600→5400), then `POST /stop` + `/resume
{"mode":"continue"}` to load it (config reads at worker start; audited progress survives).
VERIFY the file changed and the next failure duration matches the NEW cap — a failure at the
old cap means the raise never applied.

### Step 4 — Fix the environment, not the run
When several runs degrade together, the cause is shared infra. Live examples: idle model pinning a
GPU so the span lane couldn't spread; `NUM_PARALLEL=1` serializing all executors; router
fallback chains reloading the very model that pinned the GPU (self-reinforcing — break the
chain, not the symptom). Also load-shedding: pause the lowest-priority run (record that the
"cancelled" status is YOUR operator stop, and the resume condition).

### Step 5 — Gate handling
- Loop-protection gates: resolve `continue` WITH a rationale naming the diagnosed cause and
  the mitigation now in place. If your own intervention caused part of the streak (a kill to
  cycle config), say so in the rationale.
- Completion gates ("Task complete. Continue?"): verify the final response + audit evidence
  first. Resolve `continue` only to extend into genuinely remaining scope; resolve
  stop/end when the contract is met and the remainder is human-gated. A completion gate on
  ANOTHER session's run is its owner's decision — leave it, notify them.
- Deploy/rollout gates: never resolve unilaterally; batch into announced windows.

### Step 6 — Replace the run (declared escalation point)
When the executor model itself cannot finish rounds under every prior step (live: qwen3.8 on
CT110 timed out rounds 8–11, 13, 16–21 across both 3600s and 5400s caps, ignoring step-budget
directives), stop the run and CREATE A SUCCESSOR instead of iterating further:
- Successor task text starts "CONTINUATION RUN (successor to <id> — do not restart from
  scratch)" and enumerates the AUDITED state it inherits (commit ids, surviving uncommitted
  edits and what to do with them first), the remaining scope, and all original constraints.
- Swap the failing seat (executor → kimi-k2.7-code:cloud) and keep the rest of the trio.
- Stop the predecessor BEFORE the successor's first executor round (shared workspace — two
  live workers in one tree collide). If a gate-resolve errors, `POST /stop` is the reliable
  fallback.
- Re-point monitors at the successor id; retire the old watch.
- Precedents: hydra 44b0f23c → aec75d88 (completed in 3 rounds); sms-door 6a0c591b →
  cf5e7362.

### Step 7 — Hand to the human
Credential blockers, prod targets that don't exist, architecture decisions, secret rotation,
host reboots, and anything where two prior steps failed for reasons you cannot explain.
Arrive with evidence and a recommendation, not a question.

### Standing rules that cut across all steps
- Round-budget exhaustion: eval audited progress; default = extend via `/resume
  {"mode":"continue"}`; notify instead when looping/drift/blocker (post the eval either way).
- Never resume a `cancelled` run without learning who cancelled it and why (`created_by:
  "web"` = a human clicked Stop; an owner may have deliberately replaced it).
- Instructions and resolves are consumed at worker safe points — "accepted" ≠ applied;
  confirm consumption in the next round's plan text.
- Monitors: change-only output; anchor terminal-state matches (`"status=failed"*`, not
  `*status=failed*` — substring globs match `rolestatus=failed`); pipe snapshots via stdin
  (argv breaks past ~128KB); treat `waiting_approval` as non-terminal.
- Every intervention gets one line in memory immediately: what, why, evidence, undo path.
- **Human-in-the-loop check before ANY stop or gate-resolve**: read `operator_messages` and ask
  whether the latest round was human-initiated. A gate raised while a human is conversing with
  the run in the workbench is THAT HUMAN'S gate — a repeated "completion ask" and a human
  conversation look identical from the status line (live incident: 32a50753 stopped mid-chat
  with the operator; recover with `/resume {"mode":"continue"}` — the ledger survives).

### Addendum — the deploy ask-loop (learned on run 009f6396)
If a task's authoritative spec includes a deploy step but the run's constraints gate it, write
the task so the CONTRACT'S TARGET STATE ends at the build boundary: "completion = the audited
branch with green tests; the deploy is explicitly OUT OF SCOPE and operator-executed later."
Otherwise the manager can never satisfy its completion rule and will raise the same
"authorize the next step" ask every round regardless of your answers — the only clean close is
an operator stop with a rationale marking it complete-at-boundary (not a failure).

## WSL worktrees for harness runs (learned 2026-09-05, broker run 2a38855a)
- Create the run worktree FROM WSL (`git worktree add -b <branch> /mnt/c/... origin/<base>`) so the worker's git works, then IMMEDIATELY
  `git worktree lock /mnt/c/... --reason "lh-harness run <id> (WSL paths; do not prune from Windows)"`.
- Windows git cannot resolve the `/mnt/c/...` gitdir pointer, lists the worktree as `prunable`, and `git worktree prune` / `git worktree remove`
  run from Windows DELETES the live worktree's admin entry. The worker then sees "not a git repository", and a resourceful executor falls back
  to committing through the main checkout with `--git-dir/--work-tree` — its commit lands on whatever branch the main checkout has out
  (this put msal-broker work on `ci/qa-gate-uat-prod`). Locked worktrees are exempt from prune.
- Never run `git worktree prune`/`remove` from Windows git in a repo that has WSL worktrees. Clean up finished worktrees from WSL only.
- Repair if it happens: recreate `.git/worktrees/<name>/{gitdir,commondir,HEAD}` (gitdir = `/mnt/c/.../<wt>/.git`, commondir = `../..`,
  HEAD = `ref: refs/heads/<branch>`), then `git reset -q` inside the worktree from WSL to rebuild the index (files untouched), re-commit the
  stray commit on the right branch by explicit path, `git reset --mixed` the main checkout back, and send the run an instruction naming the fix.
- Task text for every run: "never cd into or run git against <main checkout>; plain git commands only (never --git-dir/--work-tree)".

## Launch payload keys (verified against src/lh_harness/webapi/server.py, 2026-09-05)
- POST /api/runs reads `roles` (per-role `{agent, model}`) and `max_rounds`. Unknown keys such as `role_configs` or `rounds` are SILENTLY
  ignored: the run then executes every role on the top-level `model` with the default 25 rounds. After every launch, confirm with
  `pgrep -af 'lh_harness run'` that `--manager-model/--auditor-model/--max-rounds` are present. (Run 2ff4e39d ran all-kimi for this reason.)
- Resume (`POST /api/runs/{id}/resume`) takes only `mode` (continue|retry) and `extra_rounds`; it cannot change models. To change a role model,
  launch a NEW run on the same worktree with a CONTINUATION paragraph naming the commits already on the branch.

## glm-5.3:cloud as manager — known failure (2026-09-05, broker run 2a38855a rounds 7-8)
- Symptom: the manager phase fails with `API Error: Content block is not a thinking block` once its prompt grows past ~43 KB; LiteLLM logs 200 OK
  (relay 192.168.21.110:11438, no fallback fired), so it is the streamed thinking/signature deltas of glm-5.3 that Claude Code rejects, the same
  mechanism that disqualified glm-5.3-flash for the executor seat. Resume re-fails immediately (same prompt).
- Mitigation used: manager `kimi-k2.7-code:cloud` (executor model; handles far larger prompts through the same relay), auditor `kimi-k3:cloud`.
  Paxton set the glm-5.3 manager standard; report the deviation and let him confirm or pick another thinking-free manager.

## Repo-level Claude hooks poison harness audits (learned 2026-09-05, run 13ccf895)
- cognizioware-powerplatform and cognizioware-mcp-tools commit `.claude/settings.json` hooks that run `kb-article/kb-hook.py` on PostToolUse/Stop.
  Inside a harness run every manager/executor/auditor `claude --print` phase fires them: the hook wrote `kb-article/kb-hook.log` into the
  workspace (the auditor's integrity check then voids EVERY audit as "workspace mutated") and filed a `[session-end]` KB article per phase.
  Run 13ccf895 burned all 25 rounds on `blocked/violation` with finished code on the branch.
- Fix shipped (harness 375778c; worktrees 51c6b36 ops, 11faf4b broker, 19e9072 mru, aac0d46 ci/env-promotion): kb-hook.py exits when
  `LH_HARNESS_CLAUDE_ROLE` (set by the harness on every phase) or `KB_HOOK_DISABLE` is present and logs to `~/.claude/kb-hook.log`.
- Before launching on a repo: `grep -n hooks .claude/settings.json` in the workspace; any hook that writes inside the repo must be guarded the
  same way (the powerplatform repo also has a graphify post-commit hook writing `graphify-out/.hook.log`).
- When a run audits `blocked/violation` for 3+ consecutive rounds with commits landing, read the auditor's integrity reason before spending rounds.

## Ollama Cloud pool: why it never engaged, and the `:pool` model groups (2026-09-05)
- Prod LiteLLM (CT202) had `routing_strategy: least-busy` in the DB `router_settings` (DB overrides YAML). A lane that fails instantly
  (quota 429 → APIConnectionError, which never triggered cooldown: `Cooldown Deployments=[]` all day) always looks least busy, so 99.9% of
  Claude Code traffic pinned to the ptait01 relay (identity prax211) until prax211 hit its session usage limit; the direct `openai/`
  deployments (KEY_1/2/3) got 2 of ~5,200 selections. Switched the DB setting to `simple-shuffle`.
- Direct lanes that the anthropic `/v1/messages` bridge actually uses: `ollama_chat/<model>` with `api_base: https://ollama.com` and
  `api_key: os.environ/OLLAMA_CLOUD_KEY_n` (ollama_chat sends the bearer). Added DB model groups `kimi-k2.7-code:pool`, `kimi-k3:pool`,
  `glm-5.3:pool` (KEY_2 ai-dev01 + KEY_3 ai-dev02, rpm 40 each; KEY_1 prax211 to be added when its window resets), plus a KEY_2 lane in
  `kimi-k2.7-code:cloud`. Virtual keys have model allow-lists: `lh-harness`, `hive-mind-contact-memory`, `hive-eval-run-20260818` were
  extended with the `:pool` names (3 keys under the missing `librechat` team could not be updated).
- Harness standard until Paxton says otherwise: manager `kimi-k2.7-code:pool` · executor `kimi-k2.7-code:pool` · auditor `kimi-k3:pool`.
  These live only in the LiteLLM DB — port them into `infrastructure/litellm-config.yaml` (repo) so a deploy does not lose them.
- Diagnosis commands: `/model/info` (deployments per group), router log `get_available_deployment ... api_base` counts, `/v1/messages` probe
  with the master key, direct `https://ollama.com/v1/chat/completions` per key for quota.

## Deploy authority (revised 2026-09-05)
Paxton: prod deploys/flips through the repo pipelines are auto-approved — no per-deploy "go". Gates still decide (stop on red), rollbacks
stay documented, and the hard constraints remain (no ptait09 reboots, no bare `docker compose up`, no `git add -A`, no secrets in docs).

## "Green" lanes that deploy nothing (learned 2026-09-05, mcp-tools run 33983953419)
- A step that loops `while read … < file` and calls `ssh`/`scp` inside consumes the rest of the file on the first iteration: only the
  first build context was ever synced/built. Always `ssh -n` / `scp … < /dev/null` inside read loops.
- `cmd 2>&1 | tail -N` returns tail's status: without `set -o pipefail` a failed `docker compose up` still exits 0 and the job goes green.
- Verify a deploy on the box, not from the job colour: `docker ps` for the new services, `docker images` for the new tag, public `/ping`.
  The ops lane was green while Caddy returned 502 (`lookup ops-oauth2-proxy: no such host`). Fixed in mcp-tools #66.
- Shell hygiene for the overseer itself: background/tool commands reset cwd; use absolute paths or `git -C <repo>` for every git command.

## Promotion-pipeline gate lessons (pp promote.yml, runs 6-9, 2026-09-05 evening)
- **A job-level `permissions:` grant does not reach jobs that have no block.** Run 6's secondary gate polled commit statuses and got 403 "Resource not accessible by integration"; adding `statuses: read` to the three deploy jobs (#51) fixed nothing because the poll runs in the secondary-gate jobs, which inherited the restricted default. Fix = workflow-level `permissions:` (#53): contents/packages/actions/statuses read.
- **A CE QA dispatch needs `env_id` and the right profile.** `vars.QA_ENV_ID_DEV/UAT/PROD` were unset → the QA run failed validation in 1 s and posted `score=0 suites=0/0`. The ids live in the cognizioware-qa README "CE environment ids" table (set 2026-09-05). The default `trms` profile scores ~26 on the bare powerplatform orgs purely from drift; gate them with `profile=greenfield` (#55). The ce job only fails when the checkpoint-suite step itself fails, and the suite exits 0 on failed checks, so greenfield can pass.
- **The QA backend for `[self-hosted, qa-host]` is Docker on ptait09** (`cognizioware-qa-backend-1` :8400 + `cognizioware-qa-postgres-1`). Both were `Exited (255)` after a Docker Desktop restart with no restart policy → every CE gate would time out. Restart policy set to `unless-stopped`; check `curl localhost:8400/health` before dispatching a gate.
- **Image race is a real state, not a flake.** A merge to develop changes the sha the promotion deploys; `Deploy to dev` fails with "no matching manifest … sha-<new>" until the develop CI image build finishes. Wait for the CI run on that sha, then dispatch (promo9-chain.sh pattern).

## Owner-session pings need the FULL session UUID
`claude --resume 6803faad -p` fails ("not a UUID and does not match any session title"). Resolve it first: `ls ~/.claude/projects/*/6803faad*.jsonl`. ListAgents names never map to these UUIDs.

## Answering a manager question through the API (learned 2026-09-05, run d96ca9e6)
`POST /api/runs/<id>/approvals/<approval_id>/resolve` with `{"action":"continue","user_input":"<answer>"}`. The answer field is **`user_input`** — `answer` is silently ignored and the manager re-asks the same question next round. Verify with the snapshot: the approval's `user_input` must show your text. Also: files copied into a WSL worktree from Windows are visible immediately, but tell the manager the exact `git status` line so it stops searching.

## Promote by sha, not by branch name (learned 2026-09-05, run 13 + pp #59)
promote.yml checks out `inputs.ref` in EVERY env job and tags the image `sha-<HEAD>`; dispatching with `ref=develop` means uat/prod can deploy a NEWER
develop sha than dev tested if anything merges mid-run (a docs-only merge still moves the sha and needs its CI image). Dispatch with the full sha
(`-f ref=<40-hex>`), and never merge to develop while a promotion is between dev and prod. Pipeline fix pending: resolve the sha once in a first job.

## Commit-status polls must be freshness-bound (learned 2026-09-06, pp promotion run 13)
Run 13 passed both dev gates, yet the score gate read `score=0`: the poll took the NEWEST `cognizioware-qa/ce-dev` status on `github.sha` (the workflow
ref, main) with no regard to when this run dispatched, so it matched a stale status from an earlier attempt while its own gate posted 100 a minute
later. Fix (#60/#61): `caller_sha` and the poll both use the deployed HEAD (`git rev-parse HEAD`), statuses older than the poll start (−3 min) are
ignored, newest match wins. General rule: any "dispatch then poll a shared status" step needs a dispatch timestamp or a run-specific correlation id.

## Promotion approval watcher must outlive the eval gates (learned 2026-09-06, run 16)
A full dev+uat pass takes ~3.5 h (two 150-min-capped eval gates); promo7-watch.sh's 400×30 s loop expired before the production gate appeared and
printed "timeout". Size the watcher loop for ≥ 6 h, or watch `pending_deployments` with a long fallback. Approve with
`gh api -X POST repos/<r>/actions/runs/<id>/pending_deployments --input <file.json>` (process substitution does not work from git-bash on Windows);
the response is a list of strings, so don't pipe it through an object jq. Do not merge to develop while a promotion is between dev and prod unless
the run is pinned to a sha AND its dev stage is already done (a dev-lane redeploy mid-eval disturbs the gate).

## Shared concurrency groups cancel pending promotion gates (learned 2026-09-06, run 16)
eval-gate.yml used one `concurrency.group: eval-gate` for its schedules AND for the promotion's workflow_call gates. GitHub keeps ONE pending run per
group: when the 08:00 PT prod schedule queued behind the in-progress 07:00 uat schedule, it evicted promotion run 16's pending prod gate ("cancelled",
no actor). Fix #68/#69: group per run + tier. Recovery: cancel the queued schedule, `gh run rerun <id> --failed` (re-runs the cancelled job and the
skipped downstream jobs on the ORIGINAL workflow files). Avoid promotions whose prod stage lands in the 06:00–09:00 PT schedule window until then.

## Zombie-process storm on ptait01 (incident 2026-09-06 ~08:30–09:15 PT)
Symptom: SSH MCP gateway, LiteLLM and billing all timing out; ptait01 load 334 with no hot process; `ps -eo comm | sort | uniq -c` showed 3,092
`timeout` entries, all `<defunct>`, parented by the litellm processes in CT204 (1,574) and CT202. LiteLLM spawns `timeout` helpers for MCP/health
probes and, with no init as PID 1, never reaps them. Restarting the CT204 litellm container took the load from 334 to 51 within 20 s.
Fix: cognizioware-mcp-tools #75 — `init: true` on litellm-router, doctor row "Compose stack zombie processes" (socket proxy `/containers/{id}/top`,
warn >5 / fail >50), e2e suite 33 asserting the row is green. Diagnose with a DIRECT `ssh root@<pve>` from Windows (BatchMode works for
ptait01/ptait07/corsairai300) — the MCP gateway path is itself a casualty when LiteLLM is sick.
- Follow-up (2026-09-06 09:55 PT): the mcp-tools lane's "Apply LiteLLM config to UAT CT204" job pushes only litellm-config.yaml; CT204 keeps its OWN
  docker-compose.yml under /opt/cognizioware-mcp-tools, so compose-level fixes (like `init: true`) must be applied there by hand (done; backup
  docker-compose.yml.bak-init-*). Zombie counts after the fix: CT202 ~0, CT204 recreating. Doctor row live: "Compose stack zombie processes ok=True".

## CI runners live on CT210 (ptait07), not the dev PC (2026-09-06 09:10–11:20 PT)
Paxton's rule: dev boxes run only active local dev tests and explicit exceptions; every scheduled/PR/promotion/e2e/eval/QA job runs on PVE runners.
CT210 `ci-runners` (192.168.21.170; Debian 12, Docker, Node 22, pwsh 7.5; user `runner`) hosts `ct210-pp` (self-hosted,Linux,X64,lan-deploy) and
`ct210-qa` (self-hosted,Linux,X64,qa-host) as systemd services, plus the QA backend compose (/opt/cognizioware-qa/repo, override 8400→4000, data
migrated from ptait09). Needed on the runner: `/home/runner/.ssh/id_ed25519_proxmox` (deployments/ssh/config), Playwright `--with-deps`, QA health at
`/api/health`. Proof: the pp prod eval gate ran end-to-end on ct210-pp (15/16; the one failure is the product's multi-webhook-crud QA verdict in
n8n, not the runner). Remaining Windows-ism in pp workflows: the deploy step's `"C:\Program Files\Git\bin\bash.exe" scripts/deploy/rollout.sh` —
replace with `bash scripts/deploy/rollout.sh` before the next promotion. New LXCs on any PVE host inherit the host's Tailscale resolver: write
/etc/resolv.conf (Pi-hole 192.168.21.3 + 1.1.1.1) before apt. `pct set --nameserver` does not change a running container.

## Ollama Cloud quota exhaustion kills every run at once (2026-09-06 17:51–17:57Z)
Five parallel CT110 runs on the `kimi-k2.7-code:pool` trio drained all three Ollama Cloud accounts (prax211, ai-dev01, ai-dev02: "reached your session
usage limit"); the :pool groups have no non-Ollama fallback, so all runs failed with `litellm.APIConnectionError … No fallback model group`. Rules: at
most TWO concurrent :pool runs; before launching, probe quota with a 4-token chat per key (ollama.com/api/chat); when a run fails with this message,
do NOT relaunch — wait for the session window to reset (probe hourly) then `POST /api/runs/<id>/resume {"mode":"continue"}` on the failed runs two at
a time. Ask Paxton before buying extra usage.

## Local qwen3.8 executor: use Ollama's OpenAI-compatible endpoint (learned 2026-09-06, run 86cd5320)
With `ollama_chat/qwen3.8:27b` behind LiteLLM, Claude Code's tool-result turns came back 500 "no user query found in messages" from Ollama and the
executor looped on api_retry until the episode failed (no LLM traffic, GPUs idle — looks like a hang). Direct `/v1/chat/completions` with tool
messages worked, so the qwen3.8 deployment is now `openai/qwen3.8:27b` at `http://192.168.21.110:11442/v1`, and the 64k context is pinned with
`OLLAMA_CONTEXT_LENGTH=65536` on the ptait01 `cognizioware-ollama-span` container (both RTX 3090s, `OLLAMA_SCHED_SPREAD=1`, 12.3 GB each) because
`/v1` cannot pass num_ctx. Verify a run is alive by `curl :11442/api/ps` (model + ctx 65536 + VRAM) and `nvidia-smi` utilization, not by the run's
status. Langfuse: the per-key public/secret pair stored in LiteLLM returned 401 on the public API — the read keys need re-issuing before a Langfuse
review is possible; LiteLLM spend logs by date window are the fallback evidence.
- Addendum (15:15 PT): the qwen3.8 executor also fails through the OpenAI-compatible route once Qwen3 *thinking* output is present — LiteLLM's
  Anthropic adapter raises "Content block is not a thinking block" (same bug that killed the glm-5.3 manager). Manager rounds survive (they are
  single-shot); executor rounds die on the first tool turn. A `/no_think` model variant (`qwen3.8-nothink`, Modelfile SYSTEM "/no_think") was created
  on the span server but could not be loaded next to the resident 27b model (GPU1 20 GB, host RAM 37/43 GB) within 8 minutes. Verdict: local qwen3.8
  is usable for MANAGER/AUDITOR roles with a 900 s auditor budget (workspace `.lh-harness/config.toml` `[run.timeouts]`), not for the executor until
  LiteLLM's thinking-block handling is fixed or a non-thinking local coder model is loaded. The per-workspace config path is what web-launched runs read.

## Local model policy (Paxton, 2026-09-06 16:00 PT)
Locally we use **qwen3.8 and newer only**, unless an explicit exception is granted. `qwen3-coder:30b` was pulled as a one-off diagnostic and is not a
lane. Consequence: the local lane needs the LiteLLM thinking-block/tool-turn normalizer (task litellm-thinking-fix-2026-09-06.md) before qwen3.8 can be
an executor; until then qwen3.8 is manager/auditor only (600/900 s budgets) and executors run on the kimi cloud pool (two concurrent runs max).
Capacity decision: renting GPU compute is 15–150x the cost of the Ollama subscriptions for our volume (8×H100 ≈ $11.5k/mo; GLM-5.3-Flash is a 320B MoE
needing ~306 GiB FP8); Paxton is adding Ollama capacity instead (Max or a 4th Pro).
- 16:30 PT: Paxton added a 4th Ollama Cloud key (alias `litellm-cognizioware`) → stored as `OLLAMA_CLOUD_KEY_4` in CT202 `mcp-tools.env` (effective at the
  next router recreate) AND added immediately as a 4th DB deployment on `kimi-k2.7-code:pool`, `kimi-k3:pool`, `glm-5.3:pool` via `/model/new` (no
  restart). Concurrency cap raised to THREE `:pool` runs; the quota probe now checks 4 keys and resumes when ≥3 answer. The :pool groups remain
  DB-only (todo: port into infrastructure/litellm-config.yaml with `os.environ/OLLAMA_CLOUD_KEY_1..4`).

### Lesson (2026-09-06 16:45 PT): every repo with a `lan-deploy` job needs its own runner registration
GitHub self-hosted runners are per-repo (no org-level runner group here). Deregistering the PC runners left mcp-cognizioware's deploy job queued
forever (`self-hosted,lan-deploy`, no runner) — the earlier note "mcp-cognizioware needs no self-hosted runner" was wrong; only its build/image jobs
are ubuntu-latest. Fix recipe (CT210, user `runner`): registration token via `gh api -X POST repos/<org>/<repo>/actions/runners/registration-token`,
`tar xzf ../actions-runner-linux-x64-2.337.0.tar.gz` into /home/runner/<name>, `./config.sh --unattended --url … --token … --name ct210-<name>
--labels lan-deploy --replace`, `./svc.sh install runner && ./svc.sh start`. Then grep the repo's deploy step for pwsh syntax ($LASTEXITCODE, $env:,
.Substring, bash.exe) and convert to `shell: bash` before the first run. Runners today: ct210-pp, ct210-qa, ct210-billing.

### Incident (2026-09-06 16:52 PT): mcp-tools #77 rewrote 562 files with CRLF — reverted by #78 within 3 minutes
Cause: `gh repo clone` on Windows with global autocrlf=true, then `git config core.autocrlf false` WITHOUT `git reset --hard` → every
checked-out file (CRLF) now differed from the index (LF) and `commit -a` swept them all in. I only saw the 563-file stat in the merge output
because `pr create` and `pr merge` were chained in one command. Rules from now on: (1) clone with `git -c core.autocrlf=false clone …` (or
config + `reset --hard` BEFORE touching files); (2) never chain `pr create` + `pr merge` — print `git diff --stat origin/<base>` and require the
expected file count first; (3) prefer editing on CT110/WSL for repos that auto-deploy on main (mcp-tools "Deploy MCP Tools Stack" runs on every
main push: E2E gate → CT204 config apply + LiteLLM restart → QA gate → CT202 deploy). The CRLF run was cancelled mid QA-gate (CT204 had already
taken the CRLF config, harmless; the fix run re-applies LF). Main is now pre-#77 + the intended single-file change (verified `git diff --stat`).
