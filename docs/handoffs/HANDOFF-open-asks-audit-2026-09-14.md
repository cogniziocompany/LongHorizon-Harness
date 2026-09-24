# Handoff for longhorizon-harness-99 — OPEN-ASKS.md audit, from session [fe2679], 2026-09-14

Paxton attached your OPEN-ASKS.md (as of 16:43 PT) and asked for help with it. Bottom line agrees with your
L483: nothing is genuinely on Paxton. Ten items, three of which change queue behaviour (1, 5, 7).

1. **The GitHub-capability premise flipped; the queue was not re-swept.** L30 (09-10) says CT110 has no gh
   and no token, so PR work is structurally impossible for a run. Several blocked entries were parked on that
   basis (144, 147, 149, 150, 102; and [fe2679]'s 162/163/164/132 notes cite it). L486-510: token installed,
   push + PR proven, service restarted 16:21 with GH_TOKEN, STEP 1 doctrine changed. Amend the L30 row
   ("superseded 09-14: runs push/PR; merge stays the overseer's") and sweep blocked/ for entries whose ONLY
   blocker was "no gh" — release to a run or restate the real blocker. Task 102's note carries the old decision.
2. The "three runs stalled on git ops; deserves its own task" pattern in your 16:xx status is moot after 16:21.
   If a task was queued for it, rescope to "merge only" or drop.
3. Open table (L18) still lists 144-window while L448 says it moved to Answered. Remove the row; state that
   the table is empty.
4. "STILL OPEN — Seq ingest key" heading (L392) is contradicted by L417. Strike it.
5. **Retention is NOT live** (L455, task 170). Audit section K assumed task 115 had landed it. Nothing else in K
   changes (Paxton: no migration), but 125's supersession is "covered by retention" only once 170 reads the
   setting back from the router DB.
6. Ruflo section (L257-284) still says the gateway points at dead .218 and waits for a yaml edit; LEDGER
   L20583 says mcp-tools.env L49 = .155 landed and survived the recreate. Mark resolved (task 171 owns the
   remaining drift in repo files).
7. **Synthetic CAP = 3 accepted (L480)** is a launcher constant + restart. Verify applied: `grep -n "^CAP\|SYN_CAP"
   C:/tmp/launch_queue.py` and process start time > file mtime. LEDGER L20867 mentions "held the next six at
   SYN_CAP", so it may be live — read it back and record it.
8. Typo that misdirects a run: L382 "ptait06-easybutt0n-ai" → ptait09.
9. Structure: 511 lines, ~40 live. Split into OPEN-ASKS.md (rules + table + "on Paxton now") and
   ASKS-HISTORY.md (all struck/answered), linked by id.
10. Security note to keep, not an ask: terranas01 sshd rotated in place on a 5-day uptime (L198-207).

Also from the same audit pass (section M of C:/tmp/HANDOFF-overseer-audit-2026-09-11.md, updated today):
- **Task 114 is in done/ with run 476970f4 (09-11 03:51Z) and no later ledger row.** Your stranding sweep
  reports 0 for the last ticks; 148b and 145 say they are blocked on 114. Read /api/runs/20260911T035112Z_476970f4
  and either requeue or record why it is not a stranding.
- Six rows of your fleet-plane handoff are wrong against the repos (details in that file's appended
  CORRECTIONS section): task 55's three secondary commits are all on main; hydra-ci.yml HAS a deploy job;
  feat/harness-fleet's content is already in main (only PR #5 remains, Paxton: close as superseded);
  hydrafleet already has run/gate/capacity tools — only enqueue is missing; the 413 is one line at
  fleet-admin/src/server.js L40 plus an unbounded reporter payload; the interlock has a home in
  fleet-routes.js L94.
- Ledger `### TICK` headers stopped 09-11 (last is [fe2679]'s); 09-14 rows are bullets. Cosmetic, but your
  own "latest TICK" greps will mislead you.
