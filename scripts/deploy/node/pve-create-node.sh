#!/usr/bin/env bash
# Create one LongHorizon-Harness node LXC on a Proxmox host (run as root on the PVE host).
#
#   pve-create-node.sh <ctid> <hostname> <ip> [storage] [template] [bridge]
#   e.g. pve-create-node.sh 111 cct-cfo-harness-01 <ip> local-lvm
#
# ctid, hostname and ip are REQUIRED and have no defaults (a node's IP is assigned per
# install; nothing in this repo may guess it).  Sizing mirrors CT110: unprivileged
# Debian 12, 4 cores, 6144 MB RAM, 512 MB swap, 50G rootfs, starts on boot.
#
# Optional positionals / environment:
#   storage   (4th arg, default local-lvm)   storage for the rootfs
#   template  (5th arg, default: newest debian-12-standard on $LH_NODE_TEMPLATE_STORAGE,
#              downloaded with pveam when absent); pass a full volid to pin one, e.g.
#              local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst
#   bridge    (6th arg, default vmbr0)
#   LH_NODE_GATEWAY (192.168.21.1)  LH_NODE_CIDR (24)
#   LH_NODE_DNS (192.168.21.3)      LH_NODE_SEARCH (lan.easybutt0n.ai)
#   LH_NODE_TEMPLATE_STORAGE (local)
#   LH_NODE_CORES (4) LH_NODE_MEMORY_MB (6144) LH_NODE_SWAP_MB (512) LH_NODE_ROOTFS_GB (50)
#   LH_NODE_NESTING (1)
#
# Gotchas this script handles:
#   - New CTs on this LAN do not resolve names until the nameserver/searchdomain are set
#     explicitly (`pct set <id> --nameserver 192.168.21.3 --searchdomain lan.easybutt0n.ai`).
#     It is applied at create time AND re-applied with `pct set`, then verified from inside.
#   - CT and VM ids share one namespace cluster-wide: the id is refused if ANY guest (LXC or
#     QEMU, on any cluster node) already owns it.
#   - An IP that answers ARP is in use even when no PVE guest claims it (DHCP lease, bare
#     metal): the script aborts rather than create a duplicate address.
#   - nesting=1: Debian 12's systemd (252) runs degraded in an unprivileged CT without it
#     (it is also what the Proxmox UI sets for unprivileged CTs).  The harness itself needs no
#     Docker-in-LXC; it only needs systemd to delegate the memory+pids cgroup controllers to
#     lh-harness.service (Delegate=memory pids, cgroup v2).  Set LH_NODE_NESTING=0 to match a
#     host where `pct config 110` shows no nesting feature.
#   - No root password and no SSH key are set: the deploy pipeline and the operator reach the
#     CT with `pct exec`/`pct enter` from the PVE host, which is the only path it needs.
set -euo pipefail

CTID="${1:?ctid (e.g. 111)}"; NAME="${2:?hostname (e.g. cct-cfo-harness-01)}"; IP="${3:?ip (IPv4, no CIDR suffix)}"
STORAGE="${4:-local-lvm}"; TEMPLATE="${5:-}"; BRIDGE="${6:-vmbr0}"
GW="${LH_NODE_GATEWAY:-192.168.21.1}"; CIDR="${LH_NODE_CIDR:-24}"
DNS="${LH_NODE_DNS:-192.168.21.3}"; SEARCH="${LH_NODE_SEARCH:-lan.easybutt0n.ai}"
TEMPLATE_STORAGE="${LH_NODE_TEMPLATE_STORAGE:-local}"
CORES="${LH_NODE_CORES:-4}"; MEMORY="${LH_NODE_MEMORY_MB:-6144}"
SWAP="${LH_NODE_SWAP_MB:-512}"; ROOTFS_GB="${LH_NODE_ROOTFS_GB:-50}"
NESTING="${LH_NODE_NESTING:-1}"

abort() { echo "ABORT: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || abort "run as root on the PVE host"
command -v pct >/dev/null 2>&1 || abort "pct not found - this is not a Proxmox VE host"
[[ "$CTID" =~ ^[1-9][0-9]{2,8}$ ]] || abort "ctid '$CTID' is not a valid guest id (100 or above)"
[[ "$NAME" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || abort "hostname '$NAME' is not a valid DNS label"
[[ "$IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || abort "ip '$IP' is not a bare IPv4 address (no /CIDR)"
[ "$IP" != "$GW" ] || abort "ip $IP is the gateway"

# The id must be free cluster-wide, for LXC and QEMU alike.
for conf in /etc/pve/nodes/*/lxc/"$CTID".conf /etc/pve/nodes/*/qemu-server/"$CTID".conf; do
  [ -e "$conf" ] && abort "guest id $CTID already exists ($conf)"
done
ip link show "$BRIDGE" >/dev/null 2>&1 || abort "bridge $BRIDGE does not exist on this host"
command -v arping >/dev/null 2>&1 || abort "arping not found (apt-get install iputils-arping) - cannot prove $IP is free"
if arping -c 2 -w 3 -I "$BRIDGE" "$IP" >/dev/null 2>&1; then abort "$IP answers ARP (in use)"; fi

# Template: an explicit volid wins; otherwise the newest debian-12-standard already on the
# template storage; otherwise download the newest one the appliance index offers.
# (`|| true`: under pipefail a failing listing must read as "none found", not kill the script.)
if [ -z "$TEMPLATE" ]; then
  TEMPLATE="$({ pveam list "$TEMPLATE_STORAGE" 2>/dev/null || true; } | awk '$1 ~ /debian-12-standard_.*_amd64/ {print $1}' | sort -V | tail -n 1)"
fi
if [ -z "$TEMPLATE" ]; then
  pveam update >/dev/null || abort "pveam update failed - cannot look up a Debian 12 template; pass a template volid as the 5th argument"
  tmpl_name="$({ pveam available --section system || true; } | awk '$2 ~ /^debian-12-standard_.*_amd64/ {print $2}' | sort -V | tail -n 1)"
  [ -n "$tmpl_name" ] || abort "no debian-12-standard template in the pveam index; pass a template volid as the 5th argument"
  echo "downloading template $tmpl_name to $TEMPLATE_STORAGE"
  pveam download "$TEMPLATE_STORAGE" "$tmpl_name" >/dev/null
  TEMPLATE="$TEMPLATE_STORAGE:vztmpl/$tmpl_name"
fi
echo "template: $TEMPLATE"

pct create "$CTID" "$TEMPLATE" --hostname "$NAME" --ostype debian --unprivileged 1 \
  --features "nesting=$NESTING" --cores "$CORES" --memory "$MEMORY" --swap "$SWAP" \
  --rootfs "$STORAGE:$ROOTFS_GB" \
  --net0 "name=eth0,bridge=$BRIDGE,ip=$IP/$CIDR,gw=$GW" \
  --nameserver "$DNS" --searchdomain "$SEARCH" --onboot 1 \
  --description "LongHorizon-Harness node ($NAME). Runbook: scripts/deploy/node/README.md in cogniziocompany/LongHorizon-Harness."
# The resolver fix, applied explicitly as well (see gotchas): idempotent when create took it.
pct set "$CTID" --nameserver "$DNS" --searchdomain "$SEARCH"
pct start "$CTID"

# Prove the resolver fix from inside before handing over to bootstrap-node.sh.
resolved=no
for _ in $(seq 1 15); do
  if pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1; then resolved=yes; break; fi
  sleep 2
done
[ "$resolved" = yes ] || echo "WARNING: CT $CTID cannot resolve deb.debian.org yet - check 'pct config $CTID' nameserver/searchdomain and the gateway before bootstrapping" >&2

echo "CT $CTID ($NAME) started at $IP (dns resolves: $resolved). Next: push scripts/deploy/node/ + a wheel into the CT and run bootstrap-node.sh as root inside it."
