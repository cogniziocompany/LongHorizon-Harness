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

Remaining required implementation before enabling this on delegated production
runs: concrete Manager reflection adapter; full packet and outbox transport;
runtime identity/policy checks; authenticated reply consumption; pending-debrief
reconciliation before resume; idempotent round admission across crashes; Supervisor
and task-detail waiting state projection. The optional hook must stay disabled in
production until those requirements have executable acceptance coverage. An
explicit resume currently follows the existing Manager resume behavior; the new
boundary by itself does not qualify safe resume of a pending parent dependency.

Validation: first seven tests failed before implementation. Then 28 focused Linux
tests passed across parent-debrief, Manager hardening and resume regression suites,
including a full round proving Executor and Auditor both return before the hook,
and no second round starts while waiting. Windows cannot run this repository's
secure POSIX control-bus file operations; Linux is the relevant verification target.
