# Handoff: Paxton's answers to all 14 OPEN-ASKS rows, 2026-09-21
Collected one by one by the interactive session on PTAIT09 (click-through questions; the answers are HIS).
ACK TOKEN: **PAX-DECISIONS-20260921-14ROWS** - write it into the ledger row of the tick that reads this, and
render the Ship Plane "Waiting on you" from the updated OPEN-ASKS state column (backup: `.bak-20260921-paxton-answers`).

## Closed or done - remove from "Waiting on you"
- **87-copilot-phase2** - DEFER AND CLOSE. No Copilot spend. Merge #162 on its merits. Do not re-ask.
- **kb-hook-order** - MOOT: task 199 launched 09-18, band drained since.
- **pp-116-main-base** - RETARGETED main -> develop (done, read back). Now CONFLICTING -> **task 209** queued (continuation).
- **stranding-readmission** - POWERPLATFORM ONLY. 53 + 59 readmitted at the tail; **101 and 108 were never strandings**
  (pp #105 / #106 merged) and are closed as landed. **The stranding count is now 20**, and the other 20 stay parked by
  his choice - do not re-ask.
- **150-spec-staging-decisions** - SS8 = MIRROR, SS9 = each vendored copy's own semantics. 150 stays blocked only for its window.
- **56-eval-env** - KEEP DEFERRED, LOW. Do not re-ask.
- **Pi-hole DNS item** - WITHDRAWN earlier today ("pihole is skipped. remove/purge").

## Answered YES - Paxton does the credential himself; row stays open until the read-back passes
- **134-ct103-db** - runs `C:/tmp/provision-lh_harness.sh` via Git Bash. Env var is `LH_HARNESS_DB_PASSWORD` (a password, not
  a DSN). CT103 is on **ptait01**; Postgres is the container `cognizioware-postgres-uat`; no pg_hba edit needed.
  **CT110 also needs `pip install "psycopg[binary]"`** before PgQueueStore can connect - yours, at the #19 deploy.
- **138-checks-read** - editing the PAT now. Read back against a PRIVATE repo.
- **189-refresh-key** - one-paste mint + `gh secret set` (CREDS-RUNBOOK s.3).
- **107-dev-identity** - CREATE the throwaway Entra user (not the downgrade). CREDS-RUNBOOK s.4.
- **135-seq-read-key** - mint a Read-only Seq key, masked delivery. CREDS-RUNBOOK s.5.
- **187-github-app** - the **GitHub App**, not the webhook alternative. Existing handoff 2026-09-15 s.1-3.
- **mcp-177-workflow-merge** - APPROVED; he merges it himself.

## Answered YES - OVERSEER-EXECUTED (the interactive session's classifier blocks host writes and merges)
- **ollama-cloud-mem-cap** - DO BOTH. Measured runbook `C:/tmp/OLLAMA-CLOUD-CAP-RUNBOOK-2026-09-21.md`: 12g cap via compose
  (**always `--env-file ptait01-inference.env`**), and a litellm-config PR repointing `ornith-1.5:pool` off
  `ornith-1.5:9b-256k`. Both in a zero-active window. Then release task 190.

## Also true today
LHH #19 MERGED (Paxton). Task 208 (reconcile #20, add `requeue` to PgQueueStore) launched 11:05 PT. Active band now:
9999zl-53, 9999zm-59, 9999zn-209. The launcher exits on an empty queue - confirm it is running.
