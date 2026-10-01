# Adding a LongHorizon-Harness node (first install)

`scripts/deploy/ct110/` upgrades a node that already exists. This directory is the missing
first half: creating the container and installing the harness into the layout the deploy
pipeline assumes, so the node can then be deployed by the same workflow
(`.github/workflows/deploy-ct110.yml`, input `target`).

It was written for **CT111** — nodeId `ct111`, LXC 111 `cct-cfo-harness-01` on `corsairai300`,
the finance-only node for the agent CFO1 — and the examples use it. The node inventory is
`scripts/deploy/nodes.json`.

No secret value appears in this repo. Everything below refers to credentials by NAME.

| File | Runs where | What it does |
| --- | --- | --- |
| `pve-create-node.sh` | PVE host, as root | `pct create` an unprivileged Debian 12 CT with CT110's sizing, resolver fix, onboot, start |
| `bootstrap-node.sh` | inside the new CT, as root | user, runtime, venv + wheel, directories, config, secrets file, systemd unit, verify |
| `templates/lh-harness-node.service` | — | unit template (EnvironmentFile only, LAN bind, workspace root) |
| `templates/config.ct111.toml` | — | CT111's `config.toml`: `finance` MCP profile, trios, capacity, runs root |

## What makes CT111 different from CT110

| | CT110 | CT111 |
| --- | --- | --- |
| Purpose | general work, all repos | finance work only |
| Workspace root | `/home/harness/work` | `/home/harness/work-cfo` (only repos the owner approves) |
| Gateway key (`LH_HARNESS_MCP_GATEWAY_KEY`) | its own | its own, finance-scoped |
| GitHub token (`GH_TOKEN`) | all repos | its own, finance repos only — never CT110's token |
| Fleet key (`LH_HARNESS_FLEET_KEY`) | its own | its own device key |
| MCP profile | built-in profiles | `finance` (kb, guides, skills, memory, quickbooks, stripe) + read-only `finance_audit` for the auditor |
| Queue capacity | harness defaults (kimi 3 / qwen 1) unless its config says otherwise | kimi 2 / qwen 1 |
| Overseer-sweep units | installed by the pipeline | not installed (the `overseer-units` job is skipped) |
| Hydra | `primary` + `fallback` | `dedicated: true`, never primary/fallback; callers pass `nodeId: "ct111"` |
| Web token in the unit file | inline `Environment=` (known hygiene issue) | `EnvironmentFile` only |

## Runbook

Roles: **operator** = whoever runs the infrastructure steps; **admin/owner** = the person who
may mint and handle credentials. An operator must not be asked to create, read or copy a
credential value.

### 0. Decide the address — owner

Pick a free LAN IP for the node. It is a required argument everywhere and is written nowhere
in this repo (`nodes.json` holds only the variable NAME `CT111_WEB_URL`).

### 1. Create the container — operator, on the PVE host as root

Copy this directory to the PVE host (for example `scp -r scripts/deploy/node
root@corsairai300:/root/lh-node-bootstrap`), then:

```bash
bash /root/lh-node-bootstrap/pve-create-node.sh 111 cct-cfo-harness-01 <CT111-IP>
# optional: [storage] [template-volid] [bridge]; see the script header for LH_NODE_* overrides
```

It aborts if guest id 111 exists anywhere in the cluster or if the IP answers ARP, applies the
resolver fix (`--nameserver 192.168.21.3 --searchdomain lan.easybutt0n.ai`), and checks name
resolution from inside the CT before returning.

### 2. Bootstrap the harness — operator, on the PVE host as root

Get a wheel **that carries the Web bundle** (a wheel built without the frontend build step
ships no UI). In order of preference:

1. CT110's rollback archive, on the same PVE host — the build CT110 is running:
   `pct exec 110 -- ls /home/harness/deploy/wheels/` then
   `pct pull 110 /home/harness/deploy/wheels/<wheel> /root/lh-node-bootstrap/<wheel>`.
2. The wheel artifact of a pipeline run (`gh run download <run-id> -n ct110-wheel`; kept 30 days).
3. Build it the way the workflow's `build` job does (frontend `npm ci` + `npm run build`, then
   `uv build --wheel`).

Push the files in and run the bootstrap:

```bash
cd /root/lh-node-bootstrap
pct exec 111 -- mkdir -p /root/lh-bootstrap/templates
for f in bootstrap-node.sh templates/config.ct111.toml templates/lh-harness-node.service <wheel>; do
  pct push 111 "$f" "/root/lh-bootstrap/$f"
done
pct exec 111 -- bash /root/lh-bootstrap/bootstrap-node.sh \
  /root/lh-bootstrap/<wheel> ct111 /home/harness/work-cfo
```

Success ends with `NODE_BOOTSTRAP_OK node=ct111 ... service=active api_meta=200` and a list, by
NAME, of what is still empty. The script is idempotent: re-running it never overwrites the
config, the secrets file or an installed harness, and never restarts a running service.

What it leaves behind:

- user `harness`; root-owned venv `/home/harness/venv` (Python 3.12) with the wheel installed
  non-editable; Node 22, git, gh;
- `/home/harness/work-cfo` (empty), `/var/lib/lh-harness/runs`;
- `/home/harness/deploy/wheels/<wheel>` and `/home/harness/deploy/state/previous_version`, so
  the first pipeline deploy has a recorded version and an archived wheel;
- `/home/harness/node/.lh-harness/config.toml` (service working directory) and
  `/home/harness/.lh-harness/mcp_profiles.json` (the same profiles, visible to workers whose
  working directory is a workspace);
- `/home/harness/.lh-harness-secrets.env`, mode 600, owner `harness`: a freshly generated
  `LH_HARNESS_WEB_TOKEN` (never printed), `LH_HARNESS_FLEET_NODE=ct111`,
  `LH_HARNESS_FLEET_LABELS=kind=ct111,host=cct-cfo-harness-01`, and empty placeholders for the
  rest;
- `/etc/systemd/system/lh-harness.service`: `EnvironmentFile` only, `--host 0.0.0.0 --port 8799`,
  `--workspace-root /home/harness/work-cfo`, enabled and started.

### 3. Supply the node's credentials — admin/owner, inside the CT

`pct enter 111`, edit `/home/harness/.lh-harness-secrets.env` with an editor (not `echo`, so no
value lands in shell history), then `systemctl restart lh-harness.service`.

| NAME | Secret? | Value |
| --- | --- | --- |
| `LH_HARNESS_MCP_GATEWAY_KEY` | yes | a gateway (LiteLLM) key created for CT111, scoped to the finance servers |
| `GH_TOKEN` | yes | a GitHub token scoped to the finance repos only |
| `LH_HARNESS_FLEET_KEY` | yes | CT111's own fleet device key (step 5) |
| `LH_HARNESS_FLEET_URL` | no | fleet-admin ingest origin |
| `LH_HARNESS_MCP_GATEWAY_URL` | no | empty = public prod gateway, `lan` = LAN alias |
| `LH_HARNESS_FLEET_NODE` | no | already `ct111` — must equal the host name enrolled in fleet-admin; do not change it to the container hostname |
| `LH_HARNESS_FLEET_LABELS` | no | pre-filled; the reporter needs it non-empty |

The gateway key is the enforcement boundary for which MCP servers CT111 can reach. The `finance`
profile only selects what the harness asks for; a key that can reach more than the finance
servers makes the profile cosmetic.

Still inside the CT, also owner-approved:

- clone the approved finance repos under `/home/harness/work-cfo` as user `harness`;
- install and configure the agent CLIs the trios in `config.toml` name (`claude_code`, `codex`)
  the way CT110 has them (`pct exec 110 -- npm ls -g --depth=0` shows what is installed there),
  and confirm the trio `model` values match what CT110 runs. The bootstrap does not install or
  configure the agents.

### 4. Register the node in Hydra — admin for the token, operator for the rest

On the Hydra host, add `HARNESS_TOKEN_CT111` (CT111's `LH_HARNESS_WEB_TOKEN`) to the hand-managed
`.env` and append the `ct111` entry to `HARNESS_NODES_JSON`, then recreate the orchestrator.
The exact entry and rules are in `docs/fleet/DEPLOY-corsairai300.md` of `cognizioware-hydra`
(section "Adding CT111"): `kind: external`, `primary: false`, `fallback: false`,
`dedicated: true`, `maxConcurrentRuns: 2`. Callers always pass `nodeId: "ct111"`.

### 5. Enrol the node in fleet-admin — admin

`POST /admin/tokens` → `POST /enroll` → device key, enrolling the host name `ct111`. Put the key
in `LH_HARNESS_FLEET_KEY` and the ingest origin in `LH_HARNESS_FLEET_URL` (step 3), restart the
service, and confirm `GET /api/meta` reports `fleet_configured: true` and the node appears in
the roll-up once heartbeats arrive.

### 6. GitHub environment — admin/owner

Create environment **`ct111-prod`** in this repo (add required reviewers if deploys should be
approved) with:

| Kind | NAME | Value |
| --- | --- | --- |
| secret | `CT111_API_TOKEN` | CT111's `LH_HARNESS_WEB_TOKEN` |
| secret | `CT111_PVE_SSH_KEY` | private key allowed to ssh to the PVE host (environment secrets are per environment, so it is set here even when it is the same identity as CT110's) |
| variable | `CT111_WEB_URL` | `http://<CT111-IP>:8799` — **required**, the workflow has no default |
| variable | `CT111_CT_ID` | optional, default `111` |
| variable | `CT111_PVE_HOST` | optional, default `corsairai300` |
| variable | `CT111_PVE_SSH_USER` | optional, default `root` |

### 7. First pipeline deploy — operator

```bash
gh workflow run deploy-ct110.yml -f target=ct111 -f target_ref=vX.Y.Z -f dry_run=true
gh workflow run deploy-ct110.yml -f target=ct111 -f target_ref=vX.Y.Z
```

The dry run checks the ref, builds the wheel and validates that the `ct111-prod` environment
has its URL and both secrets; it never touches the node. The real run drains the queue, waits
for a zero-active window, pushes the wheel through the PVE host, restarts and verifies, exactly
as for CT110. Omitting `target` deploys CT110, as before.

## How the `finance` profile is applied (and where it is not)

- `config.toml` defines `[run.mcp_profiles.finance]` and a read-only
  `[run.mcp_profiles.finance_audit]`, binds `[run.roles.auditor]` to the latter, and sets both
  trios to `mcp_profile = "finance"`.
- The auditor binding is required: the launcher refuses every queue entry whose auditor would
  resolve to a non-read-only profile.
- **Queue launches do not pass the trio profile to the worker** (`launcher._role_configs`
  strips it; it is only used for that eligibility check). A run started through `POST /api/runs`
  gets the profile only if the caller sends `mcp_profile: "finance"` together with
  `roles.auditor.mcp_profile: "finance_audit"`. Until the worker round-trips the trio profile,
  the scoping that actually holds on this node is the finance-scoped gateway key.

## Known gaps

- **fleet-admin poller is ct110-only** until plan "L0" (`feat/fleet-harness-rollup`) lands:
  CT111 appears in the roll-up from its own heartbeats but is not reconciled by the poller.
- **Hydra's node registry is in memory**, seeded from `HARNESS_NODES_JSON` at orchestrator
  start; registering CT111 needs the env entry and an orchestrator restart.
- **Reporter `uiBaseUrl` is empty** until "L2", so fleet deep links to CT111 runs do not resolve.
- **Trio names are fixed** (`kimi`, `qwen`) by `config.py`; CT111 reuses them.
- **Trio `mcp_profile` is not applied to queue-launched workers** (above).
- **Log markers** in the workflow and deploy scripts read `CT110_*` for both targets; the
  deployment record names the target node.
- **Rollback wheel glob**: on `main`, `ct110_deploy.sh rollback` looks for
  `lh_harness-<version>-*-py3-none-any.whl`, which does not match a plain
  `lh_harness-<version>-py3-none-any.whl`; PR #74 fixes the glob. Until it merges, an automatic
  rollback of a plain-version wheel fails loudly on either node even though the archive is seeded.
- **DEPLOY-HOLD** on CT111 is `/home/harness/DEPLOY-HOLD` (there is no repo checkout at the
  CT110 path).

## Not verified against the live hosts

- The Debian 12 template name and the storage/bridge names on `corsairai300` (the template is
  auto-detected or downloaded; storage defaults to `local-lvm`, bridge to `vmbr0`).
- Whether CT110 has `nesting` enabled (`pct config 110`); the script defaults to `nesting=1`.
- CT110 itself is documented as Ubuntu 24.04; this node is Debian 12 with a uv-provided
  Python 3.12. To match CT110 instead, pass an Ubuntu 24.04 template volid as the 5th argument
  of `pve-create-node.sh` — `bootstrap-node.sh` then uses the system `python3.12`.
- That the gateway exposes servers under the aliases `quickbooks` and `stripe` exactly as
  written in the `finance` profile.
- The bootstrap was exercised end to end in a Debian 12 container with a stand-in for
  `systemctl`; it has not run under real systemd in an LXC.
