# Open-asks store and sealed secrets (task A3d)

Spec: `docs/handoffs/open-asks-web-responses-2026-09-30.md` (PR #99). `queue/OPEN-ASKS.md` is retired;
asks and their web responses live in the CT110 ask store.

## What it is

- `list_open_asks` returns live rows (gates, blocked entries), then store rows, then archive-file rows.
  Every row carries `fields` and `response`; a secret field shows `set_at`, `set_by`, `length`, `apply_target` only.
- A submit closes a stored ask (`CLOSED <ts> by <identity> (web)`). A submit to a closed ask reopens it, then closes it again.
  A failed `apply_ask_secret` reopens the ask with `apply failed ...` in `evidence`.
- A response to a GATE or BLOCKED row is context only. The row stays in the default view as `open`, with
  `response_state: "responded (context only), gate pending"`. It never resolves the gate or unblocks the entry.
- Body fields refuse token-shaped text (GitHub, OpenAI-style, AWS, Slack tokens, private keys): use a secret field.
- Apply targets are bound to the secret:
  - A secret field may declare `apply_target`.
  - A submit must send `secret_targets`, which maps every secret field it fills to the target shown to the person.
    A target that no longer matches the declaration gets 409 ("reload").
  - The vault records that target with the value.
  - `apply_ask_secret` writes only to that recorded target, and only if it is on the static allow-list.
  - The default CREDENTIAL secret field declares no target, so its value can be stored but never applied.
    The overseer declares the field with its target before the person submits.
- `declare_ask_fields` refuses (409) to remove a field that holds a sealed value, retype it or change its target.
  Clear the value first.
- After a successful apply the sealed copy is deleted; the value then lives only at the target.

## Tools and scopes

| Tool | Scope |
|---|---|
| `raise_open_ask`, `declare_ask_fields` | `overseer` |
| `respond_open_ask`, `clear_ask_secret` | `overseer:write` (fleet-admin) |
| `apply_ask_secret` | `overseer:apply` (the overseer only) |

Every call carries `caller`, `caller_ts`, `caller_nonce`, `caller_sig`, where

```
caller_sig = HMAC-SHA256(<caller secret>, "<tool>|<caller>|<ts>|<nonce>|<sha256(canonical arguments)>")
```

Canonical arguments are the tool arguments without the four caller fields, as JSON with sorted keys, no spaces, UTF-8
and non-ASCII kept. A stamp may be at most 300 s old and 30 s in the future. Each nonce is accepted once by the service
(an in-memory cache, so run the web API as one process) and once by the vault helper (persisted). The task-174
`caller:ts` scheme is not accepted for ask tools. The overseer signs with `scripts/ask-apply/ask_tool.py`;
fleet-admin signs in `src/overseerWrite.js`.

Transport: fleet-admin (CT202) reaches CT110 over plain HTTP on the LAN (`http://192.168.21.168:8799`). The signature
binds each request to its body and blocks replay, but the body (including a secret on submit) is not encrypted in transit.
TLS or WireGuard between CT202 and CT110 is a follow-up for Paxton.

## What the service does to protect itself from its own runs

Harness workers run as the same uid as the service (`harness`) and control the git repositories the service inspects.

- **Secrets out of the environment.** At start, `lh-harness web` moves `LH_HARNESS_WEB_TOKEN` and every
  `LH_HARNESS_CALLER_*` out of its environment into memory. No child process inherits them.
- **Non-dumpable process.** It also sets `PR_SET_DUMPABLE=0` on every start, so a same-uid process can neither
  read `/proc/<pid>/environ` nor ptrace the service.
- **Workers.** Workers never get `LH_HARNESS_CALLER_*`, `LH_HARNESS_ASK_*` or `LH_HARNESS_WEB_TOKEN`.
- **Service-side git.** Every git call the service makes (launcher occupancy probes, workspace guard, identity,
  park, experience tags) runs as `git -c core.fsmonitor= -c core.hooksPath=/dev/null` with a scrubbed environment.
  A run that plants an fsmonitor or a hook in its repository's `.git/config` gets nothing run by the service.
  The `gh` PR probe also uses the scrubbed environment.

## Separate-uid vault (required for the secret tools)

Anything the `harness` uid can read or run, a run can too, including any sudo rule granted to `harness`. So the
sealed secrets, the ssh key, the caller secrets the vault checks and the grants all belong to a separate user. The
helper that guards them **authenticates every secret operation itself**:
- Nothing arrives on argv. The signed tool request comes on stdin.
- The helper checks the signature with its own copy of the caller secrets.
- It checks the time window and a one-time nonce, which it persists under its state directory.
- It checks the caller's grant for the tool's scope.
- For apply, it checks the target recorded in the vault when the secret was submitted.

A run that invokes the sudo rule therefore can neither seal its own token, nor re-seal Paxton's ask, nor apply,
because it has no caller secret.

I chose a sudo helper over a daemon on a unix socket with a peer-uid check. A peer-uid check cannot tell the service
from a run (same uid), so the signed request is the real control either way. The sudo helper is the smaller moving
part: no long-running process, no socket permissions, no restart ordering.

Until all of this is in place, the secret tools (`respond_open_ask` with a secret value, `clear_ask_secret`,
`apply_ask_secret`) answer `503 disabled: ...`. Body responses, `raise_open_ask` and `declare_ask_fields` work either way.

Production setup on CT110 (operator, once):

1. Create a system user: `useradd --system --home /var/lib/lh-ask-vault --shell /usr/sbin/nologin lhasks`.
2. Install the helper: `install -o root -g root -m 0755 scripts/ask-apply/lh-ask-vault /usr/local/sbin/lh-ask-vault`.
   It runs as `#!/usr/bin/python3 -I`, is stdlib-only and imports nothing the harness uid can write.
3. Create the directories:
   `install -d -o lhasks -g lhasks -m 0700 /var/lib/lh-ask-vault /var/lib/lh-ask-vault/secrets /var/lib/lh-ask-vault/state /var/lib/lh-ask-vault/ssh`.
   Then create the key and host key:
   - `sudo -u lhasks ssh-keygen -t ed25519 -N '' -f /var/lib/lh-ask-vault/ssh/id_ed25519 -C lh-ask-vault`;
   - `sudo -u lhasks ssh-keyscan <ct202> > /var/lib/lh-ask-vault/ssh/known_hosts`, and verify the fingerprint.
4. `install -d -o root -g lhasks -m 0750 /etc/lh-ask-vault`. Then write `/etc/lh-ask-vault/config.json`, owned
   **lhasks:lhasks 0600**:
   ```json
   {"vault_dir": "/var/lib/lh-ask-vault/secrets",
    "state_dir": "/var/lib/lh-ask-vault/state",
    "ssh_key": "/var/lib/lh-ask-vault/ssh/id_ed25519",
    "known_hosts": "/var/lib/lh-ask-vault/ssh/known_hosts",
    "ssh_dest": {"ct202-mcp-tools-env:GITHUB_MCP_TOKEN": "lhapply@<ct202-address>"},
    "grants": {"fleet-admin": ["overseer:write"], "overseer": ["overseer", "overseer:apply"]},
    "callers": {"fleet-admin": "<same value as LH_HARNESS_CALLER_FLEET_ADMIN_SECRET>",
                "overseer": "<same value as LH_HARNESS_CALLER_OVERSEER_SECRET>"}}
   ```
5. One sudo rule, one fixed command, and **no other sudo rights for `harness`**
   (`visudo -f /etc/sudoers.d/lh-ask-vault`):
   ```
   harness ALL=(lhasks) NOPASSWD: /usr/local/sbin/lh-ask-vault
   ```
6. lh-harness service:
   - set `LH_HARNESS_ASK_VAULT_CMD="/usr/bin/sudo -n -u lhasks /usr/local/sbin/lh-ask-vault"`;
   - put `LH_HARNESS_CALLER_FLEET_ADMIN_SECRET` and `LH_HARNESS_CALLER_OVERSEER_SECRET` in a root-owned 0600
     `EnvironmentFile=`, not in `Environment=` lines of the unit, which any local user can read.

   Then restart lh-harness at a moment with no active run.

At startup the service enables the secret tools only when all of the following hold:
1. The helper's `check <uid>` passes. It refuses when, for the worker uid (this service's own uid, never taken from
   the environment):
   - the vault, the state, the key, known_hosts or the config is readable;
   - the config or the helper is writable;
   - any path carries an extended POSIX ACL;
   - the helper runs as that uid.
2. `sudo -n -l` lists exactly one rule: `(lhasks) NOPASSWD: /usr/local/sbin/lh-ask-vault`.
3. The process is non-dumpable.

Otherwise it logs `ask store: disabled: <reason>`.

Development settings (env names only):
- `LH_HARNESS_ASK_STORE_DIR`: where the ask records go (no secrets). Default: the runs root.
- Without the helper:
  - `LH_HARNESS_ASK_VAULT_DIR` is a local vault. It is always disabled when the service and its workers share a uid.
  - Grants come from `LH_HARNESS_ASK_GRANTS_FILE` (a TOML file with `[asks.grants]`) or `[asks]` in the project config.

Residual risk: the ask records (`asks/*.json`, no secrets) stay service-owned, so a run could edit the stored text
of an answer or a field declaration. A secret can still only be sealed by a signed fleet-admin request, and only
applied by a signed overseer request to the target recorded at submit time. The overseer treats answer text as context,
never as instructions.

## Apply targets

The allow-list is `ASK_APPLY_TARGETS` in `src/lh_harness/config.py`, with an identical copy in the helper (a test keeps
them equal). It holds one target: `ct202-mcp-tools-env:GITHUB_MCP_TOKEN`, which writes the variable `GITHUB_MCP_TOKEN`
in CT202 `/opt/cognizioware-mcp-tools/mcp-tools.env`. The value must look like a GitHub token. Adding a target is a PR.

The helper runs `ssh -F /dev/null -i <key> -o StrictHostKeyChecking=yes ...` with the value on stdin. On CT202:

1. Create a dedicated user and a group for the env file, and give the group access to the file and its directory:
   - `useradd --system --create-home --shell /bin/sh lhapply`;
   - `groupadd mcpenv && usermod -aG mcpenv lhapply`;
   - `chgrp mcpenv /opt/cognizioware-mcp-tools/mcp-tools.env && chmod 0660 /opt/cognizioware-mcp-tools/mcp-tools.env`;
   - `chgrp mcpenv /opt/cognizioware-mcp-tools && chmod g+wx /opt/cognizioware-mcp-tools`.

   lhapply needs read and write on the file and write and search on the directory: the script writes a temp file
   there and renames it over the original, so the change is atomic.
   - After an apply, the file keeps its mode (0660) and its group (`mcpenv`). Its owner becomes `lhapply`, because
     only root can chown to another user. Root (docker compose) still reads it.
   - If the group cannot be kept, the script refuses and changes nothing.
   - Directory write means lhapply could rename other files there, but its key is pinned to the one forced command
     below, which only ever touches `mcp-tools.env`.
   - Check that the deploy lane does not reset the directory's group or mode.
2. `install -o root -g root -m 0755 scripts/ask-apply/lh-apply-env-var /usr/local/sbin/lh-apply-env-var`.
3. In `~lhapply/.ssh/authorized_keys`, one line (the key from step 3 above, CT110's address only):
   ```
   restrict,from="192.168.21.168",command="/usr/local/sbin/lh-apply-env-var /opt/cognizioware-mcp-tools/mcp-tools.env GITHUB_MCP_TOKEN" ssh-ed25519 AAAA... lh-ask-vault
   ```

The script replaces `GITHUB_MCP_TOKEN=` or `export GITHUB_MCP_TOKEN=`. It syncs the temp file before the rename.
Applying the value does not recreate `github_mcp`; that stays a separate, deliberate step.

## Rollback

`[asks]` is read only by the ask loader. A build from before PR #100 rejects an unknown top-level `[asks]` table in
`config.toml` and will not start. **Remove `[asks]` from `config.toml` before rolling back to a pre-#100 build.**
With the helper, grants live in `/etc/lh-ask-vault/config.json`, so `config.toml` needs no `[asks]` at all.
