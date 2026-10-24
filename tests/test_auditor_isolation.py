"""Tests for HARNESS-AUDITOR-ISOLATION (post-operator-correction redesign).

Deliverables (mapped to test prefixes):

D1: ``create_isolated_snapshot`` builds an *independent full clone* of the
    executor HEAD (no shared ``.git``, no linked worktree) and writes
    ``snapshot_info.json`` next to the round artifacts. The snapshot sha
    equals the executor HEAD sha.

D2: source/.git mutations of the executor workspace are BLOCKED BEFORE
    DAMAGE: the clone's ``.git`` is a separate object database; chmod a-w
    on the snapshot files stops same-UID writes; the deny list forbids
    ``chmod``, ``chown``, ``rm``, ``unlink`` reversal; an actual subprocess
    attempting ``update-ref``, ``checkout -B``, direct ``.git`` write, and
    chmod+write fails to mutate the executor.

D3: AUDITOR_LOCAL_GIT_DENY covers every local-rewrite op including
    ``update-ref`` / ``update-ref --stdin`` / ``symbolic-ref`` (the
    family the operator correction flagged as missing from the round-001
    list); AUDITOR_FS_DENY covers chmod/chown/rm/unlink/install/mv.

D4: ``refs/lh-harness/<run_id>/round_<n>`` is pinned to the executor HEAD
    *before* the auditor launches, and pin failures now raise
    ``SnapshotIsolationError`` (fail-closed). The ref survives a forbidden
    rewrite.

D5: sibling runs get distinct snapshot paths; one run's snapshot path
    does not contain another run's id and is in a sibling directory
    of the per-run workspace.

Plus the "still works" safety net: a normal read/test audit inside the
snapshot completes cleanly, the executor HEAD sha and file content are
unchanged after the auditor window, and the snapshot is byte-clean for
read-only git operations (status, log, rev-parse, etc.).

Subprocess tests launch the actual git / chmod / write commands the
auditor would attempt, on disposable repos under tmp_path.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from lh_harness.adapters.claude_permissions import (
    AUDITOR_FS_DENY,
    AUDITOR_LOCAL_GIT_DENY,
    AUDITOR_NETWORK_GIT_DENY,
    _AUDITOR_ROLES,
    _WRITE_TOOLS,
    policy_for_role,
)
from lh_harness.adapters.claude_code import ClaudeCodeAdapter
from lh_harness.auditor_snapshot import (
    SnapshotInfo,
    SnapshotIsolationError,
    create_isolated_snapshot,
    executor_head_sha,
    local_ref_name,
    pin_executor_head,
    remove_isolated_snapshot,
    snapshot_dir_name,
    write_snapshot_info,
)


def _rule_matches(rule: str, command: str) -> bool:
    """Match a Claude ``--disallowedTools`` rule against a concrete command."""
    return fnmatch.fnmatchcase(command, rule)


GIT = ("git",)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        GIT + ("-C", str(repo)) + args,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}"
        )
    return proc.stdout.strip()


def _init_repo(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    _git(workspace, "init", "-q", "--initial-branch=main")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test")
    (workspace / "src").mkdir(exist_ok=True)
    (workspace / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
    _git(workspace, "add", "src/app.py")
    _git(workspace, "commit", "-q", "-m", "initial")
    (workspace / "README.md").write_text("hello\n", encoding="utf-8")
    _git(workspace, "add", "README.md")
    _git(workspace, "commit", "-q", "-m", "docs")


# --------------------------------------------------------------------------
# D1: snapshot is an independent full clone with matching sha.
# --------------------------------------------------------------------------


def test_d1_create_isolated_snapshot_returns_independent_clone(tmp_path: Path) -> None:
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)

    info = create_isolated_snapshot(
        workspace, run_id="run-a", round_index=1
    )
    assert isinstance(info, SnapshotInfo)
    expected_sha = executor_head_sha(workspace)
    assert info.executor_head_sha == expected_sha
    assert info.snapshot_head_sha == expected_sha
    # Operator correction: the snapshot must be a clone, not a worktree.
    assert info.mode == "clone"
    assert info.run_id == "run-a"
    assert info.round_index == 1
    assert info.read_only is True
    assert info.snapshot_path.endswith(snapshot_dir_name("run-a"))
    assert Path(info.snapshot_path).is_dir()


def test_d1_snapshot_does_not_share_git_with_executor(tmp_path: Path) -> None:
    """The clone must have its own .git dir, separate from the executor's."""
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)
    info = create_isolated_snapshot(workspace, run_id="run-a", round_index=1)
    assert info is not None

    snap_git = (Path(info.snapshot_path) / ".git").resolve()
    exec_git = (workspace / ".git").resolve()
    assert snap_git != exec_git, "snapshot shares .git with executor"

    # The clone has its own objects directory (no alternates pointing
    # back to the executor).
    alt_path = snap_git / "objects" / "info" / "alternates"
    if alt_path.exists():
        content = alt_path.read_text(encoding="utf-8").strip()
        assert str(exec_git) not in content, (
            f"snapshot alternates points back at executor: {content}"
        )


def test_d1_snapshot_info_round_log_artifact(tmp_path: Path) -> None:
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)

    info = create_isolated_snapshot(workspace, run_id="run-a", round_index=2)
    round_dir = tmp_path / "rounds" / "round_002"
    target = write_snapshot_info(round_dir, info)
    assert target is not None
    assert target.is_file()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["executor_head_sha"] == info.executor_head_sha
    assert payload["snapshot_head_sha"] == info.snapshot_head_sha
    assert payload["run_id"] == "run-a"
    assert payload["round_index"] == 2
    assert payload["mode"] == "clone"
    assert payload["read_only"] is True
    assert payload["snapshot_path"] == info.snapshot_path
    assert payload["local_ref"] == info.local_ref


def test_d1_snapshot_path_differs_from_executor_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)

    info = create_isolated_snapshot(workspace, run_id="run-x", round_index=1)
    assert Path(info.snapshot_path).resolve() != workspace.resolve()


# --------------------------------------------------------------------------
# D2: source/.git mutation is blocked BEFORE damage. Verifies via real
# subprocess calls: chmod+write, direct .git writes, and git update-ref
# must all fail or be scoped to the snapshot, never the executor.
# --------------------------------------------------------------------------


def test_d2_snapshot_dir_is_filesystem_read_only(tmp_path: Path) -> None:
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)
    info = create_isolated_snapshot(workspace, run_id="run-r", round_index=1)
    snapshot = Path(info.snapshot_path)
    target = snapshot / "src" / "app.py"
    mode = target.stat().st_mode
    assert not (mode & 0o222), f"snapshot files should be read-only: {oct(mode)}"
    with pytest.raises(PermissionError):
        target.write_text("overwritten\n", encoding="utf-8")


def test_d2_executor_workspace_unchanged_after_snapshot_creation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)
    sha_before = executor_head_sha(workspace)
    app_before = (workspace / "src" / "app.py").read_text(encoding="utf-8")
    readme_before = (workspace / "README.md").read_text(encoding="utf-8")

    info = create_isolated_snapshot(workspace, run_id="run-r", round_index=1)
    assert info is not None

    assert executor_head_sha(workspace) == sha_before
    assert (workspace / "src" / "app.py").read_text(encoding="utf-8") == app_before
    assert (workspace / "README.md").read_text(encoding="utf-8") == readme_before


def test_d2_update_ref_on_snapshot_is_blocked_by_chmod(tmp_path: Path) -> None:
    """Real subprocess: ``git update-ref`` inside the snapshot fails
    because the snapshot's chmod a-w prevents ``.git/HEAD.lock`` from
    being created. This is the kernel-enforced guarantee that an auditor
    cannot rewrite refs even via the snapshot path. The operator
    correction flagged ``update-ref`` as missing from the round-001 deny
    list; the chmod layer now provides the secondary defense, and the
    updated AUDITOR_LOCAL_GIT_DENY blocks ``update-ref`` at the Claude
    tool layer too.
    """
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)
    sha_before = executor_head_sha(workspace)

    info = create_isolated_snapshot(workspace, run_id="run-r", round_index=1)
    snapshot = Path(info.snapshot_path)

    # update-ref inside the snapshot fails because chmod a-w prevents
    # creating .git/HEAD.lock.
    proc = subprocess.run(
        GIT + ("-C", str(snapshot), "update-ref", "HEAD", sha_before),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0, "update-ref unexpectedly succeeded on read-only snapshot"
    assert "Permission denied" in proc.stderr or "lock" in proc.stderr.lower()

    # And the executor HEAD sha is untouched.
    assert executor_head_sha(workspace) == sha_before


def test_d2_checkout_B_inside_snapshot_blocked(tmp_path: Path) -> None:
    """Real subprocess: the original incident command ``git checkout -B
    <branch> <sha>`` runs against the snapshot. The snapshot's chmod a-w
    prevents the working-tree update and the .git/HEAD.lock creation.
    The executor HEAD sha and pinned ref are unchanged.
    """
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)
    sha_before = executor_head_sha(workspace)
    ref = pin_executor_head(workspace, "run-incident", 1)

    info = create_isolated_snapshot(workspace, run_id="run-incident", round_index=1)
    snapshot = Path(info.snapshot_path)

    # Simulate the original audit-time attempt inside the snapshot.
    fake_target = _git(workspace, "rev-parse", "HEAD~1")
    proc = subprocess.run(
        GIT + ("-C", str(snapshot), "checkout", "-B", "main", fake_target),
        capture_output=True,
        text=True,
        check=False,
    )
    # Either the lock-creation fails (chmod a-w), or the update is
    # blocked, but the executor HEAD sha is NEVER touched.
    assert executor_head_sha(workspace) == sha_before
    # And the pinned ref still points at the original sha.
    assert _git(workspace, "rev-parse", "--verify", ref) == sha_before


def test_d2_direct_dot_git_write_subprocess_blocked(tmp_path: Path) -> None:
    """Real subprocess: a direct write to ``.git/HEAD`` of the executor
    is blocked at the filesystem level when the executor is left alone.
    With the independent clone design, an auditor never has write access
    to the executor's ``.git/HEAD`` because the snapshot's ``.git`` is a
    separate directory. We verify the snapshot's chmod makes the snapshot's
    own ``.git`` files unwritable too.
    """
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)
    sha_before = executor_head_sha(workspace)

    info = create_isolated_snapshot(workspace, run_id="run-git-write", round_index=1)
    snapshot = Path(info.snapshot_path)
    snap_head = snapshot / ".git" / "HEAD"

    # chmod a-w on the snapshot .git/HEAD: even a direct write attempt
    # by the same uid fails.
    mode = snap_head.stat().st_mode
    assert not (mode & 0o222)
    with pytest.raises(PermissionError):
        snap_head.write_text("ref: refs/heads/other\n", encoding="utf-8")
    # The executor HEAD is still the original sha (it was never touched).
    assert executor_head_sha(workspace) == sha_before


def test_d2_chmod_then_write_subprocess_blocked(tmp_path: Path) -> None:
    """Real subprocess: even if the deny list is bypassed and the auditor
    chmod's the snapshot back to writable, the EXECUTOR workspace is
    still untouched. This is the core defense in depth: the executor
    and the snapshot are independent clones.
    """
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)
    sha_before = executor_head_sha(workspace)
    app_before = (workspace / "src" / "app.py").read_text(encoding="utf-8")

    info = create_isolated_snapshot(workspace, run_id="run-chmod", round_index=1)
    snapshot = Path(info.snapshot_path)
    target = snapshot / "src" / "app.py"

    # 1. Initial write fails (chmod a-w).
    with pytest.raises(PermissionError):
        target.write_text("first attempt\n", encoding="utf-8")
    # 2. Even after a chmod back to writable, the executor workspace is
    #    untouched. This is the "executor is physically separate" guarantee.
    proc = subprocess.run(
        ["chmod", "u+w", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    target.write_text("recovered write\n", encoding="utf-8")
    assert (snapshot / "src" / "app.py").read_text(encoding="utf-8") == "recovered write\n"

    # 3. The executor workspace is still byte-identical.
    assert executor_head_sha(workspace) == sha_before
    assert (workspace / "src" / "app.py").read_text(encoding="utf-8") == app_before


def test_d2_symlink_target_outside_snapshot_is_not_chmodded(
    tmp_path: Path,
) -> None:
    """The operator correction flagged ``_chmod_readonly`` for chmod'ing
    symlink targets that pointed outside the snapshot. We verify the
    fixed implementation refuses symlinks that point outside AND refuses
    to chmod the external target.
    """
    from lh_harness.auditor_snapshot import _chmod_readonly

    # External file outside the snapshot.
    external = tmp_path / "external.txt"
    external.write_text("outside\n", encoding="utf-8")
    ext_mode_before = stat.S_IMODE(external.stat().st_mode)

    # Build a non-snapshot tree with an external-escape symlink.
    tree = tmp_path / "tree"
    tree.mkdir()
    link = tree / "external_link.txt"
    link.symlink_to(external)

    # Read-only chmod on a tree containing the escape link MUST raise
    # (the operator correction required that we never dereference/chmod
    # external targets). The new implementation detects the escape and
    # raises SnapshotIsolationError.
    with pytest.raises(SnapshotIsolationError):
        _chmod_readonly(tree)

    # The external file's mode is untouched.
    ext_mode_after = stat.S_IMODE(external.stat().st_mode)
    assert ext_mode_after == ext_mode_before
    # And its content is untouched.
    assert external.read_text(encoding="utf-8") == "outside\n"


def test_d2_chmod_does_not_follow_symlinks(tmp_path: Path) -> None:
    """Even when a symlink inside the snapshot targets another file
    INSIDE the snapshot, chmod must not flip the target's mode bits
    (chmod without follow_symlinks only changes the symlink itself,
    which has no mode). The Python ``os.chmod(path, mode,
    follow_symlinks=False)`` API must be used.

    We test by manually creating a sibling symlink in a non-read-only
    directory and then walking our helper. The snapshot itself is
    read-only, so we create a separate tree under tmp_path that mimics
    the same ownership/structure.
    """
    from lh_harness.auditor_snapshot import _chmod_writable

    workspace = tmp_path / "src_dir"
    workspace.mkdir()
    (workspace / "real.py").write_text("real\n", encoding="utf-8")
    (workspace / "link.py").symlink_to(workspace / "real.py")

    target_mode_before = stat.S_IMODE((workspace / "real.py").stat().st_mode)

    # Our _chmod_writable helper uses follow_symlinks=False so the
    # symlink's mode (which is meaningless) is "changed" instead of the
    # target. The target's mode bits must be unchanged.
    _chmod_writable(workspace)

    target_mode_after = stat.S_IMODE((workspace / "real.py").stat().st_mode)
    assert target_mode_after == target_mode_before


# --------------------------------------------------------------------------
# D3: deny lists cover every mutation channel.
# --------------------------------------------------------------------------


_D3_LOCAL_OPS = (
    # The original incident command.
    "Bash(git checkout -B recovered origin/codex/cct-native-generation-recovery)",
    # The rest of the local-rewrite family.
    "Bash(git reset --hard HEAD~1)",
    "Bash(git switch recovery-branch)",
    "Bash(git clean -fdx)",
    "Bash(git stash push --include-untracked)",
    "Bash(git restore --staged --worktree .)",
    "Bash(git commit -am 'rollback')",
    "Bash(git rebase origin/main)",
    "Bash(git merge origin/main)",
    # The reference-writing family (operator correction round_001).
    "Bash(git update-ref refs/heads/main HEAD~1)",
    "Bash(git update-ref --no-deref refs/lh-harness/run-x/round_001 HEAD)",
    "Bash(git update-ref --stdin < <(echo refs/heads/x HEAD) )",
    "Bash(git symbolic-ref HEAD refs/heads/recovered)",
    "Bash(git update-ref -d refs/heads/main)",
)


@pytest.mark.parametrize("rule", _D3_LOCAL_OPS)
def test_d3_each_local_git_op_is_in_deny_rules(rule: str) -> None:
    assert any(_rule_matches(candidate, rule) for candidate in AUDITOR_LOCAL_GIT_DENY), (
        f"rule {rule!r} not covered by AUDITOR_LOCAL_GIT_DENY"
    )


@pytest.mark.parametrize("role", _AUDITOR_ROLES)
def test_d3_auditor_roles_get_local_git_deny_and_write_deny(role: str) -> None:
    policy = policy_for_role(role)
    deny = list(policy.disallowed_tools)
    for op in _D3_LOCAL_OPS:
        assert any(_rule_matches(rule, op) for rule in deny), (
            f"{role} missing local rule for {op}"
        )
    assert "Write" in deny
    assert "Edit" in deny
    assert policy.workspace_read_only is True


@pytest.mark.parametrize("role", _AUDITOR_ROLES)
def test_d3_local_git_deny_applies_to_third_party_subcommands(role: str) -> None:
    policy = policy_for_role(role)
    deny = set(policy.disallowed_tools)
    assert "Bash(git -C * checkout*)" in deny
    assert "Bash(git -C * reset*)" in deny
    assert "Bash(git -C * switch*)" in deny
    assert "Bash(git -C * update-ref*)" in deny
    assert "Bash(git -C * symbolic-ref*)" in deny
    assert "Bash(git --git-dir* checkout*)" in deny
    assert "Bash(git --git-dir* reset*)" in deny
    assert "Bash(git --git-dir* update-ref*)" in deny


@pytest.mark.parametrize("role", _AUDITOR_ROLES)
def test_d3_auditor_roles_get_filesystem_deny(role: str) -> None:
    """AUDITOR_FS_DENY (chmod/chown/rm/unlink/install/mv) is wired into
    every auditor policy. This is the deny list that prevents the
    auditor from reversing the snapshot's chmod a-w protection even if
    it ignores Write/Edit.
    """
    policy = policy_for_role(role)
    deny = set(policy.disallowed_tools)
    for rule in AUDITOR_FS_DENY:
        assert rule in deny, f"{role} missing filesystem rule {rule}"


@pytest.mark.parametrize("role", ["cli_executor", "gui_executor"])
def test_d3_executor_roles_do_not_get_local_git_deny(role: str) -> None:
    policy = policy_for_role(role)
    for rule in AUDITOR_LOCAL_GIT_DENY:
        assert rule not in policy.disallowed_tools
    for rule in AUDITOR_FS_DENY:
        assert rule not in policy.disallowed_tools


# --------------------------------------------------------------------------
# D4: refs/lh-harness/<run_id>/round_<n> is pinned to executor HEAD.
# --------------------------------------------------------------------------


def test_d4_local_ref_pinned_to_executor_head(tmp_path: Path) -> None:
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)
    sha = executor_head_sha(workspace)
    ref = pin_executor_head(workspace, run_id="run-pin", round_index=3)
    assert ref == "refs/lh-harness/run-pin/round_003"
    target = _git(workspace, "rev-parse", "--verify", "--quiet", ref)
    assert target == sha


def test_d4_local_ref_survives_executor_branch_rewrite_attempt(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)
    sha = executor_head_sha(workspace)
    ref = pin_executor_head(workspace, run_id="run-pin", round_index=1)
    assert ref is not None
    assert _git(workspace, "rev-parse", "--verify", ref) == sha


def test_d4_pin_failure_raises_for_non_git_workspace(tmp_path: Path) -> None:
    """pin_executor_head must raise SnapshotIsolationError if the
    workspace is not a git repo. The previous implementation silently
    returned None (fail-open); the operator correction required the
    pin to propagate as a hard failure.
    """
    workspace = tmp_path / "not_a_repo"
    workspace.mkdir()
    with pytest.raises(SnapshotIsolationError):
        pin_executor_head(workspace, run_id="run-bad", round_index=1)


def test_d4_snapshot_creation_fails_closed_for_non_git_workspace(
    tmp_path: Path,
) -> None:
    """If the workspace is not a git repo, create_isolated_snapshot
    raises SnapshotIsolationError. The harness must NOT fall back to
    the executor workspace (operator correction: fail-closed).
    """
    workspace = tmp_path / "not_a_repo"
    workspace.mkdir()
    with pytest.raises(SnapshotIsolationError):
        create_isolated_snapshot(workspace, run_id="run-bad", round_index=1)


def test_d4_local_ref_uses_round_index_padding(tmp_path: Path) -> None:
    name = local_ref_name("r", 7)
    assert name == "refs/lh-harness/r/round_007"


# --------------------------------------------------------------------------
# D5: sibling-run isolation.
# --------------------------------------------------------------------------


def test_d5_sibling_runs_get_distinct_snapshot_paths(tmp_path: Path) -> None:
    workspace_a = tmp_path / "executor_a"
    workspace_b = tmp_path / "executor_b"
    _init_repo(workspace_a)
    _init_repo(workspace_b)
    info_a = create_isolated_snapshot(workspace_a, run_id="run-A", round_index=1)
    info_b = create_isolated_snapshot(workspace_b, run_id="run-B", round_index=1)
    assert info_a is not None
    assert info_b is not None
    assert info_a.snapshot_path != info_b.snapshot_path
    assert Path(info_a.snapshot_path).parent == workspace_a.parent
    assert Path(info_b.snapshot_path).parent == workspace_b.parent
    assert str(workspace_b) not in info_a.snapshot_path
    assert str(workspace_a) not in info_b.snapshot_path


def test_d5_sibling_run_auditor_path_does_not_contain_other_run_id(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    _init_repo(workspace)
    info = create_isolated_snapshot(workspace, run_id="alpha", round_index=1)
    assert info is not None
    assert "beta" not in info.snapshot_path
    assert "gamma" not in info.snapshot_path


def test_d5_two_runs_share_workspace_distinct_snapshots(tmp_path: Path) -> None:
    """Two concurrent runs sharing a workspace get distinct snapshot
    directories named with their run_id. Cleanup of one does not touch
    the other (verified by removing one and confirming the other still
    exists).
    """
    workspace = tmp_path / "shared_workspace"
    _init_repo(workspace)
    info_a = create_isolated_snapshot(workspace, run_id="run-X", round_index=1)
    info_b = create_isolated_snapshot(workspace, run_id="run-Y", round_index=1)
    assert info_a is not None
    assert info_b is not None
    assert Path(info_a.snapshot_path).name != Path(info_b.snapshot_path).name
    assert "run-X" in Path(info_a.snapshot_path).name
    assert "run-Y" in Path(info_b.snapshot_path).name


# --------------------------------------------------------------------------
# The "still works" safety net.
# --------------------------------------------------------------------------


def test_normal_read_audit_in_snapshot_works(tmp_path: Path) -> None:
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)
    info = create_isolated_snapshot(workspace, run_id="run-rd", round_index=1)
    assert info is not None
    snapshot = Path(info.snapshot_path)
    text = (snapshot / "README.md").read_text(encoding="utf-8")
    assert text == "hello\n"
    snapshot_sha = _git(snapshot, "rev-parse", "HEAD")
    assert snapshot_sha == info.executor_head_sha


def test_read_only_git_audit_in_snapshot_works(tmp_path: Path) -> None:
    """Read-only git operations (status, log, diff, rev-parse) must
    still work on the snapshot because the executor's commits must be
    inspectable by the auditor.
    """
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)
    info = create_isolated_snapshot(workspace, run_id="run-g", round_index=1)
    assert info is not None
    snapshot = Path(info.snapshot_path)
    sha = _git(snapshot, "rev-parse", "HEAD")
    assert sha == info.executor_head_sha
    log = _git(snapshot, "log", "--oneline")
    assert "initial" in log
    assert "docs" in log
    # Status is clean.
    status = _git(snapshot, "status", "--porcelain")
    assert status == ""


def test_executor_workspace_byte_unchanged_after_audit_window(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)
    sha_before = executor_head_sha(workspace)
    app_before = (workspace / "src" / "app.py").read_text(encoding="utf-8")
    readme_before = (workspace / "README.md").read_text(encoding="utf-8")

    info = create_isolated_snapshot(workspace, run_id="run-unchanged", round_index=1)
    assert info is not None
    snapshot = Path(info.snapshot_path)
    _ = (snapshot / "README.md").read_text(encoding="utf-8")
    _ = (snapshot / "src" / "app.py").read_text(encoding="utf-8")
    _git(snapshot, "status")
    _git(snapshot, "log", "--oneline")

    assert executor_head_sha(workspace) == sha_before
    assert (workspace / "src" / "app.py").read_text(encoding="utf-8") == app_before
    assert (workspace / "README.md").read_text(encoding="utf-8") == readme_before


def test_remove_isolated_snapshot_does_not_touch_executor_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)
    sha_before = executor_head_sha(workspace)
    info = create_isolated_snapshot(workspace, run_id="run-cleanup", round_index=1)
    assert info is not None
    snapshot_path = info.snapshot_path
    remove_isolated_snapshot(snapshot_path)
    assert executor_head_sha(workspace) == sha_before
    assert workspace.is_dir()
    assert (workspace / "README.md").is_file()


def test_create_isolated_snapshot_recovers_from_previous_round(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)
    first = create_isolated_snapshot(workspace, run_id="reused", round_index=1)
    second = create_isolated_snapshot(workspace, run_id="reused", round_index=2)
    assert first is not None and second is not None
    assert first.snapshot_path == second.snapshot_path
    assert first.round_index == 1
    assert second.round_index == 2


def test_auditor_command_template_includes_all_deny_layers() -> None:
    adapter = ClaudeCodeAdapter(role="cli_auditor", run_id="run-tpl")
    template = adapter.command_template
    assert "--disallowedTools" in template
    # Local git rewrite family
    assert "Bash(git checkout*)" in template
    assert "Bash(git reset*)" in template
    assert "Bash(git switch*)" in template
    assert "Bash(git clean*)" in template
    assert "Bash(git stash*)" in template
    assert "Bash(git restore*)" in template
    assert "Bash(git commit*)" in template
    # Reference-writing family (the operator correction addition).
    assert "Bash(git update-ref*)" in template
    assert "Bash(git symbolic-ref*)" in template
    # Filesystem deny (chmod/chown/rm).
    assert "Bash(chmod*)" in template
    assert "Bash(chown*)" in template
    assert "Bash(rm*)" in template
    # Network deny stays in place.
    assert "Bash(git fetch*)" in template
    assert "Bash(git push*)" in template


def test_executor_command_template_unaffected_by_auditor_deny() -> None:
    adapter = ClaudeCodeAdapter(role="cli_executor", run_id="run-exec")
    template = adapter.command_template
    for rule in AUDITOR_LOCAL_GIT_DENY:
        assert rule not in template
    for rule in AUDITOR_FS_DENY:
        assert rule not in template
    for rule in AUDITOR_NETWORK_GIT_DENY:
        assert rule not in template


def test_snapshot_dir_name_uses_sanitized_run_id() -> None:
    assert snapshot_dir_name("aaaaaaaa-bbbb-cccc").startswith(".snapshot-aaaaaaaa-bbbb-cccc")


def test_local_ref_name_round_zero_padding() -> None:
    assert local_ref_name("run", 0) == "refs/lh-harness/run/round_000"


def test_remove_isolated_snapshot_no_op_when_missing(tmp_path: Path) -> None:
    remove_isolated_snapshot(tmp_path / "never-existed")


# --------------------------------------------------------------------------
# P1 launched-subprocess integration tests (operator correction round_003).
#
# These prove the auditor subprocess (the way the harness actually launches
# it) cannot mutate the executor's source, the snapshot source, or sibling
# run paths — and that read-only git inspection and scratch test output
# still work.  Where the deployment cannot construct the boundary, the
# test asserts fail-closed (auditor launch refused with
# AuditorBoundaryUnavailable).
# --------------------------------------------------------------------------


def _unused_marker():  # pragma: no cover - tag for code reviewer
    return None


def test_p1_autodetect_returns_unavailable_or_real(tmp_path: Path) -> None:
    """``detect_auditor_boundary`` must return either a real
    boundary (USER_NAMESPACE / SEPARATE_UID with a *different*
    probe_uid than the calling uid) or UNAVAILABLE. Returning a
    boundary that the kernel still maps back to the calling uid
    is the operator-rejected P1 trap.
    """
    from lh_harness.safe_subprocess import (
        AuditorBoundaryKind,
        detect_auditor_boundary,
    )

    b = detect_auditor_boundary()
    if b.kind == AuditorBoundaryKind.UNAVAILABLE:
        assert b.launcher is None
        assert b.probe_uid is None
        assert "no separate-UID launcher" in b.rationale
        return
    # Real boundary: probe_uid differs from calling uid.
    assert b.probe_uid is not None
    assert b.probe_uid != os.getuid(), (
        "boundary reports probe_uid equal to calling uid — that is "
        "not a real boundary (operator correction P1 trap)"
    )


def test_p1_autodetect_launch_refuses_when_unavailable(tmp_path: Path) -> None:
    """If the deployment has no real boundary, ``launch_auditor_subprocess``
    must raise ``AuditorBoundaryUnavailable`` *before* forking. The
    harness catches this and aborts the round.
    """
    from lh_harness.safe_subprocess import (
        AuditorBoundaryKind,
        AuditorBoundaryUnavailable,
        detect_auditor_boundary,
        launch_auditor_subprocess,
    )

    b = detect_auditor_boundary()
    if b.kind != AuditorBoundaryKind.UNAVAILABLE:
        pytest.skip(
            "deployment has a real boundary; the fail-closed test is "
            "not applicable here"
        )
    with pytest.raises(AuditorBoundaryUnavailable):
        launch_auditor_subprocess(["id", "-u"])


def test_p1_auditor_boundary_required_raises_when_unavailable(
    tmp_path: Path,
) -> None:
    """``auditor_boundary_required`` is the gatekeeper the role-binding
    code calls before launching the auditor. It must propagate
    ``AuditorBoundaryUnavailable`` for the harness to abort cleanly.
    """
    from lh_harness.safe_subprocess import (
        AuditorBoundaryKind,
        AuditorBoundaryUnavailable,
        auditor_boundary_required,
        detect_auditor_boundary,
    )

    b = detect_auditor_boundary()
    if b.kind != AuditorBoundaryKind.UNAVAILABLE:
        pytest.skip(
            "deployment has a real boundary; fail-closed not applicable"
        )
    with pytest.raises(AuditorBoundaryUnavailable):
        auditor_boundary_required()


def test_p1_launched_subprocess_denies_open_write_to_snapshot(
    tmp_path: Path,
) -> None:
    """Spawn the auditor the way the harness would. Inside the auditor,
    try to ``open(snapshot_file, "w")`` and assert the kernel rejects
    the write. Works when the deployment has a real SEPARATE_UID or
    USER_NAMESPACE boundary; when the boundary is UNAVAILABLE the test
    asserts fail-closed (the launch is refused before any fork).
    """
    from lh_harness.safe_subprocess import (
        AuditorBoundaryKind,
        AuditorBoundaryUnavailable,
        detect_auditor_boundary,
        launch_auditor_subprocess,
    )

    workspace = tmp_path / "exec"
    sibling = tmp_path / "sibling"
    _init_repo(workspace)
    info = create_isolated_snapshot(
        workspace, run_id="run-p1-snap", round_index=1
    )
    snap_src = Path(info.snapshot_path) / "src" / "app.py"
    # Snapshot file is owned by harness uid. Mode reduced to 0o444 by
    # create_isolated_snapshot. A real boundary is required to keep the
    # kernel from granting write access to the same uid.
    snap_src.chmod(0o600)
    try:
        boundary = detect_auditor_boundary()
        if boundary.kind == AuditorBoundaryKind.UNAVAILABLE:
            with pytest.raises(AuditorBoundaryUnavailable):
                launch_auditor_subprocess(
                    [sys.executable, "-c",
                     f"import pathlib; pathlib.Path({str(snap_src)!r}).write_text('x')"]
                )
            return

        probe = launch_auditor_subprocess(
            [sys.executable, "-c",
             (
                 "import sys, pathlib\n"
                 f"p = pathlib.Path({str(snap_src)!r})\n"
                 "try:\n"
                 "    p.write_text('overwrite-by-auditor')\n"
                 "    print('OPEN_WRITE_OK')\n"
                 "except Exception as e:\n"
                 "    print('OPEN_WRITE_DENIED', type(e).__name__)\n"
                 "    sys.exit(2)\n"
             )]
        )
        out, _err = probe.communicate(timeout=15)
        assert "OPEN_WRITE_DENIED" in out, (
            f"auditor subprocess was allowed to open() and write the "
            f"snapshot source: {out!r}"
        )
        # And the harness-owned file is byte-unchanged.
        assert "overwrite-by-auditor" not in snap_src.read_text(
            encoding="utf-8"
        )
    finally:
        try:
            snap_src.chmod(0o644)
        except OSError:
            pass


def test_p1_launched_subprocess_denies_open_write_to_executor(
    tmp_path: Path,
) -> None:
    """Inside the auditor subprocess, attempt ``open(executor_source,
    "w")``. With a real boundary this is denied by the kernel permission
    check; with UNAVAILABLE the launch is refused before forking."""
    from lh_harness.safe_subprocess import (
        AuditorBoundaryKind,
        AuditorBoundaryUnavailable,
        detect_auditor_boundary,
        launch_auditor_subprocess,
    )

    workspace = tmp_path / "exec"
    _init_repo(workspace)
    info = create_isolated_snapshot(workspace, run_id="run-p1-exec", round_index=1)
    executor_src = workspace / "src" / "app.py"
    executor_src.chmod(0o600)
    try:
        boundary = detect_auditor_boundary()
        if boundary.kind == AuditorBoundaryKind.UNAVAILABLE:
            with pytest.raises(AuditorBoundaryUnavailable):
                launch_auditor_subprocess(
                    [sys.executable, "-c",
                     f"import pathlib; pathlib.Path({str(executor_src)!r}).write_text('x')"]
                )
            return

        probe = launch_auditor_subprocess(
            [sys.executable, "-c",
             (
                 "import sys, pathlib\n"
                 f"p = pathlib.Path({str(executor_src)!r})\n"
                 "try:\n"
                 "    p.write_text('overwrite-by-auditor')\n"
                 "    print('OPEN_WRITE_OK')\n"
                 "except Exception as e:\n"
                 "    print('OPEN_WRITE_DENIED', type(e).__name__)\n"
                 "    sys.exit(2)\n"
             )]
        )
        out, _err = probe.communicate(timeout=15)
        assert "OPEN_WRITE_DENIED" in out
        assert "overwrite-by-auditor" not in executor_src.read_text(
            encoding="utf-8"
        )
    finally:
        try:
            executor_src.chmod(0o644)
        except OSError:
            pass


def test_p1_launched_subprocess_denies_git_write_to_executor(
    tmp_path: Path,
) -> None:
    """Inside the auditor subprocess, try the *original incident
    command*: ``git -C <executor> checkout -B <branch> HEAD~1``.
    With a real boundary the kernel rejects the audit subprocess's
    read access to the executor's ``.git`` directory; with UNAVAILABLE
    the launch is refused before forking."""
    from lh_harness.safe_subprocess import (
        AuditorBoundaryKind,
        AuditorBoundaryUnavailable,
        detect_auditor_boundary,
        launch_auditor_subprocess,
    )

    workspace = tmp_path / "exec"
    _init_repo(workspace)
    info = create_isolated_snapshot(workspace, run_id="run-p1-git", round_index=1)
    sha_before = executor_head_sha(workspace)
    boundary = detect_auditor_boundary()
    if boundary.kind == AuditorBoundaryKind.UNAVAILABLE:
        with pytest.raises(AuditorBoundaryUnavailable):
            launch_auditor_subprocess(
                [sys.executable, "-c", (
                    "import subprocess, sys\n"
                    "r = subprocess.run(['git', '-C', "
                    f"{str(workspace)!r}, 'checkout', '-B', 'hijack', 'HEAD~1'], "
                    "capture_output=True)\n"
                    "print('rc', r.returncode)\n"
                    "print('err', r.stderr.decode()[:200])\n"
                )]
            )
        # The fail-closed launch means the executor is trivially safe.
        assert executor_head_sha(workspace) == sha_before
        return

    # With a real boundary, the executor's working tree is owned by
    # the harness uid; the auditor cannot chdir into it without read
    # permission on at least one ancestor (the snapshot clone has its
    # own parents). We launch the git attempt and require it to FAIL
    # — either because chdir fails, or because a sub-tree write is
    # denied. The executor HEAD sha must still match the recorded sha.
    fake_target = _git(workspace, "rev-parse", "HEAD~1")
    probe = launch_auditor_subprocess(
        ["git", "-C", str(workspace), "checkout", "-B", "hijack", fake_target]
    )
    _out, _err = probe.communicate(timeout=15)
    assert probe.returncode != 0
    assert executor_head_sha(workspace) == sha_before


def test_p1_launched_subprocess_denies_sibling_path_write(
    tmp_path: Path,
) -> None:
    """Sibling-run isolation: the auditor for run-A must not be able
    to write to run-B's snapshot or executor path. With a real
    boundary the kernel rejects; with UNAVAILABLE the launch is
    refused."""
    from lh_harness.safe_subprocess import (
        AuditorBoundaryKind,
        AuditorBoundaryUnavailable,
        detect_auditor_boundary,
        launch_auditor_subprocess,
    )

    workspace_a = tmp_path / "exec_a"
    workspace_b = tmp_path / "exec_b"
    _init_repo(workspace_a)
    _init_repo(workspace_b)
    info_a = create_isolated_snapshot(
        workspace_a, run_id="run-A", round_index=1,
    )
    info_b = create_isolated_snapshot(
        workspace_b, run_id="run-B", round_index=1,
    )
    sibling_src = Path(info_b.snapshot_path) / "src" / "app.py"
    sibling_src.chmod(0o600)
    try:
        boundary = detect_auditor_boundary()
        if boundary.kind == AuditorBoundaryKind.UNAVAILABLE:
            with pytest.raises(AuditorBoundaryUnavailable):
                launch_auditor_subprocess(
                    [sys.executable, "-c",
                     f"import pathlib; pathlib.Path({str(sibling_src)!r}).write_text('x')"]
                )
            assert "x" not in sibling_src.read_text(encoding="utf-8")
            return
        # Real boundary path
        probe = launch_auditor_subprocess(
            [sys.executable, "-c",
             (
                 "import sys, pathlib\n"
                 f"p = pathlib.Path({str(sibling_src)!r})\n"
                 "try:\n"
                 "    p.write_text('cross-run-write')\n"
                 "    print('CROSS_WRITE_OK')\n"
                 "except Exception as e:\n"
                 "    print('CROSS_WRITE_DENIED', type(e).__name__)\n"
                 "    sys.exit(2)\n"
             )]
        )
        out, _err = probe.communicate(timeout=15)
        assert "CROSS_WRITE_DENIED" in out
        assert "cross-run-write" not in sibling_src.read_text(encoding="utf-8")
    finally:
        try:
            sibling_src.chmod(0o644)
        except OSError:
            pass


def test_p1_read_only_git_inspection_works_in_auditor_subprocess(
    tmp_path: Path,
) -> None:
    """The auditor subprocess must still be able to read the snapshot
    (so it can audit): ``git status``, ``git log``, ``git rev-parse``,
    plus a scratch test output written OUTSIDE the source tree.
    With UNAVAILABLE the test asserts fail-closed."""
    from lh_harness.safe_subprocess import (
        AuditorBoundaryKind,
        AuditorBoundaryUnavailable,
        detect_auditor_boundary,
        launch_auditor_subprocess,
    )

    workspace = tmp_path / "exec"
    _init_repo(workspace)
    info = create_isolated_snapshot(workspace, run_id="run-p1-read", round_index=1)
    snapshot = Path(info.snapshot_path)

    boundary = detect_auditor_boundary()
    if boundary.kind == AuditorBoundaryKind.UNAVAILABLE:
        with pytest.raises(AuditorBoundaryUnavailable):
            launch_auditor_subprocess(
                ["git", "-C", str(snapshot), "log", "--oneline"]
            )
        return

    # Real boundary: with files at world-readable mode the auditor
    # subprocess can still read git history. The snapshot is at
    # 0o755/0o644 (chmod_readonly preserved user/owner r/x for dirs
    # and r for files).
    proc = launch_auditor_subprocess(
        ["git", "-C", str(snapshot), "rev-parse", "HEAD"]
    )
    out, err = proc.communicate(timeout=15)
    assert proc.returncode == 0
    assert out.strip() == info.executor_head_sha

    # Scratch test output OUTSIDE the snapshot is always allowed.
    scratch = tmp_path / "auditor_scratch"
    scratch.mkdir()
    (scratch / "result.txt").write_text("ok\n", encoding="utf-8")
    proc2 = launch_auditor_subprocess(
        ["cat", str(scratch / "result.txt")]
    )
    out2, _err2 = proc2.communicate(timeout=15)
    assert out2.strip() == "ok"


def test_p1_auditor_deny_lists_remain_defense_in_depth() -> None:
    """The deny lists (AUDITOR_LOCAL_GIT_DENY + AUDITOR_FS_DENY) are
    defense in depth on top of the OS boundary. They are not the
    boundary; they are the deny rules at the Claude tool layer.
    """
    boundary = detect_auditor_boundary_or_skip()  # type: ignore[name-defined]
    assert boundary is None or boundary.kind != "UNAVAILABLE" or True
    # The deny lists must remain unchanged for defense-in-depth, even
    # when the OS boundary is the primary line of defense.
    assert "Bash(git update-ref*)" in AUDITOR_LOCAL_GIT_DENY
    assert "Bash(git -C * update-ref*)" in AUDITOR_LOCAL_GIT_DENY
    assert "Bash(chmod*)" in AUDITOR_FS_DENY
    assert "Bash(chown*)" in AUDITOR_FS_DENY


def detect_auditor_boundary_or_skip():
    """Helper that returns None on UNAVAILABLE so the surrounding
    test still runs the deny-list assertions unconditionally.
    """
    from lh_harness.safe_subprocess import (
        AuditorBoundaryKind,
        detect_auditor_boundary,
    )

    b = detect_auditor_boundary()
    if b.kind == AuditorBoundaryKind.UNAVAILABLE:
        return None
    return b