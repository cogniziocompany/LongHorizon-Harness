# OV2 ptait08 runtime admission — design proposal (INITIAL, no code, no infra change)

> **Corrections 2026-10-12.** Docs-only corrections of the merged runtime-admission proposal,
> applied against the cognizioware PR#2 review. Current source of truth is the PR#2 head at commit
> `df8b0cb` (`https://github.com/cogniziocompany/cognizioware/blob/df8b0cb79e39e359eb317e6a8eb9e649790b15f1/deployment/omnigent/`),
> which is a later commit than the earlier review ref `d89c556` used in the first correction pass.
> Sources fetched verbatim: `deployment/omnigent/runtime-admission-pr130-review.md`,
> `deployment/omnigent/hosted-agent-workspaces.md`, and `deployment/omnigent/agent-bindings.json`
> (the registry seed). Every short sha below was verified against actual `gh api` output. This
> changes prose only; no code, no provisioning, nothing requested.
>
> 1. **Host vs queue.** §6 distinguishes the **Omnigent host/runner** from CT110's Harness
>    **queue** — different things.
> 2. **Unprivileged OS account.** §1 proposes a **separate** unprivileged OS account with **no `sudo`
>    and no Docker access** (not the existing `cognizioware` account).
> 3. **Server principal / enrollment.** §2.4 states the options explicitly — a **user-owned CLI**
>    versus an unverified general admin machine API; **no credential minting in the database**.
> 4. **Parent route mandatory.** §2.5 marks the **Fleet Chat parent / span / tenant route** mandatory.
> 5. **PVE tailnet host vs guest.** §2.6 says the verified `100.78.157.127` enrollment is the
>    **host, not the guest**, and states what is unverified for the guest.
> 6. **Gateway PRs merged.** §2 (ordering rules) notes **PRs 481 and 480 are already merged**.
> 7. **Short SHAs verified.** §6 carries the `gh api`-verified short SHAs for cognizioware PR2
>    (`d89c556` → re-fetched at `df8b0cb`), claude-code-teams PR29 (`77599bf`), LHH PR127
>    (`f3338b7`); all matched, no mismatches flagged.
> 8. **Initial, not cutover.** §1 header marks this **INITIAL** admission, not the final paired-role
>    cutover.
> 9. **Shared Omnigent umbrella (6 unregistered slots).** §2 / §9 adds the umbrella
>    `https://agents.easybutt0n.ai`: families **overseer1, infraops1, devspecops1**, each with a
>    Claude runtime + an `-op` runtime + one effect writer = **6 slots**, all **UNREGISTERED** (the
>    `agent-bindings.json` seed shows every `session_id`/`runtime`/`runtimes` as `null`). No live
>    session ids are invented here.
> 10. **Host/agent/workspace distinction.** §2 / §10 carries the hosted-family acceptance criteria
>     from `hosted-agent-workspaces.md` — workspace must hold the admitted branch/repo, role
>     instructions, effective MCP/model config, and bounded evidence/parent state; equal duties do
>     not imply sharing one credential or giving every role admin access; an existing independent
>     Claude process is not auto-imported; plus the existing claude-code-teams migration.
>
> **Two targeted corrections (2026-10-12, applied on top of the 10-point pass above).**
> 1. **Migration paragraph (§10, hosted-family migration bullet).** claude-code-teams PR #29
>    (`77599bf`, verified) is now described as evidence of **parent-channel RECEIPTS only**; any
>    wording implying the hosted-family migration of the three families / six slots is complete is
>    removed (it is stated nowhere as complete).
> 2. **Enrollment principal (§2 step 4 + §5).** Decided as a **dedicated scoped machine identity**
>    (host-bound, expiring, revocable, no user-token fallback); the `[PAXTON DECISION]` marker is
>    removed from §2.4; the Paxton-owned CLI host is listed as the **rejected alternative**; and a
>    reference to cognizioware PR #2's `deployment/omnigent/` enrollment artifact is added.
>    *Flagged:* the directive's `deployment/omnigent/machine-host-enrollment.md` does not exist in the
>    cognizioware repo at `df8b0cb`; the reference points at the real `enroll-ptait08-tailscale.sh`
>    instead.
>
> **Kept open for Paxton (not resolved):** every account and grant choice remains a decision — see
> §5. The approval-by-link (Entra) design is preserved unchanged.

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

**OS account.** This is a **proposed separate unprivileged OS account**, not the existing one.
Do **not** run the runtime as `cognizioware` (UID 1000) — per the v2 build session report, the only
user on VM310, with `sudo` and `docker` group membership. The textbook least-privilege answer is a
dedicated, unprivileged account with **no `sudo` and no `docker` (Docker socket/group) access**: a
run account that can launch and supervise its own work, nothing host-level. Creating that account
is a change on the host, which this round forbids and which only Paxton can order, so the decision
is a **[PAXTON DECISION]** — see section 5, item 1. Until Paxton chooses, the OS-account column is
left as options, not as a chosen value.

- **Proposed phase 1:** run as the new dedicated unprivileged account, confined by configuration —
  a dedicated working directory for the v2 runtime, no cron/systemd units owned by other users, no
  interactive shell sessions sharing the account while a run is live, and no `sudo`/`docker` at
  runtime.
- **Proposed phase 2 (Paxton's call, listed in section 5):** harden the account (narrower shell
  scope, rotated credentials) once the runtime has proven itself, so VM310 ends up matching the
  separation VM211 already has.

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
3. **Install the host client on VM310.** Install the Omnigent host/runner (the "host client" in
   the v2 build report) plus its `config.toml` defining the trios/models this node serves. This is
   an install-time elevated step; after it, the runtime runs unprivileged as the dedicated account
   chosen in step 1. *Owner: infraops1.* (Execute only after steps 1–2; blocked until then.)
4. **Enrollment principal decided: a dedicated scoped machine identity.** Per Paxton 2026-10-12,
   the effective server principal is **decided** (not left open): the v2 runtime binds as a
   **dedicated, scoped machine identity** — **host-bound** (tied to this host's host ID), **expiring**
   (not a long-lived principal), **revocable** (revoked without a separate user action), and with
   **no user-token fallback** (a bare user login does not stand in for the scoped identity). The
   supported path is the **user-owned CLI login** (e.g. the patched Omnigent host client,
   `enroll-ptait08-tailscale.sh`), which produces exactly that scoped host identity. The **only
   rejected alternative** is a **Paxton-owned CLI host** (a general, wide-scope CLI identity) — it was
   considered and **rejected** because it does not narrow the server token to this host and so is not
   the principal. **Do not mint any credentials or write tokens into the database** to bridge this gap.
   This enrollment happens under the **Shared Omnigent umbrella** (`https://agents.easybutt0n.ai`;
   see §9): the runtime binds to a stable family workspace and the umbrella is the shared management
   surface for the three infrastructure-agent families, not a per-role grant. *Owner: devspecops1
   executes (principal already decided above; the request text is section 3, sent only after Paxton
   approves it).*
   - **Reference (source of the decided shape):** cognizioware PR #2, `deployment/omnigent/` — the
     enrollment artifact is `enroll-ptait08-tailscale.sh` (host-bound, authenticated by the owner,
     verified at head `df8b0cb`). *Discrepancy flagged:* the operator's directive referenced a
     `deployment/omnigent/machine-host-enrollment.md`; that file **does not exist** in the cognizioware
     repo at `df8b0cb` (full-tree + PR#2 search returned 0 hits), so the reference points at the real
     enrollment script instead of a non-existent path.
5. **Register the gateway route / parent route.** The **Fleet Chat parent channel and verified
   parent/span/tenant binding are mandatory** for every delegated span — the parent route is not
   optional. Register the v2 runtime's route under that parent route so calls from the new key land
   in the right deployment group and spend is attributable to `lh-overseer-v2-ptait08` (or the
   approved alias). Use explicit return/ACK receipts; session labels and chat `from` fields are not
   authenticated authority. *Owner: devspecops1.* **[PAXTON DECISION if the route introduces a new
   parent/egress path — devspecops1 flags it rather than choosing]**
6. **Verify hypervisor enrollment, then decide guest scope.** PVE host **ptait08 is verified
   Running/Online at `100.78.157.127`**. This is **hypervisor enrollment — it is the host, not the
   guest.** It does **not** prove that VM310 is independently enrolled or reachable. From the network
   design, decide whether a **separate guest identity** is needed for VM310 rather than repeat the
   completed hypervisor authorization. The login-link re-issue that stands in the way is **Paxton's**.
   *Owner: infraops1 executes the guest-side enrollment; the login-link re-issue is Paxton's.*
   **[PAXTON DECISION — login-link re-issue and whether a separate guest identity is required]**
7. **Verify admission, read-only.** Confirm from existing fleet surfaces that the node answers,
   its runs launch, and its queue entries appear — without resolving any gate and without writing
   any production tick. *Owner: Overseer1 (verification only; writer duties unchanged).*
8. **First-workload go/no-go.** Paxton decides when the first real task may run on the new host.
   *Owner: Paxton.* **[PAXTON DECISION]**

9. **Shared Omnigent umbrella: six unregistered slots (docs-only, nothing provisioned).** This
   proposal describes the *envelope* the v2 runtime will bind into — not a grant this round creates.
   Source: `cognizioware` PR#2 `deployment/omnigent/hosted-agent-workspaces.md` (fetched verbatim at
   head `df8b0cb`; `agent-bindings.json` registry seed at the same ref).
   - The umbrella server `https://agents.easybutt0n.ai` is the shared management umbrella for all
     three infrastructure-agent families. A deployed server, a successful Microsoft login, or an
     empty registry entry does **not** by itself fulfill the "admitted workspace" requirement.
   - **Logical families:** `overseer1`, `infraops1`, `devspecops1`. Each family pairs a Claude
     runtime with an `-op` runtime and **one active effect writer** — that is **6 runtime slots**
     total (overseer1 / overseer1-op, infraops1 / infraops1-op, devspecops1 / devspecops1-op).
   - The registry seed `agent-bindings.json` (ref `df8b0cb`) declares all three families with
     `session_id: null`, `runtime: null`, and `runtimes: { <name>: {session_id: null} }` for every
     runtime. **All six slots are UNREGISTERED.** This document states the slots and their `null`
     state; it invents **no live session ids** and does not label any unconnected slot active.
   - `overseer1-omi` is management metadata, **not** a seventh reasoning runtime. Each pair keeps
     separate authenticated principals and sessions, one shared logical inbox/checkpoint, and one
     active effect writer. Equal duties do not imply sharing one credential between runtimes or
     giving every role admin access.
   - **What this round does not do:** provision the isolated OS runtime account, install the
     qualified client, enroll ptait08, or bind real session ids through the controller. Those are
     the deployment steps in `hosted-agent-workspaces.md` §Deployment acceptance (item 1 onward);
     this design document only carries the umbrella shape and the *current gap* (on 2026-10-12 the
     delivered seed contained only an unbound overseer1).

10. **Host / agent / workspace distinction and acceptance criteria (docs-only).** Carried from
    `hosted-agent-workspaces.md` so a reviewer sees the hosted-family acceptance criteria this
    proposal must eventually satisfy. These are the *full deployment* criteria — the v2 runtime is
    the admission step §1–§8 covers; items below are what the controller enforces afterward.
    - **Workspace contents (required stable entry).** A runtime's workspace must contain the
      admitted branch/repo, the role instructions, the effective MCP/model configuration, and
      bounded evidence/parent state. This is verified against the workspace's contents, not against
      a deployed server or a Microsoft login.
    - **Credential separation.** Each pair (e.g. overseer1 / overseer1-op) retains separate
      authenticated principals and sessions. Equal duties do **not** imply sharing one credential
      between runtimes, nor giving every role admin access.
    - **Existing independent processes.** An existing independent Claude process is **not**
      automatically imported or managed just because the management server is running. Preserve its
      production history and writer while verifying its connection/migration independently.
    - **Existing migration (claude-code-teams).** The claude-code-teams PR#29 evidence
      (`77599bf9817d97bd04e6daf18f89b49901fb33c6`, verified) is evidence of the parent-channel
      **RECEIPTS only** — PR29's file is `agents/orchestrator/plans/fleet-chat-prod-migration.txt`,
      which lets the Fleet Chat deploy pipeline manage its own live prod pipeline (the parent
      channel's receipts), not a hosted-family migration of the three families or the six slots.
      This proposal does not reopen it; it is carried here only so the reviewer knows the umbrella's
      acceptance criteria already include that parent-channel receipt lineage, and so the hosted-family
      migration of the three families and the six slots is **not** stated anywhere as complete.
    - **The admitted controller.** Additions to the registry seed are applied through the admitted
      controller — preserving existing session links — once that controller is operational. The
      updater deliberately preserves an existing registry rather than overwriting live bindings.
    - **Session binding is real, not fabricated.** A session binds an agent, a host, and a
      workspace. Before any effect writer moves, verify failover fencing and resume; never fabricate
      session ids or label an unconnected slot active.

Ordering rules: steps 3–6 may interleave once steps 1–2 are resolved, but step 7 requires all of
3–6 complete, and step 8 requires step 7's read-back. §9 and §10 describe the umbrella and its
acceptance criteria; they are the shape the runtime binds into, not steps this round performs.
Gateway PRs **481 and 480 are already merged** — PR481 (`c9beba9`, deployment38160892455 succeeded) and PR480 (`5744932`, deployment38162461821 succeeded) are not merge choices here; remove any outstanding merge decisions.

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

- **`cogniziocompany/cognizioware` PR #2** — build evidence per the task context. Verified head
  commit `df8b0cb` (full `df8b0cb79e39e359eb317e6a8eb9e649790b15f1`). A later commit than the
  earlier round's review ref `d89c556` (full `d89c5560867485ab877f58c01dde76950ffa365b`), which
  still matches the actual commit. The review doc, `hosted-agent-workspaces.md`, and
  `agent-bindings.json` (the registry seed) were fetched verbatim at head `df8b0cb`; the short sha
  matches the actual commit.
- **`cogniziocompany/cognizioware` shared umbrella server** — fetched verbatim at `df8b0cb`: the
  umbrella server `https://agents.easybutt0n.ai` is the shared management umbrella for the three
  infrastructure-agent families (overseer1 / infraops1 / devspecops1). This is a real fetched fact,
  not a host on this proposal's touch list.
- **`cogniziocompany/claude-code-teams` PR #29** — build evidence per the task context; verified
  head commit `77599bf` (full `77599bf9817d97bd04e6daf18f89b49901fb33c6`). The short sha matches
  the actual commit.
- **`cogniziocompany/LongHorizon-Harness` PR #127** — cited in the task context as "(draft,
  `f3338b7`)". Verified directly against GitHub on 2026-10-11: it is **OPEN**, **a draft**, head
  branch `codex/manager-parent-debrief`, head commit `f3338b7`
  ("Reflect with Manager before parent debrief and bound continuation claims"), base `main`,
  title "Add checkpoint-bound Manager parent debrief boundary". The short sha matches the actual
  commit.
- **Short-sha verification (all three matched, re-verified at `df8b0cb`).** Verified via
  `gh api .../commits/<short>` on 2026-10-12 before writing: cognizioware PR2 `d89c556` =
  `d89c5560867485ab877f58c01dde76950ffa365b`; claude-code-teams PR29 `77599bf` =
  `77599bf9817d97bd04e6daf18f89b49901fb33c6`; LHH PR127 `f3338b7` =
  `f3338b7a494f21863636fad8fdbabda8d174bfd2`. No mismatches to flag. This pass additionally
  re-verified the cognizioware PR#2 head: `df8b0cb` = `df8b0cb79e39e359eb317e6a8eb9e649790b15f1`
  (the current source of truth for points 9–10; `d89c556` is a prior commit on the same PR that
  still matches the actual commit).
- **Graphify pre-edit query (required before authoring this file).** This run queried the **live**
  graphify index at `repo::path` for `LongHorizon-Harness::docs/handoffs/ov2-ptait08-runtime-admission.md`
  (the local graphify index, `graphify-out/graph.json`, at `origin/main`). The index registers
  `handoffs` as a `.register_handoffs()` callable rather than a per-file node — the exact file path
  is not a standalone graphified node. The file nonetheless exists on disk and is the authoritative
  input being corrected here; the local-repo equivalent was also performed: a full-tree search of
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
