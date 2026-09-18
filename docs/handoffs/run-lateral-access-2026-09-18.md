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
* literal credential value (never printed): `(key|token|bearer|secret)["'':= ]+[A-Za-z0-9_.+-]{16,}`
  (two notes for re-running: the quoted class carries one `'` — `''` in this
  markdown is a doubling of that same character, not two distinct characters;
  and the `-` in the second class must stay unescaped next to `+`, since
  `\-.`+ forms an invalid range in ugrep 7.8.4 and errors with "Invalid
  range end").
* UUID bearer-value scan: `[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}`

Counting mode: tables report **occurrence counts** (`grep -oE … | wc -l`) for the
endpoint-host, remote-exec and credential-mention regexes, because one line can
carry more than one match (e.g. `https://ptait09.easybutt0n.ai/exec` matches the
endpoint regex twice on a single line). The credential-mention regex is always
case-insensitive; endpoint-host and remote-exec are case-sensitive. The literal
credential regex is applied case-insensitively and reported as matching-line
counts. File lists were produced with `find` pruning any directory named
`.lh-harness` or `node_modules` (harness-owned paths are never traversed), and
the vendored `eval/OSWorldv2-harness/` tree is excluded per the criteria above.

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

| File | endpoint-host (occ) | remote-exec (occ) | cred-mention (occ) | literal cred lines | UUID bearer values |
|---|---|---|---|---|---|
| `remote-pc/SKILL.md` (244 lines) | 44 | 6 | 18 | 0 | 3 |
| `remote-pc/bin/claude-session-bridge` (141 lines) | 14 | 1 | 19 | 3 | 3 |

In-skill literal bearer-value total: **6** (3 in `SKILL.md` + 3 in the bridge; the
two sets are distinct values). Matching-line counts for the same files are
26/6/17 (SKILL.md) and 9/1/13 (bridge); both modes are given so an auditor can
reproduce either.

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
* `host_key()` (lines 44–46) resolves per-host runner keys with
  `${PTAIT09_RUNNER_API_KEY:-<uuid>}` style fallbacks — **3 embedded UUID bearer
  keys for ptait09, ptait-desk03 and htpc01, distinct from the three in
  `SKILL.md`** (i.e. 6 distinct in-skill bearer values across the skill);
* builds a payload that injects `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY` into a
  PowerShell or bash prelude (lines 94, 101) — i.e. it forwards a live model credential
  to whatever host it targets.

### B. Live run environment (env vars a run inherits)

50 env vars in the launch environment (stable across `bash -lc` / `bash -c` /
`sh -c` / `env`); 5 lines match the credential-mention regex, 2 match the
endpoint-host regex, and 8 variable names match the credential-assignment regex
(7 of them are credential-valued; `CLAUDE_CODE_MAX_OUTPUT_TOKENS` is token-shaped
config, not a credential):

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

(A repair-pass note: an earlier draft recorded this section as "50 vars /
19 matches" and one audit re-run recorded "64 vars"; both were re-derived for this
revision with the documented regexes over the launch environment itself — 50 vars,
5 credential-mention matches — and the 50 figure is stable across every shell
invocation mode tested.)

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
* `known_hosts` pins 43 host entries (`known_hosts.old` 19) — includes
  `192.168.21.151`, `github.com`, and 40 hashed entries.

### E. Workspace `CLAUDE.md` files (recursive under `/home/harness/work/`, 42 files)

Recursive enumeration (pruning `.lh-harness` and `node_modules`): **42 `CLAUDE.md`
files, 20 endpoint-bearing**, 0 with literal credential values. Of these, 17 sit at
a checkout top level (`/home/harness/work/*/CLAUDE.md`), 15 of them
endpoint-bearing; the remaining 25 are nested copies — 19 zero-match
`.claude/CLAUDE.md` duplicates, 1 zero-match `baseline/` copy (cognizioware-nebo),
and 5 nested endpoint-bearing copies counted in the families below (the `_w181`
mcp-tools copy, the `.claude/worktrees/model-usage-audit` mcp-tools copy, the
`mcp-cognizioware-b/.worktree-ci` copy, and the two `_w185` copies — one
mcp-cognizioware, one ptait09). Bracketed figures are the top-level-only view
(top-level `CLAUDE.md` copies per family: mcp-tools 9, mcp-cognizioware 5,
ptait09 1, nebo 1, westhive 1).

| Checkout family | copies (recursive) | endpoint-host (occ / lines, each) | remote-exec (each) | cred-mention (each) | Notable |
|---|---|---|---|---|---|
| cognizioware-mcp-tools (+ `_w143`, `-160`, `-b`, `-c`, `-task167`, `-task177`, `task181-rb`, `task87-wt-mcptools`) | 11 [9] | 123 occ / 79 lines | 1 | 11 | `pct exec 105` example |
| mcp-cognizioware (+ `-b`, `-task74`, `-task74-fresh`, `task87-wt-mcpcog`) | 7 [5] | 26 occ / 19 lines | 1 | 4 (5 in the main copy) | `ssh root@66.163.112.157` (CLAUDE.md:90) |
| ptait09-easybutt0n-ai | 2 [1] | main: 51 occ / 30 lines; nested: 6 occ / 2 lines | 3 main / 2 nested | 12 main / 1 nested | documents `POST /exec`, `/exec/stream`, bearer + `X-API-Key` auth, `RUNNER_API_KEY` ×9 |
| cognizioware-nebo | 2 [1] | 0 | 0 | 1 | clean |
| westhivecapital-hivemind | 2 [1] | 0 | 0 | 0 | clean |

### F. Repo skill trees (`.claude/skills/*/SKILL.md` in work checkouts, recursive)

| Skill | copies | endpoint-host (occ, each) | remote-exec (occ, each) | cred-mention (occ, each) | Notable |
|---|---|---|---|---|---|
| `mcp-gateway-ops` | 11 | 20 (16 lines) | 19 (13 lines) | 24 (18 lines) | names `https://ptait09.easybutt0n.ai/exec` verbatim; litellm-gateway endpoints; env names `MCP_API_KEY`, `LITELLM_MASTER_KEY`, `LITELLM_MCP_SESSION_PRIVATE_KEY`, `CLOUDFLARE_MCP_TOKEN` — 0 literal values |
| `graphify` | 19 (+11 under `.agents/skills/`, same content) | 2 (1 line) | 0 | 51 (31 lines) | credential *names* only (`GOOGLE_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`); 0 literal values; sole endpoint mention is a `ptait09-easybutt0n-ai` repo name in a fleet-scripts note |

All 11 `mcp-gateway-ops` copies are byte-identical in counts (they live only in
cognizioware-mcp-tools-family checkouts, including one nested
`.claude/worktrees/model-usage-audit` copy); all `graphify` copies are likewise
identical. This repo's own tracked `SKILL.md` files (18 vendored eval copies +
1 venv copy) carry 0 endpoint matches. Matching-line counts: gateway-ops
16/13/18, graphify 1/0/31.

(The installed-session skill list is the union of A and F: `remote-pc` at user level,
`mcp-gateway-ops`/`graphify` in any mcp-tools/mcp-cognizioware checkout a run opens.)

### G. Skill distribution source — `/home/harness/work/ptait09-easybutt0n-ai/created-skills/`

This tree seeds the installed skills; every file here is one `install.sh` away from a
run-visible location. It holds exactly 6 SKILL.md files:

| Skill | endpoint-host (lines / occ) | remote-exec | cred-mention (lines / occ) | Notable |
|---|---|---|---|---|
| `fleet/remote-pc/SKILL.md` (385 lines) | 36 / 56 | 7 | 34 / 39 | same surface as A; keys via `$*_RUNNER_API_KEY` / `$BRIDGE_API_KEY` indirection (0 UUID literals — differs from the installed copy, which inlines 3) |
| `litellm-skills/proxmox-ptait01/SKILL.md` | 11 / 16 | 3 | 8 / 11 | PVE API-token template `PVEAPIToken=<user>@<realm>!<tokenid>=<secret>`; targets ptait01 |
| `litellm-skills/proxmox-ptait07/SKILL.md` | 16 / 25 | 0 | 3 / 6 | targets ptait07 |
| `litellm-skills/proxmox-corsairai300/SKILL.md` | 7 / 11 | 0 | 4 / 7 | targets pve151 |
| `litellm-skills/cognizioware-litellm-admin/SKILL.md` | 4 / 4 | 0 | 7 / 7 | litellm admin |
| `litellm-skills/cognizioware-mcp-gateway/SKILL.md` | 4 / 6 | 0 | 11 / 13 | gateway admin |

The parent checkout also carries `fleet-admin.env` (credential keys defined but
**empty** — 0 non-empty assignments; `POSTGRES_HOST=192.168.21.154`; names include
`FLEET_ADMIN_KEY`, `FLEET_READ_KEY`, `POSTGRES_PASSWORD`, `YOUTRACK_TOKEN`;
1 endpoint line). Per-host variant skills (`ptait09-remote-pc`, `*-runner` SKILL.md
files) exist in *other* checkouts' `created-skills` trees (e.g.
cognizioware-mcp-tools), not here. Two mcp-tools-family docs round out the
run-visible skill-adjacent surface, at 11 copies each:
`ptait01-maintenance-restart-runbook.md` (endpoint-host 15 lines / 17 occ,
remote-exec 1, cred-mention 0) and `readme-Cognizioware-MCP-Tools-Gateway.md`
(endpoint-host 38 lines / 49 occ, remote-exec 3, cred-mention 34 lines / 44 occ —
value-bearing matches are SQL `public_key/secret_key` column names, not values).

### H. Checkout `.env` files under `/home/harness/work/` (recursive, 135 files)

135 env-named files (pruning `.lh-harness` and `node_modules`); 121 carry at least
one non-empty credential assignment; **1012 non-empty assignments and 486
endpoint-ref lines in total**. Families (assignments counted with the
credential-assignment regex):

| Family | files | non-empty cred assignments (total) | per-copy values | endpoint refs (total) |
|---|---|---|---|---|
| mcp-cognizioware (+ `-b`, `-task74`, `-task74-fresh`, `task87-wt-mcpcog`; includes `deployments/*.env` carriers) | 99 | 737 | prod.env 35 / uat.env 37 per copy; `deployments/cognizioware-litellm/litellm.env` 7, `litellm-v2.env` 6, `cognizioware04-phx/prod-stack.env` 7, 4× `*-inference.env` 2, `n8n-mcp-uat/*.env` 2+2, `deployments/tier/.env` 2, `cloudflare/{dev,uat}.env` 1+1, `seq/nas.env` 1 | 276 (prod.env 10, uat.env 11 per copy) |
| cognizioware-powerplatform (+ `-c`, `-evalfix`, `pp-task109`, `wt-task156`) dev/uat/prod | 25 | 223 | dev.env 11 / uat.env 11 / prod.env 15 per copy (`-evalfix` adds `.evalfix.env` 1) | 163 (dev 10, uat 7, prod 9 per copy) |
| cognizioware-qa/prod.env (+ `_w185` copy) | 2 | 14 | 7 per copy | 10 (5 per copy) |
| cognizioware-hydra (+ `-fleet`, `-rsi`, worktree + `_w185` copies) `.env` | 5 | 15 | 3 per copy | 5 (1 per copy) |
| cognizioware-mcp-tools (+ `-c`) `e2e/.env` | 2 | 22 | 11 per copy | 30 (15 per copy) |
| other: westhive `kb-article/.env` (1, `POSTGRES_PASSWORD`); ptait09 `fleet-admin.env` (0 — keys defined but empty) | 2 | 1 | — | 2 |

Representative key names (mcp-cognizioware/prod.env): `AZURE_CLIENT_SECRET`,
`CLAUDE_API_KEY`, `COGNIZIOWARE_MCP_API_KEY`, `CURSOR_TOKEN`, `GHCR_TOKEN`,
`CLOUDFLARE_TUNNEL_TOKEN`, `AGENT_*_CLIENT_SECRET`. cognizioware-qa/prod.env adds
`LITELLM_API_KEY`, `QA_API_KEY`, `BILLING_API_KEY`, `N8N_API_KEY`,
`N8N_WEBHOOK_SECRET`, `CE_PASSWORD` and endpoints
`https://mcp-cognizioware.easybutt0n.ai`, `https://litellm-gateway-api.easybutt0n.ai`.
cognizioware-hydra/.env carries `HYDRA_API_KEY`, `DEVICE_TOKENS`,
`CLOUDFLARE_TUNNEL_TOKEN` (all non-empty). Endpoint hosts referenced across these
files include `litellm-gateway-api.easybutt0n.ai`,
`mcp-cognizioware.easybutt0n.ai`, `powerplatform.easybutt0n.ai`, and
`http://192.168.21.154` (also `*.crm9.dynamics.com` tenant URLs in qa/prod.env;
the hydra files reference only internal service names).

### I. `.claude` settings in work checkouts

`kb-hook.env` × 13 (11 top-level checkouts + 2 `_w185` copies): `KB_AUTHOR` in all
13, `KB_WEBHOOK_URL` in 6 — no credentials. `settings.local.json` × 8
(7 mcp-cognizioware-family copies + cognizioware-powerplatform-evalfix); the only
one carrying an `env` block is evalfix's, and it sets `MAX_THINKING_TOKENS: "0"` —
no credential. No `settings.json` in any work checkout carries an `env` credential
block. This repo's untracked `.claude/` contains only `worktrees/` — no skill, no
settings.

### J. LongHorizon-Harness repo (tracked) and user git config

* `cognizioware-how-to.md` (repo root, tracked, 191 lines): endpoint-host 7
  occurrences / 6 lines, cred-mention 18 occurrences / 11 lines; documents the
  `RUNNER_API_KEY` gotcha for CT110 direct-IP nodes (lines 189–190) and endpoints
  `https://harness.lan.easybutt0n.ai`, `https://litellm.easybutt0n.ai`,
  `http://192.168.21.168`.
* `/home/harness/.gitconfig`: credential helper reads `GH_TOKEN` from the environment
  (no literal value) — couples J's git auth to env var B.
* Shell profiles: `~/.bashrc`, `~/.profile`, `/etc/environment`, `/etc/profile.d/*` —
  **0** endpoint/credential matches (the only grep hits were the word "executed" in
  header comments). `/home/harness/.claude.json`: no `mcpServers` configured, no
  credential values.

### K. Totals

* Remote-exec endpoint routes visible to a run: `remote-pc` skill (7 named runner /
  gateway endpoints + 6 literal bearer values across `SKILL.md` and its bridge), 20
  workspace CLAUDE.md copies naming fleet hosts (of 42), 11 copies of the
  `mcp-gateway-ops` skill naming the `/exec` proxy, 6 skill-distribution SKILL.md
  files, plus SSH root aliases for 5 hosts and 7 credential-valued env vars.
* Credential carriers (files/vars with non-empty values): 1 live + 5 backup secrets-env
  files (36 assignments total), 1 QA deploy env (9), 121 checkout/deployment `.env`
  files (1012 assignments; 135 env-named files enumerated), 7 env vars, 2 SSH private
  keys, 6 in-skill bearer values.
* Locations with endpoint names but no credentials: cognizioware-mcp-tools CLAUDE.md
  family, ptait09-easybutt0n-ai docs, `graphify`, repo `cognizioware-how-to.md`,
  powerplatform `deployments/ssh/config` (routes only).

## Deliverable 3 — recommendation (doc only; nothing applied)

Grounded strictly in the counts above, the harness user on CT110 should **not carry**:

1. **`remote-pc`** — remove from `/home/harness/.claude/skills/`. It is the only
   user-level skill, it embeds 3 live per-host bearer keys in `SKILL.md` plus 3 more
   in its companion bridge (6 distinct literal bearer values total — A), it documents
   the `/exec` route used in the task-191 defect, and the bridge forwards model
   credentials to remote hosts. This single removal cuts the run-visible surface from
   "complete lateral-access kit" to "no fleet route, no fleet credential".
2. **`mcp-gateway-ops`** — do not keep in any checkout a run may open (11 copies today,
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