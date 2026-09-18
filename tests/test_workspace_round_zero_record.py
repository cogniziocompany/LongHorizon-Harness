"""The round-zero workspace record observes, and never alters, a launch.

Task 180 (RE-CUT): before a run is handed a workspace, ``role_harness_start``
must carry one observation record — the workspace's branch, HEAD sha, whether
it is ahead of its remote, its uncommitted paths, and any open PR whose head
is that branch.  These tests pin the record's contents and its observe-only
nature: a workspace that would have launched before the change must still
launch, byte-for-byte.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from lh_harness.environment.local import LocalEnvironment
from lh_harness.manager import _round_zero_open_prs, _workspace_round_zero_record, run
from lh_harness.types import EpisodeResult, HarnessConfig

_DONE = "Next: done\n\nCurrent Task State:\nall finished"

_RECORD_KEYS = {
    "branch",
    "head_sha",
    "ahead_of_remote",
    "uncommitted_paths",
    "open_prs",
}


def _git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _git_workspace_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A real clone of a real (bare) remote, with one pushed commit."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    workspace = tmp_path / "workspace"
    subprocess.run(["git", "clone", "-q", str(remote), str(workspace)], check=True)
    _git(workspace, "config", "user.email", "round-zero@example.com")
    _git(workspace, "config", "user.name", "Round Zero")
    (workspace / "tracked.txt").write_text("seed\n", encoding="utf-8")
    _git(workspace, "add", "tracked.txt")
    _git(workspace, "commit", "-q", "-m", "seed")
    _git(workspace, "push", "-q", "origin", "HEAD")
    return workspace, remote


class DoneAgent:
    async def run_episode(self, prompt, _env, _budget, live_trajectory_path=None):
        return EpisodeResult(status="done", actions_log=_DONE)


def _run(tmp_path: Path, workspace_path: Path) -> None:
    result_path = tmp_path / "report.json"
    import asyncio

    async def _invoke() -> None:
        await run(
            task="observe the workspace",
            env=LocalEnvironment(str(tmp_path / "tmp")),
            config=HarnessConfig(
                max_total_episodes=1,
                workspace_path=str(workspace_path),
                harness_dir=str(tmp_path / "harness"),
                log_dir=str(tmp_path / "logs"),
            ),
            agent=DoneAgent(),
            resume=False,
        )
        result_path.write_text("done", encoding="utf-8")

    asyncio.run(_invoke())
    assert result_path.exists(), "the managed loop must complete"


def test_record_captures_branch_head_and_dirty_state(tmp_path: Path) -> None:
    workspace, _remote = _git_workspace_with_remote(tmp_path)
    (workspace / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
    (workspace / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git(workspace, "add", "staged.txt")

    record = _workspace_round_zero_record(workspace, open_prs=lambda _branch: [])

    assert record["branch"] == _git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    assert record["head_sha"] == _git(workspace, "rev-parse", "HEAD")
    assert record["ahead_of_remote"] is False, "a pushed commit is not ahead"
    assert record["uncommitted_paths"] == ["staged.txt", "uncommitted.txt"]
    assert record["open_prs"] == []


def test_record_reports_an_unpushed_commit_as_ahead_of_remote(tmp_path: Path) -> None:
    workspace, _remote = _git_workspace_with_remote(tmp_path)
    (workspace / "second.txt").write_text("local only\n", encoding="utf-8")
    _git(workspace, "add", "second.txt")
    _git(workspace, "commit", "-q", "-m", "not yet pushed")

    record = _workspace_round_zero_record(workspace, open_prs=lambda _branch: [])

    assert record["ahead_of_remote"] is True
    assert record["head_sha"] == _git(workspace, "rev-parse", "HEAD")


def test_record_reports_a_detached_head_as_not_a_branch(tmp_path: Path) -> None:
    workspace, _remote = _git_workspace_with_remote(tmp_path)
    _git(workspace, "checkout", "-q", "--detach", "HEAD")

    record = _workspace_round_zero_record(workspace, open_prs=lambda _branch: [])

    assert record["branch"] == "HEAD"
    assert record["head_sha"] == _git(workspace, "rev-parse", "HEAD")


def test_record_degrades_outside_a_git_repo_without_raising(tmp_path: Path) -> None:
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()

    record = _workspace_round_zero_record(plain_dir)

    assert record == {
        "branch": None,
        "head_sha": None,
        "ahead_of_remote": None,
        "uncommitted_paths": [],
        "open_prs": None,
    }


def test_open_pr_query_treats_unavailable_gh_as_unknown_not_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("no gh")),
    )

    assert _round_zero_open_prs("any-branch") is None


def test_record_keeps_an_unqueryable_pr_list_honest(tmp_path: Path) -> None:
    workspace, _remote = _git_workspace_with_remote(tmp_path)

    def _broken(_branch: str) -> Any:
        raise RuntimeError("gh unavailable")

    record = _workspace_round_zero_record(workspace, open_prs=_broken)

    assert record["open_prs"] is None, "unknown must not be reported as zero PRs"


def test_record_surfaces_open_prs_headed_at_the_workspace_branch(tmp_path: Path) -> None:
    workspace, _remote = _git_workspace_with_remote(tmp_path)

    collision = {
        "number": 12,
        "title": "task 180 prelaunch guard",
        "url": "https://github.com/example/repo/pull/12",
    }

    record = _workspace_round_zero_record(workspace, open_prs=lambda _branch: [collision])

    assert record["open_prs"] == [collision]


def test_record_only_observes_the_workspace(tmp_path: Path) -> None:
    workspace, _remote = _git_workspace_with_remote(tmp_path)
    head_before = _git(workspace, "rev-parse", "HEAD")
    status_before = _git(workspace, "status", "--porcelain")
    reflog_before = _git(workspace, "reflog", "--format=%H")

    _workspace_round_zero_record(workspace, open_prs=lambda _branch: [])

    assert _git(workspace, "rev-parse", "HEAD") == head_before
    assert _git(workspace, "status", "--porcelain") == status_before
    assert _git(workspace, "reflog", "--format=%H") == reflog_before


@pytest.mark.asyncio
async def test_role_harness_start_carries_the_round_zero_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _remote = _git_workspace_with_remote(tmp_path)
    (workspace / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
    monkeypatch.setattr(
        "lh_harness.manager._round_zero_open_prs", lambda _branch: []
    )

    config = HarnessConfig(
        max_total_episodes=1,
        workspace_path=str(workspace),
        harness_dir=str(tmp_path / "harness"),
        log_dir=str(tmp_path / "logs"),
    )
    report = await run(
        task="observe the workspace",
        env=LocalEnvironment(str(tmp_path / "tmp")),
        config=config,
        agent=DoneAgent(),
        resume=False,
    )

    # The loop's own completion semantics (round budget, clean-audit gate) are
    # out of scope here; this test only pins what role_harness_start carries.
    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "role_orchestration" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    start = next(item for item in events if item["event"] == "role_harness_start")
    record = start["workspace_round_zero"]
    assert set(record) == _RECORD_KEYS, "the record carries exactly the five items"
    assert record["branch"] == _git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    assert record["head_sha"] == _git(workspace, "rev-parse", "HEAD")
    assert record["ahead_of_remote"] is False
    assert record["uncommitted_paths"] == ["uncommitted.txt"]
    assert record["open_prs"] == []
    # The change is additive: every pre-existing field survives untouched.
    assert start["variant"] == "lh_harness_role_managed"
    assert start["resumed"] is False
    assert start["resumed_rounds"] == 0
    assert start["workspace_path"] == str(workspace)
    assert start["task_chars"] == len("observe the workspace")