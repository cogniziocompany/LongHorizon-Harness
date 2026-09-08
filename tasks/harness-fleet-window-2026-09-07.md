# Harness Fleet Window (2026-09-07)

**Ask (Paxton 22:05 PT):** add the plan below as harness tasks and put it on the pipeline deployment schedule.

**Queue:** `19a-fleet-report-admin` (ptait09-easybutt0n-ai fleet-admin: schema, ingest, read API, socket.io, YouTrack, view, Dockerfile; 10 rounds) -> `19b-fleet-report-deploy` (mcp-tools: vendored copy, compose + Caddy, lane + e2e 31, S14; 6 rounds) -> `19c-fleet-reporter-harness` (LongHorizon-Harness: reporter, hooks, round content push, docs; 8 rounds). Deploy order: 19a PR merged -> 19b PR -> lane -> CT202 (`caddy reload` only) -> enrol ct110 via POST /enroll, LH_HARNESS_FLEET_* into /home/harness/.lh-harness-secrets.env -> 19c PR -> CT110 release via idle auto-deploy (never a forced restart) -> cloud node image rebuild on corsairai300 -> Ship Plane artifact refresh (same URL, title unchanged). Task 05e (fleet hostname -> Hydra) is SUPERSEDED: fleet.easybutt0n.ai stays fleet-admin; Hydra remains the control surface.

**Schedule position:** after the guest click-through work (05h) and the prod restore/pp follow-ons (M1/M3); 19a can run in parallel with M3 since it touches a different repo. Target: view live on fleet.easybutt0n.ai Sep 9; CT110 reporter on the next idle release.

## Progress
- 22:10 PT: three queue entries created; plan copied below verbatim for the runs to read.

---

# Harness Fleet Window: every lh-harness session pushes to fleet.easybutt0n.ai; one live, read-only window into each session (thinking + all artifacts) + YouTrack

## Plan header (for the expert who builds and schedules this)
| Field | Value |
|---|---|
| Planning session id | `c84a6035-425c-4792-b2db-4d485733bd7b` (Claude Code, PTAIT09, 2026-09-07) |
| Plan file | `C:\Users\PaxtonTait\.claude\plans\base-don-curent-infra-serene-mango.md` |
| Primary repo / dir | `cogniziocompany/LongHorizon-Harness` at `c:\Users\PaxtonTait\source\LongHorizon-Harness`, branch `main`, HEAD `b2d3343` |
| Repo 2 / dir | `cogniziocompany/ptait09-easybutt0n-ai` at `c:\Users\PaxtonTait\source\ptait09-easybutt0n-ai`, branch `easybutt0n-runner-v2` (HEAD `55c667c`), subdir `fleet-admin/` |
| Repo 3 / dir | `cogniziocompany/cognizioware-mcp-tools` at `c:\Users\PaxtonTait\source\cognizioware-mcp-tools` (Caddy, compose, e2e, deploy lane) |
| Reference only | `cognizioware-hydra` fleet worktree `c:\Users\PaxtonTait\source\cognizioware-hydra-fleet` (`feat/harness-fleet`, HEAD `721de8f`): control surface, unchanged by this plan |
| Public host | `https://fleet.easybutt0n.ai` (Caddy on CT202 `192.168.21.161` -> compose service `fleet-admin:8787`) |
| Database | agent-db Postgres `192.168.21.154:5432/ai_easybutt0n`, schema `fleet` |
| Harness nodes today | CT110 `192.168.21.168:8799` (systemd, 3 runs live, cap 3); cloud containers from `docker/compose.node.yml`; local installs (opt-in) |
| Related tasks | `tasks/fleet-entry-point-and-harness-ux-2026-09-07.md` (12/13a/13b), `tasks/repo-problems-from-ship-plan-2026-09-07.md` item 05e (superseded by this plan), `tasks/reporting-first-slice1-2026-09-06.md` |
| Ship Plane artifact | `https://claude.ai/code/artifact/8b0c8b20-79bf-4181-9179-b64b1cbd44c5` (title stays "Cognizioware Ship Plane") |
| Schedule constraint | CT110 release only at idle via auto-deploy; never restart `lh-harness.service` with runs live; CT202 compose edits need `.bak-YYYYMMDD` and `caddy reload` only |

## Explicit use case
**A read-only window into every lh-harness session, wherever it runs.** From one page at fleet.easybutt0n.ai the overseer (or any expert) can:
1. See every harness node (CT110, cloud containers, local installs) and every run on it, live, in the Ship Plane visual grammar.
2. Open any run and read, round by round, what the manager, executor and auditor did: the full role trajectories **including thinking/reasoning text**, tool calls, and every artifact the round produced (plans, diffs, audit reports, final responses), plus gates raised and how they were resolved.
3. See the YouTrack issue each run is tied to, and the open MCP-project issues in the AI states.
4. Do none of the control actions. No stop/resume/instructions/gate-resolve from this surface in this phase; control stays in the CT110 UI and Hydra's fleet MCP.

This means the reporter pushes **content**, not just status: trajectories and artifacts are shipped per round so NAT'd nodes are fully visible without any inbound path.

## Context
Today the only way to see harness runs is the CT110 UI, one node at a time. Hydra's `feat/harness-fleet` branch aggregates by *pulling* registered nodes, which cannot see local installs or cloud containers behind NAT. The user wants every lh-harness session, wherever it runs, to *push* to a public API at `fleet.easybutt0n.ai`, and a high-level "ship plane"-style view over all harnesses, live via socket.io, read-only in this phase, with YouTrack issue data alongside. The user also asked to keep the artifact name "Cognizioware Ship Plane" (intentional) and refresh its facts.

Decisions confirmed by the user: name stays "Ship Plane"; API lives at fleet.easybutt0n.ai on the existing fleet-admin code; push model; socket.io with polling fallback; internal tool, no admin/role pattern; read-only phase.

## What already exists (reuse, do not rebuild)
- **fleet-admin** (`c:\Users\PaxtonTait\source\ptait09-easybutt0n-ai\fleet-admin\`, branch `easybutt0n-runner-v2`): Node/Express + pg, `:8787`. `src/server.js` has `POST /enroll` (bootstrap token -> per-host `device_key`), `POST /checkin` (HMAC over raw body, `X-Fleet-Host` + `X-Fleet-Signature`), admin reads/controls behind `FLEET_ADMIN_KEY`, `logEvent()`, `GET /health`. `schema.sql` = schema `fleet` in agent-db `192.168.21.154:5432/ai_easybutt0n` (`hosts`, `enroll_tokens`, `heartbeats`, `events`, view `host_status`). `src/migrate.js` idempotent. Deployed 2026-08-09 to CT202 as compose service `fleet-admin` behind Caddy site `fleet.easybutt0n.ai` (mcp-tools `infrastructure/docker/caddy/Caddyfile`), but the container was pruned in the disk-full event (task 05e reports 502). **This plan supersedes 05e's "repoint to Hydra": fleet-admin is redeployed and extended instead.**
- **lh-harness** (this repo): event funnel `manager._append_event` (`src/lh_harness/manager.py:2400`) with public names in `EVENT_TYPE_MAP` (`src/lh_harness/webapi/events.py:25`); status writer `ControlBus.write_status` (`src/lh_harness/supervisor/control_bus.py:690`); gates `src/lh_harness/dashboard/gate.py` L148/L172; run summary `build_run_summary` (`src/lh_harness/webapi/snapshot.py:195`); `EventEnvelope` (`webapi/models.py`). No node identity, no HTTP client dep (use stdlib `urllib.request` on a daemon thread; no new dep). Config TOML is allow-listed (`config.py:177`), so use env vars. Docker: `docker/entrypoint.sh`, `docker/compose.node.yml`, CT110 systemd `lh-harness.service`.
- **YouTrack**: instance `youtrack.cognizio.company`, project `MCP`; REST client already exists in mcp-cognizioware BillingService; gateway MCP `youtrack_mcp` returns 0 tools (placeholder). Harness-run link exists only as nullable `youtrack_issue_id` in the reporting API. Instance auto-offs nightly, so calls must soft-fail.
- Not reusable: `packages/fleet-view` on `feat/chat-shield-fleet-view` is a CDP screencast relay (different thing, same name). Hydra fleet MCP stays as the *control* surface; this phase is read-only view + ingest.

## Design

### A. Harness reporter (repo LongHorizon-Harness, branch `feat/fleet-reporter`)
New module `src/lh_harness/fleet/reporter.py`:
- Enabled only when `LH_HARNESS_FLEET_URL` is set. Identity: `LH_HARNESS_FLEET_NODE` (default `socket.gethostname()`), key `LH_HARNESS_FLEET_KEY` (the per-host `device_key`), optional `LH_HARNESS_FLEET_LABELS` (e.g. `kind=ct110|cloud|local,repo=...`).
- Queue + single daemon thread; `urllib.request` POST with HMAC-SHA256 over raw body (same headers as `/checkin`); batches events every 2 s, drops after bounded retry (never blocks a run; log once at WARN on persistent failure).
- Hooks: wrap `_append_event` (send `EventEnvelope` public-form: run_id, type, ts, round, role, status, payload trimmed to summary fields, no transcripts); `ControlBus.write_status` -> `run.status` event; gate create/resolve already flow through the ledger (`approval_created/resolved`).
- **Round content push** (the "window"): on `managed_round_recorded` (`manager.py:2345`) the reporter reads `logs/role_orchestration/rounds/<n>/` via `safe_run_rounds`/`safe_run_role` (`utils/run_boundary.py`) and posts, gzip-compressed, every artifact file and every role trajectory (the same files `GET /api/runs/{id}/rounds/{n}/artifacts` and `/trajectory/{role}` serve, so thinking/reasoning blocks are included as stored). Cap 8 MB per round after gzip; larger files are truncated with a `truncated: true` marker and byte count, never dropped silently. Also pushed: `logs/report.json` at run end. Redaction: reuse the existing secret-scan patterns if present in the repo; otherwise mask `sk-`, `ghp_`, `Bearer ...` tokens before send.
- Heartbeat every 30 s: `{node, version, runs: [build_run_summary(...)], capacity: {active, cap}, queue_len}` using the existing snapshot code, so the fleet has a full picture even if events were dropped.
- Optional run-level link field: accept `youtrack_issue_id` in `POST /api/runs` body (store in `control/status.json` meta) and include it in summaries. Small, additive.
- Wiring: `webapi/server.py` `create_app()` starts the reporter; `docker/compose.node.yml` + `docker/entrypoint.sh` pass the three env vars; CT110 unit gets them in `/home/harness/.lh-harness-secrets.env`. Docs: `docs/fleet-reporting.md`, README env table.
- Tests: unit test for HMAC + batching with a local HTTP stub (pytest, existing `httpx2` test extra not needed).

### B. fleet-admin extension (repo ptait09-easybutt0n-ai, branch `feat/harness-fleet-report`)
- `schema.sql` additions (idempotent): `fleet.harness_nodes(host FK, version, kind, labels jsonb, ui_base_url, last_heartbeat, capacity jsonb)`, `fleet.harness_runs(host, run_id PK(host,run_id), repo, workspace, status, round, active_role, model, youtrack_issue_id, started_at, ended_at, last_event_at, summary jsonb)`, `fleet.harness_events(id, host, run_id, type, ts, round, role, payload jsonb)` with index `(host, run_id, ts)`, `fleet.harness_gates(host, run_id, approval_id, trigger, status, created_at, resolved_at)`. Retention: events pruned >14 d by a daily `setInterval` job.
- Content tables: `fleet.harness_rounds(host, run_id, round, recorded_at, summary jsonb)`, `fleet.harness_artifacts(host, run_id, round, role NULL, name, kind artifact|trajectory|report, content_type, size_bytes, truncated bool, body bytea gz)` PK `(host, run_id, round, kind, role, name)`. Retention 30 d for bodies (summaries kept). Body limit per request 10 MB (`express.json` limit raised on this route only, raw body kept for HMAC).
- Ingest (device auth = existing HMAC path, factored into `requireDevice()` middleware): `POST /harness/heartbeat` (upsert node + runs), `POST /harness/events` (batch insert, upsert run status/gates from event types), `POST /harness/rounds` (round summary + artifacts + trajectories, idempotent upsert).
- Window read API: `GET /api/fleet/runs/:host/:runId/rounds` (list), `.../rounds/:n/artifacts`, `.../rounds/:n/artifacts/:name` (decompressed, correct content-type), `.../rounds/:n/trajectory/:role` (JSON, thinking preserved), `.../report`. Same shapes as the harness's own routes so a viewer component can be reused later.
- Read API (internal tool; single optional `FLEET_READ_KEY` via `X-Fleet-Read-Key` or `?key=`; when unset, open): `GET /api/fleet/overview` (nodes + active runs + open gates + counts, one call for the view), `GET /api/fleet/nodes`, `GET /api/fleet/runs?status=&host=`, `GET /api/fleet/runs/:host/:runId` (summary + last 200 events + gates), `GET /api/fleet/youtrack/issues` (see C). Every non-2xx returns JSON.
- Realtime: `socket.io` server on the same http server (add deps `socket.io@4`, keep `express`, `pg`). Namespace `/fleet`; on connect emit `overview` snapshot, then `node.heartbeat`, `run.updated`, `event`, `gate.updated` deltas as ingest lands. Rooms: `run:<host>:<runId>` on request. Auth handshake uses the same read key. Transports `['websocket','polling']`; Caddy already proxies WS.
- **Front-end stack (Sonar Pro research, 2026-09-07, via the gateway `sonar-pro`, cost $0.025):** primary recommendation React 18 + Vite + TypeScript + Tailwind + shadcn/ui; runner-up Svelte 5 static; Vue judged viable but fragmented across UI kits; Alpine/htmx/no-build rejected because virtualized transcripts, SVG system map, markdown/diff and socket.io deltas are SPA territory. Sources: shadcn Vite install + CLI v4 changelog (agent-oriented docs, `ui.shadcn.com/llms.txt`), svelte.dev 2026 blogs, shadcn-vue docs, github.com/satnaing/shadcn-admin (reference dashboard). **Adopted: React + Vite + shadcn/ui**, because it also matches what already exists: the harness UI (`frontend/web/`, React 18.3 + Vite 5 + TS, `useRunFeed.ts`, shared `frontend/core`) and the powerplatform admin (`AdminPage.tsx`). Minimal effort = copy the harness's transcript/artifact rendering into the fleet window instead of writing a viewer.
- View app `fleet-admin/web/` (Vite, builds to `fleet-admin/public/` in the Docker multi-stage build; Express serves static). Packages: `react`, `react-dom`, `vite`, `typescript`, `tailwindcss`, shadcn/ui generated components (`card`, `badge`, `tabs`, `sheet`, `scroll-area`, `tooltip`), `socket.io-client`, `@tanstack/react-virtual`, `react-markdown`, `diff` (+ `lucide-react` icons, same as the harness UI). Overview = Ship Plane visual grammar as hand-written SVG components (host bands = nodes, run cards, blue flow arrows, green/amber/red pills, dark/light via CSS tokens, animated status pulses). Clicking a run opens the **session window** (shadcn `sheet`): round timeline, per-role transcript pane (virtualized) with thinking blocks collapsible but present, artifact list with inline markdown/diff/json viewer, gate history, YouTrack issue card; `round.recorded` over socket.io appends the new round live. Optional later: `echarts` for capacity/time panels only. Read-only: no mutating request anywhere in the bundle (assert in e2e).
- Health: extend `/health` with db + last ingest age. Dockerfile in-repo, multi-stage: stage 1 `node:20` runs `npm ci && npm run build` in `web/`; stage 2 `node:20-alpine` copies `src/`, `schema.sql`, built `public/`, runtime deps only; CMD `migrate.js && server.js`. Lets the mcp-tools deploy rsync + build it (today only a hand-copied Dockerfile exists on CT202).

### C. YouTrack read-through (in fleet-admin)
- Env `YOUTRACK_URL`, `YOUTRACK_TOKEN` (permanent token, names only in `.env.example`). Server-side poll every 60 s: `GET /api/issues?query=project: MCP State: {Ready for AI Review},{Needs PR Review},{Needs Fix} ...&fields=idReadable,summary,customFields(name,value(name)),updated`. Cache in memory + `fleet.youtrack_issues` table; on fetch error keep last cache and set `youtrack.stale=true` (instance auto-offs nightly). Join to runs on `harness_runs.youtrack_issue_id`; overview carries `issues[]` and each run carries its issue summary.
- Read-only. No writes to YouTrack in this phase.

### D. Deploy + ingress (repo cognizioware-mcp-tools, branch `fix/fleet-admin-redeploy-harness-report`)
- `infrastructure/docker-compose.yml`: add/restore service `fleet-admin` with `build.context: ./docker/fleet-admin`, `expose: 8787`, `env_file: mcp-tools.env`, journald logging tag, healthcheck `wget -q -O /dev/null http://127.0.0.1:8787/health`. Source arrives by a build step that vendors `fleet-admin/` from the runner repo (git subtree or a pinned copy under `infrastructure/docker/fleet-admin/`; recommend pinned copy + `SOURCE_REF` note, matching how other in-stack services are laid out).
- Caddyfile: keep `fleet.easybutt0n.ai -> fleet-admin:8787`; reload only (`caddy reload`, no LiteLLM restart). DNS records already exist (grey-cloud A + Pi-hole host + ddns cron).
- Env names added to `infrastructure/.env.example`: `FLEET_ADMIN_KEY`, `FLEET_READ_KEY`, `POSTGRES_*` for agent-db, `YOUTRACK_URL`, `YOUTRACK_TOKEN`. Values live only in `/opt/cognizioware-mcp-tools/mcp-tools.env`.
- Health checks step (`deploy-mcp-tools.yml` ~L771): add `https://fleet.easybutt0n.ai/health`. New e2e suite `e2e/suites/31-fleet-report.test.js` with bootstrap-grace soft-skips (pattern from suite 29): health, overview shape, socket.io connect receives `overview`.
- Close task 05e as superseded; add a new task file `tasks/harness-fleet-report-2026-09-07.md` recording the three branches and deploy order.

### E. Ship Plane artifact refresh (same URL 8b0c8b20…)
Keep the title. Update: Hydra box -> "Hydra fleet MCP (control) · fleet.easybutt0n.ai fleet-admin (report view, this plan)"; red line "fleet.easybutt0n.ai dead upstream -> 05e" becomes "fleet-admin redeploy + harness report -> new task"; runner label `self-hosted, lan-cognizioware` on CT210; component table rows for "Harness fleet report" (pend) and "YouTrack read-through" (pend); date stamp. Publish with `url` so the link is unchanged.

## Deploy order
1. fleet-admin branch: schema + ingest + read API + socket.io + view + YouTrack; unit tests; local `docker compose` smoke against agent-db (or a throwaway pg).
2. mcp-tools branch: compose/Caddy/e2e; PR -> lane -> CT202. Verify `/health` through Caddy.
3. Issue a bootstrap token, enroll `ct110` (and each cloud/local node) via `POST /enroll`; put `LH_HARNESS_FLEET_*` in the node env.
4. Harness branch: reporter; PR; CT110 release rides the existing auto-deploy at idle (no forced restart; 3 runs live). Cloud containers pick it up on next image build.
5. Refresh the Ship Plane artifact.

## Hard rules carried from the repos
- Never pip into `/home/harness/venv`; never restart `lh-harness.service` to force a deploy.
- Back up `docker-compose.yml` and `Caddyfile` as `.bak-YYYYMMDD` before edits; `caddy reload`, not restart.
- Scope any LiteLLM keys by access group, never server id (not needed in this phase).
- Secrets: names only in repos. Flag: mcp-tools `design docs/mcp-placeholder-services-audit-and-plan.md` L240 appears to hold a literal billing bearer; rotate/remove (separate small task).
- Windows clones: `autocrlf=false`, check `git diff --stat` before PR.

## Verification
- Harness: `pytest tests/fleet` (HMAC, batching, no-block on failure); run one local `lh-harness web` with `LH_HARNESS_FLEET_URL` pointing at a local fleet-admin and confirm heartbeat rows in `fleet.harness_nodes` and events in `fleet.harness_events`.
- fleet-admin: `node --test`; `curl https://fleet.easybutt0n.ai/health` shows db ok and ingest age; `GET /api/fleet/overview` lists ct110 with its 3 live runs; open the view in a browser, resolve a gate on CT110 UI, and see the gate card flip live over socket.io within ~2 s; open a run's window and confirm the executor trajectory for the latest round matches CT110's `GET /api/runs/{id}/rounds/{n}/trajectory/executor` byte-for-byte after decompression, and that thinking blocks are present; confirm no mutating request exists in the page's network log; block YouTrack (bad URL) and confirm view shows stale badge instead of erroring.
- mcp-tools lane: e2e suite 31 green (or soft-skipped pre-deploy), health step passes, smoke unaffected.
- Ship Plane artifact re-published at the same URL with the refreshed facts and unchanged title.
