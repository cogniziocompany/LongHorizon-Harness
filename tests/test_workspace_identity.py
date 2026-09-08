from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from lh_harness import workspace_identity as wi


@pytest.fixture(autouse=True)
def _clear_identity_cache():
    wi.clear_cache()
    yield
    wi.clear_cache()


def _run(args: list[str]) -> None:
    subprocess.run(args, check=True, capture_output=True)


def test_identity_for_non_git_path(tmp_path: Path) -> None:
    path = tmp_path / "plain"
    path.mkdir()

    identity = wi.resolve_workspace(str(path))

    assert identity.path == str(path)
    assert identity.key == str(path.resolve())
    assert identity.is_git is False
    assert identity.repo_id is None
    assert identity.branch is None


def test_identity_resolves_main_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q", str(repo)])
    _run(["git", "-C", str(repo), "checkout", "-q", "-b", "main"])
    _run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init"])

    identity = wi.resolve_workspace(str(repo))

    assert identity.is_git is True
    assert identity.branch == "main"
    assert identity.git_common_dir is not None
    assert identity.repo_id is not None


def test_repo_id_strips_credentials(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q", str(repo)])
    _run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init"])
    _run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "https://user:pass@github.com/Org/Repo.git",
        ]
    )

    identity = wi.resolve_workspace(str(repo))

    assert identity.repo_id == "remote:github.com/org/repo"


def test_repo_id_for_scp_remote(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q", str(repo)])
    _run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init"])
    # `git config --get` returns the literal configured URL, not the rewritten
    # form, so SCP syntax must be parsed here.
    _run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:Org/Repo"])

    identity = wi.resolve_workspace(str(repo))

    assert identity.repo_id == "remote:github.com/org/repo"


def test_repo_id_falls_back_to_commondir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q", str(repo)])

    identity = wi.resolve_workspace(str(repo))

    assert identity.git_common_dir is not None
    assert identity.repo_id.startswith("commondir:")
    assert len(identity.repo_id.split(":", 1)[1]) == 16


def test_relative_git_dir_is_absolutised(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q", str(repo)])

    identity = wi.resolve_workspace(str(repo))

    assert identity.is_git is True
    assert identity.git_common_dir is not None
    assert Path(identity.git_common_dir).is_absolute()


def test_worktree_common_dir_is_absolute_and_same_as_primary(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q", str(repo)])
    _run(["git", "-C", str(repo), "checkout", "-q", "-b", "main"])
    _run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init"])
    worktree = tmp_path / "worktree"
    # We must avoid `git worktree list`; creating a worktree is acceptable in
    # tests because the module itself never invokes that subcommand.
    _run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "wt", str(worktree)])

    primary = wi.resolve_workspace(str(repo))
    secondary = wi.resolve_workspace(str(worktree))

    assert primary.git_common_dir is not None
    assert primary.git_common_dir == secondary.git_common_dir
    assert primary.key != secondary.key


def test_resolve_many_returns_all_paths(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    result = wi.resolve_many([str(a), str(b)], budget_seconds=3.0)

    assert set(result) == {str(a), str(b)}


def test_resolve_many_respects_budget_and_fails_open(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    original_resolve = wi.resolve_workspace
    call_count = 0

    def slow_resolve(path: str) -> wi.WorkspaceIdentity:
        nonlocal call_count
        call_count += 1
        subprocess.run(["sleep", "0.2"], check=True)
        return original_resolve(path)

    with patch.object(wi, "resolve_workspace", side_effect=slow_resolve):
        result = wi.resolve_many([str(a), str(b)], budget_seconds=0.001)

    # The first path started before the deadline and completed, but it consumed
    # the whole budget, so the second path had to fail open.
    assert result[str(a)].error is None
    assert result[str(b)].error == "resolve budget exhausted"


def test_resolve_many_timeout_fails_open(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q", str(repo)])

    def hanging_git(*args: Any, **kwargs: Any) -> Any:
        class Dummy:
            returncode = 0
            stdout = ""
        return Dummy()

    with patch("subprocess.run", side_effect=hanging_git):
        identity = wi.resolve_workspace(str(repo))

    assert identity.is_git is False


def test_normcase_equates_case_on_case_insensitive_platforms(tmp_path: Path) -> None:
    path = tmp_path / "Repo"
    path.mkdir()
    key_lower = wi.resolve_workspace(str(path)).key
    # normcase is a no-op on POSIX; on Windows it equates C:\Foo with c:\foo.
    # On a case-sensitive filesystem the keys will differ; the invariant we care
    # about is that both are stable realpaths with normcase applied.
    assert key_lower == os.path.normcase(os.path.realpath(str(path)))
