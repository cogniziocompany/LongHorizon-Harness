from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from lh_harness.adapters.claude_permissions import (
    snapshot_workspace,
    workspace_snapshot_diff,
)
from lh_harness.auditor_agent import audit_report_from_episode_result
from lh_harness.manager import _workspace_mutation_detected
from lh_harness.types import EpisodeResult


def _episode_result(mutations: dict[str, list[str]]) -> EpisodeResult:
    """EpisodeResult the way the adapter produces it, with a clean auditor report."""
    return EpisodeResult(
        status="done",
        actions_log=(
            "Status: complete\n"
            "Integrity: clean\n"
            "Contract audit: aligned\n\n"
            "Everything is in order."
        ),
        metadata={
            "verifier_workspace_mutation_detected": True,
            "verifier_workspace_mutations": mutations,
            "verifier_workspace_restored": False,
            "verifier_workspace_restore_on_mutation": True,
            "runtime_signals": [],
        },
    )


# The measured defect: per-worktree git admin metadata inside the audited
# workspace root, refreshed by read-only `git status`/`git diff` bookkeeping.
_MEASURED_GIT_METADATA_PATHS = [
    ".git/index",
    ".git/worktrees/task-200/index",
    ".git/worktrees/lh-task201/index",
    ".git/worktrees/task-201/HEAD",
]


@pytest.mark.parametrize("path", _MEASURED_GIT_METADATA_PATHS)
def test_git_internal_only_mutation_is_not_read_only_violation(path: str) -> None:
    """Git-internal metadata churn (incl. linked-worktree admin files) must not void a clean audit."""
    mutations = {"added": [], "changed": [path], "deleted": [], "type_changed": []}
    report = audit_report_from_episode_result(_episode_result(mutations), 1, language="en")
    assert report.status == "complete"
    assert report.integrity_status == "clean"
    assert report.contract_audit_status == "aligned"
    assert not any(finding["type"] == "verifier_workspace_write" for finding in report.integrity_findings)


@pytest.mark.parametrize("path", _MEASURED_GIT_METADATA_PATHS)
def test_git_internal_only_mutation_does_not_gate_the_manager(tmp_path: Path, path: str) -> None:
    """The manager's format-repair gate must not see a mutation either."""
    result = _episode_result({"added": [], "changed": [path], "deleted": [], "type_changed": []})
    assert _workspace_mutation_detected(result) is False


@pytest.mark.parametrize(
    "paths",
    [
        # Mixed with a real working file.
        [".git/worktrees/task-200/index", "src/file.py"],
        # Only real working files.
        ["src/file.py"],
        ["untracked_file.txt"],
        ["src/nested/artifact.txt"],
    ],
)
def test_real_workspace_write_is_still_read_only_violation(paths: list[str]) -> None:
    """A genuine working-file write outside .git/ must still be flagged as a read-only violation."""
    mutations = {"added": paths, "changed": [], "deleted": [], "type_changed": []}
    result = _episode_result(mutations)
    report = audit_report_from_episode_result(result, 1, language="en")
    assert report.status == "blocked"
    assert report.integrity_status == "violation"
    assert report.contract_audit_status == "unknown"
    assert any(finding["type"] == "verifier_workspace_write" for finding in report.integrity_findings)


# --- The episode-level fingerprint path: snapshot -> diff, exactly what the
# --- auditor adapter runs around the episode (claude_code.run_episode).


def _workspace_with_linked_worktrees(root: Path) -> Path:
    """A repo-style workspace whose linked-worktree admin dirs live under .git/worktrees/."""
    workspace = root / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
    for name in ("task-200", "lh-task201"):
        admin = workspace / ".git" / "worktrees" / name
        (admin / "logs").mkdir(parents=True)
        (admin / "index").write_bytes(b"DIRC" + b"\x00" * 64)
        (admin / "HEAD").write_text("ref: refs/heads/task-200\n", encoding="utf-8")
        (admin / "ORIG_HEAD").write_text("abc123\n", encoding="utf-8")
    return workspace


def _fingerprint_after(workspace: Path, mutate) -> dict:
    """snapshot_workspace before/after `mutate`, diffed like the adapter does."""
    before = snapshot_workspace(str(workspace))
    mutate()
    after = snapshot_workspace(str(workspace))
    return workspace_snapshot_diff(before, after)


def _shift_mtime(path: Path) -> None:
    stat = path.stat()
    os.utime(path, ns=(stat.st_mtime_ns + 2_000_000_000, stat.st_mtime_ns + 2_000_000_000))


def _read_only_git_bookkeeping_refresh(workspace: Path, *, only: str | None = None) -> None:
    """What read-only `git status`/`git diff` do to linked-worktree metadata: stat refreshes."""
    names = ("task-200", "lh-task201") if only is None else (only,)
    for name in names:
        admin = workspace / ".git" / "worktrees" / name
        # git rewrites the worktree index (lock + rename, identical bytes).
        index = admin / "index"
        tmp = admin / "index.lock"
        tmp.write_bytes(index.read_bytes())
        os.replace(tmp, index)
        # HEAD/ORIG_HEAD and the admin dir stat data churn too.
        _shift_mtime(admin / "HEAD")
        _shift_mtime(admin)
    time.sleep(0.01)


@pytest.mark.parametrize("name", ["task-200", "lh-task201"])
def test_read_only_git_worktree_metadata_refresh_is_not_a_violation(tmp_path: Path, name: str) -> None:
    """Read-only git bookkeeping under .git/worktrees/<name>/ must not count as a task write."""
    workspace = _workspace_with_linked_worktrees(tmp_path)
    diff = _fingerprint_after(
        workspace, lambda: _read_only_git_bookkeeping_refresh(workspace, only=name)
    )
    assert diff["verifier_workspace_mutation_detected"] is False, diff["verifier_workspace_mutations"]
    assert _workspace_mutation_detected(_episode_result(diff["verifier_workspace_mutations"])) is False


def test_worktree_metadata_only_episode_verdict_stands_end_to_end(tmp_path: Path) -> None:
    """Full episode path: measured .git/worktrees churn -> fingerprint -> clean verdict stands."""
    workspace = _workspace_with_linked_worktrees(tmp_path)
    diff = _fingerprint_after(workspace, lambda: _read_only_git_bookkeeping_refresh(workspace))
    result = _episode_result(diff["verifier_workspace_mutations"])
    result.metadata.update(diff)
    report = audit_report_from_episode_result(result, 1, language="en")
    assert report.status == "complete"
    assert report.integrity_status == "clean"
    assert report.contract_audit_status == "aligned"


@pytest.mark.parametrize("write", ["tracked", "untracked"])
def test_real_working_file_write_is_still_flagged_end_to_end(tmp_path: Path, write: str) -> None:
    """A real working-file write (tracked or untracked, outside .git/) still trips the guard."""
    workspace = _workspace_with_linked_worktrees(tmp_path)

    def auditor_writes() -> None:
        if write == "untracked":
            (workspace / "auditor_notes.md").write_text("notes\n", encoding="utf-8")
        else:
            (workspace / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")

    diff = _fingerprint_after(workspace, auditor_writes)
    assert diff["verifier_workspace_mutation_detected"] is True
    result = _episode_result(diff["verifier_workspace_mutations"])
    result.metadata.update(diff)
    report = audit_report_from_episode_result(result, 1, language="en")
    assert report.status == "blocked"
    assert report.integrity_status == "violation"
    assert any(f["type"] == "verifier_workspace_write" for f in report.integrity_findings)
