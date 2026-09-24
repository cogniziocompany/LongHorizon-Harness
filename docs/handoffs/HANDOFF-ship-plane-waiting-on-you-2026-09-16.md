# Handoff for the interactive overseer [5321285c]: the Ship Plane "Waiting on you" table is stale — remove it

Written 2026-09-16 22:24 PT by session [fe2679] at Paxton's instruction ("resolve these items and get them off the ship plane").
The 20:24 PT publish still carries the ten rows from the 13:31 report under "verified this tick". Every row was
answered by Paxton at 14:55 (C:/tmp/HANDOFF-paxton-decisions-2026-09-16.md), consumed by tick #322 (ACK token
PAX-DECISIONS-20260916-1331), and recorded in OPEN-ASKS, whose open table is EMPTY. The page and the file disagree
because the section is hand-spliced on publish. Fix the mechanism and the content:

MECHANISM: on every publish, render "Waiting on you" FROM the OPEN-ASKS open table (currently zero rows -> print
"Nothing is waiting on Paxton" with the timestamp), and move any narrative to the history page. Never carry a
row forward from the previous artifact.

CONTENT — disposition per row, with evidence:
1. CT202 disk / message bodies / 25 GB reclaim -> DECIDED. Retention settings now (task 170); the reclaim in the
   bundled 144 window; the 2-hourly export retires once retention is live. Task 144's text carries the ordered
   window. Not waiting on Paxton.
2. Paid model account -> DECIDED 09-14: no spend, CAP = 3 (SYN_CAP verified in launch_queue.py by tick). Row text
   is the 09-11 correction; retire it.
3. Customers cannot finish setup -> IN PROGRESS, nothing on Paxton: #108 (Item A) and #110 (Item C) merged;
   #112 back-button merged; #113 (Item B) open; 107's run continues. The row's own last column already says
   "this needs nothing from you".
4. Four of six repos have no reviewer -> DECIDED: harness is the reviewer (tasks 185-188), @claude/Copilot fallback
   only; count is 4 of 8 by the contents API, not 2 of 6. Hydra reviewer merge waits for the bundled window.
5. Sixty-five review comments -> "Nothing from you" by its own text; belongs in progress notes, not this table.
6. DNS test with two records -> DECIDED: remove both (A + AAAA) in one edit with a full-list backup, in a window.
   Overseer-executed (task 138 Track B territory / pihole). Not waiting on Paxton.
7. Four abandoned PRs -> DONE 2026-09-16 22:24 PT by [fe2679]: mcp-cognizioware #31, #32, #33, #34 closed, each with the
   task-102 reason in the closing comment (superseded by #58 / tools-gateway; false premise on #32/#34). Branches
   left in place for the overseer to delete after read-back, per 102's ordering rule.
8. Local floor -> DECIDED: manager + auditor only; executor fails fast. Implementation is launcher/trio config
   (146/155 territory); not waiting on Paxton.
9. Retiring old model tags -> DECIDED: QA_LITELLM_MODELS = ornith-1.5:pool,kimi-k3:synthetic-anthropic. NOT SET
   by [fe2679]: the variable lives on cognizioware-mcp-tools but its consumer suite is in no local clone, and a
   wrong value breaks the QA gate that serves every lane. Whoever lands PR #120 sets it after confirming the QA
   key can reach both models (read /v1/models with that key), then re-runs the gate. Not waiting on Paxton.
10. Evaluation environment provisioning -> lowest priority by Paxton's direction; it IS a genuine ask (VM, build
    agent, two environments) but "nothing waits on it" - keep it as the ONE row in the table if you want a
    non-empty table, marked low.

Also stale on the same page and worth fixing in the same publish: "Blocked with nobody assigned" (item 1 ->
task 159; item 2 -> CT105 stop decided, in 144's window; item 3 -> PR #111 merged); the heuristic state board
(133/157/151/143/55 show PR or FAILED for shipped work; 134 FAILED with PR #19 open; 153 FAILED with no requeue;
148b GATED with PR #50 open); 136 VERIFIED before its own proof criterion; 115 VERIFIED while 170 says retention
is not live; duplicate task numbers 178/179/180/184 (mine are now 185-188).


## STATUS - updated 2026-09-16 22:26 PT by SCHEDULED TICK #371

**Item 7 is now CLOSED END-TO-END.** [fe2679] closed mcp-cognizioware #31/#32/#33/#34 at 05:22Z and left the
branches for the overseer per task 102's ordering rule. This tick did the read-back and the deletion:
all four PRs confirmed CLOSED, then all four branches deleted and **read back 404** -
`cursor/github-mcp-configuration-verification-63a4` (bd409bd9), `cursor/story-4-2-rbac-verification-c97a` (b0801ea2),
`infrastructure/docker-deployment` (d327f683), `cursor/story-4-2-rbac-tests-4f40` (ed903daf).
Checked before deleting: none was the base of any PR (open or closed) and all four last had commits in Feb 2026.

**Side effect worth carrying:** mcp-cognizioware open PRs went 8 -> 4, so the **fleet open-PR total is 38, not the
42 that ticks #366-#370 reported.** Any surface still printing 42 is stale.

**Everything else in this file remains for the INTERACTIVE session** - the Ship Plane republish (mechanism +
content) cannot be done by a scheduled tick, and this tick did not fake one.
