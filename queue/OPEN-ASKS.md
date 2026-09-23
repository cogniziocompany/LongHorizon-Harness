# OPEN ASKS — one row per thing genuinely waiting on Paxton

Maintained by the overseer (`longhorizon-harness-99` `[2df7a7]`, cron `84a5be19`).
Updated 2026-09-23 00:26 PDT (SCHEDULED TICK #1616): **ZERO OPEN ROWS.** Counted directly from the table, not carried: every row now reads CLOSED/ANSWERED/DONE. The last two - `168-trio-agreement-scoring` and `uat-fleet-admin-env` - were answered by Paxton DIRECTLY and written into the state column by the interactive session at 00:19 (handoff `C:/tmp/HANDOFF-paxton-decisions-2026-09-23.md`); this header had still read "16 open" from #1609. The overseer carried both as "Needs Paxton" for ~30 ticks; it should stop, and the per-tick report now has NOTHING on Paxton. previous: Updated 2026-09-22 23:26 PDT (SCHEDULED TICK #1609 [f41eec]): three rows gain answers read from peer session `61855af8`'s live transcript - `177-lhharness-upstream` (Option A: scope the existing `hydrafleet`, no credential needed - this row may stop being a CREDENTIAL ask), `nebo-190-gate-b` (check 3 of 3 answered: the reroute is REVERTED to Telnyx by Paxton's ruling, executed by task 217 step 3), and `uat-fleet-admin-env` (a peer-inferred 'approved' does NOT override the 12:56 withdrawal - nothing copied, recommendation is now (a) UAT gets its own DB). Row count unchanged at **16 open**. previous: 2026-09-22 22:15 PDT (SCHEDULED TICK #1597 [4d0117]): one row edited - `168-trio-agreement-scoring` gains the evidence that task 168's other two blockers (boot crash, workspace-occupancy skip) are CLEARED and measured, leaving this decision as the last gate on the shadow window. Row count unchanged at **16 open**. previous: 2026-09-22 20:58 PT (SCHEDULED TICK #1586 [427970]): header date only - the interactive session [5321285c] added `168-trio-agreement-scoring` at 20:56 PT without bumping this line; row count is now **16 open**, counted directly from the table (not carried from an earlier header - the last quoted figure, 12, was stale). previous: 2026-09-22 12:26 PT (SCHEDULED TICK #1519 [24e28d]): two rows corrected against live measurement, both first surfaced by peer session [61855af8] and re-proved here. `177-lhharness-upstream` — the recommended answer is WITHDRAWN as not executable: CT110 :8799 serves no MCP endpoint (/mcp, /mcp/, /api/mcp, /sse all 404 while /api/meta and /api/runs return 200 on the same reader), so the bridge URL this table recommended would 404. `nebo-190-gate-b` — overseer check 2 of 3 ANSWERED: `CLAWDTALK_SERVER` is UNSET across every corsairai300 CT (105/106/110/120) and absent from the host filesystem, so PR #2 reroutes to an empty value; the review hold hardens. previous: 2026-09-22 04:29 PT (SCHEDULED TICK #1427 [eaec57]): `nebo-190-gate-b` recommendation corrected - merge is on REVIEW HOLD (routing regression suspected, three overseer checks pending), no longer "merge on its own merits". previous: 2026-09-22 03:14 PT (SCHEDULED TICK #1413 [05f913]): added `nebo-190-gate-b` (task 190 reached PR; STOP GATE B items 1-4 are Paxton's). previous: 2026-09-21 17:26 PT (SCHEDULED TICK #1313 [1571b3]): direct row count = **12 open rows** - moved `189-refresh-key` (CLOSED end to end; qa #38 merged e9968a4 by this tick) and `187-github-app` (credential half CLOSED by Paxton 17:05 PT; remaining blocker is LHH #23 / task 186, code not Paxton) to Answered. previous: 2026-09-21 09:07 PT (SCHEDULED TICK #1225 [0a70d5]): direct row count = **14 open rows** - added `mcp-177-workflow-merge` (mcp-tools PR #177 edits a deploy-lane workflow file; logged as "Needs Paxton" by interactive [5321285c] at 09:05 PT but not yet in this table). previous: 2026-09-21 00:30 PT (SCHEDULED TICK #1136 [17e84e]): direct row count = **13 open rows**, unchanged - 208-rss-deploy-window was already moved to Answered by interactive [5321285c] at 00:24 PT today (closed by events, evidence on CT110), so this table needed no edit, only the header refresh. previous: 2026-09-19 07:10 PT (interactive [ed5631], tick #856, added `150-spec-staging-decisions` after correcting tick #855's mischaracterization of task 150's branch WIP as orphaned - it is tracked, deliberately BLOCKED, and carries two decisions (SS8/SS9) the task's own note reserves to Paxton. Table now carries 12 open rows (208-rss-deploy-window closed by tick #759, 150-spec-staging-decisions added this tick, net unchanged from #682's count). previous: 2026-09-18 11:15 PT (SCHEDULED TICK #682 retired `184-band-order` to Answered: the priority question was settled by action (184 promoted to `007b-`, run `20260918T072506Z_5378bb86`, PR #171), but the risk it named is NOT closed - 40 of 48 CT202 containers are still uncapped and #171 adds no `mem_limit`. Table is now **12 open rows**. previous: 2026-09-17 19:33 PT (SCHEDULED TICK #516 strengthened `107-dev-identity`: task 109 reached PR #117 and its specs now hard-depend on the `PP_DEV_E2E_*` names, so one dev user closes two tasks. previous: 2026-09-17 17:48 PT (SCHEDULED TICK #503 re-measured `138-checks-read` and found the approved Checks:read/Actions:read scopes ABSENT on the PAT (403 on both private repos), and corrected that row's read-back, which had named the PUBLIC LongHorizon-Harness repo and so would have passed while the capability was missing. Also: this table holds **13 open rows**, not the '9' that ticks #497-#502 reported. previous: 2026-09-17 16:16 PT (TICK #493 executed the task-170 section-3 write on the CT202 prod router and proved it by read-back: ACCEPTANCE PASS 6/6 - `store_prompts_in_spend_logs` True->False, `maximum_spend_logs_retention_period` None->'7d', and the four cleanup controls bounded at 1000/50/'5m'/'30s', all `stored_in_db=True`, router 200 after. **SPEND-LOG RETENTION IS NOW LIVE**, which is the precondition two rows below hang on (the 125 export-family supersession, and the '2-hourly export retires once retention is proven live' half of the CT202 disk item). No new ask; nothing here needs Paxton because of it. previous: 2026-09-17 15:29 PT (TICK #488 filed `pp-116-main-base` after proving from the live `ci.yml`/`rollout.sh`/`instances.json` that a push to powerplatform `main` DOES deploy prod ungated — the doctrine's standing claim that it cannot is stale and erred on the unsafe side; previous: 2026-09-17 14:42 PT (TICK #483 proved all 24 strandings were provider-layer failures, not task failures, and filed the `184-band-order` row that #482 reported to Paxton but never wrote into this table; previous: 2026-09-17 14:27 PT (TICK #482 recorded the SECOND and THIRD occurrences of the 09-11 'caps select a victim' defect and measured 40 of 47 CT202 containers uncapped; previous: 2026-09-17 14:11 PT (TICK #481 added `kb-hook-order` after proving the pp repo's own Claude hook invalidates every harness audit in that workspace; previous: 2026-09-17 14:00 PT (TICK #480 added `ollama-cloud-mem-cap` after a prod-router outage; previous: 2026-09-17 12:45 PT TICK #474 dated the header: `189-refresh-key` was added by the interactive session at 12:31 PT without a header bump; previous: 2026-09-17 05:53 PT TICK #415 added 87-copilot-phase2)).

**2026-09-16 22:45 PT (interactive [5321285c]):** the Ship Plane's "What is blocked / Waiting on you" section is now RENDERED FROM THE OPEN TABLE BELOW on every publish (C:/tmp/scratch/make_board.py), so this file is the single source for that section. A row added here appears on the page at the next publish; a row removed here disappears. Nothing is hand-written into that section and no row is carried forward from the previous artifact version. Source: C:/tmp/HANDOFF-ship-plane-waiting-on-you-2026-09-16.md.

**Rules for this file, which exist because the overseer got them wrong:**
1. **Before adding a row, SEARCH FIRST.** `C:/tmp/queue/done/*.json`, `C:/tmp/resolve*.json`, the task
   files and this ledger. On 2026-09-10 the overseer escalated 69b's "what are 69a's three
   checkpoints?" at 20:00 while the answer had been sitting in `C:/tmp/resolve69a.json` since 18:41.
   An ask that is already answered on disk is not an ask.
2. **Only three kinds of thing belong here:** a CREDENTIAL, a SPEND, or a PROD-WINDOW decision.
   Anything else the overseer should decide and record.
3. **Every row carries a RECOMMENDED ANSWER and a DEFAULT** — what happens if Paxton says nothing.
   A row with no default is an unfinished row.

| id | ask | kind | evidence | recommended | default if silent | state |
|---|---|---|---|---|---|---|
| 138-checks-read | Add **Checks: read** and **Actions: read** to the `lh-harness-ct110` fine-grained PAT (cogniziocompany, all repos) | CREDENTIAL | ORIGINAL reason (138's run could not name the red preview check) is RETIRED: Track A moved to task 159 on 09-16. CURRENT reason: the PR-review gate (tasks 185-188) has the harness reviewer consume pr-gate results and post/read check runs; without checks:read every CI read is relayed by the overseer, and runs stay blind to CI. Evidence: checks API 403 on private repos, run 07e50378 gate 33b2b5b4159c (09-15) -- **RE-MEASURED 2026-09-17 17:48 PT, SCHEDULED TICK #503: THE SCOPES ARE NOT ON THE TOKEN, AND THIS ROW'S OWN READ-BACK WAS INVALID.** Ran the prescribed check from CT110 as `harness` with the live `GH_TOKEN`: LongHorizon-Harness `commits/main/check-runs` returned **HTTP 200, total_count 0**, which is exactly what this row said would close it. It closes nothing: **LongHorizon-Harness is PUBLIC**, so that endpoint answers 200 for any token regardless of Checks:read. Enumerated against the PRIVATE repos where the pr-gate CI actually lives: mcp-tools `commits/main/check-runs` -> **403 'Resource not accessible by personal access token'**; powerplatform `commits/main/check-runs` -> **403**; powerplatform `actions/runs` -> **403**, so Actions:read is absent too. Conclusion: Checks:read and Actions:read were approved on 09-16 22:49 PT but **never landed on the PAT**. | **Yes** - read-only scopes. Click path: GitHub -> Settings -> Developer settings -> Fine-grained tokens -> `lh-harness-ct110` (expires 2027-09-15) -> Edit -> Repository permissions: Checks = Read-only, Actions = Read-only -> Update. Editing keeps the value; nothing on CT110 reinstalls. Read-back: from CT110 `gh api repos/cogniziocompany/LongHorizon-Harness/commits/main/check-runs --jq .total_count` returns a number, not 403 -- **READ-BACK CORRECTED BY TICK #503: USE A PRIVATE REPO.** The old check named a public repo and would have passed while the scopes were missing. Correct acceptance, both required: from CT110 `gh api repos/cogniziocompany/cognizioware-mcp-tools/commits/main/check-runs --jq .total_count` returns a NUMBER (today 403), AND `gh api 'repos/cogniziocompany/cognizioware-powerplatform/actions/runs?per_page=1' --jq .total_count` returns a NUMBER (today 403). LongHorizon-Harness proves nothing either way. | Runs stay blind to CI; task 186's reviewer gets pr-gate results only via the overseer | **CLOSED 2026-09-21 16:10 PT - RE-SCOPED, THE USEFUL HALF IS LIVE.** Paxton edited the PAT: Actions is now Read+Write. MEASURED from CT110 as harness with the PAT, against PRIVATE repos: `actions/runs` returns a NUMBER (powerplatform 611, mcp-tools 682 - was 403), and `actions/runs?head_sha=<sha>` gives the per-commit CI verdict per workflow plus per-JOB conclusions (E2E Gate / QA Gate (UAT) / Deploy to CT202 all readable). **`check-runs` and `check-suites` STILL return 'Resource not accessible by personal access token' and the token page lists NO Checks permission - fine-grained PATs do not appear to offer one (Checks is a GitHub-App capability). So the original acceptance test can NEVER pass on this PAT: stop asking for it.** USE INSTEAD: `gh api 'repos/<o>/<r>/actions/runs?head_sha=<sha>'` for CI state - all fleet CI is GitHub Actions, so this covers it. Real check-run access arrives with the GitHub App (row 187-github-app, which Paxton chose). OPTIONAL, small: `commits/<ref>/status` also 403s - adding 'Commit statuses: Read-only' to the PAT would let runs read the cross-repo QA-gate status. UPDATE 16:40 PT: Paxton ALSO added Commit statuses - measured from CT110: `commits/main/status` on mcp-tools -> state=success, 2 statuses (was 403). |
| 134-ct103-db | Create the `lh_harness` database and role on CT103 Postgres for the harness PgQueueStore | CREDENTIAL | Task 134's stated prerequisite ('a run never gets credentials'); PR LongHorizon-Harness #19 is open; task 168 (queue cutover) and 174 are blocked behind 134 landing | Create db `lh_harness` + role with DDL rights on schema `harness`; **CORRECTED 2026-09-18 21:2x PT (session 61855af8, read from `pg_queue.py` directly): deliver a PASSWORD ONLY, env name `LH_HARNESS_DB_PASSWORD` - NOT a DSN, NOT `LH_HARNESS_DATABASE_URL`. Host/db/user come from `database_url` in the project config, which the overseer sets.** Full draft commands at `C:/tmp/CT103-lh_harness-provisioning.md`; overseer applies the migrations | 134 merges without a live store; cutover (168) cannot start; the PC launcher stays authoritative | **CLOSED 2026-09-21 15:50 PT - PROVISIONED BY PAXTON, VERIFIED INDEPENDENTLY.** role/db 1/1, db owner + schema `harness` owner = lh_harness, login=true super=false createdb=false, SCRAM-SHA-256 verifier, search_path=harness, DDL (table+type) proven; CT110 secrets file 600 harness:harness with exactly 1 `LH_HARNESS_DB_PASSWORD` line (backup `.bak-20260921-224859`). REMAINING, overseer's: `pip install "psycopg[binary]"` on CT110 at the #19 deploy, set `queue_backend="postgres"` + `database_url` (host 192.168.21.154, db lh_harness, user lh_harness) in project config, apply migrations 001/002, THEN the true end-to-end auth test from CT110. |
| 135-seq-read-key | A **read-capable Seq API key** (read scope only) for prod Seq, delivered to CT110 as `SEQ_READ_KEY_PP_PROD` in `/home/harness/.lh-harness-secrets.env` | CREDENTIAL | Task 135's gate `2ded74268ee9` (run `8688c2e7`) asked which read path it may use. Measured by TICK #378 from inside CT110 at 2026-09-16 23:15 PT: `SEQ_INGEST_KEY_PP_PROD` -> GET /api/events **403** (correctly ingest-only), anonymous -> **401**, and no other SEQ read variable exists in the secrets file. The task's own exit criterion is 'read the event back and confirm its properties' because **Seq accepts anonymous ingest and returns 201 to a dead key or no key at all** - so there is no substitute for a read key | **Mint one temporary read-only key in the Seq UI** (read API scope, revocable after the soak) and place it on CT110 as `SEQ_READ_KEY_PP_PROD`, mode 600 harness:harness - the same delivery you used for the ingest key on 09-10, which is proven: worker processes inherit that file. Read-back: from CT110, GET /api/events?count=1 with it returns **200**, not 403 | Task 135 lands its config + hardening change and opens its PR (the run was told to do exactly that), but **stops one step short of VERIFIED** - the shipped logs are never proven authenticated, and the same gap blocks tasks 137 and the other Seq tasks that must read their own events back | **CLOSED 2026-09-21 16:05 PT - DELIVERED AND ACCEPTED.** Paxton minted a Read-only Seq key; `SEQ_READ_KEY_PP_PROD` is in CT110 `/home/harness/.lh-harness-secrets.env` (1 line, 20 chars, 600 harness:harness, backup `.bak-20260921-230225`). ACCEPTANCE from CT110: `GET /api/events?count=1` -> **200 with the key, 401 without**, on both https://seq.easybutt0n.ai and http://192.168.21.128:5341. The service loads the file via systemd EnvironmentFile, so **a harness service restart (zero-active window) is needed before RUNS see the new variable**. Revoke the key in the Seq UI after the soak - its value passed through an interactive transcript. |
| 107-dev-identity | A **dev-tenant test identity** (Entra/MSAL user in the powerplatform DEV tenant) so the setup flow can be walked end to end on dev | CREDENTIAL | Task 107 is LIVE on dev (all three items on `develop`, `app:sha-3bd9040` serving at 192.168.21.163:3000) but its own note defines VERIFIED as 'a new user completes setup end to end on dev'. Measured tick #376: a real browser on 163:3000 **302s to /login**, and `e2e/e2e-msal-example` in the repo shows dev auth is MSAL/Entra - no overseer tick can complete that flow without an identity. Searched queue/done/blocked/OPEN-ASKS: no seeded dev account exists **SECOND TASK NOW WAITS ON THIS IDENTITY - measured by SCHEDULED TICK #509, 2026-09-17 18:45 PT.** Task 109's run `3cbedb2f` gated at round 9/14 (approval `6294daf89bbe`) asking for the very same thing in different words: an authenticated dev-lane browser session for the session-flow specs, offered as (A) an MFA'd `.visual-profile` on a host that can reach 192.168.21.163:3000 or (B) `AI_DEV01` credentials. Neither artefact exists anywhere on the fleet; both reduce to this row. The tick resolved the gate `continue` rather than escalating a duplicate, directing 109 to write the auth-dependent specs IN FULL behind a single guard `test.skip(!process.env.PP_DEV_E2E_USER, ...)` - i.e. the env-var names recommended in this row are now BAKED INTO 109's tests, so minting the identity closes both tasks with no further code change. | **Create one throwaway Entra user in the DEV tenant only** and hand the credentials to CT110's secrets env by name (e.g. `PP_DEV_E2E_USER`/`PP_DEV_E2E_PASSWORD`); scope it to dev, never uat/prod. Alternative if you would rather not mint an identity: **downgrade 107's exit criterion to LIVE** and record that end-to-end setup is verified by the repo's own e2e suite instead | **Task 107 stops at LIVE (dev) and never reaches VERIFIED** - it will sit as the one row in the ledger that cannot close, and the same gap will block every future 'verify on dev' task in powerplatform  **STRENGTHENED 2026-09-17 19:33 PT, SCHEDULED TICK #516: the dependent code is now ON GITHUB, not just on a run's disk.** Task 109's branch is pushed and **PR #117** is open into `develop` (19 files, +2217/-16), and the five session-flow specs in it are gated on exactly `PP_DEV_E2E_USER`/`PP_DEV_E2E_PASSWORD` - the names recommended in this row. Without the identity every one of them exits code 2 as `skip-never-pass`, which the PR deliberately treats as NOT green. So minting one throwaway dev user now closes task 107 AND turns 109's five specs from permanently-skipped into a real gate, with no code change in either. | **CLOSED 2026-09-21 18:22 PT - DELIVERED.** Paxton created the DEV-tenant user `Ai-dev01@cognizio.company` and handed the credentials to the session; placed on CT110 via deliver-secret.sh (backup `.bak-20260922-002132`, 600 harness:harness, 1 line each, pw len 18). NOT yet verified against MSAL - the first real proof is task 109's session-flow specs going green (and 107 reaching VERIFIED) after a harness service restart makes the vars visible to workers. Paxton: confirm the user is excluded from MFA/Conditional Access, or the headless login will still fail. Password passed through a transcript - rotate after the first green run if you care. |
| 56-eval-env | Evaluation environment provisioning: a VM, a registered build agent, and two named environments only Paxton can create | CREDENTIAL | Task 56 Phase 1; lowest priority by Paxton's own direction (09-09) | Do it when convenient; nothing waits on it | Nothing waits on it | **CLOSED 2026-09-23 00:19 PT - Paxton, directly to the interactive session [5321285c] in this session: this row was already closed and should not have rendered as waiting on him.** The decision is unchanged and stands: KEEP DEFERRED, LOW, do not re-ask. State prefix normalised to CLOSED so the board filter and any tick read it as closed; only the prefix changed, not the decision. (prev: **ANSWERED 2026-09-21: KEEP DEFERRED (Paxton, after a full explanation of what it entails). Stays LOW. Do not re-ask.** (prev: open, LOW - added to the table 2026-09-16 22:49 PT so it survives the table-rendered board)) |
| 87-copilot-phase2 | Task 87 Phase 2 (the BLOCKING stop gate): (a) mint the fine-grained PAT + set the org secrets per the task's section 5, and (b) authorise SPENDING Copilot credits on the two pilot PRs | CREDENTIAL + SPEND | Run `20260917T121125Z_61ad4c06` gated at round 2/12 (approval `4928b9f9666c`) asking for exactly this. Phase 1 is already done and no longer waiting: the overseer pushed the run's commit `1211024` (blob `0d8a7297` verified byte-for-byte off CT110) as **PR cognizioware-mcp-tools#162**, because a CT110 run token refuses `.github/workflows/` pushes by design. Phase 2's stop gate needs a MEASURED credit cost from two live pilot reviews against a **1,468/1,500** balance with a **$0 overage budget** - no run can make that spend, and no overseer tick should make it unasked | **Defer, and say so.** This is Paxton's own lowest-priority task (moved to the end of the queue 2026-09-09 12:50 PT) and the money is real: exhausting the pool stops Copilot everywhere, including IDE completions and Chat. Recommended answer: **leave 87 blocked, merge #162 on its own merits** (it fixes a false-green reviewer job, independent of Copilot), and revisit Phase 2 only when the credit cycle resets or you want the reviewer badly enough to spend | **Task 87 stays BLOCKED indefinitely and nothing else waits on it.** #162 is the whole of its shipped value; phases 2-5 never start. The overseer will not re-ask | **CLOSED 2026-09-21: Paxton - DEFER AND CLOSE THE ASK. 87 stays blocked; merge #162 on its own merits; no Copilot spend; do not re-ask until he raises it.** (prev: open, added 2026-09-17 05:53 PT by SCHEDULED TICK #415) |
| ollama-cloud-mem-cap | Cap (or repoint) the **uncapped** host container `cognizioware-ollama-cloud` on ptait01, which is OOM-killing the host | PROD-WINDOW (host-level change on ptait01) | Measured tick #480 2026-09-17: **three global OOM kills of `llama-server` in 25 minutes** (13:10:52, 13:17:00, 13:35:49; `dmesg -T`, anon-rss 6.9-9.2 GB each). Victim model is **`ornith-1.5:9b-256k` with a 262,144-token context** (`ollama ps`, and `-c 262144` on the llama-server cmdline) loaded into a container with **`mem=0`, i.e. NO limit** (`docker inspect .HostConfig.Memory`), on a host with 42 GB total and 35 GB already used. Same window, CT202 (.161) - the prod LiteLLM router, the fleet wallboard, ops and the MCP gateway - stopped answering and had to be rebooted by this tick. The alias `ornith-1.5:pool` is bound as the EXECUTOR of harness tasks (task 190 was one), so any run that binds it can repeat this | **Do both, and they are small:** (1) put a memory limit on the container (~12 GB leaves headroom on a 42 GB host), and (2) stop serving the **256k** variant to the `ornith-1.5:pool` alias - a 262,144 KV allocation is what makes it unsurvivable, and no harness role needs it. **HAZARD, already recorded:** the ptait01 inference stack must be brought up with `docker compose --env-file ...`; without it `OLLAMA_API_KEY` is blank and the :11438 relay breaks. Read-back: `docker inspect` shows a non-zero Memory, and `ollama ps` no longer lists a 262,144 context | **Task 190 stays BLOCKED** (tick #480 moved it to `blocked/` precisely so the launcher cannot relaunch that executor), and any OTHER task that binds `ornith-1.5:pool` can OOM the host again and take the prod router down with it. The overseer will not make a host-level change on ptait01 unasked - that is your standing rule | **DONE 2026-09-22 00:26 PT [5321285c] - both fixes applied in the zero-run window and read back.** FIX 1: compose backup taken, mem_limit/memswap_limit 12g under ollama-cloud, config valid, up -d with --env-file, docker inspect mem=swap=12884901888, relay :11438 answers, OLLAMA_API_KEY present in the container. FIX 2: two POST /model/update (needed model_info.id in the body; the first attempt without it was a 400), GET /model/info reads ollama_chat/ornith-1.5:9b on both pool rows, api_base and rpm unchanged; a live pool call loaded ornith-1.5:9b on :11441 with ctx 16384 (no 262144 anywhere). Residual: model_info.max_input_tokens still advertises 262144 on both rows (the update ignored it) - cosmetic, noted on the runbook. Task 190 released to the queue. |
| kb-hook-order | Promote **task 199** (`9999zd-199-kb-hook-audit-poison`) above **task 109** in the active band, or leave it at the tail? | PRIORITY (yours by rule - the overseer does not reorder) | Proven by TICK #481 at 14:09 PT: `cognizioware-powerplatform`'s **git-tracked** `.claude/settings.json` runs `kb-article/kb-hook.py` on PostToolUse Bash/Edit/Write **and on Stop**, so it fires inside the harness **auditor** window, writes `kb-article/kb-hook.log` into the tree, and the integrity check returns `Integrity: violation` - the auditor's real findings are demoted to diagnostics-only. Task 169's run `f23e48d2` burned **all 8 rounds** on it (rounds 1, 3, 4 and 7 each invalidated by that one path) on an entirely Synthetic trio, produced no commits, and is now BLOCKED. **109 is at the band head and runs in the same workspace**; 156 does too | **Promote 199 above 109.** It is a small develop-only change (guard the hook on `LH_HARNESS_CLAUDE_ROLE` + keep the log out of the tree) and every pp task behind it audits cleanly once it lands. I have NOT moved it - only you change priority | 199 stays at the band tail; **109 launches into the same poison and is likely to burn its rounds the way 169 did**, and 156 after it. Nothing breaks, but pp round-time keeps being spent on audits that cannot count | **CLOSED AS MOOT 2026-09-21: task 199 already launched 2026-09-18 (run 20260918T091826Z_b36816e7) and the band has since drained; there is no ordering left to decide.** (prev: open, added 2026-09-17 14:11 PT by TICK #481) |
| pp-116-main-base | **pp PR #116 is based on `main`, and merging it deploys powerplatform PROD with every gate bypassed.** Merge it as a deliberate prod deploy, rebase it onto `develop`, or leave it open? | PROD-WINDOW | **Measured by TICK #488 at 15:29 PT, each link read from the live files on `main`, not from prose.** PR #116 (`fix/ruflo-drift-preflight`, TASK-165, author prax211, created 2026-09-17T18:31:05Z = 11:31 PT) is **MERGEABLE**, base **`main`**, 5 files +97/-40, 0 comments, checks build=SUCCESS gate=SUCCESS. `main...head` = **ahead 1, behind 0**, so it merges by fast-forward; `develop...head` = **34 ahead / 169 behind**, so the commit is NOT on develop's lineage. **The doctrine said a push to main cannot deploy prod. THAT IS STALE AND IT ERRED ON THE UNSAFE SIDE:** `ci.yml` (3,961 bytes) contains **zero occurrences of `PROD_PROMOTED_SHA`** - the reader was removed, not just the writer, so nothing fails closed. Live chain: `on.push.branches: [main, develop, release]` -> job `image` (`if: push`) -> job `deploy` (`if: push && vars.LAN_DEPLOY_ENABLED == '1'`, **and that variable IS `1`**, read from the repo variables API) -> `rollout.sh "sha-${GITHUB_SHA:0:7}" "$GITHUB_REF_NAME"`. `rollout.sh` filters `deployments/instances.json` by that branch arg and the `branch: main` instance is **`prod`, host `pve151`, health `https://powerplatform.easybutt0n.ai/api/health`**. `promote.yml:483` calls **the identical `rollout.sh <sha> "main"`** for its prod tier - and `promote.yml` contains **no `git push`, no `update-ref`, no `refs/heads` write anywhere in its 30,714 bytes**, so the old "promote.yml fast-forwards main, main FOLLOWS prod" line is false too. Net: merging #116 runs the same prod rollout a promotion runs, with **no eval-score gate, no `environment: production` protection and no `is_production` acknowledgment** | **Do NOT merge it as an ordinary PR. Rebase the branch onto `develop` and retarget the PR there**, so it ships dev -> release -> prod through `promote.yml`'s eval gate like everything else; if the ruflo pre-flight fix is genuinely wanted in prod NOW, say so explicitly and I will merge it as a declared prod deploy in a window with zero active runs. **Separately worth fixing as its own task: restore a guard on the `deploy` job so a bare push to `main` cannot reach prod** - that protection is currently absent for the whole repo, not just this PR | **I have not merged it and will not.** #116 sits open indefinitely, which is SAFE - but the underlying exposure stays: **any** PR retargeted to `main`, or any direct push to `main` by anyone, ships prod ungated, and no ledger row or surface would warn first | **DONE 2026-09-21: Paxton chose RETARGET. pp #116 base changed main -> develop (read back). It now reports CONFLICTING/DIRTY, so task 209 (`9999zn-209-pp116-reconcile-onto-develop`, continuation) is queued to merge develop into the branch. The prod-bypass exposure for THIS PR is gone; the missing guard on pushes to main is still a separate, unowned gap.** (prev: open, added 2026-09-17 15:29 PT by SCHEDULED TICK #488) |
| stranding-readmission | Readmit the ~18 live tasks among the **24 strandings** at TAIL prefixes (a few per tick as slots free), or leave them parked? | PRIORITY (yours by rule - readmission at their ORIGINAL prefixes would jump the whole band) | **TICK #483 read all 24 snapshots - the first tick in ~400 to ask WHY they failed. NOT ONE failed on its own task content.** All 24 died in the provider layer between 09-08 and 09-11: **14 x 429** RateLimitError, **4 x 400** (3 x `Ollama_chatException KeyError: 'messages'`), **3 x 402** quota/billing, **2 x 403** key-not-allowed, 1 x worker-vanished. That is the same provider-outage era the doctrine already records ('19 of 24 on 09-09 were one missing fallback') - these are its fallout, and the tasks were never tried on their merits. Full evidence + per-task table: `C:/tmp/queue/STRANDING-DISPOSITION.md`. **Two are already SUPERSEDED by subject** (`002-115-spendlog` by active `9999j-170`; `995-133-reviewer-hydra` by `007-116`, hydra's reviewer landed in PR #26) - a task-NUMBER check finds no successor for any of the 24, so a number check alone would have readmitted both as duplicates | **Readmit the ~18 non-superseded tasks at TAIL prefixes**, task number preserved, identity stripped, provider cause in the note, **a few per tick as Synthetic slots free** - not in bulk, the band is already holding at `SYN_CAP 3`. Close 115 and 133 as SUPERSEDED naming their successor. I have NOT readmitted any: doctrine says 'requeue at its original prefix', but those prefixes (`0000-`, `0004-`, `001-`, `002-`) sort to the HEAD and would jump all 16 active tasks including prod work - that is your call, not mine | The 24 stay parked and the ledger keeps counting them forever. Nothing breaks - but ~18 tasks that failed only because a provider was throttled are never retried, and the count goes on being reported without meaning | **DONE 2026-09-21: Paxton chose POWERPLATFORM ONLY. Of the 24, four are pp: 53 and 59 READMITTED at the tail (9999zl-53, 9999zm-59; 59 carries the Synthetic-only prod rule); 101 and 108 CLOSED AS LANDED (pp #105 merged 09-17; pp #106 merged 09-10) - they were never strandings. The other 20 stay parked by his choice; count is now 20, do not re-ask.** (prev: open, added 2026-09-17 14:42 PT by SCHEDULED TICK #483) |
| 150-spec-staging-decisions | Task 150 (`1007-150-spec-staging-land`, blocked/) needs two architecture calls before it can be built toward deploy: **(SS8)** how the file-queue talks to QueueStore, **(SS9)** whether the double-spend history-cap fix applies uniformly across the vendored eval harnesses | PRIORITY/ARCH (not a credential/spend/prod-window, but the task's own note reserves both to Paxton by name - the note wins over the usual 3-kinds filter) | Found this tick (2026-09-19) while correcting tick #855's claim that the branch's uncommitted WIP was "orphaned, not a tracked queue task" - it is tracked, as task 150, deliberately BLOCKED. Its note: SS8 is the file-queue -> QueueStore bridge; the building session recommends **option 1 (Mirror)**, and calls option 3 (porting probe/rotation/ramp into `launcher.py`) HIGH RISK, "must not be chosen unilaterally". SS9 is the role_history_chars double-spend fix (task 94's follow-through; main still spends it twice at `role_prompts.py:58,60`, the branch fixes it at `72,74`) replicated into the vendored eval harnesses, but whether `0` means unlimited or none **differs per copy** and must be read, not assumed. Neither decision has appeared in this table before | **SS8: take the building session's own recommendation, option 1 (Mirror)** - lowest risk, and the note itself rules out option 3. **SS9: read each vendored copy's own semantics before editing it and keep its existing per-copy convention** rather than standardizing on one meaning for `0` | Task 150 stays BLOCKED; the branch (18+ commits ahead of main, plus uncommitted WIP) keeps sitting unpushed and nothing is built or deployed toward it | **DONE 2026-09-21: BOTH recommendations accepted - SS8 = option 1 MIRROR, SS9 = each vendored copy's own semantics. Recorded on the task. 150 stays blocked ONLY for its zero-active deploy window; its work exists solely in Paxton's unpushed local tree (feat/spec-staging, 22 uncommitted files).** (prev: open, added 2026-09-19 07:10 PT by interactive [ed5631]) |
| mcp-177-workflow-merge | Merge **mcp-tools PR #177** (`ci(uat): build + recreate CT204 app-level containers in the UAT deploy lane`) — it edits `.github/workflows/deploy-mcp-tools.yml` (+90/-3), base `main` | PROD-WINDOW (CI/CD pipeline edit — deploy-lane workflow file) | Logged by interactive [5321285c] 2026-09-21 09:05 PT: mergeable CLEAN (GitHub recomputed post other merges), but the session's own operating rules treat a workflow-file change as hard-to-reverse/shared-infra and will not merge it without explicit sign-off ("classifier-refused, needs Paxton"). Re-checked this tick (09:07 PT): 1 file, `.github/workflows/deploy-mcp-tools.yml`, mergeable=UNKNOWN (GitHub still recomputing) | **Yes, merge it** — it only adds a UAT-lane build/recreate step for CT204 app containers, no prod path touched, and #171/#146/#168/#170 (also workflow-adjacent or CI-touching) merged clean this iter with no incident | PR #177 sits open; the UAT deploy lane keeps not building/recreating CT204 app-level containers on its own, so that step stays manual until someone merges it | **CLOSED 2026-09-21 - MERGED by Paxton** (mcp-tools #177). |
| uat-fleet-admin-env | CT204 (UAT) mcp-tools.env has neither `FLEET_DATABASE_URL` nor `FLEET_ADMIN_KEY`; the fleet-admin container that mcp-tools #177 now recreates on UAT crash-loops on connect ECONNREFUSED 127.0.0.1:5432 (empty DSN). Prod CT202 has both names and its fleet-admin is healthy. Decide: (a) UAT shares prod agent-db (copy both values from CT202 env, names only ever printed), or (b) UAT gets its own DSN/key. | credential | measured 2026-09-21 20:42 PT: UAT env names lack FLEET_*; container restarts=9+; the deploy lane does NOT assert fleet-admin health, so this does not block the lane | (a) copy from CT202 - one agent-db, schema fleet, is what the compose comment describes | if silent, the overseer copies both from CT202 to CT204 at the next zero-run window and reads back a healthy container | **CLOSED 2026-09-23 - PAXTON ANSWERED DIRECTLY (asked in-session, his selection, NOT a session summary): OPTION (a) - GIVE UAT ITS OWN FLEET DB AND KEY.** He was re-asked WITH the overseer's 12:56 PT evidence in front of him (prod fleet DB is a LIVE device registry taking ct110 heartbeats since 16:04Z after LH #40), because his 09-22 'copy from prod' was given when that DB looked inert. **The overseer was RIGHT to withdraw its default and hold - do not copy prod's DSN to CT204.** ACTION: provision a separate fleet database + key for UAT (same shape as the CT103 lh_harness runbook: least-privilege role, own schema, password generated on the consuming host, only a verifier crosses hosts), set FLEET_DATABASE_URL and FLEET_ADMIN_KEY in CT204 mcp-tools.env by NAME, read back a healthy fleet-admin container. Until then the UAT container keeps restarting - harmless, nothing routed, no lane blocked. (prev: **DEFAULT WITHDRAWN 2026-09-22 12:56 PT [5321285c] - I am NOT copying it silently, because new evidence change) |
| 177-lhharness-upstream | Task 177 runbook hit its own STOP condition at step 2: the CT202 gateway has NO `lhharness` MCP server registration, no `lh-harness` keys by alias, no `fleet-chat` team and neither harness group (30 groups, none of them harness-chat/harness-operator), and mcp-tools.env carries no LH_HARNESS_* names - so the LongHorizon-Harness WebAPI MCP-bridge URL and bearer cannot be determined from the box. Provide (a) the bridge URL the gateway should call (CT110 :8799 path?) and (b) which credential to use (the CT110 LH_HARNESS_WEB_TOKEN, or a dedicated one), or say the bridge is not wanted yet. | credential | read-only probe 2026-09-22 00:34 PT [5321285c]: GET /v1/mcp/access_groups, /v1/mcp/server, /key/list, /team/info; #169 stays unmerged because the E2E gate runs RUN_LIVE_REGRESSION=1 and its suites would hard-fail | **CORRECTED 2026-09-22 12:26 PT (SCHEDULED TICK #1519 [24e28d]) — THE PREVIOUS RECOMMENDATION WAS NOT EXECUTABLE AND IS WITHDRAWN.** It said "(a) http://192.168.21.168:8799 MCP bridge + the existing web token". Measured this tick against the live CT110 WebAPI, with the token: `/mcp` 404, `/mcp/` 404, `/api/mcp` 404, `/sse` 404, while `/api/meta` 200 and `/api/runs` 200 on the same reader — so the API is alive and the reader is sound; **CT110 simply exposes no MCP endpoint at all.** The harness WebAPI is REST, not MCP, so there is nothing for the gateway to register as an MCP server and the old answer would have had someone register a URL that 404s. Independently measured first by peer session [61855af8] at 19:21:55Z and re-proved here before recording. **REPLACEMENT RECOMMENDATION: either (a) the gateway consumes the CT110 REST API via an adapter rather than a native MCP registration, or (b) task 177 is deferred until the harness actually serves MCP. Paxton is working this thread live in [61855af8]; that session reports `hydrafleet` already exists on the gateway and was checking whether it already covers what 177 needs — prefer its finding over this row if it lands first.** | if silent: nothing is created and #169 stays open; no keys are minted, no executor strip happens | **CLOSED 2026-09-23 - NOT A CREDENTIAL ASK AT ALL; ANSWERED BY MEASUREMENT IN [61855af8], sub-question included.** (1) OPTION A CONFIRMED: there is no `lhharness` alias (all 52 registered servers enumerated); use the EXISTING `hydrafleet` -> https://hydra.cognizioware.com/fleet-mcp, which already carries enqueue_task / list_fleet_runs / get_run_snapshot / resolve_run_gate. CT110 :8799 has no MCP endpoint (4 paths, all 404) - the harness WebAPI is REST, so there is nothing to register and NO URL OR CREDENTIAL IS MISSING. (2) THE SUB-QUESTION IS NOW ANSWERED, MEASURED LIVE ON THE PROD ROUTER: per-tool scoping DOES work - server registrations carry an `allowed_tools` list and **`stripe` uses it today: 10 declared -> exactly 10 `stripe-*` served**. (3) BUT `allowed_tools` belongs to the SERVER REGISTRATION, not the access group, so two groups with different tool sets REQUIRE TWO REGISTRATIONS of the same URL - the split the task text anticipated is the REQUIRED mechanism, not a fallback. No URL is registered twice today (52 servers / 52 URLs), so prove the pattern on a throwaway alias first. RECOMMENDED SHAPE (written onto task 177): `hydrafleet-chat` (enqueue/list/status) + `hydrafleet-operator` (+resolve_run_gate), both -> the same fleet-mcp URL, created via the **/v1/mcp/server admin API, NOT litellm-config.yaml** (a yaml edit force-recreates the prod router; these registrations are DB-owned). Scope 3 (strip hydra/hydrafleet from EXECUTOR keys) unchanged. #169 can proceed. (prev: open, added 2026-09-22 00:34 PT by the interactive overseer [5321285c] **NEW ANSWER 2026-09-22 23:2x PDT (SCHE) |
| nebo-190-gate-b | Nebo outbound STOP GATE B (task 190, PR cognizioware-nebo #2 OPEN at def4b8d, MERGEABLE, unreviewed): (1) Dataverse privileges for the outbound campaign records, (2) Entra app registration, (3) provision LXC `nebo.easybutt0n.ai`, (4) one live single-lead send to verify | CREDENTIAL + PROD-WINDOW | Run af6b0651 finished 10/10 rounds 2026-09-22 03:13 PT; auditor rounds 5/6/9 verified Steps 2-6 + DEFECT-1/2 on the branch, npm test 61/61, DRY_RUN true throughout, second dry-run pass = 0 sends. Live Dataverse read-only proof and G2 header proof are DEFERRED because no .env credentials exist on CT110. PR body carries all four statements; tick #1413 read PR state from the GitHub API | **REVIEW HOLD since 04:25 PT (interactive [5321285c]): #2 repoints the send nodes from api.telnyx.com back to $env.CLAWDTALK_SERVER (the host PR #1 recorded as dead) and drops the hive-api outreach gate. Overseer clears three checks first (read the run's routing note in full, confirm CLAWDTALK_SERVER is live today — **CHECK 2 OF 3 IS NOW ANSWERED, 2026-09-22 12:26 PT (SCHEDULED TICK #1519 [24e28d]): `CLAWDTALK_SERVER` IS UNSET, NOT MERELY DEAD.** Enumerated every CT on corsairai300 (105, 106, 110, 120): zero occurrences of `CLAWDTALK_SERVER=` in any container's environment, and no file under /root, /opt or /srv on the host mentions the name. So PR #2's send nodes resolve `$env.CLAWDTALK_SERVER` to empty — the reroute does not point at a dead host, it points at nothing. Independently measured first by peer session [61855af8] at 19:21:09Z and re-proved here. **THE REVIEW HOLD IS NOW STRONGER, NOT WEAKER: do not merge #2.** Remaining checks: (1) read the run's routing note in full, (3) confirm the outreach-gate removal was intended, confirm the gate removal was intended) - those are overseer work, not yours. Then (1)-(3) when you want outbound live; (4) is yours alone, it emails a real person.** Overseer will not merge it unreviewed and will not invoke @claude | PR #2 sits open unreviewed; task 190 stays at PR and never reaches LIVE - nebo has no deploy lane and no LXC. Nothing else in the queue waits on it | **CLOSED AS AN ASK 2026-09-23 00:19 PT - Paxton, directly to the interactive session [5321285c] in this session.** The reroute question was settled by tick #1609 on his direct ruling ("clawdtalk is deprecated, remove it and all parts"): revert the send nodes to api.telnyx.com, restore the hive-api outreach gate PR #2 dropped, and do not merge #2 as-is; execution is owned by task 217 step 3. **NOTHING IS ERASED BY THIS CLOSE:** Gate B items (1) Dataverse privileges, (2) the Entra app registration, (3) the nebo.easybutt0n.ai LXC and (4) the one live single-lead send remain real outstanding work - they are now tracked as TASK work on 190/217, not as an overseer ask, which is why this row closes. If that reclassification is wrong, say so and it reopens. (prev: open, added 2026-09-22 by SCHEDULED TICK #1413 **CHECK 3 OF 3 IS NOW ANSWERED, AND THE WHOLE REROUTE QUESTION WITH IT - 2026-09-22 23:2x PDT (SCHEDULED TICK #1609 [f41eec]).** Paxton ruled directly ('clawdtalk is deprecated, remove it and all parts'); the peer session recorded the consequence: **REVERT THE SEND NODES TO api.telnyx.com AND RESTORE THE hive-api OUTREACH GATE PR #2 DROPPED.** So the outreach-gate removal was NOT intended, and #2 must not merge as-is. This is now a DECISION, not a hold pending overseer checks. **Execution is owned by TASK 217 STEP 3** (`9999zr-217-clawdtalk-deprecation-removal`, launched by this tick, run `20260923T062359Z_5879857b`). What REMAINS Paxton's on this row is only GATE B items (1)-(4): Dataverse privileges, the Entra app registration, the `nebo.easybutt0n.ai` LXC, and the one live single-lead send. If he says nothing: 217 lands the revert as a PR, #2 stays unmerged, outbound stays dark.) |
| 168-trio-agreement-scoring | Cutover 168 promotion evidence item 1 ("same entry selected, SAME TRIO") cannot be met as written, and the migration doc contradicts itself on it. Measured 2026-09-22 20:56 PT: the PC launcher rotates six trio permutations by a durable monotonic counter (`_TRIOS`/`_trio(i)`, launch_queue.py:180-226) and diverts on live Synthetic health; the CT110 launcher resolves a STATIC `[queue.trios]` map keyed kimi/qwen, whose shipped defaults carry model = null (probe on 2026-09-22 recorded roles with model None). The doc's own section 7 acknowledges the harness uses static trio config and assigns dynamic routing to task 49 - yet section 5 demands trio agreement. Decide how item 1 is scored: (a) score it on ENTRY SELECTION and launch/skip only, with trio explicitly out of scope for this migration (doc amended), (b) port dynamic selection into the harness launcher first (that is task 49 work, weeks), or (c) pin the PC launcher to one trio for the window (costs throughput and realism). | decision | reproduced against launch_queue.py and origin/main config defaults; shadow probe roles came back model None **CONFIRMED ON A REAL ENTRY 2026-09-22 20:57 PT, not just a synthetic probe:** task 221 was queued and the PC launcher shadow-filed it as q-1be173d258f74892. The PC launched it with `TRIO SELECT 2 (no divert: executor already non-local)` - manager/executor/auditor bound to real Synthetic-pack models. The CT110 shadow launcher recorded `queue.shadow_launch` for the SAME entry with trio "kimi" and models {manager: None, executor: None, auditor: None}. Same entry, same launch/skip decision, totally different trio - which is exactly the shape of the disagreement item 1 would record on every entry. | (a) - the doc already assigns dynamic routing elsewhere, so scoring trio here measures the wrong thing | if silent: the shadow window is NOT started, because as written it would fail item 1 on nearly every entry for a reason unrelated to launcher correctness | **CLOSED 2026-09-23 - PAXTON ANSWERED DIRECTLY (his selection): OPTION (a) - SCORE ITEM 1 ON ENTRY SELECTION AND LAUNCH/SKIP ONLY. Trio agreement is explicitly OUT OF SCOPE for this migration.** Amend the migration doc so section 5 stops contradicting section 7, which already assigns dynamic routing to task 49. Rationale he accepted: the PC launcher rotates six trio permutations by a durable counter and diverts on live Synthetic health, while the CT110 launcher resolves a STATIC kimi/qwen map - so scoring trio here measures a difference the migration is not trying to remove. **This was 168's LAST gate from Paxton** (the boot crash and the workspace-occupancy skip were both cleared 2026-09-22 22:15 PDT by tick #1597). The 7-day shadow window can start as soon as one real LHH entry is queued in the normal course. (prev: open, added 2026-09-22 20:56 PT by the interactive overseer [5321285c]. **STILL THE ONLY THING 168 WAITS ON FR) |

## Answered — kept so they are not re-asked

| id | answer | when |
|---|---|---|
| 189-refresh-key | Create a **read-only admin** LiteLLM credential for the nightly fallback-expectations refresh, and store it as the `LITELLM_FALLBACK_REFRESH_KEY` secret on `cognizioware-qa`? | CREDENTIAL | task 189 merged (qa 8b4aa44): `refresh-fallback-expectations.mjs` reads `GET /router/settings`, which answers **401 "Only proxy admin can be used"** to the QA key; the task forbids the master key in that repo or in CI, so the cron (43 6 * * *) fails until a credential exists. The gate itself is GREEN (run 35265225804, 27/27) - only the refresh is blocked | **Yes - a `proxy_admin_viewer` user + key on CT202, which can read router settings but change nothing.** The overseer can create it; moving the VALUE into a GitHub secret without printing it is the part that needs you (or a permission rule) | The overseer refreshes `fallback-expectations.json` by hand from CT202 when a chain changes, and the nightly workflow stays red as a visible reminder. Chains change rarely - task 136 added ten in one edit | **CLOSED 2026-09-21 18:05 PT - END TO END.** Key delivered (22:54Z); script fix cognizioware-qa #37 MERGED by Paxton 23:03:49Z; dispatched `fallback-expectations` run 35671313196 -> **success**, both steps green, and it opened **PR #38 `chore(litellm): refresh declared fallback chains` (+140/-3)** - the first real refresh from the live router. The nightly job is no longer red. Overseer: review/merge #38 (it is the declared-chain baseline the QA gate will assert against). |
| 187-github-app | Create + install the GitHub App `cognizioware-lh-reviewer` (or choose the org-webhook + PAT alternative) so Hydra receives pull_request events | CREDENTIAL | Task 187 (github-app-hydra-webhook) is BLOCKED on it; step-by-step handoff with the manifest JSON: C:/tmp/HANDOFF-github-app-lh-reviewer-2026-09-15.md (sections 1-3). No gh CLI for App creation - web, ~5 min | **Create the App** (Check Runs + a separate reviewer identity). Alternative: org webhook + add Commit statuses RW to the PAT (statuses instead of check runs, single identity) | Task 187 stays blocked; PRs keep piling with no reviewer; @claude/Copilot fallback never wired | **CREDENTIAL HALF CLOSED 2026-09-21 17:05 PT - APP CREATED, INSTALLED, SECRETS ON HYDRA, TOKEN MINT PROVEN.** App `cognizioware-lh-reviewer` id 5026860 (events pull_request/check_suite/check_run/issue_comment; perms checks:w contents:r issues:w pull_requests:w statuses:w), installation 163626033 (selected repos - Paxton to confirm the eight). On corsairai300: `/opt/cognizioware-hydra/.env` has the 4 names (600), `secrets/lh-reviewer.pem` 600/1675. **3b acceptance from the host: installation token minted (HTTP 201), perms checks,contents,issues,metadata,pull_requests,statuses.** Task 187 still BLOCKED ON 179 (review contract) - code, not Paxton. HAZARD found on the way: Paxton's first attempt put the pem + the 4 lines into the LOCAL hydra repo checkout, where `.env` is TRACKED (commit 13356dd force-tracked it WITH live HYDRA_API_KEY / DEVICE_TOKENS / CLOUDFLARE_TUNNEL_TOKEN). Reverted before any commit; pem moved out of the repo. The committed secrets are a pre-existing defect - see ledger. UPDATE 18:05 PT: Paxton CONFIRMED the App is installed on the eight repos; pem deleted from Downloads. Credential side fully closed. |
| 208-rss-deploy-window | **CLOSED BY EVENTS 2026-09-21 00:24 PT (interactive [5321285c]).** The window it asked for was TAKEN by scheduled tick #759 on 2026-09-18 21:26-21:29 PT, following C:/tmp/RUNBOOK-worker-rss-bound-live-verify.md, right after task 211's self-relocation fix landed. Verified on CT110 by the interactive session on 09-19, from the box not the report: /etc/systemd/system/lh-harness.service carries `Delegate=memory pids` and `OOMPolicy=continue`; the service cgroup's controllers read `memory pids`; zero processes sit directly in the service cgroup (MainPID lives in the `main` leaf); a real episode (run 20260919T042718Z_c2ccaecb) ran in its own child cgroup with memory.max = 2147483648 (2 GiB) and memory.peak = 195,563,520 (0.18 GiB), and its owner record names mechanism `cgroup-subtree`. All four tests/test_worker_rss_bound.py tests passed live. A2 and A3 are closed; nothing remains to schedule | 2026-09-21 00:24 PT |
| 184-band-order | **CLOSED BY EVENTS, not by a reply — retired 2026-09-18 11:15 PT by SCHEDULED TICK #682 on the interactive session's instruction.** The question ('move 184 to the band head or leave it at the tail?') was answered by action: 184 was promoted from `9999v-` to **`007b-`**, launched as run **`20260918T072506Z_5378bb86`**, and is now in `done/` having produced **mcp-tools PR #171** — so the row's central evidence ('184 has `run_id: None`, it has never launched') is false as of today. **THE UNDERLYING RISK IS NOT CLOSED AND IS TRACKED ON TASK 184 ITSELF:** measured live on CT202 18:13:33Z this tick, **48 running containers, only 8 capped = 40 uncapped**. PR #171 adds two `mem_reservation` lines and **zero `mem_limit`**; reservations are soft floors, not bounds, so **#171 must not be allowed to close 184**. What DOES now work is the azure-mcp 4 GiB cap from #153: it fired on a leak recurrence ~11.4 h in (`OOMKilled=true`, `restarts=0`, still up) and contained it, with the router untouched at 6 GiB — caps work; absence of caps is what selects a stranger | 2026-09-18 11:15 PT |
| pp-8210a96-live | **CLOSED — live on dev, not prod.** Merged to develop as `ff690e1` (PR/review skipped, owner instruction, docs-only). dev `sha-ff690e1`; uat and prod both `sha-f2a3633`. Mechanism verified from the workflow files and corroborated by a peer: `ci.yml:6` branch-per-env (`develop`->dev, `release`->uat, `main`->prod), `promote.yml` dispatch-only and fast-forwards main to the promoted sha, `deployments/instances.json` agrees. Docs-only, so nothing to verify in the served app — it rides the next real promotion | 23:00 PT |
| 69b-token | **RESOLVED 22:25 PT.** Paxton created **`HYDRA_REPO_READ_TOKEN`** in `cogniziocompany/cognizioware-qa` (verified present by name; value never handled). 69b moves from "test `HYDRA_API_KEY` empirically" to **option (a) with a real credential** — the three hydra checkpoints RUN instead of skipping, and option (c) becomes the failure path only. **Retirement debt recorded:** this is the FIRST cross-repo checkout secret in that repo (the audit of 16 existing secrets found no `actions/checkout` precedent), so if cognizioware-qa moves to a GitHub App or an org-level secret, this token is the thing to retire | 22:25 PT |
| 125-scope | **Option (a)**: new action-name write path on the EXISTING ops-control-center audit ingest. RULE 2 forbids duplicate surfaces and the task text already says extend the audit route. Build + PR allowed now; **deploy to CT202 ONLY in the 144 window** | 20:25 PT |
| 69b-Q1 | 69a is **COMPLETE** (8 min). Three checkpoints: **`hydra-device-register`, `hydra-device-session`, `hydra-device-cleanup`**, all dereferencing `HYDRA_REPO_DIR` at lines 168–171 (directly in register, via the spawned process for the other two). Red continuous since ≥2026-09-06. Source: `C:/tmp/resolve69a.json`, written 18:41 | 18:41 PT — **the overseer already had this and escalated anyway** |
| hydra-26 | **Merge inside the drain window**, right after #140 and before the repack (it deploys corsairai300, not CT202). Task-116 BMAD gate **WAIVED** — 111 proved BMAD cannot run as a generator. Billing `@claude` to Paxton's subscription on hydra is **accepted** | 20:25 PT |
| push-handoffs | **RESOLVED.** `8210a96` is the tip of `origin/ci/env-promotion` with `docs/handoff/PLAN-powerplatform-session-ux-fixes.md` present. The runner handoff is on origin as **`5e8f15d`** (tip of `easybutt0n-runner-v2`) - **NOT `3b5042a`, which is an orphaned local commit with a byte-identical message.** Tasks 152 and 154 were corrected to cite the right commits, and 154 now warns the doc is NOT on `develop` (its base) | 21:25 PT, verified by ls-remote + merge-base + ls-tree |
| mcp-cogn-gh | **SUPERSEDED 2026-09-14: runs push and open PRs themselves (GH_TOKEN on CT110, service restarted 16:21); merge stays the overseer's.** Earlier: DECIDED: make it overseer-executed, like 144/147/149/150. CT110 has no `gh` and no GitHub token, so PR-centric work is structurally impossible for a run - not a capability to add per-task. Entry moved to `blocked/` and the run stopped | 21:30 PT |
| 135-ssh | **RESOLVED 21:33 PT.** The CT110 `harness` public key (`SHA256:Yd0Wan9IugV02Z7RlNYTTmqCEEedE8Lf4TgpVqfutE4`, `lh-harness@ct110`) was appended to `root@192.168.21.151:~/.ssh/authorized_keys` (backup `.bak-213351`, 19 -> 20 lines, idempotent guard). **Proven from the host and identity that matter** — from CT110 as `harness`, both the direct `-i` call and the `pve151` alias return `cognizio03-corsairai300`. A relayed report that this was already fixed had NOT reproduced, because it was probed from a workstation as a different user | 21:33 PT |
| 135-seq-key | **RESOLVED.** Paxton created a NEW dedicated key and placed it in the Windows `seq/.env` as `SEQ_INGEST_KEY_PP_PROD`. The overseer verified it is genuinely scoped — **403 on `/api/events`, `/api/apikeys` and `/api/users`** — then delivered it to `/home/harness/.lh-harness-secrets.env` on CT110 (mode 600, harness:harness). The value was moved through a shell variable, base64-wrapped, so it never appeared in a transcript. Worker processes inherit that file (verified) | 22:30 PT |
| seq-anon-ingest | **INTENDED — Paxton confirmed.** Seq accepts unauthenticated ingest (no key and bogus key both return 201). Not a defect to fix. **The consequence still binds:** a 201 from `/ingest/clef` proves nothing about a credential, so task 135 must verify logging by READING THE EVENT BACK and checking its properties, never by the POST status code | 22:30 PT |
| claude-invocations | The overseer **never** posts an `@claude` comment — it bills Paxton's personal subscription (task 117). Only Paxton triggers a review, from his own account | 19:55 PT |

## Not asks — the overseer decides these and records them

- Whether a failed run is a backend fault or oversized scope → the three-strikes discriminator.
- Whether a stale queue row blocks a precondition → settled: `running + waiting_approval == 0`.
- Which PRs are mergeable → read the BASE first; `mergeable` is scoped to it.

---

## CT202 litellm memory leak - a SPEND/PROD-WINDOW decision

**Measured 2026-09-10 22:36-23:25 PT.** `cognizioware-mcp-tools-litellm` grew to **12.7 GB RSS in
8h20m**, exhausted CT202's 16 GB cgroup, and the kernel OOM-killed its `node` siblings - taking
ops.easybutt0n.ai, fleet.easybutt0n.ai and the MCP ssh gateway down for ~21 minutes. ptait01 load
peaked 359. Cgroup counters show this has happened **123 times** (`oom_kill 123`). Two post-restart
samples show it still growing at **~240 MB/h while completely idle**.

**Already done (no decision needed):** CT202 cap raised 16 -> 20 GB on Paxton's go, verified live.
Handoff at `C:/tmp/ct202-memory-handoff.md`.

**~~THE ASK~~ - DONE 2026-09-11 01:00 PT, no window was needed.** `mem_limit`/`memswap_limit` = 6g
were applied LIVE with `docker update` (no recreate, no downtime): `HostConfig.Memory` went
**0 -> 6442450944** while the container stayed `Up (healthy)` and the router kept serving HTTP 200.
PR #143 then merged so the cap survives compose recreates. **The router now dies alone instead of
taking ops/fleet/gateway with it.**

**WHAT REMAINS OPEN HERE IS THE LEAK ITSELF, NOT THE CAP** - that is task 158
(`1014-158-litellm-router-memleak`), which has the reproducible trigger recorded in its task file.

**SIZED AT 6g, NOT 14g** - the 14g figure came from the withdrawn 240 MB/h idle extrapolation. The
measured loaded rate is ~16 GB in ~7 minutes, so a large cap buys nothing and only widens the blast
radius. 6g leaves 14g of CT202's 20g for the siblings, which is the entire point.
**A router OOM under this cap is the cap WORKING and must NOT be answered by raising it.**

---

## ~~Task 157 / PR #143~~ - CLOSED BY THE OVERSEER 2026-09-11, kept for the reasoning

**NO LONGER AN ASK.** Paxton 2026-09-11 told the overseer to solve it at lowest effort rather than
wait. The cap was applied LIVE with `docker update` (no recreate, no downtime) and #143 merged so it
survives future deploys. The `@claude` ban itself is UNCHANGED - the overseer still never posts one.
Original reasoning kept below.

**Why it was yours:** task 117 bans overseer `@claude` invocations - they bill Paxton's personal
subscription. Five were fired in error on 2026-09-10 and four runs were cancelled over it. So the
overseer opens the PR and stops; the review comment is Paxton's single action.

**What it unblocks, concretely:** `mem_limit` on the litellm container. Tonight the router reached
~16 GB **twice** and the kernel OOM-killed its `node` SIBLINGS instead - taking ops.easybutt0n.ai,
fleet.easybutt0n.ai and the MCP gateway down both times. With a container cap the router dies alone
and the operator surfaces stay up.

**It also unblocks two other things that are stuck behind it:**
 1. **PR #142 is merged but NOT deployed.** Its lane's advisory E2E suite reproduces the outage, so
    the deploy can only run safely once the cap exists.
 2. **DRAIN cannot be cleared.** Harness runs route through this router, so resuming launches before
    containment would drive the same leak. Paxton's "both GPUs earning their keep" ask waits on this.

**THE PR IS OPEN: https://github.com/cogniziocompany/cognizioware-mcp-tools/pull/143**
One file, +12/-0: `mem_limit: 6g` + `memswap_limit: 6g` on the `litellm-router` service, with the
reasoning in comments. `litellm-config.yaml` is NOT touched. MERGEABLE/CLEAN.

**RECOMMENDED ANSWER:** Paxton posts `@claude` on #143 once; the overseer reads the verdict, acts on
it, and merges at ACTIVE RUNS = 0.
**DEFAULT IF PAXTON SAYS NOTHING:** the PR sits unreviewed, #142 stays undeployed, DRAIN stays
armed, and the leak recurs on the next burst of router traffic.

---

## RESOLVED 2026-09-11 by the overseer - these are no longer asks

**`pg_repack` is DROPPED from the 144 window.** Not deferred - dropped. `pg_repack` exists to
reclaim bloat, and `LiteLLM_SpendLogs` measures **`n_dead_tup = 0`**: autovacuum is fully keeping
up and there is nothing to reclaim. The 26 GB is LIVE data (~92 KB/row of TOASTed request/response
bodies), which `pg_repack` does not touch. Running it would take a heavy lock on a table the prod
router writes to on every request, in exchange for approximately nothing. **The real size lever is
what gets STORED per row, which is task 125's territory and Paxton's ratio-trigger instruction.**

**Task 157 / PR #143 no longer waits on an `@claude` comment.** Paxton 2026-09-11: *"you have to fix
these, get the best solution with lowest effort to keep the e2e testing going."* The overseer is
still banned from POSTING `@claude` (task 117, unchanged) - it is now merging on its own verification
instead: 1 file, +12/-0, YAML re-parsed under the correct service key, CRLF-clean (+12 exactly),
`litellm-config.yaml` untouched, MERGEABLE/CLEAN, and the existing `mem_limit: 2g` precedent in the
same file. That is a judgement the overseer owns; it is recorded here so it is not mistaken for a
skipped gate.

---

## ~~SYNTHETIC CREDIT EXHAUSTED - a SPEND decision, and it is blocking the queue~~ RETIRED 2026-09-14 10:10 (see 'STALE HEADING RETIRED' below); history only

**Measured 2026-09-11 01:03-01:20 PT from run `failure_reason` and the router's own log.** Five runs
launched; **all five failed**, every one on Synthetic:

    4 x  429  "You've exceeded your subscription rate limits. Upgrade, or try again later."
    1 x  402  "Insufficient quota and token balance: consider upgrading your subscription"
             (run 598bdade, nemotron-3-super:synthetic-anthropic)

*** CORRECTED 01:26 PT - THE OVERSEER OVERSTATED THIS AND IS WALKING IT BACK. ***
The first draft of this row said "the 402 is the important one" and framed it as the WEEKLY CREDIT
being gone - a multi-day outage. **Counted over 45 minutes of router log (23,076 lines):**

    Error code: 429                          219
    "exceeded your subscription rate limits" 307
    Error code: 402                            4
    "Insufficient quota and token balance"     4

**The dominant failure is the RATE LIMIT, not credit exhaustion** - by roughly 50:1. **And all four 402s
belong to ONE deployment:** `model_group=nemotron-3-super:synthetic-anthropic`. Every other Synthetic
model fails with a plain 429 (`syn:large:vision` 132, `glm-5.2` 66, `kimi-k3` 63). That is a
per-deployment quota, **not** the pack-level weekly credit. The 402 is a
minority signal seen 4 times during the burst. A 429 clears on its own; the evidence does NOT
support telling Paxton his weekly credit is gone, and spend is precisely where the overseer must not
overstate. The router does also log `Error doing the fallback ... 429`, so the fallback chain is
rate-limited too and no alias rotation helps - that part stands.

**WHY IT BLOCKS MORE THAN IT LOOKS:** trios 0 and 1 are described as "local", but only their
EXECUTOR is ornith - the manager is `glm-5.2:synthetic-anthropic` and the auditor is
`kimi-k3`/`syn:large:vision`. **2 of 3 roles still need Synthetic**, so even GPU-backed runs die on
this. The ONLY genuinely Synthetic-free configuration is the DEGRADED all-ornith trio, which is
capped at `DEGRADED_CAP=2`.

**CURRENT STATE (2026-09-11, SUPERSEDED - see the 09-16 04:45 recovery at the foot of this section):**
the launcher flipped to `synthetic=DOWN` at 01:19 on its own and is now running
all-ornith at 2 concurrent - one run live (`e0f6e5bc`, 125b), GPU 1 at 95% / 302 W. The queue drains,
but at 2 slots instead of 6, and only on tasks the local model can do.

*** UPDATE 2026-09-16 03:22 PT, tick #240 - THE 09-11 WALK-BACK HOLDS, AND NOW THERE IS A POPULATION BEHIND IT. ***
The correction above was reasoned from 45 minutes of router log. Tick #240 enumerated **536 launcher
status lines** and reaches the same verdict from an independent direction, plus one new fact:

    ok->DOWN transitions:                       25
    cycles with kimi=0 (NO synthetic run live): 416
    ...of those, DOWN anyway:                  165

**165 DOWN cycles with ZERO Synthetic runs of ours live**, including five ok->DOWN transitions with
`kimi=0` before AND after (one with `active` 0->0). **Our own consumption does not drive the DOWN
state.** That also kills a hypothesis tick #240 formed and discarded before publishing - that the
launcher's burst during each recovery window re-exhausts the pack. It does not; n=2 suggested it, n=536
refuted it.

**THE NEW FACT, and it is not reassuring:** availability is falling monotonically.

    last  536 cycles: duty  59.9%      last 100 cycles: duty  18.0%
    last  200 cycles: duty  52.5%      last  25 cycles: duty  16.0%

(Ordered by line number - `launch_queue.log` has no dates.) Synthetic now answers roughly **1 cycle in
6** where it used to answer 3 in 5. With `DEGRADED_CAP=2`, that is the whole of the queue's current
drain rate: 22 tasks held, and the holds are correct launcher behaviour, not a fault.

**RESOLVED 2026-09-16 03:32 PT (scheduled tick #241) - IT IS 100% RATE LIMIT AND 0% CREDIT.** The line
below used to say this measurement could not distinguish the two because it counted probe outcomes, not
error bodies. Tick #240 named the cheap decisive step - count 429-vs-402 in the router log, one grep, no
probe, no money - and #241 ran it. Six hours of `cognizioware-mcp-tools-litellm` on CT202:

    synthetic-matching log lines ............ 900
    of those, RateLimitError / rate_limit_error  900   (1:1, every single one)
    BudgetExceeded / insufficient credit / payment required .. 0
    anchored status histogram (HTTP/1.1" NNN): 200 x 64425, 401 x 2273, 404 x 1297,
                                              429 x 142, 400 x 86, 202 x 86, 405 x 5,
                                              500 x 2, 304 x 2  -- NO 402 ROW AT ALL

**Reader caveat, recorded because it nearly produced the opposite headline:** a bare `grep -c "402"`
returned **333**, which reads exactly like mass credit exhaustion. It is an artifact - "402" is a
substring of ephemeral port numbers like `54026`. The anchored `HTTP/1.1" NNN` pattern is the honest
reader and it shows zero 402s. Verify the reader before trusting the reading.

**So the spend question is not merely "not reopened", it is DISPROVEN on evidence.** The 09-11
correction (429:402 roughly 50:1) was directionally right; at 6 h today the ratio is 900:0.
**What changed is the magnitude, not the diagnosis** - and the diagnosis is now measured, not inferred.

**RECOMMENDED ANSWER: probably NOTHING TO DO - this now reads as a rate limit that clears, not a
spend decision.** The row is kept because 4 x 402 is not zero and Paxton may want to look at
https://synthetic.new/billing himself, but the overseer is NOT asking for money on this evidence.
**DEFAULT IF PAXTON SAYS NOTHING:** the overseer does NOT spend and does NOT touch billing. The fleet
runs degraded all-ornith at 2 concurrent (both GPUs), Synthetic-dependent tasks fail fast, and the
rate limit is expected to clear on its own.

*** THE PREDICTION CAME TRUE - MEASURED 2026-09-16 04:41 PT, scheduled tick #253. ***
The default above ended with "the rate limit is expected to clear on its own". **It did, with no money
spent and nothing touched.** Read from the launcher's own 04:41 cycle:

    04:41 active=2 kimi=0 qwen=2 keys_ok=3 queued=23 synthetic=ok doctor_quota=ok
    04:41   RECOVERY RAMP: Synthetic just returned - launching at most ONE run this cycle
    04:41   emit-probe multi-file-attach-accumulate [trio 0]
              syn:large:vision:synthetic-anthropic=1.3s  ornith-1.5:pool=2.0s  kimi-k3:synthetic-anthropic=1.8s
    04:41 LAUNCHED multi-file-attach-accumulate 20260916T114234Z_e6945c31

That ends a **13-cycle / ~2 h 49 m DOWN streak** (since ~02:23) that had 21 tasks held behind one
Ollama key. Two things are worth keeping. **(1) The emit probe answered on BOTH Synthetic aliases**
(1.3 s and 1.8 s, inside the 1-3 s gate) - so this is a real recovery of the rate limit, not a
launcher flag flapping. **(2) It is the THIRD independent confirmation of the no-spend verdict**,
after the 09-11 log count (429:402 = 50:1) and #241's 6-hour histogram (900:0, no 402 row at all):
*a spent weekly credit does not come back by itself at 04:41 on a Sunday morning.* A credit
exhaustion would have stayed exhausted until Paxton paid.

**THE ASK IS UNCHANGED AND STILL NOT A SPEND.** Recommendation and default both stand exactly as
written above; this update only removes the last reason to doubt them. The row stays open solely
because 4 x 402 is not zero and Paxton may still want to look at the billing page himself.

*** FOURTH CONFIRMATION, AND A CORRECTION TO FOUR OF MY OWN TICKS - 2026-09-16 06:23 PT, scheduled tick #268. ***
**I read the 429's actual text for the first time, and it names the limit.** One nonce'd probe (so it
could not be a cache hit) to `POST /v1/messages`, `nemotron-3-super:synthetic-anthropic`, harness
virtual key -> HTTP 429 in 5.10 s, body verbatim:

    "You've exceeded your subscription rate limits. Upgrade, or try again later.
     You can view your usage at https://synthetic.new/billing"

That is the **rate-limit** message - the regenerating pack budget - not a credit/402. It matches
`launch_queue.py:345`'s own docstring for the 2026-09-10 event word for word. With `doctor_quota=ok`
on the 06:14 cycle as an independent second reading, **the no-spend verdict now has four independent
confirmations** (09-11 log ratio 50:1; #241's 6 h histogram 900:0; #253's 04:41 recovery; this).
**Nobody had read the message text before - every prior round argued from 429 COUNTS.** The reason it
was never read: `emit_ok()` truncates the error to **70 characters**, which cuts the sentence off
before the word that distinguishes the two limits. Verify the reader before trusting the reading.

**CORRECTION - TICKS #264, #265, #266 AND #267 HANDED THIS FORWARD AS AN UNDECIDED SPEND DECISION
"becoming real at ~06:44". IT WAS NEVER UNDECIDED, AND 06:44 WAS NEVER A DISCRIMINATOR.** A wall-clock
boundary cannot separate a regenerating request cap from a spent weekly credit; only the message text,
the 402 count and the doctor reading can, and all three were already on file here - two of them since
09-11. **Blast radius: four consecutive ticks carried a "NEEDS PAXTON (pending)" item that this
document had already answered and disproved three times, for roughly 45 minutes of tick agenda.** No
money was spent, no run was routed differently, and nothing was asked of Paxton - the error was that
it stayed on the escalation list at all, against this file's own Rule 1 ("SEARCH FIRST... an ask that
is already answered on disk is not an ask").

**STATE: this row needs NOTHING from Paxton and no tick should hand it forward again.** The single
remaining reason it stays open is unchanged and is not a question for the overseer: 4 x 402 is not
zero, and Paxton may want to look at the billing page himself. **A future `synthetic=DOWN` is
expected, self-healing behaviour** - the correct response is to read the 429 body (full, untruncated)
and `doctor_quota`, and only if a **402/credit** signal appears does this become a spend decision.

---

## ~~terranas01's SSH HOST KEY~~ - RESOLVED 2026-09-11 06:41 on Paxton's instruction. One question left.

**Measured 2026-09-11 05:55 PT** attempting the 125a destination check
(`ssh -p 9222 prax211@192.168.21.128`):

    @@@  WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!  @@@
    Offending ECDSA key in /c/Users/PaxtonTait/.ssh/known_hosts:21
    new ED25519 fingerprint: SHA256:M4ugvLmZUogHxX4VvjvwRwmCrtbDQwRaldoS3AF+YZk
    Password authentication is disabled to avoid man-in-the-middle attacks.

**THE OVERSEER HAS NOT TOUCHED IT AND WILL NOT.** Accepting a changed host key on your NAS is exactly
the class of action that must not be taken unilaterally - if the change is legitimate it is trivial
for you to confirm, and if it is not, silently trusting it is the worst possible move. No key was
removed from `known_hosts`, and no connection was completed.

**WHAT IT BLOCKS:** task 125a's whole safety design is export -> transfer -> **verify at the
destination** -> only then delete. With the destination untrusted and unreachable, the cycle cannot
be completed by anyone, run or overseer. 125b's audit-entry work is unaffected (PR #144 is open).

*** RESOLVED - Paxton said "You have ssh... go do it". ACTIONS TAKEN, all reversible: ***
 1. `known_hosts` backed up to `known_hosts.bak-20260911-064057` (5,835 b, 42 lines) BEFORE any edit.
 2. Both stale entries removed with `ssh-keygen -R "[192.168.21.128]:9222"` (lines 20 and 21).
 3. The currently-offered keys installed - and they were scanned from TWO vantage points, this
    workstation AND ptait01, which agreed on `SHA256:M4ugvLmZUog...`. That rules out a
    man-in-the-middle on one network path (not one on a shared segment).
 4. **IDENTITY CONFIRMED FROM INSIDE, which is the check that actually matters:** the host answers
    as `terranas01`, owns `/Volume2` (19 T, 61% used), and holds the real spendlog exports. A
    substituted host would not have the data.

**THE ONE QUESTION LEFT, and it is narrower than the original:** the overseer told Paxton this looked
"consistent with a rebuild". **It is not.** `uptime` reads **up 5 days, 7 hours; boot Sep 5 22:52** -
the machine has NOT rebooted. So the host keys were rotated IN PLACE on a host that has been up five
days, which means something restarted or reinstalled sshd rather than the box being reimaged.

**RECOMMENDED ANSWER:** just confirm you (or a NAS package/firmware update) touched SSH on terranas01
in the last day. If you did, this is closed entirely.
**DEFAULT IF PAXTON SAYS NOTHING:** the new keys stay trusted (identity is proven by the data), and
the overseer still ships NOTHING to that host without a separate go. The backup allows a one-command
revert.

---

## ~~WHICH terranas01 spendlogs path is canonical? - blocks task 125, and 5.5 GB is at stake~~ CLOSED AS MOOT 2026-09-14 10:10 (see below); nothing moves

**Paxton 2026-09-11 06:42:** *"Ur path off in terranas01
/Volume2/usbdrive/cognizioware-mcp-gateway/spendlogs path"*.

**Measured one minute later, and it does not match:**

    /Volume2/cognizioware-mcp-gateway/spendlogs           EXISTS - 5.5 GB of exports
    /Volume2/usbdrive/cognizioware-mcp-gateway/spendlogs  DOES NOT EXIST
    /Volume2/usbdrive                                     exists but holds personal files
                                                          (HEICs, ImageArchive, Job PTAIT09, #recycle)

**CORRECTION: they are NOT the same filesystem.** `stat -c%d` reports **dev 52 for usbdrive vs
dev 53 for the legacy path** - two distinct shares on one pool, which is why `df` shows identical
19T/11T/7.2T/61% figures for both. That makes any consolidation a **real copy, not a rename**:
non-atomic, and needing 5.5 GB of headroom while both copies exist.

**Evidence the existing path is the one in use:** it is the path written in task 125a's text, AND it
contains four write-verify probe files from Sep 10 21:48-21:49 - something was proving transfers to
exactly that directory.

**NOTHING HAS BEEN MOVED.** Those 5.5 GB are the only copy of rows already deleted from the
production database. The overseer will not relocate them on an ambiguous instruction, and will not
create the usbdrive directory speculatively.

*** PARTIALLY RESOLVED 2026-09-11 06:49: Paxton said "Create this dir if blocking". ***
`/Volume2/usbdrive/cognizioware-mcp-gateway/spendlogs` now EXISTS and is proven writable - created
as prax211 (`drwxrwx---`), then write -> read back -> checksum -> remove, 7.2 TB free. Task 125a and
125c have been repointed at it. **Nothing was moved.**

**THE REMAINING QUESTION IS ONLY ABOUT THE 5.5 GB OF HISTORY:** does the earlier archive at
`/Volume2/cognizioware-mcp-gateway/spendlogs` stay where it is, or get consolidated into the new
location? **RECOMMENDED:** leave it. It is the only copy of rows already deleted from production,
a cross-filesystem copy is non-atomic, and nothing depends on the two being together. If you do want
it consolidated, the overseer copies it, verifies checksums at the destination BEFORE removing the
original, and never deletes first.
**DEFAULT IF PAXTON SAYS NOTHING:** nothing moves. Task 125 stays script-only. The existing path
remains the documented destination because it is the one that demonstrably holds the data.

**A SEPARATE FINDING WORTH YOUR EYE:** there is **no checksum or manifest file** anywhere beside
those 5.5 GB. 125a's safety design is "verify at the destination before deleting", but nothing stored
records that these files were ever verified after transfer. The rows they represent are already gone
from the database.

---

## WITHDRAWN - the ruflo ask was mine, measured on the WRONG container. Nothing for Paxton.

**RETRACTED 2026-09-11 09:20 PT.** The earlier version of this section asked Paxton to `pct start 101`
on ptait01 or retire the eval step. **Both options were wrong, and starting that container would have
fixed nothing.** Keeping the retraction here rather than deleting it, so the error is visible.

**What I got wrong:** I matched on the CT NUMBER. CTIDs collide across hosts in this fleet.
  - ptait01 CT101: ip 192.168.21.218, onboot=0, STOPPED - a stale leftover. **.218 is dead**
    (100% packet loss from CT202).
  - **ptait07 (192.168.21.138) CT101 `ruflo`: onboot=1, RUNNING**, `ruflo-mcp.service (HTTP :3000)`
    and `ruflo-daemon.service` both active. **Current IP 192.168.21.155** (net0 is `ip=dhcp`, so the
    address CHANGED when it moved hosts). `:3000` answers **HTTP 401** - alive, auth-gated.

**The real cause, verified by reading the gateway rather than guessing:**
`e2e/evals/ruflo-live-verify.mjs` never contacts ruflo directly. It POSTs to the MCP gateway with
header `x-mcp-servers: ruflo`. The gateway answers:

    "litellm.ai/server_outcomes":{"ruflo":{"status":"unreachable"}}, "tools":[]

**The reader was verified before this was trusted:** the same call with NO filter returns 617,844
bytes with langfuse(87), playwright(23), cloudflare(13), stripe(10), memory(4), auth(2) all `ok`.
So the gateway works and ruflo works - **the gateway cannot REACH ruflo**, almost certainly because
its registered URL still points at the dead .218.

**Why this is not fixed yet, and it is a real constraint not a stall:** `LiteLLM_MCPServerTable` has
ZERO rows, so this registration is NOT DB-shadowed - it lives in `infrastructure/litellm-config.yaml`,
and **any edit to that file force-recreates the CT202 prod router.** Two harness runs are active.
It waits for the same zero-active-run window as PR #144. **Nothing is needed from Paxton.**

## The qa checkpoint suite needs a hydra checkout provisioned on CT210 - NOT a decision, just notice

**Measured 2026-09-11 09:37 PT. Nothing is needed from Paxton; recorded so it is not a surprise.**

`cognizioware-qa` master has failed three times today (06:11, 06:27, 12:10 UTC) in the `hydra-e2e`
job. **It is a machine-migration leftover, not a code fault.** The `qa-host` label now resolves to
exactly one runner - `ct210-qa`, **Linux** - while the workflow still documents *"qa-host is a
Windows box (ptait09)"* and the repo variables still point at that old machine:

    HYDRA_REPO_DIR   = C:\hydra-ci      (exists on ptait09, NOT on CT210)
    FACTORY_REPO_DIR = C:actory-ci    (same shape, same problem)

**Neither obvious fix works on its own**, which is why it keeps failing:
 - repointing the label back at ptait09 fails - `C:\hydra-ci\orchestrator
ode_modules` is missing;
 - repointing the variables at a Linux path fails - CT210 has no hydra checkout anywhere.

**It needs a provisioning step** (clone cognizioware-hydra on CT210 + install orchestrator deps,
then update both variables). Queued as work, not raised as a blocker. **Note it does NOT block the
QA Gate that serves the other lanes** - that ran GREEN twice today (07:26, 07:31) in between the
failures.

**One genuinely open item inside this, and it is small:** `QA_LITELLM_MODELS = qwen3.5:27b,qwen3.5:27b`
names the same model twice. **That is the concrete thing blocking the retirement of the old model
tags (PR #120)** - the tests lose their second target when the duplicate is removed.
**Paxton picks the replacement model; there is no safe default to guess.**

## ~~The launch cap is higher than the paid provider can carry~~ - **ANSWERED BY PAXTON 2026-09-14, AND IMPLEMENTED. Closed by tick #240, 2026-09-16 03:22 PT.**

**This sat open for two days after it had been decided.** The recommended answer below was "cap
all-Synthetic launches at 3 (not 6)". Paxton set exactly that, and it is in the running code:

    launch_queue.py:101   # 2026-09-14 PAXTON: SYNTHETIC CAP = 3. At most three LIVE runs may hold
    launch_queue.py:102   #                    any :synthetic-anthropic role.
    launch_queue.py:104   SYN_CAP=3

Confirmed live, not just on disk: the launcher (pid 126116, started 02:08:18) prints
`synthetic runs live=0 SYN_CAP=3` every cycle. **Nothing is waiting on Paxton here.** The predicted
failure shape this row described - "ramp to 6, blow the limit, lose four runs" - can no longer occur;
tick #240 measured zero `active` excursions above 5 across 536 status lines.

Kept below for the reasoning, which is still sound.

### Original row (superseded)

**Measured 2026-09-11 11:31 PT.** Synthetic recovered at 10:23; the launcher ramped 1 -> 2 -> 6 runs
(CAP=6) over thirty minutes exactly as designed; **at ~11:05 the provider returned 429 "exceeded your
subscription rate limits" and all four Synthetic runs died at round 0.** The two local-lane runs were
untouched. The launcher then correctly re-marked synthetic=DOWN and fell back to the degraded cap of 2.

**The ramp is not the bug; the ceiling is.** CAP=6 assumes one Synthetic pack can carry six trios of
three roles each. It cannot (memory: ~2500 req/5h, and a week of credit was burned in 18 h at 2.8x the
sustainable rate on 09-09). Every recovery cycle now repeats the same shape: ramp to 6, blow the limit,
lose four runs, strand four tasks, fall back to 2.

**RECOMMENDED ANSWER:** cap all-Synthetic launches at **3** (not 6), OR pace launches by measured
requests/hour against the ~500/hr sustainable rate instead of by run count. Either is a one-line
constant change in `launch_queue.py` plus a launcher restart at a cycle boundary.
**DEFAULT IF PAXTON SAYS NOTHING:** nothing breaks - degraded mode already holds at 2 while the provider
is down - but every recovery wastes itself and costs four runs. The overseer will NOT change CAP on its
own: it is a spend-shaped decision (how hard to lean on a paid subscription), which is one of the three
things that genuinely need Paxton.

## ANSWERED 2026-09-11 11:35 PT - Seq secrets committed in mcp-cognizioware seq/

Paxton: **DONE.** Closed on his authority. Measured at the same time on the default branch: the four
values (README.md L75/L107, env.nas.template L12, nas.env L12) are still present; seq/ last changed
2026-03-10. Read as rotated-or-accepted. No overseer action; history scrub is not requested.


---

## 2026-09-11 16:00 PT - THREE ITEMS FROM THE CT202 OUTAGE (each carries a recommended answer and a default)

### 1. AZURE MCP NAMESPACE IS DOWN ON PURPOSE (about 70 gateway tools)
`cognizioware-mcp-tools-azure-mcp-backend` on CT202 was in a crash loop holding 475 processes and
15.7 GiB of CT202's 20 GiB, which OOM-killed the prod router. It is now stopped with
`--restart=no`. It installs its own dependencies from the network on EVERY start
(`apt-get update && npm install -g @azure/mcp supergateway`), so each restart is a rebuild.
**RECOMMENDED ANSWER: leave it down until task 122 lands, which is amended with all of today's
measurements and now includes building a proper image.** Turning it back on as-is re-creates the
outage.
**DEFAULT IF YOU SAY NOTHING: it stays down and task 122 fixes it.** No further ask.

### ~~2. A llama-server IS RUNNING ON ptait01 AND IT IS OOM-KILLING THINGS - NEEDS YOU~~ DEFAULT ACCEPTED by Paxton 2026-09-14 10:45: leave it; re-raise only if it kills the router again
Measured during the outage: `llama-server` at 85% CPU and 8.5 GiB on ptait01, and the host's
own kernel OOM-killed it at 14:17 (global_oom, not a cgroup limit). The host is 42 GiB with
**ZERO swap** and was at 40 GiB used. Memory already records ptait01 as oversubscribed, and
host-level changes there need your explicit go, so I have NOT touched it.
**RECOMMENDED ANSWER: move that inference off ptait01, since CT202 (the prod gateway) shares the
host and is the thing that dies.** ptait07 is the box with RAM/CT headroom.
**DEFAULT IF YOU SAY NOTHING: I leave it alone and report it again if it kills the router a second
time.** This is the one item here that genuinely needs a human.

### 3. ONE-CONTAINER MEMORY CAPS SELECT A VICTIM - a defect in what task 157 shipped
PR #143 capped the litellm container's memory and capped nothing else. Today the capped process
(litellm, 642 MiB) is the one the kernel killed, while the uncapped one (15.7 GiB) survived.
**RECOMMENDED ANSWER: cap every container on CT202 from its measured steady state, and assert in a
test that none lacks a limit. Folded into task 122's amendment, coordinated with task 158.**
**DEFAULT IF YOU SAY NOTHING: task 122 carries it.** No decision needed from you.

**TICK #482 UPDATE, 2026-09-17 14:27 PT - THE DEFAULT WAS ACCEPTED AND HAS NOT BEEN DELIVERED IN SIX
DAYS, AND THIS EXACT DEFECT HAS SINCE TAKEN PROD DOWN TWICE MORE.** Same signature both times: the
kernel OOM-kills the CAPPED router while UNCAPPED siblings survive.
- 2026-09-16 17:16:06 - `oom_memcg=/lxc/202 task=litellm pid=894368`; azure-mcp-backend 14.59 GiB vs
  litellm 745 MiB/6 GiB; gateway 502 for ~13 min. (Recorded in task 184's note at 17:33; 184 queued.)
- 2026-09-17 13:50:33 - `oom_memcg=/lxc/202 task=litellm pid=2335692` anon-rss 1,436,252 kB; :4000
  refused, gateway 502, recovered only by `pct reboot 202` at 13:54. (Tick #480 read this as a fork
  failure; the kernel line says memcg OOM. Corrected in tick #482.)
**MEASURED THIS TICK: 47 containers in CT202's 20 GiB cgroup, exactly SEVEN capped** (litellm 6G,
audioqa 3G, puppeteer 2G, playwright x4 2G). The remaining 40 are uncapped, `azure-mcp` among them.
So the 09-11 finding is not merely unfixed - it is unchanged.
**STILL NOT A DECISION FOR YOU.** The owning task **184** (`9999v-184-ct202-container-memory-caps`)
has sat in the active band since 2026-09-16 17:33 with `run_id: None` - it has never launched, held
behind SYN_CAP and the band order. Precedent says the fix needs no window: PR #143 applied
`docker update --memory` LIVE with no recreate and no downtime.
**THE ONE THING THAT IS YOURS: band order.** *Recommended:* move 184 to the head of the active band
so it launches on the next free Synthetic slot - it is the only open item with two prod outages
attributed to it. *Default if you say nothing:* the overseer does NOT reorder (your standing rule),
184 waits its turn behind 156/109/167/192b and the next uncapped climb takes the router down a
third time. No present pressure as of 14:29 (CT202 4,116/20,480 MB; azure-mcp 11.72 MiB).


---

## 2026-09-11 17:05 PT - FOUR EARLIER ASKS RESOLVED (source: session [fe2679]; each re-verified by the overseer immediately before recording, not accepted on report)

### ANSWERED - push-handoffs. NOTHING WAS EVER WAITING ON YOU.
Both handoffs are already on origin. The asks were built on two shas, one of which is an orphan.
- **8210a96** IS ON ORIGIN: it is the TIP of `ci/env-promotion` in cognizioware-powerplatform
  (gh compare `8210a96...ci/env-promotion` = identical, behind 0), carrying
  `docs/handoff/PLAN-powerplatform-session-ux-fixes.md`. Recorded on task 154.
- **3b5042a** is a SUPERSEDED LOCAL COPY and is orphaned. The same handoff was re-committed and
  pushed as **5e8f15d**, the tip of `easybutt0n-runner-v2` in cogniziocompany/ptait09-easybutt0n-ai
  (read back: 5e8f15da5e36226210152afb5cca54fdd892fbe8), carrying
  `docs/HANDOFF-ptait09-runner-claude-lane.md`.
  **Task 152 still cited 3b5042a and would have STOPPED looking for a sha that does not exist on any
  branch. Corrected in blocked/1009-152 with the evidence in-row.**

### ANSWERED - the 135 SSH key. It works.
`ssh -i ~/.ssh/id_ed25519_proxmox root@192.168.21.151 hostname` returns
**`cognizio03-corsairai300`**. Re-run by the overseer just now, not quoted from the earlier report.

### ~~STILL OPEN - the Seq ingest key.~~ STRUCK 2026-09-14: answered below (no key needed). This is the ONLY half of 135 left, and it is genuinely yours.
It is a manual step in the Seq UI: an INGEST-ONLY key, Application=cognizioware-powerplatform,
Environment=prod. Nothing else blocks on it.
**RECOMMENDED ANSWER: create the ingest-only key when convenient; the tasks that consume it (137,
139, 161) stay blocked until then and nothing else is affected.**
**DEFAULT IF YOU SAY NOTHING: those Seq tasks stay blocked and I stop re-raising this.**

### ~~OPEN DECISION - mcp-cogn-gh capability.~~ SUPERSEDED 2026-09-14: runs hold a scoped GH_TOKEN (push + PR) since 16:21; merging stays the overseer's
**RECOMMENDED ANSWER: accept the earlier recommendation and make it overseer-executed, exactly like
144, 147, 149 and 150.** That is consistent with how every other lane-touching item is handled and
needs no new capability.
**DEFAULT IF YOU SAY NOTHING: I treat it as overseer-executed and proceed.**

### ~~UNCHANGED - drain windows.~~ SUPERSEDED 2026-09-16: one bundled window, ordered steps in task 144's text (136 apply, 170 retention, CT105 stop, reclaim, impact:deploy merges)
147 is MERGED (this session). 144 is still blocked on the 69b token, the hydra git-clone-scope
secret - and note task 69b itself is now at PR (cognizioware-qa #29), so that dependency is moving.

**NOT CHANGED BY ANY OF THIS:** the two live LiteLLM bugs, task 151 (arity) and task 82
(`cannot pickle`), remain unfixed.


---

## 2026-09-14 09:40 PT - TWO ITEMS CLOSED BY PAXTON, ONE NEW ITEM FOR HIM

### ANSWERED - the Seq ingest key. Paxton: "This is a dev environment. You can ingest without any auth."
PROVEN before recording: POST /api/events/raw?clef with NO Authorization header returns **HTTP 201
{"MinimumLevelAccepted":null}** on BOTH https://seq.easybutt0n.ai and http://192.168.21.128:5341.
(The /api/events query endpoint returns 401 "Please log in" - that is the UI session, not ingest.)
Tasks 137, 139 and 161 are unblocked: SEQ_API_KEY is now OPTIONAL in all three task texts, and any
"missing SEQ_API_KEY" guard is declared a bug to remove - task 161's run had stopped on exactly that
guard. 139 and 161 (both dead on a Synthetic 429 while filed in done/) are requeued with the delta.

### FOUND WHILE CHECKING THE OLLAMA KEYS (Paxton's ask) - THE LAUNCHER WAS BLIND FOR THREE DAYS
C:/tmp/keys_status.txt, the ONLY thing the launcher reads for key capacity, was FROZEN at
"keys ok: 0" since 09-11 10:03 because the quota watcher has a 72-hour lifetime and expired. So
for three days every launch was steered onto the single-slot local executor, which is why runs
timed out at 3600 s and hit three strikes. A fresh probe from CT202 against ollama.com:
  key1 OK · key2 OK · key3 OK
  key4 LIMIT (aidev03_cogniz)       - MONTHLY usage limit
  key5 LIMIT (aidev03cogniziocompany) - MONTHLY usage limit
  key6 LIMIT (aidev02cognizio)      - MONTHLY usage limit
  key7 LIMIT (aidev01cogniziocompany) - MONTHLY usage limit
The watcher is restarted. **NEEDS PAXTON, low urgency:** four Ollama accounts are at their MONTHLY
cap and will not recover until the month rolls over unless credits are added.
**RECOMMENDED ANSWER: add usage credits to the two aidev0x accounts you use most, or accept running
on three keys (about two kimi runs at a time) until the reset.**
**DEFAULT IF YOU SAY NOTHING: run on three keys; nothing else changes.**
**Durable fix, queued for the overseer, not you:** the launcher must treat a keys_status.txt older
than one cycle as UNKNOWN and probe itself, never as "0 keys" - a stale reading was worse than none.


---

## 2026-09-14 10:10 PT - HOUSEKEEPING FROM AUDIT [fe2679] SECTIONS C AND K (overseer)

### MOVED TO ANSWERED - 144-window. It was APPROVED, so by this file's own Rule 2 it was never an ask.
Execution of that window is task 147's job (147 itself MERGED 2026-09-11). 144 remains blocked on
the 69b token; 69b is now at PR (cognizioware-qa #29).

### CLOSED AS MOOT - "WHICH terranas01 spendlogs path is canonical / the 5.5 GB".
Paxton 2026-09-11: the export family is deprecated, no migration. NOTHING MOVES. The existing
5.5 GB stays where it is as a historical archive - it is the only copy of rows already deleted -
and the usbdrive directory is NOT created. (Retention itself is NOT yet live - task 170 owns that -
but that changes nothing about this path question.)

### CLOSED AS AN ASK - terranas01 sshd. It only ever blocked 125a, which is finished.
Kept as a one-line SECURITY NOTE, not an ask: confirm whether a NAS update rotated sshd's host key.
No decision needed from Paxton.

### STALE HEADING RETIRED - "SYNTHETIC CREDIT EXHAUSTED - blocking the queue".
Synthetic has read ok on the launcher since 2026-09-11 10:23 (and reads ok today). The heading
stays in the file as history; treat it as closed. The Synthetic CAP ask (recommend 3) is unchanged.

### STILL GENUINELY ON PAXTON (the complete list, nothing else):
1. mcp-cogn-gh capability decision - default: overseer-executed like 144/147/149/150.
2. Four Ollama accounts at their MONTHLY cap (key4-7) - default: run on three keys until reset.
3. The llama-server sharing ptait01 with the prod gateway - default: leave it, report if it kills
   the router again.
4. Synthetic recovery CAP (recommend 3) - default: 3.


---

## 2026-09-14 10:45 PT - PAXTON ANSWERED. Three defaults accepted; one listing corrected.

- **Ollama monthly caps (key4-7): DEFAULT ACCEPTED.** Run on three keys until the monthly reset. No credits added.
- **llama-server on ptait01: DEFAULT ACCEPTED.** Leave it. Re-raise only if it kills the prod router again.
- **Synthetic recovery CAP: DEFAULT ACCEPTED - CAP = 3.** The launcher's DEGRADED/Synthetic ramp must not exceed 3 concurrent Synthetic runs.
- **mcp-cogn-gh: CORRECTION - THIS WAS NOT OPEN.** It was DECIDED 2026-09-10 21:30 PT (row above: "make it overseer-executed, like 144/147/149/150; CT110 has no gh and no GitHub token"). Task 102's note carries the same decision. I re-listed it on 09-14 from the audit's copy without checking this table. Withdrawn as an ask. Paxton asked for more information; given in chat and recorded in the ledger.

**ON PAXTON NOW: nothing.** Every earlier ask is answered or defaulted.


### 2026-09-14 11:10 PT - IN PROGRESS, needs the token: CT110 scoped GitHub write path
Paxton decided it (reversing 09-10). gh is installed; installer + proof ready. The expert creates a
fine-grained token per C:/tmp/HANDOFF-ct110-scoped-github-token-2026-09-14.md, drops it in
C:/tmp/secrets/gh-harness-token.txt, runs the installer, reads the proof. Nothing else blocks on it.


### 2026-09-14 12:15 PT - CT110 scoped GitHub token: INSTALLED, PUSH PROVEN IN REAL USE, ONE PERMISSION STILL WRONG
- Installed by the expert session; the overseer used it at 12:12 to push feat/seq-logging and open
  LongHorizon-Harness #18 from CT110 (remote head read back). Push and PR create work.
- **STILL OPEN, Paxton's action:** the token can READ SECRET NAMES (HTTP 200, secrets=read, on
  LongHorizon-Harness, mcp-tools and powerplatform). Values are never returned by GitHub.
  **RECOMMENDED ANSWER: edit the token that expires 2027-09-15 (not 2027-09-13, which is the old
  personal token) and set Secrets to No access.** Editing keeps the value; nothing reinstalls.
  **DEFAULT IF YOU SAY NOTHING: the CT110 harness restart that gives runs GH_TOKEN stays held, so runs
  keep stopping at push/PR and the overseer finishes those steps by hand, as today.**


### 2026-09-14 16:17 PT - ANSWERED: CT110 token Secrets permission. Paxton fixed it; verified.
Secrets list returns 403 from CT110 on all three repos checked, on the installed 2027-09-15 token.
Nothing is on Paxton for the token now. Remaining steps are the overseer's: restart lh-harness in a
zero-active window, then update the doctrine so runs push and open PRs (merging stays the overseer's).
**BOTH DONE (verified 16:43 PT):** lh-harness restarted 16:21 PT (23:21:22 UTC) and the service env
carries `GH_TOKEN`; LOOP-PROMPT STEP 1 says runs push + open PRs, merging stays the overseer's
(its restart time corrected from 16:30 to the measured 16:21). Secrets re-checked 403 on all three
repos; PR-create permission check returns 422 (permitted). Closed.


---

## 2026-09-14 21:08 PT - OPEN-ASKS AUDIT FROM SESSION [fe2679] APPLIED (its handoff: C:/tmp/HANDOFF-open-asks-audit-2026-09-14.md)
- Item 1: the "CT110 has no gh" premise is SUPERSEDED (row amended above). blocked/ swept for entries whose ONLY blocker was that - result in the ledger.
- Item 2: no separate "runs stall on git ops" task was ever queued, so nothing to drop.
- Items 3, 4, 8: applied (144-window row removed from the table, which is now EMPTY; the contradicted Seq heading struck; ptait06 -> ptait09).
- Item 5: agreed and already recorded - retention is NOT live; 125's supersession holds only once task 170 reads the setting back from the router DB. **RESOLVED 2026-09-17 16:16 PT by TICK #493: the read-back was done against the live CT202 router (ACCEPTANCE PASS 6/6, retention '7d', prompts off, stored_in_db=True on all six keys). The condition is met and 125's supersession now holds.**
- Item 6: RESOLVED - the ruflo section's ".218 / waits for a yaml edit" is stale: mcp-tools.env line 49 = .155 landed on 09-11 and survived the router recreate; task 171 owns the remaining repo-file drift.
- Item 7: VERIFIED - SYN_CAP=3 is in C:/tmp/launch_queue.py (lines 104-105, 724-725, 759-760, 776), the running launcher (pid 40724) started 10:08:48 after the last edit at 10:08:27, and the 16:28 cycle held six tasks at "3 live Synthetic runs >= cap 3" with no probes sent.
- Item 9 (split into OPEN-ASKS + ASKS-HISTORY): DEFERRED, deliberately. Three sessions cite this file by LINE NUMBER today; a 500-line move would break every one of those references. Do it at the next quiet point with a pointer left behind.
- Item 10: kept as a security note, not an ask.
**ON PAXTON NOW: nothing. The open table is empty.**


### ~~2026-09-14 21:12 PT - NEEDS PAXTON: the 5-minute loop cannot be trusted to the in-session scheduler~~ ANSWERED + IMPLEMENTED 2026-09-14 21:26: Windows Scheduled Task LH-Overseer-Sweep, 330+ ticks; hidden launcher since 09-16 18:40
Twice today the registered 5-minute job fired nothing for hours (13:00-16:17 and 16:30-21:07) while
CronList showed it present. Every sweep today ran only because you messaged. Three runs sat at gates for
four and a half hours this evening as a result.
**RECOMMENDED ANSWER: a Windows Scheduled Task on ptait09 that runs, every 5 minutes,
`claude -p "Run one overseer sweep tick now: read C:\tmp\queue\LOOP-PROMPT.md in full and execute it."`
from C:\Users\PaxtonTait\source\LongHorizon-Harness, with a single-instance guard so ticks never overlap and a log file whose mtime you can check.
It survives session restarts, which the in-session job never did.** The overseer can create the task itself
(a workstation change, not a PVE host); it needs your go because each tick is a billed Claude invocation.
**DEFAULT IF YOU SAY NOTHING: the in-session job stays registered, and sweeps happen when you message.**


### ~~2026-09-14 21:21 PT - NEEDS PAXTON (low urgency): the gateway alias glm-5.2:synthetic-anthropic points at a model the provider has retired~~ ANSWERED 2026-09-15 08:28 PT (Paxton: "proceed" on C:/tmp/HANDOFF-glm52-alias-repoint-2026-09-15.md)
**DONE, READ BACK:** POST /model/update on row fd0770d4-b747-461c-aee0-1b0d09182ef5 returned 200; GET /model/info afterwards shows that row as custom_openai/hf:moonshotai/Kimi-K3 (before: custom_openai/hf:zai-org/GLM-5.2); POST /v1/messages model=glm-5.2:synthetic-anthropic max_tokens=1 with a fresh nonce returned HTTP 200 (before: 404 "no longer supported"). Run inside CT202 via pct exec; the master key never left the box. Still open, not an ask: the yaml twin `glm-5.2:synthetic` (row 87b1d6c0..., openai/hf:zai-org/GLM-5.2) stays 404 until the litellm-config.yaml edit at the next zero-run window (spend check 2026-09-15 08:28: 2 calls in 7 days, last 09-09 = no consumer; decision: delete that row inside the next yaml PR that needs a window anyway, no separate PR).

Every call returns 404 "hf:zai-org/GLM-5.2 is no longer supported" - measured on the prod router today, failing since at least 04:14. The launcher no longer uses it (four trio rows repointed to kimi-k3 / syn:large:vision, three distinct backends per row). Any other consumer of that alias is broken.
**RECOMMENDED ANSWER: repoint the alias to the provider's suggested replacement (hf:moonshotai/Kimi-K3 - i.e. make glm-5.2:synthetic-anthropic an alias of the same upstream as kimi-k3:synthetic-anthropic), via POST /model/update and a read-back, and note it in litellm-config.yaml at the next zero-run window.** This is a router model row (Postgres-shadowed), not a yaml-only edit.
**DEFAULT IF YOU SAY NOTHING: the alias stays as it is and keeps 404ing; the launcher is unaffected.**


### ANSWERED 2026-09-16 14:55 PT - PAXTON'S DECISIONS ON "WHAT IS BLOCKED 13:31 PT" (relayed by session [fe2679]; ACK TOKEN PAX-DECISIONS-20260916-1331) - **CONSUMED by SCHEDULED TICK #322, 2026-09-16 15:57 PT**: handoff read in full, token echoed into the ledger row. Bridge verified.
Full text: C:/tmp/HANDOFF-paxton-decisions-2026-09-16.md. The tick that consumes it writes the ACK TOKEN into its ledger row.
1. CT202 disk: store_prompts_in_spend_logs=false + 7-day retention NOW (task 170, decided 09-09); the 25 GB reclaim APPROVED for the next zero-active window (same as 144) after re-measuring margin; the 2-hourly export retires once retention is proven live **(PROVEN LIVE 2026-09-17 16:16 PT, TICK #493 - read-back ACCEPTANCE PASS 6/6; the export may now retire, the 25 GB reclaim still waits on a zero-active window)**. Restate the self-contradicting row.
2. Synthetic: NO new spend; CAP=3 stands (09-14). Refresh the stale row text.
3. pp setup defects: top of queue, develop -> dev -> one promote per the chronology plan; Ruflo gate green first.
4. Reviewers: count is FOUR repos, not two (re-measure). Do NOT install @claude as primary; harness is the reviewer (PLAN-pr-review-gate, tasks 185-188); @claude/Copilot fallback only. Re-scope 130/132/149. Hydra reviewer merge held for a window: correct.
5. Review comments: nothing from Paxton.
6. DNS test: fix the naming - remove BOTH A and AAAA in one edit, full-list backup first, read back every other name, in a window.
7. Four abandoned PRs: CLOSE, reason attached.
8. Local floor: ACCEPTED - manager + auditor only; executor fails fast.
9. QA_LITELLM_MODELS = ornith-1.5:pool,kimi-k3:synthetic-anthropic; land PR #120 behind it.
10. Eval environment: lowest, nothing waits.
NEW WORK queued today: task 182 (overseer web UX at fleet.easybutt0n.ai/overseer, DB-backed handoffs/asks/tasks/ledger). Do not duplicate.
**ON PAXTON NOW: nothing.**


### ANSWERED 2026-09-16 15:38 PT - "BLOCKED WITH NOBODY ASSIGNED" (three items; Paxton via session [fe2679]; Step 0 measured from PTAIT09)
1. Red preview build: it is pp #105 `build` (only red build check on any open PR); REASSIGNED to task 159, removed from 138.
2. Stale twin + duplicate routing: CT105 on corsairai300 confirmed as the twin (identical container names, Up 8 days, own postgres+litellm). DECISION: `pct stop 105` + `onboot=0`, OVERSEER-EXECUTED in the next zero-active window (with 144), after a postgres-volume backup and a read of every connector's ingress (7 connectors listed in 138's note; none inside CT105). Rollback = `pct start 105`.
3. Green build without tests (pp #107, 09-11): DECISION (a) add the test job to powerplatform ci.yml NOW (task 183), then (c) the harness-review check when the PR-review-gate lands (185-181, queued today). **(a) DONE 2026-09-16 16:56 PT (tick #328): pp PR #111 merged debb37d to develop; CI 35163766193 ran backend+frontend suites and the image job depends on them. Task 183 closed VERIFIED. (c) still open on 185-188.**
**ON PAXTON NOW: nothing.**

### 2026-09-16 22:24 PT - DONE by [fe2679] on Paxton's decision 7: mcp-cognizioware #31/#32/#33/#34 CLOSED with task-102 reasons. Decision 9 (QA_LITELLM_MODELS value) recorded, NOT applied - set it when landing PR #120 after confirming the QA key reaches both models. Ship Plane 'Waiting on you' table is stale vs this file: see C:/tmp/HANDOFF-ship-plane-waiting-on-you-2026-09-16.md. **ON PAXTON NOW: nothing.**

### 2026-09-16 22:49 PT - TABLE MAINTENANCE by [fe2679] on Paxton's review: 138-checks-read APPROVED (evidence rewritten to the review-gate reason; token edit is Paxton's own, closes on read-back); rows ADDED 187-github-app, 134-ct103-db, 56-eval-env (all genuine CREDENTIAL asks that were missing from the rendered table); six stale headings struck (6); header timestamp fixed. **ON PAXTON NOW: three credential items in the table (138 token scopes, 187 GitHub App, 134 CT103 db) + 56 at low priority; nothing else.**
