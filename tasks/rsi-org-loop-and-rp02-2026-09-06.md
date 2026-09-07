# TASKS — Revenue-pipeline P0 runs on CT110: RP-02 Hydra Ship Plan (model + UI) and the RSI org-loop specification

**Trigger (Paxton 2026-09-06 10:00–10:10 PT):** "add to this [hydra 30-backlog.md] and use another lh-harness task for it: apply using all the
surfacing data from all the different parts of our infra, including its org-level sibling powerplatform — the reporting top-level data exported or
extracted via the ops or admin control centers. We should have a LiteLLM or similar agent; chat.easybutt0n.ai needs to be directly wired,
accessible and CONTROLLABLE, ideally in a second phase; but could view all / read-only reporting and make recommendations in a YouTrack issue on
a repo/project; if approved via the YouTrack UI it could trigger an lh-harness run (build/fix) for powerplatform or any repo. This will be our RSI
org loop. Deep-research how to wire Open WebUI properly, persistently." And: "everything else in the Revenue Pipeline backlog and decision register
is proceed / auto-approve until I can start using it" → R01–R11 accepted by default (hydra #10).
**Harness node:** CT110. **Overseer role:** manager; deploy/apply steps are the overseer's.

## Run RP-02 — Hydra Ship Plan data model, REST API, generated UI page
- Task text: `design/ship-plan-integration/40-harness-tasks/RP-02-…task.txt` (written by run c9ab282f), paths rewritten for CT110
  (`/home/harness/work/cognizioware-hydra`, branch `feat/ship-plan-reporting` from origin/main @ 0d7c528; ship-plan.html seeded in design/inputs/).
- Run: `20260906T165427Z_d595f424` (14 rounds).

## Run RSI — org-loop architecture, Open WebUI persistent wiring, YouTrack approval trigger, backlog + task texts
- Workspace: dedicated clone `/home/harness/work/cognizioware-hydra-rsi`, branch `docs/rsi-org-loop` from origin/main @ 0d7c528.
- Task text: `C:\tmp\rsi-org-loop-task.txt` (CT110 copy `/home/harness/work/rsi-task.txt`). Deliverables under design/rsi-org-loop/: 00 architecture
  (surfacing catalog across ops/admin centres, Hydra, LiteLLM, Langfuse, memory, powerplatform, QA, billing, GitHub, fleet; Phase 1 read-only +
  YouTrack recommendations + approval-gated harness trigger; Phase 2 controllable from chat), 10 Open WebUI wiring (persistent config rules,
  functions as versioned files + idempotent admin-API apply script, LiteLLM virtual key + `x-mcp-servers` allow-list, controllability matrix),
  20 YouTrack approval trigger contract (human approver ≠ service account; one issue → one active run; write-back), 30 backlog rows RP-13+ and
  decision rows ACCEPTED-BY-DEFAULT, 40 harness task texts.
- Research fed in (overseer, WebSearch 2026-09-06): Open WebUI persistent config semantics and HA (env-configuration reference, SRE HA guide, FAQ);
  Pipelines legacy vs in-process Functions/Tools; native MCP = Streamable HTTP only since v0.6.31, OpenAPI/mcpo preferred; YouTrack + AI agents
  = repo-aware automation with structured write-back.
- Run: `20260906T165610Z_9a4f5ef4` (14 rounds).

## Model trio (both)
manager `kimi-k2.7-code:pool` · executor `kimi-k2.7-code:pool` · auditor `kimi-k3:pool`.

## Queue after these (dependency order, all pre-approved)
RP-01 YouTrack Phase 2 Hydra events bridge (after YouTrack Phase 1 is on the dev lane and CT110 is free) → RP-03 fleet-mcp Ship Plan tools →
RP-04 customer admin API → RP-06 D365 Sales service → RP-05/07/08 → RSI org-loop P0 build tasks (from run 9a4f5ef4's 40-harness-tasks/).

## After completion (overseer)
- RP-02: review, PR → main, deploy the orchestrator on corsairai300 (tarball; restore live .env; batch with a LiteLLM window; chat drops briefly), verify the Ship Plan page in Hydra.
- RSI: review, PR → main; hand Paxton the decision rows; launch its P0 tasks.

## RSI run completed (2026-09-06 11:00 PT)
Run 9a4f5ef4 done in ~10 rounds: 13 files +1471 — design/rsi-org-loop/00 architecture (61-row surfacing catalog), 10 Open WebUI wiring, 20 YouTrack
approval trigger (human approver ≠ service account; RSI_LOOP_ENABLED/RSI_TRIGGER_ENABLED default off), 30 backlog RP-13..RP-16 + decision rows,
40 task texts; openwebui/functions/{rsi_org_loop,rsi_loop_tag_filter,hivemind_session_capture}.py; scripts/openwebui-apply.mjs. Merged as hydra #11.
Next (pre-approved): RP-13 Open WebUI functions + apply script (launching on CT110 in the rsi clone), RP-15 Hydra trigger endpoint + write-back,
RP-16 surfacing-catalog adapters (after RP-02 frees the hydra checkout), RP-14 YouTrack approval receiver (after billing slice 1 frees mcp-cognizioware).

## RP-02 completed (2026-09-06 11:15 PT)
Run d595f424: ship-plan-store.js (JSON store under /data), ship-plan-routes.js (/api/ship-plan, Hydra auth, activity log, mcp-cognizioware event bridge
stub), /ship-plan page route, tests 12/12. Merged as hydra #12. Deploy pending (orchestrator tarball on corsairai300, batched). RP-13 (Open WebUI
functions + apply script) launched as run 00c95986 in the rsi clone.
- 11:55 PT: RP-13 running as 00c95986 (rsi clone, branch feat/rsi-openwebui-wiring); RP-15 running as f94faabb (hydra checkout, branch
  feat/rsi-hydra-trigger). A first RP-15 launch (51ccc09c) went out with an empty task because the checkout was detached — stopped immediately.
- multi-webhook-crud@prod (the only red case on the promotion's prod gate) is now its own run in a second pp workspace on CT110
  (`cognizioware-powerplatform-evalfix`, branch fix/eval-multi-webhook-crud; task `C:\tmp\webhook-crud-fix-task.txt`; hypothesis: the case asks for a
  Lead record in eval orgs that have no Lead entity).
- 15:35 PT: RP-13 (00c95986) done after the quota resume: refined openwebui/functions/* + scripts/openwebui-apply.mjs, tests 7/7 → hydra #13 merged.
  Not applied live yet (overseer step: run openwebui-apply.mjs against chat.easybutt0n.ai with the admin token from the orchestrator env).
- Ollama quota incident 10:50–15:16 PT (all three Pro accounts hit the 5-hour session limit with 5 parallel runs); auto-resume script resumed billing
  slice 1 + RP-13 at 15:16; guest slice 1 relaunched on kimi (84bec9d9) after the local qwen3.8 executor attempts failed on LiteLLM translation bugs
  (see docs/handoffs/local-executor-and-cloud-capacity-handoff-2026-09-06.md). RP-15 (f94faabb) and the webhook-crud fix (cccbef69) remain queued
  behind the two-run cap.
- 2026-09-06 16:57 PT: RP-15 (f94faabb) had died at round 1 on the ai-dev01 session limit ("No fallback model group"). With keys 1 and 4
  (litellm-cognizioware) healthy and only one other :pool run active, resumed via `POST /api/runs/{id}/resume {"mode":"continue"}` →
  status starting, resume_epoch 1. The ≥3-keys quota watcher was stopped (manual resume supersedes it); state-change watcher now covers RP-15 + bf5e2f69.
- 17:35 PT: RP-15 (f94faabb) completed at round 7 after the resume: one explicit-path commit 2c53b36 on feat/rsi-hydra-trigger (worktree
  cognizioware-hydra-rsi) — POST /fleet/rsi/runs (RSI_TRIGGER_ENABLED default off, RSI_ALLOW_LIST, 11-field validation, rsi-loop actor logging),
  orchestrator/src/rsi-poller.js write-back poller with injectable sink + pendingEvents fallback, 22/22 node tests, design §4.4/§6 updated.
  Overseer review: no secrets, kill switch verified in code. Run closed via approval f31a05fcdadd (stop). PR opened → main.
- 17:34 PT: RP-15 merged as hydra #14 (main ebe0153). Orchestrator deploy on corsairai300 still batched (RP-02 #12 + RP-15 #14 + RP-13 apply);
  RSI_TRIGGER_ENABLED stays unset (off) until Paxton starts using the loop.
- 17:35 PT: RP-14 (YouTrack approval-trigger receiver, mcp-cognizioware `feat/rsi-approval-trigger` from develop b9b06b7 = billing slice 1 + #72)
  launched as `20260907T003459Z_81e6ed65`, 8 rounds, qwen3.8 / kimi-k2.7-code:pool / qwen3.8. All four Ollama keys answered 200 at 17:33, so three
  pool runs are in flight (bf5e2f69, 2785e057, 81e6ed65). RP-16 (surfacing adapters) waits for the mcp-tools workspace (bf5e2f69) to finish.
- 17:52 PT: RP-14 81e6ed65 stopped at round 3 — same cause as the harness run: no workspace `.lh-harness/config.toml`, qwen3.8 manager timed out
  twice at 300 s, round 3 emitted an invalid route. Budgets file added to the mcp-cognizioware workspace (untracked). Relaunched with a kimi manager
  (three qwen3.8 planners on one span with NUM_PARALLEL=2 queue behind each other); auditor stays qwen3.8. New id on the next line.
- 17:53 PT: RP-14 relaunched as `20260907T005013Z_adf11963` (kimi manager+executor, qwen3.8 auditor, 8 rounds, budgets 900/900/1800).
- 17:58 PT: RP-14 adf11963 FAILED in round 1 on the qwen3.8 AUDITOR: `API Error: Content block is not a thinking block` (F1, LiteLLM thinking-block
  echo) after a 732 s tool-using audit episode. Executor (kimi) had completed the round. Relaunched with the standard all-kimi trio
  (kimi-k2.7-code:pool / kimi-k2.7-code:pool / kimi-k3:pool); qwen3.8 stays out of tool-using roles until bf5e2f69's normalizer is deployed.
- 18:05 PT: RP-14 relaunched as `20260907T010509Z_060e4809` (all-kimi trio, 8 rounds).
- 18:15 PT: RP-14 060e4809 FAILED at round 1 with `/bin/sh: 1: --unsetenvvar=SSH_AUTH_SOCK: not found` — collateral of the CT110 editable-install
  hazard (see tasks/mcp-profiles-auditor-lock-2026-09-06.md 18:15). Released package restored; relaunched (id on the next line).
- 18:22 PT: RP-14 relaunched as `20260907T011226Z_c471fab5` (all-kimi, 8 rounds), on the restored released package.
- 19:05 PT: RP-14 (c471fab5, all-kimi) COMPLETE at round 5: d915bf3 (slices 1–2: RsiApprovalController/handler, validation incl. service-account
  approver guard `ai-dev01@cognizio.company`, harness_trigger_request builder) + a64bee0 (slices 3–4: Postgres `rsi_approvals` idempotency store,
  409 active-run guard, continuation branches `feat/rsi-<type>-<issue>-continue-<n>`, `pending_youtrack_events` fallback, DI, tests). 22 files
  +2697, all inside src/McpCognizioware.BillingService{,.tests}; kill switches `Rsi:LoopEnabled`/`Rsi:TriggerEnabled` (legacy env
  RSI_LOOP_ENABLED/RSI_TRIGGER_ENABLED) default false; simulation trace: 202 / 400 / 409 / 202-continuation. No secrets. The run also edited the
  design doc in the hydra-rsi worktree (outside its workspace) — committed separately as hydra #15 (doc-only). Run closed via approval
  51c2958ed596. Next: dotnet test → PR → develop (dev billing lane deploy; the receiver is inert with the switches off).
- 19:45 PT: RP-14 verification — hermetic suite 285/289: four PostgresPendingEventQueueTests failed because enqueue set NextAttemptAtUtc = now+5 min
  (fresh write-backs invisible to GetPendingAsync). Overseer fix (2 lines): due immediately, RetryDelay only after a failed attempt → 289/289.
  PR → develop opened (deploys to the dev billing lane via ct210-billing; receiver inert with switches off).
- 19:50 PT: RP-16 (surfacing-catalog adapters ops-status + qa-run, mcp-tools `feat/rsi-surfacing-adapters` from main b407ddc) launched as `20260907T014548Z_d2cdac30` (all-kimi, 8 rounds). RP-14 merged as mcp-cognizioware #73 → dev billing lane deploy in progress.
- 20:00 PT: RP-14 live on the dev billing lane (develop CI 34074020118 green from ct210-billing; app sha-1a82ca5 healthy, /api/health 200, guest route 401). Receiver inert (switches off).
- 20:20 PT: RP-16 ac894162 died in round 1 on the ai-dev01 session limit (no commits yet). Queued for automatic resume with the webhook fix; the MCP-profiles relaunch (new task text) follows by hand once the quota watcher reports ≥2 healthy keys. Nothing else runs on the pool tonight until the windows roll.
- 22:45 PT: RP-16 (ac894162, resumed after quota) COMPLETE at round 4: f51a8d8 — services/ops-status-mcp (7 read-only ops_status_* tools),
  services/qa-run-mcp (qa_run_summary, qa_service_run_status), litellm-config.yaml `rsi-loop` access group (separate from fleet-runners/billing/
  repo-tools), self-check scripts; 9 files +3421; no secrets; no mutating HTTP verbs. GAP: no Dockerfiles/compose services for the two adapters, so
  the config would reference unresolvable hosts → opened as a DRAFT PR; follow-up RP-16b (compose + env + e2e) before merge. Design-doc §4.2 edit
  committed separately (hydra docs PR). Run closed via approval 5e3110b43b8a.
- 22:50 PT: hydra #16 (RP-16 doc deviations) merged. RP-16b launched as `20260907T033030Z_63fa3d04` (same branch feat/rsi-surfacing-adapters; Dockerfiles + compose services + e2e 35; all-kimi, 6 rounds). Pool now: webhook fix 084159ae, MCP-profiles 716506b3, RP-16b — three runs on four healthy keys.
- 23:30 PT: RP-16b (63fa3d04) COMPLETE at round 6: d0482a5 Dockerfiles (node:22-alpine, non-root, HEALTHCHECK /health), dedf223 compose services
  ops-status-mcp :3130 / qa-run-mcp :3131 (env by NAME from mcp-tools.env, init: true), 313593c e2e suite 35 + docs + .env.example names,
  4d568b7 env-name alignment. Branch total 16 files +3711, compose/config diffs are pure additions (CRLF preserved). No secrets. Run closed via
  approval 8f6bf01b451b. Overseer: compose validated on CT204 with the real env → PR #81 marked ready → merge → pipeline builds the two images
  and deploys (E2E gate, CT204, QA gate, CT202). Operator env to set on CT202/CT204 (names only): OPS_CONTROL_CENTER_URL, OPS_INGEST_KEY, QA_BACKEND_URL, QA_READ_TOKEN, QA_REPORTS_DIR
  (MCP_API_KEY already exists). Overseer trimmed `env_file` from both services (6bebefa) so they receive only these; validated on CT204.
- 23:45 PT: PR #81 (RP-16 + RP-16b + env trim) MERGED (main 541d7aa) → pipeline builds ops-status-mcp / qa-run-mcp images and deploys CT204 → CT202. Adapter env not yet set on either router host (CT204 has none; CT202 has OPS_INGEST_KEY only) — adapters will start but answer errors until OPS_CONTROL_CENTER_URL / QA_BACKEND_URL / QA_READ_TOKEN / QA_REPORTS_DIR are added; overseer wiring next.
- 23:55 PT: adapter env wired by NAME on CT202 + CT204 mcp-tools.env: QA_BACKEND_URL (CT210 QA backend :8400) and QA_READ_TOKEN (CT210's QA API key, copied host-to-host, never in docs). OPS_CONTROL_CENTER_URL uses the compose default (ops-control-center:8080; OPS_INGEST_KEY already on CT202). QA_REPORTS_DIR left at default (no report mount on the router hosts; the adapter falls back to the API). The pipeline's CT202 full deploy creates the two adapter containers with this env; CT204's apply step only touches LiteLLM, so uat has no adapters (by design of the lane).
- 00:50 PT Sep 7: adapters pipeline (541d7aa): E2E + CT204 + uat QA gate green, **CT202 deploy failed at 'Build local images'** — the lane derives build contexts from compose and syncs only infrastructure/<ctx>; RP-16 placed the services at repo-root services/, so CT202 had an empty context (no Dockerfile). Prod router untouched (failed before compose up; health 200). Fix: move both adapters to infrastructure/docker/<name> and point the compose contexts there (branch fix/rsi-adapters-build-context).
- 02:20 PT Sep 7: **RP-16 LIVE on prod** — build-context fix merged; pipeline fully green (E2E, CT204, uat QA gate, CT202 deploy, smoke, prod QA
  verify). CT202: ops-status-mcp and qa-run-mcp containers healthy, /health OK (ops sections doctor/catalog/e2e/ci/cloud/langfuse/actions;
  qa reports_dir /app/qa-reports). The CT202 deploy restarted LiteLLM (~6 min ago) — pool runs may have lost one episode. Next: gateway tools/list
  check for the rsi-loop group and a virtual key with access group rsi-loop for the Open WebUI reporting agent (RP-13 apply, later).
- 02:35 PT Sep 7: gateway verification — JSON-RPC `tools/list` on CT202 `/mcp/` with `x-mcp-servers: ops_status_mcp,qa_run_mcp` returns exactly the
  nine rsi-loop tools (`ops_status_mcp-ops_status_{doctor,catalog,e2e,ci,cloud,langfuse,actions}`, `qa_run_mcp-qa_run_summary`,
  `qa_run_mcp-qa_service_run_status`). Gotcha for callers: the `/mcp/tools` and `/mcp/tools/list` GET/POST shortcuts answer "Client must accept
  text/event-stream" — use a real MCP JSON-RPC POST with `Accept: application/json, text/event-stream` (the e2e suite's POST shape soft-skips).
  Optional polish: add dash-free aliases (`opsstatus`, `qarun`) in mcp_aliases to shorten tool names for the Open WebUI agent.
- 02:37 PT Sep 7: aggregate gateway tools/list = 954 (no collapse; the plan's ~695 baseline was older).
