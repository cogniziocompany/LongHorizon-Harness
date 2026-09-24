# Handoff: would KubeRocketAI (krci-ai) change how we build agents? — audit for an incoming expert
Written 2026-09-22 by the interactive session on PTAIT09, at Paxton's request. Read time ~5 min.
**Bottom line (REVISED 2026-09-23): do NOT adopt it. Take ONE idea from it — role prompts as externalised,
versioned data — and implement that inside LongHorizon-Harness. The validation benefit that justified adoption
does not survive scrutiny (see §3.1), and the tool's differentiators (bundling, IDE integration) assume a human
in an editor, which our headless fleet is not.** Confidence: high on our side (measured today), medium on
theirs (I read the repo landing page and `docs/architecture.md` only — see "What I did not verify").

## 1. What krci-ai actually is
"AI-as-Code": agents are **version-controlled Markdown files with YAML frontmatter** under a `.krci-ai/` directory:
```
.krci-ai/
  agents/     role definitions (YAML: identity{name,role,goal}, commands{}, tasks[])
  tasks/      step-by-step procedures the agent follows (Markdown)
  templates/  output formats, mustache-style {{variable}}
  data/       org standards / best practices
```
CLI: `krci-ai install --ide=cursor`, **`krci-ai validate --all`**, `krci-ai bundle --all --output project-context.md`,
`krci-ai list agents`. Validation covers schema compliance, template-variable completion, dependency satisfaction
and **file-reference integrity**.
**Execution model — the decisive fact: it GENERATES CONTEXT, it does not run agents.** It prepares structured
prompts for an IDE/LLM (Cursor, VS Code, Claude Code) or a CI job to consume. There is no scheduler, no
multi-round loop, no gate, no provider fallback.

## 2. What we have today (measured 2026-09-22, not recalled)
| Layer | Ours today | krci-ai equivalent |
|---|---|---|
| Role definitions | **Hard-coded in Python**: `src/lh_harness/prompt_texts.py` (23,914 chars, 10 constants) + `role_prompts.py` (`build_role_manager_prompt`, `..._executor_...`, `..._auditor_...`, format-repair, final-response) | `agents/*.yaml` |
| Task definitions | **Freeform prose `.txt`** in `C:/tmp/*-task.txt`. No schema, no validation, conventions by habit (`HARD RULES`, `STOP GATE`, `DONE MEANS`) | `tasks/*.md` |
| Queue entries | 268 JSON files, **14 distinct fields**, evolved ad hoc (`state` used twice, `blocked_reason` once, `continue_branch` twice) | none — out of scope for krci-ai |
| Skills | `~/.claude/skills/*/SKILL.md` — **already Markdown + YAML frontmatter** | `agents/` + `data/`, near-identical shape |
| Execution | LongHorizon-Harness: manager/executor/auditor trio, N rounds, gates, timeouts, provider fallback, CT110 launcher | **no equivalent** |
| Tool access | LiteLLM MCP gateway, 52 servers, access groups, `allowed_tools` | none |

## 3. Where it genuinely helps us
1. **Validation — CORRECTED 2026-09-23, read this carefully.** My first draft of this section claimed
   `krci-ai validate --all` "would plausibly have caught" this week's four defects. **That was wrong and I am
   retracting it.** Tested against krci-ai's *documented* validation (schema compliance, template-variable
   completion, dependency satisfaction, file-reference integrity), it would have caught **none of the four**:
   | Defect | Why krci-ai misses it |
   |---|---|
   | runbook said `LH_HARNESS_DATABASE_URL`; code reads `LH_HARNESS_DB_PASSWORD` | doc-vs-Python-source mismatch; it does not read our source |
   | task 187's blocker recorded as "179", real blocker is 186 / PR #23 | prose cross-reference between task notes, not a file reference |
   | task 177 written against the `lhharness` gateway alias | reference to **live infrastructure state**; no static validator can know this |
   | two queue entries both numbered 208 | it does not manage our queue |
   All four are **cross-boundary** references — to Python source, to a live gateway, to the queue — which is
   exactly the class a generic file-graph validator cannot see. The headline benefit therefore largely
   evaporates, and this changes the verdict in §5.

2. **Externalising role prompts.** Today changing a role prompt means editing Python and **deploying**, and a
   CT110 deploy restarts the service and kills every in-flight run. Prompts-as-data decouples "change a role"
   from "restart the fleet". This is the highest-value structural change on offer.
3. **Skills already fit.** Our SKILL.md files are structurally what krci-ai expects. Lowest-friction entry point.
4. **`bundle`** gives a reviewable single-file context artifact — useful for the Ship Plane / handoff habit.

## 4. Where it does NOT fit — read this before anyone proposes a migration
- **Category error risk.** krci-ai is an authoring/validation layer. Our hard problems are *execution* problems:
  round budgets, three-strikes discrimination, gate resolution, SYN_CAP, provider fallback, stranding detection,
  workspace/branch guards. krci-ai addresses **none** of these and does not claim to.
- **Our "tasks" are not their "tasks".** Theirs are static procedures. Ours carry live operational state —
  measured inventories, STOP GATES, requeue history, "do not re-derive" notes, blocked reasons. A krci-ai task
  file has nowhere to put that, and flattening it would lose the institutional memory that keeps runs honest.
- **Queue/launcher/trio have no analogue.** Do not try to express `trio`, `max_rounds`, `continue_branch` or
  `roles_bound` in agent YAML.
- **It targets IDE-driven development.** Our fleet is headless and autonomous. The overlap is the prompt text,
  not the loop.

## 5. Recommended shape — REVISED 2026-09-23 after the §3.1 correction
**Verdict: do not adopt the framework.** Cost = a new dependency of unknown maintenance status, a migration
across 268 queue entries and our task-file conventions, and an unverified secret story (we are strict:
env-var NAMES only, never values — a file-inlining bundler is exactly the wrong shape for that). Benefit, after
§3.1 = one idea we can implement ourselves.
**Do these two things instead:**
1. **Externalise role prompts** out of `prompt_texts.py` into versioned data loaded at runtime. Gate: loadable
   **without a service restart**, or the main benefit is lost (a CT110 deploy kills every in-flight run).
   This is a LongHorizon-Harness change and needs its own task. This is the whole of krci-ai's value to us.
2. **Write our own queue/task validator** with rules matched to our ACTUAL failure modes — the cross-boundary
   checks krci-ai structurally cannot do. Filed as **task 218**, and it already has real findings (see below).
**Do not** put the harness's execution semantics (trio, max_rounds, gates, continue_branch) into agent YAML.

## 6. What I did not verify — do this first
- I read the repo landing page and `docs/architecture.md`. **I did not** clone it, run the CLI, read the source,
  check the licence, release cadence, issue backlog, or whether the project is actively maintained.
- I did not test whether `bundle` output fits our context budgets, nor whether the agent YAML schema can express
  a three-role trio at all.
- Unknown: how it handles secrets (we are strict — env-var NAMES only, never values). **Check this before any
  pilot**, because our agents' prompts reference credentials by name and a bundling tool that inlines files is a
  leak risk.
- Unknown: whether validation is extensible. If we cannot add our own rules (task-number uniqueness, env-var
  cross-checks), the main benefit in §3.1 does not materialise.

## 7. Suggested first question for the expert
"Can krci-ai validation be extended with project-specific rules, and can agents/prompts be reloaded at runtime
without redeploying the consuming service?" If both answers are no, take the *idea* (prompts + tasks as
validated data) and implement it ourselves in LongHorizon-Harness — that is a smaller change than adopting a
framework whose execution model we do not use.
