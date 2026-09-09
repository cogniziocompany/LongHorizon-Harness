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

## Queue entry contract

This is the field-level interface the chat agent and any enqueue client code
against. Every queue entry is a `QueueEntry` dataclass
(`src/lh_harness/queue.py:58`–`79`), persisted as one atomic JSON file at
`runs_root/queue/<queue_id>.json` (`_QUEUE_DIR="queue"` `queue.py:23`; `_write`
via `_atomic_bytes_write` `queue.py:324`–`326`). It survives API restarts and is
visible to any caller with the bearer token through `GET /api/queue`.

### Fields

| Field | Type | Default | Validation | Written by |
|---|---|---|---|---|
| `queue_id` | `str` | — (minted) | `q-<16 hex>` from `uuid.uuid4().hex[:16]` (`queue.py:355`); never caller-supplied | Store, at `create` |
| `name` | `str` | — (required) | non-empty after strip; ≤256 chars; no NUL (`_validate_name`) | Caller, at enqueue |
| `task` | `str` | — (required) | non-empty after strip; ≤100,000 chars; no NUL. Supplied as `task` (text) **or** `task_file` (server path read into `task`), not both (`queue.py:232`–`239`) | Caller, at enqueue |
| `workspace` | `str` | — (required) | non-empty after strip; ≤4096 chars; no NUL (`_validate_workspace`) | Caller, at enqueue |
| `max_rounds` | `int` | `25` (`DEFAULT_MAX_ROUNDS`, `types.py:41`) when omitted | integer (bool rejected); `1 ≤ value ≤ 1000` (`MAX_ROUNDS`, `types.py:40`) (`_validate_max_rounds`) | Caller, at enqueue |
| `trio` | `str` | — (required) | one of `kimi`, `qwen` (lower-cased). Supplied as `roles` **or** `trio` (`queue.py:242`) (`_validate_trio`) | Caller, at enqueue |
| `priority` | `int` | `0` when omitted | integer (bool rejected) (`_validate_priority`) | Caller, at enqueue (mutable via `POST /api/queue/{id}/priority` while pending) |
| `requested_by` | `str` | — (required) | non-empty after strip; ≤256 chars (`_validate_requested_by`) | Caller, at enqueue |
| `base_check` | `str` | `""` | string, stripped (`_validate_base_check`) | Caller, at enqueue |
| `status` | `str` | `"pending"` | one of `pending`, `launched`, `done`, `failed` (`_VALID_STATUS` `queue.py:28`) | Store, on every transition |
| `run_id` | `str \| None` | `None` | set when the entry is launched | Store, via `mark_launched` (`queue.py:430`–`439`) |
| `reason` | `str \| None` | `None` | truncated to 4,000 chars (`_MAX_QUEUE_REASON_CHARS` `queue.py:26`) | Store, via `mark_done` (optional) / `mark_failed` (required) |
| `skip_reasons` | `list[str]` | `[]` | appended only while `pending`; each item truncated to 4,000 chars | Store, via `record_skip` (`queue.py:458`–`465`) |
| `created_at` | `float` | `time.time()` at create | epoch seconds | Store, at `create` |
| `updated_at` | `float` | `time.time()` at create | epoch seconds; re-stamped on every write | Store, on every `update` |
| `launched_at` | `float \| None` | `None` | epoch seconds; set at launch | Store, via `mark_launched` (`queue.py:438`) |
| `last_checked_at` | `float \| None` | `None` | epoch seconds; stamped when the launcher evaluates the entry | Launcher (`launcher.py:206`, `:350`, `:365`) — never set by the store or the API |
| `dedup_key` | `str \| None` | `None` | see *Idempotent enqueue* below | Caller, at enqueue |

There is no dedicated `done_at`/`failed_at` field. The terminal time of an entry
is the `updated_at` value at the moment `mark_done` or `mark_failed` runs (both
go through `update`, `queue.py:406`–`409`); the terminal cause is in `reason`.

### Caller-supplied vs. service-owned

A caller supplies nine input fields through the enqueue body: `name`, `task` (or
`task_file`), `workspace`, `trio` (or `roles`), `max_rounds`, `priority`,
`base_check`, `requested_by`, and `dedup_key`. Of these, `max_rounds`,
`priority`, `base_check`, and `dedup_key` are optional; the rest are required.
`task_file` and `roles` are alternative input keys for `task` and `trio`
respectively, not separate stored fields.

The store owns everything else. At `create` it mints `queue_id`, stamps
`created_at`/`updated_at`, and sets `status="pending"`; `run_id`, `launched_at`,
`reason`, `skip_reasons`, and `last_checked_at` begin at their defaults (`None`
/ `[]`). Status transitions are made only by the store's `mark_launched` /
`mark_done` / `mark_failed` / `record_skip` methods, and `last_checked_at` is
written only by the launcher. A caller cannot set `queue_id`, `status`,
`run_id`, `launched_at`, `reason`, `skip_reasons`, `created_at`, `updated_at`,
or `last_checked_at` through the enqueue body — `QueueStore.create` reads only
the nine input fields via `_normalize_request` (`queue.py:228`–`253`).

## Idempotent enqueue (`dedup_key`)

`dedup_key` is the single-orchestrator floor: two orchestrators (or a retrying
client) asking for the same work resolve to one queue entry, so the work can be
launched at most once. It was added in commit `0de7b2d3`; the `dedup_key` field
is the 18th `QueueEntry` field (`queue.py:79`).

**Validation** (`_validate_dedup_key`, `queue.py:213`–`225`):

- `None` → stored as `None` (no key; the entry is never de-duplicated).
- Empty or whitespace-only → `None` (same as omitted). The value is stripped
  before any further check, so `" key "` and `"key"` are the same key.
- Non-string, including `bool` → `ValueError("dedup_key must be a string")`.
- Longer than 256 characters after stripping →
  `ValueError("dedup_key is too long")` (`_MAX_QUEUE_DEDUP_CHARS = 256`,
  `queue.py:27`).
- Contains a NUL byte (`\x00`) → `ValueError("dedup_key contains a NUL byte")`.

**De-duplication** (`QueueStore.create`, `queue.py:328`–`359`):

- When `dedup_key` is a non-empty string, `create` looks for an existing
  **non-terminal** entry (status `pending` or `launched`,
  `_NON_TERMINAL_STATUS` `queue.py:32`) with the same key
  (`_find_non_terminal_by_dedup`, `queue.py:361`–`366`).
- If one exists, `create` returns that existing entry **unchanged** — no new
  `queue_id`, no new file, no status change. Two enqueues with the same key
  therefore yield one entry.
- Otherwise `create` mints a fresh `q-<16 hex>` and writes a new entry.
- A key is freed once its entry reaches a terminal state (`done` or `failed`).
  Reusing the key afterwards creates a **fresh entry** — a retry — rather than
  returning the terminal one.

The launcher's `mark_launched` pending-guard (`queue.py:430`–`439`: it raises
`ValueError("entry is not pending")` unless `status == "pending"`) then ensures
that single entry is launched at most once. Together: same key → one entry → at
most one launch.

**Residual (honest).** The lookup is a read-then-write over the entry directory.
It removes duplicate enqueues from sequential or retrying callers, but a
simultaneous cross-process race — two `create` calls that both miss the lookup
before either writes — is a narrow window this code does **not** close. The full
single-orchestrator guarantee against that window is the **launcher lease**
proposed as **new work** in the migration plan (§4; explicitly `NEW WORK`, and
it collides with task 48). The guarantee today is "harmless via idempotency"
(this commit), not "impossible via lease."

## Enqueue and result behavior

`POST /api/queue` (`create_queue_entry`, `server.py:1030`–`1038`):

- Passes the request body wholesale to `QueueStore.create(body)` — there is no
  server-side field allowlist, so every input field above, including
  `dedup_key`, is forwarded to the store with no `server.py` change.
- On `ValueError` (any validation failure, including an invalid `dedup_key`)
  returns `422` with `detail=str(exc)`.
- Returns `501` if no `runs_root` is configured (no queue store).
- On success returns `{"ok": true, "queue_id": "q-<16hex>"}` — the same shape
  whether a new entry was created or an existing non-terminal entry was returned
  by de-duplication. A caller cannot tell from the response alone which
  happened; it can only observe that the `queue_id` is stable for a given
  `dedup_key`.

`dedup_key` is recoverable after enqueue: `QueueEntry.to_dict` serializes all 18
fields (`queue.py:81`–`86`, via `asdict`), and `GET /api/queue` returns each
entry through `to_dict` (`server.py:1060`, `:1062`), so the key is visible on
entries and groups. This audit-level visibility is established by the dataclass
serialization; it is not asserted by a dedicated endpoint test. The hermetic
test `test_api_dedup_key_passes_through` (`tests/webapi/test_queue_dedup.py`)
proves behavioral forwarding end-to-end — two `POST /api/queue` calls with the
same key yield one entry and a single pending count — but does not assert the
key appears in any response or audit field.

## Liveness (queue depth)

The fleet liveness heartbeat reports real queue depth, not a placeholder. When a
`QueueStore` is configured, `_heartbeat` (`server.py:659`–`682`) reports
`queue_len = counts["pending"] + counts["launched"]` from the durable store —
the non-terminal entries waiting for or undergoing launch
(`server.py:677`–`679`). The fleet reporter itself is a fail-open side-car that
starts only when `LH_HARNESS_FLEET_URL` is set (`server.py:645`); with the env
var unset the web server behaves exactly as before. (This is the queue-depth
half of the liveness signal, delivered in commit `3efb79bd`; the launcher
last-tick half is deferred to the migration plan §4 as new work.)
