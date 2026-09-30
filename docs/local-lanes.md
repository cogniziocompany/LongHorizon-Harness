# Local lanes: qwen3.8 / ornith-1.5 on two RTX 3090s

## What they are
Local inference runs on Proxmox ptait01 (192.168.21.110): two RTX 3090s (24 GB each), one Ollama
instance per card, both reading one shared model-blob volume.

| Lane | Container | GPU |
|---|---|---|
| `:11441` | `cognizioware-ollama-gpu1` | RTX 3090 #1 |
| `:11438` | `cognizioware-ollama-cloud` | RTX 3090 #0 (also the Ollama Cloud relay and embeddings) |

- Model `ornith-1.5:9b-256k` (hybrid SSM; tools, thinking, vision), about 11.3 GB VRAM per card at full context.
- One request at a time per lane (`n_seq_max = 1`; `OLLAMA_NUM_PARALLEL` has no effect). **Local concurrency is 2.**
- Gateway aliases `qwen3.8`, `qwen3.8-nothink`, `general-chat`, `qwen3.6`, `ornith-1.5`, `ornith-1.5--256k` all route to
  these two lanes. "qwen" and "ornith" are one pool of 2, not two pools.
- Context: proven at 262144 (needle test, 186k-token prompt, 2026-09-10). Harness budget: 128k.

## Harness settings
- Trio caps live in the runtime config `.lh-harness/config.toml` (gitignored, per node), read only when the launcher starts:

      [queue.capacity]
      kimi_max = 3
      qwen_max = 2

  All local trios together must not exceed 2. A change needs an lh-harness restart, and a restart kills every live run
  (`KillMode=control-group`), so do it when no run is active.
- Slow local models can trip the no-output stall watchdog (floor 120 s, a quarter of the episode budget, cap 900 s).
  Ops override: `LH_HARNESS_STALL_SECONDS` in the lh-harness service environment (for example 600). Also needs a restart.
- A lane unloads after Ollama's 5-minute idle keep-alive; reload takes 0.5 to 8.6 s plus prompt prefill. Allow about 30 s
  for a cold first response.

## Landmines
- Never send `num_ctx`: it reloads the lane under running work. The 256k comes from the tag's own `num_ctx 262144`;
  the containers default to 16k, so a tag without it loads at 16k.
- `ornith-1.5:pool` currently points at tag `ornith-1.5:9b` (no `num_ctx`, loads at 16k while advertising 262144).
  Operator fix (LiteLLM admin API, no YAML edit, no router restart): `POST /model/update` on both `ornith-1.5:pool`
  rows setting `litellm_params.model` to `ollama_chat/ornith-1.5:9b-256k`, keeping `rpm: 20`; then read back with
  `GET /model/info`.
- Proving a request reached a GPU: count `POST     "/api/chat"` (quoted, multi-space) in `docker logs` on both lane
  containers before and after, with a unique prompt per request. A 200 from the router can be a cache hit.

Sources: cognizioware-mcp-tools `design docs/handoff-docs/ornith-db-owned-local-lane.md`,
`proving-ollama-lane-routing.md`.
