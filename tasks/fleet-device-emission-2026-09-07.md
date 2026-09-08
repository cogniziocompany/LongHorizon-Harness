# Fleet emission: every remote-PC function pushes to fleet.easybutt0n.ai over socket.io (2026-09-07)

**Ask (Paxton 23:15 PT):** add this plan as a harness task to manage. Planning session 54360c27 (PTAIT09). Companion to the Harness Fleet Window (tasks/harness-fleet-window-2026-09-07.md, queue 05h5/05h6/05h7): same fleet-admin ingest, same /fleet socket.io namespace, same requireDevice() auth - built once.

**Queue:** `05h8-fleet-device-emission` (ptait09-easybutt0n-ai, kimi, 10 rounds, text `C:	mpleet-device-emission-task.txt`), scheduled right after 05h5 (fleet-admin) because it bases on that branch. Slices: Phase 0 consolidation plan + kept device scripts -> fleet-admin device_events ingest over socket.io with device-key handshake + REST fallback -> runner loopback bridge on every function with secret masking -> easysvc socket.io relay -> rollout docs. Rollout order after the runs: fleet-admin deployed via 05h7 first, then devices repointed to main one at a time (PTAIT09 last; never touched by a run).

## Progress
- 2026-09-07 23:20 PT: queued; plan copied below verbatim.

---

# Fleet emission: every remote-PC function pushes to fleet.easybutt0n.ai over socket.io

## Plan header (for the expert who builds and schedules this)

| Field | Value |
|---|---|
| Planning session id | `54360c27-f7c3-568a-b3b4-eb72e2cbcba3` (Claude Code, PTAIT09, 2026-09-07) |
| Plan file | `C:\Users\PaxtonTait\.claude\plans\for-ptait09-easybutt0n-ai-repo-and-mighty-sifakis.md` |
| Primary repo | `cogniziocompany/ptait09-easybutt0n-ai` — **the only repo**; all four "device repos" are checkouts of it |
| Primary dir / branch | `C:\Users\PaxtonTait\source\ptait09-easybutt0n-ai`, branch `easybutt0n-runner-v2`, HEAD `55c667c` |
| Target branch | `main` (Phase 0 promotes `easybutt0n-runner-v2` → `main`; `main` becomes the single pipeline branch) |
| Work branches | Phase 1 `feat/fleet-device-events` (fleet-admin); Phase 2 same branch, `svc/` + `src/` |
| Server component | `fleet-admin/` subdir → compose service `fleet-admin:8787` behind Caddy on CT202 `192.168.21.161` |
| Public host | `https://fleet.easybutt0n.ai` (**502 today** — container pruned in the disk-full event; restore is task 19b) |
| Database | agent-db Postgres `192.168.21.154:5432/ai_easybutt0n`, schema `fleet` |
| Device checkouts | ptait09 `C:\Users\PaxtonTait\source\ptait09-easybutt0n-ai` · desk03 `C:\Users\PaxtonTait\source\ptait-desk03-easybutt0n-ai` · htpc01 `/home/prax211/htpc01-easybutt0n-ai-git` · ptait10am5 `C:\Users\PaxtonTait\source\ptait10am5-easybutt0n-ai` |
| Related plan | LongHorizon-Harness `tasks/harness-fleet-window-2026-09-07.md` (task 19 = 19a/19b/19c). **This plan shares 19a's socket.io server + `requireDevice()` — coordinate, do not build twice.** |
| Reference only | `cognizioware-hydra-fleet` (control surface, unchanged); `agentic-remote-pc` (clean upstream template) |
| Host registry | `connectors/fleet.json` in the runner repo (env-var names only, never key values) |
| Skills to drive hosts | `/remote-pc` (fleet-wide), `/htpc01-remote-pc`, `/ptait09-remote-pc`, `/ptait-desk03-remote-pc` |
| Hard rules | Secrets: names only in repos. Windows clones `autocrlf=false`, check `git diff --stat` before PR. Back up CT202 `docker-compose.yml`/`Caddyfile` as `.bak-YYYYMMDD`; `caddy reload`, never restart. |

## Context

Today each fleet device runs the `*-easybutt0n-ai` runner (HTTP shell proxy exposing
`run_command`, `claude_prompt`, `cursor_prompt`, `write_file`, … over REST + MCP), and the only
outbound signal is a coarse HTTP heartbeat from the C# supervisor (`svc/FleetAgent.cs` →
`POST /checkin`, every `IntervalSeconds`) — and that is **disabled**, because `fleet.url` is `""`
in every `svc/*.svc.json`. Nothing reports *what the device actually did*. There is no live view
of the fleet.

This is exactly the gap the Fleet Window (task 19) describes — "every node pushes events to
fleet.easybutt0n.ai, with socket.io deltas" — but task 19's three queues (19a/19b/19c) scope the
push side to **lh-harness sessions**. This plan covers the other, more basic source the user is
asking for: **the remote-PC runner functions themselves**, on every live device and every OS,
feeding the same fleet-admin ingest and the same `/fleet` socket.io namespace.

Second problem in the way: the four "per-device repos" are not separate repos. They are all
checkouts of `cogniziocompany/ptait09-easybutt0n-ai` on diverged per-device branches. Building an
emitter on any one of them would strand it. The user's direction is **one universal `main` on the
pipeline going forward**, so consolidation is Phase 0.

Outcome: from `https://fleet.easybutt0n.ai` you see every device live, and every function
invocation on it, streaming in.

### Note on "our public IP"
No inbound path is opened. Devices dial **out** to `fleet.easybutt0n.ai` (Caddy on CT202
`192.168.21.161` → compose service `fleet-admin:8787`). This is what makes NAT'd and
tunnel-only hosts work, and it matches the existing enroll/checkin design
(`FleetAgent.cs:14-24`). No device needs a public IP or a forwarded port.

## Current state (verified 2026-09-07)

| Fact | Evidence |
|---|---|
| One repo, per-device branches | All 4 checkouts: `origin = github.com/cogniziocompany/ptait09-easybutt0n-ai` |
| `easybutt0n-runner-v2` is the superset | 49 commits ahead of `main`, only 3 behind → the basis for the universal main |
| Device branches diverged | `ptait09` 29/19, `ptait-desk03` 24/25, `ptait10am5` 11/18 (ahead/behind main) |
| Siblings are an older fork | `htpc01`/`desk03`/`agentic-remote-pc` `src/` are byte-identical to each other, and older than v2 — **no `svc/`, no `fleet-admin/`** |
| Devices live | ptait09, ptait-desk03, htpc01 all `/health` OK; **ptait10am5 tunnel down** |
| Emitter half-exists | `src/runner.js:190` `emitCompletionEvent()` — HMAC-signed, fire-and-forget, off unless `RUNNER_EVENTS_ENABLED=1`; wired only to agent helpers, **not `/exec`** (`runner.js:1052`) |
| Device auth exists | `POST /enroll` (bootstrap → per-host `device_key`) + `POST /checkin` (HMAC-SHA256 over raw body, `X-Fleet-Host` + `X-Fleet-Signature`) — `fleet-admin/src/server.js:59,91` |
| **socket.io does not exist yet** | Neither in `fleet-admin/` nor in any device code. Server + client are both new. |
| `fleet-admin` container was pruned | Disk-full event; `fleet.easybutt0n.ai` currently 502s (task 05e) |

## Decisions (confirmed with user)

1. **Promote `easybutt0n-runner-v2` → `main`**, make `main` the single pipeline branch, build the
   emitter on `main`, repoint every device to track `main`.
2. **Emitter lives in the C# supervisor** (`svc/FleetAgent.cs`) — it already owns enrollment, the
   device key and the HMAC contract, and it survives a runner crash.
3. **Events emitted:** invocation **start + finish** (tool/shell, host, task id, cwd, status, exit
   code, duration) and **command + truncated output** (last ~4 KB, secret-masked).
   *Not* in scope: live stdout streaming, periodic host heartbeat beyond the existing checkin.

### Consequence of decision 2 that shapes the design
The supervisor is a **separate process** from the Node runner, so it cannot see per-function
invocations by itself. Decision 3 requires per-function detail. Therefore the runner must hand its
events to the supervisor over a **loopback-only bridge**, and the supervisor relays them upstream.
This keeps the socket.io client, the device key and the HMAC in one place (as chosen) while still
getting function-level fidelity. The bridge is the one piece of new plumbing this choice adds.

```
runner function (src/runner.js)
   └─ emitFleetEvent()  ──POST 127.0.0.1:<bridgePort>/events──▶  easysvc FleetAgent
                                                                   └─ socket.io client ──▶ fleet.easybutt0n.ai /fleet
```

## Phase 0 — One universal `main`

1. Open PR `easybutt0n-runner-v2` → `main`. Only 3 commits on `main` are absent from v2; reconcile
   those, then merge. `main` becomes the v2 code (easysvc supervisor, `fleet-admin/`, claude-rc,
   `scripts/onboard-device.{ps1,sh}`).
2. Triage each device branch's unique work against the new `main`, cherry-picking what is real and
   dropping what is scratch. Representative items to decide on, not blindly merge:
   - `ptait09`: `scripts/graphify-*.ps1` (keep), `patch-cursor.mjs`/`patch-cursor2.mjs`,
     `qa_ptait09_local_pipeline.js`, `pm2-resurrect.bat` (all likely drop — superseded by easysvc)
   - `ptait-desk03`: `scripts/restart-runner.ps1`, graphify scripts (dedupe with ptait09's)
   - `ptait10am5`: `e2e/harness-e2e.mjs` (keep), `ecosystem.config.cjs` (drop — PM2 is superseded;
     note its `cwd` wrongly points at `ptait09-easybutt0n-ai`)
   - `htpc01`: `deploy/install-linux.sh`, `scripts/fix-htpc01-service.sh` — fold the Linux install
     path into `scripts/onboard-device.sh` rather than keeping a parallel one.
3. Per-device config stays **data, not branches**: `svc/runner.<host>.svc.json` +
   each host's `.env`. `connectors/fleet.json` remains the canonical host registry.
4. Repoint each device checkout to `main` (`git fetch && git checkout main`). Retire the
   `ptait09` / `ptait-desk03` / `ptait10am5` / `htpc01*` branches once their hosts are on `main`.

> Do this before the emitter work so the emitter is written once, on `main`, and every device gets
> it by pulling the same branch.

## Phase 1 — fleet-admin: socket.io server + device event ingest

Repo `ptait09-easybutt0n-ai`, subdir `fleet-admin/`, branch `feat/fleet-device-events` off `main`.

**Schema** (`fleet-admin/schema.sql`, idempotent, applied by the existing `src/migrate.js`):
- `fleet.device_events(id, host, task_id, tool, shell, phase, cwd, status, exit_code, duration_ms,
  command text, output text, truncated bool, ts timestamptz)`, index `(host, ts desc)` and
  `(host, task_id)`.
- Daily prune job (`setInterval`) dropping rows older than 14 d — same retention rule task 19 sets
  for `fleet.harness_events`.

**Ingest** — reuse the existing HMAC device path. Factor the verification currently inline in
`POST /checkin` (`src/server.js:91-135`, `hmac(hostRow.device_key, req.rawBody)` +
`timingSafeEqual`) into a `requireDevice()` middleware, exactly as task 19 §B specifies, so both
this and the later `/harness/*` routes share it. Do not invent a second auth scheme.

**socket.io** (`socket.io@4`, new dep, mounted on the same http server) — follow task 19 §B line 71
so the two event sources land in one namespace:
- Namespace `/fleet`. Transports `['websocket','polling']` (Caddy already proxies WS).
- **Browser side (read):** on connect emit an `overview` snapshot, then `node.heartbeat`,
  `device.event`, `run.updated`, `gate.updated` deltas. Handshake auth = `FLEET_READ_KEY`.
- **Device side (write):** devices connect to the same namespace with handshake auth
  `{host, signature}` where signature is HMAC-SHA256 of the host+nonce under the `device_key`,
  validated by the same `requireDevice()` logic. Devices emit `device.event`; the server persists
  and fans out to browser subscribers.
- Room `device:<host>` for per-host subscription.

**Read API:** `GET /api/fleet/devices` (hosts + last event age + live status) and
`GET /api/fleet/devices/:host/events?limit=&since=` — gated by the optional `FLEET_READ_KEY`
(`X-Fleet-Read-Key` or `?key=`), open when unset. Extend `GET /health` with db status and last
ingest age.

**Redeploy:** `fleet.easybutt0n.ai` currently 502s — the container was pruned. Restoring it is a
prerequisite, and is already scoped as task **19b** in `cognizioware-mcp-tools`
(compose service `fleet-admin`, `build.context ./docker/fleet-admin`, Caddy site reload only,
back up `docker-compose.yml`/`Caddyfile` as `.bak-YYYYMMDD`). Coordinate rather than duplicate.

## Phase 2 — Device emitter

### 2a. Runner → supervisor bridge (`src/runner.js`, `src/server.js`)

Generalize the existing `emitCompletionEvent()` (`src/runner.js:190`) rather than writing a new
emitter — it already has the payload shape, the truncation (`.slice(-4000)`), the HMAC helper and
the fail-open `catch`:
- Rename/extend to `emitFleetEvent(task, phase)` taking `phase: 'start' | 'finish'`.
- Call it at **both** ends of every function, and crucially wire it into `/exec` / `startShell` /
  `execShell` too — today it is only reached from the agent helpers (`runner.js:1052`), which is
  why `run_command` reports nothing. The `AGENT_PROVIDERS` table and the `SHELL_NAMES` list
  (`src/mcp.js:50`) enumerate everything that must be covered:
  `run_command`, `claude_prompt`/`claude_session`, `cursor_prompt`/`cursor_session`,
  `aider_prompt`, `opencode_prompt`, the generic agents (gemini/codex/copilot/goose/amp/qwen/crush),
  `write_file`/`read_file`/`stat_file`, `get_task`/`list_tasks`/`cancel_task`.
- Target becomes the loopback bridge (`FLEET_BRIDGE_URL`, default `http://127.0.0.1:7335/events`).
  **Bind 127.0.0.1 only** — this is an unauthenticated local socket, consistent with the existing
  `harden/runner-localhost-bind-and-docs` work.
- **Secret masking before send** (new, required): mask `sk-…`, `ghp_…`, `Bearer …` in `command` and
  `output`. Task 19 §A line 59 mandates the same for the harness reporter; use one shared helper.
- Keep the existing `RUNNER_EVENT_URL` webhook path working — it is an independent feature.

### 2b. socket.io client in the supervisor (`svc/FleetAgent.cs`)

- Add `SocketIOClient` (NuGet, socket.io v4 protocol) to `svc/EasySvc.csproj` (.NET 10).
- New loopback listener in `FleetAgent` accepting the bridge POSTs, into a bounded in-memory queue
  (drop-oldest on overflow — a telemetry backlog must never wedge the supervisor).
- Persistent socket.io client to `${fleet.url}/fleet`, handshake auth signed with the device key
  from `FleetCredential.Load(_cfg.ResolvedCredFile)` (`FleetAgent.cs:47`). Reuse the existing
  `Sign()` helper (`FleetAgent.cs:270`) — same HMAC-SHA256-hex as checkin.
- Batch-drain the queue every ~2 s, emit `device.event`. Auto-reconnect with backoff; buffer while
  down, bounded. **Never throw into the supervisor loop** — the existing `RunAsync` catch pattern
  (`FleetAgent.cs:58-60`) is the model.
- Keep the existing HTTP `/checkin` loop untouched as the liveness floor and the config/restart
  directive channel. socket.io is additive.
- Config: extend `FleetConfig` (`svc/ServiceConfig.cs:29-47`) with `EventsEnabled`, `BridgePort`,
  `BatchMs`. Set `fleet.url` to `https://fleet.easybutt0n.ai` in `svc/runner.svc.json` and
  `svc/runner.desk03.svc.json` (both currently `""`).

## Phase 3 — Roll out to every device / OS

`easysvc` is already cross-platform (`svc/Installer.cs`: Windows `sc create`; Linux systemd unit,
`Type=notify`), and `FleetAgent.OperatingSystemName()` already returns `win32`/`linux`/`darwin`
(`FleetAgent.cs:285`). But **only ptait09 and ptait-desk03 run easysvc today** — htpc01 runs a plain
`htpc01-runner` systemd unit and ptait10am5 is still v1 under NSSM. Because decision 2 puts the
emitter in the supervisor, installing easysvc on those two is a hard prerequisite, not a nicety.

Per host, using `scripts/onboard-device.{ps1,sh}` (which already installs runner + hydra-agent):

| Host | OS | Work |
|---|---|---|
| **ptait09** | Windows | Already on easysvc. Checkout `main`, set `fleet.url`, enroll, restart via `svc\publish\easysvc.exe restart --config svc\runner.svc.json` |
| **ptait-desk03** | Windows | Same, with `svc\runner.desk03.svc.json` |
| **htpc01** | Linux | Largest job — see **§3a**. Needs `main` checkout, a self-contained linux-x64 easysvc build, a **new `--user` install mode in `Installer.cs`**, then cutover + enroll |
| **ptait10am5** | Windows | **Blocked: Cloudflare tunnel down.** Fix tunnel → v2 rollout (`docs/FLEET-V2-ROLLOUT.md` §B) → NSSM→easysvc → enroll. Do last; do not block the other three. |

Enrollment per host: issue a bootstrap token (`POST /admin/tokens`), then
`easysvc enroll --token <t> --url https://fleet.easybutt0n.ai`. Secrets stay out of the repo —
`.env.example` and `svc/*.svc.json` carry **names only**.

### 3a. htpc01 (Linux) — the one host that needs real migration work

**Live state, probed 2026-09-07 via `/htpc01-remote-pc`** (all verified, not assumed):

| Fact | Value |
|---|---|
| OS | Ubuntu 24.04.4 LTS, x86_64 |
| Repo | `/home/prax211/htpc01-easybutt0n-ai-git`, branch `harden/runner-localhost-bind-and-docs`, HEAD `82f7a44` |
| `svc/` and `fleet-admin/` | **Absent** — that branch predates them |
| .NET | **Not installed** (`dotnet: command not found`) |
| Supervision | **User-level** unit `~/.config/systemd/user/htpc01-runner.service`, `Restart=always`, `Linger=yes` (survives logout). Same pattern as the co-resident `hydra-agent.service`. |
| Node | Unit pins nvm **v22.22.1**; the default login shell resolves node **v24.20.0** |
| Bind | `*:7334` (all interfaces), pid under `user@1000.service/app.slice` |

**Two corrections to what's documented elsewhere:**
1. The `/htpc01-remote-pc` and `/remote-pc` skills both say restart with
   `sudo systemctl restart htpc01-runner`. That **fails** — there is no system unit. The correct
   command is `systemctl --user restart htpc01-runner`. Fix the skill docs as part of this work.
2. The `/htpc01-remote-pc` skill names the project dir `/home/prax211/ptait09-easybutt0n-ai`,
   which **does not exist** on the box. The real path is `/home/prax211/htpc01-easybutt0n-ai-git`.
3. The `ProtectHome`/`ProtectSystem` hardening I flagged earlier is in the repo *template*
   (`deploy/agentic-remote-pc.service`), **not** in the live unit — the live unit has no hardening
   directives, so the credential-write gotcha does not currently apply. It would apply if the
   template were adopted verbatim.

**The one real code change: `Installer.cs` needs a user-unit mode.** Today its Linux path
(`Installer.cs:97-127`) writes a *system* unit to `/etc/systemd/system/`. htpc01's convention —
and the reason Cursor/Claude auth and the GNOME keyring work there — is **user** units under
`~/.config/systemd/user/` with linger enabled. A system unit with `User=prax211` would lose that
session context. So add a `--user` install mode that writes to `~/.config/systemd/user/`, drives
`systemctl --user`, and asserts `loginctl show-user -p Linger` is `yes` (it already is).
Everything else is cross-platform already: `Program.cs:97-98` detects systemd,
`FleetAgent.OperatingSystemName()` (`:285`) returns `linux`.

Steps:
1. **Phase 0 first.** `cd /home/prax211/htpc01-easybutt0n-ai-git && git fetch && git checkout main`.
   This is what puts `svc/` and `fleet-admin/` on the box at all. Verify the runner still starts on
   the new code *before* touching supervision. Note the node-version gap: `main`'s `package.json`
   engines must be satisfied by the pinned nvm v22.22.1, or repoint the unit at v24.20.0.
2. **Publish easysvc self-contained** — htpc01 has no .NET and should not need one:
   `dotnet publish svc/EasySvc.csproj -c Release -r linux-x64 --self-contained -o svc/publish-linux`.
   Build on a machine with the SDK; ship the output. Mirrors how Windows uses `svc\publish\easysvc.exe`.
3. **Implement the `--user` installer mode** above; unit-test the rendered unit text.
4. **Add `svc/runner.htpc01.svc.json`** modelled on `svc/runner.desk03.svc.json`: child command =
   the nvm node absolute path + `src/server.js`, `cwd = /home/prax211/htpc01-easybutt0n-ai-git`,
   health `http://127.0.0.1:7334/health`, `fleet.url = https://fleet.easybutt0n.ai`.
5. **Cutover, in this order** — both supervisors up at once means two processes fighting for :7334:
   `systemctl --user disable --now htpc01-runner`, then
   `svc/publish-linux/easysvc install --user --config svc/runner.htpc01.svc.json`, then `start`.
   Keep `~/.config/systemd/user/htpc01-runner.service` on disk until step 7 passes — it is the
   rollback, and re-enabling it is a one-liner.
6. **Enroll:** `svc/publish-linux/easysvc enroll --token <t> --url https://fleet.easybutt0n.ai`.
7. **Verify:** `curl https://htpc01.easybutt0n.ai/health` (expect `platform: linux`);
   `easysvc logs --follow` (replacing `journalctl --user -u htpc01-runner -f`); then run a `bash`
   command through the gateway and confirm the `device.event` lands in `fleet.device_events` and on
   the `/fleet` socket — verification items 1, 3 and 6 above.

**Fallback if easysvc-on-Linux fights back:** the runner's own Node emitter (Phase 2a) can post
directly to fleet-admin instead of via the loopback bridge, using the same HMAC and the device key
read from the credential file — same wire protocol, same events, no supervisor dependency. It only
loses the "survives a runner crash" property that motivated decision 2. Given htpc01 already has
`Restart=always` supervision, this is a cheap and legitimate way to get htpc01 emitting on day one
while the easysvc migration lands behind it.

## Critical files

- `src/runner.js` (`emitCompletionEvent` :190, `EVENTS_ENABLED` :184, agent-only call site :1052)
- `src/mcp.js` (`SHELL_NAMES` :50, tool registrations :80-361 — the catalog to cover)
- `svc/FleetAgent.cs` (`RunAsync` :45, `CheckinOnce` :67, `Sign` :270, `Enroll` :186)
- `svc/ServiceConfig.cs` (`FleetConfig` :29-47, protected keys :40-44)
- `svc/SupervisorService.cs` :40-43 (where `FleetAgent` is constructed and started)
- `svc/EasySvc.csproj`, `svc/Installer.cs` (Windows `sc` :77, systemd :97-127)
- `svc/runner.svc.json`, `svc/runner.desk03.svc.json` (`fleet.url` — empty today)
- `fleet-admin/src/server.js` (`/enroll` :59, `/checkin` :91, raw body :26, sig header :30),
  `fleet-admin/schema.sql`, `fleet-admin/src/migrate.js`
- `connectors/fleet.json` (host registry), `e2e/fleet-e2e.mjs`, `svc/testlogs/mock-fleet.mjs`

## Verification

1. **Local, no fleet:** point `fleet.url` at `svc/testlogs/mock-fleet.mjs` (the existing mock) and
   `svc/fleet-test.svc.json` (`http://127.0.0.1:8787`). Run `run_command` on the host; assert a
   `start` then a `finish` event arrives with the right tool, exit code and duration.
2. **fleet-admin unit:** `node --test` in `fleet-admin/`; assert `requireDevice()` rejects a bad
   signature and that `device_events` rows persist.
3. **socket.io round trip:** connect a `socket.io-client` to `/fleet` with the read key while a
   device is connected; run `ptait09-run_command` through the gateway; assert the `device.event`
   delta arrives in browser-side < ~2 s.
4. **Masking:** run a command containing a `sk-` literal; assert the stored `command`/`output` are
   masked in Postgres.
5. **Fail-open (the important one):** stop fleet-admin, then run commands on the device. Assert
   every function still returns normally, the runner does not block, and the supervisor logs one
   WARN rather than looping hot. Restart fleet-admin; assert reconnect and buffered flush.
6. **Cross-OS:** repeat 1 and 3 on htpc01 (Linux, `bash`) after its easysvc migration. Confirm
   `platform` reports `linux` and that bash invocations emit identically.
7. **Fleet-wide:** `curl https://fleet.easybutt0n.ai/health` shows db ok + ingest age;
   `GET /api/fleet/devices` lists all live hosts with a recent event.
8. **Regression:** `npm run test:fleet` (`e2e/fleet-e2e.mjs`) and `npm run test:hydra-enroll` stay
   green — enroll/checkin is the stable contract and must not change shape.

## Risks / call-outs

- **`fleet.easybutt0n.ai` is down right now** (container pruned, 502). Phase 1 cannot be verified
  end-to-end until the 19b redeploy lands. Sequence accordingly.
- **Emitting from every function is a real volume increase.** Start/finish + 4 KB output per
  invocation, retained 14 d. Watch agent-CLI calls, which are chatty. The prune job and the
  drop-oldest queue are the two backstops.
- **Overlap with task 19.** 19a builds `/harness/*` ingest, the `/fleet` socket.io server and the
  React view. This plan needs the same socket.io server and the same `requireDevice()`. Build them
  once — ideally land Phase 1 as part of 19a rather than in parallel, or the two will collide in
  `fleet-admin/src/server.js`.
- **Phase 0 is the risky part, not the emitter.** ~100 commits of device-branch divergence get
  triaged. Do it as a reviewed PR per branch, with each host verified on `main` (health + a real
  `run_command`) before its old branch is deleted.
- **ptait10am5 stays dark** until its tunnel is fixed. It is deliberately not on the gateway.
- Windows clones: `autocrlf=false`; check `git diff --stat` before any PR.
