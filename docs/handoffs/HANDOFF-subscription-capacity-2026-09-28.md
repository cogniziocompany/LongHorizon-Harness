Written for: an expert, human or another Claude session, who has not seen this conversation and will explore solutions with you.

---

# Handoff: managing our included subscription capacity (Synthetic + Ollama Cloud) across the harness and the chat overseer

**From:** primary overseer session `[909876]` (Claude Code on ptait09), 2026-09-28.
**Goal of the conversation:** agree how we should use the model capacity we already pay for, the Synthetic pack plus the Ollama Cloud accounts, so that the harness runs and the chat.easybutt0n.ai overseer tick both stay alive, *before* anything more gets built.

## 1. The problem, as measured today
- **The chat overseer** is an Open WebUI automation (`6aef9800…`) that runs every 12 minutes on model preset `overseer-sweep`. It wrote only **7 of about 60 expected tick rows** in 12 hours.
  - **Why:** its model, `kimi-k3:synthetic`, returned `429 "You've exceeded your subscription rate limits"` on every tick from 08:00 to 13:30Z. That was while harness runs were using the same Synthetic pack.
  - **When it does run, it works:** 3 correct gate resolutions and 2 correct merge-ready flags.
- **The Ollama Cloud pool:** `kimi-k3:pool` returned `429 "you (aidev02cognizio) have reached your monthly usage limit"`. LiteLLM then logged `No fallback model group found for original model_group=kimi-k3:pool`. So one exhausted account sinks the pool, and there's no cross-provider failover.
- **The harness:** every run uses `kimi-k3:synthetic-anthropic` for all three roles, so it competes with the chat for the same pack.
- **Known Synthetic limits** (from overseer memory; verify): about 2,500 requests per 5 hours, *and* a weekly dollar credit. The weekly credit is the scarce one, and exhausting it means a multi-day outage.

## 2. What already exists
- **ops.easybutt0n.ai:** Paxton says included subscription capacity **has been added there**.
  - **Not yet verified by me.** It isn't in `cognizioware-mcp-tools` main under `ops-control-center`; the only match is a docs link to Synthetic's `/quotas` API in `AdminCentres.razor`.
  - The live ops container was rebuilt 2026-09-28 13:43Z.
  - **First thing to check:** where the capacity feature lives (repo or branch), what it measures, and whether it has an API.
- **`ops-status-mcp`** is a container on CT202, an MCP server over ops status. It's a candidate channel for overseers to *read* capacity. Its tool list hasn't been checked yet.
- **Task 84, the provider-quota surface on ops:** merged, display only.
- **Task 146 (mcp-tools #149, a Synthetic quota check as three measured signals):** still **open**. Its conflict fix is task 267, pending at priority 50.
- **The old quota watcher** (`C:/tmp/quota_resume*.py`, the PC launcher's gate) was **retired** at the CT110 cutover on 2026-09-23. Nothing replaced it.
- **The doctrine** (`C:/tmp/queue/LOOP-PROMPT.md`, STEP 5) requires a live quota watcher and says Synthetic meters requests and dollars, never tokens.

## 3. Already queued (may overlap; decide before it runs)
- **Task 282** (`q-59de9bd8cdd04eb6`, priority 150, mcp-tools): a new 5-minute monitor for Synthetic and the Ollama accounts. It publishes to the ops overview, a JSON endpoint and an MCP tool, and adds router failover between synthetic and pool with cooldowns, applied by a database script.
  - **It may duplicate what ops now has.** If the ops capacity feature covers monitoring, 282 should shrink to *consume* it plus add failover. Paxton can hold or re-scope 282 before it launches. It's behind 275 in the mcp-tools queue.
- **OPEN-ASKS row `ticker-model-spend`:** a paid model for the chat ticker, now **deferred** behind capacity management.

## 4. Questions to explore with the expert
1. **What the ops capacity feature gives us.** Is it the live signal (requests per 5 hours, weekly credit, per-Ollama-account status), how fresh is it, and can a machine read it through an API or `ops-status-mcp`?
2. **Allocation policy.** How should the included capacity be split between the harness (heavy, bursty) and the overseer tick (light, must never starve)?
   - Options: a reserved slice for the overseer, per-key rate limits or budgets in LiteLLM, or time-of-day windows.
3. **Routing.** Should the router fail over across providers (`kimi-k3:synthetic` ↔ `kimi-k3:pool`/`:cloud`), and on which errors?
   - **Rate limit** (short-lived, back off) versus **monthly or credit exhausted** (bench that account until the reset).
4. **Admission control.** Should the CT110 launcher gate new runs on the capacity signal, as the old PC launcher did with `synthetic_down()`, instead of launching into 429s?
5. **The overseer's model.** Once capacity is managed, can the ticker stay on included capacity? Is a small paid model still worth it just for reliability?
6. **Ollama Cloud accounts.** Which accounts are near their monthly limits, when do they reset, and should the pool skip an exhausted account automatically (per-deployment cooldown)?

## 5. Constraints the expert must know
- **LiteLLM on CT202** is prod.
  - Any edit to `litellm-config.yaml` restarts the prod router.
  - Model and router settings are **database-owned**, and the database row wins over the YAML.
  - Changes go UAT (CT204) first, as an overseer-applied script with a dry run by default.
- **Host-level changes on ptait01** need Paxton's explicit go, a rollback plan and a quiet window.
- **No new credential purchases or spend** without Paxton. Account *names* are fine to discuss; key values never go in docs, logs or PRs.
- **Deploys:** harness runs never deploy. The overseer merges and deploys through the lanes. CT110 restarts need drain on and no active runs.

## 6. Where to look
| What | Where |
|---|---|
| Overseer ledger and open asks | `C:/tmp/queue/LEDGER.md` (tail), `C:/tmp/queue/OPEN-ASKS.md` |
| Chat overseer instructions and tick rows | `LongHorizon-Harness/docs/handoffs/overseer-secondary-session.txt` |
| Task 282 spec | `C:/tmp/provider-quota-monitor-failover-task.txt` |
| Task 146 / #149 | `cognizioware-mcp-tools` PR #149 |
| Live queue | CT110 `GET http://192.168.21.168:8799/api/queue` (bearer token from CT110's secrets env) |
| Router | CT202 container `cognizioware-mcp-tools-litellm`; UAT is CT204 on ptait07 |

## 7. The decision this session should end with
A short written policy covering:
- the capacity signal source;
- the harness-versus-overseer allocation;
- the failover rules;
- the admission gate;
- the overseer's model.

Plus a call on task 282: **keep, shrink, or cancel.** With that, the overseer can re-scope the queued work in one step.

## 8. Additional exploration: run agent work on Paxton's Claude subscription (headless, device-code auth)

**Paxton's idea:** wire a Claude subscription into CT110 or our own code with a device-code style login, possibly automated through the ptait09 runner, which he believes is logged in as his user, `PaxtonTait`. The point would be to add capacity that isn't the Synthetic pack or the Ollama accounts.

**Facts to start from.** These come from Anthropic's docs via a research pass. Verify each before relying on it.
- **Claude Code has a headless path for subscriptions.**
  - `claude setup-token` issues a **long-lived (about one-year) OAuth token** for a Pro/Max/Team/Enterprise subscription.
  - Headless use sets it as `CLAUDE_CODE_OAUTH_TOKEN`, then runs `claude -p …`. That's the same CLI the harness already drives for its roles.
  - The docs describe a code-style flow for machines with no browser (SSH, containers, WSL).
  - Source: https://code.claude.com/docs/en/authentication.md#generate-a-long-lived-token
- **The Claude Agent SDK does *not* support subscription (claude.ai) login.** It takes API keys, workload identity or cloud-provider credentials only. The docs say: *"Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK."*
  - So "SDK in our code with a subscription" isn't a supported path. The Claude Code CLI with a setup token is the realistic one.
  - Source: https://code.claude.com/docs/en/agent-sdk/overview
- **Terms: this is the part the expert must settle first.**
  - Consumer subscriptions are single-user. Anthropic points shared production automation to the Claude API with an API key and commercial terms.
  - Running unattended harness roles 24/7, or several concurrent sessions for a fleet, on a personal Max plan may fall outside acceptable use, and heavy concurrent use can trigger account limits.
  - The exact Consumer Terms wording wasn't retrieved (**unconfirmed**); read https://privacy.claude.com/en/collections/10663362-consumers.
- **Limits:** subscriptions have a rolling 5-hour window plus a weekly cap. Exact numbers aren't published, and per-plan hour figures online are third-party estimates. See https://claude.com/pricing and https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan.

**Facts about our environment (measured by the overseer):**
- **The "ptait09 runner" identity isn't confirmed as `PaxtonTait`.**
  - The gateway's `ssh_exec` to ptait09 runs as **`ptait09\svc-mcp`**. That user has no GitHub login and presumably no Claude login.
  - The Claude Code sessions Paxton opens interactively on ptait09 do use his login.
  - Which user the HTTP runner (`ptait09.easybutt0n.ai`) and the Hydra device agent's spawned shells run as is **unverified**. Check it before designing anything around it.
- **There's a standing prohibition that conflicts with this.** Overseer memory records that `@claude` PR-review invocations were banned (task 117) *because they spend Paxton's personal subscription*. Paxton would need to lift or narrow that deliberately.
- **Automating through ptait09 cuts against the current goal of moving the overseer off the dev box.** A token placed on CT110 wouldn't.

**Options to compare:**
1. **Setup token on CT110.** Paxton runs `claude setup-token` once, and the token goes into CT110's secrets env as `CLAUDE_CODE_OAUTH_TOKEN`. The harness uses it for one role (the manager, say) or only for the overseer tick. It stays off ptait09, but it's the most "automated shared workload"-like, so settle the terms first.
2. **Subscription only for Paxton-initiated work.** Keep the subscription for his own interactive and overseer sessions (what it's used for today). Fix capacity with the Synthetic/Ollama policy from sections 4–7 plus a small **API-key** budget for unattended roles. This is the cleanest on terms.
3. **ptait09 runner as a subscription executor.** A queue role runs `claude -p` on ptait09 under his login, reached through the runner or Hydra. This needs the identity check above. It re-couples the fleet to the dev box, and it has the same terms question as option 1.
4. **Ask Anthropic.** Use a Team/Enterprise seat, or request approval for this usage pattern, if the value is high enough.

**Questions for the session:**
1. Which workloads would run on the subscription: the overseer tick only, one harness role, or everything? At what concurrency?
2. Is that within the Consumer Terms? If not, is an API-key budget (spend, Paxton's call) or a Team plan the right vehicle?
3. If a token is used: where does it live (CT110 secrets env, names only in docs), who rotates it, and how is its usage metered alongside the Synthetic/Ollama capacity signal from section 4?
4. Does Paxton want to lift or narrow the task-117 "never spend the personal subscription from automation" rule? It has to be an explicit decision, recorded in OPEN-ASKS.

**Add to the decision in section 7:** a yes or no on subscription-backed automation, the vehicle if yes (setup token on CT110, API key, or a Team seat), and the scope, meaning which roles and ticks.
