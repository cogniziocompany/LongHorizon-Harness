# Handoff: writable responses on the Overseer "Open asks" page

## Session & agent identity — for a reviewing agent

**This session**
| field | value |
|---|---|
| Agent name (address it with this) | none issued; this session was never given a fleet or harness agent name |
| Short ref | `[6515d7]` |
| Session UUID | `6515d718-e37f-4142-aa66-a1240535ed94` |
| Repo / cwd | `c:\Users\PaxtonTait\source\LongHorizon-Harness` |
| Role | Interactive VSCode Claude Code session. Discovery and handoff only: no code changed, no cron. |

**Consumer:** Overseer1 builds this as **A3d** (relayed by `longhorizon-harness-21` [b50385]).
**Written:** 2026-09-30. **Updated:** 2026-10-03 with Paxton's answers and the A3a/A3b changes.
**Verified against:** LongHorizon-Harness `origin/main` `b6f51f9`, and cognizioware-mcp-tools `origin/main` as fetched 2026-10-03.

---

## Context: what was asked

On `fleet.easybutt0n.ai/overseer`, the **Open asks** tab is read-only. Paxton wants to respond to each open-ask row from that page. A response has two field types:
- **response body**: free text
- **secret input**: masked; the value is never shown back

One ask can have **more than one** of each field; the count depends on the ask. Every field stays **editable** from the web UI.

Motivating row: `279-gateway-github-token`, kind CREDENTIAL. The ask is a fine-grained GitHub PAT for the LiteLLM gateway's `github_mcp`, stored as `GITHUB_MCP_TOKEN` in CT202 `/opt/cognizioware-mcp-tools/mcp-tools.env`. The live value is an 11-character placeholder, so the chat overseer's merge path (task 279) can't work.

## Decisions (Paxton)

| # | Question | Answer | Status |
|---|---|---|---|
| D1 | Where a submitted secret goes | **Sealed store + direct apply.** The LLM overseer only ever sees a handle. A deterministic tool writes the value to an allow-listed target. | Confirmed 2026-10-03 |
| D2 | Who defines the fields for each ask | **The overseer declares them; defaults come from the kind.** No declaration means one body field, plus one secret field when `kind == CREDENTIAL`. | Confirmed 2026-10-03 |
| D3 | What a submit does to the state | **CLOSED immediately**, with attribution. Editing a closed row (through "include closed") reopens it as OPEN. | Confirmed 2026-10-03, after a first answer asking for more context |
| D4 | Who may write | **An explicit email allow-list in fleet-admin**: the new env var `FLEET_OVERSEER_WRITERS` (comma-separated, case-insensitive). It is checked against the **proxy-verified** identity and **fails closed**: unset or empty means nobody can write. It starts with Paxton only. | Confirmed 2026-10-04 (Slack about 01:24Z "D3 close, D4 list", relayed by Overseer1, then confirmed in this session). Supersedes the 2026-10-03 "any Entra tenant user" |
| D5 | A check between submit and apply | **None.** A submission from an allow-listed writer is applied as it is. `apply_ask_secret` adds no approver check and no gate. | Confirmed 2026-10-04 in this session ("Allow-list submit only") |

Supersedes two peer readings:
- A relay read Paxton's first state answer ("provide more context") as "a submit adds context and does not close". He was given the context and then chose **CLOSED immediately** (D3).
- D4 went back and forth. First Paxton wrote "any users that's on the admin center user lists". On 2026-10-03 he picked "any Entra tenant user". On 2026-10-04 he settled on an **explicit allow-list**, after Overseer1 pointed out that `apply_ask_secret` can overwrite a prod token. **The allow-list is final.**
- Use a new variable, `FLEET_OVERSEER_WRITERS`, rather than `FLEET_CONTROL_USERS`. That one is the run-control allow-list, deliberately unset, and it covers a different power.

**Consequence of D3 plus D1 (the implementer must handle this):** a CREDENTIAL row closes as soon as you submit, before anything has applied the secret.
- If `apply_ask_secret` later fails, the failure has to be **visible**. The tool **reopens** the row as `OPEN` with the reason in `evidence` (for example `apply failed: <target> <error>`). It must not leave the row closed and silent.
- This is the only automatic reopen.

## What exists today

### Two repos, and neither local checkout is current
- **Page and proxy:** `cognizioware-mcp-tools/infrastructure/docker/fleet-admin` (Express + React), on `origin/main` (TASK-256 `b6aa115`, TASK-262 `579d069`).
- **Data:** LongHorizon-Harness `src/lh_harness/overseer_state.py` plus `mcp_tools.py`, on `origin/main`.
- Cut fresh worktrees from `origin/main` in both repos with `core.autocrlf=false`. Paxton's local LH `feat/spec-staging` is far behind, and its dirty `mcp_tools.py` and `server.py` are pre-TASK-235 versions.

### Where open asks come from now (A3a `108132b`, A3b `ac2cc49`)
- `list_open_asks` in `overseer_state.py:696` returns **live rows first, then archive-file rows**:
  - **Live rows** come from `live_open_ask_rows` (`overseer_state.py:645`). There are two kinds:
    - `GATE`, one per pending approval of a `waiting_approval` run, with id `gate-<run>-<approval>`;
    - `BLOCKED`, one per blocked queue entry, with id `blocked-<queue_id>`.
  - Live rows are always `state: "open"` and carry the same seven columns.
  - **File rows** come from `_list_open_asks_file` (`:529`), which parses `<overseer_root>/queue/OPEN-ASKS.md`. It uses the first table with header `| id | ask | kind | evidence | recommended | default if silent | state |`. A row is closed when its state starts with `closed`, `answered`, `done`, `resolved` or `superseded`.
- Dispatch: `mcp_tools.py:486-497` wires `registry`, `supervisor` and `queue_store` into `_overseer_list_open_asks` (`:586`).
- **The repo `queue/OPEN-ASKS.md` was retired to a stub** on 2026-10-01 (`6d958ae`): "open asks are derived live from CT110". The `C:\tmp\queue` copy on ptait09 is a **read-only archive**, and a PreToolUse hook blocks writes to it.
- **So nothing can write a new overseer-raised ask today.** Asks like `279-gateway-github-token`, which are neither a gate nor a blocked entry, survive only as rows in the archive file under `overseer_root` on CT110.
- **A3d needs a writable ask store on CT110** (next section). Hand-editing markdown is gone.

### fleet-admin (mcp-tools `origin/main`, `infrastructure/docker/fleet-admin/`)
- **UI:**
  - `web/src/App.tsx` routes `/overseer` to `pages/OverseerPage.tsx`, which has the tabs and `AsksPanel` (badges, markdown ask, Recommended / Default-if-silent / Evidence, include-closed checkbox).
  - `web/src/lib/overseerApi.ts`: `fetchOverseerAsks` sends `GET /api/overseer/asks`. Its header comment says it deliberately has no mutating functions.
  - Row type: `OverseerAskRow` in `web/src/types/index.ts`.
- **Proxy:** `server.js` has `app.use('/api/overseer', requireReadKey, buildOverseerRouter())`. In `src/overseer.js`, `callLh` sends `POST {LH_RUNS_BASE_URL}/api/mcp/fleet/<tool>` with `{arguments}` and `Bearer LH_RUNS_TOKEN`. Only GET routes are registered.
- **Identity pattern to reuse:** `src/runControl.js` (fleet run control):
  - It reads identity from `x-forwarded-email`, falling back to `x-auth-request-email`, `x-forwarded-user` and `x-auth-request-user` (`:32`).
  - `GET /api/fleet/auth/me` reports what the caller can do.
  - It records every action, with its actor, in `fleet.harness_actions`.
  - Copy its shape, including the fail-closed allow-list (`:37`, `:52`). Point it at `FLEET_OVERSEER_WRITERS` instead of `FLEET_CONTROL_USERS` (D4).
- **Audit pattern:** `logEvent(host, kind, detail)` writes into `fleet.events`, as in the `/admin/*` POSTs in `server.js`.

### Edge, SSO and the identity-trust gap
- In `caddy/Caddyfile`, the machine paths `/health /healthz /harness/* /enroll /checkin /hosts /admin/* /export/*` go straight to `fleet-admin:8787`. Everything else, including `/api/overseer/*`, goes through `fleet-oauth2-proxy:4180`.
- `fleet-oauth2-proxy` (`infrastructure/docker-compose.yml` around line 2530) uses Entra ID with `EMAIL_DOMAINS "*"` and **no group gate**: any tenant user can **read**. Writes are narrowed inside fleet-admin by D4. It also sets `PASS_USER_HEADERS`, `SET_XAUTHREQUEST` and `PREFER_EMAIL_TO_USER`. `PASS_AUTHORIZATION_HEADER` is **false on purpose**, because turning it on stripped CI ingest bearer keys (the ops lesson from 2026-09-05).
- **Gap (raised by Overseer1):** `fleet-admin:8787` can be reached from any container on the `cognizioware-mcp-tools` network. Such a caller can send its own `X-Forwarded-Email`. For reads that's acceptable. For writes it is **not**, because a forged header can close asks and plant secrets.
- **Fix (recommended):** a shared proxy secret.
  - Caddy sets `header_up X-Fleet-Proxy-Auth {env.FLEET_PROXY_AUTH}` on the oauth2-proxy-routed site block. This replaces any copy the client sent.
  - oauth2-proxy passes that header upstream, while overwriting the identity headers itself.
  - fleet-admin's write middleware requires `X-Fleet-Proxy-Auth` to match in constant time **and** a non-empty forwarded email. Otherwise it returns 401.
  - **Verify on deploy:** a curl from another container to `fleet-admin:8787` carrying a forged email gets 401, and the browser path gets 200.
  - A stronger alternative is verifying an Entra ID token in fleet-admin. That needs the Authorization header, which conflicts with the `PASS_AUTHORIZATION_HEADER=false` lesson. Don't do it in A3d.

### LongHorizon-Harness web API and auth
- `webapi/server.py`: `GET /api/mcp/fleet/tools` and `POST /api/mcp/fleet/{tool_name}`, both through `_invoke_fleet_tool`, which `/mcp` also uses.
- Auth: the bearer `LH_HARNESS_WEB_TOKEN`, plus per-caller scopes from `caller_auth.py` (task 174).
- Tool names must be registered in `config.py` (`_KNOWN_TOOLS` and the overseer set, validated together; see `config.py:90`).
- Write-tool examples: `harness_resolve_gate`, `harness_enqueue_task`.
- Redaction: `src/lh_harness/experience/redact.py` (`redact_text`, `redact_value`).
- Prior UX for "set, never shown": `tasks/env-keys-webux-2026-09-07.md`.

## Design for A3d

### 1. Ask store on CT110 (LongHorizon-Harness)
New module `src/lh_harness/ask_store.py`. It uses JSON files under the harness state dir, e.g. `<state>/asks/`, outside the repo and on the same volume as the queue store.
- `asks/<id>.json` holds:
  - the seven columns `{id, ask, kind, evidence, recommended, default_if_silent, state}`;
  - `fields`: a list of `{name, type: "body"|"secret", label, required}`;
  - `response`: `{body fields: text, secret fields: {set_at, set_by, length}}`;
  - `history`: an append-only list of `{at, actor, action, field_names}`.
- Secrets go to `asks-secrets/<id>/<field>`, a directory owned by the service user with mode 0700 and files at 0600. **A secret value never appears in `asks/*.json`, a tool result, a log or `history`.**
- Writes are atomic (temp file then rename) and hold a per-ask file lock.
- Rows from this store join `list_open_asks` **between the live rows and the archive-file rows**. Each row gains `fields` and `response`, with secrets reported as metadata only.
- **Defaults (D2):** no declared `fields` means one body field `response`, plus a secret field `secret` when `kind == CREDENTIAL`.
- **Live rows (`GATE` and `BLOCKED`) also accept responses**, keyed by their row id:
  - The response is stored as context, and `state` is set to closed in the store.
  - A store entry **does not resolve the gate or unblock the entry**. That stays `harness_resolve_gate` and the queue's own path.
  - While the underlying gate or blocked entry still exists, show the row with its closed response. Once it is gone, drop it.
  - **Flag for Paxton:** a later A3 item could make a GATE response actually resolve the gate.
- **Archive-file rows** such as 279 are read-only historical rows. The first response to one **copies it into the store**, and from then on the store wins for that id. Nothing writes back to `OPEN-ASKS.md`.

### 2. Tools (`mcp_tools.py`, registered in `config.py`)

| tool | caller scope | does |
|---|---|---|
| `raise_open_ask` | overseer | Creates a store ask with the seven columns and optional `fields`. This is how new CREDENTIAL and other asks are raised after the markdown retirement. |
| `declare_ask_fields` | overseer | Sets or replaces `fields` for an ask id (D2). |
| `respond_open_ask` | `overseer:write` (fleet-admin's token) | Arguments are `{id, actor, fields:{name:value}}`. Bodies are stored and secrets sealed. `state` becomes `CLOSED <iso-ts> by <actor> (web)` (D3). If the row was already closed, an edit **reopens** it to `OPEN` first and then applies the submit, which closes it again with the new attribution. So each save is "closed with this answer", and history records every edit. |
| `clear_ask_secret` | `overseer:write` | Deletes one sealed secret and records it in history. |
| `apply_ask_secret` | `overseer:apply`, a separate scope | `{id, field, target}`. `target` must be a key in a **static allow-list map** in config, e.g. `ct202-mcp-tools-env:GITHUB_MCP_TOKEN` maps to host, file and variable. The tool writes the value deterministically and returns only `{applied: true, target, at}`. On failure it **reopens the ask** to OPEN, puts the reason in evidence, and returns the error with no value (D1 + D3). |

- `respond_open_ask` returns the updated row with secret metadata only.
- Add `responded`/`closed` handling, so a closed store row is hidden unless `include_closed` is set, just like file rows.

### 3. fleet-admin
- `src/overseer.js` gets:
  - `GET /api/overseer/me`, which returns `{email, can_write}`. `can_write` is true only when the proxy-auth header is valid, an email is present, **and** that email is in `FLEET_OVERSEER_WRITERS` (D4).
  - `PUT /api/overseer/asks/:id/response`, body `{fields:{name:value}}`.
  - `DELETE /api/overseer/asks/:id/response/:field`, which clears a secret.
- The write middleware requires:
  - `X-Fleet-Proxy-Auth` (see the identity-trust gap above);
  - a forwarded email that is in `FLEET_OVERSEER_WRITERS` (D4). With no email it returns 401. With an email that isn't listed, or with the list unset or empty, it returns 403 and says the list is unset or the identity isn't on it.
  - `X-Requested-With: fleet-admin` plus a same-origin `Origin`/`Referer`, to block CSRF since the SSO session is a cookie.
- Writes forward to `respond_open_ask` and `clear_ask_secret` with `actor = email`.
- Audit: `logEvent('overseer', 'ask_response', {id, actor, field_names})` with **no values**. fleet-admin must not log request bodies on these routes.
- Keep the existing test that writes are impossible (`DELETE /api/overseer/asks` returns 404) for every path except the new ones.
- `web/src/lib/overseerApi.ts`: add `fetchOverseerMe`, `saveAskResponse` and `clearAskSecret`, and update the header comment.
- `AsksPanel`: each row gets an expandable "Respond" editor, shown when `can_write` is true.
  - Body fields are textareas prefilled with the saved text.
  - Secret fields are `type=password` with `autocomplete=new-password`. They are never prefilled. They show "set by X at T (N chars)" or "not set", with Replace and Clear actions.
  - Add a dirty-state indicator and a Save button. After saving, the row closes and leaves the default view. With "include closed" on, the row is visible and editable, and saving again updates it as described for `respond_open_ask` above.
  - Rows the overseer reopened after a failed apply come back to the default view with the failure in Evidence.

### Order
1. LH: `ask_store.py`, the tools, the scopes in `caller_auth`, and tests.
2. LH: PR, merge, then the CT110 deploy through the lane (the CT110 deploy job runs as root; see the editable-install note in the deploy docs).
3. mcp-tools: Caddy `header_up`, the `FLEET_PROXY_AUTH` and `FLEET_OVERSEER_WRITERS` envs (the latter set to Paxton's SSO email only), the fleet-admin routes and UI, and tests.
4. mcp-tools: PR, merge, then deploy fleet-admin and Caddy on CT202.
5. Configure the `apply_ask_secret` target map with the single entry `ct202-mcp-tools-env:GITHUB_MCP_TOKEN`, and give the `overseer:apply` scope to the overseer's caller only.
6. End-to-end check on ask 279.

## Verification
- **LH:** `pytest tests/webapi/test_overseer_state.py tests/webapi/test_mcp_fleet.py tests/test_validate_queue.py` plus new `tests/test_ask_store.py` and `tests/webapi/test_ask_tools.py`. They cover:
  - a write then read round-trip where no secret value appears in any tool result or in `asks/*.json`;
  - every new tool refused without its scope;
  - a submit closes the row and an edit reopens and then closes it;
  - an `apply_ask_secret` failure reopens the row with the reason;
  - a target outside the allow-list is refused;
  - CREDENTIAL defaults;
  - an archive row is copied into the store on first response;
  - a response to a GATE row does not resolve the gate.
- **fleet-admin:** `npm test` in `src` and `web`. Cover:
  - a missing or wrong `X-Fleet-Proxy-Auth` returns 401;
  - a missing email returns 401;
  - an email not on `FLEET_OVERSEER_WRITERS` returns 403, and an unset or empty list returns 403 for everyone (fails closed);
  - a missing `X-Requested-With` or a cross-origin request returns 403;
  - a secret never appears in response JSON, `fleet.events` or logs;
  - other `/api/overseer/*` write paths still return 404;
  - the UI never prefills a secret and hides the editor when `can_write` is false.
- **Live:**
  - A forged-header curl from another container returns 401.
  - A signed-in tenant user not on the list sees no editor, and a direct PUT from them returns 403.
  - From the browser, respond to `279-gateway-github-token` with a note and the PAT. The row closes, and with "include closed" it shows `CLOSED … by <your email> (web)`.
  - Grepping for the PAT finds nothing in the store JSON, LH logs, fleet-admin logs, `fleet.events` or Langfuse.
  - The overseer calls `apply_ask_secret`, and CT202 `mcp-tools.env` holds the new value.
  - After `github_mcp` is recreated, `tools/list` for github returns the three tool names.

## Cautions
- Windows line endings: clone with `core.autocrlf=false`, check `git diff --stat` before opening a PR, and never chain PR create and merge.
- No `@claude` PR-review invocations: since mcp-tools #131 they bill Paxton's personal subscription.
- Recreating `github_mcp` on CT202 can force-recreate the LiteLLM router (a change to the yaml hash does that, even for a comment-only edit). Pick a quiet window.
- Write nothing under `C:\tmp` on ptait09; it is a read-only archive.
