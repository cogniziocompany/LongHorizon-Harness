#!/usr/bin/env bash
# First install of a LongHorizon-Harness node (run as root INSIDE the new CT).
#
#   bootstrap-node.sh <wheel> <node-id> <workspace-root>
#   e.g. bootstrap-node.sh /root/lh-bootstrap/lh_harness-0.1.7-py3-none-any.whl ct111 /home/harness/work-cfo
#
# All three arguments are required.  <node-id> selects templates/config.<node-id>.toml and
# becomes LH_HARNESS_FLEET_NODE.  The script's templates/ directory must travel with it.
#
# What it leaves behind is the layout the CI deploy (scripts/deploy/ct110/ct110_deploy.sh,
# driven by .github/workflows/deploy-ct110.yml) assumes, so the node's FIRST pipeline deploy
# and rollback work:
#   user harness; root-owned venv /home/harness/venv with the wheel installed NON-editable;
#   /home/harness/deploy/wheels seeded with that wheel + deploy/state/previous_version;
#   <workspace-root>; /var/lib/lh-harness/runs; <service dir>/.lh-harness/config.toml;
#   /home/harness/.lh-harness-secrets.env (600, harness); lh-harness.service enabled+started,
#   listening on 0.0.0.0:8799.
#
# Optional environment: LH_NODE_SERVICE_DIR (/home/harness/node), LH_NODE_RUNS_ROOT
# (/var/lib/lh-harness/runs), LH_NODE_PORT (8799), LH_NODE_PYTHON (3.12),
# LH_NODE_NODE_MAJOR (22).
#
# Idempotent - safe to re-run; it only fills in what is missing:
#   - an existing config.toml, mcp_profiles.json or secrets file is NEVER overwritten;
#   - an already-installed lh-harness is NEVER replaced (upgrades belong to the pipeline, which
#     drains the queue and waits for a zero-active window first);
#   - a running service is NEVER restarted (a restart kills in-flight runs).
#
# Gotchas this script handles:
#   - Debian 12 ships Python 3.11; the fleet runs 3.12 (CI builds with uv's 3.12).  When no
#     python3.12 is on PATH, a standalone 3.12 is installed with uv into /opt/lh-python - NOT
#     under /root, which the harness user cannot traverse.
#   - ct110_deploy.sh calls $VENV/bin/pip, so the venv is made with `python -m venv` (pip
#     included), not `uv venv` (no pip).
#   - Debian 12 ships Node 18; agent workers need a current Node, installed from the NodeSource
#     apt repository (signed-by keyring, no curl|bash).
#   - Secrets: the web token is generated on this box, written straight into the 600 file and
#     never echoed; every other credential is an EMPTY placeholder an admin fills in.  The unit
#     reads them through EnvironmentFile only - no Environment=NAME=value line (the CT110
#     hygiene issue in scripts/deploy/ct110/README.md is not reproduced here).
#   - Settings store (docs/settings-store.md): the service keeps its configuration and
#     credentials in a SQLite DB it creates and migrates itself; only the four bootstrap
#     variables stay in the environment.  The bootstrap generates
#     LH_HARNESS_SETTINGS_KEY (fresh CT only) and seeds the node's configured
#     defaults (templates/settings.ct111.txt, the CT110-DISK retention values) into
#     the DB through the CLI's own `settings import` - never hard-coded in logic.
#   - Templates checked out on Windows may carry CRLF; CRs are stripped on install.
set -euo pipefail

WHEEL="${1:?wheel (path to lh_harness-<version>-py3-none-any.whl)}"
NODE_ID="${2:?node-id (e.g. ct111)}"
WORKSPACE_ROOT="${3:?workspace-root (e.g. /home/harness/work-cfo)}"

HARNESS_USER="harness"
HARNESS_HOME="/home/harness"
VENV="$HARNESS_HOME/venv"
SERVICE_DIR="${LH_NODE_SERVICE_DIR:-$HARNESS_HOME/node}"
RUNS_ROOT="${LH_NODE_RUNS_ROOT:-/var/lib/lh-harness/runs}"
PORT="${LH_NODE_PORT:-8799}"
PY_VERSION="${LH_NODE_PYTHON:-3.12}"
NODE_MAJOR="${LH_NODE_NODE_MAJOR:-22}"
ENV_FILE="$HARNESS_HOME/.lh-harness-secrets.env"
DEPLOY_ROOT="$HARNESS_HOME/deploy"
SERVICE="lh-harness.service"
UNIT_PATH="/etc/systemd/system/$SERVICE"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_TEMPLATE="$SCRIPT_DIR/templates/config.$NODE_ID.toml"
UNIT_TEMPLATE="$SCRIPT_DIR/templates/lh-harness-node.service"
# Node-default settings table (docs/settings-store.md "Moving from the env file"):
# plain NAME=value rows the bootstrap imports into the settings DB exactly once.
# For this node (ct111) the rows carry the CT110-DISK retention defaults sourced
# from queued task q-a14cd599278c4235.
SETTINGS_DEFAULTS_FILE="$SCRIPT_DIR/templates/settings.ct111.txt"
SEED_ACTOR="${LH_NODE_SEED_ACTOR:-ct111-bootstrap}"

log()   { echo "[bootstrap-node] $*"; }
abort() { echo "ABORT: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || abort "run as root inside the CT"
command -v systemctl >/dev/null 2>&1 || abort "systemctl not found - this script targets a systemd CT"
command -v apt-get >/dev/null 2>&1 || abort "apt-get not found - this script targets Debian/Ubuntu"
[[ "$NODE_ID" =~ ^[a-z][a-z0-9-]*$ ]] || abort "node-id '$NODE_ID' must be lowercase letters, digits and dashes"
[ -f "$WHEEL" ] || abort "wheel $WHEEL not found"
case "$(basename "$WHEEL")" in
  lh_harness-*-py3-none-any.whl) ;;
  *) abort "wheel must be named lh_harness-<version>-py3-none-any.whl (the deploy/rollback scripts match that name)" ;;
esac
case "$WORKSPACE_ROOT" in
  "$HARNESS_HOME"/?*) ;;
  *) abort "workspace-root must be an absolute path under $HARNESS_HOME" ;;
esac
[[ "$PORT" =~ ^[0-9]+$ ]] || abort "LH_NODE_PORT '$PORT' is not a port number"
[ -f "$CONFIG_TEMPLATE" ] || abort "no config template for node '$NODE_ID' ($CONFIG_TEMPLATE)"
[ -f "$UNIT_TEMPLATE" ] || abort "unit template missing ($UNIT_TEMPLATE)"
[ -f "$SETTINGS_DEFAULTS_FILE" ] || abort "settings defaults table missing ($SETTINGS_DEFAULTS_FILE)"

# Value of NAME in the secrets file ('' when absent/empty).  Callers never print it.
env_value() {
  [ -f "$ENV_FILE" ] || return 0
  { grep -E "^$1=" "$ENV_FILE" || true; } | tail -n 1 | cut -d= -f2- | tr -d "\"'"
}

# --- 1. runtime packages ----------------------------------------------------
log "installing runtime packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends ca-certificates curl git gh gnupg python3 python3-venv >/dev/null

node_major() { node --version 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/'; }
if [ "$(node_major || true)" != "$NODE_MAJOR" ]; then
  log "installing Node $NODE_MAJOR (NodeSource apt repository)"
  install -d -m 755 /etc/apt/keyrings
  curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
    | gpg --dearmor --yes -o /etc/apt/keyrings/nodesource.gpg
  echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_$NODE_MAJOR.x nodistro main" \
    > /etc/apt/sources.list.d/nodesource.list
  apt-get update -qq
  apt-get install -y -qq nodejs >/dev/null
fi
log "node $(node --version), git $(git --version | awk '{print $3}')"

# --- 2. user ----------------------------------------------------------------
if ! id "$HARNESS_USER" >/dev/null 2>&1; then
  log "creating user $HARNESS_USER"
  useradd --create-home --home-dir "$HARNESS_HOME" --shell /bin/bash "$HARNESS_USER"
fi

# --- 3. python + venv + wheel -----------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
  if command -v "python$PY_VERSION" >/dev/null 2>&1; then
    base_python="$(command -v "python$PY_VERSION")"
  else
    log "no python$PY_VERSION on PATH - installing a standalone one with uv into /opt/lh-python"
    [ -x /opt/lh-uv/bin/uv ] || { python3 -m venv /opt/lh-uv; /opt/lh-uv/bin/pip install --quiet uv; }
    UV_PYTHON_INSTALL_DIR=/opt/lh-python /opt/lh-uv/bin/uv python install "$PY_VERSION"
    base_python="$(UV_PYTHON_INSTALL_DIR=/opt/lh-python UV_PYTHON_PREFERENCE=only-managed \
      /opt/lh-uv/bin/uv python find "$PY_VERSION")"
  fi
  log "creating venv $VENV from $base_python"
  "$base_python" -m venv "$VENV"
fi
[ -x "$VENV/bin/pip" ] || abort "$VENV has no pip - ct110_deploy.sh needs \$VENV/bin/pip; remove the venv and re-run"

# `pip show` exits 1 when the package is absent; under pipefail that must read as "".
installed_version() { { "$VENV/bin/pip" show lh-harness 2>/dev/null || true; } | awk '/^Version:/{print $2}'; }
current="$(installed_version)"
if [ -n "$current" ]; then
  log "lh-harness $current is already installed - leaving it (upgrades go through the deploy pipeline)"
else
  log "installing $(basename "$WHEEL") into $VENV (non-editable)"
  "$VENV/bin/pip" install --quiet "$WHEEL"
  current="$(installed_version)"
  [ -n "$current" ] || abort "pip reported no lh-harness version after install"
fi
"$VENV/bin/python" -c 'import lh_harness; print(lh_harness.__version__)' >/dev/null \
  || abort "lh_harness does not import from $VENV"

# --- 4. directories + deploy seed -------------------------------------------
install -d -o "$HARNESS_USER" -g "$HARNESS_USER" -m 755 "$WORKSPACE_ROOT" "$SERVICE_DIR" \
  "$SERVICE_DIR/.lh-harness" "$HARNESS_HOME/.lh-harness" "$(dirname "$RUNS_ROOT")" "$RUNS_ROOT"
install -d -m 755 "$DEPLOY_ROOT" "$DEPLOY_ROOT/wheels" "$DEPLOY_ROOT/state"
# Seed the rollback archive with the wheel that is live now, and record it as the previous
# version, so the first pipeline deploy has something to roll back to.
if [ ! -f "$DEPLOY_ROOT/wheels/$(basename "$WHEEL")" ]; then
  cp -f "$WHEEL" "$DEPLOY_ROOT/wheels/"
fi
[ -s "$DEPLOY_ROOT/state/previous_version" ] || printf '%s\n' "$current" > "$DEPLOY_ROOT/state/previous_version"

# --- 5. config (only if absent) ---------------------------------------------
config_path="$SERVICE_DIR/.lh-harness/config.toml"
if [ -f "$config_path" ]; then
  log "config $config_path exists - left untouched"
else
  tr -d '\r' < "$CONFIG_TEMPLATE" | sed -e "s|@RUNS_ROOT@|$RUNS_ROOT|g" > "$config_path"
  chown "$HARNESS_USER:$HARNESS_USER" "$config_path"; chmod 644 "$config_path"
  log "wrote $config_path from $(basename "$CONFIG_TEMPLATE")"
fi
# Refuse to start a service on a config the harness itself rejects.
"$VENV/bin/python" -c 'import sys; from lh_harness.config import load_run_defaults; load_run_defaults(sys.argv[1])' \
  "$config_path" || abort "$config_path does not load - fix it and re-run"

# Workers run with cwd = the run's workspace, where this node's project config is not
# visible; mirror its [run.mcp_profiles] to the harness user's state root so the custom
# profile names resolve there too.
profiles_path="$HARNESS_HOME/.lh-harness/mcp_profiles.json"
if [ ! -f "$profiles_path" ]; then
  "$VENV/bin/python" - "$config_path" "$profiles_path" <<'PY'
import json, sys
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
with open(sys.argv[1], "rb") as fh:
    profiles = (tomllib.load(fh).get("run") or {}).get("mcp_profiles") or {}
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump(profiles, fh, indent=2, sort_keys=True)
    fh.write("\n")
PY
  chown "$HARNESS_USER:$HARNESS_USER" "$profiles_path"; chmod 644 "$profiles_path"
  log "wrote $profiles_path"
fi

# --- 6. secrets file (only if absent) ---------------------------------------
if [ -f "$ENV_FILE" ]; then
  log "$ENV_FILE exists - left untouched"
else
  (
    umask 077
    {
      echo "# lh-harness secrets for node $NODE_ID - mode 600, owner $HARNESS_USER."
      echo "# Read by $SERVICE through EnvironmentFile.  Never commit, print or copy these values."
      echo "# Format: NAME=value, no quotes, no spaces around '='."
      echo "# After editing: systemctl restart $SERVICE (only while no runs are active)."
      echo
      echo "# Bearer token for this node's web API.  Generated on this box at bootstrap."
      echo "# An admin copies it to: GitHub environment secret + the Hydra host .env."
      printf 'LH_HARNESS_WEB_TOKEN=%s\n' "$("$VENV/bin/python" -c 'import secrets; print(secrets.token_hex(24))')"
      echo
      echo "# MCP gateway.  URL is not a secret (empty = the public prod gateway; 'lan' = LAN alias)."
      echo "# KEY: a gateway key scoped to THIS node's servers only - never another node's key."
      echo "LH_HARNESS_MCP_GATEWAY_URL="
      echo "LH_HARNESS_MCP_GATEWAY_KEY="
      echo
      echo "# GitHub token scoped to the repos this node may touch - never another node's token."
      echo "GH_TOKEN="
      echo
      echo "# Fleet reporting.  URL/NODE/LABELS are not secrets; KEY is this node's own device key"
      echo "# (issued by fleet-admin enrolment).  Reporting stays off while the URL is empty, and"
      echo "# the reporter needs all four.  NODE is set explicitly: it must equal the host name"
      echo "# enrolled in fleet-admin and must never fall back to the container hostname."
      echo "LH_HARNESS_FLEET_URL="
      echo "LH_HARNESS_FLEET_NODE=$NODE_ID"
      echo "LH_HARNESS_FLEET_KEY="
      echo "LH_HARNESS_FLEET_LABELS=kind=$NODE_ID,host=$(hostname)"
      echo
      echo "# Embedded settings store bootstrap (docs/settings-store.md)."
      echo "# LH_HARNESS_SETTINGS_KEY is the only one generated here; the others are"
      echo "# placeholders that the admin/owner fills in before the admin screen is used."
      echo "# These four names are BOOTSTRAP variables and must stay in the environment;"
      echo "# the store rejects attempts to put them into the DB."
      echo "#"
      echo "# Encryption key for the settings DB (>= 32 chars). Without it the store is off."
      printf 'LH_HARNESS_SETTINGS_KEY=%s\n' "$("$VENV/bin/python" -c 'import secrets; print(secrets.token_hex(32))')"
      echo "# Database path. Empty means the default for the service user:"
      echo "#   ~/.lh-harness/settings.db  (created 0600, parent directory 0700)."
      echo "LH_HARNESS_SETTINGS_DB="
      echo "# Comma-separated SSO e-mails allowed into /settings. Empty = nobody (fail-closed)."
      echo "LH_HARNESS_SETTINGS_ADMINS="
      echo "# Secret the SSO edge sends as X-LH-Proxy-Auth. Empty = nobody."
      echo "LH_HARNESS_SETTINGS_PROXY_AUTH="
    } > "$ENV_FILE.new"
  )
  chown "$HARNESS_USER:$HARNESS_USER" "$ENV_FILE.new"
  mv "$ENV_FILE.new" "$ENV_FILE"
  log "wrote $ENV_FILE (web token generated here; every other credential is an empty placeholder)"
fi
chown "$HARNESS_USER:$HARNESS_USER" "$ENV_FILE"; chmod 600 "$ENV_FILE"
[ -n "$(env_value LH_HARNESS_WEB_TOKEN)" ] \
  || abort "$ENV_FILE has no LH_HARNESS_WEB_TOKEN - the service refuses a LAN bind without it"
[ -n "$(env_value LH_HARNESS_SETTINGS_KEY)" ] \
  || abort "$ENV_FILE has no LH_HARNESS_SETTINGS_KEY - the settings store needs it (docs/settings-store.md)"

# --- 6b. settings store (docs/settings-store.md) ------------------------------
# The service keeps its configuration and credentials in a SQLite database it
# creates and migrates itself (docs/settings-store.md); only the four bootstrap
# variables stay in the environment.  What this section does, idempotently:
#   1. generate LH_HARNESS_SETTINGS_KEY into the env file (fresh CT only);
#   2. seed the node's DEFAULTS table rows (disk retention etc.) into the DB,
#      the exact same `settings import` an admin would run on CT110 by hand;
#   3. write the four bootstrap names into the unit's EnvironmentFile (already
#      there from step 6; asserted at the end of the section).
# A secret VALUE never appears in output: import and this script both print
# names only, and the key was already written into the 600 file by step 6.
SETTINGS_DB="$HARNESS_HOME/.lh-harness/settings.db"
if [ ! -f "$SETTINGS_DB" ]; then
  install -d -o "$HARNESS_USER" -g "$HARNESS_USER" -m 700 "$HARNESS_HOME/.lh-harness"
  # The store needs the key in its environment; hand it over through `env`,
  # never as a shell argument or in a log line.
  log "importing the settings defaults table into $SETTINGS_DB (names only are printed)"
  runuser -u "$HARNESS_USER" -- env \
    LH_HARNESS_SETTINGS_DB="$SETTINGS_DB" \
    LH_HARNESS_SETTINGS_KEY="$(env_value LH_HARNESS_SETTINGS_KEY)" \
    "$VENV/bin/lh-harness" settings import --env-file "$SETTINGS_DEFAULTS_FILE" --actor "$SEED_ACTOR"
  runuser -u "$HARNESS_USER" -- env \
    LH_HARNESS_SETTINGS_DB="$SETTINGS_DB" \
    LH_HARNESS_SETTINGS_KEY="$(env_value LH_HARNESS_SETTINGS_KEY)" \
    "$VENV/bin/lh-harness" settings list >/dev/null
  chown "$HARNESS_USER:$HARNESS_USER" "$SETTINGS_DB"; chmod 600 "$SETTINGS_DB"
  chmod 700 "$HARNESS_HOME/.lh-harness"
else
  log "settings DB $SETTINGS_DB exists - left untouched"
fi
for name in LH_HARNESS_SETTINGS_KEY LH_HARNESS_SETTINGS_DB \
            LH_HARNESS_SETTINGS_ADMINS LH_HARNESS_SETTINGS_PROXY_AUTH; do
  grep -qE "^$name=" "$ENV_FILE" \
    || log "NOTE: $name is not in $ENV_FILE yet - the settings store stays off until it is"
done

# --- 7. systemd unit ---------------------------------------------------------
unit_new="$(mktemp)"
tr -d '\r' < "$UNIT_TEMPLATE" | sed \
  -e "s|@SERVICE_DIR@|$SERVICE_DIR|g" -e "s|@PORT@|$PORT|g" \
  -e "s|@RUNS_ROOT@|$RUNS_ROOT|g" -e "s|@WORKSPACE_ROOT@|$WORKSPACE_ROOT|g" > "$unit_new"
if grep -q '@[A-Z_]*@' "$unit_new"; then rm -f "$unit_new"; abort "unsubstituted token left in the unit template"; fi
if grep -qE '^[[:space:]]*Environment=' "$unit_new"; then rm -f "$unit_new"; abort "unit template carries an Environment= line - credentials go through EnvironmentFile only"; fi
unit_changed=no
if ! cmp -s "$unit_new" "$UNIT_PATH" 2>/dev/null; then
  install -m 644 "$unit_new" "$UNIT_PATH"
  unit_changed=yes
fi
rm -f "$unit_new"
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1 || abort "systemctl enable $SERVICE failed"
if [ "$(systemctl is-active "$SERVICE" 2>/dev/null || true)" = "active" ]; then
  log "$SERVICE is already active - NOT restarting it (a restart kills in-flight runs)"
  [ "$unit_changed" = no ] || log "NOTE: the unit file changed; it takes effect at the next restart"
else
  systemctl start "$SERVICE"
fi

# --- 8. verify ---------------------------------------------------------------
state=""
for _ in $(seq 1 30); do
  state="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
  [ "$state" = "active" ] && break
  sleep 2
done
[ "$state" = "active" ] || abort "$SERVICE did not become active within 60s (last: $state) - journalctl -u $SERVICE -n 100"

# The token reaches curl through a config on stdin: never on a command line, never printed.
code="000"
for _ in $(seq 1 15); do
  code="$(printf 'header = "Authorization: Bearer %s"\n' "$(env_value LH_HARNESS_WEB_TOKEN)" \
    | curl -s -o /dev/null -w '%{http_code}' --max-time 10 -K - "http://127.0.0.1:$PORT/api/meta" || true)"
  [ "$code" = "200" ] && break
  sleep 2
done
[ "$code" = "200" ] || abort "GET /api/meta returned HTTP $code, expected 200 - journalctl -u $SERVICE -n 100"

log "NODE_BOOTSTRAP_OK node=$NODE_ID version=$current service=active api_meta=200 port=$PORT"

# --- 9. what an admin still has to supply (NAMES only) ----------------------
echo
echo "Still to supply in $ENV_FILE (as root: edit the file, then 'systemctl restart $SERVICE'):"
for name in LH_HARNESS_MCP_GATEWAY_KEY GH_TOKEN LH_HARNESS_FLEET_KEY LH_HARNESS_FLEET_URL \
            LH_HARNESS_FLEET_LABELS LH_HARNESS_MCP_GATEWAY_URL; do
  if [ -n "$(env_value "$name")" ]; then status="set"; else status="EMPTY"; fi
  case "$name" in
    LH_HARNESS_MCP_GATEWAY_KEY) note="admin: gateway key scoped to this node's servers" ;;
    GH_TOKEN)                   note="admin: GitHub token scoped to this node's repos" ;;
    LH_HARNESS_FLEET_KEY)       note="admin: this node's fleet device key (fleet-admin enrolment)" ;;
    LH_HARNESS_FLEET_URL)       note="not a secret: fleet-admin ingest origin (reporting is off while empty)" ;;
    LH_HARNESS_FLEET_LABELS)    note="not a secret: comma-separated key=value labels (pre-filled; required for reporting)" ;;
    LH_HARNESS_MCP_GATEWAY_URL) note="not a secret: empty = public prod gateway, 'lan' = LAN alias (optional)" ;;
  esac
  printf '  %-28s %-6s %s\n' "$name" "$status" "$note"
done
echo "Already set: LH_HARNESS_WEB_TOKEN (generated here, not shown), LH_HARNESS_FLEET_NODE=$(env_value LH_HARNESS_FLEET_NODE)."
echo "Elsewhere (admin, values never in git): the web token goes to the node's GitHub environment"
echo "secret and to the Hydra host .env; see scripts/deploy/node/README.md for the exact names."
