#!/usr/bin/env bash
# ct110_deploy.sh — the in-container half of the CT110 deploy.
#
# This script runs INSIDE CT110 as root, invoked by the lan-deploy runner via
#   ssh <pve> "pct exec 110 -- bash /root/lh-deploy/ct110_deploy.sh <mode>"
# It is the CI-era successor to PTAIT09's deploy-harness-v2.sh (which lives
# only on PTAIT09 and could not be read from this repo; the behavior below
# re-implements its documented lessons per TASK 224 and marks assumptions).
#
# Exit codes (contract with scripts/deploy/ct110/run_on_ct110.sh):
#   0  success
#   1  unexpected error
#   2  OPERATOR ABORT — DEPLOY-HOLD present, or the idle recheck found active
#      runs.  LEGACY NOTE (2026-09-18): rc=2 is overloaded — a bash syntax
#      error in this script also yields 2 — so the caller MUST print captured
#      stderr before interpreting rc=2.  Every rc=2 path here writes its
#      reason to stderr.
#   3  rollback impossible (no recorded previous version / no archived wheel)
#   4  post-(re)install verify failed (service not active / version mismatch)
#
# Modes:
#   deploy    — hold check -> idle recheck -> record previous version ->
#               pip install staged wheel -> restart service -> verify
#   rollback  — reinstall the archived wheel for the recorded previous
#               version -> restart -> verify.  Deliberately SKIPS hold/idle
#               checks: rollback only runs after a failed deploy, when the
#               service was already restarted and the window was already
#               zero-active; blocking recovery behind the hold file that
#               (rightly) blocked the deploy would strand the host.
#   units     — TASK 236: install the overseer-sweep systemd units
#               (lh-overseer-sweep.service + .timer) via ct110_units.sh.
#               Runs AFTER the wheel deploy (the tick entrypoint lives in the
#               wheel's repo checkout on the host).  The units stage NEVER
#               enables or starts the timer — read-only shadowing and the
#               acting flip are operator steps (docs/OVERSEER-TICK-CT110.md).
set -euo pipefail

MODE="${1:?usage: ct110_deploy.sh deploy|rollback}"

# Tuning knobs (overridable via environment for testing).
DEPLOY_DIR="${LH_DEPLOY_DIR:-/root/lh-deploy}"        # where the runner staged this script + wheel
WHEEL_ARCHIVE="${LH_WHEEL_ARCHIVE:-/home/harness/deploy/wheels}"
STATE_DIR="${LH_DEPLOY_STATE:-/home/harness/deploy/state}"
VENV="${LH_VENV:-/home/harness/venv}"                 # live venv per the running unit (systemctl cat lh-harness)
SERVICE="${LH_SERVICE:-lh-harness.service}"
API_URL="${LH_API_URL:-http://127.0.0.1:8799}"
EXPECTED_VERSION="${LH_EXPECTED_VERSION:-}"

# ASSUMPTION (unverifiable from this repo — the 2026-09-18 DEPLOY-HOLD
# mechanism lived in PTAIT09-only files): the hold file path.  We honour the
# first existing file of: $LH_DEPLOY_HOLD_FILE, the repo checkout, the harness
# home.  A hold file's CONTENT is the human-written reason; it is echoed.
HOLD_FILES=(
  ${LH_DEPLOY_HOLD_FILE:+"$LH_DEPLOY_HOLD_FILE"}
  "/home/harness/work/LongHorizon-Harness/DEPLOY-HOLD"
  "/home/harness/DEPLOY-HOLD"
)

log()  { echo "[ct110-deploy] $*"; }
abort() { echo "ABORT(rc=2): $*" >&2; exit 2; }
die()  { echo "ERROR: $*" >&2; exit 1; }

check_hold() {
  local f
  for f in "${HOLD_FILES[@]}"; do
    if [[ -n "$f" && -f "$f" ]]; then
      abort "DEPLOY-HOLD present at $f — refusing to ship (reason: $(cat "$f" 2>/dev/null || echo '<unreadable>'))"
    fi
  done
}

# Bearer token for the local API.  Read from the service environment; never
# echoed.  Primary source is the unit's EnvironmentFile; fall back to the
# unit file itself (today the token is ALSO inline there — a host-side
# hygiene issue tracked separately, do not propagate it anywhere).
api_token() {
  local tok=""
  if [[ -f /home/harness/.lh-harness-secrets.env ]]; then
    tok="$(grep -E '^LH_HARNESS_WEB_TOKEN=' /home/harness/.lh-harness-secrets.env | tail -n 1 | cut -d= -f2- | tr -d "\"'")"
  fi
  if [[ -z "$tok" && -f /etc/systemd/system/lh-harness.service ]]; then
    tok="$(grep -oE 'LH_HARNESS_WEB_TOKEN=[^"]+' /etc/systemd/system/lh-harness.service | tail -n 1 | cut -d= -f2-)"
  fi
  [[ -n "$tok" ]] || die "could not read LH_HARNESS_WEB_TOKEN for the idle recheck"
  printf '%s' "$tok"
}

# Count runs in an ACTIVE lifecycle state via the LOCAL API.  Status set from
# src/lh_harness/supervisor/lifecycle.py: ACTIVE_STATUSES.
active_run_count() {
  curl -sf --max-time 15 -H "Authorization: Bearer $(api_token)" "$API_URL/api/runs" \
    | python3 -c '
import json, sys
ACTIVE = {"creating", "starting", "running", "waiting_approval", "stopping"}
try:
    data = json.load(sys.stdin)
except Exception:
    print(-1)
    raise SystemExit(0)
runs = data.get("runs") or []
print(sum(1 for r in runs if str(r.get("status", "")).strip().lower() in ACTIVE))
'
}

check_idle() {
  local n
  n="$(active_run_count)"
  [[ "$n" == "-1" ]] && abort "idle recheck could not parse GET $API_URL/api/runs — refusing to restart the service blind"
  (( n == 0 )) || abort "idle recheck found $n active run(s) immediately before restart — a deploy RESTART kills in-flight runs, so this window is not safe"
  log "idle recheck: 0 active runs"
}

wait_service_active() {
  local i state
  for i in $(seq 1 30); do
    state="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
    [[ "$state" == "active" ]] && return 0
    sleep 2
  done
  echo "post-install verify: $SERVICE did not become active within 60s (last: $state)" >&2
  return 4
}

installed_version() {
  "$VENV/bin/python" -c 'import lh_harness; print(lh_harness.__version__)'
}

staged_wheel() {
  local matches
  matches=( "$DEPLOY_DIR"/lh_harness-*-py3-none-any.whl )
  [[ -f "${matches[0]}" ]] || die "no staged wheel under $DEPLOY_DIR"
  [[ ${#matches[@]} -eq 1 ]] || die "more than one staged wheel under $DEPLOY_DIR: ${matches[*]}"
  printf '%s' "${matches[0]}"
}

restart_and_verify() {
  local want="$1" got
  systemctl restart "$SERVICE"
  wait_service_active
  got="$(installed_version)"
  if [[ "$got" != "$want" ]]; then
    echo "post-install verify: installed lh_harness.__version__ is $got, expected $want" >&2
    return 4
  fi
  printf '%s' "$got"
}

case "$MODE" in
  deploy)
    [[ -n "$EXPECTED_VERSION" ]] || die "LH_EXPECTED_VERSION is required for deploy"
    check_hold
    check_idle

    prev="$("$VENV/bin/pip" show lh-harness 2>/dev/null | awk '/^Version:/{print $2; exit}')"
    [[ -n "$prev" ]] || die "cannot determine the currently installed lh-harness version in $VENV"
    mkdir -p "$WHEEL_ARCHIVE" "$STATE_DIR"
    # Recorded BEFORE the install; rollback reads exactly this.
    printf '%s\n' "$prev" > "$STATE_DIR/previous_version"
    log "previous version recorded: $prev"

    wheel="$(staged_wheel)"
    log "installing $(basename "$wheel") into $VENV"
    "$VENV/bin/pip" install --upgrade "$wheel"

    got="$(restart_and_verify "$EXPECTED_VERSION")"
    # Archive the wheel we just installed so a FUTURE deploy's rollback has a
    # wheel to return to even after this one ages out of the artifact store.
    cp -f "$wheel" "$WHEEL_ARCHIVE/"
    log "CT110_DEPLOY_INNER_OK version=$got previous=$prev"
    ;;

  rollback)
    [[ -f "$STATE_DIR/previous_version" ]] || {
      echo "rollback: no recorded previous version at $STATE_DIR/previous_version" >&2; exit 3; }
    prev="$(cat "$STATE_DIR/previous_version")"
    [[ -n "$prev" ]] || { echo "rollback: $STATE_DIR/previous_version is empty" >&2; exit 3; }
    wheel=""
    for candidate in "$WHEEL_ARCHIVE"/lh_harness-"$prev"-*-py3-none-any.whl; do
      [[ -f "$candidate" ]] && wheel="$candidate" && break
    done
    [[ -n "$wheel" ]] || {
      echo "rollback: no archived wheel for version $prev under $WHEEL_ARCHIVE" >&2; exit 3; }
    log "rolling back to $prev from $(basename "$wheel")"
    "$VENV/bin/pip" install --upgrade "$wheel"
    got="$(restart_and_verify "$prev")"
    log "CT110_ROLLBACK_INNER_OK restored=$got"
    ;;

  units)
    log "installing overseer-sweep units (never enabling: operator step)"
    bash "$DEPLOY_DIR/ct110_units.sh"
    log "CT110_UNITS_STAGE_OK"
    ;;

  *)
    echo "usage: ct110_deploy.sh deploy|rollback|units" >&2
    exit 1
    ;;
esac
