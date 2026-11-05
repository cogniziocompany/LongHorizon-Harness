#!/usr/bin/env bash
# run_on_ct110.sh — runner-side half of the CT110 deploy (executes on the
# lan-deploy runner on CT210).  Transfers ct110_deploy.sh (and, for deploys,
# the built wheel) to CT110 as BYTES and runs it via the PVE host:
#
#   runner --scp--> PVE host (/tmp) --pct push--> CT110 (/root/lh-deploy)
#   runner --ssh--> PVE host: pct exec <CT_ID> -- bash <inner> <mode>
#
# Lesson coverage (TASK 224):
#   * BYTES, never a text pipe: the 2026-09-18 incident piped the script as
#     text and bash died on `$'do\r'`.  scp and pct push are binary copies;
#     we additionally sha256-verify the payload at BOTH hops and refuse to
#     execute on mismatch.  (.gitattributes pins *.sh to eol=lf so the bytes
#     leaving the checkout are already LF.)
#   * rc=2 is OVERLOADED (idle-recheck abort vs bash syntax error): captured
#     remote stderr is printed BEFORE any rc interpretation, always.
#
# Usage:
#   run_on_ct110.sh deploy          stage wheel+script, run deploy inside CT110
#   run_on_ct110.sh rollback        stage script, run rollback inside CT110
#   run_on_ct110.sh units           stage script+units, install units inside CT110 (TASK 236)
#   run_on_ct110.sh script <file> [args...]   stage a script, run it inside CT110
#   run_on_ct110.sh exec <cmd...>   run an arbitrary command inside CT110
#
# Required environment: PVE_HOST, PVE_USER, SSH_KEY_FILE, CT_ID.
# deploy additionally: EXPECTED_VERSION, and EXPECTED_COMMIT (full sha the
# wheel was built from; the inner script verifies the installed build commit).
# The wheel is picked up from ./stage/.
# units additionally: the two unit files from packaging/ staged under ./stage/units/.
#
# script mode (added for LHH-DEPLOY-CT110-PIPELINE) stages ONE script from
# ./stage/ and runs it inside the container with the given arguments.  It
# reuses the byte-verified transfer path above unchanged: the payload moves as
# BYTES, is sha256-verified at both hops, and is refused on mismatch.  This
# is how scripts/deploy-node.sh reaches the node for a ref-based deploy; the
# node-side script installs from a git ref, so there is no wheel to stage.
set -euo pipefail

MODE="${1:?usage: run_on_ct110.sh deploy|rollback|exec ...}"

: "${PVE_HOST:?set PVE_HOST (Proxmox host/IP, e.g. corsairai300)}"
: "${PVE_USER:?set PVE_USER (ssh user on the PVE host, e.g. root)}"
: "${SSH_KEY_FILE:?set SSH_KEY_FILE (private key with PVE access)}"
: "${CT_ID:?set CT_ID (e.g. 110)}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INNER_LOCAL="$SCRIPT_DIR/ct110_deploy.sh"
REMOTE_DIR="/root/lh-deploy"

SSH=(ssh -i "$SSH_KEY_FILE" -o BatchMode=yes -o StrictHostKeyChecking=accept-new
     -o ConnectTimeout=15 "$PVE_USER@$PVE_HOST")
SCP=(scp -i "$SSH_KEY_FILE" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -q)

remote_exec() {  # remote_exec <command-string>; rc/stderr discipline lives here
  local remote_cmd="$1" rc stderr_file
  stderr_file="$(mktemp)"
  set +e
  "${SSH[@]}" "$remote_cmd" 2>"$stderr_file"
  rc=$?
  set -e
  # Lesson 2: ALWAYS show captured stderr before anyone reasons about the rc.
  if [[ -s "$stderr_file" ]]; then
    echo "----- remote stderr (rc=$rc) -----" >&2
    cat "$stderr_file" >&2
    echo "----------------------------------" >&2
  fi
  rm -f "$stderr_file"
  return "$rc"
}

run_inner() {  # run_inner deploy|rollback|units
  local mode="$1" full rc
  full="pct exec $CT_ID -- env LH_EXPECTED_VERSION='${EXPECTED_VERSION:-}' LH_EXPECTED_COMMIT='${EXPECTED_COMMIT:-}' bash '$REMOTE_DIR/ct110_deploy.sh' '$mode'"
  set +e
  remote_exec "$full"
  rc=$?
  set -e
  if [[ "$rc" -eq 2 ]]; then
    # Overloaded code: could be the inner idle-recheck/hold abort OR a bash
    # syntax error in the transferred script.  stderr was already printed
    # above; decide ONLY from what it showed.
    echo "::error::remote rc=2 (OVERLOADED per the 2026-09-18 lesson): read the stderr block above — it is either an operator abort (DEPLOY-HOLD / active-run idle recheck) or bash failed to parse the transferred script." >&2
  fi
  return "$rc"
}

# --- exec mode: no transfer, just discipline -------------------------------
if [[ "$MODE" == "exec" ]]; then
  shift
  [[ $# -ge 1 ]] || { echo "exec mode needs a command" >&2; exit 1; }
  # Re-escape EACH argument so the remote bash -c sees exactly what we mean
  # ($* alone would silently drop the caller's quoting).
  cmd="$(printf '%q ' "$@")"
  remote_exec "pct exec $CT_ID -- bash -c $(printf '%q' "$cmd")"
  exit $?
fi

[[ "$MODE" == "deploy" || "$MODE" == "rollback" || "$MODE" == "units" || "$MODE" == "script" ]] || {
  echo "usage: run_on_ct110.sh deploy|rollback|units|script <file> [args...]|exec ..." >&2; exit 1; }

# --- script mode: stage ONE script from ./stage/ and run it ---------------
# Added for LHH-DEPLOY-CT110-PIPELINE: scripts/deploy-node.sh installs from
# a git REF, so there is no wheel to stage -- but the script itself still has
# to reach the node as BYTES with both hops sha256-verified, exactly like
# every other payload this script moves.
SCRIPT_REMOTE_NAME=""
SCRIPT_ARGS=()
# Environment variables passed INTO the container for script mode.  Names
# only here; the values are read from the caller's environment at run time
# and are never printed.  LH_HARNESS_WEB_TOKEN is the one the node deploy
# needs: it authenticates the /api/meta read-back without the deploy ever
# opening the node's secrets file.
SCRIPT_ENV_NAMES=("LH_HARNESS_WEB_TOKEN" "LH_DEPLOY_REF" "LH_DRAIN_WAIT")
if [[ "$MODE" == "script" ]]; then
  shift
  [[ $# -ge 1 ]] || { echo "script mode needs a script path" >&2; exit 1; }
  script_path="$1"; shift
  [[ -f "$script_path" ]] || { echo "script mode: no such file: $script_path" >&2; exit 1; }
  SCRIPT_REMOTE_NAME="$(basename "$script_path")"
  SCRIPT_ARGS=("$@")
fi

# --- stage payload ----------------------------------------------------------
payload=("$INNER_LOCAL")
if [[ "$MODE" == "deploy" ]]; then
  : "${EXPECTED_VERSION:?set EXPECTED_VERSION for deploy}"
  shopt -s nullglob
  wheels=( stage/lh_harness-*-py3-none-any.whl )
  shopt -u nullglob
  [[ ${#wheels[@]} -eq 1 ]] || {
    echo "expected exactly one wheel under ./stage, found ${#wheels[@]}" >&2; exit 1; }
  payload+=("${wheels[0]}")
fi
if [[ "$MODE" == "units" ]]; then
  shopt -s nullglob
  units=(
    stage/units/lh-overseer-sweep.service
    stage/units/lh-overseer-sweep.timer
    stage/units/tick.sh
    stage/units/tick_notify.py
  )
  shopt -u nullglob
  [[ ${#units[@]} -eq 4 ]] || {
    echo "expected both units + tick.sh + tick_notify.py under ./stage/units, found ${#units[@]}" >&2; exit 1; }
  [[ -f "$SCRIPT_DIR/ct110_units.sh" ]] || {
    echo "expected ct110_units.sh next to run_on_ct110.sh" >&2; exit 1; }
  payload+=("$SCRIPT_DIR/ct110_units.sh" "${units[@]}")
fi
if [[ "$MODE" == "script" ]]; then
  payload+=("$script_path")
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
remote_tmp="/tmp/lh-deploy-$stamp"
"${SSH[@]}" "mkdir -p '$remote_tmp'"

# Hop 1 (runner -> PVE host): scp is a binary copy; verify bytes anyway.
declare -A sha_by_base=()
for local_file in "${payload[@]}"; do
  base="$(basename "$local_file")"
  sha="$(sha256sum "$local_file" | awk '{print $1}')"
  sha_by_base["$base"]="$sha"
  "${SCP[@]}" "$local_file" "$PVE_USER@$PVE_HOST:$remote_tmp/$base"
  got="$("${SSH[@]}" "sha256sum '$remote_tmp/$base'" | awk '{print $1}')"
  [[ "$got" == "$sha" ]] || {
    echo "::error::BYTE VIOLATION hop1: $base sha256 $got != $sha — refusing to execute translated bytes" >&2
    exit 1; }
  echo "hop1 ok: $base sha256=$sha"
done

# Hop 2 (PVE host -> CT110): pct push is a binary copy; verify inside.
"${SSH[@]}" "pct exec $CT_ID -- mkdir -p '$REMOTE_DIR'"
for base in "${!sha_by_base[@]}"; do
  "${SSH[@]}" "pct push $CT_ID '$remote_tmp/$base' '$REMOTE_DIR/$base'"
  got="$("${SSH[@]}" "pct exec $CT_ID -- sha256sum '$REMOTE_DIR/$base'" | awk '{print $1}')"
  [[ "$got" == "${sha_by_base[$base]}" ]] || {
    echo "::error::BYTE VIOLATION hop2: $base sha256 $got != ${sha_by_base[$base]} inside CT110 — refusing to execute translated bytes" >&2
    exit 1; }
  echo "hop2 ok: $base sha256=$got"
done
"${SSH[@]}" "rm -rf '$remote_tmp'"

# --- execute ----------------------------------------------------------------
if [[ "$MODE" == "script" ]]; then
  # Run the staged script inside the container with the caller's arguments.
  # Each argument is re-escaped individually so the remote shell sees exactly
  # what the caller passed ($* alone would silently drop quoting).
  #
  # SCRIPT_ENV names environment variables that must reach the script
  # INSIDE the container (a `pct exec` does not inherit the runner's
  # environment).  The values are passed through `env NAME=value`, the same
  # way run_inner passes LH_EXPECTED_VERSION/LH_EXPECTED_COMMIT.  Only the
  # names are configured here; the values come from the caller's environment
  # and are never printed.
  full="pct exec $CT_ID -- env"
  for name in ${SCRIPT_ENV_NAMES[@]+"${SCRIPT_ENV_NAMES[@]}"}; do
    full="$full $(printf '%q=%q' "$name" "${!name-}")"
  done
  full="$full bash '$REMOTE_DIR/$SCRIPT_REMOTE_NAME'"
  for arg in ${SCRIPT_ARGS[@]+"${SCRIPT_ARGS[@]}"}; do
    full="$full $(printf '%q' "$arg")"
  done
  set +e
  remote_exec "$full"
  rc=$?
  set -e
  if [[ "$rc" -eq 2 ]]; then
    # Same overloaded rc as the inner script: the remote stderr block above
    # says whether this was an operator abort or a parse failure.
    echo "::error::remote rc=2 (OVERLOADED per the 2026-09-18 lesson): read the stderr block above before interpreting it." >&2
  fi
  exit "$rc"
fi

run_inner "$MODE"
