# SECRETS.md - placeholder registry for the archived overseer apparatus (task 104)

Hard rule (from the task 104 brief): commit env var NAMES only; never a value.
This file lists every placeholder installed during the 2026-09-23 sanitization pass,
what it stands for, and where the real value lives. Rotation of any value named here
is out of scope by standing decision.

## Placeholders installed

| Placeholder | File(s) | Stands for | Where the real value lives |
|---|---|---|---|
| `<CT110_BEARER_TOKEN_REDACTED_SEE_docs/SECRETS.md>` | `tools/launch_queue.py` (was line 6), `tools/optimistic_deploy_harness.py` (line 33), `tools/spin_watchdog.py` (line 46) | The CT110 harness API bearer credential (a 48-char hex string, identical in all three scripts) used in `Authorization: Bearer <token>` against the launcher/harness API | The live copies under `C:\tmp\` on the overseer workstation, and CT110's service environment (queried at runtime via `pct exec 110` / systemd `Environment`; the scripts themselves also grep `^ANTHROPIC_AUTH_TOKEN=` from CT110 env files at runtime). Not in any repo. |

Each edit replaced exactly one line (the `TOKEN = "..."` assignment) and nothing else;
the scripts are otherwise byte-identical to the 2026-09-23 export snapshot. They are
archived AS-IS per the brief (no refactor: the env-var name above is documentation,
not wiring - the scripts do not read it).

## Triaged as NOT secrets - left unchanged, on purpose

- `docs/LEDGER.md` (LEDGER line ~7098): `` `sk-ant-api0...` `` is the standard *public*
  Anthropic key prefix, already author-truncated with `...`. Not a usable value.
- `feat/task-sk-...` branch label referenced in `docs/LEDGER.md`, `tasks/hydra-fleet-plane-deploy-task.txt`,
  `queue/blocked/9999f-166-*.json`, `queue/done/995-133-*.json`, `ship-plane.html`: a git
  branch NAME suffix (hydra PR #25), not a credential. Matched scanners only because it
  starts with `sk-`.
- 40-hex and 64-hex strings throughout (git SHAs, docker `sha256:` manifest digests,
  `SHA256:` SSH host-key **fingerprints** in the LEDGER): public identifiers, not keys.
- `Bearer $HYDRA_API_KEY`, `$LH_HARNESS_API_TOKEN`, `secret_env`-style mentions in task
  briefs and handoffs: env var NAMES, which the brief explicitly allows.
- `tools/deliver-secret.sh`: carries no literal secret; it passes secret NAMES and reads
  values at an operator prompt on your terminal.
- Internal endpoints kept for the behavior contract of the archived scripts
  (e.g. `http://192.168.21.168:8799`, the router URL, `root@<pve-host>` ssh targets):
  internal topology with no attached credentials. The brief's scan targets are tokens,
  bearer values, keys and passwords - these scripts are archived AS-IS.
- Stripe-shaped ids (`price_...`, meter ids) and HF model slugs in task briefs: product
  config identifiers, publishable by design.

## Verification (2026-09-23)

- Fixed-string search for the live value across all 593 copied files: **0 occurrences**.
- `git grep -lF <value>` over the tracked tree: **no match**.
- Masked pattern rescan (bearer/token-assign/ghp/sk/AKIA/JWT/PEM/hex-40..64/userinfo-url):
  the only removed hits vs the pre-edit baseline are the 3 embedded tokens; the 212
  remaining candidates are the benign classes listed above.
- Exported snapshot `/home/harness/ptait09-export-20260923/` unmodified (sha256 check of
  the 4 counterpart files; copies differ by exactly the 3 TOKEN lines - the LEDGER copy
  is byte-identical to the snapshot).
