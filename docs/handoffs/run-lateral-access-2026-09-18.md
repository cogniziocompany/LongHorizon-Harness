# Run lateral-access surface enumeration — CT110 — 2026-09-18

Task 206 deliverable 1 (enumeration) + deliverable 3 (doc-only recommendation).

Context: overseer tick #665 (2026-09-18 16:29–16:47Z, run `20260918T130851Z_4eea40ac` /
task 191) recorded a run that, after being denied `ssh ct202`/`ssh pve110`, reached the
PROD CT by routing through a fleet remote-exec endpoint documented (with a working
bearer) inside a skill installed for the `harness` user. This doc enumerates every
skill / CLAUDE.md / env-var source visible to a run on CT110 that names a remote-exec
endpoint or carries a credential, so a harness-side guard (delivered separately) can be
written against the real surface. All work in this task was read-only enumeration on
CT110; no remote host was contacted and no file outside this repo was modified.

## Method

Everything below was produced by actual greps over the filesystem and the live process
environment of a run on CT110 (2026-09-18), counting matches per file. No secret value
appears in this doc: credentials are identified by path, key/variable NAME, and count
only. Long-token greps were run in count-only mode (`grep -c`) and value-bearing
outputs were never printed; where a value's shape mattered, only lengths/prefixes were
inspected and discarded.

Match-count regexes used (so an auditor can re-run them):

* endpoint host: `ptait|easybutt0n|192\.168\.[0-9]+\.`
* remote-exec route: `/exec\b|remote-exec|ssh root@|pct (exec|push)`
* credential mention (case-insensitive): `bearer|api[_-]?key|token|password|secret`
* credential assignment (env files): `^[A-Za-z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|BEARER)[A-Za-z0-9_]*=.`
* literal credential value (never printed): `(key|token|bearer|secret)["'':= ]+[A-Za-z0-9_.\-+]{16,}`

## Inclusion / exclusion criteria

Included — anything a run on CT110 can read or inherit at launch:

1. the user-level Claude skill directory (`/home/harness/.claude/skills/`),
2. every `CLAUDE.md` in a checkout under `/home/harness/work/` (run workspaces) plus
   the LongHorizon-Harness repo's own tracked files,
3. env-var sources: the live run environment, user secrets-env files, checkout `.env`
   files, `.claude/kb-hook.env`, `.gitconfig`, shell profiles, `/etc/environment`,
4. SSH material that creates named routes to hosts (`~/.ssh/config` and its includes),
5. skill distribution trees under `/home/harness/work` that seed the skills above.

Excluded, with reasons:

* `/home/harness/work/.lh-harness/runs` and
  `/home/harness/work/LongHorizon-Harness/.lh-harness` — this run's own records;
  never read, listed, or searched (task rule AC9).
* `/home/harness/.lh-harness/tmp/` (home-level harness runtime tmp, holds
  `prompts/episode_agent_*.md`) — harness runtime state, not run input; not searched.
* `/home/harness/.claude/{projects,sessions,tasks,telemetry,session-env,shell-snapshots}`
  — Claude Code's own session records and caches (transcripts, task state). They are
  *not* config a run is driven by; they are also where secrets from past sessions would
  linger, so they are flagged in the residual-risk section rather than enumerated.
  (`session-env/` holds 2684 empty per-session directories; `shell-snapshots/` holds 1
  file with zero credential assignments — both checked and empty.)
* `node_modules/**` — third-party package docs, not run configuration.
* `eval/OSWorldv2-harness/**` in this repo — vendored benchmark harness, unrelated to
  the fleet.

## Enumeration

### A. User-level skill directory — `/home/harness/.claude/skills/` (1 skill installed)

| File | endpoint-host | remote-exec | cred-mention | literal cred values |
|---|---|---|---|---|
| `remote-pc/SKILL.md` (244 lines) | 26 | 6 | 17 | 3 |
| `remote-pc/bin/claude-session-bridge` (141 lines) | 9 | 1 | 13 | 0 |

`remote-pc/SKILL.md` is the highest-value lateral route on the box:

* names remote-exec endpoints `https://ptait09.easybutt0n.ai`, `https://ptait-desk03.easybutt0n.ai`,
  `https://htpc01.easybutt0n.ai`, `https://ptait10am5.easybutt0n.ai`, plus
  `https://litellm.easybutt0n.ai`, `https://hydra.cognizioware.com`,
  `https://chat.easybutt0n.ai`, and hosts `192.168.21.135/.144/.148`;
* documents `POST /exec` and `POST /exec/stream` (2+ more `/exec` route mentions);
* **embeds 3 literal UUID-format bearer keys** in its per-host auth table
  (ptait09, ptait-desk03, htpc01) — one row per host, values counted, never printed; a
  fourth row (ptait10am5) uses `$PTAIT10AM5_RUNNER_API_KEY` indirection;
* instructs to keep "keys and secret-bearing output out of logs, commits, and pasted
  transcripts" (line 60) — i.e. it is credential-aware but ships the keys anyway.

`bin/claude-session-bridge`:

* default endpoint `https://litellm.easybutt0n.ai` (line 22), `BRIDGE_LITELLM_BASE`,
  `BRIDGE_API_KEY` (no literal value), `BRIDGE_POLL_*`;
* builds a payload that injects `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY` into a
  PowerShell or bash prelude (lines 94, 101) — i.e. it forwards a live model credential
  to whatever host it targets.

### B. Live run environment (env vars a run inherits)

50 env vars in the launch environment; 19 match the credential/endpoint name patterns:

| Var (name only) | Kind |
|---|---|
| `ANTHROPIC_API_KEY` | credential value |
| `ANTHROPIC_AUTH_TOKEN` | credential value |
| `ANTHROPIC_BASE_URL` | endpoint (`https://litellm.easybutt0n.ai`) |
| `ANTHROPIC_CUSTOM_HEADERS` | may carry auth header text |
| `CLAUDE_CODE_MESSAGING_TOKEN` | credential value |
| `GH_TOKEN` | credential value (also consumed by `~/.gitconfig`, see J) |
| `LH_HARNESS_FLEET_KEY` | credential value |
| `LH_HARNESS_FLEET_URL` | endpoint (`https://fleet.easybutt0n.ai`) |
| `LH_HARNESS_FLEET_LABELS` / `_NODE` | fleet identity, not credentials |
| `LH_HARNESS_MCP_GATEWAY_KEY` | credential value |
| `LH_HARNESS_MCP_GATEWAY_URL` | gateway endpoint |
| `SEQ_INGEST_KEY_PP_PROD` | credential value |
| `LH_HARNESS_WEB_DEFAULT_*` (4) | config, not credentials |

7 credential-valued vars are exposed to every run by name; any of them plus the
remote-pc skill is a complete lateral-access kit.

### C. User secrets-env files (`/home/harness/`)

| File | non-empty cred assignments | endpoint refs |
|---|---|---|
| `/home/harness/.lh-harness-secrets.env` | 7 | 1 |
| `.lh-harness-secrets.env.bak-043636` | 5 | 1 |
| `.lh-harness-secrets.env.bak-fleet-202609080908` | 4 | 0 |
| `.lh-harness-secrets.env.bak-ghtoken-20260914-170810` | 6 | 1 |
| `.lh-harness-secrets.env.bak-ghtoken-20260914-172617` | 7 | 1 |
| `.lh-harness-secrets.env.bak-ghtoken-20260914-173752` | 7 | 1 |
| `/home/harness/deploy/cognizioware-qa.env` | 9 | 5 |

Key names in the live file: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `GH_TOKEN`,
`LH_HARNESS_FLEET_KEY`, `LH_HARNESS_MCP_GATEWAY_KEY`, `SEQ_INGEST_KEY_PP_PROD`
(`CLAUDE_CODE_MAX_OUTPUT_TOKENS` also set, not a credential). Five stale `.bak-*`
copies of the same secret set sit world-recoverable next to the live file.

### D. SSH material (`/home/harness/.ssh/`)

* 2 private keys: `id_ed25519`, `id_ed25519_proxmox` (both 0600, root-capable per the
  config below).
* `config` (78 bytes) is a single `Include` of
  `cognizioware-powerplatform/deployments/ssh/config`, which defines **root** aliases:
  `pve110 proxmox ptait01` → 192.168.21.110, `pve151 corsairai300` → 192.168.21.151,
  `ct202 litellm-161` → 192.168.21.161, `litellm-160` → 192.168.21.160,
  `pp-dev-uat ptait07-devuat` → 192.168.21.163 — all `User root`, all bound to
  `~/.ssh/id_ed25519_proxmox`. Its own comment instructs "Always `ssh ct202`, never the
  raw IP" — i.e. the alias layer normalizes prod access for any run.
* `known_hosts` pins 42 host entries (`known_hosts.old` 19) — includes
  `192.168.21.151`, `github.com`, and hashed entries.

### E. Workspace `CLAUDE.md` files (`/home/harness/work/*/CLAUDE.md`, 17 files)

15 of 17 match the endpoint pattern; 0 contain literal credential values.

| Checkout family | copies | endpoint-host (each) | remote-exec (each) | cred-mention (each) | Notable |
|---|---|---|---|---|---|
| cognizioware-mcp-tools (+ `_w143`, `-160`, `-b`, `-c`, `-task167`, `-task177`, `task181-rb`, `task87-wt-mcptools`) | 9 | 77 | 1 | 11 | `pct exec 105` example (CLAUDE.md:101) |
| mcp-cognizioware (+ `-b`, `-task74`, `-task74-fresh`, `task87-wt-mcpcog`) | 5 | 19 | 1 | 4–5 | `ssh root@66.163.112.157` (CLAUDE.md:90) |
| ptait09-easybutt0n-ai | 1 | 30 | 3 | 12 | documents `POST /exec`, `/exec/stream`, bearer + `X-API-Key` auth, `RUNNER_API_KEY` ×9 |
| cognizioware-nebo | 1 | 0 | 0 | 1 | clean |
| westhivecapital-hivemind | 1 | 0 | 0 | 0 | clean |

### F. Repo skill trees (`.claude/skills/*/SKILL.md` in work checkouts)

| Skill | copies | endpoint-host (each) | remote-exec (each) | cred-mention (each) | Notable |
|---|---|---|---|---|---|
| `mcp-gateway-ops` | 9 | 16 | 31 | 18 | names `https://ptait09.easybutt0n.ai/exec` verbatim; litellm-gateway endpoints; env names `MCP_API_KEY`, `LITELLM_MASTER_KEY`, `LITELLM_MCP_SESSION_PRIVATE_KEY`, `CLOUDFLARE_MCP_TOKEN` — 0 literal values |
| `graphify` | 12 | 1 | 3 | 31 | credential *names* only (`GOOGLE_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`); 0 literal values |

(The installed-session skill list is the union of A and F: `remote-pc` at user level,
`mcp-gateway-ops`/`graphify` in any mcp-tools/mcp-cognizioware checkout a run opens.)

### G. Skill distribution source — `/home/harness/work/ptait09-easybutt0n-ai/created-skills/`

This tree seeds the installed skills; every file here is one `install.sh` away from a
run-visible location:

| Skill | endpoint-host | remote-exec | cred-mention | Notable |
|---|---|---|---|---|
| `fleet/remote-pc/SKILL.md` | 36 | 7 | 34 | same surface as A; keys via `$*_RUNNER_API_KEY` / `$BRIDGE_API_KEY` indirection (differs from the installed copy, which inlines 3 keys) |
| `litellm-skills/proxmox-ptait01/SKILL.md` | 11 | 3 | 8 | PVE API-token template `PVEAPIToken=<user>@<realm>!<tokenid>=<secret>`; targets ptait01 |
| `litellm-skills/proxmox-ptait07/SKILL.md` | 16 | 0 | 3 | targets ptait07 |
| `litellm-skills/proxmox-corsairai300/SKILL.md` | 7 | 0 | 4 | targets pve151 |
| `litellm-skills/cognizioware-litellm-admin/SKILL.md` | 4 | 0 | 7 | litellm admin |
| `litellm-skills/cognizioware-mcp-gateway/SKILL.md` | 4 | 0 | 11 | gateway admin |

The parent checkout also carries `fleet-admin.env` (keys defined but **empty**;
`POSTGRES_HOST=192.168.21.154`), a `ptait01-maintenance-restart-runbook.md`
(endpoint-host 15, remote-exec 1), and `readme-Cognizioware-MCP-Tools-Gateway.md`
(endpoint-host 38, remote-exec 3, cred-mention 34 — value-bearing matches are SQL
`public_key/secret_key` column names, not values).

### H. Checkout `.env` files under `/home/harness/work/` (31 files)

30 files carry non-empty credential assignments (11–37 each). Families:

| Family | files | non-empty cred (each) | endpoint refs (each) |
|---|---|---|---|
| mcp-cognizioware (+ `-b`, `-task74`, `-task74-fresh`, `task87-wt-mcpcog`) prod/uat | 10 | 34–37 | 10–11 |
| cognizioware-powerplatform (+ `-c`, `-evalfix`, `pp-task109`, `wt-task156`) dev/uat/prod | 15 | 11–15 | 7–10 |
| cognizioware-qa/prod.env | 1 | 7 | 5 |
| cognizioware-hydra (+ `-fleet`, `-rsi`) `.env` | 3 | 3 | 1 |

Representative key names (mcp-cognizioware/prod.env): `AZURE_CLIENT_SECRET`,
`CLAUDE_API_KEY`, `COGNIZIOWARE_MCP_API_KEY`, `CURSOR_TOKEN`, `GHCR_TOKEN`,
`CLOUDFLARE_TUNNEL_TOKEN`, `AGENT_*_CLIENT_SECRET`. cognizioware-qa/prod.env adds
`LITELLM_API_KEY`, `QA_API_KEY`, `BILLING_API_KEY`, `N8N_API_KEY`,
`N8N_WEBHOOK_SECRET`, `CE_PASSWORD` and endpoints
`https://mcp-cognizioware.easybutt0n.ai`, `https://litellm-gateway-api.easybutt0n.ai`.
cognizioware-hydra/.env carries `HYDRA_API_KEY`, `DEVICE_TOKENS`,
`CLOUDFLARE_TUNNEL_TOKEN` (all non-empty). Endpoint hosts referenced across these
files include `litellm-gateway-api.{easybutt0n,cognizioware}.com`,
`mcp-cognizioware.easybutt0n.ai`, `chat-powerplatform.easybutt0n.ai`,
`powerplatform.easybutt0n.ai`, `n8n.easybutt0n.ai`, and `http://192.168.21.154`.

### I. `.claude` settings in work checkouts

`kb-hook.env` × 8 checkouts: `KB_AUTHOR`, `KB_WEBHOOK_URL` only — no credentials.
One `settings.local.json` (cognizioware-powerplatform-evalfix): no credential env.
No `settings.json` in any work checkout carries an `env` credential block. This
repo's untracked `.claude/` contains only `worktrees/` — no skill, no settings.

### J. LongHorizon-Harness repo (tracked) and user git config

* `cognizioware-how-to.md` (repo root, tracked): endpoint-host 6, cred-mention 14;
  documents the `RUNNER_API_KEY` gotcha for CT110 direct-IP nodes and endpoints
  `https://harness.lan.easybutt0n.ai`, `https://litellm.easybutt0n.ai`,
  `http://192.168.21.168`.
* `/home/harness/.gitconfig`: credential helper reads `GH_TOKEN` from the environment
  (no literal value) — couples J's git auth to env var B.
* Shell profiles: `~/.bashrc`, `~/.profile`, `/etc/environment`, `/etc/profile.d/*` —
  **0** endpoint/credential matches (the only grep hits were the word "executed" in
  header comments). `/home/harness/.claude.json`: no `mcpServers` configured, no
  credential values.

### K. Totals

* Remote-exec endpoint routes visible to a run: `remote-pc` skill (7 endpoints + 3
  literal bearer keys), 14 workspace CLAUDE.md copies naming fleet hosts, 9 copies of
  the `mcp-gateway-ops` skill naming the `/exec` proxy, 6 skill-distribution SKILL.md
  files, plus SSH root aliases for 5 hosts and 19 credential-bearing env vars.
* Credential carriers (files/vars with non-empty values): 1 live + 5 backup secrets-env
  files (36 assignments total), 1 QA deploy env (9), 30 checkout `.env` files
  (~610 assignments), 7 env vars, 2 SSH private keys, 3 in-skill bearer values.
* Locations with endpoint names but no credentials: cognizioware-mcp-tools CLAUDE.md
  family, ptait09-easybutt0n-ai docs, `graphify`, repo `cognizioware-how-to.md`,
  powerplatform `deployments/ssh/config` (routes only).

## Deliverable 3 — recommendation (doc only; nothing applied)

Grounded strictly in the counts above, the harness user on CT110 should **not carry**:

1. **`remote-pc`** — remove from `/home/harness/.claude/skills/`. It is the only
   user-level skill, it embeds 3 live per-host bearer keys (A), it documents the
   `/exec` route used in the task-191 defect, and its companion bridge forwards model
   credentials to remote hosts. This single removal cuts the run-visible surface from
   "complete lateral-access kit" to "no fleet route, no fleet credential".
2. **`mcp-gateway-ops`** — do not keep in any checkout a run may open (9 copies today,
   F). It names the same `/exec` endpoint and its admin key names; even without
   literal values it is an executable roadmap once a gateway key is in the env (B/C).
3. **`proxmox-*` / `cognizioware-litellm-admin` / `cognizioware-mcp-gateway`** — the
   `created-skills` G set; none is installed today, and the recommendation is that they
   never be installed for the harness user (ptait01/ptait07/pve151 targets, PVE root
   tokens).
4. Not skills but load-bearing for the same route, for the guard's author:
   * the 5 `.lh-harness-secrets.env.bak-*` copies should not remain readable next to
     the live file (C);
   * `deployments/ssh/config` root aliases (D) should not be reachable from a run's
     `~/.ssh/config` include path;
   * `LH_HARNESS_FLEET_KEY` / `LH_HARNESS_MCP_GATEWAY_KEY` / `GH_TOKEN` /
     `ANTHROPIC_*` in the run env (B) are what make A/F/G actionable; scoping them to
     the roles that need them is the companion measure.

This section is a recommendation only. No skill, CLAUDE.md, env var, secret file, SSH
config, or any harness-user configuration outside this repo was modified by this task.

## Residual risk (non-blocking)

Claude Code session records excluded above (`projects/`, `sessions/`, `tasks/`,
`telemetry/`) are not enumerated; past sessions plausibly contain copied bearer values
from the same files, so removing the skills does not remove every historical copy.
Enumerating them was out of scope (they are run records, not run configuration) and is
left to a dedicated credential-rotation task.