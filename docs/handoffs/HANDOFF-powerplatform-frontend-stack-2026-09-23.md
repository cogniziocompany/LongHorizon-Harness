# cognizioware-powerplatform — frontend delivery stack, for expert review
Read from the `develop` branch of `cogniziocompany/cognizioware-powerplatform` (PRIVATE) on 2026-09-23 by the
interactive session [61855af8]. Everything below comes from the repo's own files, not from docs or memory.

## 1. Stack, as declared
| Layer | Choice | Version (package.json range) |
|---|---|---|
| UI library | React + react-dom | ^19.2.1 |
| Routing | react-router-dom | ^7.1.0 |
| Language | TypeScript | ^5.7.0 (`tsc -b` runs as part of the build) |
| Build / dev server | Vite + `@vitejs/plugin-react` | ^6.0.0 / ^4.3.0 |
| Unit tests | Vitest + happy-dom | ^5.0.0 / ^20.14.0 |
| Auth | `@azure/msal-browser` (Entra ID, SPA flow) | ^5.4.0 |
| Spreadsheet I/O | `xlsx` (SheetJS) | ^0.18.5 |
| Styling | plain CSS — 2 `.css` files, no CSS framework, no component library | — |
| State / data fetching | none declared — React state and hooks only, no Redux/Zustand/React Query | — |

The entire runtime dependency list is **five packages**, which makes this a deliberately small, framework-light SPA.

## 2. Source layout
`packages/frontend/src`: 136 files (86 `.tsx`, 19 `.ts`, 2 `.css`). Feature folders: `admin`, `auth`, `billing`,
`checkout`, `environments`, `features`, `flow`, `hooks`, `leads`, `sessions`, `ui`, `workspace`.
Monorepo using npm workspaces: `packages/frontend` + `packages/backend`.

## 3. How it is built and delivered
- `npm run build` = `tsc -b && vite build` → `packages/frontend/dist`, with **`sourcemap: true`**.
- Dev: Vite on :5173, proxying `/api` → `http://localhost:3000` and `/ws` → `ws://localhost:3000` (WebSocket in use).
- **Production: no separate web server or CDN.** The root `Dockerfile` (`node:22-slim`) copies the pre-built
  `packages/frontend/dist/` into `./public/`, and the **Express** backend (`express`, `pg`, `ws`, `zod`) serves it on
  port 3000 — one container for API, WebSocket and static SPA.
- The frontend is built **outside** the image (in CI) and copied in, so the image is only correct if CI built the
  frontend first — the "Build frontend (tsc -b + vite)" check added in PR #111.
- Promotion: `develop` → dev, `release` → uat, `main` → prod via `promote.yml` (per the repo's own correction notes,
  landing on `develop` deploys dev only).

## 4. Findings for the reviewer — in priority order
1. *(Removed 2026-09-23 by Paxton: the committed env files are known; the stack is internal-only and not publicly reachable, so it is out of scope for this review.)*
2. **`xlsx` 0.18.5 is the last version SheetJS published to npm**, and — from my knowledge, please verify — it carries
   known advisories: prototype pollution (CVE-2023-30533, fixed in 0.19.3) and ReDoS (CVE-2024-22363, fixed in
   0.20.2). Fixed versions are distributed from the SheetJS CDN, not npm, so `npm audit fix` will not resolve it.
   This matters more if the app parses user-uploaded spreadsheets (the `sessions` requirement-doc upload is a
   candidate — check).
3. **Production sourcemaps are shipped publicly.** `sourcemap: true` plus copying all of `dist/` into `public/` means
   the `.map` files, and so the original TypeScript, are downloadable from prod. Use `sourcemap: 'hidden'` or strip
   the maps from the image if that isn't intended.
4. **Build artifacts committed.** `packages/frontend/dist/` (2 files) and `packages/frontend/build-test-output.txt` are
   tracked on `develop`, so the committed `dist` can drift from what CI builds. Since the Dockerfile copies `dist/`,
   confirm the image never picks up a stale committed build.
5. **Single-container coupling.** Static assets, API and WebSocket share one Express process: no CDN caching, and a
   frontend-only fix requires a full backend redeploy. Fine at current scale; worth a deliberate decision.
6. **No design system and no data-fetching layer.** Consistent with a small SPA, but 86 components on hand-rolled CSS
   and ad-hoc fetch is where inconsistency accumulates. A reviewer judgment call, not a defect.

## 5. What I did not check
Accessibility, bundle size, React 19 compiler use, the e2e suite's framework (`e2e/golden-path/run.mjs` exists; the
`.playwright-mcp` directory suggests Playwright-driven QA), CSP headers, or MSAL cache/redirect configuration. The
CVE details in finding 2 come from my knowledge, not from a live advisory lookup — confirm them before acting.
