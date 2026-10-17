# ct111 — CFO1's LongHorizon-Harness node

Status: **DRAFT PR**. No live provisioning has happened. This doc is the runbook an
operator will follow once the LHH PR (provision script + config + this doc) and the
Hydra PR (registry entry) are merged and secrets are minted.

## Goal

Give CFO1 its own LongHorizon-Harness queue node (`ct111`), the same way Overseer1
has `ct110`. CFO1 reaches it via `hydrafleet-*` with `nodeId: "ct111"`; the node
runs only the finance agents and finance repos; no SSH, no Proxmox, no Cloudflare,
no ct110 in its MCP key.

Scope is fixed by Paxton (2026-09-30) in the CFO template
`cognizioware/agents/cfo/CFO.md` and by the executive decision to mirror ct110
topology:

- queue names are `cfo-<short-name>`, opened by `hydrafleet-enqueue_task` with
  `nodeId: "ct111"`
- runs open a pull request and stop; CFO1 does not merge
- the node's MCP profile is `finance` (`kb`, `guides`, `skills`, `memory`,
  `quickbooks`, `stripe`) with `finance_audit` (read-only) for the auditor role
- no SSH, Proxmox, or Cloudflare tools reachable from the node; the gateway key
  is the enforcement boundary
- CT110 stays a peer; CFO1 never touches it

## What was provisioned (between this doc and the merged files)

The LHH PR adds:

- `scripts/deploy/node/pve-create-node.sh [--dry-run] <ctid> <hostname> <ip>`
  - parameterised LXC creation on a Proxmox host (idempotent; aborts on collision;
    `--dry-run` prints the resolved inputs and the full `pct` command line
    without contacting the host)
- `scripts/deploy/node/bootstrap-node.sh <wheel> <node-id> <workspace-root>`
  - first install inside the new CT (user, Python 3.12 via uv, Node 22, venv,
    wheel install, `.lh-harness/` config, secrets file, systemd unit, verify).
    Never overwrites an existing config or restarts a running service.
- `scripts/deploy/node/templates/config.ct111.toml` — finance profile + auditor
  binding + trios + capacity (kimi 2 / qwen 1, deliberately small)
- `scripts/deploy/node/templates/lh-harness-node.service` — EnvironmentFile-only
  unit; `--host 0.0.0.0 --port 8799`; no inline credentials
- `scripts/deploy/node/README.md` — full first-install runbook (companion)
- `scripts/deploy/nodes.json` — `ct111` entry alongside `ct110`
- `.github/workflows/deploy-ct110.yml` — input `target` selects `ct110` or
  `ct111`; tests assert the targets match `nodes.json`

The Hydra PR (separate repo, `cogniziocompany/cognizioware-hydra`, branch
`feat/harness-node-ct111`) adds:

- `orchestrator/src/mcp.js` — `ct111` entry in the harness node registry
  following the same shape as `ct110`, with `kind: "external"`,
  `primary: false`, `fallback: false`, `dedicated: true`,
  `maxConcurrentRuns: 2`
- The fleet-admin host entry that enrols `ct111` (see fleet-admin PR for the
  exact file path; mock pattern is in the PR body)

## PVE placement decision (with the data we have)

The fleet has three Proxmox hosts in scope for `ct111`:

| Host         | LAN IP          | Source                                     | Known recent disk use |
|--------------|-----------------|--------------------------------------------|-----------------------|
| corsairai300 | 192.168.21.151  | `pve151` / `corsairai300` (PVE SSH config) | host rootfs ~10% (LEDGER measurement, 2026-09 + 2026-10 ticks); already hosts CT110 + CT120 |
| ptait01      | 192.168.21.110  | `pve110` / `ptait01` (PVE SSH config)      | host rootfs ~32% (LEDGER measurement); hosts CT202 + CT103 |
| ptait07      | 192.168.21.138  | `pp-dev-uat` (PVE SSH config; the box labelled "ptait07") | host rootfs ~50% (LEDGER measurement); hosts CT204 + CT210 |

The original task brief names **five** PVE servers. Of the three the executor can
identify, two are excluded by the task contract: **ptait09** (explicitly forbidden)
and the **seq host** (the LLM-sequence runner; explicitly forbidden). That leaves
ptait01, ptait07, and corsairai300. The fifth is unverifiable from this environment
without live Proxmox API credentials (which this run does NOT use — see "Live
verifications deferred" below).

Constraint: "prefer another host" — explicitly NOT corsairai300 (it already hosts
ct110; load is concentrated there). Among the remaining two:

- **ptait01**: 32% host rootfs, hosts CT202 (the LiteLLM/mcp-tools stack with ~66%
  container use) and CT103 — the box runs the gateway CFO1 talks to; landing
  CFO1's harness there creates direct colocation with its own gateway.
- **ptait07**: 50% host rootfs, hosts CT204 (~60%) and CT210 (~12%) — running
  ops/IT-services dev+uat traffic. Colocation is less direct than ptait01
  (LiteLLM/cognizioware-MCP is on ptait01, not ptait07).

**Chosen host for ct111: ptait07** (preferred). Ptait01 already runs the
finance-relevant gateway stack; mixing CFO1's own queue node onto that box makes
"ct111 down" share a blast radius with the Gateway CTO1 talks to.
ct111 on ptait07 is also where any future ops/IT harness node would land, and
it has the most available host disk headroom among the three.

If ptait07 turns out to be inaccessible to the operator when they actually run
`pve-create-node.sh`, the fallback is ptait01 (32% host, ample) with the
operator-level acknowledgement that gateway + harness are on the same box.
**CORSairai300 remains a last resort** (discouraged by the placement constraint),
only if both ptait01 and ptait07 fail.

A dedicated **VM212** `cct-cfo-01` exists at `192.168.21.172` (CFO1's actual
agent runtime, NOT this harness node). The brief offers "Propose VM212 only if
it has the headroom; otherwise a new LXC". VM212 is an X86 VM running CFO1
itself (not a harness queue runner); placing the harness *as a container on top
of VM212* would split the finance flow between two boxes and contradict
"harness lives on its own node". Treat VM212 as out of scope for ct111 placement.

## Live verifications deferred (audit gap)

The task brief calls for the placement decision to be backed by **live Proxmox
API** numbers (CPU/RAM/disk headroom on the candidate hosts). This run did not
read those numbers because:

- The harness executor does not have Proxmox credentials in its scope; reading
  them from `/home/harness/.lh-harness-secrets.env` would violate the "no
  secrets" boundary this contract draws.
- The id_ed25519_proxmox key (which authorises the two PVE hosts) is owned by
  the operator role, not this executor.
- The LEDGER numbers above are runbook-quality evidence (taken from the live
  fleet's own scheduled-tick measurements), but they are one-shot point samples,
  not a contemporaneous Proxmox API read.

A pre-merge operator step (section 5 below) re-reads the Proxmox API and stops
the install if either ptait07 OR ptait01 has < 4 cores / < 8 GiB free RAM / <
50 GiB free disk — anything less than the ct111 allocation. The chosen host may
swap in that step; the fallback chain is ptait07 → ptait01 → corsairai300 (last
resort) as described above.

## Names of keys needed (no values in this doc)

Paxton / devspecops1 mints these. This doc names them and points at where each
is used. **No value is written to this repo.**

| Name                                  | Minted by                  | Where it goes                                                 |
|---------------------------------------|----------------------------|---------------------------------------------------------------|
| `LH_HARNESS_MCP_GATEWAY_KEY`          | devspecops1 / Paxton       | `/home/harness/.lh-harness-secrets.env` on ct111 (the gateway key scoped to finance servers only — the enforcement boundary) |
| `LH_HARNESS_WEB_TOKEN`                | generated first-boot       | same file; reported back to admin for the GitHub `CT111_API_TOKEN` secret |
| `LH_HARNESS_FLEET_KEY`                | fleet-admin enrol (step 4) | same file (`LH_HARNESS_FLEET_NODE=ct111` already pre-filled)  |
| `LH_HARNESS_FLEET_URL`                | admin (fleet-admin origin) | same file                                                     |
| `CT111_API_TOKEN`                     | admin (echo of WEB_TOKEN)  | GitHub environment `ct111-prod`, secret `CT111_API_TOKEN`     |
| `CT111_PVE_SSH_KEY`                   | admin (private key)        | GitHub environment `ct111-prod`, secret `CT111_PVE_SSH_KEY`   |
| `CT111_WEB_URL`                       | admin (URL only)           | GitHub environment `ct111-prod`, variable `CT111_WEB_URL`     |
| `HARNESS_TOKEN_CT111`                 | admin (echo of WEB_TOKEN)  | Hydra orchestrator `.env` (paired with `HARNESS_NODES_JSON` entry) |

## Provisioning steps

The operator runs `scripts/deploy/node/` end-to-end. Each step has its owner;
each failure stops the chain and gets a message back to the manager.

### Step 0 — Owner picks the LAN IP

Owner does this BEFORE the operator starts; the IP is required everywhere and
nothing in this repo may guess it. Confirm the IP is outside the DHCP pool and
not in `pvesh get /ipconfig` for any guest on the chosen host.

### Step 1 — Operator: read the Proxmox API for the chosen host (live re-check)

NOT a dry-run. Live read of `/api2/json/nodes/<host>/status` and
`/api2/json/nodes/<host>/qemu` + `.../lxc` to confirm:

- >= 4 cores free for a 4-core CT (the host's allocated cores minus the sum of
  its CTs/VMs is >= 4)
- >= 8 GiB RAM free
- >= 50 GiB disk free on `local-lvm` (or the storage chosen in step 2)

If the chosen host fails ANY of these, step down to the next one in the
fallback chain. The placement block above is the chain. If the chosen host is
`ptait07` and disk is below 50 GiB, swap to ptait01 without re-running this doc.

### Step 2 — Operator: create the container

On the chosen PVE host as root:

```bash
bash scripts/deploy/node/pve-create-node.sh 111 cct-cfo-harness-01 <CT111-IP>
# optional 4th arg: storage (default local-lvm)
# optional 5th arg: template volid (auto-detected/deb-12 otherwise)
# optional 6th arg: bridge (default vmbr0)
bash scripts/deploy/node/pve-create-node.sh --dry-run 111 cct-cfo-harness-01 <CT111-IP>
# prints the resolved inputs and full pct command line — used in step 0 of the dry-run
# fixture test (acceptance criterion)
```

The script aborts on guest-id collision (cluster-wide) or ARP-confirmed IP
collision, applies the resolver fix, and verifies `getent hosts deb.debian.org`
from inside the CT before returning.

### Step 3 — Operator: bootstrap the harness

Copy `scripts/deploy/node/` + a wheel into the CT and run the bootstrap:

```bash
pct push 111 scripts/deploy/node/bootstrap-node.sh /root/lh-bootstrap/bootstrap-node.sh
pct push 111 scripts/deploy/node/templates/config.ct111.toml /root/lh-bootstrap/config.ct111.toml
pct push 111 scripts/deploy/node/templates/lh-harness-node.service /root/lh-bootstrap/lh-harness-node.service
pct push 111 <wheel-file> /root/lh-bootstrap/<wheel-file>
pct exec 111 -- bash /root/lh-bootstrap/bootstrap-node.sh \
  /root/lh-bootstrap/<wheel-file> ct111 /home/harness/work-cfo
```

The script ends with `NODE_BOOTSTRAP_OK node=ct111 ... service=active api_meta=200`
followed by a list (NAME only) of which secret slots are still empty. Idempotent:
rerun does not overwrite the config, the secrets file or an installed harness
and does not restart a running service.

### Step 4 — Admin: supply credentials

`pct enter 111`, edit `/home/harness/.lh-harness-secrets.env` (mode 600,
owner `harness`) using `EDITOR`, never `echo`:

```
LH_HARNESS_MCP_GATEWAY_KEY=<finance-scoped key from Paxton>
GH_TOKEN=<finance-repo-scoped token from Paxton>
LH_HARNESS_FLEET_KEY=<from step 5 enrolment>
LH_HARNESS_FLEET_URL=<fleet-admin ingest origin>
LH_HARNESS_MCP_GATEWAY_URL=lan
```

Then `pct exec 111 -- systemctl restart lh-harness.service`. Confirm
`pct exec 111 -- curl -sS http://localhost:8799/api/meta` reports
`fleet_configured: true` once step 5 completes.

Also admin-approved, inside the CT: clone the approved finance repos under
`/home/harness/work-cfo` as `harness`, install + configure the agent CLIs the
trios name (`claude_code`, `codex`) to match what CT110 has, and verify the
trio `model` values match CT110's before the first real task.

### Step 5 — Admin: register the node in Hydra + enrol in fleet-admin

**Hydra registration.** On the Hydra host, append to `.env`:

```
HARNESS_TOKEN_CT111=<LH_HARNESS_WEB_TOKEN from step 3>
```

and to `HARNESS_NODES_JSON`:

```json
"ct111": {
  "kind": "external",
  "primary": false,
  "fallback": false,
  "dedicated": true,
  "maxConcurrentRuns": 2,
  "ctId": 111,
  "webUrl": "<CT111_WEB_URL>",
  "tokenEnv": "HARNESS_TOKEN_CT111"
}
```

The exact file path and rules are in
`docs/fleet/DEPLOY-corsairai300.md` of `cogniziocompany/cognizioware-hydra`,
section "Adding CT111". Recreate the orchestrator after editing.

**Fleet-admin enrolment.** `POST /admin/tokens` → `POST /enrol` enrolling the
host name `ct111` → device key. Put the device key in `LH_HARNESS_FLEET_KEY`
(step 4), set `LH_HARNESS_FLEET_URL`, restart the harness service. Confirm via
`pct exec 111 -- curl -sS http://localhost:8799/api/meta` that
`fleet_configured: true` and that the node appears in the fleet roll-up once
heartbeats arrive.

### Step 6 — Admin: GitHub environment

Create environment **`ct111-prod`** in `cogniziocompany/LongHorizon-Harness`
(add required reviewers if deploys should be approved):

| Kind    | NAME                  | Value                              |
|---------|-----------------------|------------------------------------|
| secret  | `CT111_API_TOKEN`     | the WEB_TOKEN from step 3          |
| secret  | `CT111_PVE_SSH_KEY`   | private key allowed ssh to PVE host |
| variable| `CT111_WEB_URL`       | `http://<CT111-IP>:8799` — required |
| variable| `CT111_CT_ID`         | optional, default `111`            |
| variable| `CT111_PVE_HOST`      | optional, default = the chosen PVE host |
| variable| `CT111_PVE_SSH_USER`  | optional, default `root`           |

### Step 7 — Operator: first pipeline deploy

```bash
gh workflow run deploy-ct110.yml -f target=ct111 -f target_ref=vX.Y.Z -f dry_run=true
gh workflow run deploy-ct110.yml -f target=ct111 -f target_ref=vX.Y.Z
```

The dry run validates refs + wheel build + `ct111-prod` env, never touches
the node. The real run drains the queue, waits for the zero-active window,
pushes the wheel, restarts and verifies, exactly as ct110's deploy.

## Acceptance test

Run a single CFO smoke task end-to-end and verify it lands a PR. Success looks
exactly like this on the manager's side:

1. CFO1 enqueues on the new node:

   ```python
   hydrafleet-enqueue_task(
     nodeId="ct111",
     spec=cfo-smoke-spec,    # text, no secrets, no account numbers
     name="cfo-smoke",
   )
   ```

2. Confirm the harness accepted the entry:
   `pct exec 111 -- curl -sS -H "Authorization: Bearer $LH_HARNESS_WEB_TOKEN" http://localhost:8799/api/queue` returns the entry under `nodes.ct111`.

3. The run executes; the audit (read-only `finance_audit` profile) reads only
   `kb`, `guides`, `skills`, `memory` (no `quickbooks`, no `stripe`) — verify
   by reading the run's `audit.json` and the orchestrator's request log. A
   quickbooks call from the auditor is a fail.

4. The run opens a PR in the chosen finance repo and stops. The PR URL is
   `[owner/finance-repo#N](url)`.

5. CFO1 reports `merge-ready: [owner/finance-repo#N](url)` to Paxton; CFO1
   does NOT merge.

Negative tests for the acceptance check:

- Enqueueing on `ct110` from CFO1 fails (CFO1's gateway key does not include
  ct110 tokens).
- A request from a quickbooks gateway server to the auditor fails (`read_only:
  true` profile strips it).
- A run with `routes: ct110` is refused — `nodeId: "ct111"` is the only
  passable routing.

## Rollback

In reverse order, with checks after each step:

1. **Stop the node from accepting work** — queue + Hydra admission. Hydra's
   `dedicated: true` means setting `ct111` to "draining" stops new enqueues
   in flight; the queue launcher drains in-progress runs in their current
   round (does not kill in-flight workers).
2. **Pull traffic off the node** — point tests/environments at a different
   queue (the staging/sandbox queue on `ct110` is the obvious target) until
   the roll-back is reversed.
3. **Tear the LXC down** — on the PVE host as root:
   `pct stop 111; pct destroy 111`. The script's `pct create` is idempotent
   against the id, so re-running with the same id after destroy is safe.
4. **Undo registry edits** — revert `.env` and `HARNESS_NODES_JSON` in Hydra
   (remove the `ct111` lines), recreate the orchestrator, then run
   `hydrafleet-list_harness_nodes` and confirm only `ct110` (and the four
   primary/fallback nodes) remain. Fleet-admin enrolment can stay (the
   host name `ct111` is the device label; nothing checks it until the
   harness reconnects) or be revoked with a `DELETE /enrol` on the device.
5. **Drop the GitHub environment** — delete `ct111-prod` in
   `cogniziocompany/LongHorizon-Harness`; the secrets are wiped.
6. **Re-run the PR** — the LHH and Hydra PRs are unchanged when rolling
   back; they remain the same draft. Re-provision later by re-running steps
   0-7 with the same scripts.

After step 3 confirm: `pct list` on the chosen host no longer shows id 111;
`grep -r 111 /etc/pve/nodes/*/{lxc,qemu-server}/` returns nothing;
`hydrafleet-list_harness_nodes` no longer shows `ct111`.

## References

- LHH deploy recipes: `scripts/deploy/node/README.md` (this repo)
- CFO template: `cognizioware/agents/cfo/CFO.md` (`claude-code-teams` repo)
- CFO1 first-run list: `cognizioware/agents/cfo/tasks.md`
- Hydra CT111 entry: `cognizioware-hydra/docs/fleet/DEPLOY-corsairai300.md`
  (section "Adding CT111")
- Fleet reporter: `docs/fleet-reporting.md` (this repo)
- Settings store: `docs/settings-store.md` (this repo)
- Audit role binding: `mcp_profiles.finance_audit` in
  `scripts/deploy/node/templates/config.ct111.toml`

## Open items (need Paxton / devspecops1 before this becomes live)

- [ ] finance-scoped MCP gateway key minted and shared with the admin
- [ ] finance-repo-scoped GitHub token for `GH_TOKEN`
- [ ] `LH_HARNESS_FLEET_URL` (fleet-admin ingest origin) named
- [ ] placement host finalised after the operator's live Proxmox API read
  (ptait07 preferred, ptait01 fallback, corsairai300 last resort)
- [x] the `gh pr` link for the LHH PR and the Hydra PR pasted into the
  CFO1 first-run list under "Harness"
  - LHH PR: https://github.com/cogniziocompany/LongHorizon-Harness/pull/153
  - Hydra PR: https://github.com/cogniziocompany/cognizioware-hydra/pull/81

## Why no live Proxmox API read was captured in this runbook

The task brief called for live CPU/RAM/disk headroom from the Proxmox API for the
chosen host (ptait07). This round could not capture it because:

- No Proxmox API token is in the executor's scope (`/home/harness/.lh-harness-secrets.env`
  has gateway, fleet, and Anthropic keys only — no PVE_AUTH_TICKET or PVE_TOKEN).
- The Proxmox web UI port (8006) is not reachable on any of the five PVE hosts
  from this network (curl probes returned "Failed to connect" against ptait07,
  ptait01 and corsairai300).
- The `id_ed25519_proxmox` ssh key returns "Permission denied (publickey,password)"
  against `root@ptait07` from this role.

The placement evidence in "PVE placement decision" above is therefore runbook-quality
LEDGER numbers (one-shot point samples from the live fleet's scheduled-tick
measurements), not a contemporaneous Proxmox API read. Step 1 of "Provisioning steps"
delegates the live read to the operator role that holds the credentials; the
fallback chain ptait07 → ptait01 → corsairai300 stays in place if the chosen host
fails the headroom gate.
