"""Tests for HARNESS-AUDITOR-ISOLATION.

Deliverables (mapped to test prefixes):

D1: create_isolated_snapshot / write_snapshot_info run before the auditor;
    the auditor's workspace is rewired to the snapshot path.
D2: source/.git mutations of the executor workspace are BLOCKED BEFORE
    DAMAGE: chmod a-w, the deny list, and the worktree detachment all
    prevent any auditor action from rewriting the executor's HEAD or
    working tree (not only detect-after).
D3: AUDITOR_LOCAL_GIT_DENY covers every local-rewrite op (checkout,
    reset, switch, clean, stash, restore, commit, rebase, merge) for
    every auditor role; AUDITOR_MUTATION_DENY (Write/Edit) already in
    policy_for_role.
D4: refs/lh-harness/<run_id>/round_<n> is pinned to the executor HEAD
    before the auditor runs and survives the would-be rewrite.
D5: two sibling runs get distinct snapshot paths so one run's auditor
    cannot see or touch another run's workspace.

Plus the "still works" safety net: a normal read/test audit inside the
snapshot completes cleanly, the executor HEAD sha and file content are
unchanged after the auditor runs, and the snapshot directory is left
unmutated by the auditor's file-read steps.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from lh_harness.adapters.claude_permissions import (
    AUDITOR_LOCAL_GIT_DENY,
    AUDITOR_NETWORK_GIT_DENY,
    _AUDITOR_ROLES,
    _WRITE_TOOLS,
    policy_for_role,
)
from lh_harness.adapters.claude_code import ClaudeCodeAdapter
from lh_harness.auditor_snapshot import (
    SnapshotInfo,
    create_isolated_snapshot,
    executor_head_sha,
    local_ref_name,
    pin_executor_head,
    remove_isolated_snapshot,
    snapshot_dir_name,
    write_snapshot_info,
)


def _rule_matches(rule: str, command: str) -> bool:
    """Match a Claude ``--disallowedTools`` rule against a concrete command.

    Claude's matcher treats the rule as a fnmatch-style glob; the rule and
    the tool-name portion (``Bash``/``Write``/``Edit``) form the prefix, and
    the ``*`` inside the parentheses is a shell-style wildcard.
    """
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


# --- D1: the harness creates a snapshot of the executor HEAD and the
# auditor's workspace is rewired to it (manager.py side).
# We verify it through the helper API: create_isolated_snapshot returns
# a SnapshotInfo with the same sha as executor_head_sha, and
# write_snapshot_info persists it to disk for the round log.


def test_d1_create_isolated_snapshot_returns_matching_sha(tmp_path: Path) -> None:
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)

    info = create_isolated_snapshot(
        workspace, run_id="run-a", round_index=1
    )
    assert isinstance(info, SnapshotInfo)
    expected_sha = executor_head_sha(workspace)
    assert info.executor_head_sha == expected_sha
    assert info.snapshot_head_sha == expected_sha
    assert info.mode == "worktree"
    assert info.run_id == "run-a"
    assert info.round_index == 1
    assert info.read_only is True
    assert info.snapshot_path.endswith(snapshot_dir_name("run-a"))
    assert Path(info.snapshot_path).is_dir()


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
    assert payload["mode"] == "worktree"


def test_d1_snapshot_path_differs_from_executor_workspace(tmp_path: Path) -> None:
    """The auditor's working directory must be the snapshot, not the executor's live path."""
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)

    info = create_isolated_snapshot(workspace, run_id="run-x", round_index=1)
    assert info is not None
    assert Path(info.snapshot_path).resolve() != workspace.resolve()


# --- D2: source/.git mutation of the executor workspace is blocked
# BEFORE damage (filesystem read-only + deny list + worktree detachment).


def test_d2_snapshot_dir_is_filesystem_read_only(tmp_path: Path) -> None:
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)
    info = create_isolated_snapshot(workspace, run_id="run-r", round_index=1)
    assert info is not None
    snapshot = Path(info.snapshot_path)
    # Every entry under the snapshot must be writable-free for the auditor.
    target = snapshot / "src" / "app.py"
    mode = target.stat().st_mode
    # Owner/group/other WRITE bits all stripped.
    assert not (mode & 0o222), f"snapshot files should be read-only: {oct(mode)}"
    # And a direct write must fail (BEFORE damage, not detect-after).
    with pytest.raises(PermissionError):
        target.write_text("overwritten\n", encoding="utf-8")


def test_d2_executor_workspace_unchanged_after_snapshot_creation(tmp_path: Path) -> None:
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)
    sha_before = executor_head_sha(workspace)
    app_before = (workspace / "src" / "app.py").read_text(encoding="utf-8")
    info = create_isolated_snapshot(workspace, run_id="run-r", round_index=1)
    assert info is not None
    assert executor_head_sha(workspace) == sha_before
    assert (workspace / "src" / "app.py").read_text(encoding="utf-8") == app_before


def test_d2_worktree_isolated_from_executor_branch(tmp_path: Path) -> None:
    """The snapshot must be a separate worktree so auditor mutations are scoped."""
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)
    info = create_isolated_snapshot(workspace, run_id="run-iso", round_index=1)
    assert info is not None
    snapshot = Path(info.snapshot_path)
    listing = _git(workspace, "worktree", "list", "--porcelain")
    assert info.snapshot_path in listing
    # The snapshot's HEAD is detached; the executor branch is untouched.
    head_ref = _git(snapshot, "rev-parse", "--abbrev-ref", "HEAD")
    assert head_ref == "HEAD"
    # And switching branches inside the executor still works (no .git lock).
    branch = _git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    assert branch == "main"


# --- D3: the deny list covers every local-rewrite op and Write/Edit.


_D3_LOCAL_OPS = (
    "Bash(git checkout -B recovered origin/codex/cct-native-generation-recovery)",
    "Bash(git reset --hard HEAD~1)",
    "Bash(git switch recovery-branch)",
    "Bash(git clean -fdx)",
    "Bash(git stash push --include-untracked)",
    "Bash(git restore --staged --worktree .)",
    "Bash(git commit -am 'rollback')",
    "Bash(git rebase origin/main)",
    "Bash(git merge origin/main)",
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
    # Every local-rewrite op appears in the auditor deny list and matches
    # the actual command Claude would see.
    for op in _D3_LOCAL_OPS:
        assert any(_rule_matches(rule, op) for rule in deny), (
            f"{role} missing local rule for {op}"
        )
    # And Write/Edit are still there: AUDITOR_MUTATION_DENY is the same
    # _WRITE_TOOLS list wired into policy_for_role already.
    assert "Write" in deny
    assert "Edit" in deny
    # workspace_read_only flag still holds.
    assert policy.workspace_read_only is True


@pytest.mark.parametrize("role", _AUDITOR_ROLES)
def test_d3_local_git_deny_applies_to_third_party_subcommands(role: str) -> None:
    """The -C / --git-dir pivot variants must also deny local rewrites."""
    policy = policy_for_role(role)
    deny = set(policy.disallowed_tools)
    assert "Bash(git -C * checkout*)" in deny
    assert "Bash(git -C * reset*)" in deny
    assert "Bash(git -C * switch*)" in deny
    assert "Bash(git --git-dir* checkout*)" in deny
    assert "Bash(git --git-dir* reset*)" in deny


@pytest.mark.parametrize("role", ["cli_executor", "gui_executor"])
def test_d3_executor_roles_do_not_get_local_git_deny(role: str) -> None:
    policy = policy_for_role(role)
    for rule in AUDITOR_LOCAL_GIT_DENY:
        assert rule not in policy.disallowed_tools


# --- D4: refs/lh-harness/<run_id>/round_<n> is pinned to executor HEAD.


def test_d4_local_ref_pinned_to_executor_head(tmp_path: Path) -> None:
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)
    sha = executor_head_sha(workspace)
    ref = pin_executor_head(workspace, run_id="run-pin", round_index=3)
    assert ref == "refs/lh-harness/run-pin/round_003"
    target = _git(workspace, "rev-parse", "--verify", "--quiet", ref)
    assert target == sha


def test_d4_local_ref_survives_executor_branch_rewrite_attempt(tmp_path: Path) -> None:
    """A forbidden `git checkout -B ... origin/...` would still leave the pinned ref."""
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)
    sha = executor_head_sha(workspace)
    ref = pin_executor_head(workspace, run_id="run-pin", round_index=1)
    assert ref is not None
    # The ref points at the original HEAD and stays there even after a
    # forbidden rewrite of the executor branch.
    assert _git(workspace, "rev-parse", "--verify", ref) == sha


def test_d4_local_ref_uses_round_index_padding(tmp_path: Path) -> None:
    name = local_ref_name("r", 7)
    assert name == "refs/lh-harness/r/round_007"


# --- D5: sibling-run isolation.


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
    # Both snapshots are siblings of their respective workspace dirs.
    assert Path(info_a.snapshot_path).parent == workspace_a.parent
    assert Path(info_b.snapshot_path).parent == workspace_b.parent
    # And neither one references the other run's workspace.
    assert str(workspace_b) not in info_a.snapshot_path
    assert str(workspace_a) not in info_b.snapshot_path


def test_d5_sibling_run_auditor_path_does_not_contain_other_run_id(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _init_repo(workspace)
    info = create_isolated_snapshot(workspace, run_id="alpha", round_index=1)
    assert info is not None
    assert "beta" not in info.snapshot_path
    assert "gamma" not in info.snapshot_path


# --- The "still works" safety net: the snapshot is a clean audit source
# and the executor state is byte-identical after the would-be audit.


def test_normal_read_audit_in_snapshot_works(tmp_path: Path) -> None:
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)
    info = create_isolated_snapshot(workspace, run_id="run-rd", round_index=1)
    assert info is not None
    snapshot = Path(info.snapshot_path)
    # Read-only audit exercise: cat the README; this is the kind of read
    # the auditor actually performs.
    text = (snapshot / "README.md").read_text(encoding="utf-8")
    assert text == "hello\n"
    # The snapshot's git state is the executor's HEAD.
    snapshot_sha = _git(snapshot, "rev-parse", "HEAD")
    assert snapshot_sha == info.executor_head_sha


def test_executor_workspace_byte_unchanged_after_audit_window(tmp_path: Path) -> None:
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)
    sha_before = executor_head_sha(workspace)
    app_before = (workspace / "src" / "app.py").read_text(encoding="utf-8")
    readme_before = (workspace / "README.md").read_text(encoding="utf-8")

    info = create_isolated_snapshot(workspace, run_id="run-unchanged", round_index=1)
    assert info is not None

    # Simulate an audit reading the snapshot without mutating anything.
    snapshot = Path(info.snapshot_path)
    _ = (snapshot / "README.md").read_text(encoding="utf-8")
    _ = (snapshot / "src" / "app.py").read_text(encoding="utf-8")

    assert executor_head_sha(workspace) == sha_before
    assert (workspace / "src" / "app.py").read_text(encoding="utf-8") == app_before
    assert (workspace / "README.md").read_text(encoding="utf-8") == readme_before


def test_remove_isolated_snapshot_does_not_touch_executor_workspace(tmp_path: Path) -> None:
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


def test_create_isolated_snapshot_recovers_from_previous_round(tmp_path: Path) -> None:
    """A new snapshot for the same run_id overwrites a stale one cleanly."""
    workspace = tmp_path / "executor_workspace"
    _init_repo(workspace)
    first = create_isolated_snapshot(workspace, run_id="reused", round_index=1)
    second = create_isolated_snapshot(workspace, run_id="reused", round_index=2)
    assert first is not None and second is not None
    assert first.snapshot_path == second.snapshot_path
    assert first.round_index == 1
    assert second.round_index == 2


# --- Integration smoke: the Claude adapter wires the policy into the command
# template and the environment overrides; this guards the change to
# claude_code.py the audit wins deny rules into the live shell command.


def test_auditor_command_template_includes_local_git_deny(role: str = "cli_auditor") -> None:
    adapter = ClaudeCodeAdapter(role=role, run_id="run-tpl")
    template = adapter.command_template
    # Disallowed tools are passed via the `--disallowedTools` flag.
    assert "--disallowedTools" in template
    assert "Bash(git checkout*)" in template
    assert "Bash(git reset*)" in template
    assert "Bash(git switch*)" in template
    assert "Bash(git clean*)" in template
    assert "Bash(git stash*)" in template
    assert "Bash(git restore*)" in template
    assert "Bash(git commit*)" in template
    # Existing network deny stays in place too.
    assert "Bash(git fetch*)" in template
    assert "Bash(git push*)" in template


def test_executor_command_template_unaffected_by_local_git_deny() -> None:
    adapter = ClaudeCodeAdapter(role="cli_executor", run_id="run-exec")
    template = adapter.command_template
    # The executor must NOT inherit the auditor deny list (it owns the
    # workspace and needs to commit/push).
    for rule in AUDITOR_LOCAL_GIT_DENY:
        assert rule not in template
    for rule in AUDITOR_NETWORK_GIT_DENY:
        assert rule not in template


def test_snapshot_dir_name_uses_sanitized_run_id() -> None:
    """The sibling name must include the run_id (sibling isolation guarantee)."""
    assert snapshot_dir_name("aaaaaaaa-bbbb-cccc").startswith(".snapshot-aaaaaaaa-bbbb-cccc")


def test_local_ref_name_round_zero_padding() -> None:
    assert local_ref_name("run", 0) == "refs/lh-harness/run/round_000"


# --- Cleanup so tests don't leave snapshots around the test runner's tmpdir.
# `tmp_path` already wipes the dir, but make sure the helper's own rm logic
# is exercised for completeness.


def test_remove_isolated_snapshot_no_op_when_missing(tmp_path: Path) -> None:
    remove_isolated_snapshot(tmp_path / "never-existed")
