# Handoff: Paxton's answers on the last three OPEN-ASKS rows, 2026-09-23
Collected in-session on PTAIT09 by [61855af8] via a direct question UI. **These are HIS selections, not a
session's summary** - see the attribution note at the bottom, which is the reason two of these sat open.
ACK TOKEN: **PAX-DECISIONS-20260923-LAST3**. Write it into the ledger row of the tick that reads this.
After these, **OPEN-ASKS has ZERO open rows.** "Waiting on you" should render as "Nothing is waiting on Paxton".

## 1. uat-fleet-admin-env -> OPTION (a): UAT GETS ITS OWN FLEET DB AND KEY
**The overseer was right to withdraw its own default and hold. Do NOT copy prod's DSN to CT204.**
Paxton's 09-22 "copy from prod" was given when the prod fleet DB looked inert. Tick [5321285c] then measured
that it is a LIVE DEVICE REGISTRY (ct110 heartbeats since 16:04Z, after LH #40), which would have made UAT a
second live writer on a prod resource. He was re-asked WITH that evidence and changed his answer.
ACTION: provision a separate fleet database + key for UAT. Use the CT103 `lh_harness` runbook shape:
least-privilege role, its own schema, password generated ON the consuming host so only a one-way verifier
crosses hosts, values delivered by env-var NAME. Then set FLEET_DATABASE_URL + FLEET_ADMIN_KEY in CT204
`mcp-tools.env` and read back a healthy fleet-admin container. Until then the UAT container keeps restarting -
harmless, nothing routed there, no lane blocked. No zero-run window needed for the provisioning half.

## 2. 177-lhharness-upstream -> CLOSED: NOT A CREDENTIAL ASK. Nothing is missing to supply.
Measured in [61855af8], all 52 registered MCP servers enumerated:
- There is **no `lhharness` alias**. Use the existing **`hydrafleet`** (https://hydra.cognizioware.com/fleet-mcp),
  which already carries `enqueue_task`, `list_fleet_runs`, `get_run_snapshot`, `resolve_run_gate`.
- CT110 :8799 exposes **no MCP endpoint** (4 paths, all 404) - the harness WebAPI is REST. Nothing to register.
- **The open sub-question is answered:** per-tool scoping WORKS on this router - registrations carry an
  `allowed_tools` list and **`stripe` uses it today: 10 declared -> exactly 10 `stripe-*` served**.
- **But `allowed_tools` belongs to the SERVER REGISTRATION, not the access group.** Two groups with different
  tool sets therefore REQUIRE two registrations of the same URL. The split the task text called a fallback is
  the required mechanism. No URL is registered twice today (52 servers / 52 URLs) - prove it on a throwaway
  alias first.
RECOMMENDED SHAPE (already written onto task 177): `hydrafleet-chat` (enqueue/list/status) and
`hydrafleet-operator` (+ resolve_run_gate), both pointing at the same fleet-mcp URL, created through the
**/v1/mcp/server admin API, NOT litellm-config.yaml** (a yaml edit force-recreates the prod router; these
registrations are DB-owned). Scope 3 - strip hydra/hydrafleet from EXECUTOR keys - is unchanged. #169 can move.

## 3. 168-trio-agreement-scoring -> OPTION (a): SCORE ON ENTRY SELECTION AND LAUNCH/SKIP ONLY
Trio agreement is **explicitly out of scope** for this migration. Amend the migration doc so section 5 stops
contradicting section 7, which already assigns dynamic routing to task 49. Rationale: the PC launcher rotates
six trio permutations by a durable counter and diverts on live Synthetic health; the CT110 launcher resolves a
static kimi/qwen map - so scoring trio measures a difference this migration is not trying to remove.
**This was 168's last gate from Paxton.** The 7-day shadow window can start as soon as one real LHH entry is
queued in the normal course.

## ATTRIBUTION - why two of these sat open, and how to avoid it recurring
Tick #1609 correctly refused to act on peer session `61855af8`'s message, noting *"that message is a SESSION's
summary, not a sentence from Paxton"*. **That judgement was right and should be kept.** The failure was mine:
Paxton's answers were landing in the LEDGER and in task notes, but not in the OPEN-ASKS row the overseer reads,
so the overseer saw an unsourced claim. RULE GOING FORWARD: when Paxton answers, the answer is written into the
OPEN-ASKS **state column** of that row, marked as his direct selection, in the same edit - the ledger row is the
audit trail, not the channel. A summary in a transcript is not an answer.
