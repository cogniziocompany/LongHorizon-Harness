# Workspace guard path truth table (easybutt0n-17)

Base: origin/main 35ea475f04ff4a8a31853101f4d637b915307029 ("Merge pull request #204").
Every cell below is decided by reading that commit and by running the guard's own
functions against real directories (see "How these cells were pinned" at the end).
No service was started and no run was launched to produce this table. Where the
code does not decide a cell, the cell says UNKNOWN; no cell is a guess.

File name legend (all paths relative to the repo root):

- WG  = src/lh_harness/workspace_guard.py
- SRV = src/lh_harness/webapi/server.py
- LCH = src/lh_harness/launcher.py
- SVC = src/lh_harness/supervisor/service.py
- Q   = src/lh_harness/queue.py

## The three paths and their gates, in order

1. Direct create path: `POST /api/runs` (SRV:3819-3820). Gates in order:
   caller auth (SRV:2185-2199) -> guard base resolution `resolve_run_base`
   (SRV:3856-3861; a WorkspaceBaseError becomes 409 "workspace base refused",
   SRV:3862-3863) -> `require_git_checkout` (SRV:3878; a refusal becomes 409
   with the text from WG:85, SRV:3879-3880) -> `supervisor.create_run`
   (SRV:3881-3894). Inside create_run, the workspace-root boundary and other
   validation raise ValueError, which surfaces as 400 (SRV:3899-3905); the
   "reserved by another launch" ValueError surfaces as 409 (SRV:3903-3904).
2. Queue enqueue path: `POST /api/queue` (SRV:2520). The workspace value is
   validated ONLY for string shape by `_validate_workspace` (Q:572-584,
   invoked from the launch-params build at Q:1223): non-empty string, at most
   4096 chars, no NUL byte. There is NO filesystem check of any kind at
   enqueue, in either the scoped (SRV:2524-2589) or the unscoped
   (SRV:2590-2618) branch.
3. Queue launch path: the launcher's eligibility pass `_check_eligibility`
   (def LCH:1761, called at LCH:1156) -> skip rules (drain LCH:1768-1770,
   maintenance pause LCH:1771-1773, disk LCH:1774-1776, waiting LCH:1777-1779,
   trio capacity LCH:1785, node capacity LCH:1792-1795, interrupted worker
   LCH:1802-1804, workspace reservation LCH:1810-1815, active run on the same
   workspace LCH:1816-1820, dirty tree LCH:1830-1837, unpushed branch
   LCH:1838-1842) -> `resolve_run_base` (LCH:2101-2108; WorkspaceBaseError is
   recorded as a skip and the entry stays PENDING, LCH:2109-2131) ->
   `require_git_checkout` (LCH:2155; refusal recorded as a skip, entry stays
   PENDING, LCH:2156-2175) -> `supervisor.create_run` (LCH:2177).

The guard itself (WG:258-501 `prepare_workspace_base`) never checks the
workspace root. It classifies a path as "not-a-repo" when the directory has no
`.git` entry (WG:296: `(repo / ".git").exists()`; WG:297-301), and
`require_git_checkout` (WG:88-115) refuses exactly when the resolved base has
mode "not-a-repo" (WG:110-111) AND the resolved workspace path is an EXISTING
directory (WG:112: `Path(base.workspace).is_dir()`); a path that does not
exist yet returns None ("the worker creates it", WG:99-105, 112-114).

## Table

Rows are the workspace state at the moment of the call; columns are the three
paths. ACCEPT means the request/entry/launch proceeds past that path's
workspace gates; REFUSE names the reason code and where it fires; UNKNOWN
means this code read cannot decide the cell.

| Workspace state | Direct create path | Queue enqueue path | Queue launch path |
|---|---|---|---|
| 1. Missing (path does not exist) | ACCEPT -- WG:297-301 (mode not-a-repo) + WG:112-114 (not is_dir -> no refusal), wired at SRV:3878. Trace A1 below | ACCEPT -- Q:572-584 (string only), no FS check anywhere in SRV:2520-2618 | ACCEPT -- dirty/unpushed probes return False for a missing dir (LCH:683-684, LCH:702-703), guard passes per WG:112-114 via LCH:2155. Trace A1 below |
| 2. Existing EMPTY dir, not a git checkout | REFUSE(workspace_not_a_repo), HTTP 409 -- WG:110-115 (is_dir() is TRUE for an empty dir), wired at SRV:3878-3880 | ACCEPT -- Q:572-584 (string only) | REFUSE(workspace_not_a_repo), entry stays PENDING with the reason in skip_reasons and reason -- WG:110-115 via LCH:2155-2175 |
| 3. Existing NON-EMPTY dir, not a git checkout | REFUSE(workspace_not_a_repo), HTTP 409 -- WG:110-115, wired at SRV:3878-3880 | ACCEPT -- Q:572-584 (string only) | REFUSE(workspace_not_a_repo), entry stays PENDING -- WG:110-115 via LCH:2155-2175; the occupancy probes read a non-repo as clean (LCH:683-688, LCH:702-706), so they never fire first |
| 4. Git checkout, clean | ACCEPT -- on the default branch: mode "on-default", run executes in place on that branch (WG:343-346); on a non-default branch: a fresh run branch is cut in place via checkout -B from origin/<default> (WG:374, 383-393). require_git_checkout returns None (WG:110-111, mode is not not-a-repo) | ACCEPT -- Q:572-584 (string only) | ACCEPT -- dirty probe clean; unpushed probe fires only for a no-upstream branch carrying commits not on origin/main (LCH:704-722). Then the guard mirrors the direct-create classification (LCH:2101, WG:343-346 / WG:383-393) |
| 5. Git checkout, dirty | ACCEPT -- dirty on the DEFAULT branch: mode "on-default", the dirty tree is left as found and the run executes in it (WG:343-346 runs before any dirty check; the dirty/ahead logic at WG:376-381 is only reached on a non-default branch). Dirty on a NON-default branch: fresh linked worktree from origin/<default> (WG:398-426), or named stash + in-place if no worktree is possible (WG:432-450) | ACCEPT -- Q:572-584 (string only) | REFUSE for the default configuration: skip "workspace ... occupied: dirty tree", entry stays PENDING (LCH:1830-1837). Two opt-outs: the `occupancy_ignore_dirty` config override and continuation entries (`branch`/`continue_branch`), both checked at LCH:1830-1832; past them the guard decides as in the direct-create column (LCH:2099-2108) |
| 6. Symlink to a git checkout | ACCEPT when the symlink's RESOLVED target is inside the configured workspace root: the guard sees through the symlink (`repo / ".git"` .exists() follows it, WG:296) and classifies the TARGET's checkout state, so acceptance mirrors row 4 or 5 (pinned: symlink to a clean checkout on default -> mode "on-default", no refusal). REFUSE(workspace must be inside configured workspace root), HTTP 400, when the resolved target is outside the root -- SVC:2825 (resolve) + SVC:2828-2831 (relative_to check), reached via SRV:3881 -> SVC:2850 -> SVC:2862, surfaced at SRV:3901-3905 | ACCEPT -- Q:572-584 (string only) | ACCEPT with the same resolved-target boundary as the direct path (guard pass-through WG:296 via LCH:2101, then SVC:2850/2862); a target outside the root is accepted by every preflight but the entry is then marked FAILED, non-retryably, when create_run raises (LCH:2248-2263; "workspace outside the root" named non-retryable at LCH:2258-2260) |
| 7. Symlink to a NON-git dir (existing target) | REFUSE(workspace_not_a_repo), HTTP 409 -- WG:110-115: `repo/.git` does not exist through the symlink (WG:296) so the mode is not-a-repo, and WG:112's `Path(...).is_dir()` FOLLOWS the symlink, so the existing target counts as an existing directory (pinned). Wired at SRV:3878-3880 | ACCEPT -- Q:572-584 (string only) | REFUSE(workspace_not_a_repo), entry stays PENDING -- same WG:110-115 via LCH:2155-2175; the occupancy probes read the non-repo target as clean (LCH:683-688) so they never fire first |
| 8. Path outside the configured work root | REFUSE(workspace must be inside configured workspace root), HTTP 400 -- NOT a guard refusal: workspace_guard.py contains no root check (base_root only anchors relative paths, WG:287-289, and places run worktrees, WG:399-404). The refusal comes from SVC:2828-2831 via SRV:3881 -> SVC:2850 -> SVC:2862, surfaced as 400 at SRV:3901-3905 | ACCEPT -- Q:572-584 (string only); the enqueue path never resolves the path | REFUSE at the last step: every preflight passes (the eligibility probes do not check the root), then create_run raises the SVC:2831 ValueError and the entry is marked FAILED with "launch failed: workspace must be inside configured workspace root", non-retryably (LCH:2248-2263, LCH:2258-2260). Note the entry is not kept PENDING here, unlike rows 2/3/7 |
| 9. Path belonging to another tenant/owner | The code has NO tenant/owner notion for workspaces. Searched: no workspace-to-caller binding exists in caller_auth.py (its scopes govern ask scopes, REST run control and queue-entry deletion, caller_auth.py:357-361 and the ownership rule at caller_auth.py:537), and the only "tenant" strings in src/ are checkpoint-manifest schema keys unrelated to workspaces (checkpoint/manifest.py:268). The nearest notions are process-level, not tenant-level: the launch-time workspace reservation (SVC:2850-2913) and the active-run occupancy rule (LCH:1816-1820), both of which refuse only while ANOTHER LIVE LAUNCH/RUN holds the same workspace. Any caller permitted to control runs may name any workspace (SRV:2185-2199) | Same: no tenant notion; `requested_by` is stamped from the verified caller (SRV:2543-2544) but nothing binds a workspace to that caller | Same: no tenant notion |

## Traces: what happens NEXT on every ACCEPT of a non-checkout state

Only two rows produce an ACCEPT of a state that is not a git checkout, and
both are the same state seen by two paths.

### A1. Missing workspace directory (row 1, direct create and queue launch)

Pinned behaviour at 35ea475: the guard returns mode "not-a-repo" for a missing
directory (WG:297-301) and `require_git_checkout` returns None because the
path is not an existing directory (WG:112-114). The run is created. What
happens next, in code order:

1. `supervisor.create_run` (SVC:1983) -> `_create_run_once` (SVC:2148) enters
   `reserve_workspace` (SVC:2209 -> SVC:2850), which resolves the path
   (SVC:2862 -> SVC:2814-2832) and enforces only the workspace-root boundary.
2. `_create_run_once_reserved` executes
   `workspace_path.mkdir(parents=True, exist_ok=True)` (SVC:2263). THIS IS THE
   ENTIRE "the worker creates it" allowance (docstring WG:99-105): a plain,
   empty directory is created. Nothing else is materialized.
3. The path is re-resolved to catch a symlink introduced by the mkdir window
   (SVC:2264-2266), the run directory and owner record are written, and the
   worker command is spawned with `cwd=str(workspace_path)` (SVC:2595).

There is NO clone and NO `git init` anywhere in this chain. A full-tree search
of the launch chain (workspace_guard.py, launcher.py, supervisor/) for
clone/init finds none: the only repository-mutating git calls in the guard are
fetch/checkout/worktree-add/stash, and every one of them sits inside the
is-a-repo branch (WG:336-450); the supervisor and launcher never invoke git
clone or git init at all. Therefore: a run accepted on row 1 STARTS IN A
DIRECTORY WITH NO REPOSITORY, by design. Nothing initializes one later; the
mode "not-a-repo" is even recorded on the run for queue launches
(LCH:2189) precisely so the run says it is not branch-managed.

### Enqueue-column ACCEPTs (rows 1-9)

At enqueue time nothing touches the filesystem for the workspace: the only
workspace validation is the string check Q:572-584 reached from Q:1223, and
the entry is stored (SRV:2552-2556 / SRV:2597). The deciding moment for every
enqueue-ACCEPT cell is therefore deferred to the launch path, whose verdicts
are the third column. An enqueued entry naming a refused state simply sits
PENDING until the launch path refuses it (rows 2, 3, 7), fails it (rows 6 and
8 when the resolved target is outside the root), or launches it (rows 1, 4,
5-with-opt-out, 6-inside-root).

## The empty-directory case (row 2): what the code says, and what a probe must observe

Code answer at 35ea475 (this is a code fact, not a guess; it was also executed
against a real empty directory, see the evidence section): an EXISTING EMPTY
non-git directory is REFUSED with HTTP 409 and the exact reason text
"workspace_not_a_repo: workspace <path> is not a git checkout" on the direct
create path. The decider is WG:112: `Path(base.workspace).is_dir()` is TRUE
for an empty directory, so the "not created yet" escape at WG:112-114 does not
apply, and WG:110-111 already classified it as not-a-repo via WG:296-301.

Consequences at 35ea475:

- There is NO bounded initialization for the empty-directory case, because the
  case never reaches create_run; the refusal fires before any run row exists
  (SRV:3877-3880).
- There is also no hole at HEAD in the sense of "a run starts repo-less in an
  empty existing directory", for the same reason. The only repo-less start the
  code allows is the MISSING-directory allowance (trace A1), which is the
  documented pass-through.

History that explains how an installed build could behave differently (both
statements verified with git against this repository, and neither is used to
decide any cell above):

- Before commit 29f73b9 (merged as PR #204, 2026-11-15), the direct create
  path had NO workspace_not_a_repo preflight at all:
  `git show 29f73b9^:src/lh_harness/webapi/server.py` contains zero
  occurrences of require_git_checkout. On such a build, BOTH an empty and a
  non-empty non-git directory are accepted on the direct path.
- The queue launch path gained the refusal earlier, in b134d71 (PR #202,
  2026-11-14, LCH:2155).

So the observed ct110 acceptance of an existing empty directory (2026-11-15
21:58Z, an installed-build observation that is background only and decides
nothing in this table) is inconsistent with 35ea475's direct-create code and
is consistent with an installed build that predates 29f73b9. Which of the two
is true can only be settled on the installed node:

What the installed probe must observe (exactly):

1. `GET /api/meta` on the installed node and read `build.commit` (route
   SRV:2376; the value comes from the wheel's build stamp, build_info.py:33-40;
   `null` means the wheel was built without the deploy stamp, which is itself
   an answer: the build predates the stamped pipeline or is a dev wheel).
2. If `build.commit` contains 29f73b9 (any commit at or after it, including
   35ea475): POST /api/runs naming an EXISTING EMPTY non-git directory must
   return HTTP 409 with detail exactly
   "workspace_not_a_repo: workspace <path> is not a git checkout"
   (WG:85 via SRV:3878-3880). If it instead returns 200 with a run in status
   "starting", that contradicts the code read above and is a genuine new
   defect to raise against the direct path.
3. If `build.commit` predates 29f73b9: the acceptance is explained by the
   stale install (the pre-29f73b9 direct path had no preflight), and the
   empty-vs-non-empty difference the operator saw cannot both come from the
   same build -- re-observe the non-empty refusal on the SAME build before
   concluding anything further.
4. On any accepted create that reaches a run, whether anything later
   initializes a repository in the directory: at HEAD nothing does (trace A1,
   no clone/init in the chain), so the expectation is the run starts with no
   repository; observing a clone/init would falsify trace A1.

## Tenant/owner row (row 9): explicit statement

The code has no tenant/owner notion for workspaces. No data model, config key
or check binds a workspace to a caller, owner or tenant; the auth layer gates
WHO may create runs or enqueue (SRV:2185-2199, SRV:2524) but never WHICH
workspace they may name, and the only same-workspace protections are the
launch-time reservation and the active-run occupancy rule, both process-level
(LCH:1810-1820, SVC:2850-2913). Row 9 is therefore not ACCEPT/REFUSE but
"not applicable: no such notion in the code"; if a deployment needs tenant
isolation of workspaces, it is absent today.

## How these cells were pinned (re-runnable evidence)

Every behavioural claim above was additionally executed against the imported
functions from this checkout (no service, no network, no run), using local
temp directories and a local file-remote git repository:

- `prepare_workspace_base` + `require_git_checkout` were called directly for:
  an existing empty dir -> mode "not-a-repo", refusal returned; a missing dir
  -> mode "not-a-repo", refusal None; an existing non-empty non-git dir ->
  refusal returned; a symlink to a clean checkout on default -> mode
  "on-default", refusal None; a symlink to a non-git dir -> refusal returned;
  a dangling symlink -> mode "not-a-repo", refusal None (the dangling case
  then dies later: `mkdir(parents=True, exist_ok=True)` on a dangling symlink
  raises FileExistsError, so create_run cannot proceed -- noted here because
  it is the one state where the guard's "not created yet" escape lets a path
  through that create_run then rejects).
- Branch-position cases against a local bare remote: clean on default ->
  "on-default"; dirty on default -> "on-default" (dirty tree left as found);
  clean non-default -> "in-place" with a fresh run branch (WG:386); dirty
  non-default -> "worktree" in a sibling directory (WG:406).
- Symlink boundary semantics: `Path.resolve` on a root-inside symlink whose
  target is outside the root fails `relative_to(root)`, which is exactly the
  SVC:2825-2831 refusal.

The pinning tests for DELIVERABLE 2 (tests/test_workspace_guard_truth_table.py)
follow in a subsequent commit on this branch and encode exactly these cases.
