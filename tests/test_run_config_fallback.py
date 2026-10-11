"""``run`` must find the node-level config when the workspace has none (CT110, 2026-10-11)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lh_harness import config as config_mod
from lh_harness.config import PROJECT_CONFIG_PATH, load_run_defaults, resolve_run_config_path


def _write(path: Path, manager: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"[run.timeouts]\nmanager = {manager}\n", encoding="utf-8")
    return path


def test_workspace_without_config_falls_back_to_runs_root_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = _write(tmp_path / "work" / ".lh-harness" / "config.toml", manager=600)
    workspace = tmp_path / "work" / "q2-repo"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.delenv(config_mod.ENV_PROJECT_CONFIG, raising=False)
    runs_root = str(tmp_path / "work" / ".lh-harness" / "runs")
    assert resolve_run_config_path(["run", f"--runs-root={runs_root}"]) == node
    assert resolve_run_config_path(["run", "--runs-root", runs_root]) == node
    defaults = load_run_defaults(resolve_run_config_path(["run", f"--runs-root={runs_root}"]))
    assert defaults["manager_timeout"] == 600


def test_workspace_config_wins_over_node_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path / "work" / ".lh-harness" / "config.toml", manager=600)
    workspace = tmp_path / "work" / "repo"
    _write(workspace / ".lh-harness" / "config.toml", manager=1200)
    monkeypatch.chdir(workspace)
    monkeypatch.delenv(config_mod.ENV_PROJECT_CONFIG, raising=False)
    runs_root = str(tmp_path / "work" / ".lh-harness" / "runs")
    assert resolve_run_config_path(["run", f"--runs-root={runs_root}"]) == PROJECT_CONFIG_PATH
    assert load_run_defaults(PROJECT_CONFIG_PATH)["manager_timeout"] == 1200


def test_explicit_env_path_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = _write(tmp_path / "elsewhere" / "config.toml", manager=777)
    workspace = tmp_path / "repo"
    _write(workspace / ".lh-harness" / "config.toml", manager=1200)
    monkeypatch.chdir(workspace)
    monkeypatch.setenv(config_mod.ENV_PROJECT_CONFIG, str(explicit))
    assert resolve_run_config_path(["run"]) == explicit


def test_load_run_defaults_without_a_path_uses_the_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The run command calls load_run_defaults() with no arguments; the child's
    # own argv carries --runs-root, which is where the node-level file lives.
    _write(tmp_path / "work" / ".lh-harness" / "config.toml", manager=600)
    workspace = tmp_path / "work" / "q2-repo"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.delenv(config_mod.ENV_PROJECT_CONFIG, raising=False)
    monkeypatch.setattr(
        config_mod.sys, "argv", ["lh-harness", "run", f"--runs-root={tmp_path / 'work' / '.lh-harness' / 'runs'}"]
    )
    assert load_run_defaults()["manager_timeout"] == 600


def test_nothing_found_keeps_relative_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(config_mod.ENV_PROJECT_CONFIG, raising=False)
    monkeypatch.delenv("LH_HARNESS_RUNS_ROOT", raising=False)
    missing = tmp_path / "nowhere" / "runs"
    assert resolve_run_config_path(["run", f"--runs-root={missing}"]) == PROJECT_CONFIG_PATH
    assert load_run_defaults(PROJECT_CONFIG_PATH) == {}
