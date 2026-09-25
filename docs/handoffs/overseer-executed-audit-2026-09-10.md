# Audit: the overseer-executed task process (task 152 as the specimen)

Date: 2026-09-10 (PT). Audience: Paxton, the overseer session (`longhorizon-harness-99`), and whoever
implements task 134 (queue store to Postgres) and task 150 (spec staging).

## Session & agent identity — for a reviewing agent

**This session**
| field | value |
|---|---|
| Agent name (address it with this) | not assigned (no fleet/harness agent name was issued to this session) |
| Short ref | `[421241]` |
| Session UUID | `4212416b-7566-4042-9ea2-ee0599ff77ab` |
| Repo / cwd | `C:\Users\PaxtonTait\source\LongHorizon-Harness`, host PTAIT09 |
| Role | interactive VSCode session: audit plus interview with Paxton. Not the overseer loop (`longhorizon-harness-99`, cron `84a5be19`) and not a subagent |

## Scope

Paxton asked for an audit of how an OVERSEER-EXECUTED task is authored, filed, executed and
verified, using TASK 152 as the specimen:

- task text `C:\tmp\ptait09-claude-lane-task.txt`
- queue entry `C:\tmp\queue\blocked\1009-152-ptait09-claude-lane.json`
- source handoff `ptait09-easybutt0n-ai/docs/HANDOFF-ptait09-runner-claude-lane.md`

Reviewed read-only: the live file queue (`C:\tmp\queue`: `LOOP-PROMPT.md`, `OPEN-ASKS.md`,
`LEDGER.md` tail, `blocked/*.json`), `C:\tmp\launch_queue.py`, this repo (`docs/queue.md`,
`docs/handoffs/spec-staging-2026-09-10.md`, `tasks/TEMPLATE-overseer-hierarchy.md`,
`templates/task-spec.md` on `feat/spec-staging`), and `cognizioware-hydra` (main checkout on
`fix/resolve-shells-across-both-profiles`, fleet plane on worktree `cognizioware-hydra-fleet`,
branch `feat/harness-fleet`). No file outside this document was changed by this audit.

## How the process works today

1. A peer session writes a handoff. The overseer's `claude-bridge` step finds it in the session
   transcripts during a tick.
2. The overseer writes the task prose to `C:\tmp\<slug>-task.txt` and a queue entry
   `C:\tmp\queue\<NNN>-<num>-<slug>.json` whose `note` opens with `OVERSEER-EXECUTED`.
3. The entry is placed in `C:\tmp\queue\blocked\`. The launcher globs only the top level
   (`launch_queue.py:572`, `C:/tmp/queue/*.json`), so it never sees the entry.
4. The overseer executes the task itself during a tick and records the result as a paragraph in
   `LEDGER.md`. Nothing moves the entry out of `blocked/` automatically.

The reasons a task is overseer-executed are always one of: CT110 has no `gh` and no GitHub token;
the work needs credentials a run is never given; the work needs a host's service manager; or the
deploy restarts CT110 and would kill the run doing it.

## Findings

### 1. The task text names a commit that is no longer on the branch

Task 152 says the handoff is commit `3b5042a` and to STOP if it is not on origin.

| commit | parent | where it is |
|---|---|---|
| `3b5042a` | `55c667c` (old local base) | dangling on PTAIT09; reflog `HEAD@{3}` |
| `5e8f15d` | `ce6bde4` (origin tip, PRs #43-#47) | `origin/easybutt0n-runner-v2` |

The local branch was rebased onto origin and the handoff commit was replayed as `5e8f15d`. The
handoff file is byte-identical in both commits: `git diff 3b5042a 5e8f15d --
docs/HANDOFF-ptait09-runner-claude-lane.md` is empty. The 103-file diff between the two commits is
the upstream PR work, not a change to the handoff. The precondition is satisfied, but an executor
following the text literally would stop.

**Rule:** task text should name the file and the branch on origin, and quote a sha only as
"verified at", never as the gate.

### 2. The restart in task 152 is also a partial deploy

| fact | measured |
|---|---|
| Service | `ptait09-runner`, LocalSystem, easysvc pid 7100, started 2026-09-07 10:52 |
| Process serving :7334 | node pid 11540, child of 7100, started 2026-09-07 10:52 |
| Code location | the dev checkout `C:\Users\PaxtonTait\source\ptait09-easybutt0n-ai` |
| `src/runner.js` on disk | rewritten by the 2026-09-10 21:04 rebase: +233 lines, PR #45 fleet bridge |
| `svc/publish/easysvc.exe` | gitignored, built 2026-08-09, predates PR #45's FleetAgent relay |
| Listener on :7335 | none |

Restarting the service to pick up the `.env` change also starts the node half of PRs #43-#47. The
relay half in easysvc will not be running, because git does not rebuild the binary. This is safe:
`postBridge()` in `src/runner.js` is fire-and-forget and swallows every error. The risk is a later
misreading: PR #45 will look deployed and broken when it is only half deployed.

**Decision:** accept it, and record it in the ledger when 152 runs.

### 3. `blocked/` holds two different kinds of entry

Overseer-executed entries (144, 147, 149, 150, 151, 152) share `blocked/` with harness tasks that
are genuinely waiting on something (69c, 77b, 82, 88, 104, 109, 113, 116). Only the prose note tells
them apart. Their `max_rounds: 1, trio: kimi` fields are filler that no code reads.

**Decision:** a separate directory, `C:\tmp\queue\overseer\`. No `executor` field and no new status.
Task 134's launcher mirror must carry the directory into the store using fields it already has:
`overseer/` becomes `status=blocked, reason="overseer-executed"`, and `blocked/` becomes
`status=blocked, reason=<the named blocker>`. Without that mapping the marker is lost at the Postgres
cutover.

### 4. The class exists only in prose

Every overseer-executed note re-derives the same reasons. No code, template or schema knows the
class. `LOOP-PROMPT.md` STEP 6 makes the note authoritative ("the note wins"), so correctness
depends on every author restating every constraint.

### 5. Task 152 has no working elevation path

The handoff says `Restart-Service ptait09-runner`. From the user shell on PTAIT09 that is
access-denied. The path proven on 2026-08-30 is Windows sudo:

```powershell
sudo sc.exe stop ptait09-runner
sudo sc.exe start ptait09-runner
```

The runner's own `/exec` can also restart it as SYSTEM, but it kills the caller mid-request.

**Decision:** `sudo sc.exe stop/start` from the overseer's own shell is the sanctioned path.

### 6. Verification is recorded only as ledger prose

Task 152's DONE MEANS is well written: health, a pwsh regression task, the `/claude` task id and
exitCode, and the resolved binary path. It is recorded only as a paragraph in a 1.1 MB append-only
`LEDGER.md`, so nothing can check it.

**Decision:** keep the ledger entry, and also write `verified_at` and `evidence[]` onto the queue
JSON. Task 134 carries both fields into `harness.queue`.

### 7. Hydra and the runner resolve CLIs by different rules on the same box

- The runner uses `CLAUDE_PATH` from `.env`. `resolveClaudePath()` (`src/runner.js:43-73`) probes
  only when the variable is unset.
- The hydra device agent (`cognizioware-hydra` `3b9ea19`) unions the derived user's PATH, the
  user's conventional bin dirs, and the agent's (SYSTEM) bin dirs, user first.

On PTAIT09 claude exists in both the SYSTEM profile and the user profile, so the two stacks can
disagree about which binary runs.

**Decision:** both stacks stay. The runner serves REST lanes and hydra serves terminals.

**Recommendation:** align the runner on hydra's rule, and skip the explicit-path short-circuit only
when the explicit path does not exist. That is a code change and is deliberately not part of 152,
whose hard rule is to leave `resolveClaudePath()` alone.

Separately, the fleet plane (`feat/harness-fleet`) is four commits ahead of hydra `main` and not
merged. Hydra's deploy fires only on `main`, so what corsairai300 runs is unverified from disk.

### 8. Skill drift is in the handoff but not in the task

The handoff lists what the `ptait09-remote-pc` skill gets wrong: pm2 instead of the Windows service,
`server.js` instead of `src/server.js`, and a stale claude version. Its `pm2 restart` step would
not restart the real service.

**Decision:** a separate task, not folded into 152.

### 9. Spec staging does not cover overseer-executed tasks

No queue entry has a `spec_file` yet, and the running launcher predates the spec gate. Overseer
entries are never launched, so they would bypass the gate even once it is live.

**Decision:** use the same `templates/task-spec.md` with frontmatter `status: ready-for-overseer`.
The BMAD hold covers running BMAD_Cognizioware as a generator, not this in-repo template.

### 10. Three sessions edit `launch_queue.py`

The spec gate, the DRAIN switch and overseer fixes all land in one file outside git, reconciled by
`.bak-*` copies and byte-prefix checks recorded in the ledger. Nothing in `C:\tmp` reaches a
pipeline; only repo code does.

## Decisions (Paxton, 2026-09-10 interview)

| topic | decision |
|---|---|
| Handoff sha | Name file and branch on origin; a sha is evidence, not a gate |
| `blocked/` overload | `C:\tmp\queue\overseer\`; no new field or status; task 134's mirror maps it to `status=blocked, reason="overseer-executed"` |
| Second overseer concept | None. The web UX supports one overseer; memory is the record of what it did |
| Runner vs hydra | Both stay: runner for REST lanes, hydra for terminals |
| Skill drift | Separate task |
| Restart scope | Accept the partial deploy of PRs #43-#47 and record it |
| Elevation | `sudo sc.exe stop/start ptait09-runner` |
| Evidence | Ledger plus `verified_at` / `evidence[]` on the queue JSON, carried into the DB by task 134 |
| Spec format | `templates/task-spec.md`, `status: ready-for-overseer` |

## Caveat on "memory will resolve this"

Memory is recall, not a launch gate. The launcher and the queue store decide what runs, so the
`overseer/` marker has to survive the store migration on its own. Also, only the transcript ingest
Scheduled Task is live. The run and activity ingest (`scripts/hivemind-ingest.mjs`) is on the
unmerged `feat/harness-fleet` branch.

## Follow-ups recorded, not done in this pass

1. Correct `C:\tmp\ptait09-claude-lane-task.txt`: origin branch instead of `3b5042a`, the
   `sudo sc.exe` path, the restart-scope note, and the evidence fields in DONE MEANS.
2. Create `C:\tmp\queue\overseer\`, move the six overseer entries there, and add the convention
   to `LOOP-PROMPT.md`.
3. Queue three tasks: the `ready-for-overseer` status (extends 150); a pinned runner checkout so git
   activity on PTAIT09 cannot change what the next restart runs; the remote-pc skill drift fix.
4. Append the directory-to-status mapping and the evidence fields to task 134's note.
