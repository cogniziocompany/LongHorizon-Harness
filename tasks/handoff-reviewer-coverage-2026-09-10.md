# Handoff — PR reviewer coverage for the four repos that have none

**For:** whoever picks up reviewer rollout (this is the substance of queue task 87,
`990-87-copilot-pr-review-infra-mcp`, currently last in the band).
**Written:** 2026-09-10 08:45 PT. Everything below was read from the live GitHub API today, not
from notes.

## The short version

**The only missing artifact is a 65-line workflow file, copied into four repos.** The hard parts —
the OAuth token, its org-wide visibility, the trigger semantics, the two bugs that made earlier
attempts useless — are already solved and are recorded below so you don't rediscover them.

## Current coverage — verified 2026-09-10, not inherited from a note

| repo | `.github/workflows/claude-code.yml` |
|---|---|
| `cognizioware-mcp-tools` | **present** — this is the reference implementation |
| `mcp-cognizioware` | present |
| `cognizioware-powerplatform` | **404 — missing** |
| `cognizioware-hydra` | **404 — missing** |
| `LongHorizon-Harness` | **404 — missing** |
| `cognizioware-qa` | **404 — missing** |

`cognizioware-powerplatform` is the one that matters most: the customer-facing session-flow defects
(queue task 107, currently at the head of the queue) ship there, and they will merge unreviewed
unless this lands.

## The secret is already solved — do not provision anything per repo

This is the single biggest time-saver here, and it is easy to get wrong by assuming.

```
orgs/cogniziocompany/actions/secrets/CLAUDE_CODE_OAUTH_TOKEN
  visibility = all        created 2026-09-10
```

**`visibility = all` means every repo in the org can already read it.** Per-repo secrets show only
`ANTHROPIC_API_KEY` on the four missing repos, and `mcp-tools` additionally has a repo-level
`CLAUDE_CODE_OAUTH_TOKEN` — that repo-level copy is redundant now that the org secret exists.

So: **add the workflow file. Add nothing else.** No token minting, no per-repo secret, no org change.

## The reference implementation

`cognizioware-mcp-tools/.github/workflows/claude-code.yml` — 65 lines, 2,491 bytes. Copy it
verbatim; every non-obvious line in it exists because something broke.

```yaml
name: Claude Code PR Review

on:
  issue_comment:
    types: [created]
  pull_request_review_comment:
    types: [created]

concurrency:
  group: claude-pr-${{ github.event.issue.number || github.event.pull_request.number || ... }}
  cancel-in-progress: false

permissions:
  contents: write
  pull-requests: write
  issues: write
  id-token: write
```

Job gate — the reviewer only runs when a **human with write access** types `@claude` on a PR:

```yaml
    if: |
      ( github.event_name == 'issue_comment' &&
        github.event.issue.pull_request != null &&
        contains(github.event.comment.body, '@claude') &&
        ( author_association == 'OWNER' || 'MEMBER' || 'COLLABORATOR' ) )
      || ( github.event_name == 'pull_request_review_comment' && ...same... )
```

Invocation:

```yaml
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Run Claude Code in tag mode
        uses: anthropics/claude-code-action@v1
        with:
          claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

## Four things that will bite you — all of them already bit us

### 1. It does NOT fire on `pull_request`. Opening a PR reviews nothing.
The triggers are `issue_comment` and `pull_request_review_comment` only. **A PR sits unreviewed
until a human posts a comment containing `@claude`.** This is the single most common
misunderstanding: people open a PR, see no review, and conclude the workflow is broken. It isn't —
nobody tagged it. If you want review-on-open, that is a *different* workflow and a deliberate
decision, because of the cost note below.

### 2. `fetch-depth: 0` is load-bearing, not hygiene.
A depth-1 clone contains **only the PR head commit**, so the reviewer cannot see `main`. On
2026-09-09 that made it report that a real commit "does not exist" and that symbols present on
`main` appeared nowhere — **it accused correct work of being fabricated.** Full history is what lets
a cross-branch claim be checked before it is made. Do not "optimise" this.

### 3. `timeout_minutes` is not a valid input and never worked.
`claude-code-action@v1` rejects it with `Unexpected input(s) 'timeout_minutes'`. The 15-minute limit
it appeared to set **was never in effect.** The real cap is `timeout-minutes: 20` at the **job**
level. This matters because `cancel-in-progress: false` means a hung job would otherwise block every
later `@claude` mention on that PR until the runner's 6-hour default fired.

### 4. Every invocation bills a personal subscription.
Since mcp-tools PR #131 the reviewer authenticates with `CLAUDE_CODE_OAUTH_TOKEN` — a **subscription
OAuth token, not a metered API key.** Each `@claude` spends Paxton's personal Claude quota. That is
why nobody has bulk-invoked it across the 25-PR backlog, and why review-on-open was not chosen.
**Adding the workflow to four repos does not itself cost anything** — the cost is per invocation.
Say this explicitly to whoever asks for review-on-open.

## Rollout order — lowest blast radius first

1. **`cognizioware-qa`** — gates other lanes, ships nothing itself. Safest place to confirm the copy
   works end to end: open a throwaway PR, comment `@claude`, watch the run.
2. **`LongHorizon-Harness`** — active PRs (#7, #8, #10 open) and no deploy fires on push to `main`
   (its only workflow is `release.yml`, on `push: tags`), so a workflow file lands with no side
   effects.
3. **`cognizioware-powerplatform`** — the one that actually matters. Do it once 1 and 2 have each
   produced a real review.
4. **`cognizioware-hydra`** — **CHECK BEFORE ACTING.** PR **#26** already adds the reviewer to hydra
   and is deliberately gated behind the BMAD spec process (queue task 116, itself behind task 111)
   by Paxton's direction. **Do not duplicate it and do not merge it** — coordinate, or you will
   create a conflicting second workflow.

## How to verify it actually works — do not stop at "the file is there"

A workflow file existing proves nothing. For each repo:

1. Open a trivial PR (or use an existing one).
2. Post a comment containing `@claude` **from an account with OWNER/MEMBER/COLLABORATOR
   association** — a comment from an outside contributor is silently ignored by the `if:` gate, and
   that silence looks identical to a broken workflow.
3. Confirm a workflow run appears under Actions **and** that a review comment is posted back.
4. Record which repo, which PR, and what the reviewer said.

If nothing runs, check in this order: was the comment on a **pull request** (not a plain issue);
does the body literally contain `@claude`; is the commenter's `author_association` one of the three
allowed values.

## What is out of scope here

- **Copilot** is quota-exhausted across the org — every recent PR carries only *"Copilot was unable
  to review this pull request because the user who requested the review has reached their quota
  limit."* It is not an alternative and should not be waited on.
- Bulk-reviewing the existing open-PR backlog. That is queue task 117
  (`748-117-pr-backlog-triage`), which deliberately produces the triage map **without** spending
  subscription quota. Adding coverage and draining the backlog are separate decisions.
