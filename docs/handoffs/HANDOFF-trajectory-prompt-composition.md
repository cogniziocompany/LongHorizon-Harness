# HANDOFF — where the ~53k-token prompts come from, and how to read the data yourself

**From:** overseer session `5321285c-35e2-459a-9dae-92ea8811669f` (longhorizon-harness-9e)
**To:** session `bd1a9709-7456-43ef-a545-fde719a70e88`
**Date:** 2026-09-09 13:13 PT
**Raw data saved at:** `C:/tmp/traj_r1_manager.json`, `C:/tmp/traj_r1_executor.json`, `C:/tmp/traj_r1_auditor.json`

---

## The question

Synthetic's weekly credit ran out on 2026-09-09 and the manual reset is now spent until ~2026-10-09.
Measured from `LiteLLM_SpendLogs` on CT202, Synthetic requests average **~52,700–55,000 PROMPT tokens
each** (two independent windows agree) against **~604** for local models. Prompt volume is the dominant
cost driver. **What is IN those prompts?**

## Where the data is — and where it is NOT

**NOT in `LiteLLM_SpendLogs.messages`.** That column is `{}` on every row (`avg_chars = 2`) — LiteLLM
does not persist request bodies by default. It can tell you how BIG a prompt was, never what was in it.
Do not spend time there.

**It IS in the harness's own trajectory artifacts:**

```
GET http://192.168.21.168:8799/api/runs/{run_id}/rounds/{n}/trajectory/{role}
Authorization: Bearer <LH_HARNESS_WEB_TOKEN>
```

Roles are `manager`, `executor`, `auditor` — **not** the `cli_executor` / `cli_auditor` names that
appear in LiteLLM's `request_tags` (those 404 on this endpoint).

Response shape:

```json
{ "round_index": 1, "role": "executor", "steps": [...],
  "step_count": 157, "raw_chars": 138837, "trajectory_source": "normalized" }
```

Step kinds: `session` (1, metadata), `thinking`, `tool_use`, `tool_result`, `result` (1).
The `session` step carries `model`, `cwd`, `mcp_servers`, `tool_count`.

## What one round actually looks like

Run `20260909T181244Z_9d20e133`, round 1:

| role | steps | raw_chars | tool_count | tool_use | tool_result | tool_result bytes | model |
|---|---|---|---|---|---|---|---|
| manager | 3 | 44,119 | 25 | 0 | 0 | 0 | `glm-5.2:synthetic-anthropic` |
| **executor** | **157** | **138,837** | 27 | **51** | **51** | **79,273** | `nemotron-3-super:synthetic-anthropic` |
| auditor | 25 | 47,854 | 24 | 8 | 8 | 13,297 | `syn:large:vision:synthetic-anthropic` |

**Cumulative `tool_result` bytes within that single executor round:**

```
after step 20  :   5,660
after step 60  :  23,679
after step 100 :  36,340
after step 156 :  79,273
```

Largest individual steps: **19,093 B** and **17,301 B**, both `tool_result`.

## The finding

**~79KB of accumulated tool output — roughly 20k tokens — inside ONE round, re-sent on every
subsequent call in that round.** That is the compressible part. It is accumulated *output*, not work,
and not tool definitions.

**Two things I got wrong first, so you don't repeat them:**

1. **`completion_tokens` is unreliable.** SpendLogs reports ~2 completion tokens per request over 1,812
   Synthetic requests — not credible. It is the streaming spend-logging bug (task 82:
   `cannot pickle '_thread.RLock'` inside `copy.deepcopy` in `spend_log_error_logger`). So
   `total_tokens` is a **prompt-weighted floor**, not a total. Prompt-token figures survive; ratios do not.
2. **Tool definitions are NOT the bloat here.** The `session` step reports `mcp_servers: 0`, and live
   runs show `mcp_profile=None`, `agent=codex` — runs currently get **no MCP tools at all**. Those
   24–27 tools are the agent's *native* tools. I initially overstated this.

## Context you may want

The gateway aggregate exposes **973 tools across 37 namespaces = 737,226 bytes of schema** (~184k
tokens if ever requested whole). Worst offender by density: `n8n` — 39 tools but **106,531 bytes**
(2,732 B/tool), 4% of tools and 14% of bytes.

The harness already has a profile mechanism at `/api/meta` → `mcp_profiles`: `none`, `audit`
(read-only), `default`, `ops`, `full` (**no allow-list header — all 973 tools**), with `defaults.roles`
mapping manager→`default`, executor→`default`, auditor→`audit`. **It exists and is not being used** by
current runs.

## Open, if you want to take it further

- What fraction of a prompt is (a) system/instructions, (b) workspace context re-sent each round,
  (c) accumulated conversation, (d) tool definitions? (b) and (c) are compressible; **halving them
  halves the bill with no routing change and no loss of model quality.**
- Does round N+1 re-send round N's tool results, or start fresh? Compare consecutive rounds.
- LiteLLM's redis cache is enabled (`cache: true`, ttl 300) but has **zero hits on 2,107 Synthetic
  requests** — it is exact-match response caching, which cannot hit on multi-turn agent traffic.
  Prefix/prompt caching would be the right kind. That is queued as task 91.

**Handling note:** trajectories contain repository contents. Report sizes and proportions; do not paste
prompt bodies. If anything credential-shaped appears, say that it appeared and where — do not reproduce it.
