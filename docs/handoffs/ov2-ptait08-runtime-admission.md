# OV2 ptait08 runtime admission — design proposal (no code, no infra change)

Written 2026-10-11. This is a **design document only**: nothing in it has been built, deployed,
keyed, or requested for real. Its purpose is to let Paxton and Overseer1 review and approve a plan
for admitting the new Proxmox node **ptait08** (VM **VM310** on it) as an execution host for
**Overseer v2**, before anyone touches a host or a gateway.

**Evidence status of every host fact in this document.** Everything said about ptait08, VM310, and
VM211 below comes from one source: the report of the v2 build session, as relayed in the task
context on 2026-10-11. None of it has been independently verified, because verifying it would
require host access, which this design round is forbidden to use. Where this document states a
fact about those hosts, read it as *"reported by the v2 build session"* — never as measured here.
Facts about repositories, PRs, and files in `cogniziocompany/LongHorizon-Harness` were checked
directly against the repo and GitHub on 2026-10-11 and are marked as such.

**The one thing that does not change.** **Overseer1 on VM211 (`cct-overseer-01`) is, and remains,
the production tick writer.** Nothing in this proposal switches, shadows, or prepares a switch of
the tick writer. Overseer v2 on ptait08 would be an *execution host* — a place where work runs —
not a second writer. Any move of writer duties would be its own task with its own approval, and is
explicitly out of scope here.

---

## 1. Execution account / profile proposal

Plain language first: Overseer v2 on ptait08 should run under the one OS account that already
exists on VM310, with the smallest set of rights that still lets it launch and supervise work, and
under a brand-new gateway identity that is scoped to exactly one workload — so it can be audited,
rate-limited, and revoked on its own, without touching any other key in the fleet.

**OS account.** Run as `cognizioware` (UID 1000) — per the v2 build session report, the only user
on VM310, with `sudo` and `docker` group membership. Creating a second, dedicated service account
is the textbook least-privilege answer, but it is a change on the host, which this round forbids
and which only Paxton can order. So the proposal is two-phase:

- **Phase 1 (this proposal):** run as `cognizioware`, confined by convention and configuration —
  a dedicated working directory for the v2 runtime, no cron/systemd units owned by other users, no
  interactive shell sessions sharing the account while a run is live.
- **Phase 2 (optional, Paxton's call, listed in section 5):** migrate to a dedicated unprivileged
  account once the runtime has proven itself, so VM310 ends up matching the separation VM211
  already has.

**What it does and does not need.**

- Needs: `docker` group membership (to start/stop/inspect its own containers) and read access to
  its own working directory and logs.
- Does not need at runtime: `sudo`. Install-time steps (package installs, systemd unit
  installation) need elevated rights once, and those steps are owned by infraops1, not by the v2
  runtime itself. After install, the runtime should run unprivileged.
- Does not need: root, host-level Proxmox tools (`pct`, `qm`), the Proxmox SSH key group, or any
  ability to reach other hosts directly. Anything host-level that the runtime genuinely needs
  should arrive later through a scoped gateway route, not from the box itself.

**Gateway identity (alias name only — no key value appears or will ever appear in this
document).** Propose a new, workload-scoped LiteLLM virtual key with the alias
**`lh-overseer-v2-ptait08`**. It should carry: a model allow-list limited to exactly the models
the v2 runtime's `config.toml` defines, MCP access groups limited to what an execution node needs,
and a tool allowlist that excludes gate resolution and fleet control unless Paxton separately
grants them. The alias name is a proposal — the final name is Paxton's call (section 5).

**How this differs from Overseer1 on VM211.**

| | Overseer1 (VM211, `cct-overseer-01`) | Overseer v2 (proposed, ptait08/VM310) |
|---|---|---|
| Role | Production **tick writer** — owns the tick ledger, gate resolution, run oversight | **Execution host** — runs work that other surfaces enqueue |
| Authority | Act-mode tick authority per the tick doctrine | Launch/supervise only; no tick writes, no gate resolution by default |
| Gateway identity | Its own existing writer-scoped key (unchanged by this proposal) | New workload-scoped key, alias `lh-overseer-v2-ptait08` (proposed) |
| Blast radius if compromised | Production control plane | One execution node; key revocable in isolation |
| Hosts it may touch | Per its own doctrine | None directly — host-level needs go via scoped gateway routes |

Exact names and paths used by this section: OS account `cognizioware` (UID 1000); proposed key
alias `lh-overseer-v2-ptait08`; node configuration file `config.toml` (trio/model definitions per
`docs/queue.md` in this repo); gateway URL variables `LH_HARNESS_MCP_GATEWAY_URL` /
`LH_HARNESS_MCP_GATEWAY_KEY` / `LH_HARNESS_MCP_GATEWAY_HEADERS_JSON` (naming convention per
`docs/SECRETS.md` and `src/lh_harness/mcp_profiles.py` — variable names only, never values).

## 2. Enrollment plan

Plain language first: admitting ptait08/VM310 means four things happening in order — the host
client gets installed, the gateway learns a new scoped identity and route for it, the node joins
the tailnet, and Overseer1 verifies the result read-only. Each step below has exactly one owner.
Steps that are decisions only Paxton can make are marked **[PAXTON DECISION]**; they block every
step beneath them until resolved.

1. **Approve this design.** Paxton accepts, amends, or rejects this document (via the PR it
   arrives in). *Owner: Paxton.* **[PAXTON DECISION]**
2. **Decide the account and key-scope questions in section 5.** The enrollment cannot start until
   the OS-account phase, key alias name, model allow-list, and MCP profile are chosen, because
   steps 3–5 all consume those answers. *Owner: Paxton.* **[PAXTON DECISION]**
3. **Install the host client on VM310.** Install the LongHorizon-Harness node runtime (the "host
   client" in the v2 build report) plus its `config.toml` defining the trios/models this node
   serves. This is an install-time `sudo` step; after it, the runtime runs unprivileged as
   `cognizioware`. *Owner: infraops1.* (Execute only after steps 1–2; blocked until then.)
4. **Create the workload-scoped gateway key.** Mint the LiteLLM virtual key under the alias chosen
   in step 2, with its model allow-list, MCP access groups, and tool allowlist exactly as approved.
   Key material goes only to the approved secret store on VM310 — never into this repo, a doc, or
   a chat. *Owner: devspecops1 (owns LiteLLM/gateway key creation and allowlists).* **[PAXTON
   DECISION on the scope before devspecops1 executes — the request text is section 3, sent only
   after Paxton approves it]**
5. **Register the gateway route / parent route.** Register the v2 runtime's route (and parent
   route, if the fleet's routing design requires one) so calls from the new key land in the right
   deployment group and spend is attributable to `lh-overseer-v2-ptait08` (or the approved
   alias). *Owner: devspecops1.* **[PAXTON DECISION if the route introduces a new parent/egress
   path — devspecops1 flags it rather than choosing]**
6. **Enroll VM310 in Tailscale.** Re-issue the login link, enroll the node, tag it, and confirm
   ACLs let it reach only what an execution node needs. Status and blockers: section 4. *Owner:
   infraops1 executes; the login-link re-issue is Paxton's.* **[PAXTON DECISION — login-link
   re-issue]**
7. **Verify admission, read-only.** Confirm from existing fleet surfaces that the node answers,
   its runs launch, and its queue entries appear — without resolving any gate and without writing
   any production tick. *Owner: Overseer1 (verification only; writer duties unchanged).*
8. **First-workload go/no-go.** Paxton decides when the first real task may run on the new host.
   *Owner: Paxton.* **[PAXTON DECISION]**

Ordering rules: steps 3–6 may interleave once steps 1–2 are resolved, but step 7 requires all of
3–6 complete, and step 8 requires step 7's read-back. Nothing in this plan merges
`cognizioware-mcp-tools` PR #481 — that PR is merge-ready and separate, and its merge is Paxton's
call (section 5, item 8).

## 3. devspecops1 request text (ready-to-send, placeholders only)

Plain language first: this is the message devspecops1 (or Overseer1 on Paxton's instruction) can
paste to request the key and route. **Every field is a placeholder. Nothing has been filled in,
and this text must not be sent until Paxton approves the scope** — sending it would constitute a
real key request, which this design round forbids.

```text
Request: workload-scoped gateway key + parent route for Overseer v2 runtime on ptait08 (VM310)

Requester: <who is sending this, e.g. Overseer1 on Paxton's approval of <PR link>>
Approver of scope: Paxton, <date of approval>

1. Key alias name: <alias, proposed: lh-overseer-v2-ptait08>
2. Workload / purpose: <one line: execution-host runtime for Overseer v2 on ptait08/VM310;
   not a tick writer; Overseer1 on VM211 remains the production tick writer>
3. Key type: LiteLLM virtual key, workload-scoped, single-workload attribution
4. Model allow-list: <exact model names the node's config.toml defines, e.g. <model-1>,
   <model-2> — nothing else>
5. Access groups: <MCP access groups an execution node needs, e.g. <group-1>, <group-2>;
   explicitly EXCLUDE gate-resolution and fleet-control groups unless Paxton grants them
   separately>
6. Tool allowlist: <explicit tool list, e.g. <tool-1>, <tool-2>; exclude <excluded-tool-1>,
   <excluded-tool-2>>
7. Parent route: <route name, or "none — direct gateway URL">
   7a. If a new parent route: egress destination <host/service>, reason <why>, and
       confirmation Paxton approved this egress path
8. Spend/rate ceiling: <budget cap and/or RPM, per fleet policy>
9. Key material delivery: <approved secret store path on VM310 only; never repo, docs, or chat>
10. Rotation/revocation owner: <owner, and the revocation runbook reference>

Not requested here (separate approvals if ever wanted): tick-writer authority, gate resolution,
Proxmox/host SSH access, any Entra approval-by-link flow (deferred by Paxton 2026-10-06;
approval stays key-based).
```

## 4. Tailscale enrollment status and plan

Plain language first: VM310 is not on the tailnet yet, and the one thing standing in the way is a
fresh Tailscale login link, which only Paxton can re-issue.

Known state (all per the v2 build session report, not independently verified):

- VM310 is **not enrolled** in Tailscale.
- A login link was previously expected but its **re-issue waits on Paxton**.
- No host client is installed and no runtime profile is admitted, so there is currently nothing on
  VM310 that would even place the enrollment call.

What is needed, in order:

1. Paxton re-issues the Tailscale login link **[PAXTON DECISION]**.
2. infraops1 runs the enrollment on VM310 and applies a tag (proposed naming: a
   `tag:execution-host`-style tag — exact tag scheme is Paxton's call, section 5, item 6).
3. devspecops1 confirms the tailnet ACL lets the new node reach only its required destinations
   (gateway, model backends per policy) and nothing broader.
4. Overseer1 verifies read-only that the node is visible on the tailnet.

Context worth carrying: this repo's `cognizioware-how-to.md` records that new LXCs inherit the PVE
host's Tailscale resolver and documents the nameserver fix (`pct set <id> --nameserver
192.168.21.3 --searchdomain lan.easybutt0n.ai`). Whether VM310 (a VM on ptait08, not an LXC) needs
an equivalent resolver adjustment is an open question — see section 5, item 7.

## 5. User decisions needed (for Paxton, via Overseer1's approval batch)

Plain language first: these are the choices only Paxton can make. Each has options and a
recommendation. Overseer1 can paste the block below into its approval batch verbatim.

> **OV2 ptait08 runtime admission — decisions needed (Paxton)**
>
> 1. **OS account for the v2 runtime.** (a) Run as `cognizioware` UID 1000 now, dedicated service
>    account later; (b) create a dedicated unprivileged account before any workload runs; (c) run
>    as `cognizioware` indefinitely. *Recommendation: (a)* — it matches the reported current state
>    of VM310, requires no host change beyond install, and keeps the separation decision open.
> 2. **Gateway key alias name.** (a) `lh-overseer-v2-ptait08`; (b) `lh-exec-ptait08`; (c) another
>    name of your choosing. *Recommendation: (a)* — it names the workload, not just the host, and
>    sorts next to the existing `lh-harness`-style aliases.
> 3. **Model allow-list for the key.** (a) Only the models the node's `config.toml` defines, added
>    one at a time as trios come online; (b) a broad allow-list now, narrowed later. 
>    *Recommendation: (a)* — this repo's queue doc records a real incident (2026-10-11) where a
>    node key missing one model produced immediate 403 launch failures; narrow-but-correct beats
>    broad-and-drifting.
> 4. **MCP profile for the runtime.** (a) `default`; (b) `ops`; (c) a new minimal profile built
>    for execution nodes. *Recommendation: (c), falling back to (a)* — `ops` carries
>    host/proxmox control the runtime should not have; if a new profile is not wanted now, start
>    at `default` and expand only on evidence of need.
> 5. **Runtime sudo posture.** (a) Install-time sudo only, runtime unprivileged (docker group
>    only); (b) keep passwordless sudo available to the runtime. *Recommendation: (a)*.
> 6. **Tailscale tag/ACL scheme for VM310.** (a) New execution-host tag with narrow ACLs; (b)
>    reuse an existing node tag. *Recommendation: (a)* — lets the ACL distinguish "runs work"
>    from "controls the fleet".
> 7. **Open questions routed here rather than decided** (from the design work):
>    - Is the "host client" of the v2 build report exactly the LongHorizon-Harness node runtime
>      (with `config.toml`), or a separate client package? The design assumes the former; if not,
>      step 3 of the enrollment plan changes.
>    - Does VM310 (a VM, not an LXC) inherit ptait08's Tailscale-side resolver settings, or does
>      it need its own nameserver/searchdomain configuration at enrollment?
>    - Should the gateway route for the v2 runtime attach to an existing parent route or a new
>      one? If new, it is a new egress path and needs its own approval (devspecops1 flagged, did
>      not decide).
>    - Confirm nothing in this admission may resolve gates or write ticks from ptait08 — the
>      design assumes yes (writer stays Overseer1 on VM211); please confirm so it is a decision,
>      not an assumption.
> 8. **`cognizioware-mcp-tools` PR #481 (ptait08 onboard, merge-ready, separate).** (a) Merge it
>    before enrollment starts; (b) merge after verification (step 7); (c) hold. *Recommendation:
>    (b)* — keeps this proposal's "no merges" rule intact while avoiding a reorder race at
>    verification time. This document's PR does not depend on it either way.

## 6. Existing surface

Plain language first: three PRs carry the v2 build evidence, one further PR is the separate
ptait08 onboard, and a graphify query found no existing ptait08 admission code anywhere — so this
proposal does not duplicate or conflict with anything.

- **`cogniziocompany/cognizioware` PR #2** (`bd6248e`) — build evidence per the task context.
- **`cogniziocompany/claude-code-teams` PR #29** (`62edc11`) — build evidence per the task context.
- **`cogniziocompany/LongHorizon-Harness` PR #127** — cited in the task context as "(draft,
  `f3338b7`)". Verified directly against GitHub on 2026-10-11: it is **OPEN**, **a draft**, head
  branch `codex/manager-parent-debrief`, head commit `f3338b7`
  ("Reflect with Manager before parent debrief and bound continuation claims"), base `main`,
  title "Add checkpoint-bound Manager parent debrief boundary". The draft/commit reference matches
  the task context; the PR's own subject is the manager-debrief work, which is how the build
  session referenced it.
- **Graphify pre-edit query (required before authoring this file).** The graphify MCP server was
  **not callable in this run**: no `graphify-query_graph` (or `graphifyuat-*`) tool was exposed to
  the session, and the gateway that hosts it
  (`litellm-gateway-api.cognizioware.com/mcp/` / LAN `http://192.168.21.161:4000/mcp/`, per
  `src/lh_harness/mcp_profiles.py`) is a host outside the workspace, which this run is forbidden
  to contact. The local-repo equivalent was performed instead: a full-tree search of
  `cogniziocompany/LongHorizon-Harness` at `origin/main` (`3d68b56`, 2026-10-11) for `ptait08`,
  `192.168.21.176`, and `VM310` returned **zero hits** — no admission code, no host references.
  The task context additionally reports graphify's finding that the only tailscale-adjacent
  configuration anywhere is the openclaw tailscale config in `openclaw-config/openclaw.json`
  (that file lives outside this repo; it does not exist in `cogniziocompany/LongHorizon-Harness`,
  confirmed locally). Net: this proposal adds new surface; it collides with nothing.
- **`cognizioware/cognizioware-mcp-tools` PR #481** — the ptait08 onboard, reported merge-ready
  and separate from this proposal; not merged, not depended on here (its timing is section 5,
  item 8).

---

*Carried constraints, restated once for the reviewer: design only; no keys created or requested
for real; no merges, deploys, auth or Conditional Access changes; no changes on ptait08, VM310,
VM211, terranas01, or any host; ptait09 off-limits; nothing deregistered; CrashPlan on terranas01
untouched; no tick-writer switch — Overseer1 on VM211 remains the production writer; no secret
values appear in this document (key alias names only); the approval-by-link (Entra app) flow is
not designed or assumed here — Paxton deferred it on 2026-10-06 and approval stays key-based.*
