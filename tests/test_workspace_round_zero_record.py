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
import sys
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
    # Task 201: the record carries the five observed items; the guard's mode
    # appears only when the launch supplied one (this launch did not).
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

def test_record_carries_the_guard_mode_when_the_launch_supplied_one(
    tmp_path: Path,
) -> None:
    """Task 201: the round-zero record names the mode the guard chose."""

    workspace, _remote = _git_workspace_with_remote(tmp_path)

    record = _workspace_round_zero_record(
        workspace,
        open_prs=lambda _branch: [],
        workspace_base_mode="continuation",
    )

    assert record["workspace_base_mode"] == "continuation"
    assert record["branch"] == _git(workspace, "rev-parse", "--abbrev-ref", "HEAD")


def test_record_omits_the_guard_mode_when_the_launch_supplied_none(
    tmp_path: Path,
) -> None:
    """Task 201: a launch without a resolved base omits the mode, never guesses."""

    workspace, _remote = _git_workspace_with_remote(tmp_path)

    record = _workspace_round_zero_record(workspace, open_prs=lambda _branch: [])

    assert "workspace_base_mode" not in record


@pytest.mark.asyncio
async def test_role_harness_start_carries_the_guard_mode_from_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 201 (d): a queue-triggered launch's mode reaches the record."""

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
        workspace_base_mode="worktree",
    )
    await run(
        task="observe the workspace",
        env=LocalEnvironment(str(tmp_path / "tmp")),
        config=config,
        agent=DoneAgent(),
        resume=False,
    )

    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "role_orchestration" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    start = next(item for item in events if item["event"] == "role_harness_start")
    record = start["workspace_round_zero"]
    assert record["workspace_base_mode"] == "worktree"
    # Every pre-existing field survives untouched.
    assert record["branch"] == _git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    assert record["head_sha"] == _git(workspace, "rev-parse", "HEAD")
    assert record["uncommitted_paths"] == ["uncommitted.txt"]


# ---------------------------------------------------------------------------
# LHH-WORKSPACE-TEST-PREFLIGHT
#
# The preflight answers "can this workspace's declared tests even run?"
# *before* round 1 is dispatched.  A run that discovers a missing pytest
# inside round 1 burns the whole round budget discovering it, so the gate
# has to fail fast with a typed, owned prerequisite instead.
#
# Every scenario below uses fixture/temp directories only: no network, no
# real harness paths, and no installs into the shared harness venv.
# ---------------------------------------------------------------------------


def _write_python_manifest(workspace: Path, *, test_extra: list[str] | None = None) -> None:
    """Write a minimal pyproject that declares the given ``test`` extra."""

    requirements = test_extra if test_extra is not None else ["pytest"]
    rendered = ", ".join(f'"{name}"' for name in requirements)
    (workspace / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n'
        "\n"
        "[project]\n"
        'name = "fixture-repo"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.10"\n'
        "\n"
        "[project.optional-dependencies]\n"
        f"test = [{rendered}]\n",
        encoding="utf-8",
    )


def _write_a_real_test(workspace: Path) -> None:
    """Write one trivially-collectable test into the fixture repo."""

    tests_dir = workspace / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_fixture.py").write_text(
        "def test_fixture_is_collectable():\n    assert True\n",
        encoding="utf-8",
    )


def _bare_interpreter(tmp_path: Path) -> str:
    """An interpreter that has pytest but is missing the declared httpx2 extra.

    The scenario must not depend on what the host happens to have installed,
    so the "missing dependency" case is pinned to an interpreter whose
    importability is controlled.

    The shim runs the real interpreter with a ``sitecustomize`` module on
    ``PYTHONPATH`` that blocks the ``httpx2`` import.  Blocking the *import*
    (rather than hiding the module on disk) is the faithful simulation: it
    is exactly the shape of failure the preflight exists to catch, where
    the package is nominally present but the environment cannot use it.
    """

    shim_dir = tmp_path / "bare-venv" / "bin"
    shim_dir.mkdir(parents=True)
    blocker_dir = tmp_path / "bare-venv" / "blocker"
    blocker_dir.mkdir(parents=True)
    (blocker_dir / "sitecustomize.py").write_text(
        "import sys\n"
        "from importlib.abc import MetaPathFinder\n"
        "from importlib.machinery import ModuleSpec\n"
        "\n"
        "\n"
        "class _Blocker(MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] == 'httpx2':\n"
        "            return ModuleSpec(name, _Loader())\n"
        "        return None\n"
        "\n"
        "\n"
        "class _Loader:\n"
        "    def create_module(self, spec):\n"
        "        raise ImportError('httpx2 is not available in this environment')\n"
        "    def exec_module(self, module):\n"
        "        raise ImportError('httpx2 is not available in this environment')\n"
        "\n"
        "\n"
        "sys.meta_path.insert(0, _Blocker())\n",
        encoding="utf-8",
    )
    shim = shim_dir / "python"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"_blocker = {str(blocker_dir)!r}\n"
        f"_real = {sys.executable!r}\n"
        "_existing = os.environ.get('PYTHONPATH', '')\n"
        "os.environ['PYTHONPATH'] = "
        "_blocker + ((':' + _existing) if _existing else '')\n"
        "os.execv(_real, [_real] + sys.argv[1:])\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return str(shim)


def _git_init_workspace(workspace: Path) -> None:
    """Make a fixture workspace a real git repo with one commit.

    The preflight records the run's candidate SHA from the workspace's HEAD,
    so a faithful fixture is a real repository with a real commit -- not a
    bare directory that would report an empty SHA.
    """

    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.email", "preflight@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.name", "Preflight"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace), "add", "-A"], check=True
    )
    subprocess.run(
        ["git", "-C", str(workspace), "commit", "-q", "-m", "fixture"],
        check=True,
    )


def _healthy_workspace(tmp_path: Path) -> Path:
    """A fixture workspace whose declared test extras are all importable.

    The declared extras are the ones the running interpreter already has
    (``pytest``), so the healthy scenario needs no network and no install.
    """

    workspace = tmp_path / "healthy"
    workspace.mkdir()
    _write_python_manifest(workspace, test_extra=["pytest"])
    _write_a_real_test(workspace)
    _git_init_workspace(workspace)
    return workspace


def _fixture_without_test_extras(tmp_path: Path) -> Path:
    """A fixture workspace whose declared extras cannot be satisfied.

    The declared extra (``httpx2``) is the one this repo's own manifest
    names, but the interpreter the scenario runs under does not have it and
    the fixture manifest offers no way to install it -- so the preflight
    must raise a typed prerequisite naming its owner instead of consuming a
    round.

    The interpreter is *pinned* to a bare one so the scenario does not
    depend on what the host happens to have installed: the point of the
    test is the missing dependency, not the host's package set.
    """

    workspace = tmp_path / "missing-extras"
    workspace.mkdir()
    _write_python_manifest(workspace, test_extra=["pytest", "httpx2"])
    _write_a_real_test(workspace)
    _git_init_workspace(workspace)
    return workspace


def test_preflight_does_not_block_a_workspace_with_no_test_suite(
    tmp_path: Path,
) -> None:
    """(A) "No tests to run" is not "a broken test environment".

    A workspace with no manifest and no test directory has no test suite at
    all.  Treating its empty collection as a failure would block every run
    whose workspace simply has nothing to test -- including the many
    harness self-tests that use a bare temp directory as their workspace.
    The zero-collected rule applies to a repo that *declares* tests.
    """

    from lh_harness.workspace_preflight import run_preflight

    workspace = tmp_path / "no-suite"
    workspace.mkdir()

    record = run_preflight(
        workspace,
        allow_env_prep=False,
        allow_install=False,
    )

    assert record.ok is True, record.prerequisite
    assert record.env_manifest["has_tests"] is False
    assert record.collection["collected"] == 0
    assert record.collection["ok"] is True
    assert "no tests" in record.collection["detail"]


def test_preflight_reads_the_manifest_and_the_declared_test_extras(
    tmp_path: Path,
) -> None:
    """(A) The manifest and lock files are read; the extras are extracted."""

    from lh_harness.workspace_preflight import read_manifest

    workspace = tmp_path / "manifest-repo"
    workspace.mkdir()
    _write_python_manifest(workspace, test_extra=["pytest", "httpx2"])
    (workspace / "requirements.txt").write_text("packaging\n", encoding="utf-8")

    manifest = read_manifest(workspace)

    assert manifest.ecosystem == "python"
    assert manifest.manifest_path == str(workspace / "pyproject.toml")
    assert manifest.declared_test_requirements == ["pytest", "httpx2"]
    assert manifest.requires_python == ">=3.10"
    assert any(path.endswith("requirements.txt") for path in manifest.lock_paths)


def test_preflight_records_the_interpreter_and_the_installed_package_set(
    tmp_path: Path,
) -> None:
    """(A) The interpreter and the installed package set are recorded."""

    from lh_harness.workspace_preflight import installed_packages, resolve_interpreter

    workspace = _healthy_workspace(tmp_path)

    interpreter = resolve_interpreter(workspace)
    packages = installed_packages(interpreter["executable"])

    assert interpreter["executable"], "the preflight must name an interpreter"
    assert interpreter["version"], "the preflight must record its version"
    assert interpreter["shared_harness_venv"] is False
    assert "pytest" in packages


def test_preflight_records_the_workspace_venv_interpreter_without_following_its_symlink(
    tmp_path: Path,
) -> None:
    """(A)(B) The recorded interpreter is the venv, not the base interpreter.

    A virtualenv's ``bin/python`` is normally a symlink to the base
    interpreter.  Resolving that symlink would name the *system* Python in
    the receipt and lose the fact that the interpreter carries the venv's own
    site-packages -- which is exactly the confusion that lets a run believe
    it has pytest when it does not.
    """

    from lh_harness.workspace_preflight import resolve_interpreter

    workspace = tmp_path / "venv-repo"
    workspace.mkdir()
    venv_dir = workspace / ".venv-task"
    bin_dir = venv_dir / "bin"
    bin_dir.mkdir(parents=True)
    # A symlinked interpreter, the way a real venv lays it out.
    (bin_dir / "python").symlink_to(Path(sys.executable))
    (venv_dir / "pyvenv.cfg").write_text(
        "home = /usr\ninclude-system-site-packages = false\nversion = 3.12.3\n",
        encoding="utf-8",
    )

    interpreter = resolve_interpreter(workspace)

    assert interpreter["source"] == "workspace_venv"
    assert interpreter["executable"] is not None
    assert str(venv_dir) in interpreter["executable"], (
        "the receipt must name the venv interpreter, not the base one"
    )
    assert interpreter["in_virtualenv"] is True
    assert interpreter["shared_harness_venv"] is False


def test_preflight_fails_fast_with_an_owned_prerequisite_when_a_declared_extra_is_missing(
    tmp_path: Path,
) -> None:
    """(E)(C) A missing declared extra fails fast and names its owner.

    The scenario is the exact shape of the incident that motivated the
    preflight: the workspace declares ``httpx2``, the interpreter cannot
    satisfy it, and the run must stop at a gate -- naming the owner --
    instead of burning round 1 discovering it.
    """

    from lh_harness.workspace_preflight import (
        PREREQUISITE_OWNER,
        run_preflight,
    )

    workspace = _fixture_without_test_extras(tmp_path)

    # A bare interpreter that has pytest but not httpx2: the scenario is
    # about the *unmeetable* dependency, which must become an owned
    # prerequisite rather than an install attempt.  No env prep and no
    # install, so the test can never reach the network.
    bare = _bare_interpreter(tmp_path)

    record = run_preflight(
        workspace,
        allow_env_prep=False,
        allow_install=False,
        interpreter=bare,
    )

    assert record.ok is False, "a missing declared extra must fail the gate"
    prerequisite = record.prerequisite
    assert prerequisite is not None, "the failure must be a typed prerequisite"
    assert prerequisite["owner"] == PREREQUISITE_OWNER
    assert "infra onboarding" in prerequisite["owner"]
    assert "httpx2" in prerequisite["message"], "the message names what is missing"
    assert prerequisite["code"] == "missing_declared_test_dependencies"
    # The dependency check itself was performed and recorded.
    assert record.dependency_imports.get("httpx2") is False


def test_preflight_fails_fast_when_the_only_interpreter_is_the_shared_harness_venv(
    tmp_path: Path,
) -> None:
    """(E)(B) A wrong interpreter fails fast.

    The shared harness venv is never a task environment: it is shared by
    every run on the host, so a per-run install into it corrupts other runs
    and hides a broken workspace behind another run's packages.  When it is
    the only interpreter available, the preflight must refuse it and fail
    with a typed prerequisite rather than proceeding.
    """

    from lh_harness import workspace_preflight
    from lh_harness.workspace_preflight import PREREQUISITE_OWNER, run_preflight

    workspace = _fixture_without_test_extras(tmp_path)

    # A fake shared harness venv: the path shape is what the refusal keys
    # on, so the scenario never touches the real shared venv.
    shared = tmp_path / "harness" / "venv" / "bin" / "python"
    shared.parent.mkdir(parents=True)
    shared.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    shared.chmod(0o755)

    # A "wrong" interpreter that cannot import the declared extras.
    wrong = tmp_path / "wrong" / "bin" / "python"
    wrong.parent.mkdir(parents=True)
    wrong.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    wrong.chmod(0o755)

    original_resolve = workspace_preflight.resolve_interpreter

    def _only_shared(_workspace, *, preferred=None):
        result = original_resolve(_workspace, preferred=preferred)
        result["executable"] = None
        result["version"] = None
        result["source"] = "shared_harness_venv_rejected"
        result["shared_harness_venv"] = True
        return result

    workspace_preflight.resolve_interpreter = _only_shared
    try:
        record = run_preflight(
            workspace,
            allow_env_prep=False,
            allow_install=False,
        )
    finally:
        workspace_preflight.resolve_interpreter = original_resolve

    assert record.ok is False, "a wrong interpreter must fail the gate"
    prerequisite = record.prerequisite
    assert prerequisite is not None
    assert prerequisite["owner"] == PREREQUISITE_OWNER
    assert prerequisite["code"] == "no_usable_interpreter"
    assert "shared harness" in prerequisite["message"]


def test_preflight_collects_real_tests_in_a_healthy_declared_environment(
    tmp_path: Path,
) -> None:
    """(E)(A) A healthy declared environment collects real tests.

    The count must be greater than zero: a *skipped* collection -- or a
    collection that collected nothing at all -- must never read as a healthy
    environment.
    """

    from lh_harness.workspace_preflight import run_preflight

    workspace = _healthy_workspace(tmp_path)

    record = run_preflight(
        workspace,
        allow_env_prep=False,
        allow_install=False,
    )

    assert record.ok is True, record.prerequisite
    assert record.collection["collected"] > 0, (
        "a healthy environment must collect real tests, not zero"
    )
    assert record.collection["ok"] is True
    assert record.dependency_imports.get("pytest") is True
    # The declared extras were actually checked, not assumed.
    assert set(record.dependency_imports) == {"pytest"}


def test_preflight_treats_zero_collected_tests_as_a_failure(
    tmp_path: Path,
) -> None:
    """(E)(A) Zero collected tests is never green.

    A repo that declares a ``test`` extra but collects nothing has a broken
    test environment; the preflight must fail it with a typed prerequisite
    rather than reporting a healthy environment.
    """

    from lh_harness.workspace_preflight import run_preflight

    workspace = tmp_path / "no-tests"
    workspace.mkdir()
    _write_python_manifest(workspace, test_extra=["pytest"])
    # No tests directory at all: the collection succeeds but collects zero.
    _git_init_workspace(workspace)

    record = run_preflight(
        workspace,
        allow_env_prep=False,
        allow_install=False,
    )

    assert record.ok is False, "zero collected tests must not be green"
    prerequisite = record.prerequisite
    assert prerequisite is not None
    assert prerequisite["code"] == "no_tests_collected"
    assert "0 tests" in prerequisite["message"]
    assert prerequisite["owner"] == "infra onboarding"


def test_preflight_records_an_unavailable_profile_tool_with_an_alternate_route(
    tmp_path: Path,
) -> None:
    """(E)(A) An unavailable profile tool is recorded truthfully.

    The record must say the tool is unavailable -- never assume it is
    present -- and must name the alternate route the executor should use
    instead.
    """

    from lh_harness.workspace_preflight import (
        profile_tool_availability,
        run_preflight,
    )

    workspace = _healthy_workspace(tmp_path)

    availability = profile_tool_availability()
    assert "graphify" in availability
    entry = availability["graphify"]
    assert isinstance(entry["available"], bool)
    if not entry["available"]:
        assert entry["alternate_route"], (
            "an unavailable tool must carry the alternate route"
        )
        assert "grep" in entry["alternate_route"]

    record = run_preflight(
        workspace,
        allow_env_prep=False,
        allow_install=False,
    )

    assert record.tool_availability.get("graphify") is not None
    tool_entry = record.tool_availability["graphify"]
    assert "available" in tool_entry
    assert "alternate_route" in tool_entry


def test_preflight_receipt_carries_the_contract_fields(
    tmp_path: Path,
) -> None:
    """(D) The receipt carries the five fields the contract names."""

    from lh_harness.workspace_preflight import (
        RECEIPT_FIELDS,
        receipt_from_record,
        run_preflight,
    )

    workspace = _healthy_workspace(tmp_path)

    receipt = receipt_from_record(run_preflight(workspace))

    for field in RECEIPT_FIELDS:
        assert field in receipt, f"the receipt must carry {field!r}"
    assert receipt["collected"] > 0
    assert receipt["env_manifest"]["ecosystem"] == "python"
    assert receipt["interpreter"]["executable"]


@pytest.mark.asyncio
async def test_the_run_writes_the_preflight_receipts_into_the_run_dir(
    tmp_path: Path,
) -> None:
    """(D) The run-dir record and the event both carry the receipt.

    The receipt file lands beside the role ledger, the ``workspace_preflight``
    event carries the same fields, and the ``role_harness_start`` event
    carries them too -- which is what the run snapshot projects for the
    overseer.
    """

    from lh_harness.manager import PREFLIGHT_RECEIPT_NAME

    workspace = _healthy_workspace(tmp_path)
    logs = tmp_path / "logs"

    config = HarnessConfig(
        max_total_episodes=1,
        workspace_path=str(workspace),
        harness_dir=str(tmp_path / "harness"),
        log_dir=str(logs),
    )
    await run(
        task="observe the workspace",
        env=LocalEnvironment(str(tmp_path / "tmp")),
        config=config,
        agent=DoneAgent(),
        resume=False,
    )

    role_dir = logs / "role_orchestration"
    receipt_path = role_dir / PREFLIGHT_RECEIPT_NAME
    assert receipt_path.is_file(), "the run dir must carry the preflight receipt"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["collected"] > 0
    assert receipt["interpreter"]["executable"]
    assert receipt["candidate_sha"] is not None
    assert "graphify" in receipt["tool_availability"]

    events = [
        json.loads(line)
        for line in (role_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    preflight_events = [item for item in events if item["event"] == "workspace_preflight"]
    assert preflight_events, "the ledger must carry the preflight event"
    assert preflight_events[-1]["collected"] == receipt["collected"]

    start = next(item for item in events if item["event"] == "role_harness_start")
    attached = start["workspace_preflight"]
    assert attached["collected"] == receipt["collected"]
    assert attached["env_manifest"] == receipt["env_manifest"]


@pytest.mark.asyncio
async def test_the_gate_fires_before_round_one_is_dispatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(C) The gate fires before any round is consumed.

    The fixture workspace declares a test extra the interpreter cannot
    satisfy, so the preflight must block the run before the loop dispatches
    round 1.  The report is blocked, the reason names the owner, and no
    round was run.
    """

    workspace = _fixture_without_test_extras(tmp_path)
    bare = _bare_interpreter(tmp_path)

    from lh_harness import workspace_preflight

    original_preflight = workspace_preflight.run_preflight

    def _pinned(workspace_path, **kwargs):
        # Pin the interpreter to the bare one so the scenario does not depend
        # on the host's package set, and forbid env prep so the test can
        # never reach the network.
        kwargs["allow_env_prep"] = False
        kwargs["allow_install"] = False
        kwargs["interpreter"] = bare
        return original_preflight(workspace_path, **kwargs)

    monkeypatch.setattr("lh_harness.manager.run_preflight", _pinned)

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

    assert report["rounds_run"] == 0, "the gate must fire before round 1"
    assert report["completion_satisfied"] is False
    assert report["abort_reason"] == "preflight_prerequisite_missing"
    assert "infra onboarding" in report["failure_reason"]
    assert "httpx2" in report["failure_reason"]

    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "role_orchestration" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert not [item for item in events if item["event"] == "round_start"], (
        "no round may be dispatched when the preflight blocks the run"
    )


@pytest.mark.asyncio
async def test_the_snapshot_projects_the_preflight_fields(
    tmp_path: Path,
) -> None:
    """(D) The run snapshot carries the same fields as the run-dir receipt."""

    from lh_harness.dashboard.state import DashboardState
    from lh_harness.webapi.snapshot import _safe_preflight, build_snapshot

    workspace = _healthy_workspace(tmp_path)
    logs = tmp_path / "logs"

    config = HarnessConfig(
        max_total_episodes=1,
        workspace_path=str(workspace),
        harness_dir=str(tmp_path / "harness"),
        log_dir=str(logs),
    )
    await run(
        task="observe the workspace",
        env=LocalEnvironment(str(tmp_path / "tmp")),
        config=config,
        agent=DoneAgent(),
        resume=False,
    )

    events = [
        json.loads(line)
        for line in (logs / "role_orchestration" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    start = next(item for item in events if item["event"] == "role_harness_start")

    projected = _safe_preflight(start["workspace_preflight"])

    assert projected["collected"] > 0
    assert projected["candidate_sha"]
    assert projected["env_manifest"]["ecosystem"] == "python"
    assert projected["interpreter"]["executable"]
    assert "graphify" in projected["tool_availability"]

    # The same fields must reach the assembled run snapshot, which is what
    # the overseer actually reads.
    state = DashboardState(log_dir=logs, task="observe the workspace")
    snapshot = build_snapshot(state)
    snap_preflight = snapshot["run"]["workspace_preflight"]
    assert snap_preflight["collected"] == projected["collected"]
    assert snap_preflight["collected"] > 0
    assert snap_preflight["candidate_sha"] == projected["candidate_sha"]
    assert snap_preflight["env_manifest"]["ecosystem"] == "python"
    assert snap_preflight["interpreter"]["executable"]
    assert "graphify" in snap_preflight["tool_availability"]
