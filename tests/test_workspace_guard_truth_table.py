"""easybutt0n-17: truth-table pins for the workspace branch guard (D2).

DELIVERABLE 2 of ticket easybutt0n-17.  PR #205 ("easybutt0n-17: workspace
guard path truth table", merged as 8bcc031) added
``docs/workspace-guard-truth-table.md``: a 9-row x 3-path table of what the
workspace guard decides for every workspace state, each cell derived from the
deciding code and from executing the guard's own functions.  That PR
explicitly deferred the pinning tests to a follow-up; this file is that
follow-up, seeded from the reviewer's throwaway test in PR #205 comment
6905618162 (``test_empty_non_git_dir_is_refused``, written and run at head
a7dcb4e but never committed there).

What this file pins
-------------------
Rows 1-9 of the #205 table, plus the two cells the #205 review found missing
(detached-HEAD, wrong-remote) and the dangling-symlink cell recorded in the
doc's evidence section:

    row 1  missing path                   ACCEPT on every path (guard classifies
                                        it ``not-a-repo`` and the "not created
                                        yet" escape returns None)
    row 2  existing EMPTY non-git dir    REFUSE(workspace_not_a_repo)
    row 3  existing NON-EMPTY non-git dir REFUSE(workspace_not_a_repo)
    row 4  clean checkout                ``on-default`` / clean non-default is
                                        re-branched ``in-place``
    row 5  dirty checkout                dirty-on-default stays ``on-default``
                                        with the tree left as found; dirty
                                        non-default moves the run to a
                                        ``worktree`` (or a named ``stash``)
    row 6  symlink -> git checkout      classified as the TARGET's checkout
                                        state (the guard resolves through the
                                        link)
    row 7  symlink -> non-git dir        REFUSE(workspace_not_a_repo) -- the
                                        existing target counts as existing
    row 8  path outside the work root    the GUARD ACCEPTS (workspace_guard.py
                                        contains no root check); the boundary
                                        lives in RunSupervisor._resolve_workspace
    row 9  tenant/owner binding          NO such notion exists in the guard
    cell   detached HEAD                  ACCEPT -- re-branched in place
    cell   wrong remote                   ACCEPT -- ``on-default``, in place
    cell   dangling symlink               guard passes (``not-a-repo`` + the
                                        "not created yet" escape); the later
                                        ``mkdir`` in create_run is what dies

These pins encode the CURRENT behaviour only.  Nothing here is a desired-state
spec: where the current behaviour is an accept that a hardening pass may want
to revisit (row 1's repo-less start, the wrong-remote accept, row 9's absent
tenant/workspace binding), the test pins it AS IS and marks it
``identity decision on the hardening list`` rather than changing it.  The
guard itself is not modified by this PR; if any pin below fails, the guard
changed and this table must be re-derived, not the test relaxed.

How to read the three path columns
----------------------------------
The #205 table has one column per launch path.  This file pins the guard's own
decision for every row (the guard is the component under test) plus the two
path-side facts the table cites that are pinnable without starting a service:

* enqueue column -- ``queue._validate_workspace`` accepts EVERY row: it
  checks string shape only and never touches the filesystem (Q:572-584);
* queue-launch column -- the launcher's occupancy probes
  (``_dirty_workspace_occupied`` / ``_unpushed_branch_occupied``) read a
  non-repo or a missing path as CLEAN and fail open (LCH:683-688), so on rows
  2/3/7 the guard's refusal is the first refusal the entry meets (the entry
  stays PENDING, LCH:2155-2175), while on row 5 the dirty probe fires first
  (LCH:1830-1837).

The full HTTP 409/400 wiring and the launcher's skip bookkeeping are already
pinned by the existing suites (tests/webapi/test_webapi_workspace_guard.py,
tests/webapi/test_launcher_workspace_guard.py,
tests/webapi/test_launcher_occupancy.py) and are cited, not duplicated, here.

Every git fixture is a throwaway repository under pytest's ``tmp_path`` (bare
origin + working clone, the same shape as
tests/webapi/test_launcher_workspace_guard.py).  No test touches the network:
the only remotes are local bare repositories inside ``tmp_path``, and every
open-PR probe is stubbed with ``NO_PR`` so not even ``gh`` is invoked.

Sources: docs/workspace-guard-truth-table.md at 8bcc031 (PR #205), the #205
review comment 6905618162 (the seed test), and src/lh_harness/workspace_guard.py
(WG citations below follow the doc's legend).
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import subprocess
from pathlib import Path

import pytest

from lh_harness.launcher import _dirty_workspace_occupied, _unpushed_branch_occupied
from lh_harness.queue import _validate_workspace
import lh_harness.workspace_guard as workspace_guard
from lh_harness.workspace_guard import (
    WorkspaceBase,
    prepare_workspace_base,
    require_git_checkout,
    resolve_run_base,
)

# The derivation probes never query PRs: the guard only consults the probe on
# a NON-default branch (WG:349), and this table's rows are about path/checkout
# state, so a constant None keeps every pinned decision off the network and
# off the real ``gh`` binary.
NO_PR = lambda repo, branch: None  # noqa: E731 - matches the existing guard suites

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "truth-table-pin",
    "GIT_AUTHOR_EMAIL": "truth-table-pin@example.invalid",
    "GIT_COMMITTER_NAME": "truth-table-pin",
    "GIT_COMMITTER_EMAIL": "truth-table-pin@example.invalid",
}


@pytest.fixture(autouse=True)
def _hermetic_git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the guard's own git children a deterministic identity.

    The guard builds its child environments with
    ``safe_subprocess.child_env`` -- a scrubbed copy of THIS process's
    environment -- so the identity has to live in ``os.environ``: a per-call
    ``env=`` on this file's own git helper would never reach the ``git stash``
    the guard runs internally (WG:436).
    """

    for name, value in GIT_IDENTITY.items():
        monkeypatch.setenv(name, value)


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo``; fail loudly, because every fixture must build."""

    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"git {args} failed in {repo}: {proc.stderr}"
    return proc.stdout.strip()


def _make_repo(tmp_path: Path, name: str = "ws", default_branch: str = "main") -> Path:
    """A working clone with a local bare origin, clean on ``default_branch``."""

    origin = tmp_path / f"{name}.origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", default_branch, str(origin)],
        check=True,
        capture_output=True,
    )
    repo = tmp_path / name
    subprocess.run(
        ["git", "init", "-b", default_branch, str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / "README.md").write_text(f"base for {name}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial commit")
    _git(repo, "push", "-q", "-u", "origin", default_branch)
    return repo


def _feature_branch(repo: Path, name: str = "feat/other-task") -> None:
    """A clean, pushed non-default branch carrying another task's work."""

    _git(repo, "checkout", "-q", "-b", name)
    (repo / "feature.txt").write_text("another task's committed work\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feature work")
    _git(repo, "push", "-q", "-u", "origin", name)


def _not_a_repo_refusal(path: Path | str) -> str:
    """The exact refusal text WG:85 builds for a non-checkout workspace."""

    return f"workspace_not_a_repo: workspace {path} is not a git checkout"


# ---------------------------------------------------------------------------
# Row 1 -- missing path: ACCEPT on every path (pinned as-is)
# ---------------------------------------------------------------------------


def test_row_1_missing_path_is_accepted_on_every_path(tmp_path: Path) -> None:
    """#205 row 1: a path that does not exist yet is ACCEPTED, on all three paths.

    WG:297-301 classifies it ``not-a-repo`` and WG:112-114 returns ``None``
    from ``require_git_checkout`` because the path is not an existing
    directory -- the "the worker creates it" escape.  The accepted run then
    starts with NO repository: create_run's only materialization is
    ``mkdir(parents=True, exist_ok=True)`` (SVC:2263, trace A1 in the doc) --
    there is no clone and no ``git init`` anywhere in the launch chain, so the
    run starts repo-less BY DESIGN.  Pinned as-is; the missing-path accept is
    an identity decision on the hardening list.
    """

    missing = tmp_path / "not-created-yet"

    base = prepare_workspace_base(missing, run_label="row-1", probe_open_pr=NO_PR)
    assert base.mode == "not-a-repo"
    assert base.workspace == Path(missing)
    assert require_git_checkout(base, missing) is None

    # resolve_run_base (the one helper both create paths share, SRV:3856 /
    # LCH:2101) passes it through: the run executes in the named workspace.
    resolved_base, effective = resolve_run_base(
        str(missing), run_label="row-1", probe_open_pr=NO_PR
    )
    assert resolved_base.mode == "not-a-repo"
    assert effective == str(missing)

    # Queue-launch column: the occupancy probes never fire for a missing dir
    # (LCH:683-684, LCH:702-703 -- ``not is_dir`` reads as clean).
    assert _dirty_workspace_occupied(str(missing)) is False
    assert _unpushed_branch_occupied(str(missing)) is False


# ---------------------------------------------------------------------------
# Row 2 -- existing EMPTY non-git dir: REFUSE (seed test)
# ---------------------------------------------------------------------------


def test_row_2_existing_empty_non_git_dir_is_refused(tmp_path: Path) -> None:
    """#205 row 2: an existing EMPTY non-git dir is REFUSED with the exact text.

    Seeded from the reviewer's throwaway test in PR #205 comment 6905618162.
    WG:110-115: the mode is ``not-a-repo`` (WG:296-301) and
    ``Path(base.workspace).is_dir()`` is TRUE for an empty directory, so the
    "not created yet" escape at WG:112-114 does not apply.  The occupancy
    probes read the non-repo as clean and fail open (LCH:683-688), so on the
    queue-launch path the guard's refusal is the first one the entry meets.
    """

    empty = tmp_path / "empty-ws"
    empty.mkdir()  # exists, is a directory, is NOT a git checkout

    base = prepare_workspace_base(empty, run_label="row-2", probe_open_pr=NO_PR)
    assert base.mode == "not-a-repo", base.mode

    refusal = require_git_checkout(base, empty)
    # Current behaviour at 8bcc031: REFUSED, because
    # Path(base.workspace).is_dir() is True for an empty directory, so the
    # "not created yet" escape does not apply (WG:112-114).
    assert refusal is not None, "empty non-git dir unexpectedly ACCEPTED"
    assert refusal == _not_a_repo_refusal(empty), refusal

    # Contrast pair, same guard, same calls (the seed's second half): a path
    # that does NOT exist yet takes the "not created yet" escape and is
    # ACCEPTED (row 1).
    missing = tmp_path / "not-created-yet"
    base2 = prepare_workspace_base(missing, run_label="row-2-contrast", probe_open_pr=NO_PR)
    assert base2.mode == "not-a-repo", base2.mode
    assert require_git_checkout(base2, missing) is None, "missing dir refused"

    # Queue-launch column: the probes read the non-repo as clean, so they
    # never fire before the guard's refusal.
    assert _dirty_workspace_occupied(str(empty)) is False
    assert _unpushed_branch_occupied(str(empty)) is False


# ---------------------------------------------------------------------------
# Row 3 -- existing NON-EMPTY non-git dir: REFUSE
# ---------------------------------------------------------------------------


def test_row_3_existing_non_empty_non_git_dir_is_refused(tmp_path: Path) -> None:
    """#205 row 3: an existing NON-EMPTY non-git dir gets the same refusal.

    Same decider as row 2 (WG:110-115); the doc adds that the occupancy
    probes read a non-repo as clean (LCH:683-688, LCH:702-706), so they never
    fire first on the queue-launch path either.
    """

    scratch = tmp_path / "scratch-ws"
    scratch.mkdir()
    (scratch / "notes.txt").write_text("not a repository, just files\n", encoding="utf-8")

    base = prepare_workspace_base(scratch, run_label="row-3", probe_open_pr=NO_PR)
    assert base.mode == "not-a-repo"
    refusal = require_git_checkout(base, scratch)
    assert refusal == _not_a_repo_refusal(scratch), refusal

    assert _dirty_workspace_occupied(str(scratch)) is False
    assert _unpushed_branch_occupied(str(scratch)) is False


# ---------------------------------------------------------------------------
# Row 4 -- clean checkout: on-default / re-branched in place
# ---------------------------------------------------------------------------


def test_row_4_clean_checkout_on_default_stays_in_place(tmp_path: Path) -> None:
    """#205 row 4: a clean checkout ON the default branch is used as-is.

    WG:343-346: ``current == default`` returns mode ``on-default`` and the
    run executes in place on that branch.  ``require_git_checkout`` returns
    None because the mode is not ``not-a-repo`` (WG:110-111).
    """

    repo = _make_repo(tmp_path, "row-4-default")

    base = prepare_workspace_base(repo, run_label="row-4", probe_open_pr=NO_PR)
    assert base.mode == "on-default"
    assert base.workspace == repo
    assert base.original_branch == "main"
    assert base.run_branch is None  # nothing was cut; the branch is the base
    assert require_git_checkout(base, repo) is None

    # Queue-launch column: a clean default branch is not occupied -- the
    # dirty probe is empty and the unpushed probe stops at the branch's
    # upstream (LCH:704-722).
    assert _dirty_workspace_occupied(str(repo)) is False
    assert _unpushed_branch_occupied(str(repo)) is False


def test_row_4_clean_non_default_branch_is_rebranched_in_place(tmp_path: Path) -> None:
    """#205 row 4: a CLEAN non-default branch is never launched on as-is.

    WG:374, WG:383-393: the guard cuts a fresh run branch from
    ``origin/<default>`` and switches to it in place; the previous branch and
    its commits survive untouched (deliverable 1 of the guard).
    """

    repo = _make_repo(tmp_path, "row-4-non-default")
    _feature_branch(repo)  # clean and synced with origin

    base = prepare_workspace_base(repo, run_label="row-4b", probe_open_pr=NO_PR)
    assert base.mode == "in-place"
    assert base.workspace == repo
    assert base.original_branch == "feat/other-task"
    assert base.run_branch is not None and base.run_branch.startswith("lh-run/")
    assert require_git_checkout(base, repo) is None

    # The run now sits on its own branch cut fresh from origin/main...
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").startswith("lh-run/")
    assert _git(repo, "rev-parse", "HEAD") == _git(
        repo, "rev-parse", "refs/remotes/origin/main"
    )
    # ...while the previous branch survives untouched.
    assert "feat/other-task" in _git(repo, "branch", "--list", "feat/other-task")
    assert _git(repo, "rev-parse", "feat/other-task") != _git(repo, "rev-parse", "HEAD")

    # A pushed non-default branch is not "occupied" either (upstream exists);
    # the firing side of that probe (no upstream + commits off origin/main)
    # is pinned by tests/webapi/test_launcher_occupancy.py.
    assert _unpushed_branch_occupied(str(repo)) is False


# ---------------------------------------------------------------------------
# Row 5 -- dirty checkout: on-default / worktree / stash
# ---------------------------------------------------------------------------


def test_row_5_dirty_default_checkout_stays_on_default(tmp_path: Path) -> None:
    """#205 row 5: a DIRTY checkout on the DEFAULT branch stays ``on-default``.

    WG:343-346 runs before any dirty check -- the dirty/ahead logic at
    WG:376-381 is only reached on a non-default branch -- so the dirty tree
    is left exactly as found and the run executes in it.
    """

    repo = _make_repo(tmp_path, "row-5-default")
    (repo / "uncommitted.txt").write_text("someone's work in progress\n", encoding="utf-8")

    base = prepare_workspace_base(repo, run_label="row-5", probe_open_pr=NO_PR)
    assert base.mode == "on-default"
    assert base.workspace == repo
    assert require_git_checkout(base, repo) is None
    # The dirty tree is left as found.
    assert (repo / "uncommitted.txt").read_text(encoding="utf-8") == "someone's work in progress\n"
    assert _git(repo, "status", "--porcelain").strip() != ""

    # Queue-launch column, default configuration: the dirty probe fires, so
    # the entry is skipped with "occupied: dirty tree" and stays PENDING
    # (LCH:1830-1837; the two opt-outs -- ``occupancy_ignore_dirty`` and
    # continuation entries -- are checked at LCH:1830-1832 before it).
    assert _dirty_workspace_occupied(str(repo)) is True


def test_row_5_dirty_non_default_branch_moves_the_run_to_a_worktree(tmp_path: Path) -> None:
    """#205 row 5: foreign work on a NON-default branch is preserved, not used.

    WG:398-426: the checked-out branch is left completely alone and the run
    gets a fresh linked worktree branched from ``origin/<default>``.
    """

    repo = _make_repo(tmp_path, "row-5-worktree")
    _feature_branch(repo)
    foreign = repo / "fleet-admin" / "other-task.txt"
    foreign.parent.mkdir()
    foreign.write_text("another task's uncommitted work\n", encoding="utf-8")
    _git(repo, "add", "fleet-admin")

    base = prepare_workspace_base(repo, run_label="row-5w", probe_open_pr=NO_PR)
    assert base.mode == "worktree"
    assert base.original_branch == "feat/other-task"
    assert base.workspace != repo
    assert base.workspace.is_dir()

    # ...a linked worktree branched from origin/main...
    assert (base.workspace / ".git").is_file()  # linked-worktree marker
    assert _git(base.workspace, "rev-parse", "--abbrev-ref", "HEAD").startswith("lh-run/")
    assert _git(base.workspace, "rev-parse", "HEAD") == _git(
        repo, "rev-parse", "refs/remotes/origin/main"
    )
    # ...while the original branch is untouched and still dirty.
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "feat/other-task"
    assert foreign.read_text(encoding="utf-8").startswith("another task's")
    assert _git(repo, "status", "--porcelain").strip() != ""

    # Queue-launch column: the dirty probe fires for the occupied workspace.
    assert _dirty_workspace_occupied(str(repo)) is True


def test_row_5_worktree_fallback_stash_preserves_the_dirty_tree(tmp_path: Path) -> None:
    """#205 row 5, fallback: no worktree possible -> named stash + in place.

    WG:432-450: when the linked worktree cannot be created, the leftover is
    preserved in a named stash whose message names the branch it came from,
    and the run branch is cut in place from ``origin/<default>``.
    """

    repo = _make_repo(tmp_path, "row-5-stash")
    _feature_branch(repo)
    foreign = repo / "foreign" / "keep-me.txt"
    foreign.parent.mkdir()
    foreign.write_text("foreign uncommitted work\n", encoding="utf-8")
    _git(repo, "add", "foreign")
    # Make worktree creation impossible: the sibling path the guard would use
    # (WG:398) is pre-occupied.
    sibling = repo.parent / f"{repo.name}.run-row-5s"
    sibling.mkdir(parents=True)
    (sibling / "occupied.bin").write_bytes(b"occupied")

    base = prepare_workspace_base(repo, run_label="row-5s", probe_open_pr=NO_PR)
    assert base.mode == "stash"
    assert base.stashed is True
    assert base.workspace == repo
    assert (repo / "foreign" / "keep-me.txt").exists() is False  # moved into the stash

    stash_list = _git(repo, "stash", "list")
    assert "prelaunch guard" in stash_list and "row-5s" in stash_list, stash_list
    show = subprocess.run(
        ["git", "-C", str(repo), "stash", "show", "--name-only", "stash@{0}"],
        capture_output=True,
        text=True,
    )
    assert show.returncode == 0 and "foreign/keep-me.txt" in show.stdout
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").startswith("lh-run/")
    assert _git(repo, "rev-parse", "HEAD") == _git(
        repo, "rev-parse", "refs/remotes/origin/main"
    )


# ---------------------------------------------------------------------------
# Row 6 -- symlink to a git checkout: the TARGET decides
# ---------------------------------------------------------------------------


def test_row_6_symlink_to_a_clean_checkout_is_classified_as_its_target(tmp_path: Path) -> None:
    """#205 row 6: the guard resolves through a symlink to a checkout.

    WG:296: ``(repo / ".git").exists()`` follows the symlink, so the guard
    classifies the TARGET's checkout state; a symlink to a clean checkout on
    the default branch is mode ``on-default`` with no refusal (pinned in the
    doc's evidence section).  The guard returns the path it was GIVEN -- the
    resolution to the target happens later, in the supervisor (SVC:2825).
    """

    repo = _make_repo(tmp_path, "row-6-target")
    link = tmp_path / "row-6-link"
    link.symlink_to(repo, target_is_directory=True)

    base = prepare_workspace_base(link, run_label="row-6", probe_open_pr=NO_PR)
    assert base.mode == "on-default"
    assert base.workspace == Path(link)  # the guard returns the given path
    assert base.original_branch == "main"
    assert require_git_checkout(base, link) is None


def test_row_6_symlink_to_a_dirty_checkout_inherits_row_5(tmp_path: Path) -> None:
    """#205 row 6: a symlink's cleanliness inherits the TARGET's row-4/5 state.

    The doc: "symlink cleanliness inherits the target's row-4/5 behaviour" --
    here the target is a dirty non-default checkout, so the link resolves to
    the row-5 worktree outcome, not to a refusal.
    """

    repo = _make_repo(tmp_path, "row-6b-target")
    _feature_branch(repo)
    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    _git(repo, "add", ".")
    link = tmp_path / "row-6b-link"
    link.symlink_to(repo, target_is_directory=True)

    base = prepare_workspace_base(link, run_label="row-6b", probe_open_pr=NO_PR)
    assert base.mode == "worktree"
    assert base.original_branch == "feat/other-task"
    assert require_git_checkout(base, link) is None


def test_row_6_outside_root_boundary_is_the_supervisors_not_the_guards(
    tmp_path: Path,
) -> None:
    """#205 row 6, outside-root half: the boundary is SVC:2825-2831, not the guard.

    The doc pins this with pure path semantics: ``Path.resolve`` on a
    root-inside symlink whose target is OUTSIDE the root fails ``relative_to``
    -- exactly the SVC:2828-2831 refusal ("workspace must be inside
    configured workspace root", HTTP 400 via SRV:3901-3905).  The guard
    itself has no root check at all (see row 8).
    """

    root = tmp_path / "configured-root"
    root.mkdir()
    outside_target = tmp_path / "elsewhere" / "target-ws"
    outside_target.mkdir(parents=True)
    link = root / "points-outside"
    link.symlink_to(outside_target, target_is_directory=True)

    resolved = link.resolve(strict=False)
    with pytest.raises(ValueError):
        resolved.relative_to(root)


# ---------------------------------------------------------------------------
# Row 7 -- symlink to a NON-git dir: REFUSE
# ---------------------------------------------------------------------------


def test_row_7_symlink_to_an_existing_non_git_dir_is_refused(tmp_path: Path) -> None:
    """#205 row 7: a symlink to an existing non-git dir is REFUSED.

    WG:296: ``repo/.git`` does not exist through the symlink, so the mode is
    ``not-a-repo``; WG:112's ``Path(...).is_dir()`` FOLLOWS the symlink, so
    the existing target counts as an existing directory (pinned in the doc).
    The occupancy probes read the non-repo target as clean (LCH:683-688), so
    they never fire first.
    """

    plain = tmp_path / "plain-target"
    plain.mkdir()
    (plain / "file.txt").write_text("not a repo\n", encoding="utf-8")
    link = tmp_path / "row-7-link"
    link.symlink_to(plain, target_is_directory=True)

    base = prepare_workspace_base(link, run_label="row-7", probe_open_pr=NO_PR)
    assert base.mode == "not-a-repo"
    refusal = require_git_checkout(base, link)
    # The refusal names the workspace as it will be resolved downstream: the
    # builder (WG:85) formats the path it was given, i.e. the LINK path here.
    assert refusal == _not_a_repo_refusal(link), refusal

    assert _dirty_workspace_occupied(str(link)) is False
    assert _unpushed_branch_occupied(str(link)) is False


# ---------------------------------------------------------------------------
# Row 8 -- path outside the configured work root
# ---------------------------------------------------------------------------


def test_row_8_guard_has_no_root_check_the_boundary_is_the_supervisors(
    tmp_path: Path,
) -> None:
    """#205 row 8: the GUARD accepts an outside-root path; SVC refuses it.

    The doc is explicit that row 8's direct-create refusal is NOT a guard
    refusal: workspace_guard.py contains no root check -- ``base_root`` only
    anchors RELATIVE paths (WG:287-289) and places run worktrees
    (WG:399-404).  Pinned here: the guard resolves an ABSOLUTE workspace that
    lies outside the given ``base_root`` without any root refusal, and the
    boundary semantics the supervisor applies (SVC:2828-2831, surfacing as
    HTTP 400 at SRV:3901-3905 on direct create and as a non-retryable FAILED
    entry at LCH:2248-2263 on queue launch) is the ``relative_to`` ValueError.
    """

    configured_root = tmp_path / "configured-work-root"
    configured_root.mkdir()
    repo = _make_repo(tmp_path / "elsewhere", "outside-ws")  # outside the root, clean on default

    base = prepare_workspace_base(
        repo, run_label="row-8", base_root=configured_root, probe_open_pr=NO_PR
    )
    assert base.mode == "on-default"  # no root refusal exists in the guard
    assert require_git_checkout(base, repo) is None

    resolved = repo.resolve(strict=False)
    with pytest.raises(ValueError):
        resolved.relative_to(configured_root.resolve(strict=False))


# ---------------------------------------------------------------------------
# Row 9 -- no tenant/owner notion (pinned as-is)
# ---------------------------------------------------------------------------

# The identifiers a workspace-to-caller binding would need.  Row 9 of the
# doc: "The code has NO tenant/owner notion for workspaces ... auth gates who
# may call, never which workspace."  This pin asserts the GUARD half of that
# statement structurally: no identity-shaped parameter, field or symbol
# exists anywhere in the guard's decision path.
_IDENTITY_SYMBOLS = frozenset(
    {
        "tenant",
        "caller",
        "owner",
        "requester",
        "principal",
        "actor",
        "subject",
        "account",
        "identity",
        "auth",
        "token",
        "secret",
        "credential",
    }
)

_GUARD_PUBLIC_API = (
    prepare_workspace_base,
    require_git_checkout,
    resolve_run_base,
)


def test_row_9_no_tenant_or_caller_binding_exists_in_the_guard(tmp_path: Path) -> None:
    """#205 row 9: the guard has NO tenant/owner/caller notion -- pinned as-is.

    The doc: no data model, config key or check binds a workspace to a
    caller, owner or tenant; the auth layer gates WHO may create runs or
    enqueue (SRV:2185-2199) but never WHICH workspace they may name, and the
    only same-workspace protections are process-level (the launch-time
    reservation SVC:2850-2913 and the active-run occupancy rule
    LCH:1816-1820).  Any caller that reaches the guard therefore gets the
    same decision for the same workspace.  Pinned as-is -- an identity
    decision on the hardening list, not a behaviour this file changes.
    """

    # 1. The guard's decision API takes no identity parameter of any shape:
    #    its inputs are the workspace, launch plumbing and the PR probe.
    for function in _GUARD_PUBLIC_API:
        parameters = set(inspect.signature(function).parameters)
        assert not parameters & _IDENTITY_SYMBOLS, (function.__name__, sorted(parameters))
    assert set(inspect.signature(WorkspaceBase).parameters) == {
        "workspace",
        "mode",
        "original_branch",
        "default_branch",
        "run_branch",
        "stashed",
    }

    # 2. The resolved base carries no identity field: a workspace binding
    #    would have to travel on this dataclass to reach the run record.
    assert {field.name for field in dataclasses.fields(WorkspaceBase)} == {
        "workspace",
        "mode",
        "original_branch",
        "default_branch",
        "run_branch",
        "stashed",
    }

    # 3. No identity-shaped symbol exists anywhere in the guard's source
    #    (AST, so comments about the calling code do not count).
    source = Path(workspace_guard.__file__).read_text(encoding="utf-8")
    symbols: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            symbols.add(node.id)
        elif isinstance(node, ast.Attribute):
            symbols.add(node.attr)
        elif isinstance(node, ast.arg):
            symbols.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.add(node.name)
    assert not symbols & _IDENTITY_SYMBOLS, sorted(symbols & _IDENTITY_SYMBOLS)

    # 4. Behaviourally: the decision is a function of the workspace alone --
    #    two invocations "by different callers" (there is no caller input to
    #    vary) resolve the same workspace identically.
    repo = _make_repo(tmp_path, "row-9-ws")
    first = prepare_workspace_base(repo, run_label="row-9", probe_open_pr=NO_PR)
    second = prepare_workspace_base(repo, run_label="row-9", probe_open_pr=NO_PR)
    assert first.summary() == second.summary()
    assert first.mode == second.mode == "on-default"


# ---------------------------------------------------------------------------
# Extra cell -- detached HEAD (#205 review comment 6905618162, missing cell 1)
# ---------------------------------------------------------------------------


def test_cell_detached_head_clean_checkout_is_rebranched_in_place(tmp_path: Path) -> None:
    """#205 review missing cell 1: a clean detached-HEAD checkout is ACCEPTED.

    WG:308 (``rev-parse --abbrev-ref HEAD``) yields ``HEAD``, which never
    equals the default branch (WG:343), so a clean detached checkout is
    re-branched in place by ``checkout -B`` off ``origin/<default>``
    (WG:383-393).  The reviewer asked for this as a D2 row; pinned as-is.
    """

    repo = _make_repo(tmp_path, "detached-ws")
    _git(repo, "checkout", "-q", "--detach")
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"

    base = prepare_workspace_base(repo, run_label="cell-detached", probe_open_pr=NO_PR)
    assert base.mode == "in-place"
    assert base.original_branch == "HEAD"
    assert base.workspace == repo
    assert base.run_branch is not None and base.run_branch.startswith("lh-run/")
    assert require_git_checkout(base, repo) is None

    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").startswith("lh-run/")
    assert _git(repo, "rev-parse", "HEAD") == _git(
        repo, "rev-parse", "refs/remotes/origin/main"
    )


def test_cell_detached_head_dirty_checkout_goes_to_a_worktree(tmp_path: Path) -> None:
    """Detached HEAD + dirty tree: the row-5 worktree outcome, as the review noted.

    "a dirty detached checkout goes to worktree/stash like row 5" -- the dirty
    check at WG:376 is what decides once the detached HEAD fails the
    default-branch comparison.
    """

    repo = _make_repo(tmp_path, "detached-dirty-ws")
    _git(repo, "checkout", "-q", "--detach")
    (repo / "uncommitted.txt").write_text("work in progress\n", encoding="utf-8")
    _git(repo, "add", ".")

    base = prepare_workspace_base(repo, run_label="cell-detached-dirty", probe_open_pr=NO_PR)
    assert base.mode == "worktree"
    assert base.original_branch == "HEAD"
    assert require_git_checkout(base, repo) is None
    # The detached tree is left exactly as found.
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert (repo / "uncommitted.txt").exists()


# ---------------------------------------------------------------------------
# Extra cell -- wrong remote (#205 review comment 6905618162, missing cell 2)
# ---------------------------------------------------------------------------


def test_cell_wrong_remote_checkout_is_accepted_on_default(tmp_path: Path) -> None:
    """#205 review missing cell 2: a checkout whose origin is ANOTHER repo is ACCEPTED.

    ``_detect_default_branch`` (WG:239-258) reads only ``origin`` refs and
    never checks the remote URL, so a clean checkout on the wrong origin's
    default branch is mode ``on-default`` and the run executes in place.
    Pinned as-is -- an identity decision on the hardening list: the guard
    cannot tell that this workspace belongs to a different repository than
    the deployment it serves, because it never looks at the URL.
    """

    # The repository this deployment "serves"...
    harness_origin = tmp_path / "harness-project.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(harness_origin)],
        check=True,
        capture_output=True,
    )
    # ...and a completely different project's repository, whose history the
    # workspace actually points at.
    other_origin = tmp_path / "other-project.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(other_origin)],
        check=True,
        capture_output=True,
    )

    repo = tmp_path / "wrong-remote-ws"
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True
    )
    _git(repo, "remote", "add", "origin", str(other_origin))
    (repo / "OTHER.md").write_text("a different project entirely\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "other project's initial commit")
    _git(repo, "push", "-q", "-u", "origin", "main")

    # Setup proof: the workspace's origin really is the OTHER repository.
    assert _git(repo, "remote", "get-url", "origin") == str(other_origin)

    base = prepare_workspace_base(repo, run_label="cell-wrong-remote", probe_open_pr=NO_PR)
    assert base.mode == "on-default"  # accepted, run executes in place
    assert base.workspace == repo
    assert base.default_branch == "main"
    assert base.original_branch == "main"
    assert require_git_checkout(base, repo) is None


# ---------------------------------------------------------------------------
# Extra cell -- dangling symlink (doc evidence section)
# ---------------------------------------------------------------------------


def test_cell_dangling_symlink_passes_the_guard_and_dies_at_mkdir(tmp_path: Path) -> None:
    """Dangling symlink: the guard passes it; the later ``mkdir`` is what dies.

    The doc's evidence section: a dangling symlink resolves to mode
    ``not-a-repo`` with refusal ``None`` (``(repo / ".git").exists()`` and
    ``Path(...).is_dir()`` are both False through a dangling link, so the
    "not created yet" escape at WG:112-114 applies) -- and this is the ONE
    state where that escape lets a path through that create_run then
    rejects: ``mkdir(parents=True, exist_ok=True)`` on a dangling symlink
    raises ``FileExistsError`` (SVC:2263), so no run row can be created.
    Pinned as-is, both sides of it.
    """

    link = tmp_path / "dangling-ws"
    link.symlink_to(tmp_path / "never-created", target_is_directory=True)
    assert link.is_dir() is False  # a dangling link is not an existing directory

    base = prepare_workspace_base(link, run_label="cell-dangling", probe_open_pr=NO_PR)
    assert base.mode == "not-a-repo"
    assert base.workspace == Path(link)
    assert require_git_checkout(base, link) is None  # the "not created yet" escape

    # resolve_run_base passes it through to the create path unchanged.
    resolved_base, effective = resolve_run_base(
        str(link), run_label="cell-dangling", probe_open_pr=NO_PR
    )
    assert resolved_base.mode == "not-a-repo"
    assert effective == str(link)

    # The downstream death the doc records: the ONLY materialization an
    # accepted missing-path run does (SVC:2263) cannot create through a
    # dangling symlink.
    with pytest.raises(FileExistsError):
        link.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# The enqueue column, for every row: string shape only, no filesystem check
# ---------------------------------------------------------------------------


def test_enqueue_column_validates_string_shape_only_for_every_row(
    tmp_path: Path,
) -> None:
    """#205 enqueue column, rows 1-9: NOTHING touches the filesystem at enqueue.

    Q:572-584 checks only string shape (non-empty, at most 4096 chars, no
    NUL byte), so EVERY row of the table is ACCEPTED at enqueue -- the
    deciding moment is deferred to the launch path.  Pinned directly against
    ``_validate_workspace`` with one string per row family, including the
    two strings that the other two paths refuse or fail (rows 2/3/7's
    non-checkout dir and row 8's outside-root path).
    """

    outside_root = tmp_path / "outside" / "any" / "root"
    for value in (
        str(tmp_path / "not-created-yet"),  # row 1: missing path
        str(tmp_path / "empty-ws"),  # row 2: existing empty non-git dir
        str(tmp_path / "scratch-ws"),  # row 3: existing non-empty non-git dir
        str(tmp_path / "clean-checkout"),  # row 4: clean checkout
        str(tmp_path / "dirty-checkout"),  # row 5: dirty checkout
        str(tmp_path / "symlink-to-checkout"),  # row 6: symlink to a checkout
        str(tmp_path / "symlink-to-plain"),  # row 7: symlink to a non-git dir
        str(outside_root),  # row 8: outside the configured root
        str(tmp_path / "another-tenants-ws"),  # row 9: any caller may name it
    ):
        assert _validate_workspace(value) == value

    # The only rejections are string-shape rejections -- never filesystem
    # verdicts, which is what makes the enqueue column ACCEPT for every row.
    with pytest.raises(ValueError):
        _validate_workspace(None)
    with pytest.raises(ValueError):
        _validate_workspace(123)
    with pytest.raises(ValueError):
        _validate_workspace("")
    with pytest.raises(ValueError):
        _validate_workspace("   ")
    with pytest.raises(ValueError):
        _validate_workspace("x" * 4097)
    with pytest.raises(ValueError):
        _validate_workspace("has\x00nul")
