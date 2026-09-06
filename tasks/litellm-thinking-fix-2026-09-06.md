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
