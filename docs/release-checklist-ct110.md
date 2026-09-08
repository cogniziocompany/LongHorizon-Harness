# CT110 release checklist

This checklist captures the exact deploy/rollback steps for the LongHorizon-Harness queue feature on the CT110 environment. All steps assume the service runs from the harness project directory on the CT110 host and that the launcher has already been moved server-side.

## 1. Before the deploy — abort running runs

The new supervisor owns run lifecycle. Any in-flight runs started by the old CLI-on-overseer flow must be cleanly terminated before the service is restarted on the new commit.

1. Stop submitting new queue entries or direct runs.
2. For each currently active run:
   - Resolve any pending approval gates with `action: stop` and `reason: pre-deploy abort`.
   - Wait for the run to reach a terminal status (`completed`, `cancelled`, `failed`, or `blocked`).
   - If a worker is stuck, kill the worker process from the service host and let the supervisor mark the run terminal.
3. Verify no active runs remain:
   ```bash
   curl -H "Authorization: Bearer $TOKEN" \
        https://$SERVICE_HOST/api/runs?status=running
   ```
4. Stop the old overseer-side launcher process if it is still running.

## 2. Deploy with `deploy-harness.sh`

Run the standard deploy script from the project checkout on CT110:

```bash
cd /opt/longhorizon-harness  # or whatever deploy-harness.sh expects
./deploy-harness.sh
```

The script must:

- Pull the target branch/commit for the queue feature.
- Rebuild the Web bundle (`src/lh_harness/_frontend/web/dist`) if the frontend changed.
- Recreate or upgrade `.venv` / `.venv-dev` with the current `pyproject.toml` dependencies.
- Restart the systemd service (or equivalent) that runs `lh-harness web`.

After restart, confirm the service reports `ok` and exposes the queue endpoints:

```bash
curl -H "Authorization: Bearer $TOKEN" \
     https://$SERVICE_HOST/api/meta | jq '.capabilities'
curl -H "Authorization: Bearer $TOKEN" \
     https://$SERVICE_HOST/api/queue/config
```

## 3. Exact CT110 `config.toml`

Place this file at the project working directory used by the service process, e.g. `/opt/longhorizon-harness/.lh-harness/config.toml`. The queue directory lives under the configured `runs_root`, not next to it.

```toml
[service]
runs_root = "/var/lib/lh-harness/runs"

[queue.trios.kimi]
agent = "claude_code"
model = "claude-sonnet-5"
mcp_profile = "ops"

[queue.trios.qwen]
agent = "codex"
model = "qwen3.8"
mcp_profile = "audit"

[queue.capacity]
kimi_max = 3
qwen_max = 1
min_healthy_keys = 2
key_health_url = "https://litellm.easybutt0n.ai/health"
poll_seconds = 15
```

Key points:

- `runs_root` is the top-level run storage directory. The launcher will create `queue/` (entries, service events) under this root automatically.
- `kimi` is the development trio; `qwen` is QA-only.
- `min_healthy_keys` gates `kimi` launches against the LiteLLM health endpoint.

## 4. Resume operations

Once the deploy is healthy and at least one service-side launch has been observed:

1. Confirm a test queue entry reaches `launched` and then `done` through the local API:
   ```bash
   curl -H "Authorization: Bearer $TOKEN" -X POST \
        https://$SERVICE_HOST/api/queue \
        -d '{"name":"post-deploy smoke","task":"Repo: ...","workspace":"...","trio":"kimi","requested_by":"ops"}'
   ```
2. Resolve any approval gate it creates.
3. Verify the run report shows `completion_satisfied: true` and that `queue.launched` appears in the run events.
4. Resume normal queue traffic and fleet MCP usage.

## 5. Retire the overseer launcher

After the first successful service-side launch is observed, the launcher on the overseer PC is retired.

- Disable and stop the old overseer-side launcher service/cron.
- Remove any `lh-harness queue` wrapper scripts or systemd units that ran on the overseer PC.
- Going forward, the only launcher process is the asyncio task embedded in the Web API service on CT110.
