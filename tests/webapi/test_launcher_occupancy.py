"""Workspace occupancy — task 173, scope 4 (2026-09-14 workspace-collision finding).

Beyond an active run, a workspace is OCCUPIED when ``git status --porcelain``
is non-empty (another task's uncommitted work) or when the checked-out branch
has no upstream and carries commits not on ``origin/main`` (another task's
unpushed work).  The skip reason names which condition fired, and
``[queue] occupancy_ignore_dirty = true`` is the per-environment overseer
override that disables exactly the two dirty-work probes.

Every git behaviour is proven against throwaway repos built under
``tmp_path`` (bare origin + clone) — never against a live workspace.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from lh_harness.launcher import (
    Launcher,
    _dirty_workspace_occupied,
    _unpushed_branch_occupied,
)
from lh_harness.queue import QueueStore, default_queue_config

from .test_launcher import FakeSupervisor, _base_entry
from .test_launcher_workspace_guard import GIT_ENV, _git, _make_repo


def _fixture(tmp_path: Path) -> tuple[Path, QueueStore, FakeSupervisor]:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = FakeSupervisor(root)
    return root, store, supervisor


def _launcher(store: QueueStore, supervisor: FakeSupervisor, **overrides: Any) -> Launcher:
    config = default_queue_config()
    config.update(overrides)
    return Launcher(supervisor, store, queue_config=config)


def _entry(store: QueueStore, workspace: Path) -> Any:
    return store.create(_base_entry(trio="kimi", workspace=str(workspace)))


def _commit(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")


# ----------------------------------------------------------------------
# Probe units
# ----------------------------------------------------------------------


def test_dirty_probe_true_for_untracked_and_modified(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    assert _dirty_workspace_occupied(str(repo)) is False

    (repo / "untracked.txt").write_text("x\n", encoding="utf-8")
    assert _dirty_workspace_occupied(str(repo)) is True

    proc = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True, env=GIT_ENV,
    )
    assert proc.stdout.strip() != ""


def test_dirty_probe_false_for_missing_or_non_repo(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert _dirty_workspace_occupied(str(missing)) is False
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _dirty_workspace_occupied(str(plain)) is False


def test_unpushed_probe_no_upstream_with_commits_off_origin_main(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # A local branch never pushed: no upstream, commits not on origin/main.
    _git(repo, "checkout", "-q", "-b", "feat/local-only")
    _commit(repo, "feature.txt", "local work\n")
    assert _unpushed_branch_occupied(str(repo)) is True


def test_unpushed_probe_false_for_pushed_branch(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # A clean, fully-pushed branch with an upstream is NOT occupied here.
    _git(repo, "checkout", "-q", "-b", "feat/pushed")
    _commit(repo, "feature.txt", "pushed work\n")
    _git(repo, "push", "-q", "-u", "origin", "feat/pushed")
    assert _unpushed_branch_occupied(str(repo)) is False


def test_unpushed_probe_false_on_default_branch(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # origin/main == HEAD on the default branch: nothing unpushed.
    assert _unpushed_branch_occupied(str(repo)) is False


# ----------------------------------------------------------------------
# Launcher integration
# ----------------------------------------------------------------------


def test_dirty_tree_skip_reason_names_condition(tmp_path: Path) -> None:
    # Tracked modification: never auto-parked (easybutt0n-17 auto-park covers
    # ONLY untracked leftovers — the untracked case launches now, see
    # test_untracked_only_leftovers_are_auto_parked_and_launch below).
    root, store, supervisor = _fixture(tmp_path)
    launcher = _launcher(store, supervisor)
    repo = _make_repo(tmp_path / "env")
    (repo / "README.md").write_text("base\nanother task's edits\n", encoding="utf-8")

    entry = _entry(store, repo)
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "pending"
    assert updated.run_id is None
    assert supervisor.created == []
    assert any(
        "occupied" in reason and "dirty tree" in reason
        for reason in updated.skip_reasons
    )


def test_unpushed_upstream_less_branch_skip_reason_names_condition(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    launcher = _launcher(store, supervisor)
    repo = _make_repo(tmp_path / "env2")
    _git(repo, "checkout", "-q", "-b", "feat/local-only")
    _commit(repo, "feature.txt", "local work\n")

    entry = _entry(store, repo)
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "pending"
    assert updated.run_id is None
    assert supervisor.created == []
    assert any(
        "occupied" in reason
        and "no upstream" in reason
        and "commits not on origin/main" in reason
        for reason in updated.skip_reasons
    )


def test_clean_workspace_still_launches_under_default_occupancy(tmp_path: Path) -> None:
    """A clean, fully-pushed workspace is not occupied; the launch proceeds."""

    root, store, supervisor = _fixture(tmp_path)
    launcher = _launcher(store, supervisor)
    repo = _make_repo(tmp_path / "env3")  # clean main, pushed

    entry = _entry(store, repo)
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "launched"
    assert updated.run_id in supervisor._runs


def test_occupancy_ignore_dirty_overrides_both_probes(tmp_path: Path) -> None:
    """The overseer override disables exactly the dirty-work probes."""

    root, store, supervisor = _fixture(tmp_path)
    launcher = _launcher(store, supervisor, occupancy_ignore_dirty=True)
    repo = _make_repo(tmp_path / "env4")
    _git(repo, "checkout", "-q", "-b", "feat/local-only")
    _commit(repo, "feature.txt", "local work\n")
    (repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")

    entry = _entry(store, repo)
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "launched"
    assert len(supervisor.created) == 1
    assert not any("occupied" in reason for reason in updated.skip_reasons)


def test_active_run_rule_survives_occupancy_ignore(tmp_path: Path) -> None:
    """The pre-existing active-run occupancy rule always applies, override or not."""

    root, store, supervisor = _fixture(tmp_path)
    launcher = _launcher(store, supervisor, occupancy_ignore_dirty=True)
    repo = _make_repo(tmp_path / "env5")
    supervisor.add_run("run-live", str(repo), status="running")

    entry = _entry(store, repo)
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "pending"
    assert supervisor.created == []
    assert any(
        f"active run run-live" in reason for reason in updated.skip_reasons
    )


def test_occupancy_skip_is_retryable_stays_pending(tmp_path: Path) -> None:
    """An occupancy skip records a skip reason and leaves the entry pending
    (not failed), so it is retried on the next pass like every other skip."""

    root, store, supervisor = _fixture(tmp_path)
    launcher = _launcher(store, supervisor)
    repo = _make_repo(tmp_path / "env6")
    # Tracked modification: untracked-only dirt is auto-parked since
    # easybutt0n-17, so the retryable-skip case needs tracked work.
    (repo / "README.md").write_text("base\nanother task's edits\n", encoding="utf-8")

    entry = _entry(store, repo)
    asyncio.run(launcher.tick())
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "pending"
    assert updated.failure_cause is None
    assert updated.skip_reasons


def test_occupancy_skip_is_a_shadow_skip_in_observe_mode(tmp_path: Path) -> None:
    """In observe mode an occupancy decision is recorded as a shadow skip."""

    import json

    root, store, supervisor = _fixture(tmp_path)
    launcher = _launcher(store, supervisor, observe=True)
    repo = _make_repo(tmp_path / "env7")
    (repo / "other-task.txt").write_text("another task's work\n", encoding="utf-8")

    entry = _entry(store, repo)
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "pending"
    assert supervisor.created == []
    log = root / "queue" / "shadow.jsonl"
    assert log.is_file()
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["type"] == "queue.shadow_skip"
    assert "dirty tree" in lines[0]["payload"]["reason"]


# ----------------------------------------------------------------------
# Auto-park (easybutt0n-17): untracked-only dirt is stashed, never skipped
# ----------------------------------------------------------------------


def _stash_list(repo: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "stash", "list"],
        capture_output=True, text=True, env=GIT_ENV,
    )
    assert proc.returncode == 0, f"git stash list failed: {proc.stderr}"
    return proc.stdout


def _porcelain(repo: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True, env=GIT_ENV,
    )
    assert proc.returncode == 0, f"git status failed: {proc.stderr}"
    # Keep the leading status columns (" M" etc.) — only the trailing
    # newline goes, so an empty tree still reads "".
    return proc.stdout.rstrip("\n")


def test_untracked_only_leftovers_are_auto_parked_and_launch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The CT110 case: a tree dirty ONLY with untracked run leftovers is
    auto-parked (``git stash push -u -m "lh-harness auto-park <queue_id> <utc>"``)
    and the entry launches instead of being skipped forever."""

    root, store, supervisor = _fixture(tmp_path)
    launcher = _launcher(store, supervisor)
    repo = _make_repo(tmp_path / "env-autopark")
    pycache = repo / "__pycache__"
    pycache.mkdir()
    (pycache / "launcher.cpython-312.pyc").write_bytes(b"pyc")
    newer_branch_dir = repo / "tests" / "only-on-newer-branch"
    newer_branch_dir.mkdir(parents=True)
    (newer_branch_dir / "test_new.py").write_text("# newer branch test\n", encoding="utf-8")
    assert _porcelain(repo) != ""

    entry = _entry(store, repo)
    with caplog.at_level(logging.INFO, logger="lh_harness.launcher"):
        asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "launched"
    assert updated.run_id in supervisor._runs
    # Leftovers are parked, the tree reads clean, and nothing is lost: the
    # stash names the queue id in the agreed message format.
    assert _porcelain(repo) == ""
    assert not (repo / "__pycache__").exists()
    assert not newer_branch_dir.exists()
    stash_list = _stash_list(repo)
    assert f"lh-harness auto-park {entry.queue_id} " in stash_list
    # One log line naming the queue id and the stashed file count (two
    # untracked porcelain entries parked here).
    auto_park_lines = [r for r in caplog.records if "auto-parked" in r.getMessage()]
    assert len(auto_park_lines) == 1
    message = auto_park_lines[0].getMessage()
    assert entry.queue_id in message
    assert " 2 " in f" {message} "


def test_modified_tracked_file_is_never_parked_and_reason_set(tmp_path: Path) -> None:
    """Modified tracked work keeps the skip behaviour — never stashed, never
    touched — and the skip_reason is set so it shows in the queue view."""

    root, store, supervisor = _fixture(tmp_path)
    launcher = _launcher(store, supervisor)
    repo = _make_repo(tmp_path / "env-modified")
    (repo / "README.md").write_text("base\nanother task's edits\n", encoding="utf-8")

    entry = _entry(store, repo)
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "pending"
    assert updated.run_id is None
    assert supervisor.created == []
    assert any(
        "occupied" in reason and "dirty tree" in reason
        for reason in updated.skip_reasons
    )
    # Untouched: no stash, the modification is still on disk.
    assert _stash_list(repo) == ""
    assert " M README.md" in _porcelain(repo)


def test_staged_tracked_file_is_never_parked_and_reason_set(tmp_path: Path) -> None:
    """Staged (index) work is tracked dirt too: same skip, same reason."""

    root, store, supervisor = _fixture(tmp_path)
    launcher = _launcher(store, supervisor)
    repo = _make_repo(tmp_path / "env-staged")
    (repo / "staged.txt").write_text("staged work\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")

    entry = _entry(store, repo)
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "pending"
    assert supervisor.created == []
    assert any(
        "occupied" in reason and "dirty tree" in reason
        for reason in updated.skip_reasons
    )
    assert _stash_list(repo) == ""
    assert "A  staged.txt" in _porcelain(repo)


def test_held_tree_is_never_auto_parked(tmp_path: Path) -> None:
    """No auto-park while a run holds the tree: the active-run rule fires
    first and the untracked leftovers stay exactly where they are."""

    root, store, supervisor = _fixture(tmp_path)
    launcher = _launcher(store, supervisor)
    repo = _make_repo(tmp_path / "env-held")
    (repo / "untracked-leftover.txt").write_text("leftover\n", encoding="utf-8")
    supervisor.add_run("run-live", str(repo), status="running")

    entry = _entry(store, repo)
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "pending"
    assert supervisor.created == []
    assert any(f"active run run-live" in reason for reason in updated.skip_reasons)
    assert (repo / "untracked-leftover.txt").read_text(encoding="utf-8") == "leftover\n"
    assert _stash_list(repo) == ""


# ----------------------------------------------------------------------
# Config plumbing
# ----------------------------------------------------------------------


def test_occupancy_ignore_dirty_config_round_trip(tmp_path: Path) -> None:
    from lh_harness.config import ProjectConfigError, load_run_defaults

    config = tmp_path / "config.toml"
    config.write_text("[queue]\noccupancy_ignore_dirty = true\n", encoding="utf-8")
    assert load_run_defaults(config)["queue"]["occupancy_ignore_dirty"] is True

    config2 = tmp_path / "config2.toml"
    config2.write_text("[queue.capacity]\npoll_seconds = 5\n", encoding="utf-8")
    assert load_run_defaults(config2)["queue"]["occupancy_ignore_dirty"] is False

    config3 = tmp_path / "config3.toml"
    config3.write_text("[queue]\noccupancy_ignore_dirty = \"yes\"\n", encoding="utf-8")
    with pytest.raises(ProjectConfigError, match="occupancy_ignore_dirty"):
        load_run_defaults(config3)


def test_queue_config_from_config_carries_occupancy_ignore_dirty() -> None:
    from lh_harness.queue import queue_config_from_config

    assert queue_config_from_config({})["occupancy_ignore_dirty"] is False
    assert (
        queue_config_from_config({"queue": {"occupancy_ignore_dirty": True}})
    )["occupancy_ignore_dirty"] is True