# Guest launch — E2E readiness, 2026-09-09 23:55 PT

**Supersedes** `guest-e2e-readiness-2026-09-07.md` (which stops at 2026-09-08 and is kept as the
historical record — do not edit it). This file answers one question: **how close are we?**

Everything in "Measured tonight" was read from the live systems at 23:47–23:52 PT. Anything I did
not read myself is marked **UNVERIFIED**. The old file's forecast dates were written on 2026-09-07
and are re-based here against what actually shipped.

---

## The short answer

**The dev guest path is code-complete and switched ON. The thing standing between us and a
click-through is not code — it is one unhealthy lane and an unconfigured prod.**

| Layer | State | Evidence |
|---|---|---|
| pp app, dev | **LIVE** | `192.168.21.163:3000/api/health` → **200** in 0.19s |
| pp app, uat | **LIVE** | `192.168.21.163:3001/api/health` → **200** in 0.07s |
| billing, uat | **LIVE** | `.153:20253/api/health/ready` → **200** |
| billing, prod | **LIVE** | `.153:20251/api/health/ready` → **200 healthy**, `sha-728a140` |
| billing, dev | **FAILED — not ready** | `.153:20252/api/health/ready` → **503 unhealthy**, `sha-608cf64` |
| guest switches, dev + uat | **ON** | `GuestChargingEnabled=true`, `GuestExecutionEnabled=true` |
| guest config, prod | **MISSING** | no `Billing__Guest*` / `Stripe__GuestMeter*` on `mcp-billing-service` |

---

## Measured tonight

### 1. Guest charging is now ON in dev and uat — this changed since the last file
On 2026-09-08 the record read *"charging deliberately left false"*. It is no longer false. Read from
the running containers on **CT100**:

```
billing-dev-lane-app-1    Billing__GuestChargingEnabled=true
                          Billing__GuestExecutionEnabled=true
                          Billing__NightlyCloseEnabled=false
                          Stripe__AllowLiveMode=false
                          Stripe__GuestMeterId=cognizioware_guest_tokens
billing-uat-lane-app-1    (identical, different metered price id)
```

Both lanes are on **Stripe TEST** (`AllowLiveMode=false`) and nightly close is off, so this is
observation-plus-charging against test objects — the intended pre-production posture, not a risk.
Note `GuestMeterId` now holds the **event name**, which is the 2026-09-08 stopgap; PR #90 (merged
2026-09-08 18:41) was supposed to replace that with a real `GuestMeterEventName` setting plus a
startup guard refusing an `mtr_`-shaped value. **The variable name is still `GuestMeterId`, so either
the guard shipped under the old key or the stopgap is still load-bearing — UNVERIFIED, and worth one
look before the golden run.**

### 2. Billing DEV is the blocker, and the cause looks like a defect
```json
{"status":"unhealthy","version":"sha-608cf64",
 "databases":{"secondary":"disabled","secondaryRequired":false,
   "replicationHealth":{"pendingSecondaryAck":1,"degraded":true}}}
```
`/api/health/live` returns **200** — the service is alive. Only `/ready` fails, and it fails because
**one** pending secondary ack sets `degraded: true` — while the secondary is `disabled` and
`secondaryRequired: false`. **A disabled, not-required secondary should not be able to make the
service un-ready.** That reads as a real defect in the readiness predicate introduced with the
bounded health checks (PR #85, 2026-09-08).

Why it matters beyond dev: `promote-billing` polls `/ready`. A permanently-degraded dev lane means
**the dev gate never goes green and promotion cannot start from dev.**

### 3. Prod billing exists now — that is new
On 2026-09-08 the promotion stopped at "Roll out prod lane" because `/opt/mcp-cognizioware-prod` did
not exist. Tonight `.153:20251` answers **healthy** on `sha-728a140`. So the prod lane is
provisioned and running. **But** the container carrying the live prod service shows **no**
`Billing__Guest*` or `Stripe__GuestMeter*` variables at all — the guest feature is not configured in
production, only in dev and uat.

Note dev is `sha-608cf64` and prod is `sha-728a140`. **Different shas, and prod is not obviously
behind dev** — which is unusual enough to check rather than assume. UNVERIFIED which is newer.

### 4. Merged since the last file
| PR | Merged | What |
|---|---|---|
| mcp-cognizioware #87 | 09-08 10:56 | prod lane adopts the live CT100 stack |
| mcp-cognizioware #90 | 09-08 18:41 | guest meter events keyed by event name |
| mcp-cognizioware #91 | 09-09 04:41 | guest kill switches in the admin UI |
| mcp-cognizioware #96 | 09-09 13:50 | rollout health poll window outlasts startup |

### 5. A correction to the previous file — AND a correction to that correction (2026-09-10 03:55)
The old file records *"CT100 on ptait07 192.168.21.138"*. I first wrote here that this was simply
wrong and that "CT100 is on ptait01". **That was itself wrong, and the real answer is worse: there
are TWO CT100s.** Proxmox CTIDs are per-host, and this estate reuses them. Measured 2026-09-10:

| host | CTID | name | IP | billing containers |
|---|---|---|---|---|
| ptait01 (.110) | 100 | `ct-100` | **192.168.21.153** | **7** — 20251 prod / 20252 dev / 20253 uat |
| ptait07 (.138) | 100 | `pp-dev-uat` | **192.168.21.163** | 0 — this is the pp app box (:3000, :3001) |

So the old file was not pointing at a nonexistent container; it was pointing at **the wrong one of
two real ones**. Both are load-bearing for the guest path: the billing lanes live on ptait01's CT100
and the pp app lanes live on ptait07's CT100.

**THE GENERAL TRAP: "CT100" is ambiguous without naming its host.** Three CTIDs are duplicated
across hosts — **100, 101 and 105**. CT105 is the one that has already burned us: the
"stale decoy powerplatform CT105" is corsairai300's, while ptait01's CT105 is a pihole. Always write
`<host>/CT<id>`, never a bare CTID.

---

## What actually remains

Ordered by what unblocks the most:

1. **Fix the dev readiness predicate** (or clear the single pending ack). Until `/ready` is 200,
   the dev gate cannot post green and promotion is stuck. This is the top item and it is small.
2. **Confirm the meter event-name setting really shipped** (item 1 above) — one read of the running
   config plus one guest usage event.
3. **Configure the guest feature in prod**, or make an explicit decision that prod stays guest-off
   for now. Right now it is off by absence, not by choice, and absence is not a decision.
4. **The human click-through on dev** — the thing everything else exists to enable. Blocked only by
   item 1 in practice.
5. **Operator items still outstanding** from the 09-07 audit's ten prerequisites — a non-prod
   Dataverse environment with System Customizer for ai-dev01 was still Paxton's as of 09-08 20:05.
   UNVERIFIED tonight.

## Re-based timeline

The 09-07 audit forecast: first full dev run 09-11, auditable dev E2E 09-15, UAT 09-18, earliest
prod 09-26 (seven test-mode closes). **The seven-closes wait was dropped by Paxton on 2026-09-08**
(procedural, not a code guard; the only code guard is `ProdStartupGuard` requiring
`Stripe__AllowLiveMode`), so the prod date is no longer gated on closes.

Against that: dev and uat are **ahead** of the forecast — both apps live, both lanes switched on,
charging enabled, prod lane provisioned early. The forecast's 09-11 "first full dev run" is
achievable **as soon as the dev readiness defect clears**, which is a small fix, not a slice.

**Honest position: we are one health-predicate fix and one click-through away from a dev E2E, with
prod configuration as a separate, deliberate decision.**

---

## GOLDEN QA RUN — `guest-e2e` target

This is the golden run for the guest path. It belongs with the other golden runs (task 113,
operator surfaces) and follows the same rule: **a golden run is only golden if its captures are
committed and diffable.**

**Scope:** Stripe TEST card → customer → environment grant → wizard → design → approve → build,
plus the usage event landing on the meter.

**Where it runs:** dev first (`192.168.21.163:3000` + billing `.153:20252`), then uat
(`:3001` + `.153:20253`). Not prod — prod has no guest configuration.

**Preconditions, each asserted rather than assumed:**
- `billing dev /api/health/ready` returns **200** (today it is 503 — the run must fail loudly on
  this, not skip past it)
- `GuestExecutionEnabled=true`, `GuestChargingEnabled=true`, `NightlyCloseEnabled=false`,
  `AllowLiveMode=false` — read from the running container, not from a file
- the guest meter event name resolves to `cognizioware_guest_tokens`, and a startup guard rejects an
  `mtr_`-shaped value

**Assertions:**
- `verifyAuth` PASS with a customer id that is **not** `internal` — the 09-07/09-08 seeding runs both
  produced `customerId=internal`, which proves the grant path but **not** the paying-customer path
- the Stripe test customer used actually **exists in that lane's Stripe account** — the 09-08 audit
  found the dev lane pinned `cus_TPFW7YS21tEYUs`, which does not exist in the dev account. Dev and
  uat are **different Stripe accounts**; a customer id is not portable between lanes and the run must
  assert per-lane
- a guest usage event moves the meter, and the writer line names the resolved event name
- the approval log / env-variable entry is created once and **reused** on a second solution in the
  same environment (no duplicates — Paxton's original requirement)

**Captures — COMMIT THEM.** Screenshots and reports are tracked, not gitignored; `.gitignore`
covers `_out/**/*.webm` only. The committed diff of those captures **is** the review mechanism.
Maintain the run-history table.

**The trap this run must not fall into:** a green result that proves nothing. Two precedents —
the E2E Gate reports success while a suite hard-fails underneath, and tonight a long-context test
passed only because a fallback answered. **Assert which lane and which customer actually served,
and treat a rollup-vs-detail mismatch as its own defect.**
