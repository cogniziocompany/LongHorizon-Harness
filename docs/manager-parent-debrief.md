# Manager parent debrief runtime boundary

This is a draft runtime extension, not production activation. The optional
`parent_debrief_hook` on `manager.run` executes after a round's role results and
before the existing human gate can permit another round. Executor and Auditor
receive no parent chat tools or mid-round clarification requirement.

The hook receives a compact projection and a SHA256 checkpoint covering the
original round record, outcome, task state, question and current budget. Raw
Executor trajectories are not copied into this projection. It provides bounded
Auditor/feedback summaries, evidence references and remaining round count.

The admitted runtime adapter must perform Manager reflection, construct the full
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
runs: concrete Manager reflection adapter; full packet and outbox transport;
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
