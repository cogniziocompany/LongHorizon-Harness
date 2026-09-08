# Task 41 — bare LiteLLM /health sweep in the deploy lane (close the remaining gaps)

Source: planning session b9d32bfc-53e5-4dc2-b865-d27a53dc92d3 (plan file on the overseer PC:
`~/.claude/plans/troublehsoot-known-lane-defect-cheeky-emerson.md`, written against mcp-tools main 9943faf).
Queue entry `C:/tmp/queue/00b-41-lane-health-probe.json`, task text `C:/tmp/litellm-health-sweep-task.txt`.

## Why
LiteLLM's bare `GET /health` runs a live completion against every configured backend (paid Perplexity,
Ollama Cloud quota, local GPUs). `/health/liveliness` and `/health/readiness` are the cheap probes;
`?model=<name>` probes one model. See memory `litellm-health-sweep-cost`.

## State (overseer re-verified on origin/main c323c0a, 2026-09-08)
PR #105 / 7c8125b already fixed the reported symptom (suite 01 blowing its 15 s beforeAll on
`litellm.get('/health')`, run 34205773455) and de-bared suites 01/04/05/12/39 plus added the lint guard.
The lane is not red on this today. What remains are the gaps that migration did not reach:

| # | Site | Problem |
|---|---|---|
| 1 | `deploy-mcp-tools.yml` CT202 "Health checks" (~902) | public bare `/health` with `--max-time 30`; always times out, bumps PUBLIC_FAIL, burns quota every prod deploy; only `::warning::`, which is why it survived |
| 2 | same step's loopback loop (~934) | port 4000 sits in the generic `/health` list with a 5 s budget |
| 3 | `infrastructure/deploy-mcp-tools.ps1` (~551) | `:4000/health` with `-TimeoutSec 10` |
| 4 | `e2e/suites/00-lint-no-bare-litellm-health.test.js` (~21) | `SUITES_DIR = path.resolve(__dirname)` — guard scans only `e2e/suites`, so nothing can catch 1–3 |
| 5 | **found by the overseer, not in the plan:** `e2e/run-all.sh` (~115) | pre-flight `curl "${BACKEND_URL}/health"` with the master key; BACKEND_URL resolves to LiteLLM (~31/~43), so **every lane's E2E Gate triggers a full sweep** |

## Fix shape
Reuse the two-stage probe the same workflow already uses in the UAT job and the CT202 config-change path
(liveliness, then readiness asserting `"db":"connected"`), plus `/v1/models` assertions when this deploy
changed the model list (reuse the existing `LITELLM_CONFIG_CHANGED` detection). Pull port 4000 out of the
generic loopback list. Keep `PUBLIC_FAIL > 0` a warning in this change; hard-failing is a separate call.
Widen the lint guard to the repo root over `.yml/.yaml/.ps1/.sh/.js` and add an unquoted `:4000/health`
pattern (the current regex needs a closing quote, so it misses `http://127.0.0.1:$ep/health`).

## Verification the run must produce
Prove the widened guard fails on the pre-fix state (stash the fixes → guard names the workflow, the ps1 and
run-all.sh → unstash → passes), guard + suite 01 green, diff stat, and a list of every remaining
bare-LiteLLM-`/health` hit with the reason each is legitimate.

## Deployment (overseer)
Merge → lane: E2E Gate → Apply LiteLLM config to UAT CT204 → QA Gate (UAT) all green **before** the CT202
job; no hand deploy. On the prod run, confirm the health-checks step finishes in seconds with `PUBLIC_FAIL=0`
and port 4000 reporting 200, then the post-deploy smoke suites are green, then spot-check that no
Perplexity/Ollama-Cloud completions appear in the deploy window.

## Out of scope
Non-LiteLLM `/health` endpoints (oauth2-proxy, fleet-admin, ms365, kb, compose healthchecks) and suite 13's
model-inference gate, which is supposed to sweep.
