#!/usr/bin/env bash
# ct110_units.sh — install the overseer-sweep systemd units inside CT110.
#
# Runs INSIDE CT110 as root, invoked by the task 224 deploy pipeline's units
# stage (ct110_deploy.sh mode "units"): the runner stages this script the
# same bytes-verified way as the deploy script, and the pipeline calls
#   pct exec 110 -- bash /root/lh-deploy/ct110_units.sh
# after (or alongside) the wheel install.
#
# What it does and, just as importantly, what it NEVER does (TASK 236
# rollout rule — the tick must never start acting, or even start ticking,
# as a side effect of a deploy):
#   * installs packaging/lh-overseer-sweep.service and
#     packaging/lh-overseer-sweep.timer into /etc/systemd/system/
#   * creates /home/harness/.overseer-sweep/ (tick lock + per-tick logs)
#     owned by the harness user
#   * runs systemctl daemon-reload
#   * reports the CURRENT enablement/mode state for the deploy record
#   * NEVER enables, starts, or masks either unit; NEVER writes the
#     EnvironmentFile (operator-owned, contains secret VALUES; the file the
#     operator writes is /home/harness/.overseer-sweep-secrets.env)
#
# Idempotent: re-running on a host that already has the units (enabled or
# not) just refreshes the unit files + daemon-reload and reports state.
# Enabling the timer and flipping LH_OVERSEER_TICK_MODE are the operator's
# documented steps (docs/OVERSEER-TICK-CT110.md section 5) and deliberately
# live OUTSIDE this script.
#
# Exit codes (contract with the pipeline): 0 success, 1 unexpected error.
set -euo pipefail

DEPLOY_DIR="${LH_DEPLOY_DIR:-/root/lh-deploy}"
UNIT_DIR="/etc/systemd/system"
SWEEP_HOME="/home/harness/.overseer-sweep"
HARNESS_USER="harness"

UNITS=(lh-overseer-sweep.service lh-overseer-sweep.timer)
# The tick entrypoint + its telemetry module install under the harness home
# (self-contained, so a tick does not depend on a git checkout being present
# or current); the claude -p doctrine run itself happens in the repo
# checkout (see tick.sh's REPO_ROOT).
TICK_SCRIPTS=(tick.sh tick_notify.py)

log()  { echo "[ct110-units] $*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }

# The staged payloads travel next to the deploy script under $DEPLOY_DIR.
for unit in "${UNITS[@]}"; do
  [[ -f "$DEPLOY_DIR/$unit" ]] || die "staged unit missing: $DEPLOY_DIR/$unit"
done
for script in "${TICK_SCRIPTS[@]}"; do
  [[ -f "$DEPLOY_DIR/$script" ]] || die "staged tick script missing: $DEPLOY_DIR/$script"
done

install -m 644 -o root -g root "${DEPLOY_DIR}/lh-overseer-sweep.service" "$UNIT_DIR/"
install -m 644 -o root -g root "${DEPLOY_DIR}/lh-overseer-sweep.timer"  "$UNIT_DIR/"
log "installed ${UNITS[*]} into $UNIT_DIR"

# Tick runtime directory: entrypoint + telemetry + lock + per-tick output
# logs, writable/owned by the harness user.
install -d -m 750 -o "$HARNESS_USER" -g "$HARNESS_USER" "$SWEEP_HOME" "$SWEEP_HOME/ticks" "$SWEEP_HOME/bin"
for script in "${TICK_SCRIPTS[@]}"; do
  install -m 755 -o "$HARNESS_USER" -g "$HARNESS_USER" "$DEPLOY_DIR/$script" "$SWEEP_HOME/bin/$script"
done
log "installed tick entrypoint: $SWEEP_HOME/bin/{${TICK_SCRIPTS[*]}}"

systemctl daemon-reload
log "daemon-reload done"

# Report-only enablement/mode state: the deploy record must show whether a
# previous operator step enabled the timer or flipped the mode, and the
# deploy must never have changed it.
for unit in "${UNITS[@]}"; do
  log "state: $unit $(systemctl is-enabled "$unit" 2>/dev/null || echo not-enabled) / $(systemctl is-active "$unit" 2>/dev/null || echo inactive)"
done
mode="$(systemctl show lh-overseer-sweep.service -p Environment 2>/dev/null || true)"
log "state: service Environment: ${mode:-<unavailable>}"

log "CT110_UNITS_OK (units installed; enablement untouched)"