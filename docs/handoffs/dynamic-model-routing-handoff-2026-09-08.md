# Handoff: dynamic per-run model routing across local and cloud providers

## Handoff header

| Field | Value |
|---|---|
| Written | 2026-09-08 by the overseer session `5321285c-35e2-459a-9dae-92ea8811669f` |
| For | Whoever designs the permanent solution; a harness task will implement it |
| Question in one line | At the moment a run is created, how should the orchestrator choose a model per role from what is actually available locally and across two cloud providers, and record that choice so it can be audited and re-derived? |
| **Repo that owns it** | **`LongHorizon-Harness`** — the queue, the service-side launcher and the run contract all live here (`src/lh_harness/queue.py`, `launcher.py`, `webapi/server.py`, `mcp_tools.py`). Model *definitions* live in `cognizioware-mcp-tools` (`infrastructure/litellm-config.yaml`), so a second, smaller change lands there; the harness must not hardcode provider URLs. |
| Router | LiteLLM at `https://litellm.easybutt0n.ai` (prod CT202, `192.168.21.161:4000`); every harness agent's `ANTHROPIC_BASE_URL` points at it |
| Today's state | Role models are a **hardcoded dict in the overseer's own launcher script** on the PC, changed by hand three times in one day |

## Why this exists

On 2026-09-08 model availability changed four times in a few hours and each change was absorbed by a human editing a script:

1. Six of seven Ollama keys hit their **rate** limit, so the cloud trio could not start and the queue stalled at one healthy key.
2. A local fallback was added by hand (`qwen3.8`), which worked but is one-run-at-a-time.
3. The auditor's model then hit a **monthly** cap — a different failure from a rate limit — and every new run died in under a second with `provider_provider_error` before producing a single round. Three runs were lost before it was traced, because a run that fails before round zero records no reason.
4. A new provider (Synthetic) was added, registered on the live router by hand, and the roles were re-pointed at it.

Each step was correct and each was manual. The permanent answer is for the orchestrator to make this decision itself, per run, from observed availability.

## What "available" actually means — the four signals, and their traps

- **Local (Ollama on the span pair, `192.168.21.110:11442`)** — `GET /api/ps` shows what is *loaded* (today `qwen3.8:27b`, 19.3 GB across two 3090s); `OLLAMA_NUM_PARALLEL=2` caps concurrency; `OLLAMA_CONTEXT_LENGTH=65536` caps the window. A model that is *pullable* but not *loaded* has a cold-start cost that matters for a role called dozens of times per round.
- **Ollama cloud** — failures are **not one thing**. A per-key rate limit clears within the hour; a **monthly account cap does not clear until the month rolls**. Treating them the same is what kept the overseer's watcher probing keys that could never recover. The error bodies differ and are machine-distinguishable.
- **Synthetic** — subscription packs, each 1000 requests / 5 hours and **2 concurrent per model**. Two packs are held; whether they add is unverified. Its `/models` endpoint lists what is servable.
- **The router itself** — `/v1/models` is the authority on what names resolve, and a name can resolve while its backing provider refuses. Availability must be proven by a *cheap completion*, not by presence in a list. `/health/liveliness` and `/health/readiness` are the cheap probes; a bare `/health` runs a completion against **every** configured backend and costs real money, so it is forbidden.

## What the roles actually need (measured today, one realistic question, through the router)

| model | latency | window | note |
|---|---|---|---|
| `qwen3.8-nothink` (local) | 1.3 s | 64k | fastest by far; tool-heavy work |
| `qwen3.8` (local) | 5.6 s | 64k | thinking variant |
| `glm-5.3-flash:synthetic` | 7.6 s | 512k | |
| `kimi-k3:synthetic` | 8.7 s | 512k | most concise answer |
| `glm-5.2:synthetic` | 13.1 s | 512k | most reasoning tokens |

The split in force right now, chosen by hand: manager `glm-5.2:synthetic`, executor **local** `qwen3.8-nothink`, auditor `kimi-k3:synthetic`, run concurrency 3.

The reasoning behind it, which the design should either encode or overturn:

- The **executor** makes the most calls per round and is mostly tool-driving, so it belongs on the cheapest fast model — and putting it local conserves the cloud request budget for judgement. Its cost is a **64k window**, which becomes a hard constraint on task size.
- The **manager** plans once per round and benefits most from depth and a large window.
- The **auditor** reads a diff and must be rigorous but concise; a large window matters when the diff is large.

## What we want designed

1. **A routing decision made at enqueue and re-checked at launch.** A queue entry should carry the *intent* (role requirements) and the launcher should bind the *actual* models when the run starts, because availability moves between enqueue and launch. Say where each half belongs and what happens when they disagree.
2. **A capability/requirement vocabulary.** Roles should ask for what they need — context window, tool-calling quality, latency class, cost class, locality — rather than naming a model. Define the smallest vocabulary that expresses today's split without becoming a scheduler DSL.
3. **An availability model with the right memory.** Rate limit, monthly cap, cold model, saturated concurrency and hard auth failure are different states with different recovery times. Define the states, how each is detected (cheaply), how long each is trusted, and what a probe costs.
4. **Concurrency accounting.** Local parallelism is 2; Synthetic is 2 concurrent per model per pack. The current answer is a global run cap, which is blunt: it throttles work that would not contend. Propose accounting that is per-backend, not global.
5. **Recorded provenance and re-derivability.** Every run must record which models it used per role and **why** that route was chosen, in a form the fleet surfaces can show and a person can quote. It should be possible to ask "why did this run use that model" a week later.
6. **Degradation, not stalling.** When nothing ideal is available the run should still start on something adequate, saying so, rather than the queue silently head-blocking. Define the ladder and the point at which it is honest to refuse.
7. **The task-size feedback loop.** A local executor's 64k window means a task text plus repo reading plus round history must fit. Should the router reject/route-away a task whose text exceeds a threshold, warn at enqueue, or split it? The overseer's current script refuses local fallback above ~24k characters of task text — crude, and it belongs in the design.

## Constraints that are not negotiable

- **No provider URLs or keys in the harness.** Model names resolve through the router; keys live in env files on the router boxes.
- **Never a bare `/health` against the gateway.** Liveness, readiness or a single named model only.
- **Availability is proven by behaviour, not by configuration.** A name in `/v1/models` is not availability.
- **A run that cannot start must say why**, in its own record. Today a pre-round failure leaves an empty run and a silent queue, which is how three runs were lost before anyone noticed.
- **Warn, do not block, on anything ambiguous** — consistent with the working-tree contention work already queued.

## Open questions we do not have a settled answer to

- Should routing be per-run or per-round? A long run may outlive the availability that chose it.
- Is a failed round on a degraded model worth a retry on a better one, or does that double-spend the budget that was scarce in the first place?
- Should the orchestrator ever *pull* a local model it wants, or only use what is already loaded?
- How should a human override a route for one run without editing code — and should that override survive a resume?

## Evidence to rely on

- Ollama monthly cap error body names the account and points at an upgrade URL; the rate-limit body is different — both were captured today.
- `provider_provider_error` at `[cli_executor] failed · 0.6s` is the signature of a role whose model is refusing before any work happens.
- The four Synthetic models registered on the live router today: `glm-5.3-flash:synthetic`, `glm-5.2:synthetic`, `kimi-k3:synthetic`, `qwen3.8-27b:synthetic`.
- `docs/queue.md` describes the queue contract the routing intent must fit into.

## Decisions (2026-09-08) — binding

The four open questions above are settled. They are no longer open; the implementing task must build them and may only disagree in its completion report, with evidence.

1. **Route per run, observe per round.** The worker command and the adapters are built once, and both resume paths re-read the owner's role configs, so a mid-run switch has no support in the code. Per-round failures are appended to the route history and re-routing happens at the next bind — a new run, or a resume. A transient state is waited out inside the episode rather than routed around.
2. **No retry of a round that already worked, on a better model; bounded relaunch for a refusal *before* round zero** (revised by Paxton in the 2026-09-08 interview, replacing "exactly one re-enqueue"). When no round has run and the abort reason is a provider rate limit, quota, authentication failure, unavailable model, or the new not-permitted state, the launcher marks the offending model in availability, fails the original entry with the reason recorded, and creates a new pending entry that records what it is a retry of and which attempt it is. Every attempt must bind a route that differs from every earlier attempt for the failing role, or wait until an earlier state's trust window has expired; when neither is possible the entry is refused naming everything tried. The cap is configurable and defaults to three attempts (the original plus two). A run that produced rounds is never restarted — that double-spends the budget that was scarce in the first place, and it hides whether the task or the model failed.
3. **Never pull a model.** Pulling is a host-level change, and the standing rule is that no host change happens without an explicit human go. A cold model stays a legal candidate at the adequate tier with a warning, and the rationale names it so a person can decide to load it.
4. **An override is a field on the queue entry, never code.** It is set through the API, the Hydra panel or the chat tool, carries who set it and why, is validated against the key-scoped catalogue, and is honoured even when availability disagrees — recorded at the override tier with the disagreement in the rationale. It survives a resume: continue copies the owner minus the resume-cleared keys, which must not include the route, and retry passes the role configs through while also accepting newly supplied ones.

### Strategy: one queue, two entry points, no duplicated surfaces

- **The decision point is the queue.** The route is provisional at enqueue and bound at launch, and both are recorded. Every entry point — web UX, Hydra panel, MCP tool, chat — writes a queue entry, and only the launcher creates runs.
- **The catalogue is the router, fetched with the harness's own key.** The router's model-info endpoint returns window, tool-calling, reasoning and cost, and because the key is scoped it also returns exactly the models that key may use. Drawing candidates from that response makes the 403 class that killed three runs today impossible by construction. A small harness-side overlay supplies only what the router cannot say: backend, latency class, locality, and per-backend concurrency. No third registry — the tools repo already owns the e2e and admin registry.
- **Availability memory lives in the harness**, under the runs root, with a state per model and a different trust window per state: healthy, rate limited, quota exhausted until the month rolls, authentication failed, not permitted until the catalogue changes, cold, saturated, and unknown-but-usable.
- **Provenance rides the existing reporter.** The route goes into the run owner, the public owner projection, the provenance field list and the starting status, so the heartbeat and the run-status wrapper carry it with no fleet schema change. The fleet window stays read-only; Hydra is the only write surface.

### The pre-round plumbing fix, and why it is required here

Three runs died today with no reason recorded anywhere, and a person had to trace it. That diagnosis gap is the reason this work exists, so the fix belongs in this task rather than a separate one:

- the launcher must build a failure reason from the run status or the report and put it, the abort reason, the rounds run and the route on the queue-failed event;
- the run-event emitter must also post the record to the fleet reporter, best-effort;
- the starting status must carry the route;
- the heartbeat's queue length must read the queue store's pending count;
- a worker that exits non-zero **before** writing a report must have the last 2 KB of its worker log recorded as the failure reason. That is today's four "worker exited with status 2" failures, which currently leave nothing behind at all.

### Ops step already completed

The harness virtual key's allow-list has been widened from 28 to 33 models, adding the local no-think model and the four Synthetic names, and all three role models were verified to return healthy through that key. The catalogue-scoped candidate rule above is what stops the class returning.

### Failure evidence to build fixtures from

Captured on 2026-09-08: a 403 naming the model the key could not access, on three runs; four workers exiting with status 2 at 14:04 with no recorded reason; and an upstream 500 relayed to the client as a 400 on one run. None of these is a quota body. The two real quota bodies are not in the repo — the monthly-cap wording names the account and points at an upgrade URL, and the rate-limit wording is distinct. Per the interview, the overseer captures both fresh with one 1-token probe per capped provider from the router box and pastes them, account id redacted, under "Captured provider bodies" below before task 49 launches; until that paragraph exists the monthly-cap fixture is reconstructed and must be marked as such.

### Router cooldown is not a provider cap (found 2026-09-08 14:00 PT)

Ten zero-round deaths traced to the router itself: it benched a deployment for 300 seconds after two failures, a rate limit counted as a failure, so a momentary limit became a five-minute outage while the provider's budget read fully unused. The cooldown is now 20 seconds and rate limits no longer trigger it. For the availability model this is its own short transient state: trust a router "cooldown" or "no deployments available" body for the cooldown length only, never classify it as quota exhausted, and say "router cooldown" in the rationale.

### Captured provider bodies

_Pending: to be pasted by the overseer before task 49 launches. Redact the account identifier to `<account>`; keep the rest verbatim, including the upgrade URL and any Retry-After header._

### Interfaces other tasks read

Defined once here; tasks 45 (fleet window), 46 (Hydra panel) and 16a (MSCE experience layer) are told to read these shapes and must not re-key them.

- **Runs** carry `route` in the run summary and the snapshot: `route.bound.roles.{manager,executor,auditor}.{model, backend, tier, rationale}`, plus `route.provisional` and `route.override`. `tier` is one of ideal, adequate, degraded, override, refused. An absent field renders nothing, never "unknown".
- **Queue rows** carry the same `route`; a pending row shows `provisional`, a launched row shows `bound`. A failed row's `reason` is the run's failure reason verbatim. A refused row's per-role tier is `refused` and the rationale lists everything that was tried.
- **The write path** for a human or the chat agent is one endpoint on pending entries that pins one role to a model and requires `by` and `reason`; the Hydra panel and the chat tool both call it, and the fleet window never does.
- **The experience trace** (16a) reads `owner.route.bound.roles` for the model per role, falls back to `owner.role_configs`, and carries `route_tier` and a redacted `route_rationale` per role. The seeded environment level lists routing backends by name only.

### Interview outcomes (Paxton, 2026-09-08, planning session `3e1c0873-c857-479c-bdf5-a4e3a3fca3b6`)

| Decision | Answer |
|---|---|
| Base branch | `origin/main` (PR #3 merged 2026-09-08 18:33Z) |
| Pre-round refusal | Bounded relaunch, default three attempts, different route per attempt |
| Chat key scope | One `harness-ops` virtual key with `fleet-runners`, `rsi-loop` and `hive-mind`, so chat.easybutt0n.ai can enqueue and route, read ops status and QA runs, and recall memory |
| Quota fixtures | Captured fresh by probe, pasted above |
| Human write surface | Hydra only; fleet.easybutt0n.ai stays read-only |
| Queue order on this workspace | 48 → 49 → 16a → 18 |

### Coverage of the overseer control-plane plan

The Hydra control-plane plan (`based-on-this-tasks-template-overseer-hi-fancy-fountain.md`, work items 1 to 8) planned placement, a "top-level model required" invariant, capacity routing, an executor-seat swap on migrate, an activity feed with rationale, chat fleet access and a template rewrite. Placement stays Hydra's decision (node); the model is the queue's decision (route); both are recorded on the entry. `create_fleet_run` becomes an enqueue and no longer bypasses the queue; the invariant becomes "a run needs a top-level model or a route bound by the queue". The migrate path's executor swap becomes a re-bind on the successor entry, linked through the route history. Overrides land in the same activity feed with `by` and `reason`. Chat access is the key scope above. Lifecycle vocabulary, atomic workspace reservation and confirm-exit-before-migrate remain task 14d's. The TRMS evaluation program is out of scope: a product evaluation run is not a harness run and never takes a route.

### Collisions and housekeeping

- Task 48 (working-tree contention) edits `launcher.py` on its own branch; keep launcher edits narrow.
- PR #112 in the tools repo owns the e2e and admin model registry; do not duplicate it.
- `docs/queue.md` (config example) and `src/lh_harness/config.py` (template) both point `key_health_url` at the forbidden bare `/health`; slice 8 fixes both.
- The PC-side launcher script carries a literal bearer token for CT110; rotate that token when the script is retired after the CT110 release.
