# lh-harness-node — Dockerized fleet node

Packages this repo's LongHorizon-Harness build as a container that the
**cognizioware-hydra** device agent installs and manages on fleet machines
(`harness_install` / `harness_status` / `harness_restart` / `harness_http`).

## Placements

| Placement | How | Notes |
|---|---|---|
| Managed node (any Docker host, incl. Windows via Docker Desktop/WSL2) | Hydra `POST /harness/nodes {kind:"managed", deviceId}` → agent runs this compose file | Web API on `127.0.0.1:8799` only; reached via the agent's WS proxy |
| External node (CT110) | Pre-existing systemd install at `https://harness.lan.easybutt0n.ai` | Registered as `kind:"external"`; the reference instance, default primary + overflow fallback for placement. This image is behavior-equivalent to it |

## Config surface

Identical to every other deployment — `.lh-harness/config.toml` (`[run]`,
`[run.roles.*]`, `[run.timeouts]` with ONLY `manager/gui_executor/cli_executor/auditor`),
`LH_HARNESS_WEB_TOKEN`, `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` for LiteLLM
routing and model discovery. The entrypoint seeds a fleet default at the
workspace root; a per-repo `.lh-harness/config.toml` inside a checkout wins.

Gotchas that still apply (see `cognizioware-how-to.md`):
- `POST /api/runs` requires top-level `"model"` even with `roles`, or the
  workspace config leaks and the worker dies.
- Config is read at worker start — `POST /stop` then `/resume {"mode":"continue"}`
  to apply timeout changes.

## Local smoke

```bash
docker build -f docker/Dockerfile -t lh-harness-node:latest .
LH_HARNESS_WEB_TOKEN=$(openssl rand -hex 24) \
ANTHROPIC_AUTH_TOKEN=<litellm-virtual-key> \
docker compose -f docker/compose.node.yml up -d
curl -H "Authorization: Bearer $LH_HARNESS_WEB_TOKEN" http://127.0.0.1:8799/api/meta
```

`/api/meta` should list capabilities and the LiteLLM-discovered model catalog
(`qwen3.8`, `kimi-k2.7-code:cloud`, `kimi-k3:cloud`, ...).
