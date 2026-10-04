# Open-asks store and sealed secrets (task A3d)

Spec: `docs/handoffs/open-asks-web-responses-2026-09-30.md` (PR #99). `queue/OPEN-ASKS.md` is retired;
asks and their web responses live in the CT110 ask store.

## What it is

- `list_open_asks` returns live rows (gates, blocked entries), then store rows, then archive-file rows.
  Every row carries `fields` and `response`; a secret field shows `set_at`, `set_by`, `length` only.
- Store files are in the runs root: `asks/<id>.json` and `asks-secrets/<id>/<field>` (dirs 0700, files 0600).
- A submit closes the row (`CLOSED <ts> by <email> (web)`). A submit to a closed row reopens it, then closes it again.
  A failed `apply_ask_secret` reopens the row with `apply failed ...` in `evidence`.
- A response to a GATE or BLOCKED row is context only: it never resolves the gate or unblocks the entry.
- After a successful apply the sealed copy is deleted; the value then lives only at the target.

## Tools and scopes

| Tool | Scope |
|---|---|
| `raise_open_ask`, `declare_ask_fields` | `overseer` |
| `respond_open_ask`, `clear_ask_secret` | `overseer:write` (fleet-admin) |
| `apply_ask_secret` | `overseer:apply` (the overseer only) |

Every call needs a signed caller (`caller`, `caller_ts`, `caller_sig`, HMAC over `<caller>:<ts>` with
`LH_HARNESS_CALLER_<NAME>_SECRET`). Scopes come only from `[asks.grants]`; nothing is granted by default, and the
table does not turn on task-174 `[callers]` scoping for the other tools.

```toml
[asks.grants]
"fleet-admin" = ["overseer:write"]
overseer = ["overseer", "overseer:apply"]
```

The overseer calls its tools with `scripts/ask-apply/ask_tool.py` (it signs; an LLM cannot through the gateway).

## Apply targets

The allow-list is `ASK_APPLY_TARGETS` in `src/lh_harness/config.py` and holds one target:
`ct202-mcp-tools-env:GITHUB_MCP_TOKEN` (CT202 `/opt/cognizioware-mcp-tools/mcp-tools.env`, variable
`GITHUB_MCP_TOKEN`, value must look like a GitHub token). Adding a target is a PR.

The writer runs `ssh -i $LH_HARNESS_ASK_APPLY_SSH_KEY $LH_HARNESS_ASK_APPLY_CT202_SSH` with the value on stdin.
On the target host the key is pinned to `scripts/ask-apply/lh-apply-env-var` as a forced command:

```
command="/usr/local/sbin/lh-apply-env-var /opt/cognizioware-mcp-tools/mcp-tools.env GITHUB_MCP_TOKEN",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA... lh-harness-ask-apply
```

Optional: `LH_HARNESS_ASK_APPLY_KNOWN_HOSTS` (path to a known_hosts file; host keys are always checked strictly).
Applying the value does not recreate `github_mcp`; that stays a separate, deliberate step.
