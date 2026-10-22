# Default trio router (LHH-TEV1-DEFAULT-TRIO-ROUTER)

A queue entry that arrives without a `trio` used to sit forever as
"unknown trio" — the launcher had no default it could fall back to, so the
entry stayed pending and an operator had to set the trio by hand. The
**default trio router** asks a typed decision model (tev1:4b served by
Ollama on the 2060 lane) to pick one instead.

The feature is **off by default** until Paxton flips `TRIO_ROUTER_ENABLED`
in the admin settings screen. Entries that already SET a trio are never
touched — the router only runs on the empty-trio path.

## Request shape

```
POST http://192.168.21.138:11436/v1/systemone
{
  "model": "tev1:4b",
  "state": {"task": "<task text, first 4000 chars>"},
  "questions": {
    "trio": {
      "type": "choice",
      "instructions": "Which harness trio should run this task?",
      "criteria": {
        "kimi":   "Kimi (synthetic Anthropic Sonnet) — long-running default lane.",
        "qwen":   "Local 9B qwen lane — fast/cheap but only safe for single-file tasks under ~150 lines.",
        "glm":    "Ollama Cloud glm-5.3 — the second Anthropic-compatible cloud lane.",
        "zai":    "Direct z.ai coding plan — high-throughput cloud lane.",
        "zaiq":   "Mixed z.ai manager/auditor + local qwen executor — small-task only.",
        "minimax":"MiniMax M Plan — MiniMax M family manager/executor.",
        "gemma":  "Local 2060 lane (gemma) — on-host fallback lane."
      }
    },
    "small": {
      "type": "noul",
      "instructions": "Is this task small enough for a 9B local model (one file, under 150 lines)?"
    }
  }
}
```

## Response shape

```
{
  "answers": {
    "trio": {
      "choice":         "kimi",
      "probabilities":  {"kimi": 0.76, "qwen": 0.14, "gemma": 0.08, "zai": 0.02},
      "confidence":     0.68
    },
    "small": {"noul": 0.70}
  }
}
```

## Decision logic

The router picks the model's `choice` only when **every** check passes:

1. `TRIO_ROUTER_ENABLED` is `true` in the settings store.
2. `entry.trio` is empty (the router never runs on entries that already
   carry a trio).
3. The picked trio is **enabled** in the settings store **and** appears in
   the launcher's merged `[queue.trios]` config (so the launcher can
   actually schedule a run on it).
4. `answers.trio.confidence ≥ TRIO_ROUTER_MIN_CONFIDENCE` (default 0.5).
5. For the local 9B trios (`qwen`, `zaiq`), `answers.small.noul ≥
   TRIO_ROUTER_SMALL_FLOOR` (default 0.6) — the router refuses to
   delegate work too big for a 9B local model to a local lane.

The HTTP call has a **30-second timeout**. A timeout, a 4xx/5xx, or a
malformed response falls back to the existing "unknown trio" skip and
records `reason="router_error"` (or `router_response_invalid`) on the
entry.

## Settings reference

| Setting | Default | Meaning |
|---|---|---|
| `TRIO_ROUTER_ENABLED` | `false` | Ask tev1 to pick a trio when `entry.trio` is empty. Off until Paxton flips it. |
| `TRIO_ROUTER_URL` | `http://192.168.21.138:11436` | Ollama base URL serving `/v1/systemone`. |
| `TRIO_ROUTER_MODEL` | `tev1:4b` | Model name the router asks for. |
| `TRIO_ROUTER_MIN_CONFIDENCE` | `0.5` | Confidence floor for the picked trio to take effect. |
| `TRIO_ROUTER_SMALL_FLOOR` | `0.6` | Required `small.noul` to route to a local 9B trio. |

Every setting is `restart=False` — a change from the admin settings screen
takes effect on the next launch pass. They appear in the admin screen
alongside the other queue settings (trios, capacity, time limits) because
they are entries in `settings_store.CATALOG`.

## Wiring

- `src/lh_harness/trio_router.py` — the router itself: `route_trio(entry,
  store, *, config_trios, http_call, timeout) -> str | None`. The module
  reads the four settings from the `SettingsStore` (and falls back to the
  process environment when no store is configured) and exposes a
  monkeypatchable `http_call` for tests.
- `src/lh_harness/queue.py` — `QueueEntry` gained a `trio_router: dict |
  None` field. It is persisted to disk alongside the entry (no migration
  required — `from_dict` already tolerates the absent key on legacy
  entries) and serialized through `to_dict/from_dict`.
- `src/lh_harness/settings_store.py` — the four `TRIO_ROUTER_*`
  `SettingSpec`s added to `CATALOG`; the admin settings screen renders
  them automatically.
- `src/lh_harness/launcher.py` — `_route_default_trio(entry)` is called
  inside `_launch()` after the `trio is None` check. When the router picks
  a trio, the launcher writes it back onto the entry (so the choice is
  visible in the queue JSON and Overseer1 reads it) before continuing
  with the normal launch flow. Entries that arrived with one are never
  touched.

## How Overseer1 audits the decision

Every call to `route_trio()` writes a decision record onto
`entry.trio_router` before returning, including the
fallback-without-routing cases. The record's shape:

```json
{
  "model":         "tev1:4b",
  "url":           "http://192.168.21.138:11436",
  "enabled":       true,
  "min_confidence": 0.5,
  "small_floor":   0.6,
  "probabilities": {"kimi": 0.76, "qwen": 0.14, "gemma": 0.08, "zai": 0.02},
  "confidence":    0.68,
  "small_noul":    0.7,
  "choice":        "kimi",
  "chosen":        "kimi",
  "reason":        "router_picked",
  "ts":            1761000000.123
}
```

The `reason` field is the audit handle:

| Reason | What happened |
|---|---|
| `router_picked` | The router chose `chosen` and the entry is launched. |
| `router_disabled` | `TRIO_ROUTER_ENABLED` is `false`. Entry falls back. |
| `router_no_choice` | Model response had no `choice`. Entry falls back. |
| `router_low_confidence` | Model confidence < `TRIO_ROUTER_MIN_CONFIDENCE`. Entry falls back. |
| `router_local_too_big` | Pick is a local 9B trio but `small.noul` < `TRIO_ROUTER_SMALL_FLOOR`. Entry falls back. |
| `router_pick_not_schedulable` | Model picked a trio not in the merged launcher config. Entry falls back. |
| `router_error` | `/v1/systemone` raised (timeout, network, 4xx/5xx). Entry falls back. |
| `router_response_invalid` | Response missing the `answers` block. Entry falls back. |
| `no_criteria` | Settings store + defaults produced no trios. Entry falls back. |

Read it back off the queue entry JSON at
`<runs-root>/queue/<queue-id>.json` — the field persists through
`QueueEntry.to_dict/from_dict`. The same field flows into the run's queue
outcome (the launcher's `entry.trio_router` is the run's audit surface
because the queue entry is rewritten at `mark_launched`).

## Operational guardrails (Paxton 2026-10-20)

- **Default OFF.** `TRIO_ROUTER_ENABLED` ships as `false`. A deployment
  that never opens the admin screen never sees the new path.
- **No live harness restart.** Settings changes are `restart=False`; the
  next launch pass picks them up.
- **No CT110 settings touched.** The admin screen applies only after an
  operator flips the setting.
- **No secrets.** The router URL is a public LAN address; the model is
  served behind the existing Ollama auth boundary if one is configured
  (the router itself adds no new credentials).
- **No host outside the workspace.** The router speaks to
  `192.168.21.138:11436` over the LAN; nothing else.