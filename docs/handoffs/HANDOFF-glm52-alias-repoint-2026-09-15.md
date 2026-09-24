# Handoff: repoint `glm-5.2:synthetic-anthropic` off the retired upstream — for the overseer (or Paxton)

Written 2026-09-15 by session [fe2679]. Paxton said "fix this". The read side is done; the WRITE to the prod
router was blocked by this session's permission classifier (shared-resource change), so the write is yours.

## STATUS — EXECUTED AND VERIFIED 2026-09-15 08:28 PT. DO NOT RE-RUN THE WRITE.

Added by scheduled tick #196 (2026-09-15 20:45 PT). This file sat for 12 hours still reading
"the write is yours" / "Overseer executes" AFTER the write had already landed, and on 2026-09-15 16:12 PT
tick #170 had to stop and cross-check the ledger before acting on it. **Re-running `/model/update` would be a
duplicate write against the PROD router (CT202).**

**DONE — evidence, from LEDGER row `2026-09-15 08:28 PT`:**
- BEFORE: `glm-5.2:synthetic-anthropic` id `fd0770d4-b747-461c-aee0-1b0d09182ef5` = `custom_openai/hf:zai-org/GLM-5.2` (`db_model: true`).
- `POST /model/update` -> 200, run inside CT202, master key never printed.
- AFTER (read back, per RULE 1): the same row reads `custom_openai/hf:moonshotai/Kimi-K3`, and **exactly one**
  `glm-5.2:synthetic-anthropic` row exists.
- Probe `POST /v1/messages`, `max_tokens=1`, fresh nonce `1789486053149708374` -> **HTTP 200** (was 404 the day before).
- OPEN-ASKS entry "gateway alias points at a retired model" moved to Answered.
- Read-back step 3 (emit-probe line) is **N/A, not skipped**: the launcher trios no longer reference this alias.

**STILL OPEN — the yaml twin only, and it is deliberately deferred, not forgotten:**
`glm-5.2:synthetic` (`87b1d6c0…5a8e`) is a **yaml** row and still reads `openai/hf:zai-org/GLM-5.2`. Fixing it
means editing `infrastructure/litellm-config.yaml`, and **any edit to that file force-recreates the CT202 prod
router** — so it is folded into the next `litellm-config.yaml` PR and merged only in a **zero-active-run window**.
Nothing consumes the alias today, so it 404s harmlessly in the meantime.

---
## Measured on CT202 (`docker exec cognizioware-mcp-tools-litellm printenv LITELLM_MASTER_KEY`, 57 chars, never printed)
Two rows carry the retired model `hf:zai-org/GLM-5.2`:
| model_name | id | litellm_params.model | owner |
|---|---|---|---|
| `glm-5.2:synthetic-anthropic` | `fd0770d4-b747-461c-aee0-1b0d09182ef5` | `custom_openai/hf:zai-org/GLM-5.2` | **DB row** (`db_model: true`) — fix live |
| `glm-5.2:synthetic` | `87b1d6c0…5a8e` | `openai/hf:zai-org/GLM-5.2` | yaml row (`db_model: false`) — needs a litellm-config.yaml edit = router recreate = zero-run window |
Target upstream = the one `kimi-k3:synthetic-anthropic` (`62120999-74b6-482e-a1f9-d7b50308d446`) uses:
`custom_openai/hf:moonshotai/Kimi-K3`, same api_base `https://api.synthetic.new/openai/v1`, same api_key reference.
Probe before the change (this session, 1 token, unique nonce): HTTP 404 "hf:zai-org/GLM-5.2 is no longer supported".

## The write (run INSIDE CT202 so the key never leaves the box)
My first attempt used `{"model_id": ..., "litellm_params": {...}}` and the router answered `{"error": ...}`
(body not captured — capture it). The documented shape for this LiteLLM version is:
```bash
K=$(docker exec cognizioware-mcp-tools-litellm printenv LITELLM_MASTER_KEY)
curl -s localhost:4000/model/info -H "Authorization: Bearer $K" > /tmp/mi.json
python3 - <<'PY'
import json
d=json.load(open('/tmp/mi.json'))['data']
g=[m for m in d if m['model_name']=='glm-5.2:synthetic-anthropic'][0]
k=[m for m in d if m['model_name']=='kimi-k3:synthetic-anthropic'][0]
lp=dict(g['litellm_params']); lp['model']=k['litellm_params']['model']
for f in ('input_cost_per_token','output_cost_per_token','cache_read_input_token_cost'):
    if f in k['litellm_params']: lp[f]=k['litellm_params'][f]
json.dump({'model_name':'glm-5.2:synthetic-anthropic','litellm_params':lp,'model_info':{'id':g['model_info']['id']}},open('/tmp/upd.json','w'))
PY
curl -s -X POST localhost:4000/model/update -H "Authorization: Bearer $K" -H 'Content-Type: application/json' -d @/tmp/upd.json
```
If `/model/update` still errors, the fallback that has worked before on this router (LEDGER, task 143/151 era) is
delete + re-add: `POST /model/delete {"id": "fd0770d4-…"}` then `POST /model/new` with the same body minus
`model_info.id` — read back that exactly ONE `glm-5.2:synthetic-anthropic` row exists afterwards.

## Read-back (RULE 1 — a 200 is not proof)
1. `GET /model/info` → the `fd0770d4…` row (or its replacement) shows `custom_openai/hf:moonshotai/Kimi-K3`.
2. `POST /v1/messages` with `model: glm-5.2:synthetic-anthropic`, `max_tokens: 1`, a fresh nonce in the prompt
   (the response cache hides a dead backend) → HTTP 200 and the served model reads as Kimi-K3.
3. The launcher's next emit-probe line shows no `glm-5.2 … 404`.
4. `glm-5.2:synthetic` (yaml) stays broken until the yaml edit; note it in `docs/config-authority.md` and queue the
   yaml change for the next zero-run window (or delete that alias if nothing consumes it — check spend logs by
   model_group for the last 7 days first).

## Then
OPEN-ASKS: move "gateway alias points at a retired model" to Answered with the read-back evidence. Ledger row.
