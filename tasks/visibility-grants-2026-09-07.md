# Visibility grants request package (2026-09-07)

**Ask (Paxton 21:05 PT):** run a harness task to produce a better, more thorough request for the access grants only he can click (Cloudflare tunnel scope + account id, GitHub auto-merge and production reviewers, Entra Application Administrator, plus Power Platform, Ollama, Stripe, Proxmox).

**Found today:** Cloudflare access already exists as the cloudflare-mcp server on CT202 (DNS write, tunnel read; account id unset; no tunnel-route write tool). Stripe test key already in the billing lane env. Both were missed by the overseer, which is the point of the package: one document with why / exact least-privilege grant / click path / where the secret goes / verify command / rollback, a verify script, and a patch adding a tunnel-route tool to cloudflare-mcp.

**Queue:** `18-visibility-grants-request` (LongHorizon-Harness, kimi, 4 rounds, text `C:\tmp\visibility-grants-task.txt`). Output: docs/handoffs/visibility-grants-request-2026-09-07.md, scripts/access/verify-grants.sh, docs/patches for mcp-tools.

## Progress
- 21:10 PT: queued.
