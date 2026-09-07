# TASK — cognizioware-powerplatform web UX: navigation shell + session archive (Claude Design handoff → build)

**Asked by Paxton 2026-09-05 22:40 PT:** "add this as another lh-harness task to manage and then have this claude agent's repo deploy on your planning"
(design session handoff header: originating session f2b53435-8bb9-4c64-b397-0213e1e275b9, plan `guiv-eme-an-handfoff-peppy-fog.md`).
**Authoritative spec:** `design/handoffs/web-ux-nav-archive-handoff.md` (committed on develop, 7011d0d). No canvas package (`design/ux/nav-archive/`)
exists — the run builds W1–W6 + G1 straight from the handoff; the handback marks canvas/screenshots as not produced.
**Overseer role:** manager — worktree, run, review, PR → develop, then the deploy path below.
**Repo / branch:** cogniziocompany/cognizioware-powerplatform, `feat/web-ux-nav-archive` from origin/develop @ edd45a1 (the handoff said "cut from
main"; superseded — develop is the integration branch the lanes deploy, and main/develop workflow files are now identical).
**Harness workspace (WSL-created, LOCKED):** `/mnt/c/Users/PaxtonTait/source/cognizioware-powerplatform-navarchive`.
**Harness run:** `20260906T053817Z_d96ca9e6` (WSL, `:pool` trio, 14 rounds). **Task text:** `C:\tmp\navarchive-task.txt` (verbatim).

## Slices
1. W6 tokens + shared components (Shell, Breadcrumb, SubHeader, StatusChip, Tabs, OverflowMenu, ConfirmDialog, EmptyState)
2. W1 app shell + breadcrumb (48px bar; Workspace/Flow `calc(100vh - 48px)`, three panes + sockets untouched)
3. W2 environment-scoped sessions + W5 session subheader
4. W3 archive-not-delete (DELETE = soft archive; Restore = PATCH status active; "Delete" never in copy)
5. W4 + G1 (migration 028_environment_status.sql, PATCH /environments/:id status, GET ?status=)
6. Playwright `e2e/playwright/nav-archive.spec.js` + `design/ux/nav-archive/HANDBACK.md`

## Deploy path (per the ship plan, not the handoff's manual CT105 recipe)
PR → develop → dev lane auto-rolls to CT100 (.163:3000) → promotion pipeline promote.yml (dev → uat → prod, gates decide; prod auto-approved per
Paxton 2026-09-05). G1's migration runs with the lane's normal migrate step; verify on the dev lane before promoting. If the dev lane does not
run migrations automatically, run 028 on the box before switching the app container (handoff §7 caveat).

## Model trio
manager `kimi-k2.7-code:pool` · executor `kimi-k2.7-code:pool` · auditor `kimi-k3:pool` (API keys `roles` + `max_rounds`).

## After completion (overseer)
1. Review: tsc/vite/backend tsc clean, no "Delete" copy, WorkspaceLayout socket untouched, no .github/package.json dependency changes, migration 028 shape.
2. Push branch, PR → develop, merge; watch the dev lane; smoke the shell on .163:3000 (crumb on /sessions/:id/workspace, archive/restore round-trip).
3. Promotion run with the approval watcher; then Paxton checks powerplatform.easybutt0n.ai.

## Completed (2026-09-06 03:05 PT)
Run d96ca9e6 needed the 14-round budget plus an overseer extension (the spec file was missing at round 0 — it only existed on ci/env-promotion —
and my first answer used the wrong API field). Delivered: 2a5dba1 tokens + shared components · 92011ca app shell + breadcrumb · 2dfd7e5 env-scoped
sessions + subheader **including W3 archive/restore** (single slice; "Delete" appears only as the HTTP method) · ae860ae G1 migration 028 + W4 ·
2457c8f handoff landed · 6f906ab/c29decb HANDBACK.md + Playwright spec (NOT RUN: needs MSAL creds; suites 11/12 NOT RUN). Two run commits that
committed 36 untracked backend dist artifacts were dropped by the overseer (reset to c29decb) — `packages/backend/dist` is not tracked on develop.
PR → develop: pp #67, held unmerged until promotion run 16 clears its dev stage (a dev-lane redeploy mid-eval would disturb the gate). G2 deferred.
- 07:15 PT: #67 merged; dev lane CI build/image/deploy green; pp-dev-app-1 on sha-286fecb healthy; log shows `028_environment_status.sql Migration applied`; root 200 (title "Cognizioware - Power Platform Builder"); API 401 unauthenticated as expected. Browser check of the shell/crumb/archive round-trip is Paxton's (needs MSAL sign-in) at http://192.168.21.163:3000.

## Follow-up (2026-09-07 01:35 PT): Env → Session flow redesign review (Claude Design, Modernist)
Paxton's design expert delivered `design/ux/modernist/` in cognizioware-powerplatform (Env-to-Session-Spec.md, "Env to Session Flow.dc.html",
five screenshots): 6-step process strip, 3-step wizard (Solution → Specs → Review & start), processing lockout, CGZ-Exxx coded failures,
approval-gate-only doc editing, auto-titled workspace. Overseer copied the spec to docs/handoff/ and is running the powerplatform Claude Code
session (f2b53435) headlessly to produce docs/handoff/Env-to-Session-Spec-REVIEW.md (gaps, conflicts, ordered slices, answers to the designer's
four open questions). Paxton relays the review to the designer; the build itself becomes a harness task after the designer's revision.
