# TASK — Open-source LLM audio QA / E2E voice testing on the Cognizioware LiteLLM gateway (Phases 1–3), overseer-managed to deployment

**Asked by Paxton 2026-09-07 00:55 PT:** "add this as an lh-harness task templated, you manage it and watch it through to deployment … we need to test
the westhive-hivemind repo" — with the plan's handoff header. Follows `TEMPLATE-overseer-hierarchy.md`: overseer is the repo session for
cognizioware-mcp-tools and cognizioware-qa tonight; runs on CT110; deploy through each repo's lane; live measurement steps are overseer-executed.

## Handoff header (Paxton's)
| Field | Value |
|---|---|
| Plan file | `C:\Users\PaxtonTait\.claude\plans\yes-third-pass-whimsical-candle.md` (72 lines; Phases 1–3, verification, open decisions) |
| Originating session | `9ad3edc5-7779-49ed-8a7c-b4b5c2cf8934` (Claude Code, cognizioware-qa) — research journals/outputs listed in the plan |
| Primary repo | cognizioware-qa (branch `ci/qa-gate-billing`, main `master`) — Phase 2 code home |
| Gateway repo | cognizioware-mcp-tools → CT202 (192.168.21.161 on ptait01) — Phase 1 code home |
| Gateway endpoints | https://litellm-gateway-api.cognizioware.com (/v1/*, /mcp/, /mcp/tools); LAN https://litellm.easybutt0n.ai |
| Auth | virtual key `LITELLM_QA_KEY` (cognizioware-qa .env; never commit/log); admin ops via master key (cognizioware-litellm-admin skill) |
| Judges on gateway | sonar-pro (research), qwen3.6, kimi-k2.6:cloud, minimax-m2.7:cloud |
| Voice target to test | **westhivecapital-hivemind** voice agent (`/outreach/voice/*`, Telnyx assistant; see tasks/hivemind-voice-fixes-2026-08-30.md and hive-mind/deploy/live-state.md §11–§17) |

## Plan → runs
| Phase | Run | Workspace / branch | Deliverable | Deploy |
|---|---|---|---|---|
| 1 VERSA `audioqa` MCP server | **Run A** (this file's first run) | CT110 `/home/harness/work/cognizioware-mcp-tools`, `feat/audioqa-mcp` from origin/main | build context `infrastructure/docker/audioqa-mcp` (Dockerfile, server.py, bench.py, tests), compose service :3110, `mcp_servers.audioqa_mcp` + alias `audioqa`, e2e suite 36, skill `audio-qa`, bench doc placeholder | PR → main → lane (E2E gate → CT204 → QA gate → CT202); overseer sets `AUDIOQA_MCP_TOKEN` on CT202/CT204 env (name only here), runs `bench.py` on CT202, fills docs/audioqa-bench.md, verifies `/mcp/tools` with `x-mcp-servers: audioqa` = 4 tools and the aggregate tool count does not collapse |
| 2 Behavioral E2E voice harness | Run B (after Phase 1 is live) | CT110 `/home/harness/work/cognizioware-qa`, `feat/voice-e2e-harness` from origin/master (+ `ci/qa-gate-billing` if it has unmerged work) | spike Pipecat Evals vs DeepEval voice metrics against the gateway; judge ensemble ≥2 models, randomized A/B; `voice` target in qa-gate.yml with 40-hex caller_sha status; each run calls `audioqa-compare_audio` vs last green baseline; **first scenario = hivemind voice context/recall/booking flow** | merge → qa-gate `voice` target runnable from ct210-qa; first live run against the hivemind dev voice endpoint (operator-provided creds) |
| 3 STT/TTS plumbing | Run C (optional, after B) | mcp-tools | local Whisper via `hosted_vllm` (vLLM Whisper container — GPU node decision D1), sanzaru optional | lane |

## Constraints carried from the plan (non-negotiable)
No stdio MCP servers (HTTP only; supergateway pattern if needed); every server has `access_groups` + a dash-free alias; LiteLLM image digest stays
pinned; never bare `docker compose up` on CT202 (`--env-file mcp-tools.env`); metrics are RELATIVE gates, never absolute MOS thresholds; never a
single judge; NISQA excluded (non-commercial weights); commercial platforms out of scope. Build contexts live under `infrastructure/docker/<name>`
(lane sync rule, learned 2026-09-07). No secrets in files; token names only.

## Open decisions for Paxton (carried from the plan)
1. Where the vLLM Whisper container runs — CT202 (CPU) or a GPU node (ptait07 RTX 3090 / corsairai300)? (Phase 3)
2. Pipecat vs DeepEval — decided by the Phase 2 spike; recorded here when known.

## Progress
- 01:00 PT Sep 7: Run A launched as `20260907T044506Z_8bd3a6cb` (mcp-tools workspace, feat/audioqa-mcp from main, all-kimi, 8 rounds; task text C:\tmp\audioqa-phase1-task.txt). Pool: MCP-profiles 716506b3 + this run (keys 1 and 4 healthy).
- 01:25 PT Sep 7: manager gate — port 3110 is taken by cloudflare-mcp; answered: use 3111 everywhere.
- 01:20 PT Sep 7: Run A (8bd3a6cb, all-kimi) reached the round limit CODE-COMPLETE: 4519ca0 image, 59f5e41 server + bench, 6e98def bearer auth on the
  Streamable HTTP path + pip pins, 0816fbb compose service :3111 + gateway registration + e2e 36, cc3eee7 skill + runbook + bench placeholder
  (17 files +1629). Final audit round was invalidated (auditor created .pytest_cache) — overseer verifies scope, CRLF-safe additions, port 3111
  uniqueness (auditor flagged comfyui-mcp may use 3111), unit tests. Run closed via approval 26a6bb022d91. Next: PR → main → lane (E2E gate →
  CT204 → QA gate → CT202), AUDIOQA_MCP_TOKEN on CT202/CT204 env, bench on CT202, docs/audioqa-bench.md, tools/list check; then Run B (Phase 2).
- 01:35 PT: AUDIOQA_MCP_TOKEN generated and written to the CT202 and CT204 env files (same value on both; name only here). PR opened for feat/audioqa-mcp.
- 01:40 PT: PR #83 merged → deploy lane running (builds the VERSA image on CT202; E2E gate ~1 h, CT204 apply, uat QA gate, CT202 deploy). Watcher verifies the container, /health and the four audioqa tools via JSON-RPC; then bench.py on CT202 → docs/audioqa-bench.md; then Run B (Phase 2).
- 02:20 PT Sep 7: lane for #83 died in the E2E gate — the CT203 runner unit was OOM-killed a THIRD time (06:54Z) despite the OOMScoreAdjust drop-in;
  runner restarted; mcp-tools #84 (E2E gate: 1 jest worker, 1.5 GB heap) merged so the lane re-runs with less memory and carries the audioqa deploy.
  Root constraint: ptait01 host RAM (42 GB; the qwen3.8 span holds ~38 GB) — CT203's 12 GB is not really available. Watcher on the new lane.
- 04:20 PT Sep 7: lane for the audio QA deploy (run 34093527178): E2E gate PASSED with the smaller footprint (#84 worked), CT204 apply OK, uat QA gate 5/6 — only glm-5.3-flash:cloud failed (pooled over keys 1–3, all limited; key 4 idle) → config PR adds a 4th flash deployment on OLLAMA_CLOUD_KEY_4; the next main push re-runs the lane and carries audioqa to CT202.
- 04:35 PT Sep 7: lane 34096499561 — E2E gate + CT204 apply + uat QA gate ALL GREEN (key-4 flash fix worked), then CT202 deploy failed at 'Build local images' for audioqa-mcp: `pip install -r requirements.txt` exit 1 (pins from run 8bd3a6cb; likely torch==2.9.1+cpu or speechmos). Reproducing the install on uat to get the pip error; fix = a small requirements/Dockerfile PR, then the lane again.
- 05:30 PT Sep 7: root cause confirmed on uat — openai-whisper==20240930 builds its wheel from an sdist that imports pkg_resources (removed in setuptools 81); pip's isolated build env ignores a pre-installed setuptools, PIP_CONSTRAINT=setuptools<81 fixes it (verified: wheel builds, requirements install completes). Dockerfile fix PR merged to main → lane re-running with the audioqa deploy; watcher verifies CT202.
- 05:50 PT Sep 7: LAN runner moved: ct210-lan (CT210/ptait07, label lan-cognizioware, deploy key linked as ~/.ssh/id_ed25519) online; CT203's gh-runner-lan unit stopped + disabled (host OOM on ptait01 killed it 28×). Lane 34101664897 re-run on the new runner (E2E gate → CT204 → QA gate → CT202 with the audioqa build fix).
