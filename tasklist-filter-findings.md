# LHH-TASKLIST-FILTER — Exploration Findings

**Round:** Re-add 2026-10-17, after q-5d8e11448dd9431c failure
**Source commit:** 175f8ec (current HEAD)
**Workspace:** /home/harness/work/LongHorizon-Harness
**Branch:** lh-run/minimax-f9032c15

---

## 1. Existing PR / Branch check

- `gh pr list --search "LHH-TASKLIST-FILTER" --state all` returned **no results**.
  - `gh auth status` confirmed GitHub login OK (account `prax211` via GH_TOKEN).
- `git ls-remote --heads origin | grep -i tasklist` returned **no matches**.
- `git branch -a | grep -i tasklist` returned **no matches** in local or remote branches.
- **Verdict:** No prior PR and no existing branch. Safe to create a new branch and PR.

---

## 2. Web app entry point

- **HTML host:** `frontend/web/index.html` → mounts React into `<div id="root">` from `/src/main.tsx`.
- **React mount:** `frontend/web/src/main.tsx` → renders `<App />` (or `<AdminApp />` for `/admin` paths) inside a `CrashBoundary`.
- **Top-level App component:** `frontend/web/src/App.tsx` (179,874 bytes — the largest source file in the repo). This is where the left sidebar lives.
- **Other UI:** `frontend/web/src/AdminApp.tsx` (admin screens, separate from the user workbench sidebar).
- **i18n:** `frontend/web/src/i18n.tsx` provides `language` (zh | en) and `text(zh, en)` for the UI.
- **Frontend package:** `frontend/web/package.json` (React 18 + Vite + TS; test runner: `node scripts/test.mjs`).

---

## 3. Status vocabulary (the source of truth for the colored dots)

The canonical status vocabulary used by the colored dots and the entire frontend is defined here:

- **`statusClass(status)`** — `frontend/web/src/App.tsx:199-201`
  ```ts
  function statusClass(status: string): string {
    return `status-dot status-${status.replaceAll('_', '-')}`;
  }
  ```
  Produces CSS classes like `status-dot status-running`, `status-dot status-waiting-approval`, `status-dot status-completed`, `status-dot status-failed`, etc.

- **`statusLabel(status, ...)`** — `frontend/web/src/App.tsx:185-197` — bilingual labels for both EN and ZH. Confirms the canonical status names recognized by the UI:
  ```
  starting, creating, running, stopping, aborting,
  waiting_approval, completed, complete, failed, blocked,
  incomplete, cancelled, canceled, stopped, aborted, idle
  ```

- **`terminalLifecycle(status)`** — `frontend/web/src/App.tsx:203-205` — canonical set of terminal statuses:
  ```ts
  new Set(['completed', 'complete', 'success', 'succeeded', 'done', 'finished',
           'failed', 'failure', 'blocked', 'incomplete',
           'cancelled', 'canceled', 'stopped', 'aborted']).has(...)
  ```

- **`stoppingLifecycle(status)`** — `frontend/web/src/App.tsx:207-209` — set used for in-progress stop:
  ```ts
  new Set(['stopping', 'aborting', 'stop_requested', 'abort_requested']).has(...)
  ```

- **`RunStatus` type** — `frontend/core/src/types.ts:1`:
  ```ts
  export type RunStatus = 'idle' | 'running' | 'waiting_approval' | 'completed' | 'failed' | 'cancelled' | string;
  ```
  Note: union with `string` is a fallback — the runtime values extend beyond this list (e.g. `starting`, `creating`, `stopping`, `aborting`, `blocked`, `complete`, `stopped`, `aborted`, etc.).

- **CSS for dots:** `frontend/web/src/style.css:104-105` (dark theme) and `:201` (light theme). Confirms recognized CSS classes for the dot colors.

**Recommended segment→status mapping** (derived from the canonical vocabulary above):

| Segment   | Statuses                                                                |
|-----------|-------------------------------------------------------------------------|
| Active    | `running`, `starting`, `creating`, `waiting_approval`, `stopping`, `aborting`, `stop_requested`, `abort_requested`, `suspended`, `pending`, `queued`, `launching`, `idle` |
| Completed | `completed`, `complete`, `succeeded`, `success`, `done`, `finished`    |
| Other     | `failed`, `failure`, `blocked`, `incomplete`, `cancelled`, `canceled`, `stopped`, `aborted`, `error` |

This matches Paxton's mapping (`Active = running, waiting_approval, launching/queued/pending, suspended`, etc.) but enriches it with the in-transition lifecycle values (`starting`, `creating`, `stopping`, `aborting`, `stop_requested`, `abort_requested`) so that runs visible on the screen never disappear mid-lifecycle.

---

## 4. Left sidebar task list component

The task list lives **inline** in the `App` component (`frontend/web/src/App.tsx:1478-1487`). There is no separate extracted component yet — refactoring it into one (`<TaskListSidebar />` or similar) is required so the test suite can render and assert on it. Layout:

```tsx
<aside className="codex-sidebar">
  <div className="codex-brand">…language switcher…</div>
  <button className="new-task-button">+ 新任务 / New task</button>
  <label className="session-search">
    <span><Search size={13} /></span>
    <input value={search} onChange={…} placeholder={text('搜索会话','Search tasks')} />
  </label>
  <div className="session-label">{text('会话','Tasks')} <span>{runs.length}</span></div>
  <div className="session-list">
    {filteredRuns.map((run) => <button key={run.id} className={`session-item ${run.id === runId ? 'selected' : ''}`} onClick={…}>
      <span className={statusClass(run.status)} />
      <span className="session-copy"><strong>{run.task || 'Untitled task'}</strong><small>{run.id}</small></span>
    </button>)}
    {!filteredRuns.length && <div className="sidebar-empty">{text('暂无会话','No tasks yet')}</div>}
  </div>
  <div className="sidebar-footer">…connection status…</div>
</aside>
```

**Key state hooks** (`App.tsx:839-840`):
```ts
const [search, setSearch] = useState('');            // search input
// (also: runs[], runId, ...)
```

**Current `filteredRuns`** (`App.tsx:904`):
```ts
const filteredRuns = runs.filter((run) => `${run.id} ${run.task}`.toLowerCase().includes(search.toLowerCase()));
```
This filters by search only — the new segment filter must be **OR'd together** with this: `runs.filter(bySearch).filter(bySegmentOrPassesOnAll)` (or implement segment→status-list lookup first, then search inside the subset).

**Required changes to make this testable:** extract the JSX in `<aside className="codex-sidebar">` rows 1481-1486 into a named exported component (e.g. `TaskListSidebar`) that takes props for `runs`, `runId`, `search`, `setSearch`, `selectedSegment`, `setSelectedSegment`, `segmentCounts`, `onSelectRun`. Tests can then `renderToStaticMarkup(<TaskListSidebar … />)`.

---

## 5. Task list API endpoint(s)

- **`fetchRuns()`** — `frontend/web/src/api.ts:181-187`
  ```ts
  export async function fetchRuns(): Promise<RunSummary[]> {
    const data = await getJson<{ runs: … }>('/api/runs?fields=summary');
    return data.runs.map((run) => ({ ...run, task: run.task ?? run.task_name ?? '' }) as RunSummary);
  }
  ```
- **`App.tsx:1035-1054`** — `setRuns(items)` after the fetch (in a poll loop guarded by `runListGeneration`).

- **Backend route handler:** `src/lh_harness/webapi/server.py:3050-3099` (`@app.get("/api/runs")`).
  ```python
  fields: str | None = Query(default=None),
  status: str | None = Query(default=None),
  ```
  — **already supports** a comma-separated `status=x,y` filter, in both summary and full modes. Cheap: per-run reads from `status.json`/owner records (cache window 5 s).

**Key practical implication:** because the backend filter exists and is essentially free, the recommended server-side strategy is:

1. **For "Active" / "Completed" / "Other":** extend `fetchRuns()` to accept `status` overrides (a list of status values) and pass `&status=running,starting,…` as a comma-list to `/api/runs?fields=summary`. This avoids client-side misses when a run sits beyond the paged window.
2. **For "All":** keep current behaviour (omit the status param).

**Constraints from the contract:**
- "no backend behaviour change beyond the optional status param" — the backend already has the param. Using it is **not** a backend change; only `fetchRuns()` needs to forward it.
- "Search combines with the filter" — implement search client-side over the server-filtered result, since the existing search-by-`id`/`task` is on the already-narrowed set.

---

## 6. Auxiliary structure & style hooks

- **CSS classes available for the segment filter UI** (already present in `style.css:56` — used by event activity filter for the same visual idiom):
  ```
  .filter-pills { display: flex; gap: 4px; flex-wrap: wrap; }
  .filter-pills button { padding: 6px 8px; border: 1px solid transparent; border-radius: 6px; color: #7189a8; background: transparent; font-size: 9px; }
  .filter-pills button:hover, .filter-pills button.active { border-color: var(--line-strong); color: #d6e7fa; background: rgba(63, 111, 165, .24); }
  ```
- **Existing segment counter style:** line `:282` (`.status-overview-line { display: flex; justify-content: space-between; … }`) — easy to reuse for the `Active N · Completed N · Other N` count strip.

**Suggested UI placement (above/next to "Search tasks"):**
```
[ Active N | Completed N | Other N | All ]
[ 🔍 Search tasks ]                <- existing .session-search
```

---

## 7. Test infrastructure

- **Test runner:** `frontend/web/scripts/test.mjs` (esbuild-bundles `.test.tsx` files into CJS in a temp dir, then `node --test`).
- **Existing tests** (mirrors a queueTasksEdit / queueSettings pattern):
  - `frontend/web/test/queueSettings.test.tsx`
  - `frontend/web/test/queueTasksEdit.test.tsx`
  - `frontend/web/test/runLockedModel.test.tsx`
- **Library patterns in use:**
  - `import assert from 'node:assert/strict'`
  - `{ test } from 'node:test'`
  - `renderToStaticMarkup` from `react-dom/server`
  - **No DOM/jsdom needed** — `renderToStaticMarkup` is sufficient for SSR-static assertions.
- **Network-free:** every test stubs the network via an injected seam (e.g. `QueueTasksApi`) or a replaced-and-restored `globalThis.fetch`.

**Test convention seen in queue tests:** import named exports from `frontend/web/src/App` (the file already exports dozens of helpers), construct test data inline, render with `<UiLanguageProvider>`, and assert on the returned markup.

---

## 8. Plan of action (for the implementation round)

> All of the below is the **next round's** work. This round stops at this finding file.

| # | Change | File | Round |
|---|--------|------|-------|
| 1 | Add `statusSegment(status)` helper (every known status maps to exactly one of `active`/`completed`/`other`/`all`) | `frontend/web/src/App.tsx` | R3 |
| 2 | Add `<TaskListSidebar />` extracted component taking runs + filter state | `frontend/web/src/App.tsx` (export) | R3 |
| 3 | Add segmented filter pills above the search input with per-segment counts | `frontend/web/src/App.tsx` | R3 |
| 4 | Add `STORAGE_KEY` localStorage read/write inside a `useSegmentFilter()` hook, default `'active'`, try/catch | `frontend/web/src/App.tsx` | R3 |
| 5 | Extend `fetchRuns(segment)` to call `/api/runs?fields=summary&status=running,starting,…` for non-`all` segments | `frontend/web/src/api.ts` | R3 |
| 6 | Unit test: status→segment mapping (one assertion per known status) | `frontend/web/test/taskListFilter.test.tsx` | R3 |
| 7 | Component test: default view shows only active runs; switching segments changes list + counts | `frontend/web/test/taskListFilter.test.tsx` | R3 |

**Estimated diff:** ~80 LOC of TSX + ~30 LOC of CSS (only if new pill class is added; otherwise reuse `.filter-pills`) + ~120 LOC of tests. Splitting by round:
- R3: Steps 1, 2, 4 — pure client-side filter (the most-likely-needed minimum).
- R4: Step 5 — server-side filter wiring.
- R5: Step 6 — unit tests.
- R6: Step 7 — component tests.
- R7: Branch push and PR.

---

## 9. Stable selectors / handoff

- **Side-bar entry point:** `frontend/web/src/App.tsx:1478-1487` (inline `<aside className="codex-sidebar">`)
- **Status helper:** `frontend/web/src/App.tsx:199-201` (`statusClass`)
- **Search state:** `frontend/web/src/App.tsx:839` (`const [search, setSearch] = useState('')`)
- **Current filter:** `frontend/web/src/App.tsx:904` (`filteredRuns`)
- **Run fetch:** `frontend/web/src/api.ts:181-187` (`fetchRuns`)
- **Run fetch call-site:** `frontend/web/src/App.tsx:1039-1054`
- **Backend list route:** `src/lh_harness/webapi/server.py:3050-3099` (already supports `?status=`)
- **Canonical status list:** `frontend/web/src/App.tsx:190-195` (statusLabel dict), `:203-205` (terminalLifecycle), `:207-209` (stoppingLifecycle)
- **CSS for sidebar + pill idiom:** `frontend/web/src/style.css:87-118` (codex-shell dark), `:146-204` (light), `:56` (filter-pills)
- **Test convention:** `frontend/web/test/queueTasksEdit.test.tsx` (best example of the network-free pattern)
- **Test command:** `cd frontend/web && npm test`

---

## 10. Boundaries honoured

- ✅ Read-only exploration only — **no source files modified**.
- ✅ No git operations performed (working tree still clean per `git status --porcelain`).
- ✅ No network calls made beyond `gh pr list` and `gh auth status`.
- ✅ All findings written to `tasklist-filter-findings.md` at the repo root for auditor verification.
- ✅ No usage of `ptait09` tools or hosts.
- ✅ Harness-owned paths (`/home/harness/work/.lh-harness/runs`, `.lh-harness/`) were not read or listed.
