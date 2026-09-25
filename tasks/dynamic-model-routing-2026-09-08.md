# Dynamic model routing — harness task 49

| Field | Value |
|---|---|
| Repo | LongHorizon-Harness |
| Branch | `feat/dynamic-model-routing` from `origin/main` (PR #3 merged 2026-09-08 18:33Z) |
| Planning session | `3e1c0873-c857-479c-bdf5-a4e3a3fca3b6` (PTAIT09); plan file `C:\Users\PaxtonTait\.claude\plans\answer-the-questions-that-cozy-cosmos.md` |
| Task text | `C:/tmp/dynamic-model-routing-task.txt` |
| Queue entry | `C:/tmp/queue/0004-49-dynamic-model-routing.json`, trio kimi, 10 rounds |
| Design contract | `docs/handoffs/dynamic-model-routing-handoff-2026-09-08.md`, including its binding Decisions section |
| Opened | 2026-09-08 by the overseer session `5321285c-35e2-459a-9dae-92ea8811669f` |

## Why

On 2026-09-08 model availability moved four times in a few hours and a human absorbed every change by editing a launcher script by hand. Six of seven cloud keys hit a rate limit, a local fallback was added by hand, the auditor's model then hit a monthly cap, and finally a new provider was registered and the roles re-pointed. Three runs were lost in the middle of that, because a run that fails before its first round records no reason at all.

## Scope

The orchestrator chooses a model per role from what is actually available, records why, and degrades rather than stalling. Requirements are declared per role; models are never named in code. Candidates come from the router's own key-scoped catalogue, which makes a not-permitted model impossible to select. The route is provisional at enqueue, bound at launch, and both are recorded and visible on the fleet surfaces through the existing reporter.

The pre-round failure fix rides in this task rather than a separate one, because the missing failure reason is the reason a human was needed to diagnose today's outage at all.

## Settled, not open

Route per run and observe per round. No retry of a round that already worked; a bounded relaunch (default three attempts, a different route each time) only for a refusal before round zero. Never pull a model. An override is a queue-entry field carrying who set it and why, and it survives a resume.

## Strategy this task fits (one feature, one owner)

The queue is the decision point for every entry point: chat.easybutt0n.ai through the `lhharness` gateway alias, the Hydra queue panel (task 46, the only write surface), the fleet window (task 45, read-only), and curl. The router is the catalogue, fetched with the harness's own key. Availability memory lives in the harness. Provenance rides the fleet reporter into the window and into the MSCE trace (16a). Hydra keeps placement (which node); the queue owns the route (which model); both are recorded on the entry.

## Ops steps for the overseer (not runs)

1. Capture the two Ollama error bodies with one 1-token probe per capped provider from CT202; paste them, account redacted, into the handoff under "Captured provider bodies" before 49 launches.
2. Register the `lhharness` MCP alias on the gateway; mint the `harness-ops` virtual key with access groups `fleet-runners`, `rsi-loop`, `hive-mind`; set `OPENWEBUI_GATEWAY_KEY` on the chat host; upload the Fleet Chat Knowledge document (task 13a follow-ups).
3. After 49 merges: release CT110 at idle, retire `C:/tmp/launch_queue.py`, rotate the CT110 bearer it embeds, and run the 12b happy path from chat to window.
4. Instruct tasks 45, 46 and 16a to read the route shape (progress lines added to their task files).

## Queue order on this workspace

48 (running) → 49 → 16a → 18.

## Collisions

Task 48 is editing the launcher on the workspace-contention branch. PR #3 edits four of the same files, hence the base rule above. The tools repo's PR #112 owns the e2e and admin model registry, and must not be duplicated here.

## Progress

- 2026-09-08: task text revised in place with the settled decisions, the route contract, the five plumbing edits and the captured failure classes. Round budget raised to 10. Handoff updated with the binding Decisions section. Harness key allow-list widened to 33 models and all three role models verified healthy through it.
- 2026-09-08 (planning session 3e1c0873, after interview): decision 2 revised to a bounded relaunch (cap 3, different route per attempt); quota fixtures to be probe-captured and pasted into the handoff; task text gained the "interfaces other tasks read" paragraph; handoff gained Interfaces, Interview outcomes, Coverage of the control-plane plan, and Collisions sections; chat key scope settled (`fleet-runners` + `rsi-loop` + `hive-mind`); template's model doctrine collapsed to point at the queue.
