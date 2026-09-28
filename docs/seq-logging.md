# Seq logging

LongHorizon-Harness can ship its Python logging records to a Seq server
(e.g. `https://seq.easybutt0n.ai`) as structured CLEF events.  This gives
every WSL dev shell, the CT110 systemd service, and every `lh-harness-node`
Docker fleet instance a single queryable log stream.  Shipping is **opt-in
and off by default**; it activates only when `SEQ_URL` is set.

The implementation (`src/lh_harness/seq_logging.py`) mirrors the fleet
reporter (`src/lh_harness/fleet/reporter.py`): a logging.Handler subclass
backed by one daemon thread and a bounded queue, using only the standard
library (`urllib.request`).  No third-party dependency is added.

## Enabling Seq logging

Set these environment variables before starting any `lh-harness` command
(`run`, `web`, `dashboard`, `plugins` — all dispatch through `cli.main()`,
which installs the handler):

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `SEQ_URL` | yes (to enable) | *(none)* | Seq server origin, e.g. `https://seq.easybutt0n.ai`. Unset = disabled. |
| `SEQ_API_KEY` | no | *(none)* | Seq ingestion API key sent as the `X-Seq-ApiKey` header. **Optional** — sent when present, omitted when not. |
| `SEQ_MIN_LEVEL` | no | `INFO` | Minimum Python logging level shipped to Seq (case-insensitive name or number). |

When `SEQ_URL` is unset, no handler is created, no thread is started, and
`emit()` is a no-op — the system behaves exactly as before.

### Authentication (amendment 2026-09-14)

`SEQ_API_KEY` is **optional**.  The dev Seq instance accepts unauthenticated
ingestion: `POST /api/events/raw?clef` with no API key returns HTTP 201.
The handler therefore:

- sends the `X-Seq-ApiKey` header when a key is configured;
- omits the header entirely when no key is configured, with **no warning
  and no downgrade** — absence of a key is never treated as incomplete
  configuration, never blocks startup, and never suppresses logging.

Do not add a guard that refuses to log without a key; that is a bug.  If the
Seq server is ever hardened to require keys, add `SEQ_API_KEY` to the
environment and every node picks it up on restart — no code change needed.

## What is shipped

The handler is attached to the **root logger** from `cli.main()`, so it
captures everything: lh-harness's own loggers, plus uvicorn/FastAPI access
and error logs from `lh-harness web` (an explicit decision).

Delivery is fully isolated from the emitting threads:

- `emit()` formats the record into a CLEF dictionary and enqueues it with
  `put_nowait`; it **never raises and never blocks** the caller.
- One daemon thread batches enqueued events every ~2 seconds.
- Each batch is newline-delimited JSON, gzip-compressed, and POSTed to
  `{SEQ_URL}/api/events/raw?clef` with `Content-Type:
  application/vnd.serilog.clef` and `Content-Encoding: gzip`.
- Transient failures (5xx, connection errors) are retried up to 3 times
  with a short backoff.  4xx responses are **not retried** (malformed
  payload or rejected auth) — they are warn-once logged locally instead.
- When the 10 000-event queue is full or any flush ultimately fails, the
  handler drops events and warns at most once per 5 minutes — the same
  warn-once idiom as the fleet reporter.

Every Python level at or above `SEQ_MIN_LEVEL` is shipped; below it the
standard logging level machinery filters the record before `emit()` runs.

## CLEF event shape

One compact JSON object per line:

```json
{
  "@t": "2026-09-14T12:00:00.123000Z",
  "@l": "Information",
  "@m": "run 20250914T120000Z_abcd1234 status -> running",
  "@mt": "run {0} status -> {1}",
  "logger": "lh_harness.manager",
  "app": "lh-harness",
  "version": "0.1.7",
  "node": "ct110",
  "kind": "ct110",
  "repo": "LongHorizon-Harness"
}
```

Standard properties added to every event:

- `@t` — UTC ISO-8601 timestamp of the log record.
- `@l` — Seq (Serilog) level, see the mapping below.
- `@m` — the fully formatted message; `@mt` — the raw message template when
  the record was a %-format string.
- `@x` — the full formatted exception (`traceback.format_exc` style) when
  the record carries `exc_info`, so stack traces are searchable in Seq.
- `logger` — the `logging.getLogger` name.
- `app` — always `lh-harness`.
- `version` — the installed lh-harness version.
- `node` — `LH_HARNESS_FLEET_NODE` when set, else `socket.gethostname()`.
- labels parsed from `LH_HARNESS_FLEET_LABELS` (the same
  `fleet.reporter._parse_labels` `key=value` scheme as fleet reporting —
  there is deliberately no second labelling mechanism).
- any `extra={...}` attributes passed to the logging call that are not
  standard `LogRecord` fields appear as additional CLEF properties
  (coerced to JSON-safe values; unserializable values become strings).

### Level mapping

| Python level | Seq (Serilog) level |
| --- | --- |
| `CRITICAL` | `Fatal` |
| `ERROR` | `Error` |
| `WARNING` | `Warning` |
| `INFO` | `Information` |
| `DEBUG` | `Debug` |
| below `DEBUG` | `Verbose` |

## Querying and alerting in Seq

In the Seq UI filter bar, the standard properties narrow the stream fast:

- `app = 'lh-harness'` — only lh-harness events.
- `node = 'ct110'` — one machine (compose nodes set
  `LH_HARNESS_FLEET_NODE`; everything else falls back to the hostname).
- `repo = 'LongHorizon-Harness'` — via labels when configured.
- `@Level = 'Error' or @Level = 'Fatal'` — errors only.
- `Has(@x)` — events carrying a stack trace.
- `@Exception like '%TimeoutError%'` — search inside exception text.

For alerting, save a signal from any of these filters (e.g. `app =
'lh-harness' and @Level in ['Error', 'Fatal']`) and attach a Seq alert
with your preferred notification channel, exactly as you would for any
other Seq application.  Because events are batched for ~2 s, allow that
much latency in alert windows.

## Configuration surfaces

- **Local / WSL dev**: export the variables in your shell profile, or run a
  temporary local Seq (`docker run -p 5341:80 datalust/seq`) and point
  `SEQ_URL=http://127.0.0.1:5341` at it — no key is needed.
- **CT110 prod**: `SEQ_URL` and `SEQ_API_KEY` live in
  `/home/harness/.lh-harness-secrets.env` (the same `EnvironmentFile`
  pattern the fleet variables use); a `systemctl restart lh-harness`
  applies them.  Service restarts on CT110 are performed in a counted
  zero-active window — never from inside a running task.
- **Fleet nodes** (`docker/compose.node.yml`): `SEQ_URL` defaults to
  `https://seq.easybutt0n.ai`; `SEQ_API_KEY` has no default and is passed
  through when provided — with no key the node still ships events.

## Privacy note

Seq collects the text of log messages and exception stack traces — the
same content already written to the console and to local journals — plus
the small structured `extra` fields callers attach.  It does **not** ship
run transcripts, trajectories, prompts, or thinking blocks (those remain
the fleet reporter's domain, trimmed separately).  Log records are
developer-visible operational output, so keep credentials and secrets out
of `log.info(...)` calls as usual; `@x` stack traces can contain local
variable paths but not values.  If you do not want log text to leave the
machine, leave `SEQ_URL` unset.
