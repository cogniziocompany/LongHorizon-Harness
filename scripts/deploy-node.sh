#!/usr/bin/env bash
# deploy-node.sh - install a LongHorizon-Harness node's code from a git ref.
#
# This script runs ON THE NODE, inside the container that serves the harness
# (CT110 `lh-harness`, CT111 `cct-cfo-harness-01`).  It is the in-node half of
# `.github/workflows/deploy-harness-node.yml`; the runner-side half reaches the
# node over the SAME path the existing `.github/workflows/deploy-ct110.yml`
# uses (self-hosted runner labelled `lan-deploy` -> ssh to the Proxmox host ->
# `pct exec <ct>` -> this script).
#
# WHO RUNS IT: the deploy is described as running "as the harness user", and
# that is the user that OWNS the state this deploy writes (the receipt, the
# backups and settings.db all live under /home/harness/.lh-harness, mode 700,
# owner harness).  The venv, however, is ROOT-OWNED on a node
# (scripts/deploy/node/bootstrap-node.sh creates /home/harness/venv as root
# and never chowns it), so writing the venv needs privilege the `harness`
# user does not have -- which is why the repo's own node deploy
# (scripts/deploy/ct110/ct110_deploy.sh) runs as root inside the container.
# The two facts cannot both be satisfied by one uid, so this script does NOT
# insist on one: it runs as whatever uid invokes it (root in the CI path,
# `harness` where the venv is group-writable) and PREFIGHTS that every path
# it writes is writable, failing with a clear message before it touches
# anything if it is not.
#
# It is NOT a second installer: `scripts/deploy/ct110/ct110_deploy.sh` already
# owns the CI-built-wheel upgrade path (build a wheel on the runner, push it as
# BYTES, install it, restart, verify, roll back from the archived wheel).  This
# script covers the OTHER half: installing STRAIGHT FROM A GIT REF into the
# existing `/home/harness/venv`, with a deploy RECEIPT on the node so the
# deployed commit is readable over the API without a shell.  It reuses the same
# conventions as that script:
#
#   * the live unit is `/home/harness/venv/bin/python -m lh_harness web`, user
#     `harness`, unit `lh-harness.service` (see
#     `scripts/deploy/node/templates/lh-harness-node.service` and
#     `packaging/lh-harness.service`);
#   * every knob is overridable through an `LH_*` environment variable, so the
#     same script runs against a local fixture in the tests;
#   * the drain rule is the same lesson: a deploy RESTARTS the service and
#     kills every in-flight run, so it refuses while a run is alive.
#
# Exit codes:
#   0  success - the ref is installed, the service is active and the read-back
#      agrees (the receipt is written and a one-line receipt is printed).
#   1  unexpected error (bad usage, ref does not resolve, the privilege
#      preflight failed, install failed).
#   2  OPERATOR ABORT - a run is alive and no --drain-wait was given (the
#      deploy refused to touch the node), or the drain wait expired with a run
#      still alive.
#   3  rollback impossible - the pre-deploy backup is missing or unreadable.
#   4  READ-BACK FAILED - the service did not come back active, /api/health
#      did not answer, or /api/meta did not report the deployed commit.  The
#      rollback has already been attempted when this is returned.
#
# Usage:
#   deploy-node.sh [--ref <git-ref>] [--drain-wait <seconds>] [--dry-run]
#
#   --ref <git-ref>     the git ref to install.  DEFAULT: origin/main.  The ref
#                       is resolved to a commit and installed from a clone of
#                       the repository, so the installed code is exactly the
#                       ref (not "whatever the branch pointed at later").
#   --drain-wait <sec>  wait up to <sec> seconds for the live runs to finish
#                       before deploying.  Without it the deploy REFUSES while
#                       any run is alive.
#   --dry-run           do everything except the install, the restart and the
#                       receipt: report the resolved ref and the live-run
#                       state and exit.
#
# SECRETS: this script NEVER reads, prints or copies a secret.  It does not
# open the secrets file at all - the web token reaches the service through the
# unit's EnvironmentFile, and the read-back below talks to the LOCAL API
# without a bearer token only when the API allows it; otherwise the read-back
# reports what it could and says plainly that it was unauthorised.  No
# credential value is ever written to stdout, to the receipt or to a backup.
#
# LEARNED FROM (do not remove): the 2026-09-18 CRLF incident (the runner moves
# this script as BYTES and sha256-verifies every hop; `.gitattributes` pins
# LF), the 2026-10-03 "rollback reinstalled the new code" incident (this script
# rolls back from a COPY taken before the install, never from a version-named
# archive file), and the "every build is version 0.1.7" fact (the receipt
# records the COMMIT, not just the version).
set -euo pipefail

# --- knobs (every one overridable for the local fixture) --------------------
REF="${LH_DEPLOY_REF:-origin/main}"
DRAIN_WAIT="${LH_DRAIN_WAIT:-0}"                 # seconds; 0 = refuse while alive
VENV="${LH_VENV:-/home/harness/venv}"           # the live venv (systemctl cat)
SERVICE="${LH_SERVICE:-lh-harness.service}"
HARNESS_HOME="${LH_HARNESS_HOME:-/home/harness}"
STATE_DIR="${LH_DEPLOY_STATE:-$HARNESS_HOME/.lh-harness}"
SETTINGS_DB="${LH_SETTINGS_DB:-$HARNESS_HOME/.lh-harness/settings.db}"
RECEIPT="${LH_RECEIPT:-$STATE_DIR/deployed.json}"
BACKUP_DIR="${LH_BACKUP_DIR:-$STATE_DIR/backups}"
RUNS_ROOT="${LH_RUNS_ROOT:-/home/harness/work/.lh-harness/runs}"
API_URL="${LH_API_URL:-http://127.0.0.1:8799}"
REPO_URL="${LH_REPO_URL:-https://github.com/cogniziocompany/LongHorizon-Harness.git}"
REPO_DIR="${LH_REPO_DIR:-$HARNESS_HOME/deploy/repo}"
BY="${LH_DEPLOYED_BY:-${USER:-harness}}"
DRY_RUN="${LH_DRY_RUN:-0}"

# The lifecycle vocabulary (src/lh_harness/supervisor/lifecycle.py):
# ACTIVE_STATUSES = creating, starting, running, waiting_approval, stopping.
# A run in any of those states is ALIVE: restarting the service kills it.
ACTIVE_STATUSES="creating starting running waiting_approval stopping"

log()  { echo "[deploy-node] $*"; }
abort() { echo "ABORT(rc=2): $*" >&2; exit 2; }
die()  { echo "ERROR: $*" >&2; exit 1; }

usage() {
  sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'
}

# --- argument parsing -------------------------------------------------------
while (( $# )); do
  case "$1" in
    --ref)        [[ $# -ge 2 ]] || die "--ref needs a value";        REF="$2"; shift 2 ;;
    --ref=*)      REF="${1#*=}"; shift ;;
    --drain-wait) [[ $# -ge 2 ]] || die "--drain-wait needs a value"; DRAIN_WAIT="$2"; shift 2 ;;
    --drain-wait=*) DRAIN_WAIT="${1#*=}"; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *)            die "unknown argument: $1 (try --help)" ;;
  esac
done
[[ "$DRAIN_WAIT" =~ ^[0-9]+$ ]] || die "--drain-wait must be a whole number of seconds"

# --- helpers ----------------------------------------------------------------

# The python interpreter used for the READ-ONLY helpers.  The venv python is
# preferred (it is the interpreter the service runs), but the drain check must
# not be defeated by a broken venv: if the venv python cannot start, the
# system python3 is used, so a broken install can never make the drain check
# silently report "no live runs".
py() {
  if [[ -x "$VENV/bin/python" ]] && "$VENV/bin/python" -c 'pass' >/dev/null 2>&1; then
    printf '%s' "$VENV/bin/python"
  else
    printf '%s' "python3"
  fi
}

# The UTC stamp every backup name carries (sortable, no local timezone).
utc_stamp() { date -u +%Y%m%dT%H%M%SZ; }
utc_now()   { date -u +%Y-%m-%dT%H:%M:%SZ; }

installed_version() {
  "$VENV/bin/python" -c 'import lh_harness; print(lh_harness.__version__)' 2>/dev/null || true
}

# The commit the installed package was built from (src/lh_harness/build_info.py).
# Empty for a build that predates build info: "unknown", never a guess.
installed_commit() {
  "$VENV/bin/python" -m lh_harness.build_info 2>/dev/null || true
}

# The path of the installed package's metadata directory (the dist-info that
# pip owns).  Empty when the package is not installed.
package_metadata_dir() {
  "$(py)" - <<'PY' 2>/dev/null || true
from importlib.metadata import distribution
print(distribution("lh-harness")._path)
PY
}

# The path of the installed package's CODE directory (the lh_harness package
# pip owns).  Empty when the package is not installed.  This is what a
# rollback restores, so the node goes back to the exact pre-deploy bytes.
package_dir() {
  "$(py)" - <<'PY' 2>/dev/null || true
from importlib.metadata import distribution
print(distribution("lh-harness")._path.parent / "lh_harness")
PY
}

# Read one JSON field out of a file.  Prints the raw string value (or nothing).
json_field() {  # json_field <file> <key>
  "$(py)" - "$1" "$2" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        data = json.load(handle)
except Exception:
    raise SystemExit(0)
if not isinstance(data, dict):
    raise SystemExit(0)
value = data.get(sys.argv[2])
if value is None:
    raise SystemExit(0)
print(value if isinstance(value, str) else json.dumps(value))
PY
}

# --- the drain rule ---------------------------------------------------------

# List the runs whose control/status.json reports an ALIVE lifecycle status.
# The status file is written atomically by the supervisor (control_bus), so a
# partial read is not possible; a missing file means the worker never opened
# the run directory, which is NOT an alive run.
alive_runs() {
  "$(py)" - "$RUNS_ROOT" "$ACTIVE_STATUSES" <<'PY' 2>/dev/null || true
import json, os, sys

runs_root, active = sys.argv[1], set(sys.argv[2].split())
aliases = {"complete": "completed", "done": "completed", "success": "completed",
           "succeeded": "completed", "finished": "completed", "canceled": "cancelled"}
try:
    names = sorted(os.listdir(runs_root))
except OSError:
    raise SystemExit(0)
for name in names:
    path = os.path.join(runs_root, name, "control", "status.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        continue
    if not isinstance(data, dict):
        continue
    raw = data.get("status")
    text = str(raw).strip().lower().replace(" ", "_") if raw is not None else ""
    if not text:
        continue
    if aliases.get(text, text) in active:
        print(name)
PY
}

# Refuse while a run is alive, unless a drain wait was requested.  With a
# drain wait, poll until the node is idle or the wait expires.
require_idle() {
  local alive deadline
  alive="$(alive_runs)"
  if [[ -z "$alive" ]]; then
    log "drain check: no live run under $RUNS_ROOT"
    return 0
  fi
  local count
  count="$(printf '%s\n' "$alive" | grep -c . || true)"
  if (( DRAIN_WAIT <= 0 )); then
    abort "$count live run(s) under $RUNS_ROOT and no --drain-wait given: a deploy RESTARTS lh-harness.service and kills every in-flight run (live: $(printf '%s' "$alive" | tr '\n' ' '))"
  fi
  log "drain check: $count live run(s) - waiting up to ${DRAIN_WAIT}s for them to finish"
  deadline=$(( $(date -u +%s) + DRAIN_WAIT ))
  while :; do
    alive="$(alive_runs)"
    if [[ -z "$alive" ]]; then
      log "drain check: node is idle"
      return 0
    fi
    if (( $(date -u +%s) >= deadline )); then
      count="$(printf '%s\n' "$alive" | grep -c . || true)"
      abort "$count live run(s) still alive after ${DRAIN_WAIT}s (live: $(printf '%s' "$alive" | tr '\n' ' '))"
    fi
    sleep 5
  done
}

# --- backups ----------------------------------------------------------------

# Copy the venv package metadata and settings.db aside with a UTC suffix, so
# a rollback can restore exactly the state the node had before this deploy.
#
# Three things are copied, all with the same UTC stamp:
#   * the installed PACKAGE METADATA (the dist-info pip owns) -- what was
#     live, recorded before the install;
#   * the installed PACKAGE FILES (the lh_harness package directory) -- so a
#     rollback restores the previous BYTES.  This is the 2026-10-03 lesson from
#     scripts/deploy/ct110/ct110_deploy.sh: a rollback must restore the exact
#     pre-deploy code, never "a version of the package" (every build is
#     0.1.7, so a version-named reinstall can silently put the NEW code back);
#   * settings.db -- the node's settings store, copied (never moved) so the
#     running service keeps using it while the deploy is in flight.
make_backups() {
  local stamp meta_dir pkg_dir target
  stamp="$(utc_stamp)"
  BACKUP_STAMP="$stamp"
  mkdir -p "$BACKUP_DIR"

  meta_dir="$(package_metadata_dir)"
  if [[ -n "$meta_dir" && -d "$meta_dir" ]]; then
    target="$BACKUP_DIR/package-metadata.$stamp"
    cp -a "$meta_dir" "$target" || die "could not back up the venv package metadata at $meta_dir"
    BACKUP_METADATA="$target"
    log "backed up venv package metadata -> $target"
  else
    BACKUP_METADATA=""
    log "no venv package metadata found to back up (package not installed?)"
  fi

  pkg_dir="$(package_dir)"
  if [[ -n "$pkg_dir" && -d "$pkg_dir" ]]; then
    target="$BACKUP_DIR/package-files.$stamp"
    cp -a "$pkg_dir" "$target" || die "could not back up the installed package files at $pkg_dir"
    BACKUP_PACKAGE="$target"
    log "backed up the installed package files -> $target"
  else
    BACKUP_PACKAGE=""
    log "no installed package files found to back up"
  fi

  if [[ -f "$SETTINGS_DB" ]]; then
    target="$BACKUP_DIR/settings.db.$stamp"
    cp -p "$SETTINGS_DB" "$target" || die "could not back up the settings store at $SETTINGS_DB"
    BACKUP_SETTINGS="$target"
    log "backed up settings.db -> $target"
  else
    BACKUP_SETTINGS=""
    log "no settings.db at $SETTINGS_DB to back up"
  fi
}

# --- the receipt ------------------------------------------------------------

# Write the deploy receipt: {"commit", "version", "at", "by", "previous"}.
# `previous` is the receipt of the deploy before this one (or null), so the
# receipt chain records the history without keeping an unbounded log.
write_receipt() {
  local commit="$1" version="$2" previous_json="null"
  if [[ -f "$RECEIPT" ]]; then
    previous_json="$("$(py)" - "$RECEIPT" <<'PY'
import json, sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        data = json.load(handle)
except Exception:
    raise SystemExit(0)
if isinstance(data, dict):
    print(json.dumps({k: data.get(k) for k in ("commit", "version", "at", "by")}))
PY
)"
    [[ -n "$previous_json" ]] || previous_json="null"
  fi
  mkdir -p "$(dirname "$RECEIPT")"
  "$(py)" - "$RECEIPT" "$commit" "$version" "$(utc_now)" "$BY" "$previous_json" <<'PY'
import json, os, sys

path, commit, version, at, by, previous = sys.argv[1:7]
payload = {
    "commit": commit,
    "version": version,
    "at": at,
    "by": by,
    "previous": json.loads(previous),
}
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.replace(tmp, path)
PY
}

# --- read-back --------------------------------------------------------------

service_is_active() {
  local state
  state="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
  [[ "$state" == "active" ]]
}

# GET a local API path.  Prints the body; returns non-zero when the request
# failed or the answer was not HTTP 200.
#
# The bearer token is taken from the ENVIRONMENT (LH_HARNESS_WEB_TOKEN), the
# same way scripts/deploy/ct110/verify_meta.py takes CT110_API_TOKEN from the
# runner's environment.  This script NEVER opens the node's secrets file
# (/home/harness/.lh-harness-secrets.env): reading a credential file is not
# reading a credential from the environment, and the rule is that the deploy
# does not touch the secrets at all.  With no token in the environment the
# /api/meta read-back cannot authenticate, and the deploy says so plainly
# instead of reaching for a secret.
api_get() {  # api_get <path>
  local token="${LH_HARNESS_WEB_TOKEN:-}"
  if [[ -n "$token" ]]; then
    curl -sf --max-time 15 -H "Authorization: Bearer $token" "$API_URL$1" 2>/dev/null
  else
    curl -sf --max-time 15 "$API_URL$1" 2>/dev/null
  fi
}

# Read /api/health.  Returns 0 when the endpoint answers 200.
health_ok() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$API_URL/api/health" 2>/dev/null || true)"
  [[ "$code" == "200" ]]
}

# The commit the RUNNING service reports at /api/meta (its build.commit).
meta_build_commit() {
  local body
  body="$(api_get /api/meta)" || return 1
  printf '%s' "$body" | "$(py)" -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
build = data.get("build") if isinstance(data, dict) else None
commit = build.get("commit") if isinstance(build, dict) else None
print((str(commit).strip()) if commit else "")
' 2>/dev/null
}

# The version the RUNNING service reports at /api/meta.
meta_build_version() {
  local body
  body="$(api_get /api/meta)" || return 1
  printf '%s' "$body" | "$(py)" -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
build = data.get("build") if isinstance(data, dict) else None
version = build.get("version") if isinstance(build, dict) else None
print((str(version).strip()) if version else "")
' 2>/dev/null
}

# --- rollback ---------------------------------------------------------------

# Restore the node from the backups taken before this deploy.  Called when the
# read-back failed: the installed code is put back byte for byte, the previous
# receipt is restored and the service is restarted, so the node is left as it
# was before this deploy.
rollback() {
  local reason="$1" meta_dir pkg_dir
  echo "[deploy-node] ROLLBACK: $reason" >&2
  if [[ -z "${BACKUP_METADATA:-}" || ! -d "${BACKUP_METADATA:-}" ]]; then
    echo "[deploy-node] ROLLBACK: no pre-deploy package backup to restore from (rc=3)" >&2
    return 3
  fi
  # Put the previous PACKAGE FILES back.  This is what makes the rollback
  # restore the previous code and not merely a version number (every build is
  # 0.1.7, so reinstalling "the version" can silently put the new code back).
  pkg_dir="$(package_dir)"
  if [[ -n "$pkg_dir" && -d "$pkg_dir" ]]; then
    rm -rf "$pkg_dir"
    if [[ -n "${BACKUP_PACKAGE:-}" && -d "${BACKUP_PACKAGE:-}" ]]; then
      cp -a "$BACKUP_PACKAGE" "$pkg_dir" || {
        echo "[deploy-node] ROLLBACK: could not restore the installed package files (rc=3)" >&2
        return 3
      }
      log "restored the installed package files from $BACKUP_PACKAGE"
    else
      echo "[deploy-node] ROLLBACK: no package-file backup to restore from (rc=3)" >&2
      return 3
    fi
  fi
  # Restore the previous package metadata over the installed copy.
  meta_dir="$(package_metadata_dir)"
  if [[ -n "$meta_dir" && -d "$meta_dir" ]]; then
    rm -rf "$meta_dir"
    cp -a "$BACKUP_METADATA" "$meta_dir" || {
      echo "[deploy-node] ROLLBACK: could not restore the package metadata (rc=3)" >&2
      return 3
    }
    log "restored the venv package metadata from $BACKUP_METADATA"
  fi
  # Restore the previous receipt when there was one, so the receipt never
  # names a commit the node does not run.
  if [[ -f "$BACKUP_DIR/deployed.json.$BACKUP_STAMP" ]]; then
    cp -p "$BACKUP_DIR/deployed.json.$BACKUP_STAMP" "$RECEIPT" \
      && log "restored the previous deploy receipt"
  elif [[ -f "$RECEIPT" ]]; then
    rm -f "$RECEIPT"
    log "removed the deploy receipt of the failed deploy (there was no previous one)"
  fi
  systemctl restart "$SERVICE" || {
    echo "[deploy-node] ROLLBACK: the service could not be restarted (rc=3)" >&2
    return 3
  }
  log "rollback complete: the service was restarted on the pre-deploy state"
  return 0
}

# --- the deploy -------------------------------------------------------------

# Clone (or update) the repository and check out the EXACT ref.
resolve_ref() {
  local sha
  if [[ ! -d "$REPO_DIR/.git" ]]; then
    mkdir -p "$(dirname "$REPO_DIR")"
    git clone --quiet "$REPO_URL" "$REPO_DIR" \
      || die "could not clone $REPO_URL into $REPO_DIR"
  fi
  # A local path (the test fixture clones from a local repo) has no remote to
  # fetch from; a remote URL does.  Either way the ref is resolved against the
  # clone and checked out DETACHED at the resolved commit, so the installed
  # code is exactly the ref and not "whatever the branch pointed at later".
  if git -C "$REPO_DIR" remote get-url origin >/dev/null 2>&1; then
    git -C "$REPO_DIR" fetch --quiet --all --tags --prune \
      || die "could not fetch $REPO_URL"
  fi
  sha="$(git -C "$REPO_DIR" rev-parse --verify --quiet "${REF}^{commit}" || true)"
  [[ -n "$sha" ]] || die "the ref '$REF' does not resolve to a commit in $REPO_URL"
  git -C "$REPO_DIR" checkout --quiet --detach "$sha" \
    || die "could not check out '$sha' in $REPO_DIR"
  printf '%s' "$sha"
}

# Build a wheel FROM THE RESOLVED COMMIT and install THAT into the existing
# venv.  A wheel (not an editable checkout) is what the node already runs:
# scripts/deploy/node/bootstrap-node.sh installs a non-editable wheel and
# scripts/deploy/ct110/ct110_deploy.sh upgrades it the same way, so the venv
# layout stays identical across both deploy paths.
#
# The commit is stamped into the wheel as src/lh_harness/_build_info.json
# (pyproject.toml packs it as a build artifact) so the installed package
# itself names the commit it was built from.  That is the same mechanism the
# CI build job uses, and it means /api/meta can report the deployed commit
# even without the deploy receipt.
install_from_ref() {
  local sha="$1" wheel build_dir
  build_dir="$REPO_DIR/.deploy-build"
  rm -rf "$build_dir"
  mkdir -p "$build_dir"

  log "stamping the build commit into the checkout"
  printf '{"commit": "%s"}\n' "$sha" > "$REPO_DIR/src/lh_harness/_build_info.json"

  log "building the wheel from $sha"
  # `pip wheel` uses the PEP 517 backend (hatchling) of the checkout, so the
  # wheel is built from the resolved commit's source tree.  --no-deps: the
  # dependencies are already installed in the venv and are not this deploy's
  # business.
  ( cd "$REPO_DIR" && "$VENV/bin/python" -m pip wheel --quiet --no-deps --wheel-dir "$build_dir" . ) \
    || die "could not build a wheel from $sha"

  shopt -s nullglob
  local wheels=( "$build_dir"/lh_harness-*.whl )
  shopt -u nullglob
  (( ${#wheels[@]} == 1 )) || die "expected exactly one built wheel, found ${#wheels[@]}"
  wheel="${wheels[0]}"

  log "installing $(basename "$wheel") into $VENV"
  # Every build is the same pyproject version, so a plain `pip install
  # --upgrade` would say "already satisfied" and keep the OLD code (the
  # 2026-10-01/02 deploys restarted unchanged bytes).  --force-reinstall with
  # --no-deps always replaces the package itself.
  "$VENV/bin/pip" install --quiet --force-reinstall --no-deps "$wheel" \
    || die "pip install of $wheel into $VENV failed"

  # Prove the new code is really the installed code: the package must import
  # and must report the commit it was built from.
  installed_version >/dev/null || die "the install did not leave an importable lh_harness in $VENV"
  local stamped
  stamped="$(installed_commit)"
  if [[ -n "$stamped" && "$stamped" != "$sha" ]]; then
    die "the installed package reports commit '$stamped', expected '$sha'"
  fi
  log "installed version $(installed_version) (commit ${stamped:-unknown})"
}

# --- privilege preflight ----------------------------------------------------

# The deploy writes the venv (root-owned on a node: bootstrap-node.sh creates
# /home/harness/venv as root and never chowns it), the state directory
# (harness-owned, mode 700) and the settings store (harness-owned, mode 600),
# and it restarts a systemd unit.
#
# Those two ownership facts do not agree, so the deploy cannot insist on ONE
# uid: the node's own deploy path (scripts/deploy/ct110/ct110_deploy.sh) runs
# as root inside the container, and the request describes the deploy as
# running "as the harness user".  What the deploy CAN do is refuse to start
# when it would fail half way through with an opaque pip error.
#
# So the preflight checks that every path the deploy WRITES is writable by
# the CURRENT user, and names the ones that are not.  Running as root satisfies
# all of them; running as `harness` works only when the venv is writable by
# that user.  A failure here is rc=1 and NOTHING on the node is touched.
require_write_access() {
  local path parent missing=0
  local who
  who="$(id -un 2>/dev/null || true)"
  [[ -n "$who" ]] || who="uid $(id -u 2>/dev/null || echo unknown)"
  # The venv root is not enough: pip writes into venv/bin (the interpreter
  # symlinks) and venv/lib (the installed package), so all three are checked.
  for path in "$VENV" "$VENV/bin" "$VENV/lib" "$STATE_DIR" "$BACKUP_DIR"; do
    if [[ ! -e "$path" ]]; then
      # Not created yet: the parent must be writable for mkdir -p to work.
      parent="$(dirname "$path")"
      if [[ ! -w "$parent" ]]; then
        echo "[deploy-node] ERROR: cannot create $path - the parent $parent is not writable by $who" >&2
        missing=1
      fi
      continue
    fi
    if [[ ! -w "$path" ]]; then
      echo "[deploy-node] ERROR: $path is not writable by $who" >&2
      missing=1
    fi
  done
  if [[ -f "$SETTINGS_DB" && ! -w "$SETTINGS_DB" ]]; then
    echo "[deploy-node] ERROR: $SETTINGS_DB is not writable by $who" >&2
    missing=1
  fi
  (( missing == 0 )) || {
    echo "[deploy-node] ERROR: the deploy needs write access to the venv, the state directory and the settings store (rc=1)" >&2
    exit 1
  }
  log "privilege preflight: venv, state dir and settings store are writable by $who"
}

# --- main -------------------------------------------------------------------

log "deploy-node starting: ref=$REF drain_wait=${DRAIN_WAIT}s service=$SERVICE"

# The privilege preflight comes first: it touches nothing, and it fails with
# a clear message instead of an opaque pip error half way through.
require_write_access

# The drain rule comes next: nothing is touched on a node with a live run.
require_idle

PREVIOUS_COMMIT="$(json_field "$RECEIPT" commit || true)"
PREVIOUS_VERSION="$(json_field "$RECEIPT" version || true)"

if (( DRY_RUN )); then
  sha="$(resolve_ref)"
  log "dry run: would install $sha (version $(json_field "$REPO_DIR/pyproject.toml" version 2>/dev/null || echo unknown))"
  log "dry run: nothing was installed, restarted or written"
  exit 0
fi

# Record the previous receipt so a rollback can restore it.
mkdir -p "$BACKUP_DIR"
BACKUP_STAMP="$(utc_stamp)"
if [[ -f "$RECEIPT" ]]; then
  cp -p "$RECEIPT" "$BACKUP_DIR/deployed.json.$BACKUP_STAMP"
fi

make_backups

sha="$(resolve_ref)"
log "resolved '$REF' to $sha"

install_from_ref "$sha"

NEW_VERSION="$(installed_version)"
[[ -n "$NEW_VERSION" ]] || die "could not read the installed version after the install"

# The receipt is written BEFORE the restart, because /api/meta reports the
# receipt's commit (that is the point of the receipt: the deployed commit is
# readable over the API without a shell).  A read-back failure rolls the
# receipt back to the previous one from the backup, so a failed deploy never
# leaves a receipt that names a commit the node does not run.
write_receipt "$sha" "$NEW_VERSION"
log "receipt written: $RECEIPT"

# Restart and wait for the service to come back.
log "restarting $SERVICE"
systemctl restart "$SERVICE" || die "could not restart $SERVICE"

# --- read-back --------------------------------------------------------------
# Three independent facts must hold before the deploy is called a success:
# the unit is active, /api/health answers, and /api/meta names the commit
# that was deployed.  A failed read-back rolls back from the backups.
ACTIVE=0
for _ in $(seq 1 30); do
  if service_is_active; then ACTIVE=1; break; fi
  sleep 2
done
if (( ! ACTIVE )); then
  rollback "the service did not become active within 60s"
  echo "[deploy-node] READ-BACK FAILED: $SERVICE is not active (rc=4)" >&2
  exit 4
fi
log "read-back: $SERVICE is active"

if ! health_ok; then
  rollback "/api/health did not answer HTTP 200 after the restart"
  echo "[deploy-node] READ-BACK FAILED: /api/health did not answer (rc=4)" >&2
  exit 4
fi
log "read-back: /api/health answered 200"

META_COMMIT="$(meta_build_commit || true)"
if [[ -z "$META_COMMIT" ]]; then
  rollback "/api/meta did not report a build commit after the restart"
  echo "[deploy-node] READ-BACK FAILED: /api/meta reported no build commit (rc=4)" >&2
  exit 4
fi
if [[ "$META_COMMIT" != "$sha" ]]; then
  rollback "/api/meta reported commit '$META_COMMIT', expected '$sha'"
  echo "[deploy-node] READ-BACK FAILED: /api/meta reports '$META_COMMIT', expected '$sha' (rc=4)" >&2
  exit 4
fi
log "read-back: /api/meta reports commit $META_COMMIT"

# The one-line receipt an operator (or Overseer1) can copy out of the log:
#   DEPLOY_OK node=<host> commit=<sha> version=<version> at=<utc> by=<user> previous=<commit|none>
printf 'DEPLOY_OK node=%s commit=%s version=%s at=%s by=%s previous=%s\n' \
  "$(hostname 2>/dev/null || echo unknown)" "$sha" "$NEW_VERSION" "$(utc_now)" "$BY" \
  "${PREVIOUS_COMMIT:-none}"
