# TASK — Ops Control Center v1 follow-ups from the first live doctor run (2026-09-05 23:43 PT)

**Owner session:** Claude Code `6803faad-e27d-43c0-8150-328e549e1fb1` (cwd `c:\Users\PaxtonTait\source\cognizioware-mcp-tools`).
**Overseer role:** filed after Paxton's first sign-in; the overseer reviews and merges (lane auto-deploys to CT202).
**Repo / paths:** `cogniziocompany/cognizioware-mcp-tools` main → `infrastructure/docker/ops-control-center/` (`doctor/ops_doctor.py`,
`doctor/targets.json`, `src/OpsControlCenter/Services/CatalogStore.cs`, `Components/Pages/ReadMeFirst.razor`).
Branch from `origin/main` (worktree from WSL, locked); put this file's absolute path in the task text; standard `:pool` trio.

## Live facts (verified by the overseer)
- LiteLLM answers `POST /mcp` with **307 → /mcp/**; `POST /mcp/` returns 200 with 935 tools. The app must call `/mcp/` (trailing slash).
- The container IS on the compose network; `OPS_GITHUB_TOKEN` IS set in its env (printenv) — the doctor reports "not set".
- ops.easybutt0n.ai and litellm.easybutt0n.ai certs are valid Let's Encrypt (notAfter 2026-12-04); PVE `:8006` certs are ACME too.
- MCP servers `azure-inference-adapter:3006`, `cognizioware-xrm-mcp:3102`, `kb-mcp:3103`, `kb-mcp-uat:3104`, `n8n-cloud-mcp:8000`,
  `ssh-mcp:8000`, `telnyx-mcp:8000` answer **404** on `/` and `/health`: they only serve `/mcp`. 404 (or 405/406 on `/mcp`) means alive.
- `https://mcp-cognizioware.easybutt0n.ai/` returns 403 by design (Swagger gated); `/admin/` is the public probe that should be 200.
- `tools-gateway-v3` root returning **403** is the expected auth-gated answer (spec §7 says 403/302 is a pass).
- Identity gate is the DEDICATED Entra app `ops.easybutt0n.ai` client `2562701c-211c-44cf-bb2c-25c605ad875e`, not the mcp-cognizioware admin app.

## Fix list (all in the ops-control-center context; one PR)
1. **Cert probes** (`ops_doctor.py`): `ssl.getpeercert()` returns `{}` when verification is off → every cert shows `CN=, -1d left`.
   Use `getpeercert(binary_form=True)` and parse notAfter/issuer/CN (stdlib only: `ssl.DER_cert_to_PEM_cert` + a minimal ASN.1/regex or
   a second verifying context to fetch the dict). Keep the 21-day floor and LE-issuer check.
2. **Aggregate tools/list** (doctor) and **CatalogStore.cs**: POST to `${OPS_LITELLM_URL}/mcp/` (trailing slash); follow 307 defensively; parse
   both plain JSON and SSE frames; on any missing key report `refresh failed: <reason>` without throwing (`The given key was not present in
   the dictionary` must not surface). Per-server counts come from the `name` prefix before the first `-`.
3. **tools-gateway-v3 root**: treat 302/401/403 as pass (auth-gated), only 200/5xx/timeout as fail.
4. **Service probes** (`targets.json` + doctor): give each MCP service its real path and accepted statuses — `/mcp` with `expect: [200,404,405,406]`
   or `/health` where it exists; document per row why.
5. **Off-box billing public**: probe `https://mcp-cognizioware.easybutt0n.ai/admin/` (expect 200) and `/api/v1/admin/auth-config` (expect 200);
   root 403 is fine.
6. **Secret presence**: `OPS_GITHUB_TOKEN` reported "not set" while set — check how the doctor reads env (subprocess env passed by DoctorRunner?
   `Process.Start` without inheriting?) and fix at the right layer; add a unit-style self-test line to the doctor output listing which env
   names it saw as set (names only).
7. **Read me first**: Identity paragraph → dedicated app 2562701c (roles Global.Admin / Billing.Admin defined on it), keep the "same role names
   as /admin/" wording. Footer must show the operator email (Paxton could not see it on first load — verify the header forwarding).
8. **E2E ingest**: `/data/e2e` is still empty after three lanes — the workflow's ingest step (curl to `/api/ingest/e2e`) is not landing.
   Coordinate with the overseer's finding on the lane log; if the endpoint rejects the payload, fix the contract on the app side and say why.

## Acceptance
- `python3 doctor/ops_doctor.py --json` from CT202 (via `docker exec`) shows: certs with real CN/days, aggregate tools ≥ 45, tools-gateway-v3 pass,
  all seven service rows pass, billing public pass, OPS_GITHUB_TOKEN set. Only tailnet certs remain "not probed".
- Catalog page shows the aggregate count and per-server table; E2E page shows the latest ingest after the next main push.
- Commit by explicit path; no secrets; no workflow edits unless item 8 needs one (then the minimal line, explained).

## Overseer findings on item 8 (E2E ingest), 2026-09-05 23:50 PT
- Same dry-run payload: **202 Accepted** when POSTed inside the app container (`wget --post-file` to 127.0.0.1:8080); **400, empty body,
  text/html, ~1 ms** through https://ops.easybutt0n.ai (Caddy → ops-oauth2-proxy → app). Even the no-auth variant returns 400 instead of
  401, so the request is rejected before the handler runs. Suspects: Host header rewritten to the public name (HostFiltering/AllowedHosts),
  or a forwarded header Kestrel refuses. Probe matrix (direct+Host, direct+X-Forwarded-*, via oauth2-proxy) results are in the overseer log.
- The lane's ingest step masks this with `curl -sf` → only `::warning::Ops control center E2E ingest failed`. Add `-w '%{http_code}'`
  and print the body on failure in the workflow step (one line, allowed).
- Probe matrix result: direct 202; direct + `Host: ops.easybutt0n.ai` 202; direct + X-Forwarded-Proto/For 202; direct + X-Forwarded-Host 202;
  **via ops-oauth2-proxy:4180 → 400** (oauth2-proxy access log: `POST "/api/ingest/e2e" 400 0 0.001`). So the proxy alters/drops the POST body
  on its skip-auth route. Overseer fix shipped: mcp-tools **#71** — Caddy routes `/api/ingest/*`, `/api/doctor`, `/healthz` straight to the app
  (bearer-key gated, no forwarded identity). Item 8 for the owner is now: confirm ingest lands after the next main push, and optionally find
  the oauth2-proxy cause (version/flag) for the record.

## Resolved by owner (2026-09-06 00:30 PT) — mcp-tools **#72** merged (24a4c8b)
Owner's root cause for item 8 (three stacked): ops-oauth2-proxy strips `Authorization` on skip-auth routes (PASS_AUTHORIZATION_HEADER) →
body-less 401 → status-code-pages re-executed it into the Blazor not-found page → antiforgery turned it into an empty 400 → `curl -sf` hid it.
Fixes: key also in `X-Ops-Ingest-Key`; JSON bodies on all API errors; status-code pages no longer wrap `/api`; PASS_AUTHORIZATION_HEADER off;
lane prints `ops ingest -> HTTP <code>`; `skips: []` fallback. Items 1-7 fixed in the same PR (real cert CN/days on all nine rows, /mcp/ path,
probe paths, 403 pass, billing /admin/, token presence, Read-me identity). Overseer's #71 (Caddy bypass for the key-gated paths) is also in.
