# Embedded settings store

lh-harness keeps its configuration and credentials in a SQLite database it creates and migrates itself.
Admins see and change them at `/admin` in the lh-harness web UI. These values used to live in
`/home/harness/.lh-harness-secrets.env` and the unit's `Environment=` lines.

## What it does

- **Secrets are write-only.**
  - Encrypted at rest with AES-256-GCM; the setting name is bound in, so ciphertexts cannot be swapped between rows.
  - No API, CLI or UI ever returns a secret value. Each one shows only who set it, when, and its length.
  - Names that look like credentials (`*TOKEN*`, `*KEY*`, `*SECRET*`, `*PASSWORD*`, `*CREDENTIAL*`) are always treated as secrets.
- **Plain settings** (URLs, labels, node name) are shown and edited in plain text.
- **Audit log.** Every set, rotate, annotate and unset is recorded with who and when, never the value.
- **Precedence: DB over env.** `lh-harness web` loads the store before it reads any setting. A value in the store
  replaces the same name from the environment; names the store does not hold keep their environment value.
- **Restarts.** Values are read at start. The admin screen marks every change made since the service started
  ("restart needed"). Restart lh-harness when no run is active.
- **Runs keep their access.** Runs keep full MCP tool access: gateway keys, `GH_TOKEN` and provider keys still reach
  workers exactly as before. Only the bootstrap variables below and the caller and vault settings
  (`LH_HARNESS_CALLER_*`, `LH_HARNESS_ASK_*`, `LH_HARNESS_WEB_TOKEN`) are kept from workers.

## Bootstrap (the only things left in the environment)

| Variable | Meaning |
|---|---|
| `LH_HARNESS_SETTINGS_KEY` | Encryption key, at least 32 characters. Without it the store is off and the environment is used as before. |
| `LH_HARNESS_SETTINGS_DB` | Database path. Default `~/.lh-harness/settings.db`, created with file 0600 and directory 0700. |
| `LH_HARNESS_SETTINGS_ADMINS` | Comma-separated SSO e-mails allowed into `/admin`. Empty means nobody (fail closed). |
| `LH_HARNESS_SETTINGS_PROXY_AUTH` | Secret the SSO edge sends as `X-LH-Proxy-Auth`. Unset means nobody. |

The service removes the key and the proxy secret from its environment after reading them.

## Who is an admin

All of the following must hold:
1. The request carries the web bearer. On `lh-harness.easybutt0n.ai`, Caddy adds it after SSO.
2. `X-LH-Proxy-Auth` matches `LH_HARNESS_SETTINGS_PROXY_AUTH`, so a LAN caller with the bearer cannot claim an identity.
3. The SSO e-mail (`X-Auth-Request-Email`, set by oauth2-proxy) is on `LH_HARNESS_SETTINGS_ADMINS`.

Writes also need `X-Requested-With: lh-harness`, which the admin page sends.

The Caddy change for the `lh-harness.easybutt0n.ai` `@api` block lives in cognizioware-mcp-tools and is not made here:

```
request_header -X-Auth-Request-Email
forward_auth lh-harness-oauth2-proxy:4180 { uri /oauth2/auth; copy_headers X-Auth-Request-Email }
reverse_proxy 192.168.21.168:8799 {
    header_up Authorization "Bearer {env.LH_HARNESS_UPSTREAM_TOKEN}"
    header_up X-LH-Proxy-Auth {env.LH_HARNESS_SETTINGS_PROXY_AUTH}
}
```

## Screens

- **Settings** (`/admin`): every known setting plus any custom one, with set/unset, source (db/env/unset), last change,
  note and expiry. You can set, rotate or remove a value. Secret inputs are password fields and never prefilled.
- **Key restrictions** (`/admin/keys`): every key and caller the node knows, never with a value:
  - the web bearer;
  - each harness caller, with its tools, run control, ceilings and open-asks scopes;
  - the MCP gateway key, with the MCP profiles and servers runs get;
  - `GH_TOKEN`, the fleet key, the database password, provider and Seq keys.

  For keys whose scope is enforced elsewhere (GitHub, the gateway), record the scope and the expiry in the setting's
  note and expiry fields.
- **Audit log** (`/admin/audit`).

## Moving from the env file

1. Set `LH_HARNESS_SETTINGS_KEY` (and optionally `LH_HARNESS_SETTINGS_DB`) for the harness user, then run the one-shot import:
   ```
   lh-harness settings import --env-file /home/harness/.lh-harness-secrets.env --actor <your name>
   lh-harness settings list
   ```
   Both print names and outcomes only, never values. Empty placeholders and bootstrap variables are skipped.
2. Add the four bootstrap variables to the unit's EnvironmentFile and restart lh-harness when no run is active.
   Check `/admin` shows the values with source `db`.
3. Remove the imported lines from `/home/harness/.lh-harness-secrets.env`, keeping only the bootstrap variables.
   Restart once more, and check that the API, the queue and a test run work.
