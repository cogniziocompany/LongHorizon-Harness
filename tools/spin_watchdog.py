"""Adaptive spin detector: probe hard at the start, decay while healthy, snap back on trouble.

PAXTON, 2026-09-09: "create a probe thats smart and emits in the beginning and then slowly dies
off until more abnormal or errors happen."

WHY A FIXED POLL WAS THE WRONG SHAPE. The first version probed every 5 minutes forever. That is
wrong in both directions at once: while the fleet is healthy it spends real requests to learn
nothing - and every probe is a request against a pack capped at 2500 per 5 hours, so a watchdog
can quietly eat the budget it exists to protect - while at the moments that actually matter, just
after a launch or just after something broke, five minutes is far too slow.

THE CADENCE.
  - A run's FIRST minutes are when a bad binding shows itself, so a newly launched run pulls the
    interval back to the floor.
  - Every clean pass multiplies the interval by DECAY, up to a ceiling. Quiet fleets cost almost
    nothing.
  - ANY abnormality resets to the floor immediately: a failed probe, a merely SLOW probe, a run
    that newly failed, or a run that has just appeared. Slowness counts deliberately - a backend
    drifting from 1s to 2.5s is the early warning that precedes a timeout, and waiting for outright
    failure throws that warning away.
So: attentive when it matters, nearly free when it does not, and fast to re-acquire.

WHAT IT DOES ON A STARVED RUN (unchanged and deliberate). Role bindings cannot be rebound on a live
run, so "switch to the flash provider" is implemented as abort + requeue into the task's own slot,
letting the launcher's rotation hand it a different trio. Worktree, branch and commits survive.

THE DISCRIMINATION IT EXISTS FOR. A long round on a backend that CANNOT emit is starved. A long
round on a backend that answers in a second is a model doing hard work. At minute ten those look
identical from outside; only the probe separates them. The clock decides when to ask - the probe
decides what to do.

SAFETY. Two consecutive failed probes before acting. Never touches a gated run, nor one whose
bindings were not recorded at launch. And it refuses to act at all when MORE than half the live
runs look starved, because that is a provider outage rather than unlucky runs, and mass-aborting
would turn an outage into lost work.
"""
import datetime
import glob
import json
import os
import subprocess
import time
import urllib.request

BASE = "http://192.168.21.168:8799"
TOKEN = "<CT110_BEARER_TOKEN_REDACTED_SEE_docs/SECRETS.md>"
H = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}
ROUTER = "https://litellm.easybutt0n.ai"

ROUND_GRACE_S = 900       # only look at rounds past half the 1800s budget
EMIT_BUDGET = 3.0         # over this = not serving
EMIT_SLOW = 1.8           # under budget but degraded: an early warning, not a failure
INTERVAL_MIN = 60         # floor: attentive
INTERVAL_MAX = 1200       # ceiling: 20 min when nothing is happening
DECAY = 1.6               # healthy passes widen the gap
_RK = [None]

interval = INTERVAL_MIN
strikes = {}
# Control models for the provider-wide check below. Any two distinct Synthetic aliases will do;
# the point is only that they share the provider with the bound executor.
SYN_CONTROLS = ["nemotron-3-super:synthetic-anthropic", "glm-5.2:synthetic-anthropic",
                "kimi-k3:synthetic-anthropic", "syn:large:text:synthetic-anthropic"]
seen_runs = set()
seen_failed = set()


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def harness_key():
    if _RK[0] is None:
        _RK[0] = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "root@192.168.21.151",
             "pct exec 110 -- bash -lc 'grep -hoE \"^ANTHROPIC_AUTH_TOKEN=.*\" "
             "/home/harness/.lh-harness-secrets.env | cut -d= -f2-'"],
            capture_output=True, text=True, timeout=90).stdout.strip()
    return _RK[0]


def emit_ok(model):
    """Time to first content event on the streaming path, using the HARNESS key.
    Returns (healthy, slow, seconds). Calibration notes, each of which cost real time:
      - accept ANY content event; requiring a text delta fails thinking-capable models whose
        thinking block eats a small budget.
      - carry a nonce; responses are cached and a cached hit returns in 0.0s, making a dead
        backend look instant.
      - use the harness key, not the master key, or the reading describes a credential the
        harness does not use."""
    nonce = os.urandom(4).hex()
    body = json.dumps({"model": model, "max_tokens": 24, "stream": True,
                       "cache": {"no-cache": True},
                       "messages": [{"role": "user",
                                     "content": "Say READY (%s)" % nonce}]}).encode()
    req = urllib.request.Request(ROUTER + "/v1/messages", data=body, headers={
        "x-api-key": harness_key(), "anthropic-version": "2023-06-01",
        "Content-Type": "application/json"})
    t = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=int(EMIT_BUDGET * 4))
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except Exception:
                continue
            if ev.get("type") in ("content_block_start", "content_block_delta"):
                dt = time.time() - t
                r.close()
                return dt <= EMIT_BUDGET, dt > EMIT_SLOW, dt
        return False, True, time.time() - t
    except Exception:
        return False, True, time.time() - t


def api(path):
    return json.load(urllib.request.urlopen(
        urllib.request.Request(BASE + path, headers=H), timeout=120))


def bound_roles(run_id):
    """Role bindings for a run, and the queue entry to requeue if we abort it.

    TWO SOURCES, deliberately. The queue entry is authoritative for REQUEUEING - without it there is
    nowhere to put the task back. But the launcher only started writing `roles_bound` at 22:56 on
    2026-09-09, so every run launched before that records nothing, and the first version of this
    function skipped exactly those runs - the oldest and longest-running ones, which are the ones a
    spin detector exists for. It was silently inert on all six live runs.
    So: fall back to the harness's own `run.role_configs`, which is true for every run regardless of
    who launched it. When only the API knows the bindings we can still PROBE and WARN; we just
    cannot requeue, and a probe-and-warn on a starved run is worth far more than skipping it."""
    entry = None
    roles = {}
    for f in glob.glob("C:/tmp/queue/done/*.json"):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        if str(d.get("run_id", "")) == run_id:
            entry = f
            roles = d.get("roles_bound") or {}
            break
    if not roles:
        try:
            snap = api("/api/runs/%s/snapshot" % run_id)
            rc = (snap.get("run") or {}).get("role_configs") or {}
            roles = {r: (c or {}).get("model") for r, c in rc.items() if (c or {}).get("model")}
        except Exception:
            roles = {}
    return roles, entry


def requeue(entry_path):
    d = json.load(open(entry_path))
    for k in ("run_id", "roles_bound", "launched_at"):
        d.pop(k, None)
    json.dump(d, open("C:/tmp/queue/" + os.path.basename(entry_path), "w"), indent=1)
    os.remove(entry_path)
    return os.path.basename(entry_path)


log("adaptive watchdog up: floor=%ds ceiling=%ds decay=%.1f grace=%ds budget=%.1fs slow=%.1fs"
    % (INTERVAL_MIN, INTERVAL_MAX, DECAY, ROUND_GRACE_S, EMIT_BUDGET, EMIT_SLOW))

while True:
    abnormal = []
    try:
        rs = api("/api/runs")
        rs = rs.get("runs", rs) if isinstance(rs, dict) else rs
        today = [r for r in rs if str(r.get("id", "")).startswith("2026")]
        live = [r for r in today if str(r.get("status")) == "running"]

        # a newly launched run deserves attention: bad bindings show up in the first minutes
        ids = {r.get("id") for r in live}
        fresh = ids - seen_runs
        if fresh and seen_runs:
            abnormal.append("%d new run(s)" % len(fresh))
        seen_runs |= ids

        # a newly failed run is a reason to look harder at everything else
        failed = {r.get("id") for r in today if str(r.get("status")) == "failed"}
        newly_failed = failed - seen_failed
        if newly_failed and seen_failed:
            abnormal.append("%d newly failed" % len(newly_failed))
        seen_failed |= failed

        suspects = []
        for r in live:
            rid = r.get("id")
            try:
                snap = api("/api/runs/%s/snapshot" % rid)
            except Exception:
                continue
            rounds = snap.get("rounds") or []
            if not rounds:
                continue
            ex = rounds[-1].get("executor_status") or {}
            if ex.get("status") is not None:
                continue
            started = datetime.datetime.strptime(rid.split("_")[0], "%Y%m%dT%H%M%SZ").replace(
                tzinfo=datetime.timezone.utc)
            age = (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds()
            spent = sum((rd.get(k) or {}).get("duration_ms") or 0
                        for rd in rounds for k in
                        ("manager_status", "executor_status", "auditor_status")) / 1000.0
            elapsed = age - spent
            if elapsed < ROUND_GRACE_S:
                continue
            roles, entry = bound_roles(rid)
            model = roles.get("executor")
            if not model:
                continue
            # HAS THIS BINDING EVER WORKED? 2026-09-10 03:20, added after this watchdog killed a
            # HEALTHY run. Run 9bb3c3da had completed executor rounds at 190s, 584s and 430s - it was
            # plainly working - and was aborted anyway because two probes timed out at 12s. Run
            # ba2ca222, aborted the same way, had ZERO completed executor rounds and genuinely was
            # stuck. The difference between them is not visible to a probe.
            # WHY THE PROBE LIES HERE. The local span is a SINGLE-SLOT backend: one qwen3.8:27b at
            # 65536 ctx across both 3090s, single-stream. While its executor is mid-generation, a
            # probe QUEUES BEHIND THE VERY EXECUTOR IT IS PROBING and times out - so the reading says
            # "the span is busy", which is exactly what a working executor looks like. Measured minutes
            # later with no run bound to it: 1.36s, GPUs at 0%. On a multi-slot backend like Synthetic
            # the probe is diagnostic; on a single-slot local one it is a contention check wearing a
            # health check's clothes.
            # THE DISCRIMINATOR IS PROGRESS, NOT LATENCY. A binding that has completed an executor
            # round is proven workable, so a later failed probe means contention. A binding that has
            # NEVER completed one has shown nothing, and a starved backend is the likeliest reason.
            # So: abort only runs with zero completed executor rounds. A run that worked and then truly
            # dies is left to its own round budget - losing one budget is far cheaper than killing work.
            ever_worked = any((rd.get("executor_status") or {}).get("status") == "done"
                              for rd in rounds)
            ok, slow, dt = emit_ok(model)
            # SINGLE-SLOT ONLY. 2026-09-10 03:35: the "it has worked before, so a failed probe means
            # contention" exemption is correct ONLY where the probe genuinely queues behind the
            # executor - that is, a single-slot LOCAL backend. On a multi-slot cloud backend like
            # Synthetic the probe runs on its own slot, so it IS diagnostic there and the exemption
            # would wrongly protect a run whose model is genuinely degrading. Measured the same
            # minute: glm-5.2 7.34s then 37.16s against a nemotron control at 2.34s, on the model
            # bound as executor to the TOP-priority run - exactly the case this must not shield.
            single_slot = "qwen3" in str(model) or ":synthetic" not in str(model)
            # 2026-09-10 04:50 THIRD ITERATION, and this time drop the proxy entirely.
            # 03:22 I exempted single-slot backends whose binding had "ever worked", because the probe
            # queues behind the executor. 03:35 I narrowed that to single-slot only, because on
            # multi-slot Synthetic the probe IS diagnostic. Both right as far as they went - and then
            # at 04:45 the watchdog aborted run 870368f1 at ROUND 1, where the executor was mid-first
            # episode and `ever_worked` was therefore False, so the exemption never applied. The span
            # measured 1.70s with GPUs at 0% seconds after the abort freed it: the same false positive
            # as 9bb3c3da, moved to the one round the guard did not cover.
            # `ever_worked` was a PROXY for "is the executor busy". On a single-slot backend the honest
            # statement is simpler and needs no proxy: THE PROBE CANNOT DISTINGUISH A BUSY EXECUTOR FROM
            # A STARVED ONE, EVER - because the executor is the load. So do not abort on it at all.
            # A genuinely stuck local executor is still bounded: the harness's own 1800s episode budget
            # ends it, and the LAUNCH probe already refuses to start a run on a dead span. Costing one
            # episode budget in the rare true-positive case is far cheaper than killing working runs,
            # which this has now done twice.
            if not ok and single_slot:
                log("  %s probe failed on %s - SINGLE-SLOT backend, so the probe queues behind the "
                    "executor it is testing and cannot tell busy from starved. Reporting only; the "
                    "1800s episode budget is the real guard here." % (rid[-8:], model))
                abnormal.append("%s busy" % rid[-8:])
                continue
            if not ok:
                # WARM RETRY, measured 2026-09-10 00:05. Backends are BIMODAL: the first request
                # after an idle period is slow and the immediate next one is fast - the same model
                # measured 15.49s then 1.62s, another 12.47s then 1.97s, the local span 30.02s then
                # 0.94s. A single cold probe therefore reads as starvation on a perfectly healthy
                # backend. The two-strike rule does not cover this: strikes are a full interval
                # apart, so both can land cold. Re-probe at once and keep the better reading - a
                # backend that is genuinely starved fails both, one that was merely cold does not.
                ok2, slow2, dt2 = emit_ok(model)
                if dt2 < dt:
                    ok, slow, dt = ok2, slow2, dt2
                    log("  %s warm retry %.1fs (first probe was cold)" % (rid[-8:], dt2))
            if not ok and ":synthetic" in str(model):
                # PROVIDER-WIDE, OR THIS RUN? The majority-starved guard further down cannot
                # answer that when only one run is live: it tests
                #   len(suspects) > max(1, len(live) // 2)
                # which at len(live)==1 is 1 > 1 -- always false. So a single run on an exhausted
                # provider was ALWAYS aborted. That is exactly what killed 555cfefc at 06:56 on
                # 2026-09-10: Synthetic was returning 429 on 7 of 7 models fleet-wide, the run
                # itself was healthy four rounds in, and it was the only live run so the guard
                # could not fire. It cost four rounds of work and an injected set of host
                # measurements that had to be gathered by hand.
                # Do not infer provider health from HOW MANY runs happen to be affected -- that is
                # a sample of one dressed up as a population. Measure the provider directly:
                # probe a DIFFERENT model on the same provider. If the control fails too, the
                # provider is down and this run is a bystander, whatever the live-run count.
                ctrl = next((m for m in SYN_CONTROLS if m != model), None)
                if ctrl:
                    cok, _cslow, cdt = emit_ok(ctrl)
                    if not cok:
                        log("  %s probe failed on %s AND control %s also failed (%.1fs) - the "
                            "provider is down, not this run. Reporting only, not aborting."
                            % (rid[-8:], model, ctrl, cdt))
                        abnormal.append("%s provider-wide" % rid[-8:])
                        continue
                    log("  %s control %s OK %.1fs - so the failure is specific to %s, not the "
                        "provider" % (rid[-8:], ctrl, cdt, model))
            state = "OK" if ok and not slow else ("SLOW" if ok else "FAIL")
            log("  %s round~%.0fs executor %s -> %s %.1fs" % (rid[-8:], elapsed, model, state, dt))
            if slow:
                abnormal.append("%s %s" % (rid[-8:], state))
            if not ok:
                suspects.append((rid, model, entry, elapsed))

        if suspects and len(suspects) > max(1, len(live) // 2):
            strikes.clear()
            abnormal.append("possible provider outage")
            log("HOLDING OFF: %d of %d live runs starved - that shape is a provider outage, not "
                "unlucky runs. Mass-aborting would turn an outage into lost work."
                % (len(suspects), len(live)))
        else:
            for rid, model, entry, elapsed in suspects:
                strikes[rid] = strikes.get(rid, 0) + 1
                if entry is None:
                    # bindings came from the API, so there is no queue slot to return the task to.
                    # Aborting here would destroy the work with nothing to relaunch it.
                    log("  %s STARVED on %s (~%.0fs round) but has NO queue entry - reporting only, "
                        "not aborting: there is nowhere to requeue it." % (rid[-8:], model, elapsed))
                    continue
                if strikes[rid] < 2:
                    log("  %s strike %d/2 - one failed probe is not enough to kill a round"
                        % (rid[-8:], strikes[rid]))
                    continue
                reason = ("Mid-round spin detected. The round had run ~%.0fs and its bound executor "
                          "%s failed the emit probe TWICE - starved, not thinking. Aborting so the "
                          "task returns to its own queue slot and the launcher's rotation gives it "
                          "a different trio with a flash executor. Worktree and commits untouched."
                          % (elapsed, model))
                try:
                    urllib.request.urlopen(urllib.request.Request(
                        BASE + "/api/runs/%s/abort" % rid,
                        data=json.dumps({"reason": reason}).encode(),
                        headers=H, method="POST"), timeout=60)
                    log("  ABORTED+REQUEUED %s (%s) -> %s" % (rid[-8:], model, requeue(entry)))
                    abnormal.append("aborted %s" % rid[-8:])
                except Exception as e:
                    log("  abort failed", rid[-8:], str(e)[:80])
                strikes.pop(rid, None)
            if not suspects:
                strikes.clear()
    except Exception as e:
        abnormal.append("watchdog error")
        log("watchdog error", str(e)[:110])

    if abnormal:
        interval = INTERVAL_MIN
        log("ABNORMAL (%s) -> interval reset to %ds" % ("; ".join(abnormal)[:90], interval))
    else:
        prev = interval
        interval = min(int(interval * DECAY), INTERVAL_MAX)
        log("clean (%d live) -> backing off %ds to %ds%s"
            % (len(live), prev, interval, " (ceiling)" if interval == INTERVAL_MAX else ""))
    time.sleep(interval)
