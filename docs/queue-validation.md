# Queue validation (`lh-validate-queue`)

`src/lh_harness/validate_queue.py` is the fleet's cross-boundary queue
validator: it checks the references no generic file-graph validator can see —
queue entry → harness Python source, queue entry → live gateway registry,
queue → queue numbering. Every rule exists because it caught a real defect
(task 218; the prototype found four true positives on its first run against
the live queue).

## Entry points

```bash
python -m lh_harness.validate_queue
lh-validate-queue            # [project.scripts] console entry point
```

`QUEUE_DIR` and `HARNESS_SRC` override the checked paths; the defaults point
at the in-repo layout. The module docstring carries the coverage disclaimer:
it proves that identifiers, numbers, files, and aliases it can see actually
exist — it does **not** validate prose semantics.

## Exit semantics and output format

The validator is read-only: it never writes to the queue or to any queue
entry, and it keeps no seen-findings state. Exit is `0` when clean or
WARN-only, non-zero only on ERROR — WARN is advisory, ERROR needs a
disposition, so the validator can gate a scheduled tick without blocking on
advisories.

The first two output lines are stable and machine-readable:

```
validate_queue: N live entries checked (QUEUE_DIR)
validate_queue findings: E errors, W warns
```

Individual findings follow, grouped under `ERROR` / `WARN` headers.

## Scheduled tick invocation

The scheduled tick runs on the Windows overseer, outside this repo — this
document describes the contract the tick consumes; no tick-loop code lives
here. Each tick the overseer invokes one entry point above and:

- records the finding **COUNT** in the ledger row in one clause: `E + W` from
  the `validate_queue findings: E errors, W warns` line (any `skip`-style
  rules that did not run contribute nothing — they emit no finding at all);
- enumerates only **NEW** findings — the overseer diffs this tick's finding
  lines against the previous tick's (the validator deliberately persists
  nothing);
- treats exit `0` + WARNs as advisory-only and any non-zero exit as needing a
  disposition.

## `gateway-alias-exists` and the gateway key

The gateway-alias rule checks aliases named in task files (or in an entry's
`mcp_alias` / `gateway_alias` / `mcp_gateway_alias` field) against the live
registry `GET {gateway root}/v1/mcp/server`. The gateway root derives from
this repo's own gateway configuration (`lh_harness/mcp_profiles.py`;
`LH_HARNESS_MCP_GATEWAY_URL` override), and the Bearer key comes from the
`LH_HARNESS_MCP_GATEWAY_KEY` env var — **name only; the value is never read
into findings and never printed**. With no key configured the rule skips
entirely: no task-file scan, no HTTP request, no finding, no exit-code
effect. Registry fetch failures surface as `gateway-registry-unavailable`
WARNs so a hiccup never gates a tick. The fetch is a single swappable
function; all tests mock it and no test contacts the live gateway.

## Tests

`tests/test_validate_queue.py` builds fixture queues in `tmp_path` —
never the live queue — covering every rule including the warn-only/error
exit semantics, the no-key skip path, the no-write guarantee, and the
no-secret-values output guard.
