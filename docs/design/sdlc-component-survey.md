# Delivery record store: fleet-admin component survey

Task 288 · 2026-09-29

**Source:** plan `docs/handoffs/PLAN-delivery-record-store-2026-09-29.md` §"Front end and design system",
surveyed against `cognizioware-mcp-tools/infrastructure/docker/fleet-admin/web/src` (read-only; git origin
is `cogniziocompany/cognizioware-mcp-tools`, current checked-out branch `feat/drawio-export-mcp` at
`694039a`, which adds `drawio-export-mcp` only; `origin/main` is at `198941b`).

**What this is:** confirmation or correction of the plan's "Reuse first" component table and "Design system
findings" table, plus the `oklch-inside-hsl()` colour check and the location of the gateway alias
`postgressecondary`.

---

## 1. "Reuse first" component table — checked against fleet-admin source

| Need | Plan path | Findings (this survey) | Status |
|---|---|---|---|
| Stage colours | `pages/ShipPlanePage.tsx` (`stateBadgeClass()` with `Badge`) | Confirmed. `stateBadgeClass(state)` lives at `web/src/pages/ShipPlanePage.tsx:46`; it returns `bg-ship-red`, `bg-ship-amber`, `bg-ship-green` or `bg-ship-blue` tokens. `Badge` is imported from `components/ui/badge.tsx`. | ✅ confirmed |
| Stage order | `../src/overseer.js` (server side) — `SHIP_LADDER`, `shipStateForRow()` | Confirmed. `const SHIP_LADDER = ['QUEUED', 'RUNNING', 'GATED', 'BRANCHED', 'PUSHED', 'PR', 'REVIEWED', 'MERGED', 'LANE', 'LIVE', 'VERIFIED']` is at `src/overseer.js:43`. `shipStateForRow(entry, run)` is at `src/overseer.js:159`. `SHIP_TERMINAL = ['FAILED', 'BLOCKED']` at `:45`. Note the server-side path is `src/overseer.js`, not the relative `../src/overseer.js` in the plan's UI-facing table. | ✅ confirmed (path corrected) |
| Task table | `pages/ShipPlanePage.tsx` — "Task state board" table | Confirmed. The table is at `web/src/pages/ShipPlanePage.tsx:202` with inline comment `{/* Task state board. */}` and renders rows in `Card`/`Table` layout. It is hand-written per page with no shared `DataTable` component. | ✅ confirmed |
| Filters | `pages/OverseerPage.tsx` — `QueuePanel`, `LedgerPanel`, `AsksPanel` | Confirmed. `QueuePanel()`, `LedgerPanel()` and `AsksPanel()` are all defined in `web/src/pages/OverseerPage.tsx` (lines 114, 182, 231) and selected by tab state. | ✅ confirmed |
| Detail drawer | `components/ui/sheet.tsx`, used in `pages/RunPage.tsx` | Confirmed. `Sheet`, `SheetContent`, `SheetHeader`, `SheetTitle` are imported from `components/ui/sheet.tsx` and used in `web/src/pages/RunPage.tsx:200`. | ✅ confirmed |
| Document viewer | `components/` — `ArtifactViewer`, `MarkdownView` | Confirmed. `ArtifactViewer.tsx` and `MarkdownView.tsx` exist at `web/src/components/`. `ArtifactViewer` is used by `RunPage` for the detail drawer. | ✅ confirmed |
| Version differences | `components/` — `DiffView` | Confirmed. `DiffView.tsx` exists at `web/src/components/`. | ✅ confirmed |
| Event list | `pages/RunPage.tsx` — `EventsPanel`, `GatesPanel` | Confirmed. `EventsPanel()` and `GatesPanel()` are defined in `web/src/pages/RunPage.tsx` (lines 331 and 355) and rendered in tabs. | ✅ confirmed |
| Errors | `pages/ShipPlanePage.tsx`, `components/` — `errorMessage()`, `ErrorBoundary` | Confirmed. `errorMessage(err)` is at `web/src/pages/ShipPlanePage.tsx:19`; `ErrorBoundary.tsx` is at `web/src/components/`. | ✅ confirmed |
| Dates and status | `lib/format.ts` — `formatDate`, `formatDuration`, `statusColor` | Confirmed. `web/src/lib/format.ts` exports `formatDate(ts?)`, `formatDuration(ms)`, `statusColor(status?)` and `isRunning(status?)`. | ✅ confirmed |

### Plan-extracted paths vs. actual paths

* `../src/overseer.js` (plan relative path) → actual `src/overseer.js` from the UI source tree root, or
  `infrastructure/docker/fleet-admin/src/overseer.js` from the repo root.
* All other `web/src/...` paths match the checked-out source exactly.

---

## 2. New components — still needed

The plan's table of components that do **not** exist anywhere in the React admin centres remains true:

| Component | Why it is still needed |
|---|---|
| `StageLadder` | No component draws the eleven stages as steps; today each row shows one badge. |
| `Timeline` | No shared timeline component exists. |
| `DataTable` | Tables are hand-written per page with no sorting. The Ship Plane table is the obvious extraction source. |
| `AppNav` | No shared navigation; every page hard-codes its link pills. Three new pages make this unworkable. |

---

## 3. Design system findings — checked

| Finding | Plan detail | Survey result |
|---|---|---|
| Two conflicting specs | Code uses Geist, neutral colours and `ship-*` tokens. Untracked Penpot spec in `cognizioware/design/factory-specs/penpot/` uses IBM Plex and an ochre accent. Default: follow the code, since it is what ships. | **Code spec confirmed; Penpot path not found.** `package.json` declares `"@fontsource-variable/geist": "^5.3.0"` and `index.css` uses it. I searched `cognizioware-mcp-tools` for `cognizioware/design/factory-specs/penpot/` (exact) and for any Penpot design spec — the only Penpot references are the infrastructure service `infrastructure/penpot` and the `penpot-mcp-keepalive.sh` script. The plan's proposed Penpot directory does not exist in this repository. **Conclusion:** there is only the code spec in the repo; follow the code. |
| Possible colour bug | `tailwind.config.js` wraps colours as `hsl(var(--x))` while the variables hold `oklch(...)` values. Needs a browser check before relying on `bg-background` and similar. The `ship-*` tokens are consistent. | **Confirmed — real mismatch in source.** `web/tailwind.config.js` defines colours as e.g. `background: 'hsl(var(--background))'` and `ship-band: 'hsl(var(--ship-band))'`. `web/src/index.css` defines the core variables as `oklch(...)` (`--background: oklch(1 0 0); --foreground: oklch(0.145 0 0);` etc.) while the `ship-*` variables are in HSL notation (`--ship-band: 220 14% 96%;`). Tailwind's `hsl()` wrapper around an `oklch(...)` value is invalid CSS — browsers will drop the declaration, so classes like `bg-background` / `bg-foreground` are currently ineffective. The `ship-*` tokens are internally consistent (HSL → HSL) and therefore safe. This needs a fix (convert to `oklch()` wrapper or convert variables to HSL) before the new `TasksPage` relies on the neutral background/foreground tokens. |
| Dark theme | Defined but nothing switches it on. New components support both. | **Confirmed.** `web/src/index.css` has a full `.dark { ... }` block, but `main.tsx` and `App.tsx` do not call `document.documentElement.classList.add('dark')` or expose a toggle. No dark-mode switching code was found in `web/src`. New components should use the CSS variables so they inherit the theme class automatically. |
| No token document | Tokens live only in `index.css`. Task 8 adds `web/DESIGN-SYSTEM.md` covering tokens and the four new components. | **Confirmed.** No `DESIGN-SYSTEM.md` exists under `web/` or anywhere else in the repo (search returned zero matches). Tokens are only in `web/src/index.css`. |

---

## 4. Other admin centres

* **Ops control centre** is Blazor. Confirmed: `infrastructure/docker/ops-control-center/src/OpsControlCenter/OpsControlCenter.csproj` exists; its README describes it as the operations control centre. Its components **cannot** be reused in React; its layouts can be copied as patterns only.
* **Second admin centre** mentioned by Paxton on 2026-09-29 is **not identified** in the checked repository. I searched all `package.json` web roots and did not find a second React admin centre. No candidate closer than fleet-admin was found.

---

## 5. Accessibility reminder (plan)

* The stage ladder is an ordered list with the current step announced.
* Tables sort by keyboard.
* Stage is never shown by colour alone.

These constraints are not verified by static code inspection; they belong in the fleet-admin task 8b
implementation and test checklists.

---

## 6. Host of the secondary Postgres (`postgressecondary`)

Searched in `LongHorizon-Harness` and in `cognizioware-mcp-tools` (read-only), including origin/main.

* **Gateway alias:** `postgressecondary` maps to `postgres_mcp_secondary` in `cognizioware-mcp-tools/infrastructure/litellm-config.yaml` (origin/main: line 272; capture: line 254).
* **MCP server:** `postgres_mcp_secondary` is defined at line 1020 as `url: "http://postgres-mcp-secondary:8000/sse"`.
* **Container service:** `postgres-mcp-secondary` is defined in `cognizioware-mcp-tools/infrastructure/docker-compose.yml` (origin/main: line 1528). It is an in-stack `crystaldba/postgres-mcp` service on the `cognizioware-mcp-tools` docker network.
* **DSN env variable:** `POSTGRES_MCP_SECONDARY_DSN` in `cognizioware-mcp-tools/infrastructure/.env.example` (line 101) and in the compose environment at line 1581.
* **Actual Postgres host:** **not found.** No hostname, IP, or "CTxxx" designation for the secondary Postgres database itself appears in any checked repo config file. The env value is blank in the example and is gitignored in the live `mcp-tools.env`. The design-doc `mcp-placeholder-services-audit-and-plan.md` (2026-06-22) lists `postgres_mcp_primary` / `postgres_mcp_secondary` as **"BUILT-NOT-WIRED"** — composed and registered, idle until DSNs are filled.

**Survey conclusion:** `postgressecondary` resolves to the in-stack MCP server
`postgres_mcp_secondary` in the `cognizioware-mcp-tools` docker stack, but the actual Postgres
instance it points at is not declared in any checked repo configuration. The plan's open point
("Unverified: which host the secondary Postgres is, and its free space") remains open: task 1 must
confirm the host and free space before task 9 chooses it for nightly `sdlc` dump restore.

**Where I searched:**

1. `LongHorizon-Harness/` — grep for `postgressecondary` returned only two task `.txt` files in
   `tasks/`, no service config.
2. `cognizioware-mcp-tools/infrastructure/litellm-config.yaml` and the task-91 capture.
3. `cognizioware-mcp-tools/infrastructure/docker-compose.yml` (origin/main).
4. `cognizioware-mcp-tools/infrastructure/.env.example`.
5. `cognizioware-mcp-tools/design docs/` including `mcp-placeholder-services-audit-and-plan.md` and
   the deployed config capture.
6. `cognizioware-mcp-tools/README.md` and all `.md` files mentioning "secondary Postgres".

**Result:** `not found` for the actual secondary Postgres host; only the MCP proxy alias and service
are declared.
