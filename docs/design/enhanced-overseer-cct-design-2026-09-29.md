# Enhanced overseer on Claude Code Teams: first design answers

**Date:** 2026-09-29. **Task:** 286. **Companion:** `docs/handoffs/HANDOFF-enhanced-overseer-claude-code-teams-2026-09-29.md`.

**How this was produced.** The queued run of task 286 (run `20260929T030301Z_25ff072f` on CT110) completed the research in round 2, then failed on provider rate limits before writing this file. Its notes were saved and are the evidence base here. Session `[f14d17]` on ptait09 wrote this document from those notes; claims were not re-checked beyond what is stated.

**Labels.** **Measured** means the research run read the cited file directly. **Unverified** means the file states something live that could not be confirmed. **Not found** means searched and absent; the search is described.

## a. Fleet registration

**What the onboarding script does** (measured, `ptait09-easybutt0n-ai/scripts/onboard-device.sh`, 196 lines):
- Clones the runner repo, installs dependencies and writes `.env` with a generated runner key.
- Builds and starts the `easysvc` supervisor, then checks runner health on port 7334.
- Installs the Hydra device agent, which registers itself on first start. The default Hydra address is `wss://hydra.easybutt0n.ai/agent`.
- Installs Claude Code Remote Control as a fallback link, and optionally Ollama.

**What registering involves beyond the script** (measured; the script prints these as remaining human steps):

| # | Step | Where |
|---|---|---|
| 1 | Add a public hostname `<device-id>.easybutt0n.ai` → `http://localhost:7334` on the device's Cloudflare tunnel | Cloudflare |
| 2 | Add the device's token to `DEVICE_TOKENS` in the Hydra `.env` on corsairai300, restart the orchestrator | corsairai300 |
| 3 | Ollama sign-in and LAN exposure (optional; cloud models are relay-routed) | Device |
| 4 | Verify Hydra enrolment (`e2e/hydra-enroll-e2e.mjs`) and the Remote Control link | Device |
| 5 | Enrol with fleet.easybutt0n.ai: set `fleet.url`, get a bootstrap token from fleet-admin (`POST /admin/tokens`), run `easysvc enroll`, restart the supervisor, confirm events arrive | Device + fleet-admin |
| 6 | Add the device to `connectors/fleet.json` in the runner repo (env var names only) | Runner repo |

**fleet-admin mechanics** (measured, `fleet-admin/README.md` and `docs/fleet/device-emission.md`):
- `POST /enroll` trades a one-time bootstrap token for a per-device key.
- `POST /checkin` is an HMAC-signed heartbeat; replies can carry new config, an action or an update.
- Devices dial out over HTTPS through Cloudflare, so no inbound port is needed.
- Event redaction runs on the device before sending: API keys, GitHub tokens, bearer strings, private keys and long hex strings are masked.

**How a subdomain is assigned** (measured): manually, one Cloudflare tunnel public hostname per device. **Not found:** any infrastructure-as-code for tunnel routes, or a wildcard `*.easybutt0n.ai` route (searched the runner repo and `cognizioware-hydra`).

**Implication for per-user instances:** steps 1, 2, 5 and 6 are all manual today. The gateway already has `cloudflare-dns_upsert_record` and tunnel read tools, but no tool that writes tunnel routes. Automating instance creation needs that, plus a Hydra token API. `cognizioware-hydra-tokens` has a runtime device-token provisioning API (commit `45aef9a`), which may remove the restart in step 2; unverified whether it is deployed.

## b. Sign-in reuse

**Cloudflare Access** (measured, `cognizioware-hydra/openwebui/fleet-chat/README.md` lines 43–52): chat.easybutt0n.ai is published through a Cloudflare tunnel. UAT uses the same pattern with a Cloudflare Access policy on the `easybutt0n-production` account, "where Tunnels + Access already exist". The chat's env template has no OAuth or OpenID variables, consistent with sign-in happening at Cloudflare Access rather than in Open WebUI.

**The tools gateway's Entra app** (measured, `cognizioware-mcp-tools/infrastructure/README.md` lines 49–57): registration `cognizioware-ops-it-services`, app roles `AI_Admin` and `AI_User`, fronted by OAuth2 Proxy. Its redirect address is bound to one hostname, `tools-gateway-v3.cognizioware.com`.

**Answer.** Cloudflare Access covers a new subdomain with no new Entra registration: the identity provider is connected once at account level, and each new hostname needs a tunnel route plus an Access application or policy. The gateway's Entra app does not cover new hostnames as it stands; reusing it would mean adding redirect addresses to that registration.

**Unverified:** whether the `easybutt0n-production` account uses one wildcard Access application or one per hostname. **Not found:** Access policies as code (searched `cognizioware-hydra`). The live list shows three Access applications, each for a single hostname, which suggests per-hostname.

## c. Chat channel interface

Measured from upstream `mcp/slack-channel/server.ts` (783 lines, read-only clone, nothing executed). A new chat.easybutt0n.ai channel must provide the following.

1. **Process:** an MCP server over stdio, started by Claude Code inside the agent. Upstream uses Bun. It connects its external link first and the stdio transport last, because the stdio loop never returns.
2. **Channel capability:** the server declares experimental capabilities `claude/channel` and, optionally, `claude/channel/permission`. These are what make an MCP server a channel.
3. **Instructions to the agent:** that transcript output never reaches the sender and everything visible must go through the reply tool; and the inbound envelope format, e.g. `<channel source="..." channel_id="..." ts="..." user="..." user_id="..." kind="...">`.
4. **Inbound messages:** one notification, `notifications/claude/channel`, with `content` and `meta` (`channel_id`, `ts`, `user`, `user_id`, `kind`, optional thread id and attachment paths). Before sending it, the server:
   - ignores its own messages and edits;
   - de-duplicates by message id (bounded set of 1,000);
   - checks an allow-list in `access.json`;
   - serialises events through a queue so concurrent messages can't race.
5. **Outbound tools:** `reply` (chunked at 4,000 characters), `react`, `update` (edit an earlier message; upstream recommends posting "working on it" then updating with the result), `remove_reaction`, `upload`. Errors return content with `isError: true`.
6. **Permission relay (optional):** handles `notifications/claude/channel/permission_request`, forwards the request to allowed users, and turns a `yes <code>` / `no <code>` reply into `notifications/claude/channel/permission` with `allow` or `deny`.
7. **Configuration:** a state directory under `~/.claude/channels/<name>/` holding a `.env` for credentials and `access.json`. It exits at start-up if credentials are missing.
8. **Registration:** an entry in the agent's `.mcp.json` under `mcpServers`, loaded with `--channels` / `--dangerously-load-development-channels`; the key must match. Upstream adds a `channel-reply-reminder` hook that pushes the agent to reply through the tool.
9. **Connection direction:** the Slack channel dials out over a WebSocket, so it needs no public address. A chat.easybutt0n.ai channel can do the same and stay behind Cloudflare Access.

**Design consequence:** the Open WebUI side needs something the instance's channel can connect to. The natural piece is a Pipe (see d), which relays each user message to the right instance and streams the reply back.

## d. Long tasks in chat

**Task 277's design output:** not found. Searched git history, `docs/`, the ledger and queue files; `docs/design/` did not exist before this task.

**What Open WebUI on this fleet supports** (measured; Open WebUI `v0.11.4`, mcpo `0.0.20`):

| Mechanism | What it gives | Evidence |
|---|---|---|
| **Automations** | Scheduled runs that create a chat; the result is read from that chat through the API | `fleet-chat/README.md` lines 100–139 |
| **Pipes** | A custom model in the picker, written in Python and applied through the admin API; it can call external services during a request | `design/rsi-org-loop/10-openwebui-wiring.md` lines 123–166; `openwebui/functions/rsi_org_loop.py` |
| **Filters** | Hooks before and after every request; `hivemind_outlet` posts each completed turn to memory | `openwebui/functions/hivemind_outlet.py` |
| **Chat API + websockets** | Chats can be read through the API; open browsers get updates over websockets | `10-openwebui-wiring.md` line 177 |

**Not found:** any supported way for a service to write into an existing user conversation after the request has returned, or an outbound push to the user.

**Answer.** Supported today:
- **Acknowledge, then poll:** the Pipe replies at once with a task reference; the user asks for status later. Fully supported.
- **Results in a separate chat:** an automation-style chat the user opens, updated live if it's open. Supported.
- **Notify elsewhere:** there is precedent for posting out (the memory outlet); nothing posts back into the chat.

Pushing into the same open conversation is unverified and would need testing against Open WebUI's chat API.

**Gateway tools on other models:** tool servers are attached per model preset (`meta.toolIds`) and per chat by the user; there is no global default (`fleet-chat/README.md` lines 153–181). Keeping them on other models is a configuration choice, not a code constraint.

## e. Memory

**Hivemind** (measured, `cognizioware-mcp-tools/infrastructure/docker/memory-mcp/`):
- **Store:** `hivemind_sessions.session_memories` on CT103 (pgvector, 768-dimension `nomic-embed-text-v2-moe`). Rows carry `source` (`openwebui`, `lh-harness`, `overseer`), `session_id`, `user_name`, `device`, `repo`, `branch`, `tier`, text and a content hash that prevents duplicates.
- **Tools:** `remember_session`, `recall` (semantic top-k with filters; falls back to chronological when embeddings are unreachable), `get_session`, `list_recent`. A REST ingester at `POST /api/memories`.
- **Feeds:** the chat outlet filter; `ingest-harness.js`, which walks harness run folders; and `overseer-session-ingest.mjs` every 5 minutes on ptait09 for source `overseer`.
- **Gateway alias:** `memory`, tools appear as `memory-recall` and so on.

**Against the Claude Code Teams gaps:**

| Gap | Covered by Hivemind? |
|---|---|
| Keyword-only search | **Yes.** Semantic search over embeddings. |
| Per machine and per account | **Yes, by design.** One central store through the gateway; rows carry device and user, so recall can filter or span. |
| Retrieval not automatic | **No.** Hivemind only offers tools. The automatic piece is the proposed hook on each inbound message, which doesn't exist yet. |
| Subagent transcripts skipped | **Unverified.** Depends on what the ingesters read. |
| Compaction lossy | **Partly.** Turns are captured as they happen for chat and harness, but nothing captures a Claude Code Teams session before compaction. |

**Secrets:** the Hivemind chat outlet posts raw turn text with no redaction. The fleet event plane does redact. The overseer's memory feed needs the same redaction before it goes live.

**Moving ptait09's overseer ingest:** `overseer-session-ingest.mjs` runs only on ptait09. Replacing the primary overseer means moving or re-pointing that feed to the new instance.

## Decisions for Paxton

1. **Automating instance creation:** accept manual steps for the pilot, or build tunnel-route and Hydra-token automation first?
2. **Access applications:** one wildcard Access application for `*.easybutt0n.ai`, or one per instance?
3. **Long tasks:** acknowledge-then-poll for the pilot (recommended), with results in a separate chat as the next step?
4. **Tools on other models:** keep the gateway tools on other chat models alongside the Overseer model?
5. **Redaction:** require redaction in the overseer's memory feed before it goes live?
