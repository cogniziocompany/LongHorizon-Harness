# TASK — ptait09-easybutt0n-ai: Claude Code Remote Control as the fleet device's last-resort link (build on CT110, install via the runner, e2e-gated)

**Asked by Paxton 2026-09-07 23:20 PT (Sep 6 clock):** "create another lh-harness task with this update … you manage it and make sure it's being used
moving forward. nothing on ptait09." Plan: `C:\Users\PaxtonTait\.claude\plans\come-up-wioth-asolution-sorted-eich.md` (planning session
8a31554d-28e9-415b-8dfc-933791a689a4). Follows `TEMPLATE-overseer-hierarchy.md`; overseer = repo session for ptait09-easybutt0n-ai.

## Handoff header (Paxton's)
| Item | Value |
|---|---|
| Repo | cogniziocompany/ptait09-easybutt0n-ai; deploy branch `easybutt0n-runner-v2` @ b821265 (PR #35); work branch `feat/claude-rc-fallback` |
| Device source root (RC cwd) | `C:\Users\PaxtonTait\source` (Windows `<home>\source`; Linux `~/source`) |
| Runner service | easysvc `ptait09-runner`, svc/runner.svc.json, LocalSystem, port 7334 |
| New service | easysvc `ptait09-claude-rc`, svc/claude-rc.svc.json, LocalSystem |
| Operator | Paxton (Claude Max 20x), full-scope login present in `%USERPROFILE%\.claude\.credentials.json` |
| Related docs | docs/HANDOFF-CLAUDE-SESSION-BRIDGE.md, docs/FLEET-ENTRYPOINT-RUNBOOK.md, svc/README.md, created-skills/fleet/remote-pc/SKILL.md |
| Restart rule | never restart easysvc services from inside the runner (kills the caller); elevated console only |

## Why
A fourth reach path that depends on none of our infrastructure (tunnel, gateway, Hydra): `claude remote-control` running persistently under easysvc,
rooted at the device's source dir, reachable from claude.ai/code or a phone. Fallback, not primary; reliability tied to Anthropic's cloud + the
operator's subscription (accepted). Installed by onboarding, gated by an e2e like Hydra enrollment.

## Runs
- **Run A (build, CT110):** workspace `/home/harness/work/ptait09-easybutt0n-ai` (cloned tonight, npm ci done, budgets 900/900/1800), branch
  `feat/claude-rc-fallback` from origin/easybutt0n-runner-v2. Five slices: launcher + session-file helper; service configs + auth bootstrap;
  GET /rc + host_info; e2e `claude-rc-e2e.mjs`; onboarding 4b + fleet.json + skill + docs. Task text `C:\tmp\claude-rc-task.txt`.
  Trio: all-qwen3.8 (the kimi pool's two slots are taken by the audio QA and Env→Session runs; one local run at a time).
- **Overseer after Run A:** review → PR → `easybutt0n-runner-v2` (then merge both ways with main like the 2026-09-05 fix). Spike + install on
  PTAIT09 **through the runner's /exec as SYSTEM** (no interactive work on the PC): run `claude.cmd remote-control --name spike-rc` for 2 min with a
  scrubbed env; if it needs a TTY, a follow-up slice switches the launcher to node-pty. Then `claude-rc-auth.ps1` (elevated — Paxton runs it once if
  /exec cannot), `easysvc install/start --config svc\claude-rc.svc.json`, restart `ptait09-runner` from an elevated console (Paxton),
  `npm run test:claude-rc` → `RESULT claude-rc PASS`, then once with `CLAUDE_RC_HARD=1`; `npm run test:fleet` + `test:exec-limits` unchanged.
- **Roll-out:** cherry-pick to ptait-desk03 and htpc01 branches (ptait10am5 is down, skip); manual phone check on cellular with cloudflared stopped 1 min.

## "Used moving forward"
After install: `GET /rc` on each runner + `host_info.claudeRc` through the gateway; the overseer template gains a rule — when a device's tunnel/
gateway/Hydra path fails, use the `<id>-rc` session at claude.ai/code before touching the console; the remote-pc skill documents it.

## Progress
- 23:35 PT: Run A launched as `20260907T060255Z_87d5583f` (all-qwen3.8, 8 rounds; the kimi pool's two slots are held by the audio QA and Env-to-Session runs).
- 01:00 PT Sep 7: Paxton ran `claude rc` by hand from his own account on ptait09 (cwd E:\) — Connected, Capacity 0/32, session env
  `env_013ovssrt3AB69Y1E5CgZYws` (interim link only; it dies with that PowerShell window). Requirement change: RC runs AS THE LOCAL INSTALL USER on
  every live fleet device, starting at boot/logon, with the user's own login (no SYSTEM-profile credential copy); Linux hosts and LXC/Docker
  containers get a systemd unit as the install user, installed from the host via exec (`pct exec` / `docker exec`). First run 87d5583f (round 2,
  nothing committed) stopped; relaunched with the change (id in C:\tmp\claudirc_runid.txt). Fleet scope: ptait09, ptait-desk03, htpc01 (+ CT110
  as the first container); ptait10am5 down.
- 01:12 PT: packaging rule added (same per-OS mechanism as the runner: schtask / systemd / compose profile beside cloudflared); relaunched as 20260907T063807Z_f7de0f71.
- 04:55 PT Sep 7: run f7de0f71 blocked at round 5 (qwen3.8 manager emitted no readable output; the round-4 executor hit the 1800 s budget). Slice 1 committed (230e401). Continued with a re-plan instruction (smaller executor subtasks); if it blocks again it moves to the kimi queue.
- 06:05 PT Sep 7: run f7de0f71 (qwen3.8) FAILED at round 7 with F1 ('Content block is not a thinking block') through PROD despite the normalizer — investigating which role/request shape slipped past the hook. Committed so far: 230e401 (launcher + session helper), f940711 (Windows Scheduled Task installer, env example, gitignore); Linux install script + systemd unit untracked on disk. Re-queued on the kimi pool (04-claude-rc-fallback) with a resume note; the guest readiness audit takes the local lane next.
- 02:35 PT: root cause of the f7de0f71 failure is the stream-shape bug (see litellm-thinking-fix follow-up); the requeued kimi run (04-claude-rc-fallback) is unaffected (kimi via ollama_chat streams cleanly). If it must fall back to qwen, wait for mcp-tools PR #88 and use qwen3.8-nothink for executor/auditor.
- 12:30 PT: run fd990c1c (kimi) finished all slices on feat/claude-rc-fallback @ 802c93c (24 files, +2191/-19 vs easybutt0n-runner-v2; node --test 26/26; no secrets). Deliverables: launcher (ps1/sh/js, atomic session file), auth bootstrap + service configs (schtask/systemd), GET /rc + host_info, e2e `npm run test:claude-rc`, compose profile `claude-rc` (non-root), onboarding step 4b, fleet.json claudeRc + containers, /remote-pc skill "last-resort path", docs/CLAUDE-RC-FALLBACK.md. Two doc nits (line 37 quote, line 150 phrasing) to fix in the PR. Paxton (12:00 PT): auto-approve this task through to prod and test with the cognizioware-qa app. Next: PR -> merge -> install on ptait09 first (elevated auth bootstrap + Scheduled Task, from OUTSIDE the runner), then desk03/htpc01/ptait10am5, then the qa app check.
- 12:30 PT: **Remote Control LIVE on PTAIT09.** Installed as the install user (PaxtonTait) from easybutt0n-runner-v2: auth bootstrap OK (full-scope credential, trust seeded), Scheduled Task `PTAIT09-claude-rc` (at logon, restart on failure) Running; claude.exe remote-control connected as `PTAIT09-rc`, environment env_01FcJmFx2wq2c8nUJ8EB4hgK, session session_015FjKR9C2fTNqJX1oS9i8ij (Capacity 1/32). Four host bugs fixed on the way, all merged: #37 trust-file parse + -WindowStyle, #38 here-string terminator, #39 SYSTEM-profile probe, #40 cmd.exe launch with merged log + .cmd shim. Open: session file not written (URL regex misses `_` in session ids) -> fix next; then desk03/htpc01/ptait10am5 + CT compose profile, then cognizioware-qa check.
