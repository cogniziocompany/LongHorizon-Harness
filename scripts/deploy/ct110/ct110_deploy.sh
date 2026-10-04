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
#   3  rollback impossible (no recorded previous state / no copy of the
#      pre-deploy wheel was found when the deploy started)
#   4  post-(re)install verify failed (service not active / version or
#      build-commit mismatch)
#   5  rollback NOT NEEDED: the deploy never reached the install step, so the
#      pre-deploy build is untouched (rollback mode only)
#
# Modes:
#   deploy    — hold check -> idle recheck -> record the pre-deploy build
#               (version, build commit, wheel sha256) and keep a copy of its
#               wheel -> pip install staged wheel -> restart service -> verify
#               version AND build commit (LH_EXPECTED_COMMIT)
#   rollback  — reinstall the pre-deploy wheel copied aside by THIS deploy
#               (matched by sha256, not by version: every build is 0.1.7, so
#               a version-named archive file is overwritten by the new wheel —
#               the 2026-10-03 b6f51f9 "rollback" reinstalled the new code)
#               -> restart -> verify version, wheel sha256 and, when the old
#               build carried one, its commit.  Deliberately SKIPS hold/idle
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
EXPECTED_COMMIT="${LH_EXPECTED_COMMIT:-}"               # full sha the wheel was built from
ROLLBACK_DIR="$STATE_DIR/rollback"                     # the pre-deploy wheel, copied aside

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
  local tok="" secrets_file="${LH_SECRETS_FILE:-/home/harness/.lh-harness-secrets.env}"
  if [[ -f "$secrets_file" ]]; then
    tok="$(grep -E '^LH_HARNESS_WEB_TOKEN=' "$secrets_file" | tail -n 1 | cut -d= -f2- | tr -d "\"'")"
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
  # Summary form (~123 KB, carries status) instead of the full list (~5.6 MB), which timed out
  # at 15 s and, under set -e/pipefail, killed the deploy with curl exit 28 before install
  # (run 36801068351). A failed fetch now reaches the parser as empty input -> -1 -> abort.
  { curl -sf --max-time 60 -H "Authorization: Bearer $(api_token)" "$API_URL/api/runs?fields=summary" || true; } \
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

# Commit the installed wheel was built from (src/lh_harness/build_info.py).
# Empty for builds that predate build info: "unknown", never a guess.
installed_commit() {
  "$VENV/bin/python" -m lh_harness.build_info 2>/dev/null || true
}

# sha256 of the wheel file pip installed (pip records it in direct_url.json).
installed_wheel_sha() {
  "$VENV/bin/python" - <<'PY' 2>/dev/null || true
import json
from importlib.metadata import distribution
text = distribution("lh-harness").read_text("direct_url.json") or "{}"
print(json.loads(text).get("archive_info", {}).get("hashes", {}).get("sha256", ""))
PY
}

# Print the path of a wheel under the archive (or the previous rollback copy)
# whose sha256 is $1; nothing when none matches.
find_wheel_by_sha() {
  local want="$1" f
  [[ -n "$want" ]] || return 0
  while IFS= read -r -d '' f; do
    if [[ "$(sha256sum "$f" | awk '{print $1}')" == "$want" ]]; then
      printf '%s' "$f"
      return 0
    fi
  done < <(find "$WHEEL_ARCHIVE" "$ROLLBACK_DIR" -type f -name 'lh_harness-*.whl' -print0 2>/dev/null)
}

# Record the build that is live BEFORE the install and copy its wheel aside,
# so rollback restores exactly those bytes.
record_previous() {
  local prev prev_commit prev_sha prev_wheel tmp
  # awk reads ALL of pip's output (no early `exit`): closing the pipe early
  # makes pip die with rc=120 (broken pipe), which pipefail turns fatal.
  prev="$("$VENV/bin/pip" show lh-harness 2>/dev/null | awk '/^Version:/ && !v {v=$2} END {print v}')"
  [[ -n "$prev" ]] || die "cannot determine the currently installed lh-harness version in $VENV"
  prev_commit="$(installed_commit)"
  prev_sha="$(installed_wheel_sha)"
  printf '%s\n' "$prev" > "$STATE_DIR/previous_version"
  printf '%s\n' "$prev_commit" > "$STATE_DIR/previous_commit"
  printf '%s\n' "$prev_sha" > "$STATE_DIR/previous_wheel_sha256"
  prev_wheel="$(find_wheel_by_sha "$prev_sha")"
  if [[ -n "$prev_wheel" ]]; then
    tmp="$(mktemp -d "$STATE_DIR/rollback.new.XXXXXX")"
    cp -f "$prev_wheel" "$tmp/"
    rm -rf "$ROLLBACK_DIR"
    mv "$tmp" "$ROLLBACK_DIR"
    log "previous build recorded: version=$prev commit=${prev_commit:-unknown} wheel_sha256=$prev_sha (rollback copy: $(basename "$prev_wheel"))"
  else
    rm -rf "$ROLLBACK_DIR"
    echo "WARNING: no archived wheel matches the installed sha256 '${prev_sha:-unknown}'; a rollback of this deploy will be impossible (rc=3)" >&2
    log "previous build recorded: version=$prev commit=${prev_commit:-unknown} wheel_sha256=${prev_sha:-unknown} (NO rollback copy)"
  fi
}

# Archive a deployed wheel under a per-build directory so a later deploy of
# the same version can never overwrite it.
archive_wheel() {
  local wheel="$1" key
  key="${EXPECTED_COMMIT:-sha256-$(sha256sum "$wheel" | awk '{print substr($1,1,16)}')}"
  mkdir -p "$WHEEL_ARCHIVE/by-commit/$key"
  cp -f "$wheel" "$WHEEL_ARCHIVE/by-commit/$key/"
}

verify_commit() {  # verify_commit <expected-or-empty>
  local want="$1" got
  [[ -n "$want" ]] || return 0
  got="$(installed_commit)"
  if [[ "$got" != "$want" ]]; then
    echo "post-install verify: installed build commit is '${got:-unknown}', expected $want" >&2
    return 4
  fi
}

staged_wheel() {
  local matches
  matches=( "$DEPLOY_DIR"/lh_harness-*-py3-none-any.whl )
  [[ -f "${matches[0]}" ]] || die "no staged wheel under $DEPLOY_DIR"
  [[ ${#matches[@]} -eq 1 ]] || die "more than one staged wheel under $DEPLOY_DIR: ${matches[*]}"
  printf '%s' "${matches[0]}"
}

# Every build is the same pyproject version, so a plain `pip install
# --upgrade` sees "already satisfied" and silently keeps the old code (the
# 2026-10-01/02 deploys restarted unchanged bytes). Always reinstall this
# exact wheel, then prove pip recorded it.
install_wheel() {
  local wheel="$1" want got
  # First pass resolves any new or changed dependencies; the second replaces
  # the package itself even when its version string is unchanged.
  "$VENV/bin/pip" install --upgrade "$wheel"
  "$VENV/bin/pip" install --force-reinstall --no-deps "$wheel"
  want="$(sha256sum "$wheel" | awk '{print $1}')"
  got="$(installed_wheel_sha)"
  if [[ "$got" != "$want" ]]; then
    echo "post-install verify: installed wheel sha256 is '${got:-unknown}', staged wheel is $want" >&2
    return 4
  fi
  log "installed wheel sha256 matches staged wheel ($want)"
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
    mkdir -p "$WHEEL_ARCHIVE" "$STATE_DIR"
    # Cleared first: a deploy that aborts below (hold, idle recheck) leaves
    # the host untouched, and rollback must say so instead of reinstalling.
    rm -f "$STATE_DIR/install_started"
    check_hold
    check_idle

    # Recorded BEFORE the install; rollback reads exactly this.
    record_previous

    wheel="$(staged_wheel)"
    touch "$STATE_DIR/install_started"
    log "installing $(basename "$wheel") into $VENV (expected commit ${EXPECTED_COMMIT:-not given})"
    install_wheel "$wheel"
    # Archive per build so a FUTURE deploy can find these bytes by sha256
    # (a flat lh_harness-<version>.whl is overwritten by every same-version build).
    archive_wheel "$wheel"

    got="$(restart_and_verify "$EXPECTED_VERSION")"
    verify_commit "$EXPECTED_COMMIT"
    printf '%s\n' "${EXPECTED_COMMIT:-}" > "$STATE_DIR/deployed_commit"
    log "CT110_DEPLOY_INNER_OK version=$got commit=${EXPECTED_COMMIT:-unknown} previous=$(cat "$STATE_DIR/previous_version") previous_commit=$(cat "$STATE_DIR/previous_commit" 2>/dev/null | grep . || echo unknown)"
    ;;

  rollback)
    if [[ ! -f "$STATE_DIR/install_started" ]]; then
      log "CT110_ROLLBACK_NOT_NEEDED: the deploy never reached the install step; the pre-deploy build was not touched"
      exit 5
    fi
    [[ -f "$STATE_DIR/previous_version" ]] || {
      echo "rollback: no recorded previous version at $STATE_DIR/previous_version" >&2; exit 3; }
    prev="$(cat "$STATE_DIR/previous_version")"
    prev_commit="$(cat "$STATE_DIR/previous_commit" 2>/dev/null || true)"
    prev_sha="$(cat "$STATE_DIR/previous_wheel_sha256" 2>/dev/null || true)"
    [[ -n "$prev" ]] || { echo "rollback: $STATE_DIR/previous_version is empty" >&2; exit 3; }
    [[ -n "$prev_sha" ]] || {
      echo "rollback: the pre-deploy wheel sha256 is unknown (pip recorded none), so the pre-deploy bytes cannot be identified" >&2; exit 3; }
    wheel=""
    for candidate in "$ROLLBACK_DIR"/lh_harness-*-py3-none-any.whl; do
      [[ -f "$candidate" ]] && wheel="$candidate" && break
    done
    [[ -n "$wheel" ]] || {
      echo "rollback: no copy of the pre-deploy wheel (sha256 $prev_sha) was found when the deploy started; nothing safe to reinstall" >&2; exit 3; }
    [[ "$(sha256sum "$wheel" | awk '{print $1}')" == "$prev_sha" ]] || {
      echo "rollback: $wheel does not match the pre-deploy sha256 $prev_sha; refusing to install it" >&2; exit 3; }
    log "rolling back to $prev (commit ${prev_commit:-unknown}, wheel sha256 $prev_sha) from $(basename "$wheel")"
    install_wheel "$wheel"
    got="$(restart_and_verify "$prev")"
    verify_commit "$prev_commit"
    log "CT110_ROLLBACK_INNER_OK restored=$got commit=${prev_commit:-unknown} wheel_sha256=$prev_sha"
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
