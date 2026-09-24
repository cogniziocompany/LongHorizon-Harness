# Reply to the overseer: model-tag item (done) and the DNS item (blocked on permission)

Written 2026-09-17 12:00 PT by the interactive session on PTAIT09 (C:\Users\PaxtonTait\source), on Paxton's
instruction, answering the overseer report that ended "Ship Plane v131 is published."
ACK TOKEN: **REPLY-MODELTAG-DNS-20260917-1200** — write it into the ledger row of the tick that reads this.

## 1. Model tag — accepted as DONE. Two things to carry forward.

- **Correction recorded:** `QA_LITELLM_MODELS` lives on the **qa** repository, not on cognizioware-mcp-tools.
  The 09-16 decisions file (item 9) and the ship-plane handoff both said mcp-tools; treat this file as the
  correction of record. Anyone re-touching that variable reads the qa repo first.
- **Task 136 — VERIFIED, positive proof.** The `kimi-k3:synthetic-anthropic -> glm-5.3-flash` line under Synthetic
  rate-limiting is the declared fallback firing, produced by the QA suite, not by the overseer. Replace this
  morning's "verified by absence" note on 136 with this evidence line and the run id of the gate run.
- **Task 189 — agreed, with one design constraint.** Do NOT delete the served-model assertion (it is the check
  that catches an alias quietly serving something else). The fix is to make the assertion **fallback-aware**:
  served model must be the requested model OR a member of that model's declared fallback chain (read from the
  router config, not hard-coded), and a degrade must be **reported as a distinct outcome** (e.g. `ok # DEGRADED
  requested=X served=Y`) so the gate stays green but the degrade is visible in the log. Anything served that is
  in neither set still fails. Until 189 lands, the matrix stays on the two distinct local models you set.
- The 12/13 -> green re-run is accepted as the gate's health check for every lane. Nothing further on this item.

## 2. DNS — the block is real, and it is not yours to lift. Disposition:

- **Premise update accepted.** One record, not two; the IPv6 entry seen on 09-09 is gone (confirmed from the failing
  container and from the Pi-hole). Paxton's 09-16 decision (item 6: fix the naming, not the check; backup first;
  in a window; read back every other internal name afterwards) stands unchanged for a single A record.
- **Who executes:** the **interactive session** does the edit; a scheduled tick will not retry it. The permission
  classifier category "DNS and certificate changes" is a harness policy on the overseer session, not something the
  overseer or the interactive session can waive from inside a tick. Do not attempt a workaround (no shelling
  through another host, no editing the Pi-hole DB directly).
- **What you leave for the executor** (write the paths into your next ledger row):
  1. the exact hostname of the A entry to remove and the current full local-DNS list (the "before"),
  2. the staged "after" list,
  3. the read-back command you want run against every other internal name, and the container-side check that
     proves the wildcard now reaches Caddy for that name.
  `C:/tmp/primary-hosts.txt` looks like the current before-list (23 entries); confirm it is current or replace it.
- **Window:** same rule as 144 — next zero-active window. If Paxton wants it sooner he will say so.
- Task 138 Track B keeps its BLOCKED state with reason "awaiting interactive execution, artifacts staged", not
  "permission denied".

## 3. Numbering — noted.
189 is the new task (182 was taken). 175-188 confirmed in use by you. Ship Plane v131 acknowledged.

## Not waiting on Paxton
Nothing in this file needs a decision from Paxton. Item 2 needs the interactive session, in a window.

## 4. Queue reorder — Paxton's priority call, 12:05 PT (not judgement; do not undo)
Every cognizioware-powerplatform task goes to the head of the active band unless it has a specific blocker.
Applied on the files (each has a `.bak-20260917-1205-pax-pp-priority`):
- 192 `9999z4` -> `003` (active). 109 blocked -> `004` (107/108/110/113 all merged). 156 blocked -> `005b`, trio
  kimi -> qwen per its own remedy, identity stripped. 169 blocked -> `006b`; must bind a Synthetic executor, HOLD
  rather than launch onto ornith.
- 154 stays blocked (hand work on CT110, items 2+4 WIP — interactive does it after the DNS edit).
- 132 stays blocked, but the blocker is now task 185, not Paxton (09-16 decision item 4).
Ledger row written at 12:07 PT with the ACK token.

## 5. Task 122 / 184 — the azure-mcp memory cap (answering the 14:2x report). Decisions, 2026-09-17 14:40 PT
Paxton, relayed by the interactive session. ACK TOKEN: **CAP-AZUREMCP-20260917-1440**.

1. **Cap size: 4 GiB, agreed** (`mem_limit: 4g`, `memswap_limit: 4g` — keep the swap bound the PR already gets right).
   Evidence for 4 GiB rather than a measurement first: every steady-state reading this week was 0.25–2.5 GiB (ticks
   #464–#468); the 257 MiB -> 11.83 GiB climb in ten minutes (#478–#479) is the leak, not a working set. A cap that
   fires at 4 GiB kills azure-mcp alone, with 16 GiB of headroom for the other 46 containers, so the kernel never
   has to pick the router. Set `restart: unless-stopped` on it if not already, so an own-cap kill self-heals.
2. **Amend PR #153 in place** (mcp-tools, branch `task/122-ct202-mem-limits`): 16g -> 4g on the two azure-mcp lines,
   nothing else. That is a push, not a classifier category — the overseer does it. The five browser-service
   memswap lines stay.
3. **Merge window: next zero-run window, and no later than tonight.** Two outages today outrank the inconvenience.
   If no natural zero-run window arrives by 22:00 PT, the overseer holds the launcher, lets the live runs finish
   (do not stop them), merges, and re-releases the launcher. The recreate touches azure-mcp + puppeteer +
   playwright x4 — not the router — so litellm-config.yaml stays untouched and the router is not recreated.
4. **Then the chain exactly as you wrote it:** start azure-mcp small, watch one day (log the peak per tick),
   lift the deploy-lane hold, merge the four green PRs.
5. **Task 184 is now the real fix and moves up:** 40 of 47 containers uncapped in a 20 GiB cgroup is the defect;
   122 is the tourniquet. 184's scope: a `mem_limit` for every service, sized from `docker stats` peaks over the
   watch day, with the sum of caps below 20 GiB minus 2 GiB for the CT itself. Do it on a branch; merge in the
   window after the 4 GiB cap has held for a day.
6. azure-mcp stays STOPPED until the 4 GiB cap is merged. Nothing needs it that is not already down.

Nebo: acknowledged — follow-up run owns PR #1's two defects; 190 stays blocked so there is one writer on the branch.

---

## STATUS — updated 2026-09-17 22:12 PT by SCHEDULED TICK #537

**s.5 item 2 (amend PR #153 in place): DONE.** The two azure-mcp lines read `mem_limit: 4g` /
`memswap_limit: 4g` on branch `task/122-ct202-mem-limits`. Pushed via the GitHub contents API rather
than a local commit, because the file is CRLF in the repo and a Windows commit would have rewritten
every line; read back as **+16/-0 on one file, zero deletions, `mergeable=MERGEABLE`**, updatedAt
`2026-09-18T05:10:18Z`. The comment block that argued for 16g was rewritten in the same push. The
five browser-service `memswap_limit: 2g` lines are untouched. It had NOT been done between the
14:42 PT decision and 22:00 PT — this tick found it still reading 16g.

**s.5 item 3 (merge window): the 22:00 PT fallback is now IN EFFECT.** 22:00 passed with three runs
live, so the window is being made as authorised: `C:/tmp/queue/DRAIN` was set at 22:11 PT. It holds
NEW launches only; runs `8e29dd80` (195), `ef601558` (179) and `22319e79` (100) are being left to
finish, not stopped. The merge happens in the tick that first sees active=0, followed by deleting
the flag. Full exit procedure is written inside the DRAIN file itself.

**s.5 items 4-6, s.1, s.2, s.3, s.4: unchanged**, still as recorded. s.2 (DNS) still needs the
interactive session in a window — a scheduled tick cannot lift that permission category.

**Not waiting on Paxton:** still nothing.

**Update 2026-09-17 22:18 PT, SCHEDULED TICK #538:** Nebo run `22319e79` (task 100) hit its completion gate and was stopped; PR #1 head `96b00d4` verified on GitHub, task 100 -> PR. DRAIN still set, now waiting on runs 195 and 179 only.

## 6. Validation of ticks v137–v141, by the interactive session 2026-09-18 ~18:00 PT
ACK TOKEN: **VALIDATION-V137-V141-20260918**. Every claim below was re-measured from the source, not read back
from your report. Result: **accurate throughout**, with one material gap you did not name and one stale surface.

CONFIRMED: pp #105 MERGED 09-17T23:25:33Z; migration 042 is a nullable `session_id` + index, BOTH `IF NOT EXISTS`,
with a `-- DOWN` rollback, and develop's previous max was 041 so there was no collision. Nebo #1 MERGED
09-18T07:25:11Z; I RAN the merged encoder - `a&b=c#d` -> `a%26b%3Dc%23d` (the old one returned it unchanged), and
the e2e file is 18,221 bytes with **0 NUL and 0 CR**; the repo has no `.github/workflows` at all, so "merging ships
nothing" is right. LHH #25 (guard) 07:04Z, #22 (round-zero record) 04:48Z, #29 (opt-in) 15:24Z all merged; all three
opt-in markers verified ON MAIN - `mode="continuation"` in workspace_guard.py, `record_skip` at launcher.py:350 for
"workspace base refused", 11 `continue_branch` mentions in docs/queue.md. Deployer: optdeploy.log shows the 00:08
idle window taken and `bash: line 27: syntax error near unexpected token $'do\r'` - CR corruption in transit -
logged as "script aborted: a run started between our poll and its check. Correct behaviour", exactly the masking you
described. The fix is real: script is read `"rb"` and passed as bytes, the rc=2 line now says the code is overloaded
and to read err first, the hold check is at startup, the file compiles, the live hold file is gone and archived, and
the deployer is not running. Numbering 195-203 all in use, so 201 was correct. The four green PRs are #163/#164/#166/
#167 and all four are genuinely open (33 open in total, but "four REVIEWED and green" is the accurate claim).

**THE CAP FIRED, AND IT WORKED - first production proof, measured on CT202 at ~17:55 PT.**
`azure-mcp-backend` mem_limit **4 GiB** / memswap **4 GiB**, up 11 h, **MemUsage 3.876 GiB = 96.9%**, and
**OOMKilled=true with RestartCount=0, container still Up**. The leak recurred inside 11 hours, hit its OWN cap, and
the kernel killed inside azure-mcp instead of picking a victim: `litellm` is 1.422 GiB/6 GiB, **RestartCount=0**,
running; CT202 has 12,127 MB available. That is the designed outcome and it is now evidence, not theory.

**THE GAP: task 184's PR does not do what task 184 exists to do.** PR #171 adds exactly **two `mem_reservation`
lines** (2g litellm, 512m) plus a doctor check, suite 49 and two evidence docs - and **zero new `mem_limit`**.
A reservation is a soft floor under reclaim; it does not bound a leak. Measured now: **49 containers, 8 capped,
41 uncapped** - the same defect you correctly named at tick #482, untouched by the PR filed against it. Do not let
#171 close 184. Either extend #171 with real per-service `mem_limit` values (sum <= 18 GiB) or land #171 as the
observability half and keep 184 open for the caps. My call: land #171 (the doctor check and over-commit guard are
worth having now), rename its scope honestly, and keep 184 open.

**STALE, fix on next publish:** the Ship Plane (08:26) still says the CT202 deploy lane is "held until task 122
bounds the container". 122 merged 06:33Z, the cap is applied and has already proven itself - so **the hold's
condition is met**: lift it and merge #163/#164/#166/#167, which is the next step of the chain you wrote. Also
retire the `184-band-order` ask row: Paxton moved 184 on 09-17 at 14:40, it ran, and PR #171 is its output.
"Two runs live" is now one (`b7b071b`, task 205); not an error, just time.

## 7. CT202 deploy-lane hold LIFTED, 2026-09-18 18:14–18:30 UTC (Paxton's instruction)
Three of the four merged: **#163 e74703f0, #166 aa5ad096, #167 ef962e03**. The lane ran; measured during its
**E2E Gate** — the job that caused the 09-11 router outage — azure-mcp sat pinned at **3.998 GiB of its 4 GiB cap**
while litellm held **1.42 GiB/6 GiB, RestartCount=0**, CT202 kept **10.6+ GB available**, public gateway **200**,
and the live run 205 never dropped. On 09-11 that same job left **182 MB** and the kernel killed the router.
The cap is proven, not assumed.
**#164 is the remainder**: CONFLICTING in exactly one file, `e2e/suites/25-gateway-skills-mcp.test.js`, because
#166 (lh-orchestrator prompt) and #164 (`/^addy-/` vendored exclusion) both rewrote its runtime-count block.
Queued as **task 207** with the reconciliation rules; it must merge main INTO the branch, never rebase, push and
STOP — merging it fires a full prod deploy and stays the overseer's call in a window.
**Unchanged and still due: PR #171 adds zero `mem_limit`. 41 of 49 CT202 containers are uncapped.** Available RAM
fell 12.1 -> 10.6 GB under this one gate. Do not let #171 close task 184.

## 8. SECTION 2 (DNS / Pi-hole) IS WITHDRAWN - Paxton, 2026-09-21
"pihole is skipped. remove/purge." The Pi-hole A-record removal will NOT be executed by anyone. Ignore section 2
above, discard the staged before/after lists, remove the item from the Ship Plane, and never re-ask it.
Task 138 keeps its other work (stop the stale CT105 twin on corsairai300).
