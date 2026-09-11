#!/usr/bin/env bash
# lh-harness-node entrypoint.
#
# Seeds a fleet-default .lh-harness/config.toml at the workspace root when one
# is absent (a per-repo .lh-harness/config.toml inside a checkout still wins —
# the harness loads config relative to the worker's cwd), then serves the web
# control API. The server refuses a non-loopback bind without a token, so
# LH_HARNESS_WEB_TOKEN is mandatory.
set -euo pipefail

: "${LH_HARNESS_WEB_TOKEN:?LH_HARNESS_WEB_TOKEN is required (server refuses 0.0.0.0 without a bearer token)}"

WORKSPACE_ROOT="${LH_NODE_WORKSPACE_ROOT:-/home/harness/work}"
RUNS_ROOT="${LH_NODE_RUNS_ROOT:-/home/harness/runs}"
PORT="${LH_NODE_PORT:-8799}"

mkdir -p "$WORKSPACE_ROOT" "$RUNS_ROOT"

CONFIG_DIR="$WORKSPACE_ROOT/.lh-harness"
CONFIG_FILE="$CONFIG_DIR/config.toml"

# Fleet defaults mirror the known-good CT110 values (cognizioware-how-to.md,
# TEMPLATE-litellm-qwen-local.md): manager 300 / executors 5400 / auditor 600,
# guard excludes for JS build dirs. All overridable via env at install time.
# The helper reuses the harness's own CONFIG_TEMPLATE so the emitted file never
# contains invented keys and always carries all eight [run.roles.<role>] blocks.
if [[ ! -f "$CONFIG_FILE" ]]; then
    mkdir -p "$CONFIG_DIR"
    python3 /usr/local/bin/lh-node-materialize-config --config-file "$CONFIG_FILE"
fi

# Fleet reporting is configured entirely from the environment.  When unset,
# the web server starts without the reporter and behaves as before.
export LH_HARNESS_FLEET_URL="${LH_HARNESS_FLEET_URL:-}"
export LH_HARNESS_FLEET_NODE="${LH_HARNESS_FLEET_NODE:-}"
export LH_HARNESS_FLEET_KEY="${LH_HARNESS_FLEET_KEY:-}"
export LH_HARNESS_FLEET_LABELS="${LH_HARNESS_FLEET_LABELS:-}"

exec lh-harness web \
    --host 0.0.0.0 \
    --port "$PORT" \
    --runs-root "$RUNS_ROOT" \
    --workspace-root "$WORKSPACE_ROOT" \
    --no-open \
    --auth-token "$LH_HARNESS_WEB_TOKEN"
