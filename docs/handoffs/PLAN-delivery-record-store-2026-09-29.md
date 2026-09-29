# Delivery record store: capture the overseer queue's lifecycle history and keep tracking it

## Session & agent identity — for a reviewing agent

**This session**
| field | value |
|---|---|
| Agent name (address it with this) | None issued. No fleet or harness agent name was assigned to this session. |
| Short ref | `[f14d17]` |
| Session UUID | `f14d17b0-c2d2-562b-b268-73d77e3ce8f5` |
| Repo / cwd | `C:\Users\PaxtonTait\source` (not a git repository) |
| Role | Interactive Claude Code session working on ptait09 files, run in non-interactive harness mode. Not an overseer loop; no cron id. |

## Context

The overseer has queued and delivered about 265 tasks since 2026-09-05. The record of how each one was built lives in loose files under `C:\tmp` on one dev PC. Paxton wants that history kept, organised by lifecycle stage, and wants every future task tracked the same way automatically.

**What the audit found (measured 2026-09-29):**

| Finding | Detail |
|---|---|
| Size | `C:\tmp` holds 56,064 files, 24 GB. `C:\tmp\queue` holds 1,604 files, 2.1 GB. |
| Core records | 315 task specs, 319 live queue entries (+581 state variants), 1 ledger (35,320 lines, 1,774 tick rows), open asks, 24 handoffs, 6 plans and runbooks. |
| Duplication | 508 ledger copies (about 2.4 GB) and 64 near-identical run dumps (313 MB). |
| Post-cutover gap | After 2026-09-23 tasks were queued on CT110 directly. Tasks 230, 241–261, 263–270 and 286 have no local queue file; CT110 is their only full record. |
| No per-task view | A task's state exists only inside ledger narrative. There is no master table. |
| No PR link in run records | The harness stores no PR or evidence field. That lives only in the ledger. |
| Identifier problems | Task numbers are reused (208, 209, 217, 218, 224). Tick numbers restarted at 1 after the cutover. |
| Secrets | About 470 scratch scripts carry a hard-coded harness token; about 30 more files are secrets or contain key literals. Ledger, open asks, specs and queue entries are clean. |
| No write path | The six overseer archive tools on CT110 are read-only. Nothing but the dev PC can write a ledger row. |
| Hivemind | Live, but its harness feed has been dead since about 2026-09-05 and the overseer feed since 09-08. It trims documents to fragments. |

## Decisions (Paxton, 2026-09-29)

- **Store:** Git + Postgres + Hivemind, with fleet.easybutt0n.ai as the UI.
- **Scope:** everything, including per-tick scratch. Scratch is redacted first.
- **Builder:** harness queue tasks, each ending in a pull request.

**Answer to "redact, then save deltas with the core records?"** Yes, with two details:
- Redaction must run on ptait09 before anything leaves the machine. Harness runs on CT110 cannot read `C:\tmp`, and unredacted files should not be copied to reach them.
- "Deltas" means content-addressed storage. Each distinct file body is stored once by hash; every copy becomes a version row pointing at it. The 508 ledger copies reduce to the final ledger plus the few passages that were trimmed out of it.

## Design

### Who holds what

| Layer | Holds | Existing piece reused |
|---|---|---|
| Git | Whole documents, with version history | Apparatus layout in LongHorizon-Harness: `tasks/`, `queue/`, `docs/LEDGER.md`, `docs/handoffs/` |
| Postgres | Task index, links between records, writable ledger and open asks, file versions | `agent-db` on CT103, next to the `hivemind_sessions` and `fleet` schemas |
| Hivemind | Semantic recall of sessions and decisions | `memory-mcp` and `upsert_session_memory` (pgvector) |
| qdrant | Semantic search over whole documents, chunked | qdrant on CT103 :6333, running but unused today |
| UI | Per-task lifecycle view | fleet-admin Overseer and Ship Plane pages |

### Where the data sits (added after Paxton's note: "we have qdrant and multiple Postgres")

| Data | Primary | Second copy |
|---|---|---|
| Task index, links, ledger, open asks (`sdlc`) | `agent-db` on CT103 | Nightly dump restored to the secondary Postgres (the gateway's `postgressecondary` instance) |
| Queue events | `lh_harness` on CT103, unchanged | Read by `sdlc` through links, not copied |
| Document bodies | Git, plus `sdlc.blob` | Git remote on GitHub |
| Document vectors | qdrant collection `sdlc_documents` | Rebuildable from `sdlc.blob`, so no backup needed |
| Session memory vectors | Hivemind pgvector, unchanged | – |

- **Why split vectors:** Hivemind stores trimmed fragments per session. Whole specs and handoffs need chunking and a payload filter by task, stage and kind, which qdrant does well and which keeps Hivemind's table small.
- **Unverified:** which host the secondary Postgres is, and its free space. Task 1 must confirm both before choosing it.

### New Postgres schema `sdlc` (in `agent-db`)

| Table | Purpose | Key points |
|---|---|---|
| `task` | One row per piece of work | Surrogate `task_uid`. `task_number` + `slug` are attributes, not the key, because numbers are reused. |
| `artifact` | One row per logical file | `kind`: spec, queue_entry, handoff, plan, runbook, scratch, patch, evidence, run_dump, log, script. |
| `blob` | One row per distinct content | Keyed by sha256. Body stored once. |
| `artifact_version` | Each copy or backup of an artifact | Points at a `blob`; carries original path, write time, backup suffix. |
| `ledger_row` | One row per tick row or section | Key is (`session_ref`, `tick_number`, `occurred_at`), since tick numbers restarted. |
| `open_ask` | One row per ask | The seven existing columns plus state history. |
| `stage_event` | A task reaching a lifecycle stage | Stage, evidence reference, time. |
| `link` | Joins between records | Task to run id, queue id, PR, commit, gate id, session, artifact. |
| `import_batch` | Provenance of each load | Source host, counts, redaction report. |

**Lifecycle stages** reuse the ledger's own ladder, so no new vocabulary:
`QUEUED → RUNNING → GATED → BRANCHED → PUSHED → PR → REVIEWED → MERGED → LANE → LIVE → VERIFIED`, with `HELD`, `BLOCKED`, `FAILED` and `SUPERSEDED` off the main path.

### Redaction rules

- Replace matched secret values with `[REDACTED:<kind>]`; keep the rest of the file.
- Files that are themselves secrets (`.redis_pw`, `.ops_ingest_key`, `.tok*`, `_tnas_keys.txt` and similar) are recorded by name, size and hash only. No body is stored.
- The exporter writes a redaction report: file, kind, count. Never the value.
- A second independent scan runs over the finished bundle. Any hit fails the export.

### Going forward: automatic capture

| Event | Captured |
|---|---|
| Task enqueued | Spec text, queue entry, `QUEUED` stage event |
| Run launched, gated, finished | Stage events from `harness.queue_events` and the run's `report.json` |
| PR opened or merged | PR link and merge commit, which the harness does not record today |
| Overseer tick | Ledger row through a new write endpoint |
| Handoff or decision written | Document plus links to the tasks it names |

## API

Two services, each extending an API that already exists. Writes happen in one place only.

**Write and capture API: LongHorizon-Harness on CT110** (FastAPI, `src/lh_harness/webapi/server.py`, bearer token)

| Method and path | Purpose |
|---|---|
| `POST /api/sdlc/tasks` | Create or update a task record |
| `POST /api/sdlc/tasks/{task_uid}/stages` | Record a stage reached, with its evidence reference |
| `POST /api/sdlc/artifacts` | Store a document version (body, kind, links) |
| `POST /api/sdlc/links` | Join a task to a run, queue id, PR, commit or session |
| `POST /api/overseer/ledger` | Append a ledger row |
| `POST /api/overseer/open-asks` | Create or update an open ask |
| `POST /api/sdlc/import` | Load an exported bundle (used by the backfill) |

The same operations are exposed as MCP tools beside the six read tools in `overseer_state.py`, so the chat overseer and the gateway reach them as `hydrafleet-*`.

**Read API: fleet-admin** (Express, `src/server.js`, new router `buildSdlcRouter()` in `src/sdlc.js`, mounted at `/api/sdlc`, GET only, behind the existing read key)

| Path | Returns |
|---|---|
| `/api/sdlc/tasks?stage=&repo=&q=&from=&to=` | Task list with current stage |
| `/api/sdlc/tasks/{task_uid}` | One task: stages, links, documents |
| `/api/sdlc/tasks/{task_uid}/timeline` | Stage events and ledger rows in time order |
| `/api/sdlc/artifacts/{id}` and `/versions` | A document and its version history |
| `/api/sdlc/artifacts/{id}/diff?from=&to=` | Difference between two versions |
| `/api/sdlc/search?q=&mode=keyword\|semantic` | Keyword from Postgres, semantic from qdrant |
| `/api/sdlc/stats` | Counts by stage, repo and month |

**Conventions to follow:**
- fleet-admin reads the `sdlc` schema directly through `src/db.js`; both schemas are in `agent-db`.
- New tables go in `schema.sql` **and** in the copy inside `src/test/helpers.js`, which the tests use.
- Handlers use the existing `guarded()` and `relay()` wrappers and the 502/503/404 behaviour of `src/overseer.js`.
- **API documentation:** neither service has a checked-in spec today. Task 1 adds `docs/api/sdlc-openapi.yaml`, and the FastAPI routes publish it at `/openapi.json`.

## Front end and design system

The pages are built inside fleet-admin, which already shows the overseer, the Ship Plane and run detail. Stack: React 18, TypeScript, Vite, Tailwind, shadcn (`base-nova`).

**New pages** (added to `web/src/App.tsx`):

| Route | Page | Content |
|---|---|---|
| `/tasks` | `TasksPage` | Filterable table of all tasks with current stage |
| `/tasks/:taskUid` | `TaskPage` | Stage ladder, timeline, documents, links |
| `/records/search` | `RecordSearchPage` | Keyword and semantic search across documents |

**Reuse first** (Paxton: reuse the closest components from our admin centres):

| Need | Existing component | Path in `fleet-admin/web/src` |
|---|---|---|
| Stage colours | `stateBadgeClass()` with `Badge` | `pages/ShipPlanePage.tsx` |
| Stage order | `SHIP_LADDER`, `shipStateForRow()` | `../src/overseer.js` (server side) |
| Task table | "Task state board" table | `pages/ShipPlanePage.tsx` |
| Filters | `QueuePanel`, `LedgerPanel`, `AsksPanel` | `pages/OverseerPage.tsx` |
| Detail drawer | `Sheet` | `components/ui/sheet.tsx`, used in `pages/RunPage.tsx` |
| Document viewer | `ArtifactViewer`, `MarkdownView` | `components/` |
| Version differences | `DiffView` | `components/` |
| Event list | `EventsPanel`, `GatesPanel` | `pages/RunPage.tsx` |
| Errors | `errorMessage()`, `ErrorBoundary` | `pages/ShipPlanePage.tsx`, `components/` |
| Dates and status | `formatDate`, `formatDuration`, `statusColor` | `lib/format.ts` |

**New components** (only what does not exist anywhere):

| Component | Why it is needed |
|---|---|
| `StageLadder` | Today each row shows one badge. Nothing draws the eleven stages as steps. |
| `Timeline` | Event lists exist, but no shared timeline. |
| `DataTable` | Tables are hand-written per page with no sorting. Extracted from the Ship Plane table. |
| `AppNav` | There is no shared navigation; every page hard-codes its link pills. Adding three pages makes that unworkable. |

**Design system findings to settle in task 1:**

| Finding | Detail |
|---|---|
| Two conflicting specs | The code uses Geist, neutral colours and `ship-*` tokens. The untracked Penpot spec in `cognizioware/design/factory-specs/penpot/` uses IBM Plex and an ochre accent. Default: follow the code, since it is what ships. |
| Possible colour bug | `tailwind.config.js` wraps colours as `hsl(var(--x))` while the variables hold `oklch(...)` values. Needs a browser check before relying on `bg-background` and similar. The `ship-*` tokens are consistent. |
| Dark theme | Defined but nothing switches it on. New components support both. |
| No token document | Tokens live only in `index.css`. Task 8 adds `web/DESIGN-SYSTEM.md` covering tokens and the four new components. |

**Other admin centres:** the ops control centre is Blazor, so its components cannot be reused in React; its layouts can be copied as patterns. The second admin centre Paxton mentioned on 2026-09-29 is still not identified. Task 1 surveys both and lists any closer components before task 8 starts.

**Accessibility:** the stage ladder is an ordered list with the current step announced; tables sort by keyboard; stage is never shown by colour alone.

## Overseer runtime: Open WebUI Computer as the link to Claude Code Teams

**The problem** (from the chat overseer's own account): Open WebUI automations are stateless. Every scheduled tick starts a fresh chat with no memory, so the overseer has to rebuild its state from files each time.

**What Open WebUI offers** (read from docs.openwebui.com on 2026-09-29; none of it tested here):

| Product | What it is | Fit |
|---|---|---|
| **Open WebUI Computer** (`cptr`) | Serves one machine to a browser: files, editor, git, and a shell that keeps running after the tab closes. Has scheduled tasks, messaging bots, and can act as a model provider for Open WebUI. Built for one trusted owner per machine. | **Chosen.** Matches a VM per user. |
| Open Terminal | A shell and file API the chat model can call. MIT licence. Its multi-user mode gives no security boundary between users. | Useful as a tool, but not a persistent agent. |
| Terminals | Per-user Open Terminal containers, managed. Requires an Open WebUI Enterprise licence. | Not used: licence cost, and we chose VMs. |

**Why Computer fits:**
- **Persistent session.** The agent and its shell survive between ticks, which automations cannot do.
- **Runs in the browser.** Its interface is a web page served from the machine, so it opens from chat.easybutt0n.ai like any other link.
- **Model provider.** It can appear in the chat.easybutt0n.ai model picker, giving direct chat with the overseer.
- **Same isolation model.** One owner per machine is exactly one VM per user.

### Roles (corrected by Paxton, 2026-09-29)

**Nothing is replaced except the 12-minute automation tick.** Guacamole and every Claude Code Teams capability stay.

| Piece | Role | Status |
|---|---|---|
| Claude Code Teams orchestrator | **The primary overseer.** Holds the doctrine, the task list and the routines. | Kept whole, as upstream |
| Its channels: Telegram, Slack, Discord, iMessage | Ways to reach the overseer | Kept, unchanged |
| Open WebUI Computer | **Added** channel: direct chat from chat.easybutt0n.ai, plus the browser shell | New, through our plugin |
| Apache Guacamole | Full remote desktop with all its features | **Kept. Not under review.** |
| Hydra | Live terminal view and steering | Kept |
| Gateway MCP tools | Infrastructure access for the overseer | Added through our plugin's `.mcp.json` |
| Open WebUI 12-minute automation (`6aef9800…`, preset `overseer-sweep`) | Today's stateless overseer tick | **Retired**, after the new overseer has run beside it and Paxton gives the go |

### What already exists in the Claude Code Teams repo

| Item | State upstream |
|---|---|
| `agents/orchestrator/tasks.md` | Exists, but is a 21-byte stub. It is an empty task list to fill, not a working overseer. |
| `routines/` with one `.md` per recurring task and cron frontmatter | Described in the README; the folder is created during setup. This is what replaces the 12-minute tick. |
| `agents/orchestrator/CLAUDE.md` | 1.7 KB role definition ("chief of staff"). Holds no overseer doctrine of ours. |
| Fleet MCP: start, message, compact, context-check, recall, create-agent | Present |
| `.mcp.json.example` | Present; our gateway entry is added here |

### Is it plug and play?

Partly. Three parts are configuration, two are real work.

| Part | Effort |
|---|---|
| Gateway MCP access | Configuration: one entry in `.mcp.json` with a scoped key |
| Existing channels | Configuration: upstream setup wizard |
| Record-store capture | Configuration, once the write API (task 5) exists |
| **Overseer doctrine** | **Work.** The 40 KB `LOOP-PROMPT.md` and the chat overseer's standing instructions have to be rewritten as the orchestrator's `CLAUDE.md`, `tasks.md` and routines. Upstream ships none of it. |
| **Computer to orchestrator link** | **Work, and unverified.** Whether Computer can hand a chat message to an orchestrator already running in tmux is not known. The spike answers it. |

### Channels and Remote Control (Paxton, 2026-09-29)

**No custom channel server is built.** The earlier fallback idea, and the custom channel in the handoff's section 5a, are dropped. Claude Code Teams uses the channels it ships with.

| Channel | Source | Our work |
|---|---|---|
| Telegram, Discord, iMessage | Claude Code's own channel plugins | Configure only |
| Slack | The repo's `mcp/slack-channel` server | Configure only |
| Claude Code Remote Control | Built into Claude Code; links the session to Anthropic's servers so it can be reached from claude.ai and the Claude apps | **Verify it works from the VM** |
| Open WebUI Computer | Direct chat from chat.easybutt0n.ai | Configure through our plugin, if the spike shows it can reach the orchestrator |

"rc" is Claude Code Remote Control (confirmed by Paxton).

**What was dropped, exactly:** one item, which was only ever a proposal of mine and was never built. It was a new channel server, modelled on the repo's Slack channel, to carry messages between chat.easybutt0n.ai and the orchestrator. The "fallback channel server" was the same idea under another name. No channel that ships with Claude Code Teams was dropped.

**Remote Control checks on the pilot VM:**

| Check | Passes when |
|---|---|
| Outbound access | The VM reaches Anthropic's servers over HTTPS through our network, including Pi-hole DNS and any egress rules |
| Login | The user's Claude login on the VM is valid and survives a reboot |
| Session link | The orchestrator's session appears in claude.ai and accepts a message sent from there |
| Persistence | The link re-establishes after the tmux session or the VM restarts |
| Coexistence | Remote Control and the other channels work at the same time |

### Requirement: Claude bridge from the overseer to any fleet device (Paxton, 2026-09-29)

**The overseer must be able to ask a question of, or hand work to, a Claude Code session on any fleet device that has a working Claude.** Device type and operating system must not matter.

**What exists today:**

| Piece | What it does | Limit |
|---|---|---|
| `claude-session-bridge` script (in the `remote-pc` skill's `bin/`) | Resumes a session by id on another host and returns the answer | A script run by hand, with a fixed host list |
| Runner tools `<host>-claude_prompt` and `<host>-claude_session` on the gateway | One-shot prompt or session resume on a device | One tool set per host, so the caller must know the host |
| `/claude-bridge` skill | Finds work other sessions expect the overseer to run and turns it into a queue task | Reads transcripts on the machine it runs on |

**Known constraints, from the `remote-pc` skill doc:**
- Windows runners run as SYSTEM. They can only resume sessions created under that same user, not sessions from a desktop login.
- Calls longer than about 100 seconds must be asynchronous, or the tunnel times out.
- `claude` is not on the runner user's path on Windows; the full path is needed.

**Recommendation: a fleet-level MCP tool, not a new service.** The fleet plane already knows every device and already holds each runner's credentials, so the bridge belongs there. A separate service would duplicate both.

| New tool (on the `hydrafleet` plane) | Purpose |
|---|---|
| `list_claude_devices` | Devices with a working Claude, with version and login state |
| `list_claude_sessions(device)` | Sessions the bridge can reach on that device |
| `claude_bridge_ask(device, question, session_id?)` | Ask and get the answer; starts a session if none is given. Asynchronous, returns a task id. |
| `claude_bridge_result(task_id)` | Collect the answer |

**What "working Claude" means, checked per device and reported in the fleet registry:**

| Check | Passes when |
|---|---|
| Installed | The runner finds the `claude` binary and reports its version |
| Logged in | A trivial prompt returns an answer |
| Reachable | The device is online in the fleet and its runner responds |

**Acceptance:** from the overseer, one `claude_bridge_ask` call succeeds against a Windows device, a Linux device and the pilot Ubuntu VM, with no host-specific code in the caller.

**If Computer cannot reach the orchestrator,** nothing is built to bridge it. Direct chat from chat.easybutt0n.ai is then reported to Paxton as not available, and the overseer is reached through its own channels and Remote Control.

**How it connects:**

```
chat.easybutt0n.ai (Open WebUI 0.11.0 on corsairai300)
      |  model picker entry: "Overseer"
      v
Open WebUI Computer on the user's Ubuntu VM   <-- our plugin configures this
      |  persistent shell + scheduled tasks
      v
Claude Code Teams fork (orchestrator in tmux)
      |  our plugin bundle: gateway tools, sdlc capture, hooks
      v
LiteLLM gateway, harness queue, record store
```

**Our plugin bundle gains one part:** a Computer connector that installs `cptr`, registers it with chat.easybutt0n.ai as a model provider, and points its scheduled tasks at the orchestrator.

**Not verified, to be settled by the spike (task 11):**

| Question | Why it matters |
|---|---|
| Release status | Paxton believes it is a preview feature. The docs I read do not state preview, beta or stable. |
| Licence | The docs refer to the Open WebUI licence without naming terms for Computer. |
| Driving Claude Code Teams | The docs say it supports Claude Code by subscription or key. Whether it can attach to an orchestrator already running in tmux is unknown. |
| Long tasks over the model-provider link | Whether replies that arrive later reach the chat. No custom channel is built either way. |
| Unattended use of a Claude subscription | The overseer runs around the clock on a user's login. The handoff already lists this as unconfirmed against the subscription terms. |
| Exposure | Its docs say to treat access like SSH and keep it on a private network. It would sit behind Cloudflare Access, never open to the internet. |

**Source pages:** `docs.openwebui.com/ecosystem/computer/`, `/ecosystem/computer/choose/`, `/features/open-terminal/`, `/features/open-terminal/terminals/`, `github.com/open-webui/open-terminal`.

## Open WebUI document storage (Knowledge) as the reading surface

Added at Paxton's request, 2026-09-29. Facts are from docs.openwebui.com and a read-only check of the live container; nothing was changed.

| Fact | Detail | Basis |
|---|---|---|
| What it is | Knowledge bases: documents made searchable to chat models, by semantic search, exact text search and page-by-page reading | docs |
| How documents get in | Upload, "sync directory", and an API: `POST /api/v1/files/`, `POST /api/v1/knowledge/{id}/file/add`, `POST /api/v1/knowledge/{id}/sync/diff` | docs |
| Sync from Git | A separate tool, `oikb`, syncs from GitHub, S3 and others. Recommended by the docs for thousands of files. | docs |
| Vector stores | ChromaDB and PGVector are officially maintained; qdrant is supported as a non-core integration | docs |
| Versions | **None.** Older versions of a file are not kept. | docs |
| Our instance today | Open WebUI 0.11.0. Knowledge is effectively empty: uploads 4 KB, vector data 408 KB. Embedding model `nomic-embed-text:latest` through LiteLLM. | measured |

**Role in the design: the place people and chat models read documents, not the system of record.** It keeps no version history, so Git and `sdlc` stay authoritative and Knowledge is filled from them.

| Knowledge base | Filled from | Holds |
|---|---|---|
| `delivery-handoffs` | `LongHorizon-Harness/docs/handoffs/` | Handoffs, plans, runbooks |
| `delivery-specs` | `sdlc` export of task specs | One document per task spec |
| `overseer-doctrine` | The orchestrator's instructions in the fork | Standing rules and routines |
| `delivery-ledger` | `sdlc.ledger_row`, exported by month | Tick history, readable in chat |

**To investigate (task 289):**

| Question | Why it matters |
|---|---|
| Can `oikb` or the sync API run on a schedule from CT110 with no ptait09 involvement? | Decides whether filling is automatic |
| Should Knowledge use qdrant or its default store? | One vector store to run, or two |
| The embedding model differs from Hivemind's (`nomic-embed-text:latest` against `nomic-embed-text-v2-moe`) | Vectors from one cannot be searched by the other |
| Access control per knowledge base | Per-user instances must not read each other's documents |
| Behaviour with 35,000 ledger lines | Size and retrieval quality |
| Redaction | Only redacted content may be loaded |

## Tasks queued on approval

Following `HANDOFF-queue-intake-for-experts-2026-09-29.md`. The latest task in the live queue is 287, so these take the next two numbers.

| Task | Title | Repo | Retires from the handoff's section 3 table |
|---|---|---|---|
| **288** | Land this plan as a repo document; write the record-store design, `migrations/004_sdlc.sql` (dry run) and the API spec | `LongHorizon-Harness` | Designs the replacement for: ledger, open asks, handoffs, task spec files, legacy queue folders. Retires none by itself. |
| **289** | Investigate Open WebUI Knowledge as the document reading surface; answer the six questions above; documents only | `cognizioware-hydra` (where Open WebUI resources are declared) | Designs the replacement for: handoffs, overseer doctrine, chat overseer tick rows. Retires none by itself. |

**Enqueue settings:** node `ct110`, trio `kimi`, priority 100, 6 rounds, dedup keys `task-288-sdlc-record-store-design-2026-09-29` and `task-289-openwebui-knowledge-investigation-2026-09-29`.

**Input delivery:** the plan text is copied to CT110 at `/home/harness/work/plans/` with a checksum, as done for task 286. No `C:\` path is cited in either spec.

**Three things Paxton should know before these run:**

| Issue | Detail |
|---|---|
| Runs are failing | Task 286 failed three times between 03:03Z and 04:01Z. Tasks 224, 267, 268 and 269 failed repeatedly in the same window. The handoff records that every cloud model account returns 429 and only local `qwen3.8` answers. New `kimi` tasks are likely to fail or wait until capacity returns. I have not read the failure reason of each run. |
| Ledger row | The handoff says to append a ledger row on ptait09 after queuing. Paxton's rule says nothing new is saved in `C:\tmp`. The rule wins: no row is written, and the task numbers are reported to Paxton directly. |
| VM 211 and the handoff's author | The handoff names my session `[f14d17]` as its author and says an overseer VM 211 (`cct-overseer-01`, 192.168.21.171) is being built. VM 211 exists and is running on corsairai300. Neither was created in this conversation, so another session did that work. This plan's pilot (task 13) uses VM 211 and does not create a second VM. |

## Dev cycle and where pull requests go

**Rule (Paxton, 2026-09-29): every pull request is opened against the repo the task is for.** A task that touches two repos is split into two tasks.

| Work | Repo |
|---|---|
| `sdlc` schema, write API, exporter, loader, auto-capture | `LongHorizon-Harness` |
| fleet-admin read API and pages, Hivemind feeds, qdrant indexing, gateway skills | `cognizioware-mcp-tools` |
| Plugin bundle, thin SDK, Computer connector, overseer doctrine, pilot. No channel code. | The Claude Code Teams fork |
| Open WebUI resources (presets, automation retirement) | `cognizioware-hydra`, where they are declared today |

**Local copy on ptait09 (Paxton, 2026-09-29), done by this session on approval:**

| Item | Value |
|---|---|
| Path | `C:\Users\PaxtonTait\source\claude-code-teams` |
| Method | `git clone https://github.com/VantaSoft/claude-code-teams.git`, full history kept |
| Remotes | `upstream` = VantaSoft. `origin` is pointed at our fork once task 0 creates it. |
| Not done | The upstream `install.sh` is not run, because it strips `.git` and the `workshop/` and `intro/` folders. Nothing is installed or launched, and no agent is started with permissions skipped. |
| Purpose | A development and reading copy. ptait09 runs no part of the overseer. |

An earlier attempt to run the installer on ptait09 was blocked by the permission classifier as external code. A plain clone downloads files and executes nothing. If the clone is also blocked, I report that and Paxton runs the one command.

**Task 0, before the fork tasks can run:** create the fork of `VantaSoft/claude-code-teams` in the `cogniziocompany` GitHub organisation and add it as a workspace on CT110. Creating a repo is outward-facing, so it waits for Paxton's go on name and visibility (default: private, named `claude-code-teams`).

**One correction to work already running:** task 286 is landing the Claude Code Teams handoff and design answers in `LongHorizon-Harness`, because the fork did not exist when it was queued. Once the fork exists, task 12 moves those two documents into it.

**The cycle, per task:**

| Step | What happens | Limit |
|---|---|---|
| 1 Spec | Short spec with repo, branch, deliverables and hard rules, sent in the enqueue call | One deliverable per task |
| 2 Build | Harness run on CT110 in that repo's workspace | 6 rounds |
| 3 Check | Tests run in the task; results pasted in the pull request, since CI does not run them | – |
| 4 Pull request | One, against the task's own repo. No merge by the run. | – |
| 5 Gate | Reviewed and merged by the overseer or Paxton | – |
| 6 Deploy | Through the existing lanes, never by a harness run | – |
| 7 Record | Stage events captured in `sdlc` once task 6 is live | – |

**What makes it quick:** tasks in different repos run at the same time, because the harness allows one run per workspace and five runs in total. Three tracks can move in parallel.

| Track | Repo | Order |
|---|---|---|
| A | `LongHorizon-Harness` | 1 → 2 → 3 → 4 → 5 → 6 → 9 |
| B | `cognizioware-mcp-tools` | 7, 8a → 8b, 10 (start once task 3 is merged) |
| C | Claude Code Teams fork | 0 → 11 → 12 → 13 → 14 |

## Build sequence (harness queue tasks)

Each task is one pull request, no merge, no deploy, with the hard rules used in task 286. Spec text is passed directly in the `hydrafleet-enqueue_task` call on `ct110`. No spec file is written to `C:\tmp`.

| # | Task | Repo | Depends on |
|---|---|---|---|
| 1 (queued as **288**) | Design doc, `migrations/004_sdlc.sql` (dry run by default), OpenAPI spec, and a component survey of the admin centres | LongHorizon-Harness | – |
| (queued as **289**) | Open WebUI Knowledge investigation | cognizioware-hydra | – |
| 2 | Exporter and redactor `scripts/sdlc_export.py`: classify by the audited naming patterns, redact, hash, write a bundle with a JSONL manifest. Tests use fixtures containing fake secrets. | LongHorizon-Harness | 1 |
| 3 | Loader `scripts/sdlc_load.py`: bundle into `sdlc` tables; parse the ledger into `ledger_row`; extract run ids, PR numbers and queue ids into `link`; refresh the apparatus folders | LongHorizon-Harness | 1, 2 |
| 4 | CT110 backfill: pull queue entries and run reports for the tasks that have no local record | LongHorizon-Harness | 3 |
| 5 | Write path: `POST /api/overseer/ledger` and `/open-asks`, plus MCP tools `append_ledger_row` and `upsert_open_ask`, next to the read tools in `src/lh_harness/overseer_state.py` | LongHorizon-Harness | 1 |
| 6 | Auto-capture hooks on queue and run events, including PR link lookup | LongHorizon-Harness | 1, 5 |
| 7 | Hivemind: repair the harness and overseer feeds. qdrant: create `sdlc_documents`, chunk and embed documents from `sdlc.blob`, expose a search tool on the gateway | cognizioware-mcp-tools | 3 |
| 11 | Spike, documents only: answer the open Open WebUI Computer questions from its docs, source and licence, above all how it hands chat to a running orchestrator. No install. | Claude Code Teams fork | 0, task 286 done |
| 12 | Overseer doctrine port: rewrite `LOOP-PROMPT.md` and the chat overseer's standing instructions as the orchestrator's `CLAUDE.md`, `tasks.md` and routines, inside our plugin bundle | new fork repo | 11 |
| 13 | Pilot, after Paxton approves the spike: the existing VM 211 (`cct-overseer-01`) on corsairai300 with Guacamole, Computer, the Claude Code Teams fork and our plugin, behind Cloudflare Access | new fork repo | 11, 12 |
| 14 | Side-by-side run: the new overseer and the 12-minute automation both tick; compare ledger rows and gate decisions. The automation is retired only on Paxton's go. | Claude Code Teams fork (comparison report); `cognizioware-hydra` (retirement, as its own task) | 5, 13 |
| 15a | Runner: report Claude capability (installed, logged in, version) in `host_info` | runner repo (`ptait09-easybutt0n-ai`) | – |
| 15b | Fleet plane: the four Claude bridge tools, device-agnostic, asynchronous | `cognizioware-hydra` (unverified: confirm which repo owns the `hydrafleet` MCP before queuing) | 15a |
| 15c | Plugin: expose the bridge tools to the orchestrator and add the bridge step to its routines | Claude Code Teams fork | 0, 15b |
| 9 | Nightly `sdlc` dump to the secondary Postgres, with a restore check | LongHorizon-Harness | 3 |
| 8a | fleet-admin read API: `src/sdlc.js` router, schema additions, API tests | cognizioware-mcp-tools | 3 |
| 8b | fleet-admin pages and the four new components, `DESIGN-SYSTEM.md`, front-end tests | cognizioware-mcp-tools | 1, 8a |

**Steps run by a session on ptait09, not by the harness:**
1. Run the exporter in dry-run mode and review the redaction report.
2. Run it for real and copy the redacted bundle to CT110.
3. Write the raw, unredacted `C:\tmp` to a read-only archive on terranas01 with restricted access.
4. Run the clear-out below, once its gates pass.

## Clearing out `C:\tmp` (required by Paxton, 2026-09-29)

`C:\tmp` is emptied as the last phase. Deletion cannot be undone, so it happens only after every gate below passes, and in the order shown.

**Gates before any deletion:**

| Gate | Check |
|---|---|
| G1 Loaded | Verification items 1–4 pass: counts match and post-cutover tasks are present. |
| G2 Clean | Secret scan over bundle and database returns zero hits. |
| G3 Archived | The raw archive on terranas01 lists the same file count and total size as `C:\tmp`, and a sample of 50 files restores with matching hashes. |
| G4 Second copy | The `sdlc` dump restores on the secondary Postgres. |
| G5 Nothing still reads it | No running process, scheduled task or harness entry refers to a `C:\tmp` path (see the next section). |
| G6 Paxton's go | Explicit approval, given after seeing the G1–G5 results. |

**Deletion order:**
1. Duplicates first: the 508 ledger copies and the run dumps (about 2.9 GB).
2. Per-tick scratch and logs.
3. Core records: specs, queue folders, ledger, open asks, handoffs.
4. Everything else in `C:\tmp`.

**Items in `C:\tmp` that are not overseer records** get checked before step 4, because the archive may not be the right home for them:

| Item | Check before deleting |
|---|---|
| Git worktrees and clones (most of the 115 folders) | Any uncommitted or unpushed work is listed for Paxton first. |
| `qwen36-24g-edit.gguf` (16.6 GB model file) | Confirm nothing loads it from this path. It is not copied to the archive unless Paxton asks. |
| n8n and TRMS execution dumps | Archived, not loaded; they contain key values. |

## Removing ptait09 from the architecture

After this plan, ptait09 is a fleet device only: reachable through its runner and Hydra agent, with no role in the queue, the overseer or the record store. This completes the existing plan in `C:\tmp\HANDOFF-ptait09-disconnect-plan-2026-09-23.md`, whose queue half (task 168) is already done.

| Dependency on ptait09 today | Replacement | Task |
|---|---|---|
| Task specs written to `C:\tmp\*-task.txt` | Spec text sent in the enqueue call and stored in `sdlc` | 6 |
| Queue entries carry `task_file: "C:/tmp/..."` | Loader rewrites to a `sdlc` artifact reference | 3 |
| `enqueue.py` moves files into `queue\imported` | Retired; `hydrafleet-enqueue_task` is the only enqueue path | 10 |
| Ledger and open asks written on the PC | Write endpoint on CT110 | 5 |
| Handoffs written to `C:\tmp\HANDOFF-*.md` and picked up by `/claude-bridge` | Handoffs stored through the write path; the bridge step reads from CT110 | 10 |
| Apparatus bundle synced by hand from ptait09 | Refreshed by the loader, then by auto-capture | 3, 6 |
| `LOOP-PROMPT.md` doctrine on the PC | Moved to the gateway `lh-orchestrator` skill, as task 277's design already proposes | 10 |
| Primary overseer loop running as a session on ptait09 | Stays stopped; the enhanced overseer (Claude Code Teams, task 286) replaces it | separate work |
| Workspace hook pointing at a Windows path, which errors on every CT110 shell call | Path fixed or hook removed | 10 |
| Retired scripts holding the harness token (`launch_queue.py.*`, `quota_resume*.py`, watchdog) | Archived, then deleted with `C:\tmp` | clear-out |

**Task 10 (added):** retire the PC-side paths listed above and update the skills and docs that still point at `C:\tmp`. Split by repo: 10a in `LongHorizon-Harness`, 10b in `cognizioware-mcp-tools`.

## Rule effective immediately: nothing new is saved in `C:\tmp`

Paxton, 2026-09-29. This applies from the moment the plan is approved, not after the clear-out.

| Was written to `C:\tmp` | Goes here instead, until the store is live | Once the store is live |
|---|---|---|
| Task specs | Sent as text in the `hydrafleet-enqueue_task` call; nothing on disk | `sdlc` artifact, captured on enqueue |
| Handoffs and plans | A pull request to `LongHorizon-Harness/docs/handoffs/` | Write API |
| Ledger rows and open asks | None written from ptait09. The overseer tools on CT110 are read-only until task 5, so rows wait for it. | Write API |
| Export bundle and redaction report | `C:\Users\PaxtonTait\sdlc-export\` on ptait09, deleted after transfer | – |
| Scratch for this session | The session's own scratch directory | – |

**First steps on approval, before any queue task:**
1. Replace my saved note about handing work over through `C:/tmp/HANDOFF-*.md` plus a ledger row. It is wrong under this rule.
2. Save this rule as a standing instruction for future sessions.
3. Write no further files to `C:\tmp`. The files this session already put there (the renamed handoff, the task 286 spec and one ledger row) stay until the clear-out, so the audit counts remain valid.

**Known cost:** between approval and task 5, overseer ledger rows have nowhere to go. The primary loop is already stopped, so the only writer affected is the chat overseer, which writes through CT110 and not to `C:\tmp`.

## Existing code to reuse

| Need | Reuse | Path |
|---|---|---|
| Archive layout and read tools | `overseer_state.py` | `LongHorizon-Harness/src/lh_harness/overseer_state.py` (origin/main) |
| Queue audit trail | `harness.queue_events` | `LongHorizon-Harness/migrations/001–003_*.sql` |
| Run outcome | `report.json`, `rounds.jsonl`, `approvals.jsonl` | `<runs_root>/<run_id>/lh_harness/` |
| Embedding and upsert | `upsert_session_memory` | `cognizioware-mcp-tools/infrastructure/docker/memory-mcp/schema.sql` |
| Run ingest pattern | `ingest-harness.js` | `cognizioware-mcp-tools/infrastructure/docker/memory-mcp/` |
| UI shell and overseer proxy | `overseer.js`, `OverseerPage.tsx` | `cognizioware-mcp-tools/infrastructure/docker/fleet-admin/` (origin/main) |
| Secret scan | `secret_scan.sh` if present | `LongHorizon-Harness/scripts/deploy/` |

## Constraints

- **Database changes on CT103** go to UAT first, as a script with a dry run by default, applied in an overseer window.
- **CT110 restarts** need drain on and no active runs.
- **Queue order:** Paxton's rule places fleet-plane work after powerplatform tasks. These tasks are enqueued at priority 100 and no other task is reordered.
- **One run per workspace:** tasks 1–6 share the LongHorizon-Harness workspace, so they run one after another.
- **The primary overseer loop is stopped** (tick #145). Gates on these tasks need the chat overseer or Paxton.
- **The local LongHorizon-Harness checkout** is on `feat/spec-staging`, 174 commits behind main with uncommitted edits. It is not touched; harness runs use CT110's own checkout.

## Open points

- **Raw archive location.** Default: terranas01, restricted. It contains live secrets, so it is not loaded into any searchable store.
- **The five reused task numbers.** The loader splits them into separate tasks by slug; early ids such as `05h3b` stay as-is, unmapped.
- **Fleet-admin artifact retention** is 30 days today. `sdlc` has no expiry.

## Verification

1. **Counts match the audit:** 315 specs, 319 live queue entries, 1,774 tick rows, 24 handoffs, 508 ledger copies reduced to far fewer blobs.
2. **Secret scan over the bundle and the database** returns zero hits for the harness token pattern, `sk-` literals and private-key headers.
3. **Trace one task end to end.** Task 283: spec, queue id `q-47bba3ef9ffe47e8`, run `20260928T202906Z_3b18fbe0`, PR #61, merge `2bbd7ed`, ledger rows. All reachable from its task page.
4. **Post-cutover coverage:** tasks 241–270 appear with data sourced from CT110.
5. **Semantic search:** a qdrant search for "subscription capacity failover" returns the task 282 and 283 documents; a Hivemind `recall` returns the related session memories.
5a. **Second copy:** the nightly dump restores on the secondary Postgres and its row counts match the primary.
6. **Write path:** `append_ledger_row` adds a row that `read_ledger` returns.
7. **Auto-capture:** enqueue a trivial test task and confirm its spec, stage events and PR link appear with no manual step.
7a. **API tests:** `npm test` in fleet-admin and the harness's pytest suite pass, including the new `sdlc` cases. These do not run in CI today, so each task pastes the result in its pull request.
7b. **Front end:** `npm run test:web` passes; the task 283 page is opened in a browser at fleet.easybutt0n.ai and shows its ladder, timeline and documents.
7c. **Channels and Remote Control on the pilot VM:** the five Remote Control checks pass, and a message sent through each configured channel gets a reply from the orchestrator.
7d. **Claude bridge:** the acceptance test in the bridge section passes on a Windows device, a Linux device and the pilot VM.
8. **ptait09 independence:** with the ptait09 runner stopped for the test, enqueue a task, let it run to its gate, write a ledger row and read a handoff. All succeed.
9. **No remaining references:** a search of both repos, the gateway skills and CT110's config finds no `C:\tmp` or `C:/tmp` path.
10. **Clear-out:** `C:\tmp` is empty, and the task 283 trace in item 3 still works from the store alone.
