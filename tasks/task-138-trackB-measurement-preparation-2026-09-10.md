# Task 138 - Track B Measurement Preparation (Steps 1-3) and Runbook (Steps 4-7)
**Date:** 2026-09-12
**Workspace:** /home/harness/work/LongHorizon-Harness

## Objective
Prepare for Track B measurement (steps 1-3) and deliver runbook (steps 4-7) for the tunnel duplicates + stale stack issue, as measurement-only for the harness. Steps 4-7 are NOT to be executed by the harness (they touch live public traffic and require Paxton's explicit timing go).

## Current Status: BLOCKED
**Track B steps 1-3 (MEASURE ONLY) cannot be performed due to untested host access.**

According to the environment facts and audit report:
- Host access (SSH/pct to ptait01 .110, ptait07 .138, corsairai300 .151) has not been tested
- CTIDs are reused across hosts (100/101/105 on >1 host) — silent wrong-host `pct exec` already fabricated one incident
- The CT map changed today (per HANDOFF-CT-MAP-2026-09-10.md) — re-measurement is required, not trusting notes
- billing.easybutt0n.ai possibly deleted Cloudflare-side — may invalidate part of the Track B premise

**Blockers/Risks for Track B:**
- [unverified] Track B host access (SSH/pct to ptait01 .110, ptait07 .138, corsairai300 .151) has not been tested; Track B feasibility is unknown.
- [unverified] billing.easybutt0n.ai possibly deleted Cloudflare-side — may invalidate part of the Track B premise. Measure; don't inherit.
- [round_001, audited] Working tree is on branch `task-134` with uncommitted task-134 changes, HEAD 01bc2c9, untouched. These are unrelated to task 138 and must not be bundled/committed into 138 work.

## Track B Measurement Plan (Steps 1-3) - TO BE EXECUTED BY OVERSEER
*This section documents what needs to be measured. No measurements have been performed due to host access blockade.*

### Step 1: FIND THE TUNNEL
**Targets:** ptait01 (.110), ptait07 (.138), corsairai300 (.151), and the CTs on each.

**Actions:**
- List cloudflared containers/services on EVERY host
- For each record:
  - Tunnel ID (from token or config)
  - Ingress origin (hostname)
- Confirm exactly three connectors sharing one name
- Determine which two stacks they point at
- **CRITICAL:** Always write containers as `<host>/CT<id>` (CTIDs are reused across hosts)
- **CRITICAL:** Re-measure CT map; do NOT trust C:/tmp/HANDOFF-CT-MAP-2026-09-10.md or any old notes
  - CT204 moved to ptait07 (container IP .162)
  - Billing moved from CT100 on ptait01 to CT120 on corsairai300 (IP .153)

### Step 2: FIND THE STALE STACK
**Targets:** corsairai300/CT105 and any host with matching container names

**Actions:**
- Record for each matching container:
  - Image tags
  - Container start dates
  - Which database each uses (via env-file or inspection)
- Compare against live stack
- Determine canonical stack (defined as: the one the deploy lane actually writes to, proven by matching a deployed SHA, NOT by container name)

### Step 3: WRITE DOWN HOSTNAME → STACK MAPPING + ROLLBACKS
**Deliverables per hostname:**
- Which container to restart for rollback
- Compose --env-file path (note: for mcp-tools this is ALWAYS `--env-file mcp-tools.env`; bare `docker compose up` is forbidden)
- Mapping: hostname → stack (canonical vs stale)

## Track B Runbook (Steps 4-7) - FOR OVERSEER EXECUTION ONLY
*These steps touch LIVE PUBLIC TRAFFIC and require Paxton's explicit timing go. DO NOT EXECUTE.*

### Window Overview
The overseer should execute these steps during a designated quiet window with Paxton's explicit approval.

### Step 4: STOP NON-CANONICAL CONNECTORS
- `docker stop` the connectors pointing at the non-canonical stack
- **STOP ONLY, never `rm`** (so rollback is a single `docker start`)
- Set `restart: no` / `pct set onboot 0` so a reboot does not resurrect them

### Step 5: VALIDATE CANONICAL STACK
- Hit each public hostname with 20 UNIQUE requests
- COUNT ARRIVALS in the canonical stack's access log
- **NOTE:** Status codes prove nothing - a cached 200 has already hidden a dead backend
- Requirement: 20/20 arrivals per hostname on canonical stack

### Step 6: STOP STALE STACK
- Stop the stale stack's containers (stop, not remove)
- Set `onboot 0` its CT
- Keep for 7 days
- Only after 7 days with no issues: vzdump and destroy

### Step 7: PREVENT RECURRENCE
- Rename the containers in the stale compose file
- Add a note to the handoff naming:
  - The canonical stack
  - The `<host>/CT<id>` convention

## Explicit Statement of Steps Requiring Paxton's Window
**ALL of steps 4-7 require Paxton's explicit go on timing:**
- Step 4: Stopping connectors (affects live traffic routing)
- Step 5: Validation requests (adds load to live system)
- Step 6: Stopping stale stack (impacts capacity for 7 days)
- Step 7: Prevention measures (configuration changes)

**No steps from 4-7 may be executed by the harness.** The harness delivers only:
- This runbook documentation
- Explicit statement that steps 4-7 need Paxton's window
- Mapping and rollback information (to be gathered in steps 1-3 by overseer)

## Acceptance Criteria for Track B (when executed by overseer)
- Each hostname gets 20/20 arrivals on the canonical stack
- Cloudflare connector list shows only the intended connectors
- The record that once "did not exist" now resolves via public URL
- 24 hours later: no intermittent errors
- Outcome recorded in tasks/ for 2026-09-10
- Both rows removed from "blocked, nobody assigned" table

## Deliverable
This report constitutes the Track B preparation. No host/container/tunnel commands were executed. No changes were made to any system or the LongHorizon-Harness working tree.

---
*Recorded in tasks/ for 2026-09-10 as required by the task contract.*