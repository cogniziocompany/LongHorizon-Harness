# Open-asks store and sealed secrets (task A3d)

Spec: `docs/handoffs/open-asks-web-responses-2026-09-30.md` (PR #99). `queue/OPEN-ASKS.md` is retired;
asks and their web responses live in the CT110 ask store.

## What it is

- `list_open_asks` returns live rows (gates, blocked entries), then store rows, then archive-file rows.
  Every row carries `fields` and `response`; a secret field shows `set_at`, `set_by`, `length` only.
- A submit closes a stored ask (`CLOSED <ts> by <email> (web)`). A submit to a closed ask reopens it, then closes it again.
  A failed `apply_ask_secret` reopens the ask with `apply failed ...` in `evidence`.
- A response to a GATE or BLOCKED row is context only. The row stays in the default view as `open`, with
  `response_state: "responded (context only), gate pending"`. It never resolves the gate or unblocks the entry.
- Body fields refuse token-shaped text (GitHub, OpenAI-style, AWS, Slack tokens, private keys): use a secret field.
- A secret field may declare `apply_target`. `apply_ask_secret` only writes to the target the field declared, and
  only if that target is on the static allow-list. The default CREDENTIAL secret field declares none, so the
  overseer must `declare_ask_fields` with the target before a value can be applied.
- After a successful apply the sealed copy is deleted; the value then lives only at the target.

## Tools and scopes

| Tool | Scope |
|---|---|
| `raise_open_ask`, `declare_ask_fields` | `overseer` |
| `respond_open_ask`, `clear_ask_secret` | `overseer:write` (fleet-admin) |
| `apply_ask_secret` | `overseer:apply` (the overseer only) |

Every call carries `caller`, `caller_ts`, `caller_nonce`, `caller_sig`, where

```
caller_sig = HMAC-SHA256(LH_HARNESS_CALLER_<NAME>_SECRET,
                         "<tool>|<caller>|<ts>|<nonce>|<sha256(canonical arguments)>")
```

Canonical arguments are the tool arguments without the four caller fields, as JSON with sorted keys, no spaces, UTF-8
and non-ASCII kept. A stamp may be at most 300 s old and 30 s in the future; each nonce is accepted once (the cache is
per process, so run the web API as one process). The task-174 `caller:ts` scheme is not accepted for ask tools.
The overseer signs with `scripts/ask-apply/ask_tool.py`; fleet-admin signs in `src/overseerWrite.js`.

Transport: fleet-admin (CT202) reaches CT110 over plain HTTP on the LAN (`http://192.168.21.168:8799`). The signature
binds each request to its body and blocks replay, but the body (including a secret on submit) is not encrypted in transit.
TLS or WireGuard between CT202 and CT110 is a follow-up for Paxton.

## Separate-uid vault (required for the secret tools)

Harness workers run as the same uid as the service (`harness`), so anything the service can read, a run can read.
The secret tools (`respond_open_ask` with a secret value, `clear_ask_secret`, `apply_ask_secret`) therefore stay
**disabled** (`503 disabled: store readable by worker uid ...`) until the sealed store, the ssh key and the grants
are owned by a different uid. Body responses, `raise_open_ask` and `declare_ask_fields` work either way.

Production setup on CT110 (operator, once):

1. Create a system user: `useradd --system --home /var/lib/lh-ask-vault --shell /usr/sbin/nologin lhasks`.
2. Install the helper: `install -o root -g root -m 0755 scripts/ask-apply/lh-ask-vault /usr/local/sbin/lh-ask-vault`.
   It is stdlib-only Python and imports nothing the harness uid can write.
3. `install -d -o lhasks -g lhasks -m 0700 /var/lib/lh-ask-vault /var/lib/lh-ask-vault/secrets /var/lib/lh-ask-vault/ssh`.
   Then `sudo -u lhasks ssh-keygen -t ed25519 -N '' -f /var/lib/lh-ask-vault/ssh/id_ed25519 -C lh-ask-vault`.
   Then `sudo -u lhasks ssh-keyscan <ct202> > /var/lib/lh-ask-vault/ssh/known_hosts`, and verify the fingerprint.
4. `install -d -o root -g lhasks -m 0750 /etc/lh-ask-vault` and write `/etc/lh-ask-vault/config.json` (root:lhasks 0640):
   ```json
   {"vault_dir": "/var/lib/lh-ask-vault/secrets",
    "ssh_key": "/var/lib/lh-ask-vault/ssh/id_ed25519",
    "known_hosts": "/var/lib/lh-ask-vault/ssh/known_hosts",
    "ssh_dest": {"ct202-mcp-tools-env:GITHUB_MCP_TOKEN": "lhapply@<ct202-address>"},
    "grants": {"fleet-admin": ["overseer:write"], "overseer": ["overseer", "overseer:apply"]}}
   ```
5. One sudo rule, one fixed command (`visudo -f /etc/sudoers.d/lh-ask-vault`):
   ```
   harness ALL=(lhasks) NOPASSWD: /usr/local/sbin/lh-ask-vault
   ```
6. lh-harness service environment:
   - `LH_HARNESS_ASK_VAULT_CMD="/usr/bin/sudo -n -u lhasks /usr/local/sbin/lh-ask-vault"`;
   - `LH_HARNESS_CALLER_FLEET_ADMIN_SECRET` and `LH_HARNESS_CALLER_OVERSEER_SECRET` in a root-owned 0600
     `EnvironmentFile=`, not in `Environment=` lines of the unit, which any local user can read.

   Then restart lh-harness at a moment with no active run.

At startup the service asks the helper for the grants and runs `lh-ask-vault check <worker uid>`. The check refuses
when any of the following holds:
- the vault, the key, known_hosts or the config is readable by the worker uid;
- the config or the helper is writable by the worker uid;
- the helper runs as the worker uid.

The service also makes itself non-dumpable, so a same-uid worker cannot read its `/proc/<pid>/environ` or ptrace it.
Any failure keeps the secret tools disabled and logs why. Workers never inherit `LH_HARNESS_CALLER_*`,
`LH_HARNESS_ASK_*` or `LH_HARNESS_WEB_TOKEN`.

Other settings (env names only):
- `LH_HARNESS_ASK_STORE_DIR`: where the ask records go (no secrets). Default: the runs root.
- `LH_HARNESS_WORKER_UID`: the uid workers run as. Default: the service's own uid.
- Without the helper:
  - `LH_HARNESS_ASK_VAULT_DIR` is a local vault for development; it is disabled while readable by the worker uid.
  - Grants come from `LH_HARNESS_ASK_GRANTS_FILE` (a TOML file with `[asks.grants]`) or `[asks]` in the project config.

## Apply targets

The allow-list is `ASK_APPLY_TARGETS` in `src/lh_harness/config.py`, with an identical copy in the helper (a test keeps
them equal). It holds one target: `ct202-mcp-tools-env:GITHUB_MCP_TOKEN`, which writes the variable `GITHUB_MCP_TOKEN`
in CT202 `/opt/cognizioware-mcp-tools/mcp-tools.env`. The value must look like a GitHub token. Adding a target is a PR.

The helper runs `ssh -F /dev/null -i <key> -o StrictHostKeyChecking=yes ...` with the value on stdin. On CT202:

1. Create a dedicated user `lhapply`. It needs write access to `/opt/cognizioware-mcp-tools/` and to `mcp-tools.env`.
   Grant only that (group or ACL); do not use root.
2. `install -o root -g root -m 0755 scripts/ask-apply/lh-apply-env-var /usr/local/sbin/lh-apply-env-var`.
3. In `~lhapply/.ssh/authorized_keys`, one line (the key from step 3 above, CT110's address only):
   ```
   restrict,from="192.168.21.168",command="/usr/local/sbin/lh-apply-env-var /opt/cognizioware-mcp-tools/mcp-tools.env GITHUB_MCP_TOKEN" ssh-ed25519 AAAA... lh-ask-vault
   ```

The script replaces `GITHUB_MCP_TOKEN=` or `export GITHUB_MCP_TOKEN=`, keeping mode and owner. It syncs the temp file
before the atomic rename. Applying the value does not recreate `github_mcp`; that stays a separate, deliberate step.

## Rollback

`[asks]` is read only by the ask loader. A build from before PR #100 rejects an unknown top-level `[asks]` table in
`config.toml` and will not start. **Remove `[asks]` from `config.toml` before rolling back to a pre-#100 build.**
With the helper, grants live in `/etc/lh-ask-vault/config.json`, so `config.toml` needs no `[asks]` at all.
