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
