# Task 47 — Synthetic LLM provider into the router and the deployment pipeline

Paxton added a new provider on 2026-09-08 and asked for it to be deployed through the pipeline, with instructions where an operator will
find them. Queue entry `C:/tmp/queue/0001-47-synthetic-provider.json`, task text `C:/tmp/synthetic-provider-task.txt`.

## Verified live (overseer, from CT202)
- OpenAI-compatible base `https://api.synthetic.new/openai/v1` (an Anthropic-compatible base also exists at `/anthropic/v1`).
- Auth `Authorization: Bearer ${SYNTHETIC_API_KEY}`. **The key is on CT202 and CT204 only**, in `/opt/cognizioware-mcp-tools/mcp-tools.env`,
  mode 600, backed up as `mcp-tools.env.bak-synthetic-*`. It is not in this repo and must never be — env name only, per the standing rule.
- `GET /models` → 11 ids, and a completion through `syn:small:text` succeeded with usage returned.

| id | context | note |
|---|---|---|
| `hf:moonshotai/Kimi-K3` | 512k | `syn:large:vision` alias |
| `hf:zai-org/GLM-5.3-Flash` | 512k | beta, `syn:large:text` alias |
| `hf:zai-org/GLM-5.2` | 512k | |
| `hf:Qwen/Qwen3.8-27B` | 256k | `syn:small:vision` alias |
| `hf:nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4` | 256k | |
| `hf:zai-org/GLM-4.7-Flash` | 192k | `syn:small:text` alias |
| `hf:openai/gpt-oss-120b` | 128k | |

## Subscription
Two packs. Each pack: **1000 requests / 5 hours**, **2 concurrent per model**. The task configures to the documented single-pack limits with
one named knob to raise it, rather than assuming the packs add — that gets verified by observation first.

## Why this matters beyond adding a provider
Synthetic serves **Kimi K3** and the **GLM** family, which is exactly what the harness trios use. The task appends Synthetic to the **end** of the
existing fallback chains, so an exhausted Ollama key falls through instead of failing a round. Today six of seven Ollama keys are rate limited and
the fleet is running on the local model; this is the structural answer to that.

## Where the instructions go
The gateway guides MCP catalog (`infrastructure/docker/gateway-guides-mcp/src/index.ts`), which is the router's own how-to surface — what it is,
the model ids and context sizes, the alias mapping, the limits, the env name, and when to prefer it (long context to 512k, or a rate-limited pool).
Plus a provider-table row in `infrastructure/README.md`.

## Deployment
Through the mcp-tools lane as usual: E2E gate → apply to UAT CT204 → UAT QA gate → CT202. Verify after each apply that `/v1/models` lists the new
ids and that one completion succeeds. Nothing else in the router may change position or meaning.
