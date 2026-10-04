---
spec_status: draft
---

# Power BI Report Server: one reporting surface over all fleet data (2026-10-03)

**Ask (Paxton, 2026-10-03):** the next ship plan needs a Power BI surface, local in our own infra, showing all of our data through a wrapper of some sort. Paxton chose **Power BI Report Server** (on-premises) using the Power BI Premium product key held on PTAIT09.

**Status: DRAFT for Paxton's review. Do not queue until he approves it.** The open decisions at the bottom block Phase 1.

## Plan header
| Field | Value |
|---|---|
| Planning session | `47a358d5-8c57-46ad-9226-63e23362b33b` (Claude Code, PTAIT09, 2026-10-03) |
| Repos | `cogniziocompany/cognizioware-mcp-tools` (reporting DB, sync job, Caddy, e2e); this repo for the run export |
| BI host | new Windows Server VM on **ptait07** (192.168.21.138) |
| Product key | file `C:\Users\PaxtonTait\.secrets\power-bi-premium-license-key.txt` on PTAIT09. **Never copy the key into a repo, task, queue note, PR or log.** An operator types it into setup. |

## Design

```
 sources (read-only)                   wrapper                          surface
 ------------------------------        -------------------------        -----------------------------
 CT110 queue (Postgres)        \
 CT110 runs (harness API)       \      reporting-sync (CT202)           Power BI Report Server
 LiteLLM spend/keys (CT202 PG)   >---> -> Postgres DB `reporting` <---- (Windows VM, ptait07)
 Langfuse traces                /         views: rpt.*                  scheduled refresh / DirectQuery
 fleet-admin registry          /          role: pbi_reader (SELECT)     LAN: http://<vm>/reports
```

**Why a wrapper and not direct connections:** Power BI reads one database with one read-only login. Source credentials stay on CT202, and the report models only depend on the `rpt.*` views, so a source can change without breaking the reports. Power BI Report Server supports PostgreSQL with scheduled refresh and DirectQuery (Microsoft Learn, "Power BI report data sources in Power BI Report Server").

## Phases

### Phase 1: reporting database and sync (CT202, `cognizioware-mcp-tools`)
1. Create database `reporting` on CT202's existing Postgres, with role `pbi_reader` (SELECT on schema `rpt` only) and role `reporting_sync` (owner of `stage`). Passwords live in `mcp-tools.env` as `REPORTING_PBI_READER_PASSWORD` and `REPORTING_SYNC_PASSWORD` (names only, never values).
2. Add a compose service `reporting-sync`: a small Python job that every 5 minutes upserts from each source into `stage.*`, keyed on source ids so a rerun never duplicates rows.

   | Source | How it is read | Tables |
   |---|---|---|
   | CT110 queue | Postgres, read-only role | `stage.queue_entries` |
   | CT110 runs | `GET /api/runs?fields=summary` plus per-run snapshot for runs changed since last sync, bearer `OPS_HARNESS_API_TOKEN` | `stage.runs`, `stage.rounds`, `stage.approvals` |
   | LiteLLM | its Postgres, read-only role: `LiteLLM_SpendLogs`, keys, models | `stage.llm_spend`, `stage.llm_keys` |
   | Langfuse | public API, traces and observations since cursor | `stage.traces` |
   | fleet-admin | its read API | `stage.fleet_hosts` |

3. Create views `rpt.runs`, `rpt.run_rounds`, `rpt.gate_waits`, `rpt.queue_throughput`, `rpt.llm_cost_daily` (by model, key and run tag), `rpt.fleet_hosts`. Views carry no prompts, no task text beyond a bounded title, and no secrets.
4. Add e2e suite (next free number, checked against suite 62): `pbi_reader` can SELECT `rpt.*`, cannot SELECT `stage.*`, cannot write, and every view returns rows.

**STOP GATE 1:** each `rpt.*` view returns rows that match its source within 10 minutes. Compare run counts with `/api/runs?fields=summary` and the 24 h spend total with the LiteLLM UI. A `pbi_reader` write attempt fails.

### Phase 2: Power BI Report Server VM (ptait07)
1. Create a Windows Server 2022 VM: 4 vCPU, 16 GB RAM, 100 GB disk, CPU type `host` (Report Server needs AVX; the i9-13900H has AVX2), static IP, Pi-hole records on both .3 and .4 (append, never replace the list), and `onboot 1`.
2. Install SQL Server for the report server catalog. The edition is a blocking decision (see below).
3. Install Power BI Report Server. An operator enters the product key from the PTAIT09 file. Then configure the catalog database and the web portal URL.
4. Install Power BI Desktop for Report Server (the version must match the server) on the VM or on PTAIT09 for authoring.

**STOP GATE 2:** the web portal loads at `http://<vm>/reports` from the LAN, and a test report with a PostgreSQL source on `reporting` completes a scheduled refresh.

### Phase 3: first reports
Harness operations (runs per day, completion rate, rounds per run, gate wait time), LLM cost (daily by model and key, cost per run), queue health (throughput, failure reasons), and fleet (hosts, last check-in).

**STOP GATE 3:** each report refreshes on schedule for 24 h with no failures in the Report Server execution log.

### Phase 4: access beyond the LAN (only after the access decision below)

## Hard rules that apply
- No host-level changes on **ptait01**. This plan touches ptait07 (new VM) and CT202 (compose service) only.
- ptait07 `local-lvm` has about 135 GB free. A 100 GB disk fits but leaves little room, so check `pvesm status` first.
- CT202 changes ship through the Deploy MCP Tools lane. Use `compose up -d` for the new service only. Never recreate `litellm-router`, and remember `--env-file` is mandatory.
- Secrets by name only: the product key, the DB passwords and the bearer token.
- Clone with `core.autocrlf=false`, and check `git diff --stat` before opening a PR.

## Open decisions (Paxton)
1. **SQL Server edition for the catalog.** With a Premium key, the report server database must be on SQL Server **Standard or Enterprise** (Microsoft Learn, "Reporting Services features supported by editions"). Do we have a SQL Server Standard or Enterprise license, or does the Premium entitlement cover it for this use? Express and Developer editions are not allowed with a Premium key.
2. **Windows Server license** for the VM: which key or volume license?
3. **Who opens it from outside the LAN, and how.** Report Server signs users in with Windows auth (NTLM/Kerberos), which does not pass through our Caddy and oauth2-proxy pattern. Microsoft's supported external path is Entra application proxy (needs Entra ID P1). The alternative is LAN and VPN only. Phase 4 waits on this.
4. **Data scope.** Should Langfuse traces be included (large), or start with runs, queue, spend and fleet? Should QuickBooks and finance data stay out (CFO1 / CT111) until a separate finance spec?
