Written for: an expert, human or another Claude session, who has not seen this conversation and will explore solutions with you.

---

# Handoff: the enhanced overseer on Claude Code Teams, fleet-registered, with capacity managed

**Revised:** 2026-09-29 (UTC) by session `[f14d17]` (Claude Code on ptait09, interactive; no fleet agent name assigned).
**Replaces:** the capacity-only handoff of 2026-09-28 by overseer session `[909876]`, which stays unchanged at `C:/tmp/HANDOFF-subscription-capacity-2026-09-28.md` and in `LongHorizon-Harness` `docs/handoffs/` (PR #61, `2bbd7ed`).
**Goal of the conversation:** agree the design for running a fork of Claude Code Teams as the enhanced overseer, on any fleet device, reached through chat.easybutt0n.ai, using the model capacity we already pay for.

Each fact below is marked **measured** (checked live by `[f14d17]` on 2026-09-29), **reported** (from another session, not re-checked) or **unverified**.

## 1. Status of the original handoff

| Item | Status | Basis |
|---|---|---|
| Does a capacity feature already exist? | **Reopened.** Task 282 searched `cognizioware-mcp-tools` (the ops control centre) and found none. Paxton says the feature is in a different, related admin centre in another repo; the original pointer to ops was a repo mix-up. Which admin centre and repo is not yet identified. | reported + Paxton 2026-09-29 |
| What the ops control centre does have | `AdminCentres.razor` has a "Provider quota & usage" section that only links out to the Synthetic and Ollama Cloud quota pages. It records that Synthetic's documented `/quotas` endpoint returned 404 on every API-key path tried (2026-09-09). | measured |
| Capacity monitor and router failover | Live (#203). | reported |
| Synthetic probe fix | Task 285, still pending. | reported |
| Task 283 (this handoff, queued) | Done, gate stopped after PR check. | reported |
| Who the ptait09 HTTP runner runs as | `NT AUTHORITY\SYSTEM`. It has no Claude login of its own. | measured |

The monitor and failover (#203) were built on task 282's finding. Once the other admin centre is identified, check whether #203 duplicates it or should read from it.

## 2. Decisions Paxton has made (2026-09-28)

| Topic | Decision |
|---|---|
| Overseer | Claude Code Teams becomes the enhanced overseer. It **replaces the primary overseer**, which today is a Claude Code session on ptait09 (`[909876]`, stopped by Paxton 2026-09-29 02:06Z). Confirmed by Paxton 2026-09-29. |
| Repo | A fork of `VantaSoft/claude-code-teams`. It must keep its original standalone capabilities. |
| Integration | Our infrastructure attaches as a plugin bundle plus a thin SDK, not as changes to the upstream code. |
| Device model | Fleet-device-agnostic. The host registers with the fleet/Hydra configuration using the current `remote-pc` runner, not the older per-host `ptait09-remote-pc` skill. |
| Environment | LH harness, fleet and chat run together in one Ubuntu environment, as separate components by design. |
| Front end | chat.easybutt0n.ai, which is also the pilot. |
| Isolation | One VM per user, on corsairai300. |
| Audience | Internal first, designed so external customers can be added. |
| Credentials | Mixed: the user's own for personal accounts, service accounts for infrastructure tools through the gateway. |
| Remote access | RDP to the instance, used for each user's first-time logins. |
| Registration | Each instance is registered in fleet.easybutt0n.ai and gets its own subdomain (decided 2026-09-29; this replaces the earlier idea of registering in ops.easybutt0n.ai). |
| Per-user instances | More `*.easybutt0n.ai` subdomains, with usage tracked in LiteLLM. |
| Sign-in | Reuse the Entra and Cloudflare Access setup we already have. No new app registration per instance. |
| Remote desktop | Apache Guacamole, behind the same sign-on, opened from a link in the chat front end. See section 5a. |
| Chat surface | The overseer appears as a model in the Open WebUI picker, backed by a custom chat channel in our plugin bundle. See section 5a. |
| VM 210 | Deleted 2026-09-29. Instances are built on corsairai300. |

Nothing existing is stopped or removed as part of this.

## 3. What Claude Code Teams is

Read from the upstream source on 2026-09-28. It has not been installed or run.

- Each agent is a normal Claude Code session kept alive in its own tmux session.
- Upstream channels are Telegram, iMessage, Discord and Slack.
- A fleet MCP server provides start, compact, message, context-check, recall and create-agent tools.
- Routines are per-task files with cron frontmatter, synced to the OS crontab.
- Upstream launches agents with `claude --dangerously-skip-permissions`.

**Memory by design:**

| Layer | Holds | Reaches the model |
|---|---|---|
| `CLAUDE.md` | Identity and standing rules | Every turn |
| `memory/` + `MEMORY.md` | Short facts, capped index | Auto-loaded |
| `llm-wiki/` | Shared cross-project knowledge | On demand |
| `recall` | Every session transcript, SQLite FTS5 keyword search | On demand |

**Known gaps:** retrieval is not automatic, search is keyword-only, subagent transcripts are skipped, compaction is lossy, and everything is per machine and per user account.

## 4. What our infrastructure already provides

| Need | Existing piece | Basis |
|---|---|---|
| Chat front end with sign-in | chat.easybutt0n.ai (Open WebUI + mcpo) on corsairai300, behind Cloudflare Access | measured |
| Task queue and runs | `lh-harness` CT110 on corsairai300, registered as fleet node `ct110`, online, 5 concurrent runs | measured |
| Fleet control plane | Hydra on corsairai300; devices `ptait09` and `ptait-desk03` online | measured |
| Device onboarding | `scripts/onboard-device.sh` in the runner repo installs the runner and registers the Hydra agent | reported (skill doc) |
| Semantic memory | Hivemind: pgvector on CT103, fed by the chat outlet filter and a 5-minute harness ingest | reported (skill doc) |
| Tools and model tracking | LiteLLM gateway on CT202, one virtual key per user | measured |

Hivemind already covers two of the Claude Code Teams gaps: semantic search and memory that outlives one machine.

## 5. Proposed architecture

```
chat.easybutt0n.ai (pilot front end, Cloudflare Access + Entra)
        |
        v
LiteLLM gateway (per-user virtual key; tools + usage tracking)
        |
        +--> Hydra / fleet plane --> any registered fleet device
        |                                   |
        |                                   v
        |                     Ubuntu instance (one VM per user)
        |                       - runner + Hydra agent (remote-pc)
        |                       - Claude Code Teams fork (standalone-capable)
        |                       - our plugin bundle + thin SDK
        |                       - xrdp for first-time logins
        |                       - encrypted per-user home
        |
        +--> lh-harness queue (overseer task = queue task)
        +--> Hivemind memory
```

- **Plugin bundle:** gateway MCP connection, skills and hooks. Removing it leaves upstream behaviour intact.
- **Thin SDK:** a small client for enqueue, status and recall, for callers that are not Claude Code.
- **Memory:** keep the local keyword recall and add Hivemind as the semantic layer, with a hook that retrieves automatically on each inbound message.
- **Chat versus tasks:** quick questions go straight to a model; "go do this work" becomes a queue task.

## 5a. Remote access and chat channel design (approved by Paxton, 2026-09-29)

RDP carries a screen, not messages. So remote desktop and the two-way chat link are separate pieces.

**Web RDP with sign-on**

| Option | Fit | Notes |
|---|---|---|
| **Apache Guacamole** (chosen) | Best | Open source, runs in a browser, speaks RDP to xrdp on Ubuntu, supports Entra sign-in via OpenID Connect or SAML. Has an API for creating a connection per user. |
| Cloudflare browser RDP | Possible | No new service, but built for Windows targets. Whether it works against xrdp on Ubuntu is untested. |
| Kasm Workspaces | Poor | Built around container desktops; we chose a VM per user. |

Guacamole sits on corsairai300 behind the same Cloudflare Access setup as chat.easybutt0n.ai, so users sign in once.

**Two-way channel with the chat**

Claude Code Teams treats each messaging service as a plug-in channel. Its Slack channel is a custom MCP server in the repo (`mcp/slack-channel/server.ts`), which is the pattern to copy.

- **On the instance:** a new channel server, modelled on the Slack one, that receives messages from the chat and sends the agent's replies back.
- **In Open WebUI:** the overseer appears as a model in the picker. A message to it goes to that channel, and the reply streams back into the conversation.
- **Placement:** in our plugin bundle, so the fork's standalone channels stay untouched.

**Hydra covers live viewing**

Hydra already shows a device's terminal with input and screen reading, and Claude Code Teams agents run in terminal sessions. Once an instance is registered with the fleet, Hydra can show and steer the agent without a desktop. RDP is then for what needs a real browser on the machine: first-time logins to Claude, Google and similar.

| Need | Piece |
|---|---|
| Talk to the overseer | Chat channel, as a model in Open WebUI |
| Watch or steer the agent live | Hydra |
| First-time logins, full desktop | Guacamole, opened from a link in the chat |

Guacamole opens in a new tab from a link, not embedded in the chat page. Embedded remote desktops tend to have trouble with sign-in cookies and keyboard capture.

**Not yet verified:** Guacamole against xrdp with Entra sign-in on our setup, and the fleet and memory tools already being wired into the chat (from the `remote-pc` skill doc).

## 6. Questions to explore with the expert

**Design, still open:**
1. **Fleet registration.** What does registering an instance in fleet.easybutt0n.ai involve beyond running the runner's onboarding script, and how is the subdomain assigned? Unverified.
2. **Sign-in reuse.** Which existing piece covers a new subdomain with no extra work: the Cloudflare Access policy, the Entra app behind the tools gateway, or both?
3. **Long tasks in chat.** The overseer is a model in the picker (decided). Chat is request and reply; how do results from a long task reach the user later: acknowledge then poll, push into the conversation, or notify elsewhere? Whether the existing gateway tools also stay available on other models is undecided.
4. **Permissions.** Upstream skips all permission prompts. What replaces that on a shared, internet-reachable instance?
5. **Transcript secrets.** Recall indexes full tool output. What is scrubbed at index time, and what is kept out of Hivemind?
6. **Sequencing.** Paxton ruled on 2026-09-14 that fleet-plane tasks (172–177) follow every powerplatform task, and that gate resolution from chat waits on per-user keys (tasks 140 and 177). Does this work join that lane or get its own priority?

**Capacity, carried over:**
0. **The other admin centre.** Which repo and admin centre holds the included-capacity feature, what does it measure, and can a machine read it? This comes first; it decides whether #203 stands alone.
7. **Allocation.** How is included capacity split between harness runs (heavy, bursty) and the overseer (light, must never starve)?
8. **Admission control.** Should the CT110 launcher gate new runs on the capacity signal now that the monitor is live?
9. **The overseer's model.** Can it stay on included capacity, or is a small paid model worth it for reliability?
10. **Ollama Cloud accounts.** Which are near their monthly limits, and when do they reset?

**Claude subscription, carried over and now sharper:**
11. **Per-user logins.** The use case (Paxton, 2026-09-29): each user opens their own Ubuntu desktop over RDP through the easybutt0n.ai front end and logs into Anthropic's own Claude Code themselves, with their own account. We do not build a claude.ai login into our product and no account is shared. That is a person using Claude Code on a remote machine, which is different from the case Anthropic's docs restrict (third parties offering claude.ai login or rate limits in their own products).
    - **Still to confirm:** whether unattended work on that login (routines, queue tasks the overseer sends into the user's session while they are away) stays within the subscription terms, and at what volume. Exact Consumer Terms wording is still unconfirmed.
    - **External customers:** the same question applies with more weight, because the hosted instance is then something we sell.
12. **Task 117.** Automation is currently banned from spending Paxton's personal subscription. An overseer running under his login needs that rule lifted or narrowed explicitly.
13. **Vehicle.** Setup token (`CLAUDE_CODE_OAUTH_TOKEN`), API key budget, or Team seats?

## 7. Constraints the expert must know

- **LiteLLM on CT202 is prod.** Editing `litellm-config.yaml` restarts the router. Model and router settings are database-owned. Changes go to UAT (CT204) first, as a script with a dry run by default.
- **Host-level changes on ptait01** need Paxton's explicit go, a rollback plan and a quiet window.
- **No new spend or credentials** without Paxton. Names are fine in docs; key values never are.
- **Deploys:** harness runs never deploy. CT110 restarts need drain on and no active runs.
- **corsairai300 capacity (measured):** 46 GB RAM, 37 GB available, about four 8 GB VMs. Its graphics chip shares system RAM, so a large Ollama model reduces that. It is not clustered with ptait01, and holds no Ubuntu image yet.
- **Windows runners run as SYSTEM.** Only sessions created under the runner user are resumable by it.

## 8. Current state of the build

| Item | State | Basis |
|---|---|---|
| VM 210 `cct-ai-assist` on ptait01 | Deleted 2026-09-29 on Paxton's instruction. Config and both disks are gone; 192.168.21.165 is free. It was never configured. | measured |
| Instance on corsairai300 | Not created. | measured |
| Fork of the repo | Not created. | measured |
| Claude Code Teams install | Not done. A first attempt on ptait09 was blocked by the permission classifier. | measured |
| Plugin bundle, SDK, xrdp, tunnel route, Access app, ops registration | Not started. | measured |

**Side finding:** storage `tank-zfs` on ptait01 is defined but inactive (pool `tank` cannot be imported). It predates this work and made the first delete attempt fail.

## 9. Where to look

| What | Where |
|---|---|
| Overseer ledger and open asks | `C:/tmp/queue/LEDGER.md` (tail), `C:/tmp/queue/OPEN-ASKS.md` |
| Chat overseer instructions and tick rows | `LongHorizon-Harness/docs/handoffs/overseer-secondary-session.txt` |
| Original capacity handoff | `LongHorizon-Harness/docs/handoffs/HANDOFF-subscription-capacity-2026-09-28.md` |
| Upstream source | `github.com/VantaSoft/claude-code-teams` |
| Fleet skill and runner repo | `~/.claude/skills/remote-pc/SKILL.md`; `C:/Users/PaxtonTait/source/ptait09-easybutt0n-ai` |
| Fleet registry | `connectors/fleet.json` in the runner repo |
| Live queue | CT110 `GET http://192.168.21.168:8799/api/queue` (bearer token from CT110's secrets env) |
| Router | CT202 container `cognizioware-mcp-tools-litellm`; UAT is CT204 |
| Claude headless auth | https://code.claude.com/docs/en/authentication.md#generate-a-long-lived-token |

## 10. The decision this session should end with

A short written design covering:
- how an instance is provisioned and registered (fleet, ops, subdomain, Entra);
- the plugin and SDK boundary that keeps standalone mode intact;
- the memory design (local recall plus Hivemind) and what is scrubbed;
- the permission model on shared instances;
- the capacity allocation between harness and overseer;
- a yes or no on subscription-backed use, with the vehicle and scope;
- where this sits in the queue order.
