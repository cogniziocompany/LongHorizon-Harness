# Handoff: one fleet, two surfaces — design the harness fleet views under hard constraints

## Handoff header

| Field | Value |
|---|---|
| Written | 2026-09-08, by the overseer session `5321285c-35e2-459a-9dae-92ea8811669f` |
| For | An independent expert asked to produce the design; implementation is already queued as harness tasks and can be re-pointed at whatever you decide |
| Question in one line | How should the harness fleet be presented on two surfaces that are fed differently, without adding a frontend stack, without changing server contracts, and without either surface renaming an identifier? |
| Surfaces | **Hydra** (control plane) `https://hydra.cognizioware.com` · **Fleet Window** (report plane) `https://fleet.easybutt0n.ai` |
| Hydra UI source | `cognizioware-hydra` → `orchestrator/public/index.html` (single page, ~41 KB, inline `<style>` and `<script>`, no bundler) |
| Hydra server source | `orchestrator/src/{fleet-routes,harness-client,harness-registry,harness-manager,mcp,activity-log}.js` |
| Fleet Window source | `ptait09-easybutt0n-ai` → `fleet-admin/` (vendored into `cognizioware-mcp-tools/infrastructure/docker/fleet-admin`, deployed by that repo's lane to CT202) |
| Harness | `LongHorizon-Harness`, node `ct110` at `http://192.168.21.168:8799` (its own web UI is the third surface a human can open) |
| Queued implementation | task 45 (fleet window hybrid pull + parity), task 46 (Hydra right-hand panel + drawer + deep links), task 44 (UX end-to-end gate) |

## Context: why the two surfaces disagree today

The same fleet is fed two different ways, and that is the root of every symptom.

- **Hydra pulls.** The harness is registered there as an external node, and Hydra's fleet routes call the harness API live. Its list is populated the moment a run exists.
- **The Fleet Window is pushed to.** It never queries the harness. Nodes, runs, gates and events appear only when the harness reporter and the device agents post them.

Observed 2026-09-08: the window rendered correctly, reported `connected`, and showed `Nodes 0 / Active 0 / Gates 0 / 24h Events 0` with the body text *No fleet nodes registered*, while the harness had five runs in flight and Hydra listed them. `GET /api/fleet/overview` returned `{"nodes":[],"runs":[],"gates":[],"counts":{…0},"issues":[]}`. The node **was** enrolled — its fleet URL, node id, labels and key were all present on the box — but the deployed harness build contained no reporter module. Enrolment without a producer is exactly an empty window, and every existing check passed because health was 200 and the page rendered.

## The constraints you must design within, and why each exists

1. **No frontend framework, no bundler, no new dependency on the Hydra page.** `orchestrator/public/index.html` is deliberately one plain file with inline style and script. It is edited by people and by agents at 3am, deployed by a compose recreate, and has no build step to break. Introducing a framework would add a build to a deploy path that currently cannot fail that way, and would make the file unreadable to the next agent that has to change it under time pressure. Design for progressive enhancement of a plain page.
2. **No server contract changes.** The REST and MCP surfaces are consumed by more than the page: agents call the fleet MCP tools, and the deploy lanes assert on them. A UI redesign that needs a new endpoint shape turns a page change into a fleet-wide change. If your design genuinely needs new data, say precisely what and why, and treat it as a separate, later change.
3. **Identifiers pass through untouched.** A run id, a node id and a per-round session id of the form `<run_id>.<round_tag>.<role>` must be the identical string on every surface — no truncation in the DOM value, no per-surface prefixing, no re-keying. This is not tidiness. The working mode is a human and an agent looking at different surfaces and quoting ids to each other; the moment one surface renames, that conversation breaks and so does joining a run to its trace.
4. **The push path stays authoritative and untouched.** Any pull added to the report plane is additive: pushed rows win conflicts, and the poller is read-only against the harness.

## The use case the design must satisfy

Paxton's words: *"I should see, the human user and the AI agent, an lh-harness in both fleet and hydra. SAME FLEET."*

Four consumers, one truth, at the same time:

| Consumer | Surface | Must see |
|---|---|---|
| Human | Fleet Window in a browser | the node, every non-terminal run with status/round/role, a link out to the harness UI for that run |
| Human | Hydra in a browser | the same node and the same runs, plus the controls Hydra owns |
| Agent | Fleet Window read API | the same ids and statuses, machine-readable |
| Agent | Hydra fleet MCP tools | the same ids and statuses |

## What we would like you to produce

1. **A presentation model for the two surfaces.** They are not the same view. Hydra is where a run is *acted on*; the window is where a fleet is *watched*. Say what belongs on each, what must be duplicated, and what must never be duplicated. Justify each duplication.
2. **A layout for the Hydra page.** The harness fleet currently shares a 280px left sidebar with devices and instances; the instruction is that it should own the entire right-hand side. Propose the layout at a wide desktop width and at laptop width, including what happens to the terminal area, and how the panel behaves when a node is degraded or absent. Plain HTML and CSS only.
3. **A design for the per-run popup.** There is a drawer today. It should become a cockpit: identity, current round and role, the pending gate and its question, the activity feed with actor and rationale, the controls the server already exposes (stop, resume, add instructions — each requiring a rationale, which the server enforces), and a prominent link to the live harness UI for that run. Say how it behaves while data is incomplete, how errors surface, and how it handles a gate that resolves while it is open.
4. **A staleness and disagreement model.** The window is pushed to and now also polls; Hydra pulls. Two surfaces will sometimes disagree for a few seconds. Say how each surface should show its own freshness, and what a *disagreement* should look like to a human — because silently showing an empty list is precisely the failure that hid for a day.
5. **The falsifiable check.** Given a harness with N non-terminal runs, what exactly should be asserted, on which surface, to prove they agree? An end-to-end gate is queued for this; we would rather implement your assertion than ours.

## Open questions where we do not have a settled answer

- Should the window's row show its origin (pushed vs polled) to a human, or is that noise that only belongs in the API?
- When a node is unreachable, is the honest display a degraded node with its last-known runs, or an explicit gap? We currently keep last-known and mark degraded; argue us out of it if that is wrong.
- Per-round session ids are long. They must not be truncated as *values*; is there a display treatment that keeps them quotable without dominating a row?
- Does the harness's own web UI make one of these surfaces redundant, and if so, which?

## Out of scope

Auth model, the device-agent path, the harness's internal round mechanics, and anything requiring a new server endpoint. Also out of scope: replacing the plain page with an application; if you believe that is the right long-term answer, say so as a recommendation with its cost, but design the near-term answer within the constraint.

## Evidence you can rely on

- `GET https://fleet.easybutt0n.ai/api/fleet/overview` → `{"nodes":[],"runs":[],"gates":[],"counts":{"nodes":0,"active_runs":0,"open_gates":0,"events_24h":0},"issues":[]}` (2026-09-08).
- Hydra `GET /harness/nodes` → `ct110`, kind `external`, status `online`, `hasToken: true`.
- Deployed harness build contains no reporter module; the reporter branch exists and is being rebased.
- Hydra page markers: `.sidebar` ~line 16, `#fleetList` ~117, `#instanceList` ~119, `Harness Fleet` + `#harnessList` ~120-123, run drawer ~line 168.
