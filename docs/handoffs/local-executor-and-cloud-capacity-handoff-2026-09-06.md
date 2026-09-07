# Handoff — LLM capacity for the lh-harness fleet: local executor gap + Ollama Cloud limits (2026-09-06)

**Prepared by:** the LongHorizon-Harness overseer session (PTAIT09), 2026-09-06 15:40 PT, from a live incident — nothing below is hypothetical.
**For:** another expert (human or an lh-harness run on CT110) to research and, where cheap, fix. Paxton's question that triggered this:
*"Should I get another Pro account just to make sure we have this gap filled, or should we search for other LLM providers? GLM-5.3 — can't we
download this?"* Paxton has already said the three Ollama Cloud accounts are **Pro**; no money is to be spent without his explicit go.

## 1. What we run and where (verified today)

| Piece | Fact |
|---|---|
| Harness nodes | CT110 `lh-harness` on corsairai300 (192.168.21.168:8799, 4 vCPU / 6 GB, Ubuntu 24.04; `/home/harness/work/*` repo clones; web token in `/home/harness/.lh-harness-secrets.env`). Registered in Hydra (hydra.cognizioware.com) as node `ct110`. The WSL workbench on PTAIT09 is no longer used for company runs (dev-box rule, 2026-09-06). |
| Standard trio | manager `kimi-k2.7-code:pool`, executor `kimi-k2.7-code:pool`, auditor `kimi-k3:pool` — LiteLLM model groups that shuffle across three Ollama Cloud accounts (env `OLLAMA_CLOUD_KEY_1..3` = prax211, ai-dev01, ai-dev02; `routing_strategy: simple-shuffle`; KEY_1 weighted 3x). Runs are Claude Code CLIs (`claude --print … --dangerously-skip-permissions`) pointed at LiteLLM via `ANTHROPIC_BASE_URL`, so **every request goes through LiteLLM's Anthropic-messages adapter** (`/v1/messages`). |
| LiteLLM | CT202 (ptait01, 192.168.21.161, litellm.easybutt0n.ai), image `ghcr.io/berriai/litellm@sha256:4c76cc…` = **v1.93.0**, Postgres-backed, config `/opt/cognizioware-mcp-tools/litellm-config.yaml` (repo copy `cognizioware-mcp-tools/infrastructure/litellm-config.yaml`, deployed by the lane; CT204 = uat copy with its own compose). `init: true` added today (zombie `timeout` children took ptait01 to load 334). |
| Local GPUs | ptait01 host: 2× RTX 3090 (24 GB each), 43.5 GB RAM (37 GB used: prisma query-engine 11 GB, speaches 3.5 GB, llama-server, postgres-mcp…). Container `cognizioware-ollama-span` (:11442, `CUDA_VISIBLE_DEVICES=0,1`, `OLLAMA_SCHED_SPREAD=1`, `OLLAMA_NUM_PARALLEL=2`, `OLLAMA_KEEP_ALIVE=30m`, `OLLAMA_CONTEXT_LENGTH=65536` added today). Models present: `qwen3.8:27b` (17 GB, Qwen3 thinking model), `qwen3.8-nothink` (created today: Modelfile `FROM qwen3.8:27b` + `SYSTEM "/no_think"` + `num_ctx 65536`), `qwen3.6:27b`, `qwen3.6:27b-49k`. Also `cognizioware-ollama-gpu1` (:11441, GPU1 only, ctx 16k) and `cognizioware-ollama-cloud` (:11438, embeddings). corsairai300 :11440 and ptait07 :11436 (Quadro P620, 2 GB) are LiteLLM api_bases for other groups. |
| LiteLLM `qwen3.8` group | Single deployment on the span server. Changed today from `ollama_chat/qwen3.8:27b` (+`num_ctx: 65536`) to `openai/qwen3.8:27b` at `http://192.168.21.110:11442/v1` (+ `merge_reasoning_content_in_choices: true`, no effect). |

## 2. What broke today, in order

1. **10:50–10:57 PT — cloud quota exhaustion.** Five parallel CT110 runs on the `:pool` trio (after ~6 earlier runs) drained all three Ollama Cloud
   accounts: `{"error":"you (prax211|ai-dev01|ai-dev02) have reached your session usage limit …"}`. The `:pool` groups have **no non-Ollama fallback**,
   so every run failed with `litellm.APIConnectionError … No fallback model group found for original model_group=kimi-k2.7-code:pool`. Ollama's
   session limit is a rolling **5-hour** window (weekly 7-day); all three keys answered again at **15:16 PT**. Probe used: a 2-token `POST
   https://ollama.com/api/chat` per key (`grep usage.limit`). Auto-resume script: `C:\tmp\quota_resume.py` (resumes failed runs two at a time).
2. **13:41–14:27 — local qwen3.8 executor, route `ollama_chat`.** Manager rounds worked; the executor looped on `api_retry` with **zero** LLM
   traffic and idle GPUs because Ollama answered `500 {"error":"no user query found in messages"}` to every tool-result turn that LiteLLM built
   from Claude Code's messages. Direct `POST :11442/api/chat` and `:11442/v1/chat/completions` with a trailing `tool` message both worked, so the
   defect is in LiteLLM's Anthropic→ollama_chat translation of `tool_result`-only user turns.
3. **14:27 — auditor budget.** Default `[run.timeouts] auditor = 300` (s) is too small for qwen3.8 on 45k-character audit prompts → every round
   "failed" → operator gate after 3. Fixed per workspace: `/home/harness/work/cognizioware-powerplatform/.lh-harness/config.toml` with
   `manager = 600, auditor = 900, cli_executor = 1800` (web-launched runs read the **workspace** `.lh-harness/config.toml`; `--resume` is only
   for supervised workers, so budgets cannot be changed on a running run).
4. **15:04 — route `openai/…/v1`.** Tool turns now succeed, but as soon as Qwen3 *thinking* appears in an assistant turn, Claude Code echoes the
   `thinking` block (signature `null`) back and LiteLLM's adapter fails: **`Content block is not a thinking block`** (the same error that killed
   the `glm-5.3` manager on 2026-09-05). `merge_reasoning_content_in_choices: true` did not stop the adapter from emitting a thinking block.
5. **15:10 — `qwen3.8-nothink`** could not be loaded within 8 minutes: `qwen3.8:27b` stayed resident on GPU1 (20 GB) and host RAM was 37/43 GB.

Net: today the **only working executor is the cloud kimi pool**; local qwen3.8 is usable for manager/auditor roles (single-shot) but not as an
executor. Guest slice 1 lost ~4 hours to this.

## 3. Options to research (ranked by expected value / cost)

| # | Option | Why it might work | Cost / risk | How to prove it |
|---|---|---|---|---|
| A | **Non-thinking local coder model** as the executor: `qwen3-coder:30b` (Qwen3-Coder 30B-A3B, MoE, no thinking mode, strong tool use, 256k ctx; ~18–19 GB at Q4) on the span server, replacing `qwen3.8:27b` for the executor lane | Removes the thinking-block failure at the source; MoE-3B active → fast on 2× 3090; fits with `OLLAMA_MAX_LOADED_MODELS=1` | ~19 GB download; must unload the 27b (keep-alive) or set `OLLAMA_MAX_LOADED_MODELS=1`; host RAM headroom | `ollama pull qwen3-coder:30b`; LiteLLM deployment `openai/qwen3-coder:30b` @ `:11442/v1`; run the Claude-Code-like tool-turn payload (see §5) twice (second turn with the first answer echoed back); then one real CT110 run with executor=`qwen3-coder` |
| B | **Strip/normalize thinking blocks in LiteLLM** for Ollama deployments (drop `thinking` blocks from incoming Anthropic messages when the target is not Anthropic; or upgrade LiteLLM ≥ the version that fixes signature-less thinking blocks) | Fixes glm-5.3 and every thinking model at once | LiteLLM upgrade = gateway risk (all MCP tools ride on it); a custom pre-call hook is code in mcp-tools | Read LiteLLM changelog for "Content block is not a thinking block"; test on CT204 (uat router) first with the §5 payloads |
| C | **Cloud fallbacks for the `:pool` groups** to a non-Ollama provider (OpenRouter / DeepSeek / Moonshot direct kimi API / Anthropic) | Keeps runs alive when Ollama windows close | Needs a provider account + key (money, Paxton's call); privacy posture for source code | Add `fallbacks: [{"kimi-k2.7-code:pool": ["<provider-model>"]}]` on CT204 first; simulate exhaustion with a key that has `max_budget: 0` |
| D | **Fourth Ollama Pro account / Max on one account** | Cheapest "more of the same" | $20/mo Pro, $100/mo Max; still a shared 5-hour window per account | Only if concurrency > 2 is really needed; today's cap is **two concurrent :pool runs** |
| E | **GLM-5.3 locally** | Paxton asked | GLM-5.3 full is far beyond 2× 24 GB; `glm-5.3-flash` is only available as Ollama Cloud (`:cloud`), no public weights sized for 48 GB VRAM were verified — mark UNVERIFIED, research the actual parameter count and any GGUF | Check ollama.com library + HF for `glm-5.3-flash` weights; if a ≤30B GGUF exists, treat like option A |
| F | **Free host RAM on ptait01** (prisma query-engine 11 GB belongs to LiteLLM's Prisma client; speaches 3.5 GB) or move Ollama span to a box with more RAM | Model loads/swaps stop stalling | Ops work; speaches/prisma ownership | `docker stats` on ptait01; decide what moves |

Recommendation: **A first (same day), then B on the uat router, C only with Paxton's go, D/E deprioritized.** Keep the concurrency cap at two
`:pool` runs until B or C exists.

## 4. Constraints the researcher must respect

- No spending or new accounts without Paxton; no LiteLLM changes on CT202 (prod) before the same change is proven on CT204 (uat) — restarting
  the CT202 router drops the MCP gateway ~60–120 s and every in-flight harness round.
- Never `docker compose up` without `--env-file mcp-tools.env` (empty `DATABASE_URL` kills LiteLLM); never touch the powerplatform deploys while a
  promotion is mid-eval; dev boxes run nothing.
- Runs execute on CT110; two `:pool` runs max; probe quota before launching (script above); on `usage limit` failures wait, don't relaunch.

## 5. Test payloads (Anthropic format, send to `POST http://localhost:4000/v1/messages` on CT202/CT204 with the master key)

Turn 1 (tool result) — the shape that failed on `ollama_chat`:
```json
{"model":"qwen3.8","max_tokens":40,"system":"You are a coding agent.",
 "tools":[{"name":"Bash","description":"run","input_schema":{"type":"object","properties":{"command":{"type":"string"}}}}],
 "messages":[{"role":"user","content":"Run ls then say done"},
  {"role":"assistant","content":[{"type":"text","text":"I will run ls."},{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"ls"}}]},
  {"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":[{"type":"text","text":"a.txt"}]}]}]}
```
Turn 2 — append the turn-1 answer verbatim (including its `thinking` block with `"signature": null`) as the next assistant message plus a user
"continue"; this is what Claude Code does and what produces `Content block is not a thinking block`. A route passes only if **both** turns return
`stop_reason: end_turn` and the run's executor completes a round with tool calls.

## 6. Acceptance for closing this handoff

1. One CT110 run with a **local executor** completes ≥ 3 rounds with tool calls and a clean audit (evidence: run id, events.jsonl, commits).
2. A documented fallback path for `:pool` exhaustion (option C) or an explicit decision to live with the 5-hour window (D/none).
3. `TEMPLATE-overseer-hierarchy.md` updated with the proven model/route matrix; `litellm-config.yaml` change merged in cognizioware-mcp-tools
   (not only hand-applied on CT202/CT204).

## 7. Pointers

- Template lessons: `tasks/TEMPLATE-overseer-hierarchy.md` (sections "Ollama Cloud quota exhaustion", "Local qwen3.8 executor", "glm-5.3 as manager").
- Scripts: `C:\tmp\quota_resume.py`, `C:\tmp\watch_ct110.py`, `C:\tmp\gates_ct110.py`, `C:\tmp\patch_qwen_oai.sh` (overseer scratch; copy into the repo if kept).
- Memory: `longhorizon-harness-deployment.md`, `lh-harness-langfuse-observability.md` (Langfuse read keys currently 401 — re-issue before any trace review).

## 8. Progress after writing (2026-09-06 15:45 PT)
- Option A executed: `qwen3-coder:30b` (18 GB) pulled onto the span server; LiteLLM model `qwen3-coder` = `openai/qwen3-coder:30b` @ `:11442/v1`
  added through the DB API (`/model/new`, no router restart). The §5 two-turn test passed through LiteLLM: turn 1 → `tool_use` (36.6 s incl. load),
  turn 2 with the echoed assistant turn → `end_turn` "done", no `thinking` blocks anywhere. The thinking models were unloaded (`keep_alive: 0`).
- Validation run launched on CT110: manager/auditor `qwen3.8` (budgets 600/900 s via workspace `.lh-harness/config.toml`), executor `qwen3-coder`,
  task = the multi-webhook-crud diagnosis/fix (workspace `cognizioware-powerplatform-evalfix`). Acceptance §6.1 applies to this run.
- 15:50 PT: first local-lane validation run failed instantly with `403 key not allowed to access model` — the `lh-harness` virtual key's allow-list
  did not include the new `qwen3-coder` group (added via `/key/update`; relaunched). Correction to §3 option C: the router already carries
  non-Ollama deployments with keys set in `mcp-tools.env` (`ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`): `claude-opus-4.6:anthropic`,
  `claude-sonnet-4.6:openrouter`, `gemini-3.1-pro-preview:latest`. A `:pool` fallback to one of them is a config-only change — but it spends real
  money per token on Paxton's accounts, so it still needs his explicit go and a `max_budget` on the harness key.
- 16:05 PT — Paxton's rulings: (1) rent-vs-subscribe question answered — GPU rental is 15–150x the subscription cost for our volume (8×H100 node ≈ $11.5k/mo
  vs $60–100/mo Ollama); GLM-5.3-Flash = 320B MoE, ~306 GiB FP8, 8-GPU Hopper floor; he will add Ollama capacity (Max or a 4th Pro) — add the key as
  `OLLAMA_CLOUD_KEY_4`, raise the concurrency cap to three. (2) **Local models: qwen3.8 and newer only** (qwen3-coder was a diagnostic exception; its
  validation run 936aef65 was stopped). (3) Option B (LiteLLM normalizer) is now authorized and running as harness task `litellm-thinking-fix-2026-09-06.md`
  (run bf5e2f69, CT110). qwen3-coder findings kept for the record: two-turn test passed; F3 appeared ("does not support thinking") and is worked
  around with `MAX_THINKING_TOKENS=0` in the workspace `.claude/settings.local.json` — the normalizer must handle F3 generically.
- 16:30 PT — Paxton chose option D: a 4th Ollama Cloud key (alias `litellm-cognizioware`, probed OK) is in the gateway env as `OLLAMA_CLOUD_KEY_4`
  and live as a 4th deployment on every `:pool` group (DB-stored, no restart). Cap raised to three concurrent `:pool` runs. Option B (normalizer) is
  in progress as run bf5e2f69. Option C (non-Ollama fallbacks) still needs his go because it spends per-token on Anthropic/OpenRouter accounts.

### Progress 2026-09-06 17:58 PT — F1 reproduced on the qwen3.8 AUDITOR
Run 20260907T005013Z_adf11963 (RP-14, mcp-cognizioware): auditor `qwen3.8` (openai/… @ :11442/v1, 900 s budget) failed after 732 s with
`Content block is not a thinking block` — the auditor echoes its own earlier thinking block on a later tool turn exactly like the executor did.
So F1 hits every role that runs more than one tool turn; only truly single-shot episodes are safe. Consequence: until the normalizer
(cognizioware-mcp-tools run bf5e2f69) is deployed on CT202, qwen3.8 is limited to roles that finish in one turn, and the harness lanes stay on the
kimi pool. The bf5e2f69 run itself (qwen3.8 manager + auditor, r6+) has survived so far — luck of short audits, not evidence of a fix.

### Progress 2026-09-06 20:05 PT — second quota wall of the day
Keys 1–3 (prax211, ai-dev01, ai-dev02) all 429 "session usage limit" again at 20:05 PT; only key 4 (litellm-cognizioware, added 16:30) answers.
Casualty: the MCP-profiles harness run da3cdef7 (round 4, `No fallback model group found for kimi-k2.7-code:pool`). Two runs (RP-16 ac894162,
webhook fix 084159ae) are riding key 4 alone. Practical ceiling today: four Pro accounts sustain roughly three concurrent kimi runs for ~3 h before
the rolling 5-hour windows close; the pool has no non-Ollama rung (paid providers need Paxton's go). The local qwen3.8 lane cannot absorb
the load until the normalizer (mcp-tools #79, deploying) is live and re-validated.
- 20:10 PT: quota watcher v2 running (probes 4 keys every 20 min; resumes ids listed in C:\tmp\pending_runs.txt when ≥2 keys OK, cap 2 running).

### Progress 2026-09-06 21:30 PT — span hang + uat gate blocked by quota, not by the hook
- The uat LiteLLM QA gate (`cognizioware-qa/litellm-uat`) has been FAILING since 21:08Z, i.e. BEFORE the normalizer: every failed subtest is a
  500 "session usage limit" from the Ollama relay (glm-5.3-flash:cloud, kimi-k2.7-code:cloud chat + /v1/messages). It passed at 16:42Z. So the
  CT202 deploy of #79 waits for the quota windows, then `gh run rerun --failed` on 34072999745 (or the next main push).
- The qwen3.8 span (cognizioware-ollama-span on ptait01) was HUNG: `/api/ps` listed the model but direct `/v1/chat/completions` timed out at 120 s,
  GPU util 0 %, GPU0 12.5 GB / GPU1 0.3 GB (runner dead). `docker restart cognizioware-ollama-span` fixed it: first answer in 51 s (cold load),
  now GPU0 20.4 GB + GPU1 14.4 GB (spread + 64k KV). Host RAM: 42 GB total, 4 GB available — that is the real ceiling for a second local model.
  This also explains the matrix run stalling at step [a] for qwen3.8. Add the span to the ops doctor as an inference probe (not just container health).
- 22:05 PT: normalizer (mcp-tools #79 + #80) live on uat; the Claude-Code-shaped round-trip matrix PASSES 4/4 for qwen3.8 on CT204. Prod (CT202)
  waits for the pipeline's uat QA gate, which is failing only on Ollama quota. §6.1 acceptance (harness run, 3 tool rounds, clean audit) runs after CT202.
- 23:10 PT: normalizer live on CT202 (prod) through the pipeline; qwen3.8 Anthropic-route smoke 200. §6.1 acceptance run launching (all-qwen3.8 trio).
- 00:25 PT Sep 7: **§6.1 ACCEPTED.** All-qwen3.8 harness run (9a864a8f) through prod: 3 tool-using rounds, clean audits, no F1/F2/F3. Local lane
  restored for every role. Remaining from §3: option A (Ollama capacity) done with key #4; option C (normalizer) done; B/D/E/F not needed today.
- 00:40 PT Sep 7: glm-5.3:cloud matrix 4/4 on prod; kimi pool 3/4 with the miss being a per-account 429 that the router retried on the same deployment. Routing gap noted (pool fallback chain + cooldown) as the next capacity item.

### Progress 2026-09-07 02:35 PT — span hang #2
The qwen3.8 span hung again ~5 h after the first restart: model listed as loaded (19 GB on GPU0 only, GPU1 empty, 0 % util), direct
/v1/chat/completions timed out at 150 s, the uat router's qwen3.8 route timed out at 7 min. Restarted the container again. Pattern: the hang
follows heavy sequential use (the all-qwen3.8 RC run) and the model ends up on a single GPU with no spread — investigate OLLAMA_SCHED_SPREAD on
reload and an idle-unload/reload cycle (KEEP_ALIVE 30m) as the trigger. The doctor "span inference" row (mcp-tools resilience task) is the
detection fix; the cure needs a probe-and-restart loop or a fix upstream. Until then: one local run at a time, and expect a restart every ~5 h.
- 02:45 PT Sep 7: auto-heal installed on ptait01: /etc/cron.d/ollama-span-probe runs /usr/local/sbin/ollama-span-probe.sh every 10 min (1-token generate with a 120 s timeout; on failure docker restart cognizioware-ollama-span; log /var/log/ollama-span-probe.log). Removes the manual restart from the loop until the root cause (single-GPU reload + KV growth) is fixed.

### ROOT CAUSE 2026-09-07 05:45 PT — ptait01 host is out of RAM; that is both the "span hangs" and the runner kills
Kernel log on ptait01: `Out of memory: Killed process (llama-server) anon-rss 6.6 GB … global_oom` at 01:40 — the qwen3.8 span's runner was
killed by the HOST (42 GB: span + CT202 stack + CT204 stack + CT100 + CT203 + page cache), which is exactly the "model listed but no inference"
state seen twice. CT203's cgroup: memory.max 12 GB, peak 0.5 GB, `oom_kill 28` — the LAN runner was never near its limit; the host killed it.
Consequences: (1) the mcp-tools LAN runner moves to CT210 on ptait07 (`ct210-lan`, label lan-cognizioware); CT203's unit is stopped;
(2) the span probe-and-restart cron stays as a safety net; (3) real fix = RAM for ptait01 or moving the uat stack (CT204) / the span off it —
Paxton's call. Until then, expect the span to be killed whenever CT202+CT204+E2E coincide.
