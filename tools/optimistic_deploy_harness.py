"""OPPORTUNISTIC harness deploy - takes an idle window ONLY if one appears by itself.

Paxton, 2026-09-09 12:19 PT: "No dont break anythign in process".

So this deliberately does NOT do what C:/tmp/drain_deploy_harness.py does. That script STOPS THE
LAUNCHER first so the fleet drains to idle. It never kills an in-flight run, but it does halt new
work, and halting the queue is exactly what was ruled out.

This script instead:
  - NEVER stops, signals or touches launch_queue.py
  - NEVER cancels, stops or interrupts a run
  - only polls, and only fires the deploy when CT110 is ALREADY at zero active runs

Belt and braces: deploy-harness-v2.sh re-checks for itself and hard-aborts with exit 2 -
  [ "$ACTIVE" = "0" ] || { echo "ABORT: $ACTIVE active run(s) on CT110 - restart would kill them"; exit 2; }
so even a race between our poll and the launcher's next cycle cannot kill a run. The worst case is a
wasted ssh round trip.

WHY THIS IS WORTH RUNNING AT ALL: idle looks unreachable (queue 14 deep, launcher backfills to CAP on
every completion) - but it HAPPENED TODAY at 10:33 PT, when Synthetic's credit ran out and every trio
failed its launch probe, taking the fleet to active=0 for several minutes. Windows arrive by accident.
This is here to catch the next one instead of wishing we had.

WHAT IT DEPLOYS: LongHorizon-Harness main, which now carries PR #11 (task 77a) - the fleet
registration visibility work.

VERIFY AFTERWARDS, and this is the whole point of #11 - one command, no log reading:
    GET /api/meta  must contain fleet_configured, fleet_ever_succeeded, fleet_last_ok, fleet_last_error
Absent = not deployed. Present = LIVE.
"""
import json, subprocess, time, urllib.request, datetime, sys

TOKEN = "<CT110_BEARER_TOKEN_REDACTED_SEE_docs/SECRETS.md>"
RUNS = "http://192.168.21.168:8799/api/runs"
META = "http://192.168.21.168:8799/api/meta"
DEPLOY_LOCAL = "C:/tmp/pp-fix2/deploy-harness-v2.sh"
PVE = "root@192.168.21.151"           # corsairai300 hosts CT110
POLL_SECONDS = 180
BUSY_STATES = ("running", "starting", "pending", "waiting_approval", "stopping")


def log(*a):
    print(datetime.datetime.now().strftime("%H:%M"), *a, flush=True)


def _get(url):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + TOKEN})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)


def active_runs():
    d = _get(RUNS)
    rs = d if isinstance(d, list) else (d.get("runs") or d.get("items") or [])
    return [r for r in rs if str(r.get("status")) in BUSY_STATES]


def fleet_fields_live():
    try:
        return sorted(k for k in _get(META) if "fleet" in k.lower())
    except Exception:
        return []


def deploy():
    """Stream deploy-harness-v2.sh into CT110. The script re-checks idleness itself."""
    # Send the script as BYTES, never through a text pipe. With text=True,
    # Python wraps stdin in a TextIOWrapper whose newline=None rewrites every
    # line ending to os.linesep, which on Windows appends a carriage return to
    # each line. bash on CT110 then died with a syntax error at line 27 -
    # "syntax error near unexpected token" on the do of a for loop. The file on
    # disk is clean: verified 2026-09-18, zero CRLF pairs and 74 line feeds.
    # The corruption happened IN TRANSIT, in this function, on 2026-09-18 00:08.
    script = open(DEPLOY_LOCAL, "rb").read()
    p = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", PVE, "pct exec 110 -- bash -s"],
        input=script, capture_output=True, timeout=900,
    )
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    for line in out.splitlines()[-25:]:
        log("   ", line)
    if p.returncode != 0:
        for line in err.splitlines()[-8:]:
            log("  err", line)
    return p.returncode


HOLD_FILE = "C:/tmp/DEPLOY-HOLD-lh-harness.txt"


def deploy_hold_reason():
    """Return the hold text when a deploy is deliberately blocked, else None.

    An accidental protection is not a protection. On 2026-09-18 the only thing
    stopping this watcher from deploying main was its own transit bug, fixed
    above. main carries task 195 workspace branch guard, which refuses every
    continuation task until task 201 adds an opt-in, so the hold is now
    explicit, self-describing and removable by deleting one file.
    """
    try:
        with open(HOLD_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return None


def main():
    log("opportunistic harness deploy watcher started - will NOT pause the launcher or stop any run")
    held = deploy_hold_reason()
    if held:
        log("DEPLOY HELD - refusing to start. Delete", HOLD_FILE, "to lift.")
        for line in held.splitlines()[:12]:
            log("   hold:", line)
        return
    already = fleet_fields_live()
    if already:
        log("fleet fields ALREADY live:", already, "- nothing to deploy; exiting")
        return
    while True:
        try:
            live = active_runs()
        except Exception as e:
            log("status err", str(e)[:90])
            time.sleep(POLL_SECONDS)
            continue

        if live:
            time.sleep(POLL_SECONDS)
            continue

        log("*** CT110 IDLE (0 active) - taking the window ***")
        try:
            rc = deploy()
        except Exception as e:
            log("deploy raised", str(e)[:120])
            time.sleep(POLL_SECONDS)
            continue

        log("deploy rc=%s" % rc)
        if rc == 2:
            log("deploy returned 2. rc=2 IS OVERLOADED: the script uses it for its own idle re-check abort, AND bash exits 2 on a syntax error. Read the err lines above before calling this benign - on 2026-09-18 00:08 a transit bug was logged as a harmless abort for hours.")
            time.sleep(POLL_SECONDS)
            continue

        fields = fleet_fields_live()
        if fields:
            log("VERIFIED LIVE - /api/meta now exposes:", fields)
            log("task 77b (mcp-tools fleet e2e) is now unblocked")
            return
        log("deploy returned rc=%s but /api/meta still has no fleet_* fields - NOT claiming success" % rc)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
