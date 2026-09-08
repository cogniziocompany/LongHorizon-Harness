# E2E model registry: drop sub-qwen3.8 local models + DB-backed dynamic model list (2026-09-08)

**Ask (Paxton 03:40 PT):** add this plan as a harness task in the managed queue. Planning session 3e71c218 (cognizioware-mcp-tools). Decisions already made: retire every qwen3.5:* local tag + qwen3.6; change e2e AND litellm-config.yaml; the registry lives in the admin API DB (app_configuration, category E2E-Monitoring, key E2E:ModelRegistry).

**Queue:** `20a-e2e-model-registry-api` (mcp-cognizioware, GET/PUT /api/v1/admin/e2e/models + tests; deploy via promote-billing) -> `20b-e2e-model-registry-client` (mcp-tools PR A: e2e/lib/model-registry.js, fixtures/models.json, globalSetup discovery, sync tool, suite rewrites 02/12/13/18/23/34, drift test) -> `20c-litellm-retire-qwen35` (mcp-tools PR B: config purge + team/key repoint runbook; UAT-first, FULL run-all green before prod). Operator steps after the runs: seed the registry row through the admin API, `npm run models:sync -- --dry-run` then `--write`, team-then-key repoint via /team/update and /key/update, /model/delete for lingering DB rows, drift test green on UAT then prod.

## Progress
- 2026-09-08 03:45 PT: three queue entries created; plan copied below verbatim.

---

# E2E model registry: drop sub-qwen3.8 local models + DB-backed dynamic model list

## Handoff header

| Field | Value |
|---|---|
| Plan file | `C:\Users\PaxtonTait\.claude\plans\refview-qal-the-e2e-humble-spindle.md` |
| Claude Code session id | `3e71c218-e85e-4129-a982-0a9b96f37a32` (resume: `claude --resume 3e71c218-e85e-4129-a982-0a9b96f37a32` from the primary repo) |
| Scratchpad | `C:\Users\PAXTON~1\AppData\Local\Temp\claude\c--Users-PaxtonTait-source-cognizioware-mcp-tools\3e71c218-e85e-4129-a982-0a9b96f37a32\scratchpad` |
| Planned on | 2026-09-08, plan mode (no code changed yet) |
| Requested by | Paxton Tait (`prax211`, prax211@gmail.com) — author of the original ask |
| Primary repo | `c:\Users\PaxtonTait\source\cognizioware-mcp-tools` (GitHub `cogniziocompany/cognizioware-mcp-tools`), branch `main` at `8809c88`; untracked: `.lh-harness/`, two handoff docs under `design docs/handoff-docs/` — leave them alone |
| Secondary repo | `C:\Users\PaxtonTait\source\mcp-cognizioware` (BillingService admin API). **Checked-out branch is `docs/youtrack-gap-analysis` and predates the e2e controller — branch the API work from `origin/main`** (`AdminE2eController.cs` landed in `438890c6`, PR #60) |
| Live systems touched | LiteLLM gateway CT202 `192.168.21.161:4000` (prod), UAT LiteLLM CT204 `192.168.21.162` (applied by the pipeline job `deploy-uat-litellm`), billing admin API `https://mcp-cognizioware.easybutt0n.ai` (CT100 `192.168.21.153:20251`), Ollama backends ptait07 `.138:11436`, corsairai300 `.151:11440`, ptait01 `.110:11438/11441/11442` |
| Secrets needed (read from `e2e/.env`, never edit) | `LITELLM_MASTER_KEY` (never change it), `E2E_INGEST_KEY` (must equal admin config row `E2E:IngestKey`) |
| Hard rules | UAT-first with a FULL `run-all.sh` green before any prod change; no hand-deploys to CT202; never `docker compose down -v`; never commit secrets; do not edit `.github/workflows/` unless required (not required here); mirror any live env change in-repo same day |
| CI | `.github/workflows/deploy-mcp-tools.yml` — job `e2e-tests` on `[self-hosted, lan-cognizioware]`; only suite 13 Phase 0 blocks, the full run is advisory |
| Prior art to copy | `e2e/lib/admin-baseline.js` + `e2e/baseline.json` + `e2e/suites/22-aggregate-baseline.test.js` (DB-backed baseline with committed fallback); `e2e/lib/skip-registry.js` for visible skips |
| User decisions already made | retire `qwen3.5:*` local tags + `qwen3.6` only; change e2e AND `litellm-config.yaml`; registry lives in the admin API DB |
| Expected deliverables | PR in mcp-cognizioware (endpoints + tests); PR A here (e2e registry + suite rewrites); PR B here (config purge + docs + team repoint runbook) |

## Context

The GitHub Actions e2e output still probes `qwen3.5:4b--64k`, `qwen3.5:9b`, `qwen3.5:27b` on
ptait07/corsairai300, and `kimi-k2.6:cloud` / `glm-5.2:cloud` direct on .151 fail. Every model name and
Ollama host URL is a literal inside the suites (suite 13 alone has 8 arrays + 5 URLs, with copies in
suites 02, 12, 18, 23, 34), so the tests drift from the gateway every time a model is retired.
`infrastructure/litellm-config.yaml:2245` already states "local lane policy is qwen3.8 and newer only",
yet the config still registers qwen3.5:9b/4b/27b (+ `--64k/--128k/--max-effort` tiers) and qwen3.6.

Decisions taken with the user:
- **Model floor:** retire every `qwen3.5:*` local tag and `qwen3.6`. Keep gpt-oss:20b, functiongemma,
  deepseek-ocr, glm-ocr, embeds, reranker. `qwen3.5:397b-cloud` is a cloud relay, not local — keep.
- **Scope:** e2e **and** `litellm-config.yaml` (prod gateway change → UAT-first via the pipeline's
  `deploy-uat-litellm` job on CT204, then full `run-all.sh` green, then promote).
- **Registry home:** admin API DB only (mcp-cognizioware BillingService, `app_configuration`,
  category `E2E-Monitoring`), same pattern as the aggregate baseline (`e2e/lib/admin-baseline.js`).

Outcome: suites build their model/host lists at run time from the registry; a sync tool discovers the
live catalog and updates the registry; a drift test fails when retired tags reappear.

## Design

### Registry document (one JSON config row `E2E:ModelRegistry`, category `E2E-Monitoring`)

```jsonc
{
  "version": 1, "updatedAt": "...", "updatedBy": "sync-model-registry",
  "policy": {
    "critical": ["qwen3.8"],                       // Phase 0 deploy gate
    "localFloor": "qwen3.8",                        // informational
    "retired": ["qwen3.5:*", "qwen3.6", "qwen3.6:*"],  // glob; presence in /v1/models = drift FAIL
    "relayFloor": "kimi-k2.6:cloud"
  },
  "backends": [                                     // direct Ollama hosts (Phase 6)
    { "name": "ptait07 gpu3", "url": "http://192.168.21.138:11436", "role": "local" },
    { "name": "corsairai300", "url": "http://192.168.21.151:11440", "role": "local", "directSample": true },
    { "name": "ptait01 cloud+embed", "url": "http://cognizioware-ptait01:11438", "role": "relay+embed" },
    { "name": "ptait01 gpu1 lane", "url": "http://192.168.21.110:11441", "role": "local" },
    { "name": "ptait01 span lane", "url": "http://192.168.21.110:11442", "role": "span", "spanModel": "qwen3.8:27b", "numCtx": 65536 }
  ],
  "models": {                                       // expected gateway catalog, by phase
    "local":    ["qwen3.8", "qwen3.8--128k", "qwen3.8--256k", "gpt-oss:20b", "deepseek-ocr:latest", "functiongemma:270m"],
    "cloud":    ["glm-5:cloud", ...],
    "external": ["claude-opus-4.6:openrouter", ...],
    "embed":    ["nomic-embed-text:latest", ...],
    "rerank":   ["qwen3-reranker:q5_k_m"],
    "pool":     ["kimi-k2.7-code:pool", "kimi-k3:pool", "glm-5.3:pool"]
  },
  "discovered": {                                   // auto-refreshed by the sync tool / each run
    "gateway": ["...ids from /v1/models..."],
    "backends": { "http://192.168.21.151:11440": ["qwen3.8:27b", ...] }   // /api/tags per host
  }
}
```

`models.*` is the **expected** list (operator-promoted). `discovered` is informational and drives the
"if pulled" gating so tests never probe an unpulled tag.

### Admin API (repo `C:\Users\PaxtonTait\source\mcp-cognizioware`, branch off `origin/main`)

`src/McpCognizioware.BillingService/Controllers/AdminE2eController.cs` — add two actions, reusing
`IsAuthorizedAsync` (Entra admin **or** `X-E2E-Ingest-Key`) and `IAppConfigurationService`:
- `GET /api/v1/admin/e2e/models` → returns the JSON row (404 if absent).
- `PUT /api/v1/admin/e2e/models` → validates shape (version, policy.critical non-empty, backends[].url
  http(s), no retired glob matching a model in `models.*`), stamps `updatedAt/updatedBy`, and
  `UpsertAsync` on key `E2E:ModelRegistry` (scope `global`, `is_secret=false`).
  Optional `?merge=discovered` mode that only replaces the `discovered` block (used by test runs).
Add xUnit tests under `McpCognizioware.BillingService.tests/Controllers/` for auth + validation.
No schema change: `app_configuration.config_value` holds the JSON. Deploy via `promote-billing.yml`
(dev → uat → prod). Until it is live, the e2e falls back to the committed snapshot.

### E2E client (this repo)

1. **`e2e/lib/model-registry.js`** (new) — mirrors `admin-baseline.js`:
   `fetchRegistry()` → admin GET, else fallback `e2e/fixtures/models.json` (`_source` flag);
   `putRegistry(doc)` / `putDiscovered(discovered)` → admin PUT with ingest key (never throws);
   helpers `isRetired(id)`, `pulledOn(backendUrl, tag)`, `byPhase(name)`; a synchronous
   `loadRegistrySync()` reading a per-run cache file so `test.each` arrays can be built at module load.
2. **`e2e/setup/jest.setup.js`** (globalSetup) — fetch the registry once, run discovery
   (`GET /v1/models` on the gateway + `GET /api/tags` on every `backends[].url`, 8s timeouts, soft),
   write `e2e/.model-registry.json` (gitignored) for suites to read synchronously, and if
   `E2E_INGEST_KEY` is set PUT the refreshed `discovered` block back (this is the "auto-fills on run").
3. **`e2e/scripts/sync-model-registry.js`** (new, `npm run models:sync`) — the operator tool:
   `--dry-run` prints diff (expected vs `/model/info` from the gateway, classified by
   `litellm_params.api_base`: RFC1918 + `ollama_chat|openai` → local, `ollama.com` → cloud,
   embeddings mode → embed, else external); `--promote` writes `models.*` from the live catalog minus
   `policy.retired`; `--write` PUTs to the admin API and refreshes `e2e/fixtures/models.json`.
   Requires `LITELLM_MASTER_KEY` + `E2E_INGEST_KEY` from `e2e/.env` (read, never edited).
4. **`e2e/fixtures/models.json`** (new) — committed fallback snapshot, generated by the sync tool.
5. **Suite rewrites** (pattern: replace literal arrays with registry reads; keep existing soft-fail
   helpers `isSkippable` / `chatTest` / `ollamaFetch` and use `lib/skip-registry` instead of
   `expect(true).toBe(true)`):
   - `e2e/suites/13-litellm-model-inference.test.js`: `CRITICAL_MODELS`, `LOCAL_MODELS`, `CLOUD_MODELS`,
     `EXTERNAL_MODELS`, `EMBED_MODELS`, reranker, `BACKENDS`, `CORSAIR_SAMPLE`, `QWEN38_LANES`,
     `SPAN_BASE`, `RELAY_FLOOR` all come from the registry. Delete the two `qwen3.5:4b--64k` Phase 6
     tests; replace with one generic `test.each(localBackends)` that picks a registry local model
     pulled on that host (skip via skip-registry if none). `CORSAIR_SAMPLE` = registry local+cloud
     models ∩ `discovered.backends[.151]`. Add tests: "no retired model in /v1/models" (hard fail) and
     "no chain targets a retired model" (extend `parseChains` check from `qwen3.6` to the retired globs).
   - `e2e/suites/02-litellm-models.test.js`: replace the per-model literal tests with
     `test.each(expectedIds)` from the registry + the retired-absent check; drop `qwen3.5:27b`.
   - `e2e/suites/12-external-public-endpoints.test.js:174` and `18-azure-inference-adapter.test.js:38,56,82`:
     `qwen3.5:*` → `policy.critical[0]` (qwen3.8). `DIRECT_NEW_MODELS` (:247) from registry.
   - `e2e/suites/34-litellm-thinking-roundtrip.test.js:70`, `23-qwen38-vision-quota.test.js:26,128`: read
     critical / relayFloor from registry (small change).
   - Keep every existing hard/soft/advisory policy exactly as documented in suite 13's header.
6. **Docs:** `e2e/README.md` (phase table + a "Model registry" section next to "Aggregate baseline"),
   `.env.example` (no new secrets; `E2E_INGEST_KEY` already documented), `.gitignore` for the run cache.

### Gateway config purge — `infrastructure/litellm-config.yaml`

- Delete `model_list` entries: `qwen3.6` (:900), `qwen3.5:27b` (:1002), `qwen3.5:9b` (:1028/1037),
  `qwen3.5:9b--64k/--128k/--max-effort` (:1048–1105), `qwen3.5:4b--64k` (:1115/1124).
- Delete fallback chains `qwen3.5:9b` (:250, :279) and `qwen3.6` (:262, :273); update the tier comment
  at :875 and the ptait07/corsair header at :994. Keep `qwen3.5:397b-cloud`.
- Team/key repoint (live LiteLLM DB, read first, change via `/team/update` and `/key/update` in the
  same PR runbook): any team or key whose `models` list names a retired tag → `qwen3.8`
  (`design docs/teams-permissions-oss-implementation.md:122-127` table, `infrastructure/README.md:260-278`
  examples, azure-inference adapter key used by suite 18). Memory rule: update team before key.
- After deploy, `store_model_in_db: true` means DB-added rows can linger: check `GET /model/info` for
  retired names and remove with `/model/delete`; the new drift test enforces this.
- Add a note in `design docs/mcp-placeholder-services-audit-and-plan.md` runbook per the live-env
  durability rule (no secrets involved here).

## Order of work

1. mcp-cognizioware PR: endpoints + tests → promote to dev/uat (prod row can be seeded via `PUT` once live).
2. This repo PR A (e2e only, safe to merge first): registry lib, globalSetup discovery, sync script,
   fixture snapshot, suite rewrites. Registry initially seeded with the current catalog minus retired
   tags, so PR A's drift test **expects** the purge; run it against UAT CT204 first.
3. This repo PR B: `litellm-config.yaml` purge + docs + team repoint runbook → pipeline applies to UAT
   CT204 → full `run-all.sh` green on the LAN runner → merge/deploy to CT202.
4. Run `npm run models:sync -- --write` after deploy to stamp `discovered`.

## Verification

- `cd e2e && node scripts/sync-model-registry.js --dry-run` shows the expected/live diff with zero
  retired tags after the purge.
- `npx jest --testPathPattern='02|13' --runInBand --forceExit` against UAT (`LITELLM_BASE_URL` → CT204)
  then prod: Phase 0 passes, no `qwen3.5`/`qwen3.6` test names appear, Phase 6 probes only pulled tags,
  drift tests green; with the admin API unreachable, output shows `source fallback-file` and still passes.
- `bash ./run-all.sh` full run green on the LAN runner before the prod deploy (UAT-first hard rule).
- `curl -H "X-E2E-Ingest-Key: …" $ADMIN/api/v1/admin/e2e/models` returns the stamped registry with a
  fresh `updatedAt` after a run.
- `graphify update .` after the code changes.
