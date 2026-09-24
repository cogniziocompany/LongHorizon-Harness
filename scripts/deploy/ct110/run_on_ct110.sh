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
#   run_on_ct110.sh exec <cmd...>   run an arbitrary command inside CT110
#
# Required environment: PVE_HOST, PVE_USER, SSH_KEY_FILE, CT_ID.
# deploy additionally: EXPECTED_VERSION.  The wheel is picked up from ./stage/.
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

run_inner() {  # run_inner deploy|rollback
  local mode="$1" full rc
  full="pct exec $CT_ID -- env LH_EXPECTED_VERSION='${EXPECTED_VERSION:-}' bash '$REMOTE_DIR/ct110_deploy.sh' '$mode'"
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

[[ "$MODE" == "deploy" || "$MODE" == "rollback" ]] || {
  echo "usage: run_on_ct110.sh deploy|rollback|exec ..." >&2; exit 1; }

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
run_inner "$MODE"
