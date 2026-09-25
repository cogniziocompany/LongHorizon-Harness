"""Launcher → CLI base-mode agreement tests (task 252).

The launcher's prelaunch guard resolves one base mode per launch and the
supervisor forwards it as ``--workspace-base-mode`` to ``lh-harness run``.
A mode the run parser rejects kills the worker at argparse before it writes
anything (measured 2026-09-25: run 20260925T053514Z_78483494 on a non-git
workspace died with
``invalid choice: 'not-a-repo' (choose from 'on-default', 'in-place',
'worktree', 'stash', 'continuation')`` and left no report.json).  These
tests derive the launcher's emittable mode set from its real derivation
logic — never from a hand-copied list of the CLI's current choices — and
assert every emittable value round-trips through the real ``lh-harness
run`` parser in ``main``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from lh_harness import cli
from lh_harness.workspace_guard import WORKSPACE_BASE_MODES, prepare_workspace_base

NO_PR = lambda repo, branch: None  # noqa: E731 - derivation probes never query PRs

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "mode-round-trip",
    "GIT_AUTHOR_EMAIL": "mode-round-trip@example.invalid",
    "GIT_COMMITTER_NAME": "mode-round-trip",
    "GIT_COMMITTER_EMAIL": "mode-round-trip@example.invalid",
}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=GIT_ENV
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout.strip()


def _make_repo(tmp_path: Path, name: str) -> Path:
    origin = tmp_path / f"{name}.origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True, capture_output=True, env=GIT_ENV,
    )
    repo = tmp_path / name
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, env=GIT_ENV)
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


def _feature_branch(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "feat/other-task")
    (repo / "feature.txt").write_text("work\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "work")
    _git(repo, "push", "-q", "-u", "origin", "feat/other-task")


def derive_launcher_emittable_modes(tmp_path: Path) -> set[str]:
    """Drive the REAL derivation logic over every workspace shape it handles.

    The emittable set is whatever :func:`prepare_workspace_base` returns for
    each workspace shape the launcher can hand it — proven here by running
    the guard, not by copying the CLI's choice list into this test.
    """

    modes: set[str] = set()
    label = 0

    def _probe(base):
        nonlocal label
        label += 1
        return prepare_workspace_base(base, run_label=f"rt-{label}", probe_open_pr=NO_PR).mode

    # 1. A plain directory with no .git (chat-test scratch, docs-only trees).
    plain = tmp_path / "plain"
    plain.mkdir()
    modes.add(_probe(plain))

    # 2. A clean repo already on its default branch.
    repo_default = _make_repo(tmp_path, "default")
    modes.add(_probe(repo_default))

    # 3. A clean, synced non-default branch -> switched in place.
    repo_in_place = _make_repo(tmp_path, "in-place")
    _feature_branch(repo_in_place)
    modes.add(_probe(repo_in_place))

    # 4. Foreign uncommitted work -> fresh linked worktree.
    repo_worktree = _make_repo(tmp_path, "worktree")
    _feature_branch(repo_worktree)
    (repo_worktree / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    _git(repo_worktree, "add", ".")
    modes.add(_probe(repo_worktree))

    # 5. Foreign work whose worktree cannot be created -> named stash.
    repo_stash = _make_repo(tmp_path, "stash")
    _feature_branch(repo_stash)
    (repo_stash / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    _git(repo_stash, "add", ".")
    sibling = repo_stash.parent / f"{repo_stash.name}.run-rt-{label + 1}"
    sibling.mkdir()
    (sibling / "occupied.bin").write_bytes(b"occupied")
    modes.add(_probe(repo_stash))

    # 6. The per-entry continuation opt-in returns the workspace unchanged.
    modes.add(
        prepare_workspace_base(
            repo_default,
            run_label="rt-continuation",
            probe_open_pr=NO_PR,
            continuation=True,
        ).mode
    )

    return modes


def test_launcher_emittable_modes_cover_the_shared_constant(tmp_path: Path) -> None:
    """Every mode the launcher can actually emit is in WORKSPACE_BASE_MODES."""

    derived = derive_launcher_emittable_modes(tmp_path)
    unknown = derived - set(WORKSPACE_BASE_MODES)
    assert not unknown, (
        f"the launcher emits base mode(s) outside WORKSPACE_BASE_MODES: {sorted(unknown)}"
    )
    assert derived == set(WORKSPACE_BASE_MODES), (
        "WORKSPACE_BASE_MODES and the guard's emittable set drifted apart "
        f"(guard-only: {sorted(set(WORKSPACE_BASE_MODES) - derived)})"
    )


def _parse_via_main(monkeypatch: pytest.MonkeyPatch, argv: list[str]):
    """Parse ``argv`` through the real ``lh-harness run`` parser in main()."""

    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_run_defaults", lambda: {})

    def fake_run_command(args) -> int:
        captured["args"] = args
        return 0

    monkeypatch.setattr(cli, "_run_command", fake_run_command)
    monkeypatch.setattr(cli, "PROJECT_CONFIG_PATH", Path("/nonexistent/.lh-harness/config.toml"))
    exit_code = cli.main(argv)
    return exit_code, captured.get("args")


@pytest.mark.parametrize("mode", sorted(WORKSPACE_BASE_MODES))
def test_every_guard_mode_round_trips_through_the_run_parser(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """``--workspace-base-mode=<mode>`` parses for every mode the guard emits."""

    exit_code, args = _parse_via_main(
        monkeypatch, ["run", "--task=t", f"--workspace-base-mode={mode}"]
    )
    assert exit_code == 0
    assert args is not None and args.workspace_base_mode == mode


def test_a_mode_outside_the_guard_set_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A value outside the guard's set still dies at argparse with exit 2."""

    with pytest.raises(SystemExit) as excinfo:
        _parse_via_main(
            monkeypatch, ["run", "--task=t", "--workspace-base-mode=on-fire"]
        )
    assert excinfo.value.code == 2
