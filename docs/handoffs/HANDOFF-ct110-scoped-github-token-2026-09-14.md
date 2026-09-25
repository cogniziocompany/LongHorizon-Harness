# Handoff: give CT110 harness runs a scoped GitHub write path

**Decision (Paxton, 2026-09-14):** one credential for all repos, limited to push and PR creation.
No merge, no secrets, no workflows. Written by the overseer session `5321285c` for whoever picks
this up. Everything below that says "measured" was read from the live system today.

## STATUS 2026-09-14 16:43 PT - COMPLETE. Nothing remains.
- **Restart: done** at 16:21 PT (`ActiveEnterTimestamp` 23:21:22 UTC); the running service's env has
  `GH_TOKEN` (count 1, read from `/proc/<pid>/environ`).
- **Doctrine: done.** `C:/tmp/queue/LOOP-PROMPT.md` STEP 1 says runs push and open their own PRs and
  merging stays the overseer's. The stated restart time was corrected from 16:30 to 16:21 (backup
  `LOOP-PROMPT.md.bak-20260914-164345`).
- **Re-verified from CT110 at 16:43:** secrets list 403 on LongHorizon-Harness, cognizioware-mcp-tools,
  cognizioware-powerplatform; the PR-create permission check returns 422 (permitted, bad head only).
- Not changed: task notes in `blocked/` that still say "CT110 has no gh / no GitHub token" (e.g. task
  102, mcp-cogn-gh) are now stale. Whether to relaunch those as runs is the overseer's call.

## STATUS 2026-09-14 16:17 PT (overseer) - SECRETS NOW REFUSED; token limits complete
- Paxton edited the 2027-09-15 token. From CT110 as harness: secrets list HTTP 403 on
  LongHorizon-Harness, cognizioware-mcp-tools, cognizioware-powerplatform. Push and PR create already
  proven in real use (LongHorizon-Harness #18, mcp-tools #148). Merge remains allowed; doctrine-enforced.
- Remaining: restart lh-harness in a zero-active window so runs load GH_TOKEN; then update the
  doctrine so runs push and open PRs.

## STATUS NOTE 2026-09-14 12:15 PT (overseer session 5321285c) - read before the 17:40 UTC section below
- **Secrets read is STILL ALLOWED**, re-checked from CT110 as harness: HTTP 200 with
  `secrets=read` on LongHorizon-Harness (1), cognizioware-mcp-tools (17), cognizioware-powerplatform (3).
- **The installed token expires 2027-09-15 17:35:32 UTC** (GitHub header), not 2027-09-13. The
  2027-09-13 token is the earlier prax211-owned one. The edit must be made on the **2027-09-15** token;
  the instruction to check "the token expiring 2027-09-13" points at the wrong one.
- **Push and PR create proven in real use:** the overseer pushed `feat/seq-logging` and opened
  LongHorizon-Harness #18 from CT110 with this token; remote head read back `edef634`.
- **lh-harness restart NOT done:** the running service has `GH_TOKEN` 0 times in its environment
  (it reads the secrets file only at start). Held until secrets reads as refused.

## STATUS UPDATE 2026-09-14 17:40 UTC: org token installed; 4 of 5 limits proven; ONE permission to remove
Paxton issued a new token owned by `cogniziocompany`; it is installed on CT110 and the file on ptait09
is deleted. Proof run, read line by line (exit 0 is not the verdict):

| item | result | evidence |
|---|---|---|
| push | ✅ OK | throwaway `probe/scoped-token-base-20260914-173758` + head branch pushed |
| PR create | ✅ OK | cogniziocompany/LongHorizon-Harness#17 (base and head both probe branches) |
| merge | ⚠️ ALLOWED, as the caveat predicts | #17 MERGED into the throwaway base only; `main` untouched (`c17c653`); no-merge stays doctrine |
| workflow push | ✅ REFUSED | "refusing to allow a Personal Access Token to create or update workflow ... without `workflow` scope" |
| secrets read | ❌ **ALLOWED** | `repos/.../actions/secrets` returned 200, `X-Accepted-Github-Permissions: secrets=read`, total_count=1 (names only; GitHub never returns values) |
| cleanup | ✅ | 0 `probe/scoped-token*` branches on the remote |

Other permissions probed: **Actions: read is also granted** (`actions/runs` and `environments` 200,
`actions=read`). Org secrets, Dependabot secrets, Actions variables and webhooks are refused (403).
Private org repos are visible with push (`cognizioware-mcp-tools`, `cognizioware-powerplatform`),
so "All repositories" is in effect.

**Remaining steps, in order:**
1. Paxton edits the token in GitHub: **Secrets → No access, Actions → No access**, then saves.
   Editing keeps the value, so nothing gets reinstalled.
2. Rerun only the proof: `PROOF_ONLY=1 bash C:/tmp/gh-scoped-token-install.sh`. It must show
   `secrets list -> REFUSED`, and all other lines as in the table.
3. Restart `lh-harness` on CT110 when no run is active (step 4 below).
4. Overseer updates the doctrine: runs push and open PRs; merging stays the overseer's.

## STATUS 2026-09-14, earlier (session `longhorizon-harness-db`, first two installer runs; superseded above)
- **Installed on CT110:** `GH_TOKEN` is in `/home/harness/.lh-harness-secrets.env` (1 line, mode 600,
  backups `*.bak-ghtoken-*`). The harness user's global git config has
  `credential.https://github.com.helper` (reads `$GH_TOKEN` at call time) and two `insteadOf`
  rewrites (`git@github.com:` and `ssh://git@github.com/`). The token file on ptait09 is deleted.
- **Token:** a one-year fine-grained token, `login=prax211`, expires **2027-09-13** (GitHub header).
  The first attempt, created with no expiry, was rejected by the enterprise's 366-day limit;
  Paxton replaced it. No action is needed on expiry.
- **Proof: BLOCKED on token scope, not yet green.** Measured with the installed token:
  - secrets list → REFUSED (403, genuine permission refusal) ✅
  - push → 403; GitHub says it needs `contents=write` ❌
  - PR create → 403; GitHub says it needs `pull_requests=write` ❌
  - private org repos (`cognizioware-mcp-tools`, `cognizioware-powerplatform`) → 404, invisible ❌
  - `LongHorizon-Harness` is public, which is the only reason clone worked.
  This pattern means the token has no org write access at all: either **Resource owner** is
  `prax211` instead of `cogniziocompany` (not editable, so generate a new token), or **Repository
  access** is "Public repositories" or the two permissions were never set (edit the token; the value
  stays the same, so nothing needs reinstalling). If the org requires approval for fine-grained
  tokens, a pending approval gives the same symptom.
- **Next:** fix the token, then rerun **only the proof**. If the token value changed, put it in
  `C:/tmp/secrets/<name>.txt` and run `TOK_FILE=C:/tmp/secrets/<name>.txt bash C:/tmp/gh-scoped-token-install.sh`;
  the install step is idempotent. Then do step 4 (restart), then update the doctrine.
- **Installer fixes made before the first run** (the original would have failed or lied):
  1. step 1 crashed with `set -u` because `\\$GH_TOKEN` expanded locally;
  2. the second `insteadOf` set overwrote the first, dropping the `git@github.com:` rewrite;
  3. `push | tail && echo OK` reported OK on a failed push;
  4. the merge probe targeted `main`, so an allowed merge (expected here) would have landed the
     probe commit on main; the PR is now between two throwaway `probe/` branches;
  5. `gh api -o/-w` are curl flags that `gh` rejects;
  6. the proof now aborts with exit 1 if clone fails or no PR URL comes back;
  7. `TOK_FILE` can be overridden with an env var.

## What is already done (nothing for you to redo)
- `gh` 2.45.0 is installed on CT110 (`/bin/gh`).
- An installer + proof script exists on ptait09: `C:/tmp/gh-scoped-token-install.sh`. It reads the
  token from a file, moves it to CT110 base64-wrapped (never in a transcript), writes it as
  `GH_TOKEN` in `/home/harness/.lh-harness-secrets.env` (mode 600, backup taken), sets one global
  git credential helper and one `insteadOf` rewrite so every existing `git@github.com:` remote
  works over HTTPS without touching any workspace, then **proves the limits** by pushing a
  throwaway branch, opening a PR, and attempting a merge, a workflow-file push and a secrets read.
- Measured today: CT110 currently has NO `gh` credential and NO GitHub token of any kind. Runs stop
  at every push or PR (four runs in four days). This is the gap being closed.

## What you do (10 minutes)

### 1. Create the token
GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate.

| field | value |
|---|---|
| Name | `lh-harness-ct110` |
| Expiration | 90 days (put the date in the calendar; rotation is a re-run of the installer). **Never "No expiration"**: measured 2026-09-14, the Cognizio.Company enterprise rejects fine-grained tokens with a lifetime over 366 days (HTTP 403 on clone, GraphQL error on `gh`). Expiry cannot be edited afterwards; use Regenerate and pick a date. |
| Resource owner | the `cogniziocompany` organization |
| Repository access | **All repositories** (Paxton: "one for all repos") |
| **Contents** | Read and write (this is what push needs) |
| **Pull requests** | Read and write (this is what `gh pr create` needs) |
| Metadata | Read (added automatically) |
| Everything else | **No access** — especially Actions, Secrets, Workflows, Administration, Environments |

If the org requires approval for fine-grained tokens, approve it under the org's token settings.

### 2. Drop it and run the installer
Paste the token, one line, into `C:/tmp/secrets/gh-harness-token.txt` on ptait09, then in Git Bash:

    bash C:/tmp/gh-scoped-token-install.sh

Then delete `C:/tmp/secrets/gh-harness-token.txt`.

### 3. Read the proof, do not trust the exit code
The script's last section must show exactly this shape:

    secrets list -> REFUSED: ... (HTTP 403)
    push base: OK
    push: OK
    pr create: https://github.com/cogniziocompany/LongHorizon-Harness/pull/NN
    merge -> ALLOWED (expected on a token owned by an admin; ...)   or REFUSED
    workflow push -> REFUSED: ... workflow ...
    remaining probe branches: 0 (want 0)

If merge is **not** refused, read the next section — it is expected on an admin's own token.
The PR is opened between two throwaway `probe/` branches, so an allowed merge touches nothing real.
If the token cannot clone the repo or no PR URL comes back, the proof now aborts with exit 1 rather
than printing REFUSED lines that only mean "the token is rejected outright".

### 4. Restart the service so runs see the token
`lh-harness` reads the secrets file through systemd `EnvironmentFile`, and only at startup. When no
run is active: `ssh root@192.168.21.151 "pct exec 110 -- systemctl restart lh-harness"`.

## The one honest caveat: "no merge" is not enforceable by permissions on an admin's token
Push and merge both use the same permission (Contents: write), and a token on Paxton's account is
an admin, so branch protection does not stop it. The permissions above genuinely enforce
**no secrets** and **no workflows**. For **no merge** there are two options:

- **Accept doctrine enforcement (works today).** Every harness task text already says "open the PR
  and STOP"; the overseer is the only thing that merges. The token *could* merge; the runs are told
  not to. This is what Paxton chose to start with ("we can add per-repo stuff later").
- **Enforce it on GitHub (later, if wanted).** Create a machine user (e.g. `cognizio-harness-bot`,
  needs its own email), give it **Write** (not admin) on the repos, issue the same fine-grained token
  on *that* account, and put a branch ruleset on each default branch requiring a pull request with
  1 approving review. A Write user cannot push to the protected branch and cannot approve its own
  PR, so it cannot merge; admins bypass as today. Measured today: only `cognizioware-powerplatform`
  `main` is protected; `develop` and every other default branch are unprotected.

## Rollback
    ssh root@192.168.21.151 "pct exec 110 -- bash -c 'cp \$(ls -1 /home/harness/.lh-harness-secrets.env.bak-ghtoken-* | head -1) /home/harness/.lh-harness-secrets.env; su - harness -c \"git config --global --unset-all url.https://github.com/.insteadof; git config --global --unset credential.https://github.com.helper\"'"

(The oldest backup is the pre-token file; each installer run takes a new one.)
Then revoke the token in GitHub. Existing SSH remotes keep working exactly as before.

## After it lands (overseer's job, not yours)
- Update the task template and LOOP-PROMPT: runs may push and open PRs; merging stays the
  overseer's. Remove the "the sandbox cannot open a PR" workaround from the doctrine.
- Rotate on the calendar date; the installer is idempotent.
