#!/usr/bin/env bash
# tick.sh — the CT110 overseer sweep tick (TASK 236).
#
# Successor of the archived PTAIT09 chain (tools/overseer_tick.ps1 +
# tools/overseer_tick_hidden.vbs, scheduled task `LH-Overseer-Sweep`): one
# headless `claude -p` invocation every 5 minutes, driven by the
# LOOP-PROMPT doctrine (docs/LOOP-PROMPT.md) through the CT110 adapter
# (docs/OVERSEER-TICK-CT110.md).  Differences from the archived chain:
#   * state lives on the CT110 harness API + apparatus archive (the task 235
#     surface) — NEVER C:/tmp, which is a read-only archive since cutover;
#   * default mode is READ-ONLY (observe + report); acting mode is behind an
#     explicit documented flag and requires the PTAIT09 task disabled first;
#   * single-instance lock lives in lh-overseer-sweep.service (flock -n -E 0
#     plus systemd's own no-overlap oneshot semantics) — this script takes
#     NO lock of its own and must be run through the unit (manual runs:
#     `systemctl start lh-overseer-sweep.service`, which the same flock
#     guards; a bare manual invocation is an interactive session per the
#     adapter doctrine, because it finds no LH_OVERSEER_TICK_ID in its env);
#   * per-tick telemetry: one Seq CLEF event (TASK 161 wire contract, branch
#     feat/seq-logging @ edef634) and one hivemind ingest POST (TASK 229
#     contract, tasks/overseer-ingest-task.txt) via tick_notify.py.  Both
#     sinks are fail-open and both are driven by env-var NAMES only.
#
# Exit codes:
#   0  tick ran (or was skipped by the unit lock; the unit maps that to 0)
#   2  invalid configuration (unknown mode)      — config error, tick NOT run
#   4  `claude` binary not found                 — config error, tick NOT run
#   5  missing LH_HARNESS_WEB_TOKEN (the CT110 API is unreadable without
#      the bearer; a bare call reads as a dead API per doctrine RULE 1)
# The claude tick's own exit code propagates as-is on success paths.

set -uo pipefail
# NOT `set -e`: every stage below is individually guarded so a failing
# telemetry sink can never abort the tick mid-way (the archived chain's
# $ErrorActionPreference='Continue' discipline, bash edition).

usage() { echo "usage: tick.sh [--dry-run]" >&2; exit 2; }

# --- configuration (env-var NAMES only; values never echoed) ---------------
SWEEP_HOME="${LH_OVERSEER_SWEEP_HOME:-/home/harness/.overseer-sweep}"
TICKS_DIR="${SWEEP_HOME}/ticks"
# Where the telemetry module lives (ct110_units.sh installs it next to this
# script under $SWEEP_HOME/bin; a source checkout falls back to the repo path).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TICK_NOTIFY="${TICK_NOTIFY:-${SCRIPT_DIR}/tick_notify.py}"
MODE="${LH_OVERSEER_TICK_MODE:-read-only}"
API_URL="${CT110_WEB_URL:-http://192.168.21.168:8799}"
DRY_RUN=0
[[ "${LH_OVERSEER_TICK_DRY_RUN:-0}" == "1" ]] && DRY_RUN=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

case "$MODE" in
  read-only|act) ;;
  *)
    echo "LH_OVERSEER_TICK_MODE='$MODE' is invalid (read-only|act); refusing to run" >&2
    exit 2
    ;;
esac

# Acting mode is a double-flag: the MODE variable alone is not enough.  The
# operator must also set LH_OVERSEER_TICK_ACT_CONFIRM=YES after the 24h
# read-only comparison, with the PTAIT09 `LH-Overseer-Sweep` task disabled
# (never both acting at once — disconnect checklist D3).
if [[ "$MODE" == "act" && "${LH_OVERSEER_TICK_ACT_CONFIRM:-}" != "YES" ]]; then
  echo "LH_OVERSEER_TICK_MODE=act requires LH_OVERSEER_TICK_ACT_CONFIRM=YES (docs/OVERSEER-TICK-CT110.md); refusing to run" >&2
  exit 2
fi

REPO_ROOT="${LH_OVERSEER_REPO_ROOT:-/home/harness/work/LongHorizon-Harness}"
TICK_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
DEVICE="$(hostname | tr '[:upper:]' '[:lower:]')"
TICK_LOG="${TICKS_DIR}/${TICK_ID}.log"

# The headless prompt.  The leading phrase is deliberately identical to the
# PTAIT09 chain's prompt ("Run one overseer sweep tick now") because the
# doctrine's STEP 6 dead-tick test greps transcripts for that prose phrase —
# a needle with no backslash survives JSON escaping (tick #1605/#1607
# lessons).  The adapter doc carries everything CT-specific.
TICK_PROMPT="Run one overseer sweep tick now: read docs/LOOP-PROMPT.md in full and execute it, under the CT110 adapter docs/OVERSEER-TICK-CT110.md."

log() { echo "[overseer-tick ${TICK_ID}] $*"; }

mkdir -p "$TICKS_DIR"

# --- preflight (observation only, never an abort) ---------------------------
# RULE 1: verify by reading back.  The tick records whether the CT110 API
# answered before spending a claude invocation, but does NOT abort on a dead
# API — observing and reporting the outage IS tick work.  Dry-run touches no
# network at all.
api_http_code="unset"
if [[ "$DRY_RUN" == "1" ]]; then
  api_http_code="dry-run"
elif [[ -n "${LH_HARNESS_WEB_TOKEN:-}" ]]; then
  api_http_code="$(curl -s -o /dev/null -m 15 -w '%{http_code}' \
    -H "Authorization: Bearer ${LH_HARNESS_WEB_TOKEN}" "${API_URL}/api/meta" 2>/dev/null || echo "unreachable")"
else
  api_http_code="no-credential"
fi

# --- claude invocation ------------------------------------------------------
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude 2>/dev/null || true)}"

run_tick() {
  # Dry-run never executes claude and never touches the network (beyond the
  # preflight above, which is skipped in dry-run by returning early).
  [[ "$DRY_RUN" == "1" ]] && return 0
  [[ -n "$CLAUDE_BIN" ]] || return 4
  (
    cd "$REPO_ROOT" || exit 1
    export LH_OVERSEER_TICK_ID="$TICK_ID"
    export LH_OVERSEER_TICK_MODE="$MODE"
    exec "$CLAUDE_BIN" -p "$TICK_PROMPT" --output-format text --dangerously-skip-permissions
  )
}

tick_rc=0
{
  echo "TICK ${TICK_ID} START mode=${MODE} dry_run=${DRY_RUN} api_meta_http=${api_http_code} claude=${CLAUDE_BIN:-<not-found>}"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY-RUN: would execute: claude -p '<doctrine prompt>' --output-format text --dangerously-skip-permissions"
    echo "DRY-RUN: prompt: ${TICK_PROMPT}"
    echo "DRY-RUN: working directory: $(pwd)"
    if [[ -n "${SEQ_URL:-}" ]]; then
      echo "DRY-RUN: seq sink: configured (SEQ_URL set; key NAME SEQ_API_KEY, never echoed)"
    else
      echo "DRY-RUN: seq sink: disabled (SEQ_URL unset)"
    fi
    if [[ -n "${MEMORY_URL:-}" ]]; then
      echo "DRY-RUN: memory transport: rest (MEMORY_URL set)"
    elif [[ -n "${MEMORY_MCP_URL:-}" ]]; then
      echo "DRY-RUN: memory transport: gateway (MEMORY_MCP_URL set, tool ${MEMORY_MCP_TOOL:-memory-remember_session})"
    else
      echo "DRY-RUN: memory transport: disabled (MEMORY_URL and MEMORY_MCP_URL both unset)"
    fi
    echo "DRY-RUN: neither sink posts in dry-run; tick_notify --print-only renders the payloads instead"
  elif [[ "$api_http_code" == "no-credential" ]]; then
    echo "LH_HARNESS_WEB_TOKEN is not set; the CT110 API is unreadable (a bare call reads as a 401, doctrine RULE 1). Tick aborted."
  elif [[ -z "$CLAUDE_BIN" ]]; then
    echo "claude binary not found on PATH; set CLAUDE_BIN to the absolute path. Tick aborted."
  else
    run_tick 2>&1 | tee "$TICK_LOG"
    tick_rc="${PIPESTATUS[0]}"
    echo "TICK ${TICK_ID} END rc=${tick_rc} (full output: ${TICK_LOG})"
  fi
} >> "${SWEEP_HOME}/tick.log" 2>&1

# Re-derive state for the telemetry payload after the log block above.
tick_status="ran"
if [[ "$DRY_RUN" == "1" ]]; then
  tick_status="dry-run"
  tick_rc=0
elif [[ "$api_http_code" == "no-credential" ]]; then
  tick_status="no-credential"
  tick_rc=5
elif [[ -z "$CLAUDE_BIN" ]]; then
  tick_status="claude-not-found"
  tick_rc=4
fi

# Per-tick summary (redaction happens inside tick_notify.py before any POST).
SUMMARY="$(printf 'Overseer sweep tick %s on %s in mode %s: claude rc=%s, api_meta_http=%s, full output at %s' \
  "$TICK_ID" "$DEVICE" "$MODE" "$tick_rc" "$api_http_code" "$TICK_LOG")"

if [[ "$DRY_RUN" == "1" ]]; then
  printf '%s\n' "$SUMMARY" | python3 "$TICK_NOTIFY" \
    --tick-id "$TICK_ID" --mode "$MODE" --rc 0 --tick-log "$TICK_LOG" --print-only || true
else
  printf '%s\n' "$SUMMARY" | python3 "$TICK_NOTIFY" \
    --tick-id "$TICK_ID" --mode "$MODE" --rc "$tick_rc" --tick-log "$TICK_LOG" || true
fi

exit "$tick_rc"
