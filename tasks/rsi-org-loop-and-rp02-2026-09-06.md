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
