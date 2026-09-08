# Harness task queue

The task queue lives inside the LongHorizon-Harness service so that any fleet
client — Open WebUI chat, Hydra, or plain curl — can enqueue a run and watch it
start. The queue is durable (atomic JSON files under the configured runs root),
priority-ordered, and gated by capacity rules that are expressed in config.

## Enqueue clients

| Client | Mechanism |
|---|---|
| Open WebUI | LiteLLM MCP gateway alias **`lhharness`**, access group **`fleet-runners`** |
| Hydra | Deep links to `/api/queue` + optional queue panel that POSTs enqueue |
| curl / scripts | `POST /api/queue` or `POST /api/mcp/fleet/harness_enqueue_task` |

## REST API

All queue endpoints reuse the same bearer-token boundary as the rest of
`/api/*`.

### `POST /api/queue`

Create a queue entry. Returns immediately; the launcher starts the run when
capacity allows.

```json
{
  "name": "fix login redirect",
  "task": "Repo: LongHorizon-Harness. Branch: feat/login. Deliverables: ... Hard rules: ...",
  "workspace": "./workspace",
  "trio": "kimi",
  "max_rounds": 25,
  "priority": 0,
  "base_check": "origin/main",
  "requested_by": "openwebui"
}
```

Either `task` (text) or `task_file` (absolute server path) must be supplied, not
both. The response is `{"ok": true, "queue_id": "q-<16hex>"}`.

### `GET /api/queue`

List entries. Optional `?status=pending|launched|done|failed`. The response
contains `entries`, `groups`, and `counts`.

### `DELETE /api/queue/{queue_id}`

Only pending entries can be deleted.

### `POST /api/queue/{queue_id}/priority`

Change the priority of a pending entry.

### `GET /api/queue/config`

Return the effective `[queue.*]` configuration (trios and capacity) the launcher
uses.

## Fleet MCP tools (`lhharness`)

The service exposes four MCP tools under the LiteLLM gateway alias
**`lhharness`** (access group **`fleet-runners`**). A separate gateway bridge
forwards JSON-RPC tool calls to the HTTP endpoints below.

| Tool | Endpoint | Purpose |
|---|---|---|
| `harness_enqueue_task` | `POST /api/mcp/fleet/harness_enqueue_task` | Enqueue a run |
| `harness_list_queue` | `POST /api/mcp/fleet/harness_list_queue` | List queue |
| `harness_run_status` | `POST /api/mcp/fleet/harness_run_status` | Check run status |
| `harness_resolve_gate` | `POST /api/mcp/fleet/harness_resolve_gate` | Resolve an approval gate |

Tool descriptions carry the operating rules:

- Task text must be scoped with repo, branch, deliverables, and hard rules.
- `kimi` is for development work; `qwen` is for QA only.
- Production deploys go through deployment lanes, never through the queue.
- `harness_resolve_gate` rejects any `user_input` that contains non-ASCII
  characters.

## Capacity rules

Capacity is configured under `[queue.capacity]`:

| Key | Default | Meaning |
|---|---|---|
| `kimi_max` | 3 | Concurrent kimi runs allowed |
| `qwen_max` | 1 | Concurrent qwen runs allowed (QA only, one at a time) |
| `min_healthy_keys` | 2 | Healthy Ollama Cloud keys required before kimi launches |
| `key_health_url` | `""` | URL that returns `{ "healthy_keys": [{"healthy": true}, ...] }` |
| `poll_seconds` | 15 | Launcher poll interval |

The launcher also skips any workspace that already has an active run, and it is
idempotent across restarts (a launched entry is never launched again).

## Example `config.toml` block

```toml
[queue.trios.kimi]
agent = "claude_code"
model = "kimi-k2.7-code:cloud"
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
