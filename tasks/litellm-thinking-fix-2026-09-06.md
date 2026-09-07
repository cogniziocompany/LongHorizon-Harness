# TASK — cognizioware-mcp-tools: LiteLLM thinking-block / tool-turn normalizer so qwen3.8 (local) and glm-5.3 (manager) work behind the Anthropic adapter

**Asked by Paxton 2026-09-06 15:55–16:05 PT:** "YES DO THESE WITH YOUR PLANNING/SCHEDULING" (the LiteLLM translation fix) and the rule **"we should be
using qwen3.8 and up only, locally, unless exception"** — so `qwen3-coder:30b` (pulled today) is a diagnostic exception, not a lane; the local lane
must be `qwen3.8` for every role, which requires this fix.
**Overseer role:** manager — run on CT110, review, PR → main, apply on CT204 (uat) with `--env-file mcp-tools.env`, run the round-trip matrix, then CT202.
**Repo / branch:** cogniziocompany/cognizioware-mcp-tools, `fix/litellm-thinking-blocks` from origin/main @ 2fd43bc (includes #76 qwen3.8 → `/v1`).
**Workspace:** `/home/harness/work/cognizioware-mcp-tools` on CT110 (budgets 600/900/1800 in the workspace `.lh-harness/config.toml`).
**Run:** `20260906T224541Z_bf5e2f69` — roles manager `qwen3.8`, executor `kimi-k2.7-code:pool`, auditor `qwen3.8` (local single-shot roles work; the executor
is the role the fix must unlock). **Task text:** `C:\tmp\litellm-thinking-fix-task.txt`.

## Failures it must fix (all verified live today; details in docs/handoffs/local-executor-and-cloud-capacity-handoff-2026-09-06.md)
- F1 `Content block is not a thinking block` (Anthropic adapter, echoed thinking block with null signature) — killed glm-5.3 manager (09-05) and qwen3.8 executor via `/v1` (09-06).
- F2 `500 no user query found in messages` (ollama_chat, tool_result-only user turns).
- F3 `400 "<model>" does not support thinking` (`thinking` param forwarded to non-thinking models) — worked around with `MAX_THINKING_TOKENS=0`.

## Deliverables
1. `infrastructure/litellm-tests/thinking-roundtrip.mjs` + fixtures (Claude-Code-shaped 4-step matrix, PASS/FAIL per model).
2. `design/litellm/thinking-blocks-rca.md` (file:line at LiteLLM v1.93.0; newer-release fix status or UNVERIFIED).
3. `infrastructure/docker/litellm/hooks/thinking_normalizer.py` pre-call hook for non-Anthropic deployments (strip thinking blocks; drop `thinking` param for `supports_reasoning: false`; reshape tool_result-only turns for ollama_chat), flags default on, unit tests.
4. litellm-config.yaml wiring + `supports_reasoning: true` on qwen3.8/glm-5.3; rollout README (uat first); e2e suite 34.

## Overseer follow-through
- Apply to CT204 → run the matrix for `qwen3.8`, `glm-5.3:cloud`, `kimi-k2.7-code:pool` → CT202 (batch with a quiet window; the restart drops the MCP gateway ~2 min).
- Then: local all-qwen3.8 validation run (3 tool-using rounds + clean audit) per the handoff's acceptance §6.1; restore glm-5.3 as an allowed manager.
- Concurrency rule stands: two `:pool` runs max until a non-Ollama fallback exists; Paxton is adding Ollama capacity (Max or a 4th Pro) — when the key arrives, add `OLLAMA_CLOUD_KEY_4` to the pool groups and raise the cap to three.

## Config port + deploy state (2026-09-06 17:05 PT)
- mcp-tools #78 (after the reverted #77) commits the `:pool` groups over OLLAMA_CLOUD_KEY_1..4 to infrastructure/litellm-config.yaml; CT204 env now has KEY_4.
- The main-push pipeline "Deploy MCP Tools Stack" run 34068104282: E2E deploy gate PASSED (qwen3.8 serving), but the job was cancelled at the
  artifact-upload step at 23:57Z (cause not identified; the earlier CRLF run 34068040283 was the one I cancelled) → CT204 apply / QA gate / CT202
  deploy skipped. Nothing is missing live: CT202 already has the pool groups as DB deployments (4 per group verified). DEFERRED: `gh run rerun
  34068104282` in a quiet window (bf5e2f69 + RP-15 ride CT202; the deploy restarts LiteLLM ~2 min). Batch it with the thinking-fix rollout.

## Run bf5e2f69 closed (2026-09-06 18:30 PT)
All four slices committed on fix/litellm-thinking-blocks: 3d3760c (reproduction harness), f1d2121 (RCA), eaa1b27 (normalizer hook + unit tests),
2e817d2 (config, rollout README, e2e suite 34) — 8 files, +1450. Rounds 1–5 audited clean; rounds 6–8 were qwen3.8 auditor (900 s) / manager
(600 s) TIMEOUTS from local-span contention (three qwen3.8 planners/auditors queued on NUM_PARALLEL=2 at that time), so slice 4 has no harness
audit → overseer audits it directly (below). Run ended via approval 391ddf328622 (stop).

## Overseer audit of slice 4 + review fixes (18:45 PT)
- Unit tests: 25 passed (clean venv with pytest-asyncio). YAML of config/compose/workflow parses.
- Finding 1 (blocking): the hook module was NOT inside the container — compose bind-mounts only litellm-config.yaml at /app; the callback path
  `infrastructure/docker/litellm/hooks/...` would have failed to import and crashed LiteLLM at start. Fix: mount `/opt/cognizioware-mcp-tools/hooks`
  at `/app/hooks`, callback `hooks.thinking_normalizer.ThinkingNormalizer`, deploy workflow copies the file to CT204 and CT202 next to the config.
  CT204's hand-maintained compose patched by hand with the same mount (container recreate needed — `docker restart` does not add mounts).
- Finding 2: rule 1 stripped thinking blocks only for models WITHOUT supports_reasoning; F1 reproduced today on qwen3.8 (supports_reasoning: true).
  Now strips for every non-Anthropic target.
- Finding 3 (process): first attempt at the review commit rewrote docker-compose.yml (CRLF in repo) → caught by `git diff --stat` before push
  (3219-line churn), redone with newline-preserving patching. Final: 10 files, +1462 vs main. PR opened; merge after the PR diff check.
- 18:50 PT: PR #79 merged (main b407ddc). Pipeline 'Deploy MCP Tools Stack' → E2E gate → CT204 apply (config + hooks/thinking_normalizer.py, container already recreated with the /app/hooks mount) → QA gate uat → CT202. Watcher checks CT204 logs for the hook load right after the apply; matrix run follows.
- 19:45 PT: the "cancelled" E2E gate on run 34068104282 was an OOM-KILL of the gh-runner-lan service in CT203 (ptait01; 12 GB CT, jest capped at
  2 GB heap) at 23:57Z — the runner unit died and stayed `failed`, so every later deploy run (incl. #79's 34072999745) queued. Restarted the unit
  (online 01:43Z); the #79 pipeline is now running. If the E2E gate OOMs again: raise CT203 memory / lower JEST_MAX_WORKERS — and add the runner
  unit to the ops doctor (runner offline = deploy lane dead, silently).
- 20:25 PT: pipeline run 34072999745: E2E gate PASSED (58 min), then the runner listener in CT203 was OOM-killed AGAIN 12 s later (unit had no
  OOM protection; the CT itself OOMs) and the CT204 apply job died with exit 137. Applied CT204 by hand: main's litellm-config.yaml + hooks/
  thinking_normalizer.py pushed, router restarted, healthy; `hooks.thinking_normalizer.ThinkingNormalizer` imports inside the container. Runner
  restarted with a systemd drop-in `OOMScoreAdjust=-900` and the failed jobs re-run so QA gate + CT202 deploy go through the lane (all pool runs
  are down on quota → quiet window). Matrix against uat running from CT110 (log /tmp/matrix-uat.log on CT110).
- 21:45 PT: MATRIX FINDING — the callback was registered as the CLASS; LiteLLM imports the dotted path as-is, so every post-call hook raised
  `CustomLogger.async_post_call_success_hook() missing 1 required positional argument: 'self'` → HTTP 500 on otherwise-successful completions
  (uat only; CT202 never got the hook). Hot-patched CT204 (module-level `thinking_normalizer_instance`, config path updated, router restarted);
  repo fix = mcp-tools #80 (merged). The uat QA gate failures themselves were Ollama quota 500s (pre-dating the hook) — CT202 deploy waits for
  the quota windows. Separate issue: CT110 (192.168.21.168) cannot reach CT204 (.162) on ANY port although pve110 and CT210 can — investigating;
  the matrix must run from a host that reaches uat (CT210 or pve110) until fixed.
- 21:55 PT: CT110→CT204 reachability is fine now (TCP connect OK; the 000s coincided with the two router restarts and an ARP DELAY state) — not a
  firewall. Instance fix verified on CT204: /v1/messages qwen3.8 → 200 in 1.2 s, zero "missing self" errors in a fresh log window. mcp-tools #80
  merged (main e44f91a) → pipeline running; qwen3.8-only round-trip matrix running from CT210 against uat.
- 22:05 PT: **MATRIX PASS on uat for qwen3.8** — all four steps (tool_use → echo-with-thinking-block + tool_result → same with thinking enabled →
  tool_result-only turn) return the expected stop_reason through the Anthropic route. F1/F2/F3 are fixed on CT204 for the local lane. glm-5.3:cloud
  and kimi pool could not be matrixed (Ollama quota 500s). Remaining: CT202 deploy through the lane once the uat QA gate can pass (quota), then
  the all-qwen3.8 harness validation run (3 tool rounds + clean audit) per the capacity handoff §6.1, then restore glm-5.3 as manager.
- 22:20 PT: pipeline for #80 (e44f91a): E2E gate + CT204 apply green (runner survived with the OOM drop-in); uat QA gate failed on quota again → CT202 skipped. A watcher re-runs the failed jobs automatically once the quota watcher sees ≥2 healthy keys, then verifies CT202 (hooks dir, instance callback, mount, health).
- 22:58 PT: quota back (4/4 keys at 20:31 watcher-clock); `gh run rerun --failed` on 34076620660 → attempt 3: uat QA gate PASSED, **Deploy to CT202 in progress** (the lane restarts prod LiteLLM ~2 min; three pool runs may lose one episode each). Watcher verifies CT202 (hooks dir, instance callback, mount, health, qwen3.8 /v1/messages) after completion; then the all-qwen3.8 harness validation run.
- 23:10 PT: **NORMALIZER LIVE ON PROD (CT202)** via the lane (run 34076620660 attempt 3): hooks/thinking_normalizer.py present, config callback
  `hooks.thinking_normalizer.thinking_normalizer_instance`, /app/hooks mount active, router healthy in 10 s, qwen3.8 /v1/messages → 200, zero
  hook errors. Next: §6.1 acceptance — all-qwen3.8 harness run (manager/executor/auditor) on a small 3-slice task in the idle hydra-rsi workspace.
- 23:12 PT: pipeline 34076620660 fully green incl. Post-deploy Smoke Test and QA Verify (prod). All-qwen3.8 acceptance run launched as `20260907T034412Z_9a864a8f` (hydra-rsi workspace, 3-slice env-name extractor task, 5 rounds; budgets 900/900/1800). Pass criterion: 3 tool-using rounds complete with clean audits and no F1/F2/F3 errors.

## ACCEPTANCE PASSED (2026-09-07 00:25 PT) — §6.1 of the capacity handoff
Run `20260907T034412Z_9a864a8f`, manager/executor/auditor ALL `qwen3.8` through the prod router (CT202) with the normalizer: rounds 1–3 each
completed manager → executor (tool-using, git commits) → auditor "done" with no errors; round 4 manager declared completion. Zero occurrences of
F1 (`Content block is not a thinking block`), F2 (`no user query found`), F3 (`does not support thinking`). Deliverables: 3 commits on a local
validation branch in the hydra-rsi workspace (591e5ab, 09817fd, 0e7800a — not pushed; disposable). Conclusion: **qwen3.8 is back on full lane
duty (all roles)**; the Ollama pool stays the default for throughput, qwen3.8 for $0 / off-quota runs (one run at a time on the span; three
qwen3.8 planners in parallel queue on NUM_PARALLEL=2 → 300–900 s episodes). glm-5.3:cloud manager: matrix check against prod next, then re-allow.
- 00:40 PT Sep 7: prod matrix from CT210 — **glm-5.3:cloud PASS 4/4** (thinking model through the Anthropic route with the normalizer) → glm-5.3
  is re-allowed as a harness MANAGER. kimi-k2.7-code:pool 3/4: step b failed only because the ai-dev02 deployment returned the session-limit 500 and
  LiteLLM did not route around it ("No fallback model group", 2 retries on the same deployment) — a routing gap, not a translation bug.
  Follow-up (config, not urgent): give the `:pool` groups an explicit fallback chain and a shorter cooldown so an exhausted account is skipped.

## Follow-up 2026-09-07 02:30 PT: stream-shape failure after the normalizer
- Run f7de0f71 (all-qwen3.8, executor) died at round 7 with `API Error: Content block is not a thinking block`; neither router logged it. The string is raised by Claude Code's own stream accumulator (cli.js `content_block_type_mismatch_thinking_delta/_signature`) when a thinking/signature delta lands on an index whose block is not `thinking`. Four runs total hit it (4a64dc70 executor, ab12530b + adf11963 auditor, f7de0f71 executor). The normalizer fixed the request side (echoed thinking); this is the response side of LiteLLM's Anthropic streaming adapter (`adapters/streaming_iterator.py`, index bookkeeping around block switches).
- Fix chosen: no reasoning on the harness lane. `reasoning_effort=none` is honoured by Ollama `/v1` (4 vs 40+ completion tokens) but only reaches Ollama through `extra_body` (drop_params strips the top-level param). New route `qwen3.8-nothink` -> mcp-tools PR #88 (feat/qwen-nothink-route). The span `qwen3.8-nothink:latest` Modelfile is broken (`TEMPLATE {{ .Prompt }}`, SYSTEM /no_think, still reasons) - do not use it.
- After PR #88 is live: off-quota trio = manager qwen3.8 (non-streaming, thinking OK), executor + auditor qwen3.8-nothink; update `C:\tmp\launch_queue.py` trio map.
- 2026-09-07 14:05 PT: PR #88 merged; lane 34162318474 past E2E, applying to UAT. Launcher trio switched now (no qwen entries queued, so safe): QWEN = manager qwen3.8, executor + auditor qwen3.8-nothink; launcher restarted. Verify after the lane: `curl CT202 /v1/chat/completions model=qwen3.8-nothink` returns no reasoning_content; then the next off-quota run uses it.
- 14:50 PT: lane 34162318474 deployed (E2E, UAT, QA, prod deploy, prod QA verify green; smoke failed on the health suite's bare /health hook timeout) but the router kept the old model list: the lane syncs litellm-config.yaml without restarting the router on config-only changes (router started 21:25Z, config synced 21:32Z). Overseer restarted the router once to complete the rollout; the lane defect + the health-suite defect are queued as 09b-lane-config-reload-health-suite.
