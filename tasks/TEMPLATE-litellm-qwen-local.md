# TASK TEMPLATE — LiteLLM-routed runs with local qwen3.8 executor

The benchmarked model pattern for this deployment (settled 2026-08-30 after live A/B — see
`design docs`/memory in the operator session). Copy this file, fill the blanks, launch via the
web API or `lh-run`.

## The model trio (why each seat)

| Role | Model | Why |
|---|---|---|
| Manager | `kimi-k2.7-code:cloud` | ~20s planning turns (qwen3.8 took 4–5 min for the same turn); decomposition quality shapes the run |
| Executor | `qwen3.8` (local, dual RTX 3090 via LiteLLM `--64k` lane) | ~80% of run tokens — the seat that justifies local GPU. Proven tool-calling AFTER the two fixes below |
| Auditor | `kimi-k3:cloud` | Only family verified to emit the strict 3-line control header reliably; malformed headers fail rounds |

**Never seat `minimax-m3:cloud` as executor** — it cannot see tool results through LiteLLM and
confabulates "environment broken" reports. **Nor as manager** (2026-08-30): it ignores operator
gate resolves — repeats the same ask through consumed answers.

**Cloud-executor standard (updated 2026-08-31):** manager `glm-5.3:cloud`; executor
`kimi-k2.7-code:cloud`. glm-5.3-flash:cloud (multi-account pooled, 3x max-key weighting) is the
router-level fallback/chat model but cannot hold the executor seat -- its reasoning blocks break
the Anthropic adapter ("Content block is not a thinking block"). deepseek-v4-flash does not
exist upstream; the flash chain falls back to kimi-k2.7-code:cloud.

## Non-negotiable plumbing (or qwen3.8 silently breaks)

1. **`--strict-mcp-config`** must be in the Claude adapter spawn (in main since 2026-08-30).
   Without it, a workspace `.mcp.json` with a large MCP inventory pushes Bash/Read schemas out
   of even the 64K window → the model hallucinates a fake tool list and refuses to act.
2. **Context ≥32K** on the executor model (empirical floor for Claude Code tool schemas;
   `qwen3.8` must resolve to the 64K lane, not a 16K `num_ctx` deployment).
3. **Workspace `.lh-harness/config.toml`** — qwen3.8 runs honest 20–30 min rounds at ~43 tok/s;
   the 1800s default kills them mid-flight, and auditors that run builds/tests trip the
   read-only guard on churn files:

```toml
[run]
guard_exclude_paths = ["node_modules", "dist", "build", "logs"]  # + repo-specific churn files

[run.timeouts]
manager = 300
gui_executor = 3600
cli_executor = 3600
auditor = 600
```

## Launch payload (web API `POST /api/runs`)

```json
{
  "task": "<see task-description checklist below>",
  "agent": "claude_code",
  "model": "kimi-k2.7-code:cloud",
  "workspace": "<absolute path>",
  "max_rounds": 25,
  "prompt_language": "en",
  "roles": {
    "manager":  {"agent": "claude_code", "model": "kimi-k2.7-code:cloud"},
    "executor": {"agent": "claude_code", "model": "qwen3.8"},
    "auditor":  {"agent": "claude_code", "model": "kimi-k3:cloud"}
  }
}
```

Top-level `"model"` is REQUIRED even with per-role configs: omitting it lets the workspace
config's model leak into the worker, which then dies with
`supervised run model does not match its reservation`.

## Task-description checklist (each learned the hard way)

- **Point at a committed file** in the workspace as the authoritative spec ("Read X FIRST with
  your file tools"); never a URL the agent must fetch (private repos 404 to WebFetch, and
  GitHub branch names with slashes get mis-parsed).
- **State repo provenance**: which branch/commit is authoritative, and any uncommitted state
  living elsewhere.
- **Boundaries**: commit small + well-described, do NOT push; deploys/live-service changes are
  human-gated — "raise an approval gate instead of executing".
- **Environment lanes**: name the dev/UAT surface explicitly; prod ids and prod switches are
  gated.
- **Secrets**: reference by path outside the workspace ("token at ~/.x, never into the repo or
  commits"), and pre-state known credential limits so the agent reports them as blockers
  instead of debugging its own 403s.
- **Operator context block**: tell the agents the run is monitored via the web API, that the
  operator can inject instructions (`POST /api/runs/{id}/instructions`) and resolve gates
  remotely, and that reports must name files/commits/ids precisely for a remote reader.
- **Honesty rail**: "if a step needs credentials/services you cannot reach, record exactly
  what is blocked and continue with the next implementable item — do not fabricate results."

## Operating the run

- Audit verdict grammar: `complete|incomplete / clean|violation / aligned|needs_revision|unknown`.
  `incomplete/clean/aligned` is a HEALTHY mid-milestone verdict. Guard-only violations fail the
  round, not the run.
- Round budget exhausted? Evaluate audited progress; the default is
  `POST /resume {"mode":"continue"}` for another block of rounds — the ledger carries forward.
- Config/adapter changes need a worker restart to load: `POST /stop`, then `/resume` with
  `{"mode":"continue"}` — audited progress survives.
- One web server per port, always started with the correct `--workspace-root`; a bare restart
  from the wrong directory orphans live workers and blanks the dashboard.
- Langfuse: filter traces by tag `lh-run/<run_id>` (per-key logging on the LiteLLM virtual key).
