# Local tev1 decision routing for closed-output routing decisions

**Author:** LongHorizon-Harness executor session, task `JEV-LOCAL-ROUTING-EVAL`
**Date:** 2026-10-17
**Status:** Draft PR — research only; no integration code merged
**Branch:** `research/jev-routing` (from `origin/main`)
**Audience:** Paxton, devspecops1, SYSTEM-INQUIRY-ROUTER owner

---

## 0. TL;DR (one screen)

1. **tev1 is a real Ollama decision model** and the Ollama endpoint for it is
   `POST /v1/systemone` (not `/api/chat`, not `/api/generate`). The body shape
   is `{ model, state, questions }`; each question has `type: choice | noul | score`
   with a `criteria` map (or array, for `score`); the response carries `answers`
   keyed by the question name with `choice/probabilities/confidence` for
   `choice`, `noul` for `noul` (yes/no probability), or `score/legend/probabilities/confidence` for `score`.
2. **tev1 fits the "local Jev" slot exactly** if we adapt our routing decisions
   into the choice/noul/score primitive vocabulary. The closed-output decisions
   the task lists — classify, route, score, yes/no, tool choice — are the entire
   decision surface; tev1 is fine-tuned precisely for them.
3. **`jev-gateway` is the only jev-* repo worth adopting as-is** because it
   already speaks OpenAI-compatible upstreams for the heavy LLM (`--upstream-base-url`)
   and forwards `Authorization`. Routing it at Ollama for **Jev itself** still
   needs a ~40-line adapter (the gateway only supports `TYPESAFE_BASE_URL`,
   `OPENROUTER_API_KEY`, `AI_GATEWAY_API_KEY`, `OPENCODE_API_KEY`).
4. **Recommended LiteLLM alias proposal** (admin-API script only;
   `litellm-config.yaml` is frozen):

   ```yaml
   # Proposed alias (admin-API apply only)
   model_name: decision-fast
   litellm_params:
     model: ollama_chat/tev1:4b
     api_base: http://192.168.21.110:11441  # ptait01 lane, :11438 for :0.8b fallback
     keep_alive: 0          # unload immediately; lanes also serve ornith-1.5:9b-256k
     stream: false
   model_name: decision-tiny
   litellm_params:
     model: ollama_chat/tev1:0.8b
     api_base: http://192.168.21.110:11438
     keep_alive: 0
     stream: false
   ```

   Use `decision-fast` for human-visible routing (trio choice, manager `next_step`),
   `decision-tiny` for in-loop tool-choice tiebreaks where latency dominates.
5. **Bench script** (`scripts/bench/jev-routing-bench.py`) targets the 50-item
   labelled decision set (`scripts/bench/jev-routing-decisions.jsonl`); it calls
   `/v1/systemone` with `choice` for `classify/route/tool choice`, `noul` for
   yes/no, and `score` for 0–4 severity scoring.
6. **No calls were made to the live lanes in this research round.** The script
   constructs the calls and is ready to run; an operator should execute it
   on a fresh lane or in a scratch container, never on CT110.

---

## 1. tev1 model card and prompt/label format

Source: <https://ollama.com/library/tev1> (fetched 2026-10-17).

| Field | Value |
|---|---|
| Provider | Together AI |
| Family | Tev1 — experimental decision model |
| Base | Fine-tuned from Qwen3.5 |
| Sizes | `tev1:4b` (~4.4 GB), `tev1:0.8b` (~0.8 GB) |
| Context | 256 K window; effective use ~2,000 tokens |
| Ollama endpoint | `POST /v1/systemone` (NOT `/api/chat`, NOT `/api/generate`) |
| Required Ollama version | ≥ 0.35.0; CLI surface added 0.40.4 |
| Question types | `choice` (2–26 options), `noul` (true/false), `score` (2–10 levels) |
| Decision output | `{ choice, probabilities, confidence }` / `{ noul }` / `{ score, legend, probabilities, confidence }` |
| Token usage in response | `usage.input_tokens`, `usage.output_tokens` |

The `state` field is the text to judge; it may be a string, JSON object, or
JSON array. `questions` is 1–64 named questions. `keep_alive` controls how long
the model stays loaded — **must be set to 0 on the ptait01 lanes** because the
lanes also serve `ornith-1.5:9b-256k` (~11.3 GB VRAM); tev1 is small but we
must not let it hold residency.

### 1.1 cURL example (verbatim from the model card)

```bash
curl http://localhost:11434/v1/systemone -d '{
  "model": "tev1",
  "state": "Customer message: Hi, I checked my statement and your company charged my card twice...",
  "questions": {
    "intent": { "type": "choice", "instructions": "...", "criteria": { ... } },
    "refund": { "type": "noul", "instructions": "Does the customer explicitly ask for a refund?" }
  }
}'
```

---

## 2. Ollama decision API — full request/response shape

Source: <https://docs.ollama.com/api/systemone> (fetched 2026-10-17).

### 2.1 Request fields (`SystemOneRequest`)

| Field | Required | Type | Notes |
|---|---|---|---|
| `model` | yes | string | `tev1`, `tev1:0.8b`, `nimble`, `clef`, `clef-flash` |
| `state` | yes | string \| object \| array | "A nonempty string, or an object or array serialized as JSON text. Not interpreted as chat messages or multimodal input." |
| `questions` | yes | object | 1–64 entries; name pattern `\\S` |
| `images` | no | array of base64 | Vision-capable models only (Clef) |
| `keep_alive` | no | string \| number | `0` unloads after request; default server setting (5m) |

Each question entry:

- **choice** — required: `type:"choice"`, `instructions`, `criteria` (object, 2–255 keys, non-blank).
- **noul** — required: `type:"noul"`, `instructions`; optional `criteria` with only `false`/`true`.
- **score** — required: `type:"score"`, `instructions`, `criteria` (array of 2–10 strings, lowest first; scale is `0` to `count-1`).

### 2.2 Response fields (`SystemOneResponse`)

| Field | Type | Notes |
|---|---|---|
| `model` | string | Echoes model name |
| `answers` | object | One entry per question name |
| `usage.input_tokens` | integer | Including image positions |
| `usage.output_tokens` | integer | Internal scoring tokens |

Per-answer shape:

- **choice** — `{ type:"choice", choice: <key>, probabilities: { <key>: 0..1 }, confidence: 0..1 }`.
- **noul** — `{ type:"noul", noul: 0..1 }`. 0.5 is the abstain boundary; >0.5 means "true".
- **score** — `{ type:"score", score: float, legend: [...], probabilities: { <level>: 0..1 }, confidence: 0..1 }`.

### 2.3 Error codes

- `400` — invalid request, unsupported model, prompt exceeds context.
- `401` — cloud model without API key.
- `402` — cloud model not on Free plan, payment failed.
- `404` — model not pulled locally.
- `413` — request > 32 MiB local / 13 MiB cloud.
- `500` — model load / scoring failed.

### 2.4 Limits relevant to ptait01

- Local: text + schema ≤ 64 KiB; whole request ≤ 32 MiB.
- Tev1 trained on 2–24 options for `choice`; stay in that range.
- One request at a time per lane (the local span is single-stream for 64k models; tev1 is small but the Ollama runner serialises by default and we must not set `OLLAMA_NUM_PARALLEL` on a shared lane).

---

## 3. Routing-point map in our stack

| # | Routing point | File / locus | Output space | Mappable to tev1? |
|---|---|---|---|---|
| 1 | **Launcher trio choice** | `src/lh_harness/launcher.py:518,673,900` | `trio ∈ {kimi, qwen}` + capacity skip | yes — `choice` over `{kimi, qwen, skip}` |
| 3 | **Manager `next_step`** | `src/lh_harness/role_prompts.py:481-510`; `src/lh_harness/types.py:44` | `RoleNextStep = Literal["gui","cli","done","blocked","invalid","ask"]` | yes — `choice` over the 6 labels, with `noul` confidence gate |
| 4 | **Queue triage** | `src/lh_harness/validate_queue.py` (rules `blocker-target-does-not-exist`, `gateway-alias-exists`) | pass/warn/error | yes — `score` 0..3 on cleanliness; `choice` on disposition |
| 5 | **Hard-signal classification** | `src/lh_harness/runtime_signals.py:hard_signal_labels` | label | yes — `choice` over label set |
| 6 | **Provider-error classification** | `src/lh_harness/provider_errors.py:classify_agent_runtime_failure` | `runtime_failure` enum | yes — `choice` over enum |
| 7 | **Auditor status gate** | `src/lh_harness/manager.py:1132,1187,1237` | `complete/clean/aligned` | yes — `noul` × 3 (is_complete, is_clean, is_aligned) |
| 8 | **Claude Code subagent/tool choice** | `src/lh_harness/adapters/claude_code.py`; passes `--model` and `--effort`; `claude-opus-5` / `claude-sonnet-5` / `claude-haiku-4-5` per `jev-router` ladder | tool name + arg values | yes — but only via `jev-gateway` interceptor (see §5) |
| 9 | **LiteLLM routing** | `infrastructure/litellm-config.yaml` (frozen); admin-API script only | model alias resolution | yes — proposed alias below |

Items #1, #3, #5, #6, #7 are *inside* the harness; items #2, #4, #8, #9 are at the
boundary. The harness-side points (#1, #3, #5–#7) are the right place to *start*
because they have the cleanest decision vocabulary and the lowest blast radius —
a single misclassification is recoverable inside the loop.

---

## 4. 50-item labelled decision set

The benchmark set is in `scripts/bench/jev-routing-decisions.jsonl` (50 lines).
The 50 items are **derived from real harness gates/queue triage — names only,
no secrets**. Each line has:

```json
{
  "id": "manager_next_step_01",
  "category": "manager_next_step",
  "state": "<short text the decision sees>",
  "questions": {
    "answer": {
      "type": "choice",
      "instructions": "<what to decide>",
      "criteria": { "<label>": "<description>" }
    }
  },
  "gold": "<label>",
  "source": "src/lh_harness/role_prompts.py:481-510"
}
```

### 4.1 Distribution

| Category | Count | Question type | Why this type |
|---|---|---|---|
| `manager_next_step` | 12 | `choice` over `{gui, cli, ask, done, blocked, invalid}` | matches `RoleNextStep` literal |
| `launcher_trio_choice` | 8 | `choice` over `{kimi, qwen, skip}` | capacity gate + entry.trio field |
| `provider_error_classify` | 8 | `choice` over the `runtime_failure` enum | direct mapping from `provider_errors.py` |
| `hard_signal_label` | 6 | `choice` over the hard-signal label set | from `runtime_signals.py` |
| `auditor_complete_clean_aligned` | 9 | three `noul` per item | the three control-header flags |
| `queue_triage_score` | 7 | `score` 0..3 on cleanliness | maps to validate_queue WARN/ERROR tiers |

Total = 12 + 8 + 8 + 6 + 9 + 7 = 50.

### 4.2 Sampling rule (so the auditor can re-derive)

- For `manager_next_step` and `launcher_trio_choice`: re-derive from
  `role_prompts.py` line 481-510 and `launcher.py` line 518/673/900 respectively.
- For `provider_error_classify`: re-derive from the labels in
  `src/lh_harness/provider_errors.py` (function `classify_agent_runtime_failure`).
- For `hard_signal_label`: re-derive from `runtime_signals.hard_signal_labels()`.
- For `auditor_complete_clean_aligned`: re-derive from
  `src/lh_harness/auditor_agent.py` (`has_valid_auditor_control_header`).
- For `queue_triage_score`: re-derive from `validate_queue.py` rules 1–7.

The 50-item set does not include secrets; the `state` strings contain task names
from `queue/*.json` only (no `task_file` content, no keys).

---

## 5. jev-* candidate repos — security and adapter review

The five candidates from the task description, ranked by usefulness:

| Repo | License | Verdict | Why |
|---|---|---|---|
| `vinilana/jev-gateway` (318★) | MIT | **ADOPT (with adapter)** | Already speaks OpenAI-compatible upstreams; only needs a 40-line adapter so `/v1/systemone` calls go to the local Ollama lanes instead of `api.typesafe.ai`. The Claude Code integration is the strongest of the five. |
| `satviksinha/jev-model-router` | MIT | CONDITIONAL | Only two providers (TypeSafe + Vercel AI Gateway). `TYPESAFE_BASE_URL` is the only redirect hook. Cleanly local-hostable, but the local endpoint must already speak the Jev API shape. |
| `shitianfang/jev-use` | MIT | DEFER | Plugin for Claude Code/Codex/pi; backends are TypeSafe/OpenRouter/Vercel plus a keyless `mock`. No documented path to a local Ollama endpoint. Smaller code surface but no `BASE_URL` hook. |
| `BenjaminPolge/jev-architect` | Apache-2.0 | NO | It's a planning skill, not a runtime caller; "It does not call Jev, and it adds no runtime dependency to your project." Useful for the SYSTEM-INQUIRY-ROUTER upstream research, not for our routing decision. |
| `pouramin/jev-router-windows` | MIT | NO | WPF + PowerShell Windows-only setup panel; stores `TYPESAFE_API_KEY` in DPAPI. No local-Ollama support; not portable. |

### 5.1 Security review of `vinilana/jev-gateway` (chosen)

Read at <https://github.com/vinilana/jev-gateway> on 2026-10-17. Repo is
**MIT, 318 stars, gateway listens on 127.0.0.1 only**. Behavioural summary:

- **Network surface**: by default `127.0.0.1:8787`; only opens to LAN if the
  operator sets `HOST` (and is required to also set `ROUTER_API_KEY`).
- **Auth model**: holds `UPSTREAM_API_KEY` (the heavy LLM key) server-side;
  if `ROUTER_API_KEY` is set, every client must also present it. If unset,
  the gateway refuses to bind off-loopback (the README states this).
- **Provider keys**: `TYPESAFE_API_KEY`, `OPENROUTER_API_KEY`,
  `AI_GATEWAY_API_KEY`, `OPENCODE_API_KEY`. Only the active one is read.
- **Forwards `Authorization`**: "The client's `Authorization` header is
  forwarded to the LLM upstream." — this is *exactly* what we want when
  the upstream is LiteLLM, because our harness agents already send a Bearer.
- **Debug dump**: `JEV_DEBUG_DUMP_DIR` writes bodies + system prompts, but
  the README states these are "only root-readable" (file mode 0o600).
- **Failure semantics**: "If Jev is down, slow, or your key is wrong,
  every request simply goes straight to the LLM." — safe degradation;
  the heavy LLM is always the fallback.
- **Codex/Claude/OpenCode**: integration is by `--upstream-base-url`, so the
  harness's existing `ANTHROPIC_BASE_URL` to LiteLLM is unchanged; we just
  point Claude Code at the gateway instead of the router.
- **Mode header leak**: every response carries `x-jev-gateway-mode`
  (`direct` / `forced` / `hint` / `none` / `passthrough`). Useful for
  observability; not a security risk because the gateway only listens
  on loopback.

Risks specific to using jev-gateway with **local tev1**:

1. **No native local Ollama backend for Jev itself.** The gateway's four
   Jev providers are all hosted services. The escape hatch is
   `TYPESAFE_BASE_URL=https://our-proxy`. The adapter has to:
   - accept `{model:"jev-latest", state, questions}` body shape,
   - forward to `POST {OLLAMA_BASE}/v1/systemone` with `model: "tev1:4b"`,
   - translate the four-decimal rounding to Ollama's native precision,
   - pass `keep_alive: "0"` so we never leave tev1 loaded on ptait01.
2. **Upstream key forwarding**: the gateway forwards `Authorization` to
   the heavy-LLM upstream. Our LiteLLM key is acceptable here, but the
   gateway should NOT be exposed off-loopback with `ROUTER_API_KEY` unset.
3. **`JEV_DEBUG_DUMP_DIR`**: must remain unset in production; the dump
   writes raw conversation bodies to disk, which is a secrets hazard.
4. **Passthrough mode**: when Jev is unhealthy the gateway forwards the
   request byte-for-byte to the heavy LLM. This is *good* (we never lose
   work), but we need metrics on how often it triggers, otherwise we
   will silently burn the heavy LLM budget.

**Risk verdict — adopt with mitigation.** Three mitigations are required
before devspecops1 enables jev-gateway in front of Claude Code:

- Ship the proxy as a tiny `internal/decisiond.py` service (~40 lines)
  that speaks `TYPESAFE_BASE_URL`-compatible protocol and forwards to
  Ollama `/v1/systemone`. Bind to 127.0.0.1 only. No upstream auth header
  forwarded (Ollama local lanes don't need it).
- Set `JEV_TIMEOUT_MS=4000` and `JEV_MIN_CONFIDENCE=0.7` (defaults).
- Add a Prometheus counter `jev_gateway_mode_total{mode=...}` so we can
  see how often `passthrough` fires.

### 5.2 Security review of the other four (deferred)

| Repo | Verdict | Reason |
|---|---|---|
| `satviksinha/jev-model-router` | HOLD | Only 7 stars, code is small (single provider.ts adapter). Security model is "TYPESAFE_BASE_URL" only — no built-in auth between the agent and the router. Local-hostable but we lose the OpenAI-compatible upstream integration jev-gateway already gives us. |
| `shitianfang/jev-use` | DEFER | Plugin scope is Claude Code/Codex/pi only; adapters are TypeSafe/OpenRouter/Vercel/mock. No documented `OLLAMA_BASE_URL`. Worth re-examining when the SYSTEM-INQUIRY-ROUTER task is built. |
| `BenjaminPolge/jev-architect` | N/A | Planning skill, no runtime call. Use the vendor-published `https://docs.typesafe.ai/llms.txt` directly instead. |
| `pouramin/jev-router-windows` | N/A | Windows-only WPF panel; no Linux port path; DPAPI key storage doesn't help on the ptait01 lanes. |

---

## 6. LiteLLM alias proposal

**Constraint:** `infrastructure/litellm-config.yaml` is frozen. The only
allowed change mechanism is the LiteLLM admin-API script
(`scripts/deploy/litellm_admin.py` or equivalent — operator verifies which
one is current before applying). Both new aliases below are **admin-API
additions**, never file edits.

### 6.1 Proposed YAML snippet (admin-API only)

```yaml
- model_name: decision-fast
  litellm_params:
    model: ollama_chat/tev1:4b
    api_base: http://192.168.21.110:11441
    keep_alive: 0
    stream: false
    timeout: 5
- model_name: decision-tiny
  litellm_params:
    model: ollama_chat/tev1:0.8b
    api_base: http://192.168.21.110:11438
    keep_alive: 0
    stream: false
    timeout: 5
```

Notes:

- `ollama_chat/tev1:4b` is the LiteLLM route key for any Ollama-served
  model that speaks the OpenAI chat surface. **It is NOT a chat endpoint
  for tev1** — tev1 will reject `/v1/chat/completions` calls. The harness
  must call `/v1/systemone` directly through a small adapter; LiteLLM
  cannot proxy `/v1/systemone` without an extension.
- Therefore the `decision-fast` and `decision-tiny` aliases are **local
  symbolic names the harness uses to address the decision lanes**;
  routing in LiteLLM's normal `model: ${alias}` lookup does not apply
  because the call bypasses LiteLLM. **The aliases are documentation
  sugar, not a routing change.** This is the critical caveat that
  Paxton should review before approving.
- `keep_alive: 0` is essential — the ptait01 lanes also serve
  `ornith-1.5:9b-256k` at ~11.3 GB VRAM; tev1 must not hold residency.
- `stream: false` — decision responses are a single JSON object; streaming
  would just add complexity.
- `timeout: 5` — measured tev1 latency in Together AI's published numbers
  is "low hundreds of ms"; 5 s is a generous ceiling that catches a
  stuck lane.

### 6.2 Where the harness calls tev1 directly

The harness owns a small adapter module (proposed: `src/lh_harness/decision.py`).
It will:

- hold the lane URL as an env var (`LH_HARNESS_TEV1_4B_URL`,
  `LH_HARNESS_TEV1_08B_URL`),
- expose three functions: `choice(state, questions, criteria) → {choice, probabilities, confidence}`,
  `noul(state, instructions, criteria=None) → float`,
  `score(state, instructions, levels) → float`,
- default `keep_alive="0"`,
- cache nothing; the lane is shared and we don't want to affect it.

### 6.3 Where jev-gateway fits (alternative integration path)

If devspecops1 prefers the gateway approach over the inline adapter:

```
Claude Code ──ANTHROPIC_BASE_URL──> jev-gateway :8787 ──UPSTREAM──> LiteLLM
                                         │
                                         └─TYPESAFE_BASE_URL──> decisiond :8791 ──> Ollama :11441
```

`decisiond` is the 40-line proxy described in §5.1. `TYPESAFE_BASE_URL=http://127.0.0.1:8791`
makes the gateway ask `decisiond` instead of `api.typesafe.ai`. `decisiond`
forwards verbatim to `/v1/systemone` on the ptait01 lane.

This path is the right choice for Claude Code tool routing because it
inherits jev-gateway's "if Jev is down, fall through to the LLM"
behaviour — a misclassification by tev1 cannot block Claude Code from
working.

---

## 7. Bench methodology

`scripts/bench/jev-routing-bench.py` is **read-only** in this PR. It
builds and prints the HTTP requests; it does not contact ptait01 or any
cloud provider from this round. An operator runs it against:

1. **tev1:4b** — `POST http://192.168.21.110:11441/v1/systemone`
2. **tev1:0.8b** — `POST http://192.168.21.110:11438/v1/systemone`
3. **ornith/qwen alias** — current local model (the harness's main
   local fallback today)
4. **Cloud model** — the current `claude-haiku-4-5` (or whatever the
   operator wants to use as the cloud baseline)

The bench reports per-category accuracy and median latency. The output is
JSON so the auditor can diff runs across commits.

### 7.1 What "current local model" means

The task says "ornith/qwen alias". On the ptait01 lanes today the local
model the harness uses is `qwen3.8-27b` (configured via `litellm-config.yaml`,
seen via `discover_model_catalog()` in `src/lh_harness/model_catalog.py`).
The bench treats this as a single "local baseline" lane and asks the
operator to point it at whatever Ollama URL serves that model
(`OLLAMA_LOCAL_BASELINE_URL` env var).

### 7.2 What "cloud model" means

The harness's default cloud model is `claude-haiku-4-5` (the
`DEFAULT_CLAUDE_MODEL` constant). The bench uses it as the cloud
baseline. It calls the Anthropic `/v1/messages` endpoint through
whatever `ANTHROPIC_BASE_URL` the operator has set; this lets the bench
go through LiteLLM the same way the harness does.

### 7.3 Why the cloud baseline matters

We need to know whether tev1's accuracy on the 50-item set is *good enough*
that we'd rather use it than spend a cloud request on the same decision.
A `noul` yes/no that costs 250 ms on tev1:0.8b is a clear win over
1.2 s on `glm-5.3-flash:synthetic` if accuracy is within 3 percentage
points. The bench reports both numbers so devspecops1 can decide.

---

## 8. Open questions for Paxton / devspecops1

1. **Is the `decision-fast` LiteLLM alias the right shape, given that
   LiteLLM cannot proxy `/v1/systemone`?** If yes, treat the alias as a
   documentation handle and ship the inline adapter
   (`src/lh_harness/decision.py`). If no, the alias needs an extension
   to LiteLLM, which is out of scope for this PR.
2. **Should jev-gateway be deployed on ptait01 or on the harness host?**
   ptait01 is shared; the gateway would compete with ornith-1.5:9b-256k
   for VRAM only if `JEV_DEBUG_DUMP_DIR` is set (it's a disk write, not
   VRAM). Decision: ptait01 is fine as long as `JEV_DEBUG_DUMP_DIR`
   stays unset.
3. **What is the policy when tev1's confidence < 0.7?** Two options:
   - "ask the operator" — `Next: ask` — slow but safe;
   - "fall through to the heavy LLM" — `passthrough` mode in
     jev-gateway — fast but costs a request.
   Recommendation: option (b) inside the harness (inline adapter) and
   option (a) only for the final `auditor_complete_clean_aligned`
   check, which gates the run.
4. **Who owns the `decisiond` proxy?** Suggest devspecops1, because it
   needs to land on the gateway host. Out of scope for this PR; this
   doc is the handoff.

---

## 9. Out of scope / not changed

- No harness code changed.
- No `litellm-config.yaml` edit.
- No installs on ptait01 (the shared model store). The bench script
  **does not make a live request** in this round.
- No ptait09 access.
- The audit-header protocol on auditor reports is unchanged.

---

## 10. Source-of-truth URLs (for the auditor)

- tev1 model card: <https://ollama.com/library/tev1>
- Ollama decision API reference: <https://docs.ollama.com/api/systemone>
- Ollama decision guide: <https://docs.ollama.com/capabilities/decision>
- Ollama release notes (decision models):
  <https://github.com/ollama/ollama/releases> (v0.35.0, v0.40.4)
- `vinilana/jev-gateway` README: <https://github.com/vinilana/jev-gateway>
- `satviksinha/jev-model-router` README:
  <https://github.com/satviksinha/jev-model-router>
- `shitianfang/jev-use` README: <https://github.com/shitianfang/jev-use>
- `BenjaminPolge/jev-architect` README:
  <https://github.com/BenjaminPolge/jev-architect>
- `pouramin/jev-router-windows` README:
  <https://github.com/pouramin/jev-router-windows>
- Dynamic model routing handoff: `docs/handoffs/dynamic-model-routing-handoff-2026-09-08.md`
- Role next-step parser: `src/lh_harness/role_prompts.py:481-510`
- Launcher trio logic: `src/lh_harness/launcher.py:518,673,900`
- Runtime signal labels: `src/lh_harness/runtime_signals.py:hard_signal_labels`
- Provider error classifier: `src/lh_harness/provider_errors.py`
- Validator: `src/lh_harness/validate_queue.py`