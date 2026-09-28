# Cognizioware How-To — LongHorizon-Harness on our infra

Internal operating manual for OUR two deployments of this fork, the modifications we carry on
top of upstream (AMAP-ML), and every gotcha we have paid for so far. Written 2026-08-30 after
the first night of real multi-run production. Companion file:
`tasks/TEMPLATE-litellm-qwen-local.md` (the run template). Deep dives live as KB articles in
the `billingservice` knowledge base (ids noted inline; process: `kb-article/kb-article.md`).

---

## 1. Mental model (read this first)

- The harness **never calls LLMs itself**. It spawns agent CLIs (`claude`, `codex`, …) as
  fresh child processes per role turn. All LLM routing happens via env vars injected into
  those children (`ANTHROPIC_BASE_URL` → our LiteLLM router).
- `lh-harness web` is a **supervisor**: one process owns the worker process of every run
  created through it, serves the dashboard, and is the control plane
  (create/stop/resume/instruct/approve over HTTP).
- **One supervisor, many runs, each run in its own workspace.** Runs from different Claude
  sessions/operators coexist on one server. Nothing crosses between workspaces — each spawned
  CLI is confined to its run's workspace path. What IS shared: the server process itself and
  the runs-root directory (`<workspace-root>/.lh-harness/runs/<run-id>/`).
- Roles run strictly sequentially per run: Manager → Executor → Auditor, round by round.
  The auditor is read-only and its verdict gates the round.

## 2. Our two deployments

| | WSL (ptait09) | CT110 (corsairai300 LXC) |
|---|---|---|
| Install | `~/wsl-test/.venv`, **pip -e** from `/mnt/c/.../LongHorizon-Harness` (edits to the repo apply on next worker spawn) | PyPI wheel at `/home/harness/venv` + our changed files hand-patched in |
| Server | `lh-web` bash function (see §4) on `127.0.0.1:8799`, no token | `lh-harness.service` (systemd, user `harness`) on `:8799`, bearer token, fronted by Caddy → `https://harness.lan.easybutt0n.ai` |
| Workspace root | `/mnt/c/Users/PaxtonTait/source` (all repos under it selectable) | `/home/harness/work` (git-clone repos in; account-wide deploy key installed) |
| Agents' claude | native Linux claude via nvm (`~/.nvm/.../bin`) | `/home/harness` npm claude |
| Filesystem | DrvFS (`/mnt/c`) — slow I/O, **unstable stat cache** (see §6.3) | ext4 — sane |

Never run task execution on native Windows: `LocalEnvironment.exec` uses
`os.killpg`/`SIGHUP` (108 test failures on win32; 405 pass on Linux).

### 2b. Third deployment shape: `lh-harness-node` Docker (Hydra-managed, 2026-09-05)

`docker/` in this repo builds **lh-harness-node** — this exact build in a
container (py3.12 + node22 + claude CLI), entrypoint-seeded with the CT110
config values, web API on `127.0.0.1:8799` only. The **cognizioware-hydra**
device agent installs/restarts it on fleet devices (`harness_install` /
`harness_restart`) and proxies `/api/*` over its outbound WS (`harness_http`)
— no harness port is ever exposed on the network. Windows devices run it via
Docker Desktop/WSL2, which is what makes Windows a supported placement.

Build command (run from repo root):

```bash
docker build -f docker/Dockerfile -t lh-harness-node:latest .
```

The `Dockerfile` installs the harness from the repo checkout via `pip install`
so the image is a snapshot of the tree at build time. The entrypoint
materializes a fleet-default `config.toml` from the harness's own
`CONFIG_TEMPLATE` (never inventing keys) and then serves the web control plane
with the bearer token from `LH_HARNESS_WEB_TOKEN`.

Fleet orchestration (placement, migration, offload, aggregated monitoring)
lives in Hydra's `/fleet/*` REST + `/fleet-mcp` MCP (alias `hydrafleet`,
access group `fleet-runners`); CT110 stays registered as the `external`
primary + fallback node. See `docker/README.md` and
`tasks/TEMPLATE-overseer-hierarchy.md` §Addressing.

Operational notes for Docker nodes:
- `POST /api/runs/{id}/resume {"mode":"continue"}` now requires
  `cancelReasonAck` when the run is `cancelled`. Supply a short operator note
  acknowledging the cancellation reason, e.g.:
  `{"mode":"continue","cancelReasonAck":"operator reviewed"}`.
- Run inventory that cannot be read is reported as `unknown` rather than
  `idle`; `unknown` is non-terminal and never treated as a zero-state.
- Migration/successor creation waits until the predecessor worker reaches a
  terminal lifecycle status (`completed`, `failed`, `cancelled`, `blocked`,
  `incomplete`). Do not assume the manager `report.json` alone is enough.
- Each recorded round writes a `checkpoint.json` with a SHA-256 fingerprint.
  `POST /api/runs/{id}/resume {"mode":"continue"}` validates the latest
  checkpoint before relaunching the worker; a missing or mismatched fingerprint
  rejects the resume with 409.

## 3. Our modifications on top of upstream

| Commit | What | Why |
|---|---|---|
| `a4573ad` | Proxy-aware model catalog (`model_catalog.py` queries LiteLLM `/v1/models`), env-driven role defaults (`LH_HARNESS_WEB_DEFAULT_{AGENT,MODEL,AUDITOR_MODEL}`), English prompt default (`App.tsx` was `'zh'`) | Dropdowns show our 46 router models instead of hardcoded Anthropic aliases |
| `ff86f94` | `--strict-mcp-config` always passed to spawned claude | Workspace `.mcp.json` MCP inventories push built-in tool schemas out of local models' context → hallucinated tool lists (KB 2368) |
| `11de682` | `episode_env()` hook → `ANTHROPIC_CUSTOM_HEADERS: x-litellm-tags: lh-run/<id>,round_N,<role>` | Per-run Langfuse traces via the router's per-key logging (filter by `lh-run/<run_id>`; kimi shows $0.00 cost — no price mapping, tokens accurate) |
| `f165dc6` | Guard ignores `.git` dir record, `.git/index`, `.git/FETCH_HEAD`, `.git/*.lock`; `LH_HARNESS_WEB_DEFAULT_MANAGER_MODEL`; the run template | Read-only `git diff` rewrites the stat cache on DrvFS → every honest audit self-invalidated (KB 2369) |

## 4. Starting / restarting servers

- **WSL**: `lh-web /mnt/c/Users/PaxtonTait/source` (function in `~/.bashrc` — injects the
  LiteLLM key + role defaults). **Always pass the root**: bare `lh-web` roots at the current
  dir; from `~` the dashboard shows "No tasks yet" while your runs are fine elsewhere.
- **One server per port.** If one is live, a second attempt fails harmlessly
  (`address already in use`). If the first has died, the second TAKES the port and orphans
  every in-flight worker. Check first: `pgrep -af "lh_harness web"` + `readlink /proc/<pid>/cwd`.
- **Restarting a supervisor (or CT110's `systemctl restart lh-harness`) kills all its
  in-flight runs.** They recover cleanly: `POST /api/runs/<id>/resume {"mode":"continue"}` —
  ledger and audited progress survive. Do it deliberately, never casually.
- The `~/.bashrc` PATH fix block must stay LAST in the file: an absolute `export PATH=` line
  above it drops the nvm bin dir, WSL interop then resolves `claude` to the **Windows** npm
  shim, and your "Linux" run silently executes on Windows against real Anthropic (we hit
  this; the smoking gun is `"cwd":"C:\\..."` in `claude_stream.jsonl`).

## 5. Creating runs (API)

```json
POST /api/runs
{ "task": "...", "agent": "claude_code",
  "model": "kimi-k2.7-code:cloud",          // REQUIRED even with roles (see gotcha)
  "workspace": "<abs path>", "max_rounds": 25, "prompt_language": "en",
  "roles": { "manager":  {"agent":"claude_code","model":"kimi-k2.7-code:cloud"},
             "executor": {"agent":"claude_code","model":"qwen3.8"},
             "auditor":  {"agent":"claude_code","model":"kimi-k3:cloud"} } }
```

- **Top-level `model` is required**: omit it and the workspace `config.toml` model leaks into
  the worker, which dies in ~4s with `supervised run model does not match its reservation`.
- Model trio rationale + full task-description checklist: `tasks/TEMPLATE-litellm-qwen-local.md`
  (KB 2367). Short form: kimi-k2.7 manager (~20s plans), qwen3.8 executor (the dual-3090
  payoff, ~43 tok/s), kimi-k3 auditor (only family that reliably emits the 3-line control
  header). **Never** minimax-m3 as executor — it can't see tool results through LiteLLM and
  confabulates "environment broken" (its raw stream shows every command succeeding).
- Mid-run control: `POST /api/runs/<id>/instructions {"text":"..."}` (reaches the manager
  next round), `POST .../approvals/<aid>/resolve`, `POST .../stop`, `POST .../resume`.
- Audit verdict grammar: `complete|incomplete / clean|violation / aligned|needs_revision|unknown`.
  `incomplete/clean/aligned` is a HEALTHY mid-milestone verdict.

## 6. Per-workspace config (`<workspace>/.lh-harness/config.toml`)

qwen3.8 does honest 20–60 min executor rounds; the 1800s default kills them mid-flight.
Auditors that run builds/tests write churn files and trip the read-only guard. So every
workspace we run in gets:

```toml
[run]
guard_exclude_paths = ["node_modules", "dist", "build", "logs"]  # + repo-specific churn
[run.timeouts]
manager = 300
gui_executor = 3600
cli_executor = 3600
auditor = 600
```

1. Loaded at **worker start** — after editing, `stop` + `resume {"mode":"continue"}` to apply.
2. `guard_exclude_paths` must be under `[run]`, NOT `[run.timeouts]` (a misplaced key crashes
   the worker: `unknown timeout role(s)`). `.git` excludes are rejected by design — that noise
   is handled in code (§3, `f165dc6`).
3. Guard-only violations fail the ROUND, not the run; the guard still catches real mutations
   (a straggling executor commit landing mid-audit was correctly flagged on CT110). KB 2369.
4. Do NOT set `model`/`agent` here for supervised (web-created) runs — the supervisor passes
   them explicitly and mismatches kill the reservation check.

## 7. Triage runbook (what actually broke, in one table)

| Symptom | Cause | Fix |
|---|---|---|
| Dashboard "No tasks yet" but runs exist | Browser cache/stale run-id — IF `curl /api/runs` shows them from both WSL and Windows and server cwd is right | Ctrl+Shift+R; then clear site data. **Do not restart the shared server** (KB 2372) |
| Same, but API really returns `[]` | A second `lh-web` took the port from the wrong cwd | Kill it; start with the correct `--workspace-root`; `resume` the orphaned runs |
| "model does not match its reservation" (exit 2, ~4s) | No top-level `model` in POST /api/runs | Add it |
| Executor "I don't have a Bash tool" + fake tool list | Context starvation: num_ctx < ~32K, or workspace `.mcp.json` inventory (pre-`ff86f94`) | 64K lane + `--strict-mcp-config` (KB 2368) |
| Executor claims all commands return empty | minimax-m3 via LiteLLM (can't see tool_results) | Change executor model; trust `claude_stream.jsonl`, not the agent's prose |
| `audit=blocked/violation`, "auditor changed workspace files" | Build churn or `.git` stat cache | §6 excludes / `f165dc6`; real mid-audit commits are legit catches |
| Executor `failed · 1800.3s` (exactly the timeout) | Honest long qwen3.8 round | Raise timeouts (§6) + cycle; if it hits 3600 too, instruct the manager to decompose smaller rounds instead of raising further |
| Executor still hits 3600 after decomposition; many runs concurrent | GPU contention: N concurrent qwen executors → ~55-65s/step; wall-clock pacing alone can't save a 50-step round | Escalate in order: (a) HARD step budget instruction — "under 40 tool steps, stop early, commit+report; an honest partial PASSES the round"; (b) raise executor timeouts to 5400 + cycle (done fleet-wide 2026-08-30) |
| `Integrity: violation`, changed path `.git/objects`, but wrapped report says complete/clean | The AUDITOR ran `git fetch`/`git pull` — network git ops write .git/objects and self-invalidate the audit. Do NOT exclude .git/objects (it catches real mid-audit commits) | Instruct managers to paste into every subtask: "AUDITOR CONSTRAINT: local read-only commands only; never git fetch/pull; judge ahead/behind from local refs" |
| Hundreds of "modified" files with symmetric +N/-N line counts | CRLF/LF flapping between Windows and WSL git on a DrvFS workspace | Phantom — never `git add -A`; stage intended files by explicit path and verify `git diff --cached --stat` before committing |
| Run `cancelled`, `commands.jsonl` shows `created_by:"web"` stop | A human clicked Stop in the dashboard | Ask, don't auto-restart |
| kimi/glm "issue with the selected model" + Anthropic rate-limit events | Windows claude shim executed (PATH bug, §4) | Fix PATH block; kill the poisoned server; resume |
| CT fresh clone can't resolve DNS | New CTs inherit the PVE host's Tailscale resolv.conf | `pct set <id> --nameserver 192.168.21.3 --searchdomain lan.easybutt0n.ai` + rewrite resolv.conf |

## 8. Observability & knowledge trail

- **Langfuse**: filter by tag `lh-run/<run_id>` (per-key logging on the lh-harness virtual
  key; global router callbacks stay forbidden — per-key is the sanctioned pattern).
- **Raw truth** for any dispute about what an agent did:
  `<runs-root>/<run-id>/lh_harness/*_episodes/ep*/claude_stream.jsonl` (tool_use/tool_result
  pairs) and `role_orchestration/rounds/round_N/auditor_report.txt`.
- **KB articles** (billingservice DB, see `kb-article/kb-article.md`): 2367 model trio ·
  2368 tool-calling truncation · 2369 guard violations · 2370 cross-host session bridge ·
  2371 runner restart/key rotation · 2372 workbench ops · 2373 Monday token scope. File new
  stable findings there, never credentials.

## 9. Fleet integration notes

- **LiteLLM router**: `https://litellm.easybutt0n.ai` (CT202). Harness uses the scoped
  virtual key (alias `lh-harness`); adapter strips a trailing `/v1` from base URLs. Router
  restarts must use `docker compose --env-file ...` — never bare `docker compose up`.
- **Agents' extra powers**: both harness users carry `~/.claude/skills/remote-pc/` (fleet
  runner HTTP API + `bin/claude-session-bridge <GUID> "<q>"` for talking to Claude sessions
  on other hosts — async MUST be the `?async=true` query param, full `claude.cmd` path for
  SYSTEM, KB 2370/2371).
- **CT110 repo access**: account-wide GitHub deploy key (acts as prax211 — read AND write
  everywhere; upgrade to a machine user if autonomous pushes start). New project =
  `git clone` into `/home/harness/work/<name>`, then select it as the run's Workspace.
- **CT110 API auth**: the bearer the dashboard/API expects is read from the env field
  **`LH_HARNESS_WEB_TOKEN`** (equivalently `--auth-token`), set in the `lh-harness.service`
  environment on CT110; current value starts `f8b…`. Send it as
  `Authorization: Bearer <token>` — never `?token=`, which leaks into access logs. Workers
  never inherit it (`LH_HARNESS_WEB_TOKEN` is stripped from child env, see
  `environment/local.py` / `supervisor/service.py`). Full value stays out of git: read it
  with `systemctl show lh-harness -p Environment` on CT110 (other secrets live in the unit's
  `EnvironmentFile`, `/home/harness/.lh-harness-secrets.env`).
- **Hydra fleet plane (live 2026-09-05)**: CT110 is registered in the Hydra orchestrator
  (corsairai300, `/opt/cognizioware-hydra/.env` → `HARNESS_NODES_JSON`) as the `external`
  primary + fallback node by **direct IP** `http://192.168.21.168:8799` — corsairai300
  cannot resolve `harness.lan.easybutt0n.ai`. Overseers use the gateway alias
  `hydrafleet` (`/fleet-mcp`, group `fleet-runners`) or REST `/fleet/*`; e2e suites 29/31
  in cognizioware-mcp-tools cover it. `lh-harness-node:latest` (this repo's `docker/`) is
  built on corsairai300 for managed nodes; device agents still need the `harness_*`
  handlers rolled out before a managed install can be placed.
- **Hivemind capture**: a cron on corsairai300 (`hivemind-ingest.sh`, every 5 min) writes
  every CT110 run snapshot + fleet intervention into `hivemind_sessions.session_memories`
  (CT103 agent-db, pgvector) via memory-mcp; recall with the gateway alias `memory`.
- **Gotcha**: Hydra `auth.js` reads `RUNNER_API_KEY || HYDRA_API_KEY`; a user-level
  `RUNNER_API_KEY` on a dev box overrides `HYDRA_API_KEY` when booting the orchestrator
  locally (smoke tests return 401 until you override it).
