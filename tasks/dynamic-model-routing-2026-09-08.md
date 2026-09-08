# Dynamic model routing — harness task 49

| Field | Value |
|---|---|
| Repo | LongHorizon-Harness |
| Branch | `feat/dynamic-model-routing`, based on `feat/fleet-reporter` while PR #3 is unmerged, otherwise `main` |
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

Route per run and observe per round. No retry of a round that already worked; one automatic re-enqueue only for a refusal before round zero. Never pull a model. An override is a queue-entry field carrying who set it and why, and it survives a resume.

## Collisions

Task 48 is editing the launcher on the workspace-contention branch. PR #3 edits four of the same files, hence the base rule above. The tools repo's PR #112 owns the e2e and admin model registry, and must not be duplicated here.

## Progress

- 2026-09-08: task text revised in place with the settled decisions, the route contract, the five plumbing edits and the captured failure classes. Round budget raised to 10. Handoff updated with the binding Decisions section. Harness key allow-list widened to 33 models and all three role models verified healthy through it.
