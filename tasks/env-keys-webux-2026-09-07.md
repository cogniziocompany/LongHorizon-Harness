# Env & keys from the fleet web UX (2026-09-07)

**Ask (Paxton, 20:40 PT):** "create a lh-harness task for adding the env variables from the webux for each related repo, if it doesn't already have a way to set our keys. manage it and add it to the tasks you manage for your goal."

**Reading:** a page in the fleet web UX (Hydra fleet admin, the target of fleet.easybutt0n.ai) that lists, per related repo and environment, the env var NAMES we need, whether each is set (never values), and a Set action that writes to the right target (GitHub environment secret/variable and/or the box env file) with an optional server-side verify. Repos that already have a way (GitHub environment secrets UI) get a deep link and no new write path.

**Why:** today provider keys (Cloudflare, Stripe test, Ollama Cloud, Redis, MCP tokens) and per-repo variables move over chat or by hand on boxes, and the overseer has no status view; twice today access already existed (Cloudflare MCP on CT202, Stripe test key in the billing env) and was not visible.

**Queue:** `17-env-keys-webux` (cognizioware-hydra-rsi, kimi, 8 rounds, text `C:\tmp\env-keys-webux-task.txt`). Slices: catalog per repo x env → API status/set/verify with audit rows (no values) → UI page → runbook. Deploy through the Hydra lane on corsairai300 after 14c (control plane apply).

## Progress
- 2026-09-07 20:45 PT: task created and queued behind 16a/16b.
