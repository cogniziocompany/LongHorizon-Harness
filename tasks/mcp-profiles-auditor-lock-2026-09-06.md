# TASK — LongHorizon-Harness: auditor git lock, MCP profiles per role, session-id headers (run on CT110, overseer-managed, overseer-deployed)

**Asked by Paxton 2026-09-06 17:10 PT:** "create an lh-harness task for this with template … manage it yourself and deployment", with the handoff
header below. Follows `TEMPLATE-overseer-hierarchy.md`: the overseer is the repo session for LongHorizon-Harness (no other owner), monitors the
run directly, reviews, opens the PR, deploys to CT110 in a quiet window.

## Handoff header (Paxton's, verbatim where it matters)
| What | Where |
|---|---|
| Harness code | cogniziocompany/LongHorizon-Harness, base origin/main @ 29a4e34 → branch `feat/mcp-profiles-auditor-lock` |
| Hydra fleet plane | cogniziocompany/cognizioware-hydra branch `feat/harness-fleet` @ 721de8f (CT110 clone `/home/harness/work/cognizioware-hydra-fleet`) |
| Gateway config (reference only) | cognizioware-mcp-tools `infrastructure/litellm-config.yaml` (`mcp_servers:` block), preset vocabulary `infrastructure/docker/gateway-guides-mcp/src/index.ts:37-89` |
| Prod harness node | CT110 on corsairai300, 192.168.21.168:8799, systemd `lh-harness` (venv, non-editable pip 0.1.7), secrets `/home/harness/.lh-harness-secrets.env`, Hydra node id ct110 |
| Gateway endpoints | prod `https://litellm-gateway-api.cognizioware.com/mcp/`, LAN `http://192.168.21.161:4000/mcp/` (trailing slash mandatory) |

Session id: `<run_id>.<round_tag>.<role>`, from `episode_session_id()` next to `episode_env` (claude_code.py:139-149); emitted in
`x-litellm-tags` (`lh-session/<session>`), as `X-LH-Session` in the generated `.mcp.json`, and as `result.metadata["lh_session_id"]`.
Claude Code's own session_id is never parsed.

Files/env introduced: `src/lh_harness/mcp_profiles.py`; `<run_dir>/harness/mcp/<role>.mcp.json` (0600, hidden from the agent);
`~/.lh-harness/mcp_profiles.json`; `.lh-harness/config.toml` `mcp_profile`, `[run.roles.auditor] mcp_profile`, `[run.mcp_profiles.<name>]`;
node env `LH_HARNESS_MCP_GATEWAY_KEY` (required, dedicated — no fallback to the LLM token), `LH_HARNESS_MCP_GATEWAY_URL` (prod default / `lan` / URL),
`LH_HARNESS_MCP_GATEWAY_HEADERS_JSON`; web defaults `LH_HARNESS_WEB_DEFAULT_MCP_PROFILE`, `LH_HARNESS_WEB_DEFAULT_{MANAGER,EXECUTOR,AUDITOR}_MCP_PROFILE`;
node compose `LH_NODE_MCP_PROFILE`, `LH_NODE_AUDITOR_MCP_PROFILE`; CLI `--mcp-profile`, `--{manager,executor,auditor}-mcp-profile`; API
`POST /api/runs` `mcp_profile` + `roles.<role>.mcp_profile`, `GET /api/meta` `mcp_profiles[]`, `defaults.roles.<role>.mcp_profile`,
`mcp_gateway_configured`; auditor lock `AUDITOR_NETWORK_GIT_DENY` + `AUDITOR_NETWORK_GIT_ENV` (claude_permissions.py), hint `network_git_op`
(auditor_agent.py). Decisions: auditors default to the read-only `audit` profile; derived session id; Hydra fleet patched in the same change.
Order: A (git lock) → C (session id) → B (profiles) → D (Hydra + ops) → docs.

## Run
- Workspace `/home/harness/work/LongHorizon-Harness` (cloned 17:12 PT, `.venv` with `.[test]`); secondary `/home/harness/work/cognizioware-hydra-fleet`.
- Task text `C:\tmp\mcp-profiles-task.txt`. Trio: manager `qwen3.8` (local), executor `kimi-k2.7-code:pool`, auditor `qwen3.8` — keeps cloud load to
  one pool role while bf5e2f69 and RP-15 run (keys 1 and 4 healthy).
- Run id: see "Progress" below.

## Overseer follow-through
1. Review: deny rules only on auditor roles; no secret strings; generated config under `<run_dir>/harness/mcp`; pytest before/after; precedence table.
2. PR → main (LongHorizon-Harness) and PR → feat/harness-fleet (hydra). Clone with autocrlf off if touched from Windows (see the CRLF incident).
3. Deploy CT110 in a QUIET WINDOW (the service restart kills web-launched runs): `pip install` from the clone at the merged sha into
   `/home/harness/venv`, add `LH_HARNESS_MCP_GATEWAY_KEY` (a dedicated LiteLLM virtual key with the MCP access groups — create it on CT202, never the
   lh-harness LLM key) + `LH_HARNESS_MCP_GATEWAY_URL=lan` to the secrets env, `systemctl restart lh-harness`, verify `GET /api/meta` shows
   `mcp_gateway_configured: true` and the profiles; run one small three-role validation run with the `audit` profile on the auditor and check
   Langfuse tags carry `lh-session/…`. Then the Hydra fleet plane deploy on corsairai300 per its own docs.
4. Optional mcp-tools follow-up: forward `X-LH-Session` via `extra_headers`.

## Progress
- 2026-09-06 17:22 PT: run `20260907T002208Z_2785e057` launched via CT110 `POST /api/runs` (16 rounds; qwen3.8 / kimi-k2.7-code:pool / qwen3.8).
  Baseline on the CT110 venv before the run: pytest 404 passed, 2 skipped. Concurrent runs on CT110: bf5e2f69 (LiteLLM fix, r4), RP-15 f94faabb (r2).
- 17:44 PT: 2785e057 stopped at round 3 — all three rounds were MANAGER episode timeouts at the 300 s default (the fresh clone had no workspace
  `.lh-harness/config.toml`; qwen3.8 needs ~10 min to plan a contract this long, cf. bf5e2f69 on 600 s). Added the untracked workspace config
  (manager 900 / auditor 900 / cli_executor 1800, excluded via .git/info/exclude) and relaunched — see next line. Lesson added to the template:
  every NEW CT110 workspace needs the budgets file before the first qwen3.8 run.
- 17:46 PT: relaunched as `20260907T004613Z_ab12530b` (same trio, 16 rounds, budgets 900/900/1800). Watcher running.
- 18:12 PT: ab12530b FAILED in round 1 on the qwen3.8 auditor — `Content block is not a thinking block` (F1), same as RP-14 adf11963 fourteen
  minutes earlier. qwen3.8 is out of every tool-using role until the normalizer (bf5e2f69) ships. Relaunched on the all-kimi trio; the new run
  starts from whatever slice-A commit the round-1 executor left on the branch (see next line).
- 18:07 PT: relaunched as `20260907T010653Z_3d24a18b` (all-kimi, 16 rounds). Branch already carries slice A twice (bc8a1d8 and 7021e3a, same message; 5 files +191): the executors of 2785e057/ab12530b committed the auditor git lock before their qwen3.8 auditors died. Squash/dedupe at PR time.
- 18:15 PT: 3d24a18b (all-kimi) FAILED in round 1 — auditor spawn: `/bin/sh: 1: --unsetenvvar=SSH_AUTH_SOCK: not found`. ROOT CAUSE is an
  infrastructure hazard, not the model: `/home/harness/venv` carried a stale editable `.pth` → `/home/harness/work/LongHorizon-Harness/src`, so
  once the harness repo was cloned to that exact path every CT110 worker imported the WORKSPACE code (slice A's buggy env wiring at
  claude_code.py:97) instead of the released 0.1.7. It also killed RP-14 060e4809 in the same way. Fix: .pth moved aside; site-packages copy turned
  out to be a gutted namespace dir, so the released package was reinstalled non-editably from a fresh clone of origin/main (29a4e34) into the venv;
  import now resolves to site-packages and has no AUDITOR_NETWORK_GIT_ENV. Memory: ct110-editable-install-hazard.
- 18:22 PT: relaunched as `20260907T011213Z_da3cdef7` (all-kimi, 16 rounds) with an OVERSEER NOTE in the task text: fix the slice-A env bug first
  (remove vars via the Popen env dict, never shell args; test for it), then C, B, D, docs.
- 19:55 PT: SECOND venv incident — da3cdef7's executor (rounds 1 and 4) ran `pip install -e .` and `pip install --no-deps -e /home/harness/work/
  LongHorizon-Harness` with the SERVICE venv first on PATH, removing the release package: RP-16 d2cdac30 and the webhook fix eeea214a died at
  round 0 ("worker exited with status 1" / `No module named lh_harness`). Release reinstalled from /home/harness/release-src; `/home/harness/venv`
  is now root-owned (harness user: pip → Permission denied; import verified). da3cdef7 itself keeps running (its worker process holds the old
  modules; new episodes import the release). Both killed runs relaunched.
- 22:35 PT: all four Ollama keys back at 20:11 PT-clock (watcher time); quota watcher resumed RP-16 ac894162 and the webhook fix 084159ae; MCP-profiles relaunched as `20260907T032102Z_716506b3` (all-kimi, 16 rounds, task text with venv rule + inherited-worktree note).
- 01:10 PT Sep 7: run 716506b3 (all-kimi, relaunch #5) COMPLETE at round 6: B (5f326af registry+resolver, e2ddc17 adapter wiring, cf6194b
  API/CLI surfaces, 9656f85 allow_auditor_write_mcp + provenance), docs 3ef5ff8, on top of A (bc8a1d8/7021e3a dup + ac7ddfd fix + 9d51331) and
  C (24f95a4). Harness: 470 passed / 2 skipped (baseline 404/2). Fleet plane: 38e4184 (7 files +90/−5), 8 tests. No secrets; no frontend rebuild.
  Run closed via approval f969c6863b64. Branches pushed; harness PR opened; hydra feat/harness-fleet updated in place (it is the fleet worktree
  branch itself). Deploy waits for a quiet window on CT110 (audioqa Run A is live there) and needs a dedicated LiteLLM virtual key for the MCP gateway.
- 22:45 PT (after the PC restart): harness deploy still pending — CT110 has two live web-launched runs (audioqa 8bd3a6cb r6, env-to-session 0a285435 r5); the service restart would kill them. Deploy sequence is staged in the plan file; executes in the first quiet window.
- 22:55 PT: PR #1 MERGED (main fb88bab). Dedicated LiteLLM virtual key `lh-harness-mcp-gateway` created on CT202 (models: qwen3.8 only; MCP
  scoping by the harness's x-mcp-servers header) and installed on CT110 as LH_HARNESS_MCP_GATEWAY_KEY + LH_HARNESS_MCP_GATEWAY_URL=lan (names only
  here). Deploy script staged at C:\tmp\pp-fix2\deploy-harness.sh (aborts if any run is active). Finding: the built-in profiles used invented
  aliases (ssh-list, github-read, langfuse-read…) that the gateway does not know — remapped to the real dash-free mcp_aliases (kb, guides, skills,
  memory, langfuse_mcp, github, youtrack, ssh, hydra, hydrafleet, proxmox*); audit = non-mutating servers only, since the gateway cannot make ssh read-only.
