# CT111 node verification list

Post-provisioning checks for harness node **ct111** (CFO1, host `ptait07`).
Nothing here can be automated from a harness run: Proxmox provisioning and Hydra
registration are host-side steps for overseer1 (gateway ssh/proxmox tools) or a
Paxton session, outside this PR. This document is the acceptance checklist those
steps are measured against.

## 1. Provisioning (host side, on ptait07 via Proxmox)

- CT111 exists on the Proxmox node and reaches `bootstrap-node.sh <wheel> ct111 /home/harness/work-cfo` successfully; the script's last line is `NODE_BOOTSTRAP_OK node=ct111 ...`.
- `lh-harness.service` is active and `GET /api/meta` returns `200` on the node's port.

## 2. Hydra registration

- `hydrafleet-list_harness_nodes` shows `ct111` alongside `ct110` (node id, not a fallback).
- The host `.env` supplies `HARNESS_TOKEN_CT111` with ct111's own web API token — never a ct110 value.

## 3. End-to-end smoke

- Enqueue a task attributed to **CFO1** targeting node `ct111` with a read-only smoke task (e.g. `hydrafleet-list_harness_nodes` or an equivalent no-op report); the run must appear in ct111's queue and complete successfully there, never on ct110.
- Confirm ct111's runs land in ct111's own runs root (`/var/lib/lh-harness/runs`), reported under label `kind=ct111`.

## 4. Regression guard

- `ct110` is unaffected: its queue, service state and `deploy-ct110.yml` dispatch behaviour are unchanged by the ct111 registration.
- A ct111 dispatch never falls through to a ct110 credential or URL (locked by `tests/test_deploy_node_targets.py`).

## Honest limitations of this PR

- Proxmox provisioning of CT111 and Hydra registration of ct111 are **out of scope here**: this run cannot reach hosts, so those steps belong to overseer1 or a Paxton session and are only documented, not executed, by this PR.
- Settings-DB rule compliance in `bootstrap-node.sh` is claimed but not yet independently audited against `docs/settings-store.md`.
- Retention settings in `templates/settings.ct111.txt` carry the CT110-DISK values supplied by the operator for task `q-a14cd599278c4235`; a follow-up audit against the authoritative CT110-DISK source is still owed.
- No verification of the live node is possible from this run — every check above requires host-side execution.