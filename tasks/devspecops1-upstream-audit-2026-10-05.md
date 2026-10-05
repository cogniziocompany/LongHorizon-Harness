---
spec_status: ready-for-dev
---

# devspecops1: upstream and downstream audit duty, starting with Langfuse v4 (deadline 2026-11-16)

**Ask (Paxton, 2026-10-05):** "This needs to now be part of the devspecops1 job: audit all our upstream or downstream infra, repo forks, branches etc, and work with Overseer1 to get them properly processed and run through to branch-ready deployment and tested with a certified independent QA expert, our cognizioware-qa, customized for the application."

Planning session `47a358d5-8c57-46ad-9226-63e23362b33b` (`[47a358]`). Related: `tasks/rsi-observability-reports-2026-10-05.md` (Phase 2 Langfuse, Phase 6 QA auditor).

## The standing duty (devspecops1)
devspecops1 already watches Conditional Access and egress sources. Add a recurring **dependency and deprecation audit**:

1. **Inventory**, kept in devspecops1's store and shown on the fleet DevSpecOps page:
   - upstream services with a version or API contract: Langfuse, LiteLLM, Ollama, Open WebUI, n8n, Proxmox, Cloudflare, Entra/Graph, GitHub Actions runners and action versions;
   - repo forks and their distance from upstream. For example `LongHorizon-Harness` is a fork of `AMAP-ML/LongHorizon-Harness`;
   - long-lived branches and stale PRs per repo;
   - pinned images and SDK versions per service (`docker-compose.yml`, lockfiles).
2. **Detect:**
   - vendor deprecation notices with a date;
   - end-of-support versions;
   - forks more than N commits behind;
   - branches with no activity for N days;
   - actions flagged deprecated. Example: the Node.js 20 warnings on every workflow run today.
3. **Propose, never apply.** For each finding, send Overseer1 a proposal: what, deadline, affected repos and services, the suggested change. This is the same pattern as the egress-source proposals. Overseer1 queues the fix on CT110.
4. **Process to branch-ready.** The harness run produces a PR. The PR is not "ready for approval" until **cognizioware-qa** has a recorded verdict for it (Phase 6 of the RSI spec): pass, fail or not-covered, with evidence. Where an application has no suite, the finding says so and a suite gets written first.
5. **Record.** Every finding, proposal, run, QA verdict and deploy goes to the reporting DB:
   - `stage.devspecops_findings`;
   - `rpt.upstream_deadlines`;
   - `rpt.fork_drift`;
   - `rpt.stale_branches`.
   A "Deadlines" report in Power BI and on the fleet Reports tab is sorted by days remaining.

**STOP GATE (duty):** the inventory lists every service in mcp-tools `docker-compose.yml` and every repo in the org. The Langfuse v4 item below appears as a finding with its date. One finding has gone the full path: proposal, queued run, PR, cognizioware-qa verdict, deploy.

## Work item 1 — Langfuse v4 migration (hard date: 2026-11-16)
Langfuse says that after 2026-11-16 some features may stop working unless integrations are updated. The project banners list these action items:

| Project | Action items shown | What it is in our estate |
|---|---|---|
| `lh-harness` | Update SDK (1) | The LiteLLM router on CT202 (`cognizioware-mcp-tools-litellm`, LiteLLM 1.100.4) ships **langfuse 2.59.7** and logs through the `langfuse` callback set per key. Langfuse's current LiteLLM integration is the `langfuse_otel` callback (OpenTelemetry). |
| West Hive Capital (hive-portal) | Update SDK (1), Upgrade Instrumentation (1) | The hive-portal app's own Langfuse SDK and its manual spans (`hive-orchestrator`, `llm-step-0`, `eval:tool-calling`). |
| `cognizioware-vlab` | none seen (no traffic) | Only the dataset `trms-qa-agent-evals`. |

Follow Langfuse's own workflow: `https://raw.githubusercontent.com/langfuse/skills/main/skills/langfuse/references/v4-project-migration.md`. It ends in a seven-row readiness report. Produce that report per project.

**1a. LiteLLM router (repo cognizioware-mcp-tools).**
- Move the per-key logging from the `langfuse` callback to `langfuse_otel`, or upgrade the bundled SDK to the major Langfuse requires. Decide from the upgrade-path docs and LiteLLM's `langfuse_otel` docs. Record both the declared and the resolved versions.
- Keep the logging **per key**. Global Langfuse callbacks stay forbidden on the shared router.
- `infrastructure/scripts/set-perkey-langfuse.sh` sets the callback vars. Update it, plus the keys `lh-harness` and `n8n-dev-pipeline-orchestrator`.
- Preserve the tags the reports join on: `lh-run/<run_id>`, `round_N`, `<role>`. In v4 they must be on the cost-bearing generation observations, not only on the trace.
- Changing the router image or config recreates the router. Use the deploy lane and `safe-recreate.sh`, in a quiet window, with UAT CT204 first.
- Validate against a **non-production Langfuse project** first (`cognizioware-vlab` is empty and suitable), then cut over `lh-harness`.

**1b. West Hive hive-portal (repo westhivecapital-hivemind).**
- Upgrade the SDK major and re-instrument per the v4 guide: root input/output on the root observation, session id on every cost-bearing child.
- Separate finding from the traces: on 2026-10-03/04 every `hive-orchestrator` call failed with `RateLimitError` (Ollama weekly usage limit, accounts prax211 and ai-dev01, model `kimi-k2.6:cloud`). That is a capacity problem, not part of this migration. File it with Overseer1.

**1c. Readers.** reporting-sync's Langfuse module (RSI Phase 2) must use v4-compatible endpoints from the start, per the deprecated-API guide.

**Keys.** Paxton is leaving a new API key pair per project in `C:\Users\PaxtonTait\.secrets\` on PTAIT09. Reference them by file name only. The operator copies them to CT202 as `LANGFUSE_REPORTING_<PROJECT>_PUBLIC_KEY` / `_SECRET_KEY`.

**QA.** cognizioware-qa's `litellm-acceptance` suite gets a case that sends one tagged request through the router and finds the resulting observation in the test project with its tags. That verdict gates the PR.

**STOP GATE 1:** the Langfuse banner shows no open action items for `lh-harness` and the West Hive project. A tagged harness request appears in Langfuse with run, round and role on the generation. The seven-row readiness report for each project is in the PR. The cognizioware-qa verdict is recorded.
