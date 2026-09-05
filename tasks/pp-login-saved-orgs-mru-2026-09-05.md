# TASK — Saved orgs on the login page (browser-side MRU list) → dev pipeline

**Original session (steers/deploys):** Claude Code `2e684a91-d0c1-47c2-8be4-a1837658ab5b`
(repo `c:\Users\PaxtonTait\source\cognizioware-powerplatform`; `claude --resume 2e684a91-d0c1-47c2-8be4-a1837658ab5b`).
**Orchestrator:** LongHorizon-Harness overseer — runs the build, then pings the original session to deploy
through the env pipeline (dev first) with the cognizioware-qa gate on every promotion.
**Harness workspace (worktree — main checkout busy):** `/mnt/c/Users/PaxtonTait/source/cognizioware-powerplatform-mru`
(branch `feat/login-saved-orgs-mru` from `origin/develop` @ 52ae6d9).

## Model trio (cloud-ollama standard)
manager `glm-5.3:cloud` · executor `kimi-k2.7-code:cloud` · auditor `kimi-k3:cloud`.

## Context (from the original session, decisions dated 2026-09-04)
The first thing a user does in the web UX is optionally type a Dataverse org URL on the login page
(`packages/frontend/src/auth/LoginPage.tsx`, input `data-testid="login-env-url"`). The same entry exists on the
verify-recovery banner in `packages/frontend/src/sessions/SessionList.tsx` (~lines 109-124, `data-testid="verify-fix-env-url"`).
Both call `setUserEnvUrl()` in `packages/frontend/src/auth/MsalProvider.tsx`, which keeps exactly one value per user in
localStorage (`cw.envUrl:<upn>`, pre-auth pending key `cw.envUrl:__pending__`) and uses it as the MSAL token scope.
Goal: every org a user enters is remembered for that user and offered back as a pick-list on both entry points, MRU first.
Decisions: browser-only storage (localStorage) — no backend route, no migration; save ONLY after sign-in verifies against
that org (token minted for `<url>/.default` and `POST /api/auth/verify` returns OK) — typos/inaccessible orgs never stored;
branch off `origin/develop` → PR into `develop` → merge auto-deploys dev first.

## Task text (paste verbatim)
BUILD-ONLY frontend feature in this worktree. Read LoginPage.tsx, SessionList.tsx (verify banner), MsalProvider.tsx FIRST and
map the current env-url flow (pending key -> verify -> per-user key). Implement: (1) a per-user MRU store in MsalProvider (or a
small sibling module) keyed `cw.envUrls:<upn>` (JSON array, newest first, de-duplicated by normalized URL, capped at 8) that is
appended ONLY on the verified path (after /api/auth/verify OK for that org); keep the existing single-value keys working for
compatibility. (2) A pick-list on both entry points: datalist/dropdown of saved orgs MRU-first, free text still allowed, remove-
entry affordance, keyboard accessible, test ids `login-env-url-list` and `verify-fix-env-url-list`. (3) Unit tests for the store
(add/dedupe/cap/order/remove, verified-only save) with the repo's frontend test runner; component tests if the repo has them.
(4) tsc clean for packages/frontend; run the frontend test suite and quote output. No backend changes, no migrations, no
packages/backend edits. Commit by explicit path (never git add -A — CRLF phantoms), small commits, no push (the orchestrator
pushes and the original session opens the PR into develop). Completion = commits + hashes + test output + a 6-line PR description.
Auditor: no git fetch.

## After completion (orchestrator-owned)
1. Push `feat/login-saved-orgs-mru`; ping session 2e684a91 via `claude --resume` with the branch + PR text: it opens the PR
   into `develop`, merges when green → dev auto-deploy.
2. cognizioware-qa gate (target=ce, env=dev) before promoting; then uat → prod through `promote.yml` (being built in 7dc4b478)
   with the gate between each env. If QA has no compatible check for the login MRU, notify Paxton (an oidc/login smoke may be
   added to the CE suite).
