# TASK — hivemind voice-agent fixes (Phases 1–5 of the approved 2026-08-30 plan), BUILD ONLY

Read FIRST, with your file tools: `hive-mind/deploy/fixes-plan-2026-08-30.md` — the
authoritative spec, committed in this workspace. Context for every fix is in
`hive-mind/deploy/live-state.md` §11–§14 (read §12 and §13 before touching the voice path).

## Repo provenance
- Branch `docs/n8n-cloud-lan-reachability` at commit `b34b959` is authoritative; the workspace
  is already on it. No uncommitted state elsewhere matters to you.
- 308 tests green at start: `cd hive-mind && python -m pytest tests -q` must stay green.

## Scope (implement in this order; each phase = code + tests, committed)
1. **Phase 1a** — `/outreach/voice/context` must assemble within a ~2.0s handler budget:
   parallelize dossier/recall/availability with a deadline (ThreadPoolExecutor), move
   `sync_contact` to a post-response daemon thread, add short-TTL caches (Contacts/Events
   snapshots ~60s, availability ~60s, Graph token + drive/item resolution), and add per-piece
   `ms=` timings to the `voice_context` log line. `scripts/create_whc_assistant.py` gains an
   explicit `dynamic_variables_webhook_timeout_ms` (target 4000) in `_payload()` — do NOT call
   the Telnyx API; the operator pushes the assistant.
2. **Phase 1c** — `/outreach/voice/recall` returns 404 for a contact_id that does not resolve,
   with the same "do not call another tool" guidance text booking uses. Reuse the Contacts cache.
3. **Phase 2** — new `scout/guardrails.py`: config loader (`config/guardrails.yaml`, env from
   `HIVE_ENV`, default dev), `mask_pii()` (SSN/credit-card Luhn/bank-IBAN → `[redacted]`;
   NEVER mask names/phones/emails), `mask_secrets()` (sk-/Bearer/AWS/hex≥32 → `[secret removed]`).
   Close the G2 bypasses: `dyn["last_interaction"]` and dossier's non-summary text fields get
   `strip_transport_noise` + `sanitize_memory_text`. Apply masks at the egress points
   (voice/context dyn, recall hits, contact-dossier) and ingress (booking notes/subject, optout
   reason, call-summary summary before sheet/DB writes). Annotate every guardrail in
   `config/guardrails.yaml` with truthful `enforced_at:`; fix its header (the referenced
   `apply_guardrails` script does not exist — point at the new module). New
   `tests/test_guardrails.py` + extend the structural loop in `tests/test_agent_prompts.py`.
4. **Phase 3** — `scripts/eval_gate.py` honours `EVAL_WAIVED_CASES` (comma list): waived cases
   report `WAIVED (<case>: reason=env not provisioned)` loudly and do not fail the gate, but
   waivers are REFUSED on prod tier. `deploy/deploy_api.sh` passes the variable through.
   Tests for: waived case passes gate on dev, refused on prod, non-waived skip still fails.
5. **Phase 4** — fix the post-gate `$json` defect in
   `hive-mind/n8n-workflows/outreach-comm-verify-send.json` (the "success but does nothing"
   class: post-IF nodes referencing `$json` from the wrong branch; inspect the graph, correct
   the expressions to explicit node references). Add a guard for this defect class to
   `tests/test_workflow_graph.py`. Do NOT push to n8n; the operator does.
6. **Phase 5** — wrap the second query in `_sms_latest_conversation` in the same try/except as
   the first; fix the stale DSN note in `kb-article/kb-article.md` (direct `.153:5432` auth
   fails for ai_easybutt0n; the working path is ssh root@192.168.21.110 → pct exec 100 →
   docker exec postgres-server psql). Update `hive-mind/deploy/live-state.md` with a §15
   summarizing what this run changed.

## Boundaries (non-negotiable)
- BUILD ONLY. Commit small with descriptive messages. Do NOT push. Do NOT deploy, do NOT SSH
  anywhere, do NOT call Telnyx/n8n/SharePoint/Graph/LiteLLM live APIs, do NOT run migrations.
  Anything live is operator-executed later — raise it in your report, not an approval loop.
- COMPLETION = the audited branch with all phases implemented and the full test suite green.
  Live verification is explicitly OUT OF SCOPE and operator-executed; do not ask to proceed
  with deploys.
- Secrets stay out of the repo and out of commits. No credentials exist in this workspace; if a
  step appears to need one, record it as an operator item and continue.
- Reserved paths: do not touch `.mcp.json`, `.codex/`, `kb-article/kb-hook.py`.
- Unit tests must not require network or a database: follow the existing patterns in
  `hive-mind/tests/` (monkeypatched OSH/CM/SP, fake cursors).

## Honesty rail
If a step needs services you cannot reach, record exactly what is blocked and continue with the
next implementable item — do not fabricate results, and never claim a test passed that did not run.

## Operator context
This run is monitored via the web API by the hivemind repo session. The operator can inject
instructions (POST /api/runs/{id}/instructions) and resolve gates remotely. Reports must name
files, commits and test counts precisely for a remote reader. Pacing: one phase (or half of
Phase 1a) per round; inventory surviving disk state first each round; keep every round
completable well under 45 minutes. Auditor: do not run `git fetch` or any network call.
