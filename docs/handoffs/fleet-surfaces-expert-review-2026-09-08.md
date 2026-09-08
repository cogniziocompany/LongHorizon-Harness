# Expert review of the fleet-surfaces handoff — Sonar Pro via the LiteLLM gateway

Model `sonar-pro` (perplexity/sonar-pro) called through the gateway on CT202, 2026-09-08, on
`docs/handoffs/fleet-surfaces-design-handoff-2026-09-08.md`. Two calls: the first answered the
presentation model, the Hydra layout and the run cockpit; it hit the token ceiling before the
freshness/disagreement/assertion items, so a second call covered those and the four open questions.
Reproduced verbatim below; the overseer's own commentary is only in the task text, not here.

---

## Call 1 — presentation model, layout, cockpit

192 /tmp/sonar-answer.md
Hydra is the **control cockpit**, Fleet Window is the **monitoring wallboard**. The same fleet truth (node + runs + gates + ids) must render on both, but with different emphasis and interaction, under the existing contracts and plain-page constraints.

---

## 1. Presentation model: what belongs where, and what is duplicated

**Decision:**

- **Shared, must be identical on both surfaces (humans + agents):**
  - **Node identity:** node id, labels, online/degraded/unreachable status.
  - **Run identity:** run id, non-terminal status (running, paused, gated, error), current round tag, current role.
  - **Gate identity:** gate id (if applicable), pending/closed, short gate question.
  - **Per-round session id:** full string `run_id.round_tag.role`, same literal value everywhere.
  - **Link to harness UI:** per-run link out to LongHorizon-Harness for that run (same URL generation everywhere).
  - **Timestamps:** started_at, last_update (for freshness / staleness).

- **Hydra-only (never duplicated to Fleet Window UI, but available via its API if already exposed):**
  - **Controls:** stop, resume, add instructions (with enforced rationale).
  - **Run-level control affordances:** bulk actions, scheduling, advanced filters that trigger server-side operations.
  - **Detailed control-oriented metadata:** MCP tool wiring, token presence (`hasToken`), exact control-plane error diagnostics.

- **Fleet Window-only (never duplicated to Hydra):**
  - **Aggregate fleet metrics:** counts of nodes, active runs, open gates, 24h events.
  - **Timeline / event feed emphasising observation:** events, alerts, health history.
  - **Origin hint (push vs poll) in API; minimal in UI.** UI focuses on “current truth”, API exposes origin for agents.

**Rationale:**  
Anything required to *coordinate humans/agents about a specific run* must be duplicated; anything tied to *acting on the fleet* stays in Hydra, anything tied to *watching trends* stays in the Window. That keeps the two surfaces conceptually distinct but conversationally aligned.

---

## 2. Hydra layout: wide and laptop, terminal behaviour, degraded node

### Layout decisions

- **Overall grid:**

  - At wide desktop (>1200px):
    - **Left:** 280px `.sidebar` (unchanged: devices, instances, harness fleet list).
    - **Right:** full-height **main pane** split vertically:
      - **Top:** “Harness Fleet” table / cards of nodes + runs.
      - **Bottom:** **terminal area** (existing terminal/log view) in a fixed-height band (e.g. 30–40% height) that can be expanded/collapsed.

  - At laptop (~1024px):
    - Keep sidebar fixed at 280px.
    - Main pane becomes stacked:
      - **Section 1:** Harness Fleet list/table.
      - **Section 2:** Terminal area immediately below, collapsible, default collapsed to prioritize fleet view.

- **Fleet list structure (right side):**
  - **Top bar:** node summary (id, status pill, label, “external”, hasToken) + last refresh timestamp.
  - **Runs table:**
    - Columns: run id, per-round session id (compact), status, round, role, gate summary, last update, “Open cockpit” button.
    - Each row click opens the per-run drawer/cockpit on the right.

- **Drawer/cockpit placement:**
  - Docked to the **right edge** of the main pane; 35–40% width on desktop, full-width overlay at laptop width.
  - Opening a run slides the cockpit in; closing slides it out, returning full width to the runs list.

### Terminal area behaviour

- **Desktop:**
  - Sits below the runs list, sharing the right pane; has a **toolbar** with “Pin” and “Collapse”.
  - “Pin” keeps terminal visible while switching runs; “Collapse” shrinks to a 32px bar with last log line or “Terminal collapsed”.

- **Laptop:**
  - Default **collapsed** (to avoid vertical crowding).
  - Expands to overlay or push the runs list down when explicitly opened.

**Rationale:**  
Hydra’s right side must be an **operations console**: runs list + run cockpit as the primary content, with the terminal as a secondary but always-available view. Collapsibility keeps the plain-page DOM manageable while respecting operators who live in the terminal.

### Degraded / absent node behaviour

- **States:**
  - **Online:** normal rendering; controls enabled.
  - **Degraded (reachable but unhealthy or partial):**
    - Node status pill changes to **“Degraded”** with warning color.
    - Runs list shows **last-known non-terminal runs** with a banner: “Node degraded; data may be stale since \<timestamp\>.”
    - Controls that require live harness (stop/resume/add instructions) are **disabled** with tooltip “Unavailable while node degraded”.

  - **Absent (unreachable or unenrolled):**
    - Node summary shows **“Unreachable”** with last-known contact time.
    - Runs table either:
      - Option A (preferred): still shows **last-known non-terminal runs** with strong banner: “Node unreachable; run state is last known, not confirmed.”
      - Option B (if strict): show explicit **gap** with message “No current data from node; last-known runs hidden by policy.”

**Decision:**  
Keep **last-known runs and mark degraded/unreachable**, do not hide them.

**Why:**  
Operationally, people need a memory of what was happening when the node disappeared; hiding that introduces unknowns and forces them to go to logs or harness UI. The UI must be honest via strong staleness banners, not by erasing context.

---

## 3. Per-run popup (cockpit): contents and behaviours

### Contents (always, when data is available)

In the drawer/cockpit:

- **Header:**
  - Run id (full), per-round session id, node id.
  - Status pill (Running / Paused / Gated / Error / Completed).
  - Link button: **“Open in Harness UI”** → direct URL to LongHorizon-Harness for this run (same on both surfaces).

- **Identity & state block:**
  - Current round tag.
  - Current role (e.g. `operator`, `agent`).
  - Started_at, last_update timestamps.
  - Short “origin” line: “Reported by push at \<ts\> / refreshed by pull at \<ts\>” (hydra emphasises pull; window emphasises push).

- **Gate block (if gate open):**
  - Pending gate id.
  - Gate question (short text; first 1–2 lines, expandable “More” if long).
  - Gate status (Pending, Resolved).
  - Actor expectations (“Awaiting: human / agent / both”, if available from existing data).

- **Activity feed:**
  - Chronological list of events: actor (human/agent/system), action (started, paused, gated, instructions added, gate answered), timestamp.
  - Each item shows **rationale** when present (the server already enforces rationale on control actions).
  - Minimal styling; no infinite scroll, just a constrained-height list with “Load more” if needed.

- **Controls (Hydra only):**
  - Stop, Resume, Add Instructions — each as a button or form that:
    - Requires rationale text field (disabled until non-empty).
    - Calls existing server endpoints; no new contract.

### Behaviour while data is incomplete

**Decision:**

- Cockpit opens **optimistically** with skeleton content:

  - Header: run id and node id already known from list.
  - Placeholder for round/role/status/gate/activity.
  - Banner at top: “Loading run details…” with spinner.

- If only partial data available (e.g. status but no activity yet):

  - Render known fields immediately.
  - Show placeholder sections with message: “No activity events yet received” or “Gate details not available (waiting for harness reporter)”.

**Why:**  
Operators need the identity immediately (to quote run and session ids), even if the details are delayed; skeleton UI keeps the page feeling responsive without misleading about completeness.

### Error surfacing

**Decision:**

- If the cockpit fails to load data (e.g. harness unreachable, API error):

  - Show a prominent error banner in the cockpit:  
    “Error loading run details: \<short technical reason\>. Last successful update \<timestamp or ‘never’\>.”
  - Keep **last-known fields** rendered (if any) with a badge “May be stale”.
  - Controls section is either hidden or disabled with tooltips reflecting the error.

- If an action (stop/resume/add instructions) fails:

  - Inline error message below the button/form: “Stop failed: \<reason\>”.
  - Activity feed does **not** add an event unless the server confirms success.

**Why:**  
Errors must be visible *in the cockpit where the operator is looking*, not only in terminal. Last-known state is kept to preserve context while clearly marked.

### Gate resolves while cockpit open

**Decision:**

- Gate block is **live updated** via polling (same cadence as main Hydra pull) or via existing update mechanisms:

  - When a gate transitions from Pending → Resolved while cockpit is open:
    - Gate status pill flips to “Resolved”.
    - Gate question remains, but a line appears: “Resolved at \<timestamp\> by \<actor\>”.
    - Pending-control affordances tied to an open gate (e.g. “answer gate”) disappear or become disabled.
    - Activity feed gets a new event for the resolution.

- No modal popups; instead, a subtle inline **“Updated”** badge next to the gate block for a few seconds.

**Why:**  
The cockpit is a continuous control surface. The operator must see state transitions inline, without disruptive modals, but they must be obvious enough to notice that the gate is no longer pending.

---

## 4. Staleness and disagreement model

### Freshness indicators per surface

**Decision:**

- **Hydra (pull-based):**
  - Global **“Last Hydra refresh”** timestamp in the node summary


---

## Call 2 — freshness, disagreement, the falsifiable assertion, open questions

A) Freshness UI

- **Hydra surface**  
  - UI element: **Status bar text at top-right**  
  - Wording: **“Fleet data refreshed 8s ago (auto every ~10s)”**  
  - Rationale: Humans need both last-refresh time and cadence, in one glance.

- **Fleet Window surface**  
  - UI element: **Status bar text at top-right**  
  - Wording: **“Fleet data refreshed 12s ago (push + poll every ~20s)”**  
  - Rationale: Distinguish hybrid push+poll behavior without over-explaining.

---

B) When the two surfaces disagree

- Position: **Yes, disagreement should be detected and surfaced in both UIs, not only tests.**
  - Rationale: This is an operational tool; silent inconsistency destroys trust in either surface.

- Human-facing behavior when a disagreement is detected:
  - **Hydra surface banner (non-modal, top):**  
    - Text: **“Data consistency warning: Hydra and Fleet Window show differing runs/statuses. Displaying harness view; investigate reporter/harness sync.”**  
    - Rationale: Declare which source is trusted for now and explicitly name the nature of the problem.

  - **Fleet Window banner (non-modal, top):**  
    - Text: **“Data consistency warning: Fleet Window and Hydra show differing runs/statuses. Displaying harness view; investigate reporter/harness sync.”**  
    - Rationale: Mirror message so operator doesn’t have to guess which surface is wrong.

---

C) One falsifiable assertion (automated check)

- **Assertion:**  
  - “For every run id present in the harness response at time \(T\), both Hydra and Fleet Window surfaces must display the same status value for that run id within **5 seconds** of \(T\), using the harness as the source of truth.”

- Surfaces it reads:
  - Reads **Hydra’s displayed run list + statuses**.
  - Reads **Fleet Window’s displayed run list + statuses**.
  - Reads **harness API’s run list + statuses** as ground truth.

- What it compares:
  - At a sampled time \(T\):
    - Collect from harness: set of \((run_id, status)\).
    - Within \([T, T+5s]\), confirm:
      - Hydra displays identical set of run_ids with matching status per run_id.
      - Fleet Window displays identical set of run_ids with matching status per run_id.
    - Disagreement is any missing run_id or status mismatch in either surface versus harness.

- Tolerance for polling skew:
  - **5 seconds window** between harness observation and each surface’s UI state.
  - Rationale: Covers 10s and 20s polls while being tight enough to catch staleness.

- Exact failure message a human should see:
  - **“Consistency check failed: Hydra and Fleet Window UIs do not match harness fleet state within 5s. At least one surface is showing missing or incorrect run statuses.”**

---

D) Four decisions

1. **Show origin push-vs-poll in the window row, or only in API?**  
   - Decision: **Only in API / diagnostics, not in the visible row.**  
   - Rationale: Origin is implementation detail; row should be about run meaning, not transport.

2. **Unreachable node: degraded last-known runs vs explicit gap?**  
   - Decision: **Explicit gap, with a clear “Data unavailable” state at node level, not just degraded runs.**  
   - Wording for node section: **“Node unreachable – fleet data unavailable since 3m ago.”**  
   - Rationale: Degraded suggests known bad; unreachable is *unknown* and must be visually distinct.

3. **Long per-round session ids: quotable but not dominating a row?**  
   - Decision: **Truncate with stable, copyable full value on hover/click.**  
   - UI pattern:  
     - Row column shows: **“session: run123.alpha.coordinator… (copy)”**  
     - Tooltip or detail panel shows full `run_id.round_tag.role` and supports copy.  
   - Rationale: Keeps rows scannable while leaving an exact string a human can quote and copy.

4. **Does the harness’s own web UI make one surface redundant, and if so which?**  
   - Decision: **The harness web UI functionally overlaps Hydra more; Hydra is more redundant than the Fleet Window.**  
   - Rationale: Both are harness-centric fleet views, whereas the Fleet Window is uniquely shaped around reporter-driven, operator-focused monitoring.
