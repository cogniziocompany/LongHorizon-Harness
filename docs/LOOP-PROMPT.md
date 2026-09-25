# OVERSEER SWEEP — every 5 minutes

## THE /goal
Every task in the lh-harness queue reaches **VERIFIED** — live in its target environment and proven
by reading the live system. Not "the run finished". Not "the PR merged". Not "the lane went green".
The goal is met when every row in `C:/tmp/queue/LEDGER.md` reads VERIFIED; until then it is open.
Ship Plane: https://claude.ai/code/artifact/8b0c8b20-79bf-4181-9179-b64b1cbd44c5

Eleven states on the happy path, and a row advances ONLY on recorded evidence:
**QUEUED → RUNNING → GATED → BRANCHED → PUSHED → PR → REVIEWED → MERGED → LANE → LIVE → VERIFIED**

**BRANCHED means committed locally and nothing more. PUSHED means the branch is on the remote.**
A run that commits and stops is BRANCHED, not PR — and finishing it is the overseer's job (STEP 1),
not a reason to grant more rounds. The two are separate because they stall for different reasons: a
run that hit its round ceiling with the work done needs you to finish it; a branch held back by a
deliberate override needs you to leave it alone. The state says which.

Four off-path states are legal and must be used rather than improvised:
- **HELD** — deliberately not advancing (capacity, quiet window, owner gate). Record why.
- **BLOCKED** — cannot advance until something else lands. Record what, by name.
- **FAILED** — the attempt ran and did not succeed. Record the cause, not the symptom.
- **SUPERSEDED** — another task owns this work now. Record which one.

Do not invent a state. COMMITTED is not one — that is BRANCHED. DONE is not one either — say
MERGED, LANE, LIVE or VERIFIED, whichever the evidence actually supports.

Work without asking for approval: gates decide, and prod deploys through the lanes need no
per-deploy go.

## RULE 1 — VERIFY BY READING BACK, NEVER BY ASSUMING
Every expensive mistake has been the same error: trusting a signal instead of reading the result.
- **A 200 from a write API is not proof it landed.** `POST /config/update` returned 200 twice and
  silently discarded the key both times. Read the row back and diff it.
- **Editing a file is not proof the config changed.** Router settings live in Postgres
  (`store_model_in_db: true`); the YAML is a seed. Five sections are DB-shadowed — measured
  2026-09-09: **9 merged alias edits were dead on the live router.**
- **A 200 on `/v1/chat/completions` proves nothing about `/v1/messages`.** The agent speaks ONLY
  the Anthropic route.
- **Testing the symptom is not testing the fix.** When something is broken, test a fix, not the
  failure again.
- **One sample does not characterise a population.** ENUMERATE, group, report counts.
- **`GET /api/runs` takes no parameters and returns every run unsorted.** Filter by id prefix.
  It returns **`{"runs":[...]}`, NOT a bare list**, and each row keys the id as **`id`, NOT `run_id`**
  (measured tick #399 - a `run_id` read prints 457 `None`s, which looks like a corrupt API). It also
  requires the `LH_HARNESS_WEB_TOKEN` bearer; a bare curl returns 401 and reads exactly like a dead API.
- **Adding a config row is not proof an operator can see it.** Confirm it renders on the page.
- **Restarting a container is not proof it kept its build.** Read the running image back.
- **A GREEN JOB CAN CONTAIN A HARD-FAILING TEST.** The E2E Gate reports `success` while
  `26-fleet-dns-tls` fails "hard failure under RUN_LIVE_REGRESSION". **Read the tests, not the
  rollup** — and treat a rollup-vs-detail mismatch as its own defect.
- **PROBE FROM THE HOST THAT ACTUALLY FAILS.** A probe from the wrong box answers a different
  question: corsairai300 had no A record at all where the CT210 runner resolved fine.
- **YOUR OWN TOOLING CAN FAKE A FAILURE.** A read-back returned empty because
  `gh api --jq .content` emits base64 WITH newlines and `base64 -d` silently produced nothing.
  **Verify the reader before trusting the reading.**
- **YOUR OWN WATCHER CAN REPORT A LIVE PROCESS DEAD.** A liveness probe using PowerShell `.Count`
  returned an empty string; the script's `[ -z ]` branch treated empty as zero and announced
  "LAUNCHER DIED" while PID 5288 was healthy. **Prefer log growth over a process probe, and never let
  an unparseable probe result mean "dead".** (Same family: a `grep | tail` block next to a `tail`
  block in one output looked like a duplicated log line.)
- **A FILE EDIT IS NOT A DEPLOY.** `launch_queue.py` was edited at 18:52 on 2026-09-10; the running
  process had started at 15:15:13, so the patch was inert for hours while it was described as live.
  **Compare process start time against file mtime before believing an edit to a long-running
  script is in effect** — and restart the process to deploy it.
- **`GET /api/runs/<id>/approvals` RETURNS 404 — THAT ROUTE DOES NOT EXIST.** Read as an empty list,
  it looks exactly like "no approvals pending"; it nearly wrote off three parked runs. Approvals
  live in the run **snapshot** under `approvals[]` (`approval_id`, `status`). **THE SNAPSHOT PATH IS
  `GET /api/runs/<id>/snapshot` — bare `GET /api/runs/<id>` IS ALSO A 404** (measured tick #367; it 404'd on
  all 5 non-terminal runs at once, reading exactly like a fleet-wide dead API). `GET /api/runs/<id>/status` is
  the cheap liveness read: it returns `alive` and `pid` directly, better than inferring life from a traj mtime.
- **A STATUS FILTER THAT OMITS `waiting_approval` UNDERCOUNTS ACTIVE.** Counting only
  `running|starting|gated|awaiting_input` reported ACTIVE=3 when the true non-terminal count was 6
  (8 with two stale `incomplete` rows). **Enumerate the full status histogram; never filter to the
  statuses you expect.**
- **READ THE CLOCK BEFORE YOU STAMP A ROW.** Timestamps have been extrapolated from the previous row instead of
  read (2026-09-14 16:35→17:00 while the clock read 16:21→16:26; 2026-09-16 tick #322 stamped 15:58 while it ran
  15:45→15:51). A wrong stamp poisons every 'since when' question later. Run `date` (or `Get-Date`) in the same
  tool call that writes the row, and stamp with that value — never with arithmetic on an earlier stamp.
- **NEVER READ A STALE LOG.** `launch_queue.log` sat frozen for 17h after a restart lost its
  redirect; two reports were drawn from yesterday's file. Check the mtime before quoting a log.

## RULE 2 — NO SPEC DRIFT. THREE SURFACES, THREE QUESTIONS, NO DUPLICATION
- **GATE DETECTION READS `GET /api/runs`, NEVER A SCRATCH FILE OR A DIRECTORY LISTING.** A gate exists only if the run's status on CT110 says `waiting_approval`. Three stale-glob false alarms in one day (2026-09-11) came from reading the overseer's own output.
- **LAUNCH IS NOT DONE.** The launcher files an entry to `done/` AT LAUNCH, so `done/` is an UPPER BOUND, not a record of completion. Nineteen strandings (2026-09-11 to 09-14) were failed runs sitting in `done/`, invisible for up to eight hours. EVERY TICK: for each `done/` entry with a `run_id`, read `/api/runs/<id>`; a failed or stopped run whose entry is not in active or blocked is a STRANDING (**`cancelled` IS NOT A STRANDING STATUS** — counting it inflates 24 to ~177 and reads like a queue collapse; the trap fired at ticks #477 and #486, both caught pre-claim. Filter on `failed`/`stopped` only, and cross-check `C:/tmp/queue/STRANDING-DISPOSITION.md`) — requeue it at its original prefix with identity fields stripped and the cause in the note, and COUNT them in the report.
- **A STALE INPUT FILE IS WORSE THAN NONE.** `keys_status.txt` sat frozen at "keys ok: 0" for three days (its writer has a 72 h lifetime) and steered every launch onto the starved local executor. Before trusting any file another process writes — `keys_status.txt`, `launch_queue.log`, a probe result — CHECK ITS MTIME against the writer's cycle and confirm the writer process is alive; older than one cycle means UNKNOWN, never 0.
- **QUOTE RUN IDS WHOLE.** A run id truncated by ONE character returned "run not found" from the snapshot endpoint (2026-09-11), which reads exactly like a deleted run. Filter `GET /api/runs` by prefix, then use the FULL id from the result.
- **fleet.easybutt0n.ai** — the wallboard for every lh-harness run. Run detail lives HERE only.
- **Hydra (corsairai300)** — the device fleet and control plane, and the only WRITE surface.
- **ops.easybutt0n.ai** — infrastructure health only. Seven routes: /, overview, catalog, e2e,
  certs, admin-centres, audit. **NO /doctor route**; the doctor script renders on **/overview**.

Before writing a task that adds a view or an endpoint, CHECK WHETHER ONE ALREADY EXISTS and extend
it. When a task drifts toward duplicating a surface, cut it back and say so.

## THE LANE MAP — what "LIVE" MEANS FOR EACH TASK
A task is not VERIFIED until it is live **in its own repo's target environment**. The queue is the
WORK pipeline; each task exits through its repo's own deploy lane. Read this before claiming LIVE:

| workspace | lane | where "live" actually is |
|---|---|---|
| `cognizioware-mcp-tools`, `-b` | Deploy MCP Tools Stack | CT204 uat → **CT202 prod** (192.168.21.161) |
| `cognizioware-powerplatform`, `-evalfix`, `-c` | `promote.yml` | dev/uat/prod Dataverse — **but see the correction below** |
| `mcp-cognizioware`, `-b` | billing dev + uat lanes | CT100 |
| `cognizioware-qa` | QA gate | **serves the other lanes** — it gates them, it does not deploy |
| `LongHorizon-Harness`, `-b` | PyPI release + CT110 deploy | CT110 (**`lh-harness` on corsairai300, NOT ptait01/ptait07 — `pct` 110 config is absent on both; IP 192.168.21.168 = the API base; unit is `lh-harness.service`, NOT `lh-harness-web`**) — **restarts the service and kills every in-flight run**; the deploy script aborts unless zero runs are active |
| `cognizioware-hydra`, `-rsi` | Hydra deploy | corsairai300 |
| `cognizioware-nebo` | (none yet) | new LXC `nebo.easybutt0n.ai` — **not provisioned; Paxton's** |

**CORRECTION, measured 2026-09-09 — powerplatform prod is NOT what the docs say:**
- `powerplatform.easybutt0n.ai` is served by the **corsairai300 HOST-level docker stack**, not CT105.
  **CT105 is a stale decoy** running identical container names whose newest session is 2026-06-09.
  Querying it first is what made a live session appear not to exist. Proven by uptime: public
  21597 vs host 21598 (one second apart) vs CT105 151514.
- Prod runs **develop-derived images** (`app:sha-f2a3633`, which is on `develop` and **NOT on
  `main`**). `develop` is **149 ahead / 33 behind** (22:00 PT; it drifts) `main`. **SHIPPING - CORRECTED 2026-09-10 22:00 PT, read from the workflow files.** The pipeline is
BRANCH-PER-ENV (`ci.yml:6`, and `deployments/instances.json`): **`develop` -> dev, `release` -> uat,
`main` -> prod**. So **landing on `develop` deploys DEV ONLY** - uat and prod are NOT reached by
merging to develop, and NOT by merging to `main` either.
  * uat: land on `release`, or promote.
  * **prod: a `workflow_dispatch` of `promote.yml` with that ref.** It is dispatch-ONLY (inputs:
    `ref`, `is_production`, `eval_threshold`, weights, `suite`) and each tier is gated on a weighted
    eval score STRICTLY GREATER than the threshold (default 90 - exactly 90.00 does NOT pass), plus
    GitHub `environment: production` protection and an explicit `is_production` acknowledgment.
  * **CORRECTED 2026-09-17 15:29 PT, tick #488 - BOTH LINES THAT USED TO SIT HERE WERE STALE AND
    BOTH ERRED ON THE UNSAFE SIDE. READ FROM THE LIVE FILES ON `main`, NOT FROM PROSE:**
  * **`promote.yml` DOES NOT TOUCH `main` AT ALL.** The whole file (30,714 bytes) contains no
    `git push`, no `update-ref`, no `refs/heads` write, no `ff-only`. The old "fast-forwards
    origin/main to the promoted sha (non-FF refused), so main FOLLOWS prod" claim is FALSE of the
    current file. Nothing makes main follow prod; **main is an ordinary branch that anyone can
    merge into.**
  * **A PLAIN PUSH TO `main` *DOES* DEPLOY PROD. THE GUARD IS GONE.** `ci.yml` (3,961 bytes) has
    **ZERO occurrences of `PROD_PROMOTED_SHA`** - the reader was removed, not just the writer, so
    there is no longer anything to fail closed. The live chain, each link read this tick:
    `on.push.branches: [main, develop, release]` -> job `image` (`if: push`) -> job `deploy`
    (`if: push && vars.LAN_DEPLOY_ENABLED == '1'`, **and that variable IS set to `1`**) ->
    `bash scripts/deploy/rollout.sh "sha-${GITHUB_SHA:0:7}" "$GITHUB_REF_NAME"`.
    `rollout.sh` filters `deployments/instances.json` by that branch arg, and the instance with
    `branch: main` is **`prod`, host `pve151`, health `https://powerplatform.easybutt0n.ai/api/health`**.
    `promote.yml:483` invokes **the same `rollout.sh <sha> "main"`** for its prod tier - so a push
    to main and a prod promotion run the IDENTICAL rollout.
  * **THE PRACTICAL CONSEQUENCE: merging any PR whose base is `main` ships powerplatform PROD,
    with NO eval-score gate, NO `environment: production` protection and NO `is_production`
    acknowledgment** - every safeguard described above lives in `promote.yml` and a push to main
    does not go through it. **Treat a pp PR based on `main` as a prod deploy request, never as an
    ordinary merge.**
  * LAN rollout runs only when `vars.LAN_DEPLOY_ENABLED == '1'` (it is), on `[self-hosted, lan-deploy]`.
- dev/uat live at **192.168.21.163** (`pp-dev-app-1` :3000, `pp-uat-app-1` :3001).

**Two hazards attached to the lanes:**
- Any edit to `infrastructure/litellm-config.yaml` **force-recreates the CT202 prod router** — the
  deploy job hashes the DEPLOYED file's content, so even a comment-only change triggers it. Merge
  it only in a window with **zero active harness runs**.
- Five config sections live in Postgres and **the DB row wins over the YAML**
  (`general_settings`, `guardrails`, `litellm_settings`, `mcp_servers`, `router_settings`). A file
  edit alone may change nothing: use `POST /config/update`, then **read the row back and diff it**.

## STEP 0 — LEDGER, THEN SHIP PLANE
**TWO ACTORS SHARE THIS DOCTRINE SINCE 2026-09-14 21:26 PT, AND ONLY ONE ACTS AUTONOMOUSLY.**
- The **scheduled tick** (Windows task `LH-Overseer-Sweep`, `claude -p` every 5 minutes, log `C:/tmp/overseer_tick.log`,
  full output in `C:/tmp/overseer_ticks/`) is the ACTING overseer: it resolves gates, requeues strandings, injects
  corrections, opens PRs, updates OPEN-ASKS and the task files, and writes its ledger rows tagged `SCHEDULED TICK`.
- The **interactive session** (`[5321285c]`, Paxton's terminal) does NOT act on gates or the queue on its own any more;
  it acts only on Paxton's explicit requests, and it owns the two things a headless tick cannot do: publishing the
  Ship Plane artifact and anything needing an interactive tool. When it does act on a request, it writes the ledger
  row FIRST so the next tick sees it.
- **Every actor re-reads the ledger tail and the run's `approvals[]` immediately before any write**, and treats a 409
  or 'approval is missing, resolved, or read-only' as 'someone else did it', never as an error to retry. Measured
  2026-09-14 21:24: both actors resolved run 158's gate within a minute; the second write was refused, harmless only
  because the verdict was identical.
- Ship Plane republish by a scheduled tick: SKIP with one line saying 'republish pending, interactive'; do not fake it.
- **DECIDE WHICH ACTOR YOU ARE BY ANCESTRY, NEVER BY FEEL.** Four ticks (#168's 15:50 one, then 16:12, 16:14, 16:18 on 2026-09-15) each believed they were an interactive on-request sweep 'running concurrently with a live scheduled tick' whose START time was in fact THEIR OWN. Every one of them declined all writes on that belief; a tick that defers to a peer that does not exist loses the whole tick. THE TEST, run it before STEP 0: read your own shell's winpid (`cat /proc/$$/winpid`), walk ParentProcessId up through Win32_Process, and look at the claude.exe you land on. Parent `overseer_tick.ps1` (pwsh, itself a child of svchost/Task Scheduler) => **YOU ARE THE SCHEDULED ACTING OVERSEER: act, write, resolve.** Parent VS Code / an interactive shell => you are the interactive session. Matching your prompt text to a `-p` command line is NOT sufficient on its own - Paxton types the same sentence the scheduler does, so the text matches either way; the ancestry is what separates them. A second tick would also show its own START with no END in `overseer_tick.log`: that line is YOURS.

Update `C:/tmp/queue/LEDGER.md` first: advance each row only on evidence, and put the evidence IN
the row. Record blockers. Then, if anything changed, re-read the Ship Plane artifact (it may have
been republished elsewhere), edit the file it saves, and republish to the same URL with a label
carrying the local time — updating BOTH the header timestamp and the "Where things stand" heading.
If nothing changed, say so in one line and skip the republish.

**RELATED DOCS ARE PART OF STEP 0, NOT OPTIONAL.** When a tick changes their subject, update in the same tick:
- `C:/tmp/queue/OPEN-ASKS.md` — move answered items to Answered, date the header, every open item carries a recommended answer and a default.
- The task file for the task (`C:/tmp/*-task.txt`) and its queue entry `note` — corrections, slices and verified facts go in-row, with a `.bak` first.
- `C:/tmp/HANDOFF-*.md` — when a handoff's status changes, update its STATUS section.
- The Ship Plane — republish whenever the ledger changed (above); its task state board is generated from the queue dirs plus `GET /api/runs`, never typed.
A tick that changed a task's state but left these stale did not finish STEP 0.

## STEP 1 — DRIVE EVERY TASK TO VERIFIED
For each task not VERIFIED, do the next thing that advances it: launch, resolve its gate, requeue,
push the branch, open the PR, **get it reviewed (STEP 3)**, merge what is green, re-run a red lane,
then **probe the live environment and record what proved it**.


**PAXTON 2026-09-17 13:45 PT - PROD WORK RUNS ON SYNTHETIC TRIOS ONLY.** Any task whose output lands beyond `develop` (156 auto-promote, promote.yml, release/main, LAN_DEPLOY) must bind an all-Synthetic trio (`_TRIOS` rows 2-5). Never `trio: qwen`, never `degraded`, never an `ornith-1.5:pool` executor. The launcher cannot pin roles, so YOU check `roles_bound` after launch: a local executor on a prod-landing task is stopped in round 1 and requeued, with the reason in the note. Local trios stay fine for develop-only work.
Do not escalate backlog — run it through the gates. **Do NOT reorder the queue on your own
judgement**; new tasks append to the end of the active band, and only Paxton changes priority.

**If a spec changes after its run has launched, INJECT THE DELTA** (`POST /api/runs/<id>/instructions`)
rather than letting the run build to a stale spec — but ONLY to correct or narrow. **Do NOT inject
new slices**: that sent task 57 into a three-round loop producing nothing, and the fix was to CUT
scope, not add rounds. If a run loops three times, its scope is too big — cut it and queue the rest.

**RUNS PUSH AND OPEN THEIR OWN PULL REQUESTS SINCE 2026-09-14 16:21 PT.** Paxton's decision: CT110 runs hold a
scoped GitHub token (`GH_TOKEN`, fine-grained, all repos, push + PR only; secrets and workflow pushes are
refused by permission, verified from CT110). The service was restarted with it at 16:21 PT
(`ActiveEnterTimestamp` 23:21:22 UTC; `GH_TOKEN` present once in the service's `/proc/<pid>/environ`). So a run that
finishes its work is expected to `git push` and `gh pr create` itself, and a gate that asks for a token or
for the overseer to push is now a run that did not read its environment — answer it with "push and open
the PR yourself" rather than doing it by hand. **MERGING STAYS THE OVERSEER'S**: no run merges, and the
token could merge, so the rule is doctrine, not permission — every task text keeps "open the PR and STOP".
**PR TITLES MUST NOT CONTAIN "DO NOT MERGE" (any casing).** The no-merge rule is a rule the run follows
itself, not a label for Paxton; putting the literal phrase in a PR title confused Paxton (#185). If a PR
genuinely must not be merged yet, open it as a GitHub draft PR or apply a `blocked` label, and explain why
in the body. Runs that launched BEFORE 16:21 PT still lack the token and stall at push; finish those by
hand once, as before.

**A run that hits its round ceiling with the work DONE does not need more rounds — it needs the
last three steps.** Three times today a run finished the work and stopped at commit/push/PR.
Verify the tree yourself and finish it; granting rounds re-derives what is already on disk, and
these runs blow context doing it (one hit manager 587k chars / final_response 1.8M).

## STEP 2 — HARNESS RUNS
Resolve every gate; requeue failures; **never two runs on one working tree**. When a run dies at
round zero, read the FULL failure body before changing anything. **Group failures by cause across
ALL of today's runs** — 19 of 24 on 2026-09-09 were one missing fallback.

**Re-read the approvals list every time.** A run can carry several approval rows; reusing an id
from a previous tick returns "approval is missing, resolved, or read-only". **The list is in the
run snapshot's `approvals[]`** — `/api/runs/<id>/approvals` is a 404. Resolve at
`POST /api/runs/<run_id>/approvals/<approval_id>/resolve`.

**THREE-STRIKES DISCRIMINATOR:** an oversized task moves its failure BETWEEN roles; a failing
backend fails the SAME role repeatedly. Same role twice = backend fault, **do not cut scope again**.
**AND WHEN YOU DIVERT A SEAT, CHECK THE ROTATION ACTUALLY MOVED — measured tick #1559.** `_SEQ`, the
launcher's trio counter, was IN-MEMORY ONLY, so every restart reset it to 0 and re-seated the first
all-Synthetic launch on `SYN_ONLY[0]` = trio 2 = the `nemotron-3-super` executor. Task 204's executor
died on nemotron, #1558 restarted the launcher to requeue it, and the divert drew **nemotron again** —
a rotation that resets on restart cannot rotate away from a failing backend. Now persisted to
`C:/tmp/launch_seq.txt` (fail-open, seeded 2 → trio 4, a kimi-k3 executor). Seeds 0 AND 1 both land on
nemotron, so bumping by one is NOT a divert; verify the drawn executor in `roles_bound`, never the seed.

MODEL ROLES — Synthetic for MANAGER and AUDITOR. The `/v1/messages` 404 is SOLVED by the provider
prefix: `openai/` → 404, `openai_like/` → 400, **`hosted_vllm/` → 200**, **`custom_openai/` → 200**.
The launcher falls back to **all-`:synthetic-anthropic` trios** when Ollama keys are short — never
onto the local span, which serves ~one executor and killed the same task three times.

## STEP 3 — REVIEW, LANES AND PRs
**GET EVERY PR REVIEWED BEFORE MERGING — nothing is reviewing them today.**
- **Copilot is quota-exhausted**: every recent PR carries only *"Copilot was unable to review this
  pull request because the user who requested the review has reached their quota limit."*
- **`claude-code.yml` NEVER fires on a `pull_request`.** It triggers only on `issue_comment` and
  `pull_request_review_comment`, and only when the body contains **`@claude`** from an
  OWNER / MEMBER / COLLABORATOR.
- *** NEVER POST AN `@claude` COMMENT TO INVOKE THE REVIEWER. *** Since mcp-tools PR #131 that
  reviewer runs on **PAXTON'S PERSONAL SUBSCRIPTION QUOTA**. Every invocation costs him real money.
  Task 117 (`748-117-pr-backlog-triage`) exists precisely to triage the backlog WITHOUT spending it,
  and its note bans invocations in as many words. On 2026-09-10 19:50 PT the overseer fired five
  anyway while believing it was doing queue work, and had to cancel four live runs. **Getting a PR
  reviewed goes through task 117's triage document or Paxton's explicit say-so - never the bot, never
  on your own initiative.** An unreviewed merge is still a state the pipeline should not have, so the
  answer to an unreviewed PR is to REPORT it, not to buy a review.
- **The open-PR / `reviewDecision=NONE` condition is KNOWN, not an oversight to fix — but the COUNT has nearly doubled and the old "29" was stale. MEASURED 2026-09-17 15:52 PT, tick #491, enumerated per-repo via `gh pr list --state open --limit 100`, not sampled: **47 open** = mcp-tools 25, LongHorizon-Harness 8, powerplatform 3, hydra 3, mcp-cognizioware 4, qa 2, BMAD 2. Task 117's triage doc (mcp-tools #152) predates this. A tick that quotes "29" is quoting a number no one has re-measured since.** **FOUR** of seven repos
  carry `claude-code.yml` — mcp-tools, mcp-cognizioware, LongHorizon-Harness **and cognizioware-hydra**
  (blob sha `71abcfef` on all but mcp-cognizioware; ENUMERATED against the GitHub API 2026-09-15 17:53 PT by
  scheduled tick #180, not sampled). powerplatform, cognizioware-qa and BMAD_Cognizioware have none.
  **CORRECTION — hydra USED to have none and this line used to say so.** PR #26 (`ci/add-claude-reviewer`)
  merged 2026-09-11T06:21:15Z. So an `@claude` comment on a hydra PR is **NO LONGER INERT**: it now fires the
  reviewer on Paxton's personal subscription. The stale line said hydra invocations were harmless; they are not.
  **BUT PAXTON HAS ALREADY RULED ON HYDRA SPECIFICALLY** (`OPEN-ASKS.md`, row `hydra-26`, 20:25 PT, Answered):
  *"Billing `@claude` to Paxton's subscription on hydra is **accepted**."* So hydra is the ONE repo where the
  spend is authorised — it is NOT free, and it is NOT a licence for a tick to invoke on its own initiative;
  route it through task 117's triage as everywhere else. Only in powerplatform, qa and BMAD is an invocation
  inert — and it is still unauthorised there.
- **`mergeable=MERGEABLE` and a green review are BOTH scoped to the PR's BASE. Check what the base
  actually is before treating either as merge-readiness.** mcp-tools #36 reads MERGEABLE only because
  its base is `feat/ms365-mcp-hosted-backend`, not `main` - and PR #35, that branch into main, is
  CONFLICTING. A green verdict answered "is this diff correct?", never "can it reach main?".

**BUILD IT YOURSELF BEFORE MERGING.** `gh pr checks` on mcp-tools reports *"no checks reported"* —
the images are built only AT MERGE TIME on the way to prod, so a non-compiling change cannot be
caught before it blocks a deploy. That is exactly how a one-line Razor error (RZ1010) broke Deploy
to CT202. Run the project's own build command before you merge.

**LongHorizon-Harness ON THIS BOX HAS TWO REMOTES — `gh` DEFAULTS TO THE WRONG ONE.** `origin` is
`cogniziocompany/LongHorizon-Harness` (the one the ledger tracks); `upstream` is `AMAP-ML/LongHorizon-Harness`
(an unrelated fork). A bare `gh repo view` / `gh pr list` with no `--repo` resolves to `upstream` and
returns a completely different, unrelated PR set with overlapping-looking numbers — caught tick #1121
before it was quoted as the tracked 11-open set. **Always pass `--repo cogniziocompany/LongHorizon-Harness`
explicitly** for any `gh` command run from this checkout.

Newest run on each repo's default branch; diagnose and re-run anything red. Check `diff --stat`
before every merge; never chain create+merge. **Before restarting ANY shared dependency — the
LiteLLM router above all — confirm no gate or promotion is mid-flight**, and check whether the
change touches `litellm-config.yaml` (any edit force-recreates the prod router).

**After a failed deploy, RECONCILE BEFORE RETRYING.** Re-running a partially-applied deploy renamed
27 containers and left 18/73 running. First establish where it died: a failure at IMAGE BUILD never
reached `docker compose up` and is safe; a failure after it is not.

## PRESENTING PRs TO PAXTON (task 245)
Every time an overseer (the CT overseer tick, the chat Fleet Operator, or any session reporting
for the overseer) presents PRs to Paxton, it MUST use exactly this format.

FORMAT RULES
1. Group by repository. The group heading is the plain repo name (e.g. `LongHorizon-Harness (LHH)`, `cognizioware-hydra`, `cognizioware-mcp-tools`).
2. One line per PR. The link TEXT is the full `owner/repo#N` reference and the link TARGET is the full PR URL:
   `[cogniziocompany/<repo>#<N>](https://github.com/cogniziocompany/<repo>/pull/<N>)`
   Never a bare `#N`, never a bare URL.
3. After the link: a colon, a short plain-language description of what the PR does, and the task number in parentheses. Then any sentence Paxton needs to act on it: merge order, "needs setup after merge, which I'll do", a review point, size warning.
4. When merge ORDER matters within a repo, use a NUMBERED list in merge order and say why the first one is first and what depends on what ("Only after #49, because it's built on #49's branch"). Otherwise use a bulleted list.
5. Only PRs that are actually OPEN and waiting on Paxton, verified live (gh / GitHub API) at the moment of reporting. Say plainly if any is CONFLICTING or red, and what is being done about it.
6. PR TITLES must not contain "DO NOT MERGE". That phrase is an instruction to the run (the agent must not merge its own PR), not a label for the human; runs put it in titles and it confused Paxton (#185). Keep "do not merge" in the run's own rules only. Where a PR genuinely must not be merged yet, use a GitHub draft PR or a `blocked` label and say why in the body.

REFERENCE EXAMPLE (reproduce this shape exactly):

LongHorizon-Harness (LHH)

1. [cogniziocompany/LongHorizon-Harness#50](https://github.com/cogniziocompany/LongHorizon-Harness/pull/50): queue-stall fix (task 230). Merge this first, since it's what stops the queue getting stuck.
2. [cogniziocompany/LongHorizon-Harness#52](https://github.com/cogniziocompany/LongHorizon-Harness/pull/52): enqueue fields fix (233).
3. [cogniziocompany/LongHorizon-Harness#53](https://github.com/cogniziocompany/LongHorizon-Harness/pull/53): overseer files moved into the repo (104b). It's big (598 files) but mostly records.
4. [cogniziocompany/LongHorizon-Harness#49](https://github.com/cogniziocompany/LongHorizon-Harness/pull/49): CI deploy workflow (224). It needs setup after merge, which I'll do.
5. [cogniziocompany/LongHorizon-Harness#51](https://github.com/cogniziocompany/LongHorizon-Harness/pull/51): queue drain switch (242). Only after #49, because it's built on #49's branch.

cognizioware-hydra

* [cogniziocompany/cognizioware-hydra#32](https://github.com/cogniziocompany/cognizioware-hydra/pull/32): hydra sees CT110 runs (232).
* [cogniziocompany/cognizioware-hydra#33](https://github.com/cogniziocompany/cognizioware-hydra/pull/33): removes the dead `rc_*` tools (237).
* [cogniziocompany/cognizioware-hydra#34](https://github.com/cogniziocompany/cognizioware-hydra/pull/34): tool error handling and stale devices (238).

cognizioware-mcp-tools

* [cogniziocompany/cognizioware-mcp-tools#185](https://github.com/cogniziocompany/cognizioware-mcp-tools/pull/185): fleet.easybutt0n.ai accuracy (231). This fixes the 12 dead runs showing as live.
* [cogniziocompany/cognizioware-mcp-tools#186](https://github.com/cogniziocompany/cognizioware-mcp-tools/pull/186): Penpot (241).
* [cogniziocompany/cognizioware-mcp-tools#188](https://github.com/cogniziocompany/cognizioware-mcp-tools/pull/188): keeps the chat's `ssh` tool off the Windows PCs (239). One review point: chat gets the Proxmox hosts only, not CTs.

## STEP 4 — INFRASTRUCTURE HEALTH
Cheap probes only: `/health/liveliness` or `/health/readiness`, **never the bare gateway `/health`**
(it hits every model, including paid and GPU).
**CT204 AND CT210 LIVE ON ptait07, NOT ptait01 — measured tick #712.** `pct list` on ptait01 shows a
CTID 204 too, but it is **`stopped`, a decoy** (same family as the CT100/CT105/CT120 decoys already
documented) — the running CT204 (60%) and CT210 (12%) answer only on ptait07. Query ptait07 first for
both; CT202 stays on ptait01.
**CHECK DISK on CT202, CT204, CT210 and corsairai300 every full-depth tick.** CT202 silently
reached 100%, killed the prod router for an hour and destroyed 23 runs with nothing on any operator
surface showing it. Over 80%, grow it rather than hunting for things to prune — Paxton has standing
authorisation.

## STEP 5 — CAPACITY
**Exactly ONE launcher process**, counted with PowerShell filtering on the command line — a bare
`ps | grep python` counts unrelated processes and produces false alarms. Confirm its log is being
WRITTEN (check the mtime), not merely present. Quota watcher alive. Queue draining. When the
launcher holds, read WHY from its live log — `active=N CAP=M` at cap is correct behaviour, not a
fault, and resolving gates is what frees it.

The launcher status line reads:
`active=N kimi=N qwen=N keys_ok=N queued=N synthetic=ok|DOWN doctor_quota=EXHAUSTED(advisory)|ok|unread`
**`doctor_quota` is ADVISORY AND GATES NOTHING** (Paxton, 2026-09-10 19:09: stop the doctor gating
launches until its signal is fixed — task 146). **`doctor_quota=EXHAUSTED` is not a fault and not a
reason to hold.** The gate is `synthetic_down() or synthetic_failing_real_work()`.

**THE LAUNCHER EXITS WHEN THE QUEUE DRAINS — CORRECTED 2026-09-18 14:52 PT, TICK #705, READ FROM
THE SOURCE.** `launch_queue.py:723` is (was :630, :695, then :705; the blocks added by ticks #1554, #1558 and #1559 each shifted it - re-read at #1559, do not trust the number without grepping) `if not q: print(now,"queue empty; exiting"); break`. It is a
clean self-terminating exit, NOT a crash. Measured this tick: the launcher logged
`14:30 queue empty; exiting` and pid 225444 was gone — zero launcher processes, confirmed by
positive enumeration. **So after the queue fully drains there is NO launcher, and the next task
queued will sit forever until one is restarted.** The old line here — "it refills slots every 900 s
and has no pause switch" — was FALSE and is removed; it would have had a future tick read an empty
queue as healthy idling. **WHENEVER YOU ENQUEUE A TASK, CHECK THE LAUNCHER IS ALIVE FIRST** (positive
enumeration on the command line, per STEP 5) and restart it if not:
`Start-Process cmd.exe -ArgumentList '/c','python C:\tmp\launch_queue.py >> C:\tmp\launch_queue.log 2>&1' -WindowStyle Hidden`
— the `cmd` wrapper is what holds the log redirect, so starting python bare loses the log.
Restarting it against an EMPTY queue is pointless: it re-reads line 630 and exits again.

**THE LAUNCHER'S GLOB DIRECTORY IS FULL OF TICK SCRATCH FILES, AND ONLY A DOT SAVES IT — measured
tick #1563.** `C:/tmp/queue/` holds **71 `*.json` files** that are NOT queue entries: `.runs_tick*.json`
and `.snap_*.json` written by previous ticks. They are invisible to the launcher ONLY because Python's
`glob.glob('C:/tmp/queue/*.json')` skips leading-dot names. **So NEVER write a tick scratch file into
`C:/tmp/queue/` without a leading dot** — a single `runs_tick1564.json` would be globbed as a queue
entry and launched. Also: **PowerShell `Get-ChildItem C:\tmp\queue\*.json` counts all 71 and is the
WRONG reader for this question** — it does not hide dotfiles, so it reports a full queue where the
launcher sees an empty one. Count queue entries with Python glob or a bash `*` glob, never with
PowerShell, or you will read "queue empty; exiting" as a launcher that abandoned 71 entries.

Any task needing ACTIVE=0 (e.g. 144) must make the window on
purpose. The `C:/tmp/queue/DRAIN` flag is task 147 and is **NOT implemented yet** — do not rely on
it until it lands and the launcher is restarted.

**Synthetic meters requests and dollars, never tokens:** 500 requests / 5 h (regenerates +25 per
15 min, +100/hr) and a weekly dollar credit (~1%/hr regen, the scarce one). Any token-count "quota"
you see — including the doctor's — is an invented ceiling.

## STEP 6 — READ WHAT OTHER SESSIONS ARE DOING
If anything names a session id, a plan file or a scratchpad path, **GO AND READ IT.** Never ask a
human to paste what is on disk and addressable. Plans live in
`C:\Users\PaxtonTait\.claude\plans\*.md`; transcripts in `~/.claude/projects/<slug>/<id>.jsonl`.
Address a peer session by the NAME in `ListAgents`, never by a UUID — a scratchpad UUID is not a
session id. **Check the queue before creating anything — and before ACTING on anything.** Two different
checks, and only the first was ever written down: (1) no duplicate TASKS, and (2) **read the
relevant queue entry's `note` for standing CONSTRAINTS.** The prohibitions live in the notes, not
in this file. On 2026-09-10 19:50 PT the overseer ran check (1) only — after acting — and so
missed task 117's explicit ban on `@claude` invocations, spending Paxton's personal subscription
on four live reviewer runs before catching it. A note can forbid what this prompt appears to
permit, and the note wins.

**RUN `/claude-bridge` ON EVERY FULL-DEPTH TICK** (added 2026-09-11 by Paxton's instruction). It is
the procedure for this step: DISCOVER every transcript touched in the last 30 h, RETRIEVE the long
user turns and the last assistant message, TRIAGE (handoff / informational / already captured, by
grepping this ledger for the session's `[short-ref]`), and QUEUE only class (a), never a duplicate.
The skill is local at `~/.claude/skills/claude-bridge/SKILL.md` and, once mcp-tools PR
`feat/gateway-skill-claude-bridge` merges, on the gateway as prompt `skills-skill-claude-bridge` —
read the gateway copy when it exists, so every orchestrator on the fleet runs the same procedure.
**TWO DISCOVERY FILTERS ARE KNOWN TO MISCLASSIFY, BOTH MEASURED 2026-09-22.** (1) Grepping a transcript for `overseer` / `lh-harness queue` matches the SKILL-LISTING system-reminder boilerplate, so an unrelated session reads as a handoff candidate (tick #1574). (2) **Filtering non-overseer sessions by grepping for `OVERSEER SWEEP` labels an ABORTED TICK as a peer session** - a tick that died on an API 500/529 before reading this file never contains the phrase, so it looks like an untriaged stranger with zero ledger mentions. Tick #1575 hit two at once (`b17df064`, `256d5e51`, 23 lines each, 09-21). **Before treating a never-ledgered session as class (a), check its line count and last assistant message: a short transcript ending in an API error is a dead tick, not a handoff.** **SHARPENED 2026-09-22 20:05 PDT, tick #1576 - IDENTIFY A DEAD TICK POSITIVELY, DO NOT INFER IT FROM SHORTNESS.** Both `b17df064` and `256d5e51` carry the tick's own prompt VERBATIM - the `last-prompt` record's `lastPrompt` field (and the first user turn) reads `Run one overseer sweep tick now: read C:\tmp\queue\LOOP-PROMPT.md in full and execute it.` - while `OVERSEER SWEEP` is absent, re-measured this tick. So the test is a CONJUNCTION on the transcript: **tick prompt present AND `OVERSEER SWEEP` absent => aborted tick, not class (a)**, whatever its length. Line count is only a hint and cuts the wrong way on a tick that dies late (it would be long) or a genuine one-line handoff from a peer (it would be short). **READER FAULT, measured tick #1605: THE NEEDLE MUST BE JSON-ESCAPED.** A transcript is JSONL, so the prompt is stored as `C:\\tmp\\queue\\LOOP-PROMPT.md` (doubled backslashes). Grepping the raw file for the single-backslash form returns False on a transcript that DOES carry it - which flips the conjunction and relabels an aborted tick as a class (a) handoff. Double the backslashes, or `json.loads` each record and read `lastPrompt` / the first user turn; then verify the reader on a known-positive before trusting a negative. **AND BUILD THAT NEEDLE WITH `chr(92)`, NEVER BY TYPING IT INTO A HEREDOC - measured tick #1607.** Writing the doubled form through a `cat <<'EOF'` script file collapsed it back to the single form on disk, so the reader returned 0/300 on KNOWN POSITIVES and the tick briefly concluded #1605's rule was itself wrong. Two safe readers: build the needle from `chr(92)`, or drop the path entirely and match the prose phrase `Run one overseer sweep tick now`, which carries no backslash and matched 300/300 this tick. Either way the self-check on a known positive is what catches it.
Cross-session handoff files live at `C:/tmp/HANDOFF-*.md`; a message from a peer that names one is a
class (a) candidate. Record in the ledger each tick: sessions discovered, sessions read, and the
triage table — a tick that reports zero handoffs without the discovery list did not run this step.

## REPORT EACH TICK, BRIEFLY
What advanced and the evidence, what is blocked and why, what needs Paxton. **EVERY ITEM YOU PUT ON PAXTON MUST CARRY TWO THINGS: a RECOMMENDED ANSWER, and what happens if he says nothing.** An ask with neither is not a question, it is an unfinished thought handed over - and it stalls silently because there is nothing to agree with. Before you mark anything NEEDS PAXTON, SEARCH FIRST: grep the queue, `done/`, `blocked/`, the task files and any `resolve*.json` for the answer. You have escalated questions you already had the answer to. **Only three things genuinely need him: a CREDENTIAL that must be created, a SPEND decision, or a PROD WINDOW.** Everything else you decide, state the default, and proceed. **If a claim cannot be
backed by something actually read this tick, say "unverified".** Correct your own earlier errors
plainly and move on — **a correction is not complete until the wrong claim's blast radius is
stated** (what was attributed to it, for how long). When every ledger row reads VERIFIED, say so and stop claiming further work.

## TICK HYGIENE - added 2026-09-16 13:30 PT by the interactive session on Paxton's "proceed to resolving the issues" (review of ticks #304/#305)

1. **LEDGER ROWS ARE EVIDENCE, NOT ESSAYS. HARD CAP 1,500 CHARACTERS PER TICK ROW.** The ledger is 3.3 MB and ticks were appending ~9 KB each; every actor re-reads it at STEP 0, so prose there taxes every future tick. A row carries: tick number, session ref, gates found/resolved (run id + approval id + action), state changes (task -> new state + evidence), one line per fault caught, and "Needs Paxton: <item or nothing>". Health numbers go in ONLY when one crosses a threshold (>=80% disk, router not 200, launcher dead). Identity ancestry is ONE clause ("ancestry verified: pwsh <pid> <- Task Scheduler"), not a paragraph. Corrections to earlier rows: one sentence each. Everything else belongs in the per-tick output file, which the wrapper already keeps.
2. **NO `cd` IN TOOL CALLS.** The "cd trap" has fired 58 times across ticks; it is not a fault to keep catching, it is a habit to drop. Use absolute paths in every command and pass `cwd` where a tool offers it.
3. **READ THE LEDGER TAIL, NOT THE LEDGER.** STEP 0 means the last 40 rows (tail -c 60000 is plenty) plus a grep for the specific task/run you are about to touch. Never load the whole file.
5. **WHEN YOU INJECT A WINDOWS PATH INTO THIS FILE, BYTE-VERIFY IT - A \t BECAME A TAB TWICE.** Tick #1563 wrote `C:\tmp\queue\*.json` into STEP 5 and tick #1576 wrote `C:\tmp\queue\LOOP-PROMPT.md` into STEP 6; in both the `\t` was consumed as an escape and a literal TAB landed in the doctrine, giving a future tick an uncopyable command. Both repaired at #1576 (file now holds zero tabs). The display does NOT show it - `sed`/terminal output renders the tab as indentation and looks right. **Build the path with `chr(92)` (or read it back with `repr()` and assert `chr(9) not in` the line) before believing the write.**

4. **DO NOT RE-REPORT THE SAME OPEN ITEM EVERY TICK.** If a run has been at the same branch sha for N ticks, say "unchanged since tick #k" in one clause. Repeating the full history each tick is what made rows 9 KB.
