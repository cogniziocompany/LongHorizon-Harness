# Spike: quota-aware lane allocation (2026-11-20)

**Ticket:** easybutt0n-17 — **there is no dedicated ticket yet.** This spike
was requested directly by Paxton (message timestamped 2026-11-20 02:1xZ,
quoted below) and carries the easybutt0n-17 prefix only because that is the
working label tonight's runs were filed under. The report says this up front
because the ticket ID will need re-pointing once a real ticket exists.

**Scope:** research + design only. No code changed, no configuration written,
nothing here is implemented. Read-only on every host and on the LiteLLM
router; `litellm-config.yaml` untouched (frozen); no key was used or printed.

---

## 1. Plain language: what is wrong tonight

> *"thats a silly cap ... spike a better way to work"* — Paxton, 2026-11-20 02:1xZ

Tonight (per the ticket narrative) every harness run died in round 1 because
the GLM pool — 3 Ollama Cloud accounts plus z.ai accounts 4/5 — was
weekly-exhausted. Meanwhile the Kimi lane (synthetic.new Big plan, claimed
472M Kimi K3.1 Pro tokens/month after a +30% bump) sat mostly idle behind
launcher caps written by hand: kimi 2, local 1 (the ticket's runtime
description of tonight's caps; the repo's shipped defaults differ and are
cited in §5).

The failure is structural, not bad luck:

1. **A cap is a number, not a measurement.** The launcher decides how many
   runs each lane may carry from `[queue.capacity]` integers. Those integers
   were typed by a human. They do not move when a subscription's remaining
   quota moves. A lane can be "at capacity 0" (idle GPUs, tokens unspent)
   while another lane happily launches into an account that is already dry.
2. **Exhaustion is discovered by failing.** The first evidence that an
   account is exhausted is a 429 arriving mid-run, after the run has already
   started, burned a launch, and spent round-1 wall clock. The launcher's
   backoff then parks the *entry* for 30/90 minutes — but the *account* stays
   in rotation, so the next entry walks into the same wall.
3. **Nothing spills.** A trio is bound at enqueue (`queue.py:354` documents
   the trio as a launch-time binding; `launcher.py:914-936` re-reads trios
   per pass but never re-points an entry). When the GLM-facing trios are
   quota-dead, entries wait behind their own cap instead of moving to a lane
   with headroom.

The ask, restated as four properties:

- **(P1)** no run *starts* on an account known exhausted;
- **(P2)** work spills automatically to the subscription with headroom
  (kimi → zai acct1 → local qwen/ornith) when one runs dry;
- **(P3)** caps follow quota + provider concurrency limits, not fixed
  numbers;
- **(P4)** exhausted rows are auto-blocked on a weekly/monthly 429 (z.ai
  `[1310]` or Ollama "weekly usage limit") and auto-unblocked at the reset
  time the error states, using the scoped key `infraops1-model-block` (now
  `proxy_admin`: `/model/block`, `/model/unblock` only).

---

## 2. What exists today (the surface audit, DO-a)

Every reuse point below was re-verified against the working tree at commit
`03704b2` this round. Note on method: the graphify `query_graph` tool was the
requested lookup surface; it (and the whole MCP gateway) returned HTTP 403 to
this run, exactly as in round 1. The manager has formally accepted the
labeled grep/read equivalent, which is what produced these citations.

### 2.1 Lane caps are static integers in three layers

| Surface | What it does | Citation |
|---|---|---|
| `_QUEUE_CAPACITY_KEYS` — the allowed `[queue.capacity]` key set | `kimi_max, qwen_max, glm_max, zai_max, zaiq_max, minimax_max, min_healthy_keys, key_health_url, poll_seconds, max_retries, quota_backoff_minutes` | `src/lh_harness/config.py:242-254` |
| Documented defaults with per-key meaning | `kimi_max=3, qwen_max=1, glm_max=1, zai_max=5, zaiq_max=1, minimax_max=2, min_healthy_keys=2, quota_backoff_minutes=[30,90]` | `src/lh_harness/config.py:447-457`; user-facing table at `docs/queue.md:202-212` |
| Normalized defaults applied when the key is absent | same values, hard-coded | `src/lh_harness/config.py:851-861` |
| Runtime overrides: `queue_capacity` table | `trio TEXT PRIMARY KEY, max_concurrent INTEGER, max_peak INTEGER` — an admin can move a cap at runtime without a restart | `src/lh_harness/settings_store.py:354-358`; readers `queue_capacity()` / `queue_capacity_rows()` at `src/lh_harness/settings_store.py:542-566` |
| Launcher overlay (hot reload) | every scheduling pass re-merges settings-DB trios/caps onto the bootstrap config (LHH-QUEUE-HOTRELOAD); a deleted row falls back to config.toml; `enabled=false` removes a trio | `src/lh_harness/launcher.py:914-936` |
| Peak-window companion | `max_peak` = a LOWER cap during Mon-Fri 06:00-10:00 UTC (z.ai peak billing hours, 2026-10-11), `_effective_cap` takes the min when in-window | `src/lh_harness/settings_store.py:549-566`, `src/lh_harness/launcher.py:893-905` |
| Node-wide ceiling | `node_max_runs` — one number bounding the sum of all lanes (introduced because summed per-trio caps once reached 11 active vs an advertised 5) | comment at `src/lh_harness/config.py:258-264`; enforcement `src/lh_harness/launcher.py:1652-1680` |

**Why this matters for the design:** the overlay is *already* a per-pass,
DB-driven, hot-reloaded lane-control surface. A quota-aware cap does not need
a new mechanism to take effect at runtime — it needs the *value* of
`max_concurrent` to be produced from quota instead of typed by hand. That is
the single most important reuse point in this repo.

### 2.2 Quota failures are already classified, and reset times already parsed

| Surface | What it does | Citation |
|---|---|---|
| Quota-cause signatures | `_QUOTA_CAUSE_SIGNATURES = ("provider_quota", "insufficient_quota", "insufficient quota", "quota exceeded", "usage limit")` — tried *before* generic rate-limit signatures so a quota 429 is labelled quota | `src/lh_harness/launcher.py:325-332`, classify at `:364-372` |
| Provider-error kind "quota" | regex family incl. `insufficient_quota`, `quota exceeded`, `usage limit`, `spend limit`, credit/billing phrases | `src/lh_harness/provider_errors.py:53-61` |
| Reset-time parser | `_parse_provider_reset` extracts `Retry-After`, `retry-after-ms`, "try again in 1m30s", `resets at <ISO>`, `reset_at=<epoch>`, Claude Code's `usage limit reached\|<epoch>`; clamped to a 7-day horizon | `src/lh_harness/launcher.py:385-451` (`_MAX_RESET_HORIZON_SECONDS = 7*24*3600` at `:407-408`) |
| Backoff schedule | `quota_backoff_minutes` default `[30, 90]`; **a parsed reset time later than the schedule wins** | `src/lh_harness/launcher.py:1607-1634` |
| Retry wiring | the successor entry gets `not_before` = backoff/reset and `wait_reason` = cause; emits `queue.requeued` with `not_before_utc` | `src/lh_harness/launcher.py:2515-2560` |

**Why this matters:** the harness *already* extracts "this is quota" and
"this is when it resets" from raw provider text. P4's auto-unblock loop can
reuse `_parse_provider_reset` verbatim. What is missing is that the result
only ever parks the *entry* — it is never recorded against the *account* (see
§2.4).

### 2.3 The only pre-launch health gate is key-count, not quota

`kimi` launches are gated by `key_health_url` returning
`{"healthy_keys": [{"healthy": true}, ...]}` with at least `min_healthy_keys`
(default 2) healthy (`src/lh_harness/launcher.py:1781-1784`, checker at
`:1845-1871`; documented `docs/queue.md:209-210`). This is binary health
(missing/unauthorized keys), not remaining quota. `docs/queue.md:318-319`
shows the intended URL is the LiteLLM health endpoint. No other trio has any
pre-launch provider check: glm/zai launch straight into whatever the router
picks.

### 2.4 What does not exist (gaps this design must add)

- **No per-account quota state anywhere.** Grep for a quota ledger, remaining
  tokens, or a per-account reset across `src/lh_harness/` finds nothing; the
  words "quota" in the tree refer to the entry-backoff path above. The ticket
  names a meter (`opsstatus ops_status_quota`); that is an MCP gateway tool,
  and the gateway is 403 to this run, so its shape is **unverified** — the
  design below treats the meter as a data source *candidate*, with the 429
  error text as the always-available fallback (and notes that any meter read
  used to seed numbers must be labeled "meter read 2026-11-20").
- **No spill.** A trio is fixed at enqueue; there is no lane fallback between
  trios anywhere in `queue.py` / `launcher.py` (the only "fallback" in the
  tree is bootstrap-config fallback semantics, `launcher.py:921`). The ticket
  mentions a "seat fallback pool" as a surface to audit; nothing by that name
  exists in this repo — the nearest real things are the per-pass overlay
  (§2.1) and the protected-trios write guard (`docs/queue.md:744-775`),
  which *allows* capacity edits on protected trios precisely because "caps
  are operational, not modelling choices". That sentence is effectively
  pre-authorization for quota-driven cap moves.
- **Enqueue preflight is model-liveness only.** R6 preflight validates a
  per-task `model` override against the live model list (all-or-nothing with
  an audit row) before dedup/insert: `src/lh_harness/queue.py:1652-1666`,
  `_validate_model_live` at `:803-822`, mirrored 1:1 in
  `src/lh_harness/pg_queue.py:480-515`. A quota preflight would slot into
  exactly this position — refuse at enqueue, before anything exists.

---

## 3. The subscriptions (DO-b) — inventory with sources, no credentials

No credential value appears or was read. Numbers are labeled **[docs]** when
verified from provider documentation this round (via sonar-pro/perplexity
through LiteLLM, citations in §4), **[ticket claim]** when they come from
Paxton's message, and **[meter]** when read from the quota meter (none were —
gateway 403; no number below is a meter read).

| Subscription | Count | Limit type | Numbers and their source |
|---|---|---|---|
| z.ai GLM Coding Max | 5 accounts (acct1..acct5) **[ticket claim]** | weekly (and a separate 5-hour window); error 1310 = weekly/monthly exhaustion, HTTP 429, response carries `next_flush_time` **[docs: z.ai error-code reference and DevPack FAQ]** | Prompt-window sizes per plan tier are on z.ai's plan pages; **not re-verified this round** (search returned no authoritative page for the Coding Max tier specifics) — treat all per-account token figures as **[ticket claim]** until read from the meter |
| Ollama Cloud | **ticket says 3; a repo inventory says 7. Both sources are stated here and the discrepancy is unresolved.** The repo's own historical note says the weekly quota "span[s] four accounts" (`docs/LEDGER.md:5398`, 2026-09 era) — so the count has changed over time and neither current figure is confirmed live | weekly ("you have reached your weekly usage limit"); no documented exact reset timestamp **[docs: Ollama 429 text seen verbatim in `docs/LEDGER.md:4241,5048,6703`; reset day not in official docs]** | No plan/token figures verified; the meter is the right source when reachable |
| synthetic.new Big (Kimi) | 1 **[ticket claim]** | **weekly credit quota — NOT monthly — per synthetic.new's own rate-limits page [docs]**; per-key API concurrency not publicly documented (a "5× concurrent infinite mode" plan feature exists but is not a general API cap) **[docs, explicit gap]** | Ticket claims 472M Kimi K3.1 Pro tokens/month after a +30% bump — **[ticket claim]**. synthetic.new's own pages conflict (one pricing view ~157M tokens/month for Kimi-K3.1-Pro, another ~120M) and describe the quota as weekly, so the 472M figure could not be confirmed from docs and must not be treated as fact until read from the meter |
| Local 3090 qwen/ornith lanes | 2 lanes (one per GPU), ptait01 | hard concurrency = 2 (`n_seq_max=1` per lane; `OLLAMA_NUM_PARALLEL` has no effect); no token quota (self-hosted) **[repo: `docs/local-lanes.md:1-16`]** | 24 GB VRAM per card, ctx proven 262144, harness budget 128k **[repo: `docs/local-lanes.md`]**; also carries prod/UAT router + embeddings traffic **[repo: `docs/handoffs/HANDOFF-qwen-lane-sizing-2026-10-01.md`]**

Two inventory lessons feed the design:

- **Limit types genuinely differ** (5h-window / weekly / monthly-credit /
  pure-concurrency), so one uniform "block for N minutes" cannot serve all
  rows; the reset *time*, not the limit *type*, is what the scheduler needs.
- **The local lanes are quota-infinite but concurrency-poor and contended**
  (shared with deploys' QA gates). They are the right *last* spill target,
  not a primary lane — which matches the ticket's spill order.

---

## 4. Alternatives considered (DO-c) — at least three, why / why not

Researched via sonar-pro:perplexity through the LiteLLM proxy (read-only
queries; the tool worked this round). Sources cited per alternative.

### A. LiteLLM `usage-based-routing-v2` (route by lowest observed usage)

**What it does:** keeps Redis-backed per-deployment TPM/RPM usage and steers
requests toward the least-used deployment, subject to configured deployment
limits. **[docs: litellm.ai/docs/routing]**

- **Why:** zero harness code; it is the router's own native
  load-spreading; deployments = accounts, so traffic naturally favors the
  account with headroom.
- **Why not (alone):** the signal is *proxy-observed recent usage*, not the
  provider's remaining quota. It does not know a subscription is dry until
  the provider errors, and z.ai weekly/Ollama weekly windows are far longer
  than a rolling TPM window. LiteLLM's own docs warn it adds latency and is
  not recommended for high-traffic production routing. Also requires shared
  Redis state. Fails P1 (it will still send the first request into an
  exhausted account) and P3 (still bounded by configured caps, not quota).
  **Verdict: useful as the router-side spreader behind the design's placement
  rule; not the allocator.**

### B. LiteLLM cooldowns (`allowed_fails` + `cooldown_time`) on 429

**What it does:** after qualifying failures (429 among them) a deployment is
temporarily removed from rotation and returns after `cooldown_time`.
**[docs: litellm.ai/docs/proxy/reliability; the repo already runs
`allowed_fails=50, cooldown_time=20` per `docs/LEDGER.md:9542` — a
per-minute-scale cooldown]**

- **Why:** pure router config, already proven in this fleet (the 2026-10-01
  "cooldown fix" stopped rate limits benching a model,
  `docs/LEDGER.md:5270`); requires nothing from the harness.
- **Why not (alone):** purely reactive — the discovering request has already
  failed (violates P1's spirit; P1 says *no run starts* on a known-exhausted
  account, and the first run always does). Cooldown duration ≠ provider reset
  time: a 20 s or even 60 min cooldown expires long before a weekly window
  resets, producing repeated 429-rediscovery cycles all week — exactly
  tonight's failure shape. Fails P4 (no reset-time awareness).
  **Verdict: keep as the safety net underneath the design; it is not the
  block/unblock loop.**

### C. Per-subscription token bucket (TPM/RPM limits on deployments)

**What it does:** LiteLLM refuses to select a deployment whose tracked
current-minute usage would exceed configured `tpm`/`rpm`. **[docs:
litellm.ai/docs/routing]**

- **Why:** the one native mechanism that can refuse *before* the request is
  sent — a true preflight, satisfying P1 within its own model.
- **Why not (alone):** TPM/RPM are *rate* limits, not *total-quota*
  accounting. A weekly 500M-token allowance cannot be expressed as any
  per-minute rate without either wasting capacity (set low) or 429ing (set
  high). The bucket refills every minute while the real window resets weekly.
  Getting the numbers right requires exactly the per-account quota knowledge
  the harness lacks. **Verdict: right shape, wrong time constant; use its
  idea (refuse-before-send) but with quota + reset-time as the state.**

### D. Tag routing to separate subscription pools

**What it does:** request tags select deployment pools (e.g. one tag per
subscription). **[docs: litellm.ai/docs routing/tag-based]**

- **Why:** clean partitioning; lets the harness say "this run goes to the
  kimi pool" without touching deployment lists; composes with any of A-C.
- **Why not (alone):** zero quota signal — tags are static classification.
  An exhausted account stays selected until something else removes it; P1-P4
  all fail. **Verdict: adopt as the *addressing* scheme (the harness names
  the pool; the pool maps to accounts), never as the allocator.**

### E. An external quota ledger + preflight scheduler (the general pattern)

**What it does:** the orchestrator keeps per-account
`(remaining, window_type, reset_at, blocked_until)`, refuses at dispatch when
insufficient, decrements on use, reconciles against provider headers, and
sets `blocked_until` from the provider-reported reset on quota errors.
Named products following this shape: OmniRoute (subscription-aware routing
for Claude Code/GLM with 5h/weekly quota tracking and reset views);
ShackleAI orchestrator (per-agent monthly budget, soft alert 80%, hard stop
100%). LiteLLM's own `max_budget`+`budget_duration` is the same idea bounded
to proxy-tracked spend, with documented auto-reset at day/Monday/month
boundaries (configurable `budget_reset_time`/timezone) — but it resets on
*its own configured period*, not on the upstream provider's stated reset.
**[docs: litellm.ai budgets; product pages via research]**

- **Why:** it is the only pattern that satisfies P1 (pre-first-request
  refusal) and P4 (unblock at the *stated* reset time) simultaneously, and
  it degrades gracefully — when the ledger is stale it falls back to
  error-driven learning, i.e. alternative B's behavior, then corrects.
- **Why not (the honest cost):** it is new state the harness must own and
  keep truthful; it is only as good as its quota data source; and it needs
  write access to the router's block list (the ticket's scoped
  `infraops1-model-block` key) to enforce across *non-harness* traffic too.
  **Verdict: this is the design (§5).**

### Summary

| Alt | P1 no-start-on-exhausted | P2 spill | P3 quota-following caps | P4 reset-time unblock |
|---|---|---|---|---|
| A usage-based-routing-v2 | ✗ | partial | ✗ | ✗ |
| B cooldowns | ✗ (reactive) | partial | ✗ | ✗ (duration ≠ reset) |
| C token bucket | ✓ (rate only) | ✗ | ✗ (wrong window) | ✗ |
| D tag routing | ✗ | ✓ (manual) | ✗ | ✗ |
| **E ledger + preflight** | **✓** | **✓** | **✓** | **✓** |

---

## 5. The proposed design (DO-d) — exactly one

**One sentence:** keep LiteLLM as the enforcement layer, and give the harness
launcher a small per-account quota ledger that (a) produces each lane's
effective cap from remaining quota and provider concurrency instead of a
hand-typed integer, (b) refuses to launch on any account whose window is
known exhausted, (c) re-points *new* work down a fixed spill order, and (d)
blocks/unblocks router rows on weekly/monthly 429s at the reset time the
error itself states.

### 5.1 Data source for remaining quota (layered, honestly)

No single source is authoritative, so the ledger takes a falling order:

1. **The quota meter** (`opsstatus ops_status_quota`, an MCP gateway tool).
   *Preferred when reachable.* This run could not reach it (gateway 403) and
   its payload shape is unverified; before rollout someone with gateway
   access must confirm it exposes, per subscription: remaining tokens (or
   spend), window type, and reset time. Any number it contributes must be
   stamped with its read time (e.g. "meter read 2026-11-20").
2. **The 429 error text itself** — always available, and already parsed:
   `_parse_provider_reset` (`src/lh_harness/launcher.py:413-451`) extracts
   the reset time; the z.ai 1310 body carries `next_flush_time` and Ollama's
   429 states the weekly limit. Error-derived state is *authoritative for
   blocking* (the provider just said so) even though it says nothing about
   *how much* headroom other accounts have.
3. **Configured allowance − proxy-tracked usage** as the estimator between
   resets: LiteLLM sees every token (it is the only egress), so
   spend-since-window-start from the router's usage tables approximates
   remaining for window-limited plans. Used only to *rank* lanes, never to
   block (blocking requires source 2's certainty).

The ledger is a new small table in the **existing settings store**
(`src/lh_harness/settings_store.py` already owns runtime-mutable operational
state with audit rows; a sibling `quota_account` table follows its
`queue_capacity` precedent at `:354-358`). Columns: account id, provider,
window type, allowance (nullable), remaining estimate, reset_at, blocked
until, last error digest, last source (meter/error/estimate). No
credentials — the store's existing secret discipline
(`docs/settings-store.md:9-14`) already keeps key values out of every API.

### 5.2 Placement rule (replaces the hand-typed cap)

Per scheduling pass, for each lane the launcher computes:

```
effective_cap(lane) = min(
    provider_concurrency_limit(lane),      # hard: 2 local, meter/docs per cloud plan
    floor(remaining_quota(lane) / per_run_budget),  # how many whole runs still fit
    configured_max(lane),                  # stays as the safety ceiling (never removed)
    max_peak(lane) if in_peak_window,      # unchanged: launcher.py:893-905
)
```

- `configured_max` **stays in the min** on purpose: quota raises the cap when
  there is headroom, never *above* what an operator judged safe for the node
  (the `node_max_runs` ceiling at `src/lh_harness/config.py:258-264` also
  still applies). This answers P3 — caps follow quota and provider limits —
  without deleting the human safety rail.
- `per_run_budget` comes from measured history, not a guess: the repo already
  has this measurement habit (`docs/handoffs/HANDOFF-qwen-lane-sizing-2026-10-01.md`
  records per-task token/latency outcomes). A rolling p90 of completed-run
  token usage per trio seeds it.
- **P1 refusal:** an account with `reset_at > now` (window exhausted) yields
  `effective_cap = 0` for every lane mapping to it — the entry then waits
  with a reason like `kimi at capacity (quota: resets 2026-11-24T00:00Z)`,
  produced by the existing `_capacity_reason` shape
  (`src/lh_harness/launcher.py:1636-1639`). Nothing launches. No 429 is
  needed to learn this a second time.

This lands *inside* the existing per-pass overlay
(`src/lh_harness/launcher.py:914-936`): the overlay already re-merges
capacity every pass; the only change is that the value it merges is computed
(§5.2) instead of read verbatim. Hot-reload, restart-free behavior, and the
protected-trios guard all carry over unchanged — and
`docs/queue.md:748-750` already classifies cap moves as operational (no SSO
override needed), which is what makes runtime quota-driven caps permissible
at all.

### 5.3 Spill order (P2)

Static, declarative, per trio — matching the ticket's stated order:

```
kimi (synthetic.new Big) → zai acct1 (z.ai, freshest window) → local qwen/ornith (2 lanes)
```

- Spill applies to **new entries only**; a running run keeps its trio
  (existing binding semantics, `launcher.py:921` docstring: "A run keeps the
  trio resolved at its launch"). No mid-run model swap, no corrupted
  episodes.
- Mechanics: when a trio's effective cap is 0 for a quota reason, the
  launcher rewrites the *pending entry's* trio to the next lane in the order
  with headroom, recording the original trio and reason in the entry (the
  same audit-row discipline the settings store uses). If every lane is 0,
  the entry stays pending with the aggregate reset time — visible, not lost.
- The order is config (`[queue] spill_order = ["kimi","zai","qwen"]`),
  editable per node, because CT110 and CT111 differ in what trios they
  define (a trio only exists where `config.toml` defines it,
  `docs/queue.md:661-697`).
- Local lanes stay last: they are quota-free but concurrency-2 and shared
  with deploy QA gates (`docs/local-lanes.md`, lane-contention evidence in
  the 2026-10-01 handoff). Spilling there is the "work continues, slower"
  mode, not the default.

### 5.4 Auto block/unblock loop (P4) — the `infraops1-model-block` scoped key

The ticket specifies the scoped key `infraops1-model-block`, now
`proxy_admin` with `/model/block` and `/model/unblock` **only** (ticket
statement; the key's live scopes were not verifiable this round — gateway
403 — and this is flagged as such). The loop:

**Block (on weekly/monthly exhaustion):**
1. A run's failure text is classified (existing `_classify_failure_cause`,
   `launcher.py:341-372`) as `provider_quota` **and** matches a
   weekly/monthly signature: z.ai `[1310]` / `next_flush_time`, or Ollama
   `weekly usage limit`. Short-window causes (5h flush, plain 429) do *not*
   block — they already have the entry-backoff path.
2. `_parse_provider_reset` (`launcher.py:413-451`) extracts the reset time
   from the same text. If no time is stated (Ollama's text states none),
   fall back to a configured weekly anchor per provider (e.g. rolling
   7×24 h from the block, conservative) and mark the row
   `reset_time_inferred`.
3. The launcher (or a small sidecar with the scoped key, keeping the key out
   of the launcher process) calls `POST /model/block` on the router rows
   serving that account, and writes `blocked_until = reset_at` into the
   ledger. Blocking at the router (not just in the harness) also protects
   non-harness traffic from re-discovering the exhaustion.
4. Every affected lane's effective cap recomputes to 0 on the next pass
   (§5.2) — so P1 holds for *harness* traffic even if the router call fails;
   the router block is defense-in-depth, and its failure is logged, not
   fatal.

**Unblock (at the stated reset time):**
1. The pass checks `blocked_until <= now` rows.
2. Calls `POST /model/unblock` for those rows, flips the ledger row back,
   and — before trusting the account — sends **one cheap probe request**
   (single completion, minimal tokens) through the lane. If the probe 429s
   again, re-block with the *new* stated reset time (handles providers
   whose stated time is optimistic). The probe is deliberately tiny: never
   a harness run (P1), never a real task.

**Guardrails:** block/unblock only ever uses the scoped `/model/block` +
`/model/unblock` surface — the key cannot edit models, config, or keys, so
the loop cannot damage the router even if its logic is wrong. All block and
unblock actions land in the settings-store audit log (actor, time, row,
reason, source error digest — never a credential).

### 5.5 What changes where (files, no code written in this spike)

| Change | Repo / file | Nature |
|---|---|---|
| Ledger table + reader/writer + audit rows | LongHorizon-Harness `src/lh_harness/settings_store.py` (sibling of `queue_capacity`, `:354-358` precedent) | new table, ~1 screen |
| Quota-source adapters: meter client, error-ingest hook, usage-estimate reader | LongHorizon-Harness `src/lh_harness/` (new small module, e.g. `quota_ledger.py`) | new |
| Effective-cap computation + quota-aware capacity reason | LongHorizon-Harness `src/lh_harness/launcher.py` (inside `_refresh_queue_config`/pass, `:914-936`, reason via `_capacity_reason` `:1636-1639`) | modify (the only behavioral change to scheduling) |
| Weekly/monthly signature set + block/unblock calls | LongHorizon-Harness `src/lh_harness/launcher.py` near `_QUOTA_CAUSE_SIGNATURES` (`:325-332`) and `_handle_retry` (`:2515-2560`) | extend |
| Spill order config + entry re-point | LongHorizon-Harness `src/lh_harness/config.py` (key set at `:242-254`) + `launcher.py` pass | extend |
| Router block/unblock sidecar (holds the scoped key) | LongHorizon-Harness, small new entry point reusing the settings store's secret discipline | new |
| Docs: capacity semantics, spill order, block/unblock runbook | LongHorizon-Harness `docs/queue.md` (capacity table at `:202-212`, retry section at `:214-233`) | extend |
| **Nothing** in LiteLLM config | `litellm-config.yaml` frozen per rules; the design needs only the existing `/model/block`,`/model/unblock` admin endpoints and usage tables | none |

Deliberately **not** changed: trio model bindings (protected trios,
`docs/queue.md:744-775`), `node_max_runs`, the retry/backoff schedule for
short-window causes, and the enqueue API surface (a later phase may add
quota to the R6 preflight at `queue.py:1652-1666`; not required for P1-P4).

### 5.6 Rollout steps

1. **Shadow mode.** Ledger + effective-cap computation run, results logged
   (`would-cap: kimi 2→2, glm 1→0 (resets 2026-11-24T00:00Z)`), no behavior
   change. Run for ≥1 weekly exhaustion cycle. This alone produces the
   meter-shape verification and per-run token p90s the later steps consume.
2. **Reason-only mode.** Quota-exhausted lanes report the quota reason in
   pending entries (replacing the bare "at capacity" text) but still
   schedule per old caps. Operators see truth; behavior identical.
3. **Cap mode.** effective_cap governs (P1, P3). Roll out per trio, kimi
   first (it has the headroom tonight), then glm/zai (tonight's dead pool),
   qwen last (local, concurrency-bound).
4. **Spill mode.** Enable spill re-pointing per node (P2). CT111 first (fewer
   live runs), then CT110.
5. **Block/unblock mode.** Enable the router loop with the scoped key (P4),
   starting block-only, then unblock-with-probe after one observed clean
   weekly reset.
6. Each step is independently revertible by a config key (the overlay's
   hot-reload means a revert is one DB row, no restart —
   `launcher.py:914-936`).

### 5.7 Test plan

- **Unit:** effective-cap arithmetic (quota 0 → cap 0; allowance null →
  configured_max; peak window interplay with quota cap); signature matching
  (1310 with/without `next_flush_time`; Ollama weekly text; a 5h-flush text
  that must NOT block); reset parsing reuse (already-tested regexes,
  `launcher.py:385-411`); spill order resolution with a missing trio on the
  node.
- **Integration (fake router):** a stub LiteLLM exposing
  `/model/block`,`/model/unblock`,health,usage — verify block-on-429,
  unblock-at-reset, probe-fails→re-block-with-new-time, and that a router
  outage degrades to harness-side cap 0 (P1 still holds).
- **Soak (shadow):** one full week in shadow mode against live traffic;
  compare every shadow decision against what actually happened; success =
  zero cases where shadow said "0 (exhausted)" and a real run then launched
  and succeeded on that account, and zero unblocks that immediately 429.
- **Failure injection:** kill the meter mid-pass (ledger falls to
  error-derived state); corrupt a reset timestamp (7-day horizon clamp
  already protects, `launcher.py:407-408`); concurrent block+admin manual
  unblock (audit row wins, last-write recorded).
- **Tonight-as-test (see §6):** replay the round-1 failure texts through the
  classifier and assert every GLM-pool account blocks with reset times, kimi
  cap stays ≥1, and the spill order moves the queue.

---

## 6. Tonight as the worked example (DO-e) — labeled estimates throughout

**What the ticket reports (narrative, not independently verified):** on
2026-11-20 ~02:00Z every harness run died in round 1; the GLM pool (3 Ollama
Cloud accounts + z.ai accts 4/5) was weekly-exhausted; the Kimi lane sat
mostly idle behind launcher caps kimi 2 / local 1; Kimi's plan is claimed at
472M Kimi K3.1 Pro tokens/month (+30% bump). Tonight's runtime caps are the
ticket's description — the repo's shipped defaults (kimi 3, qwen 1, glm 1,
zai 5; `src/lh_harness/config.py:851-861`) differ, and live runtime values
were not readable this round.

**What the proposed design would have done, step by step (estimate):**

| Moment | Old behavior | New behavior |
|---|---|---|
| First GLM 429 of the night (`[1310]` + `next_flush_time`, or Ollama weekly text) | entry parks 30/90 min; account stays in rotation; next entry re-discovers the wall | account row blocks with the stated reset; router row blocked; glm/zai effective caps → 0 with reason "quota: resets <time>" — no further runs *start* on it (P1, P4) |
| Second and later entries targeting GLM lanes | launch → die in round 1 → retry → die (the night's pattern) | never launch onto the dead pool; instead spill (P2) |
| Kimi lane all night | cap kimi 2 regardless of the claimed 472M/month sitting unused | effective cap = min(configured ceiling, provider concurrency, floor(remaining / p90-run-budget)) — with the ticket's claimed allowance the binding constraint would be concurrency, not quota; the lane runs at its real limit (P3) |
| Local qwen/ornith | idle-adjacent while GLM entries queued behind dead caps | final spill target absorbs what kimi+acct1 cannot carry, bounded by hard concurrency 2 (`docs/local-lanes.md:9-12`) |
| z.ai acct1 (not exhausted per ticket) | only reachable if an entry happened to be enqueued as `zai` | second spill hop; its cap likewise quota-derived |
| Weekly reset (~2026-11-24, per a 7-day rolling window) | nothing happens automatically; first run re-discovers availability by succeeding | unblock at stated time + one tiny probe before re-admitting (P4) |

**Effect estimate (explicitly an estimate, from the ticket narrative):** the
night's total loss was every run × round-1 wall clock + retry churn. Under
the design, runs that would have died in round 1 on the exhausted GLM pool
instead start on kimi/acct1/local within one pass. If even the majority of
the night's queue was lane-agnostic dev work (the repo's own sizing note
says design/research tasks belong on cloud lanes and only QA/small edits on
local — `docs/handoffs/HANDOFF-qwen-lane-sizing-2026-10-01.md:27-33`),
most of the night's throughput is recoverable at reduced speed, with local
lanes as the bounded last resort. No precise percentage is claimed: the
actual recovered fraction depends on live quota numbers and per-run budgets
that only the shadow-mode soak (§5.6 step 1) can measure.

**Cost of being wrong:** the design's failure modes are capped — a too-
pessimistic ledger idles a lane that had headroom (lost speed, no lost
work); the configured_max ceiling and node_max_runs still bound everything;
and the scoped key cannot mutate anything beyond block/unblock. The night's
failure mode (runs dying on a known-dead pool) is exactly the one this
removes.

---

## 7. Open questions for the manager / Paxton

1. **Meter shape** (blocking for rollout, not for the design): confirm
   `ops_status_quota` exposes per-subscription remaining + reset; this run
   could not reach the gateway (403).
2. **The 472M figure and the Ollama 3-vs-7 count** both need one meter read
   or a docs link before any number in §3 hardens from claim to fact.
3. **Scoped-key placement**: sidecar process vs launcher-embedded — the
   sidecar keeps the key out of the run-launching process entirely; Paxton's
   call.
4. **Spill's task-shape gate**: should spill refuse to send GUI-shaped or
   very large tasks to local lanes (per the sizing rubric) rather than
   degrading them? Recommend yes; needs a decision.
5. **Dedicated ticket**: this work needs its own ticket; easybutt0n-17 is a
   borrowed label (restated from the top of this report).

---

*Research queries for §3-§4 were run through sonar-pro:perplexity via the
LiteLLM proxy on 2026-11-20; repo citations re-verified against `03704b2`
the same day. No credential was read, used, or printed; no host outside the
workspace was accessed; `litellm-config.yaml` was not touched.*
