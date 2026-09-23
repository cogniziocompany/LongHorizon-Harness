# Handoff: create + install the `cognizioware-lh-reviewer` GitHub App and hand its secrets to Hydra

For Paxton (org owner steps) and the overseer (delivery + verification). Written 2026-09-15 by session [fe2679].
Plan: C:/tmp/PLAN-pr-review-gate-2026-09-15.md. Task that consumes this: 187 (Hydra webhook; was 180).

Measured today: org `cogniziocompany` already has the `claude` App installed on ALL repos (fallback path),
plus cursor, chatgpt-codex-connector, cloudflare-workers-and-pages, apify, claude-design-import, monday-com-github.
No App of ours exists yet. GitHub App CREATION has no gh CLI command - it is web or the manifest flow; everything
after creation is scriptable with `gh api`.

## 1. Create the App (web, ~5 minutes) - Paxton, org owner

Fastest path = the manifest flow, which pre-fills everything. Open in a browser while logged in as an org owner:

    https://github.com/organizations/cogniziocompany/settings/apps/new

Fill exactly:
| field | value |
|---|---|
| GitHub App name | `cognizioware-lh-reviewer` |
| Homepage URL | `https://hydra.cognizioware.com` |
| Webhook: Active | checked |
| Webhook URL | `https://hydra.cognizioware.com/github/webhook` |
| Webhook secret | generate: `openssl rand -hex 32` (or PowerShell: `-join ((1..32) \| % { '{0:x2}' -f (Get-Random -Max 256) })`); keep it, you paste it into Hydra's env in step 3 |
| Repository permissions | Pull requests: **Read and write** · Checks: **Read and write** · Contents: **Read-only** · Metadata: **Read-only** · Commit statuses: **Read and write** (for the check fallback) · Issues: **Read and write** (fallback comment) |
| Organization permissions | none |
| Subscribe to events | Pull request · Check suite · Check run · Issue comment (fallback verdict read-back) |
| Where can this App be installed | **Only on this account** |

Click Create. Then on the App page:
1. **Generate a private key** (button at the bottom) - downloads `cognizioware-lh-reviewer.<date>.private-key.pem`.
2. Note the **App ID** (top of the page) and the **Client ID** (not needed for webhooks, harmless to note).

Alternative if you prefer the manifest flow: the same page accepts a manifest; equivalent JSON:
```json
{"name":"cognizioware-lh-reviewer","url":"https://hydra.cognizioware.com","hook_attributes":{"url":"https://hydra.cognizioware.com/github/webhook","active":true},"public":false,
 "default_permissions":{"pull_requests":"write","checks":"write","contents":"read","metadata":"read","statuses":"write","issues":"write"},
 "default_events":["pull_request","check_suite","check_run","issue_comment"]}
```

## 2. Install it on the eight repos (web, 1 minute) - Paxton

App page -> **Install App** -> cogniziocompany -> "Only select repositories" -> pick:
cognizioware-mcp-tools, mcp-cognizioware, LongHorizon-Harness, cognizioware-hydra, cognizioware-powerplatform,
cognizioware-qa, BMAD_Cognizioware, ptait09-easybutt0n-ai. Install.

Verify from any machine with `gh` (org-owner login):
```bash
gh api orgs/cogniziocompany/installations --jq '.installations[] | select(.app_slug=="cognizioware-lh-reviewer") | "id=\(.id) repos=\(.repository_selection)"'
```
Record the **installation id** printed - Hydra needs it (`GITHUB_APP_INSTALLATION_ID`).

## 3. Hand the secrets to Hydra (corsairai300) - Paxton copies, overseer verifies; values never in a repo

Env NAMES Hydra will read (task 187 wires them; put them in `/opt/cognizioware-hydra/.env`, which is gitignored):
```
GITHUB_APP_ID=<App ID from step 1>
GITHUB_APP_INSTALLATION_ID=<from step 2>
GITHUB_APP_PRIVATE_KEY_PATH=/opt/cognizioware-hydra/secrets/lh-reviewer.pem
GITHUB_WEBHOOK_SECRET=<the hex string from step 1>
```
Copy the pem without it ever touching a transcript or a repo (from PTAIT09, Git Bash):
```bash
ssh -i ~/.ssh/id_ed25519_proxmox root@192.168.21.151 'mkdir -p /opt/cognizioware-hydra/secrets && chmod 700 /opt/cognizioware-hydra/secrets'
scp -i ~/.ssh/id_ed25519_proxmox ~/Downloads/cognizioware-lh-reviewer.*.private-key.pem root@192.168.21.151:/opt/cognizioware-hydra/secrets/lh-reviewer.pem
ssh -i ~/.ssh/id_ed25519_proxmox root@192.168.21.151 'chmod 600 /opt/cognizioware-hydra/secrets/lh-reviewer.pem && ls -la /opt/cognizioware-hydra/secrets'
```
Then delete the pem from Downloads. The four env lines: append to `/opt/cognizioware-hydra/.env` on the host
(same ssh; use a heredoc, not a paste into a shared chat). docker-compose.yml (task 187) mounts `./secrets:/secrets:ro`.

Overseer read-back (no values printed):
```bash
ssh root@192.168.21.151 'cd /opt/cognizioware-hydra && grep -c "^GITHUB_APP_ID=\|^GITHUB_APP_INSTALLATION_ID=\|^GITHUB_APP_PRIVATE_KEY_PATH=\|^GITHUB_WEBHOOK_SECRET=" .env; stat -c "%a %s" secrets/lh-reviewer.pem'
```
Expect `4` and `600 <~1700>`.

## 3b. Prove the App can act before task 187 lands (overseer, from corsairai300, values stay on the box)
Mint an installation token and read one PR - this is the exact call Hydra will make:
```bash
# needs: python3 + pyjwt on the host (pip install pyjwt cryptography), or run inside the orchestrator container
python3 - <<'PY'
import jwt,time,os,json,urllib.request
app=os.environ['GITHUB_APP_ID']; key=open(os.environ['GITHUB_APP_PRIVATE_KEY_PATH']).read()
tok=jwt.encode({'iat':int(time.time())-60,'exp':int(time.time())+540,'iss':app},key,algorithm='RS256')
req=urllib.request.Request(f"https://api.github.com/app/installations/{os.environ['GITHUB_APP_INSTALLATION_ID']}/access_tokens",method='POST',headers={'Authorization':f'Bearer {tok}','Accept':'application/vnd.github+json'})
t=json.load(urllib.request.urlopen(req))['token']
r=json.load(urllib.request.urlopen(urllib.request.Request('https://api.github.com/repos/cogniziocompany/LongHorizon-Harness/pulls/19',headers={'Authorization':f'Bearer {t}'})))
print('ok', r['number'], r['state'], 'token_prefix', t[:4])
PY
```
Expect `ok 19 open token_prefix ghs_`. A 401 means wrong App ID or key; a 404 means the installation does not
cover that repo.

## 4. powerplatform branch protection - concerns and questions (owned by the overseer/[fe2679]; DECISION = Paxton)
Measured 2026-09-15: `main` and `release` are protected with ONE required check, context `gate` (eval-gate.yml),
strict=false, 0 required reviews, enforce_admins=false. `develop` is UNPROTECTED. No rulesets. Repo setting
`allow_auto_merge=false`, `delete_branch_on_merge=false`. ci.yml deploys develop->dev on push.
1. develop is where every run's PR lands and where dev deploys from, and it has no protection at all: a direct
   push or a green-less merge deploys to dev today. Proposed: protect develop requiring `pr-gate` + `harness-review`,
   strict=false, 0 reviews (the checks are the review). CONCERN: task 154/153 workspaces commit to develop-derived
   branches on CT110 - required checks only affect merges, not pushes to feature branches, so no run breaks.
2. release: keep `gate`, ADD `pr-gate` + `harness-review`. CONCERN: promote.yml fast-forwards main and may push
   release; a required check on a branch that is fast-forwarded by a workflow can block the promotion if the check
   never ran on that sha. Must verify what promote.yml pushes to release before adding checks there.
3. main = prod, fast-forwarded by promote.yml's bookkeeping job (non-FF refused). ADDING required checks to main can
   break promotion for the same reason as 2. Proposed: main keeps `gate` only; auto-merge never targets main.
   DECISION for Paxton only if he wants harness-review required on main too (default: no).
4. `allow_auto_merge=false` at repo level: auto-merge for impact:docs PRs into develop needs this flipped to true.
   Flipping it does not merge anything by itself. DECISION: allow (default: yes, since only impact:docs uses it).
5. enforce_admins=false on main/release: an admin can bypass `gate`. Leave as is (it is how emergency fixes ship);
   note it in the doctrine.
6. `gate` is red on main right now (Ruflo live verification) - that is a pipeline blocker, not a protection setting;
   it is T0 of the powerplatform chronology. Adding more required checks does not change that.
7. The `claude` App is installed on all repos, but powerplatform has NO claude-code.yml, so the fallback there is
   Copilot only until task 132 (parked on Paxton's go). Concern: Copilot is quota-exhausted org-wide today, so
   powerplatform has no fallback at all until 132 lands. Proposed: release 132 as fallback-install after 180.
8. Squash vs merge commit: promote.yml fast-forwards main to a promoted sha; if auto-merge into develop squashes,
   develop history stays linear and promotion is unaffected. Use `--squash` for auto-merge.
