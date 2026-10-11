# Manager parent debrief runtime boundary

This is a draft runtime extension, not production activation. The optional
`parent_debrief_hook` on `manager.run` executes after a round's role results and
before the existing human gate can permit another round. Executor and Auditor
receive no parent chat tools or mid-round clarification requirement.

The hook receives a compact projection and a SHA256 checkpoint covering the
original round record, outcome, task state, question and current budget. Raw
Executor trajectories are not copied into this projection. It provides bounded
Auditor/feedback summaries, evidence references and remaining round count.

The Manager now performs a separate bounded reflection episode using its existing
role adapter and configured Manager episode budget before constructing the debrief
checkpoint. It sees the original task, bounded role-result summaries and explicit
truncation state. Mandatory context that exceeds the configured verified-context
limit is refused, not silently shortened. Its strict JSON assessment names a summary,
continue/complete/wait/stop disposition, next slice, parent question and supplied
evidence references. It cannot invent evidence, expand budget, or claim completion
without the existing completion authority. Raw reflection output stays in owned
episode artifacts. An unqualified assessment holds even if transport supplies a
receipt. Resume reuses the saved assessment rather than repeating the model call.

The admitted runtime adapter must construct the full
existing packet using authenticated tenant/project/task/run/span/parent identity,
approved scope, pinned policy/revision and checkpoint, and durably enqueue it with
an idempotency key. This projection does not supply those identities or grant
authority. No adapter or network credential is accepted through a model argument.

Adapter output requires the exact checkpoint, a durable `receipt_ref`, an explicit
boolean `reply_required` and `action` of continue/wait/stop. A required reply needs
an authenticated, correlated, adapter-validated `parent_reply_ref`; transport ACK
alone is insufficient. Missing or stale evidence and adapter errors persist a wait
disposition and stop this worker with `needs_parent_input`. No default approval or
additional rounds are inferred. Clarifications enter only the next Manager prompt
after a qualified reply. Existing human gates remain responsible for their own
authorized budget changes. Dispositions are fsynced using the existing event store.
Before dispatch, a compact private snapshot binds the projection to the task and
source-round digests and original budget. Resume revalidates it and reconciles the
same checkpoint before any role episode starts. Missing adapters, changed tasks,
altered source evidence, invalid snapshots and unanswered dependencies hold the
worker. Resume does not implicitly grant extra rounds for that pending boundary.
The run persists an enabled-boundary marker before its first role episode. Missing
snapshots and a newer round without its own debrief snapshot hold for reconciliation;
a crash between recording that round and dispatching its debrief cannot skip the
boundary. Legacy runs without the marker keep their existing resume behavior.

Remaining required implementation before enabling this on delegated production
runs: full packet and outbox transport;
runtime identity/policy checks; authenticated reply consumption; idempotent round
admission across crashes; Supervisor
and task-detail waiting state projection. The optional hook must stay disabled in
production until those requirements have executable acceptance coverage. An
resume snapshot guards the local pending dependency; it does not establish the
adapter's authenticated transport or fencing against competing worker processes.

Validation: first seven tests failed before implementation. Then 28 focused Linux
tests passed across parent-debrief, Manager hardening and resume regression suites,
including a full round proving Executor and Auditor both return before the hook,
and no second round starts while waiting. Windows cannot run this repository's
secure POSIX control-bus file operations; Linux is the relevant verification target.
The subsequent resume regression failed before its fix. The expanded 32-test Linux
suite verifies no role starts while a restored debrief waits, missing-adapter hold,
changed snapshot/round/task refusal, exact checkpoint reuse and original-budget
preservation after a qualified answer.
Two additional crash-window regressions failed before the enabled-boundary marker
and latest-snapshot checks were added; the expanded 34-test Linux suite passes.

## Queue lifecycle while waiting

A clean `incomplete` worker report with `needs_parent_input` retains the existing
launched queue item and run ID, with `wait_reason=needs_parent_input`. Its dedup key
remains occupied. Launcher polls create no retry/replacement run and publish a
waiting event when entering the state. Re-entry into an active worker clears the
waiting projection; completion/failure then follows normal reconciliation. Failed,
cancelled and time-limited workers are not converted into parent waits by a stale
report. This does not authorize resume or supply an authenticated parent answer.

The new queue regression failed before this change. All 99 selected Manager,
resume, launcher, quota-backoff and occupancy tests passed in a fresh Linux
environment installed from Harness's declared test dependencies. A shared
Omnigent environment first yielded 98 passes and one existing key-health test
failure; the unchanged launcher reproduced that failure because its optional
HTTP client rejects the test's local file URL. No unrelated HTTP behavior was
changed. Dedicated parent-wait UI controls and full delegated runtime wiring
remain incomplete.

PostgreSQL serialization also preserves needs_parent_input without a timed backoff deadline. Its regression failed before the fix. The expanded declared-dependency Linux suite passed 112 tests; three scratch-database tests skipped because no scratch database was configured. Actual live PostgreSQL round-trip is not yet qualified.

Scratch PostgreSQL 16 verification now passed all 17 retry/queue-column tests with no skips, including the new parent-wait write, fresh-connection read, dedup reuse and wait clearing. The scratch schema harness was provisioned explicitly because existing migrations assume it exists; the first attempt skipped four tests on that missing prerequisite and was not accepted as evidence. The owned disposable database/container and network were removed afterwards. This verifies scratch storage, not production database migration or runtime activation.

Manager reflection validation: parser tests failed before the module existed; the final expanded 110-test Linux suite passed. The full management-loop test uses scripted AgentAdapter results and proves Executor/Auditor finish, Manager reflection qualifies, debrief follows, and resume does not repeat the assessment. Two explicit failure tests prove a transport receipt cannot override unstructured/unqualified assessment or leak raw provider response into its parent projection. This is local kernel/adapter qualification, not a production model or authenticated transport test.
