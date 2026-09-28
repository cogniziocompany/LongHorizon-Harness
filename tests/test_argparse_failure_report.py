"""report.json is written when the ``run`` CLI dies on argparse (task 252).

Measured run 20260925T053514Z_78483494: the supervisor's worker argv carried
``--workspace-base-mode=not-a-repo``, argparse rejected it, the worker exited
2, and *nothing* was written — so the queue showed the bare "run failed"
instead of the actual cause.  These tests drive the real ``cli.main`` failure
path (argparse death inside ``main``, not a fabricated report) and assert the
durable ``report.json`` carries the real argparse message, at the same
location every other failure-path writer uses.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import lh_harness.cli as cli


@pytest.fixture
def fresh_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the CLI at a tmp project config location with no project file."""

    monkeypatch.setattr(cli, "PROJECT_CONFIG_PATH", tmp_path / "absent" / "config.toml")
    monkeypatch.setattr(cli, "load_run_defaults", lambda *a, **k: {})
    return tmp_path


def _run_argv(runs_root: Path, run_id: str, mode: str) -> list[str]:
    """The supervisor's real worker argv shape (``--flag=value`` throughout)."""

    return [
        "run",
        "--task=probe the failure path",
        "--agent=codex",
        f"--runs-root={runs_root}",
        f"--run-id={run_id}",
        "--max-rounds=1",
        "--no-dashboard",
        "--supervised",
        f"--workspace-base-mode={mode}",
    ]


def _read_report(runs_root: Path, run_id: str) -> dict:
    path = runs_root / run_id / "lh_harness" / "report.json"
    assert path.is_file(), f"expected report.json at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_argparse_death_writes_report_with_the_real_error(
    fresh_config: Path, capsys
) -> None:
    """The incident shape: a rejected choice value still leaves the cause."""

    runs_root = fresh_config / "runs"
    run_id = "20260925T000000Z_argdead1"
    argv = _run_argv(runs_root, run_id, "not-a-repo-was-never-accepted")

    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)

    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err  # stderr output unchanged

    report = _read_report(runs_root, run_id)
    assert report["status"] == "failed"
    assert report["completion_satisfied"] is False
    assert report["abort_reason"] == "argparse_failure"
    assert "--workspace-base-mode" in report["error"]
    assert "invalid choice" in report["error"]
    assert report["task"] == "probe the failure path"
    assert report["rounds_run"] == 0


def test_argparse_death_report_carries_the_exact_rejected_value(
    fresh_config: Path,
) -> None:
    """The queued cause names the offending value, not a generic exit."""

    runs_root = fresh_config / "runs"
    run_id = "20260925T000000Z_argdead2"

    with pytest.raises(SystemExit):
        cli.main(_run_argv(runs_root, run_id, "bogus-mode"))

    report = _read_report(runs_root, run_id)
    assert "bogus-mode" in report["error"]


def test_role_side_report_is_written_for_dashboard_reads(
    fresh_config: Path,
) -> None:
    """The role-side copy the dashboard's read_report() scans also exists."""

    runs_root = fresh_config / "runs"
    run_id = "20260925T000000Z_argdead3"

    with pytest.raises(SystemExit):
        cli.main(_run_argv(runs_root, run_id, "bogus-mode"))

    role_report = (
        runs_root / run_id / "lh_harness" / "role_orchestration" / "report.json"
    )
    assert role_report.is_file()
    assert json.loads(role_report.read_text(encoding="utf-8"))["status"] == "failed"


def test_argparse_death_without_a_run_id_writes_nothing(fresh_config: Path) -> None:
    """No attached run reservation means nothing to report onto."""

    runs_root = fresh_config / "runs"

    with pytest.raises(SystemExit):
        cli.main(["run", "--task=t", "--workspace-base-mode=bogus-mode"])

    assert not (runs_root / "lh_harness").exists()
    assert not runs_root.exists() or not any(runs_root.iterdir())


def test_existing_report_is_never_overwritten(fresh_config: Path) -> None:
    """A rejected run id may name an older reservation; its report is truth."""

    runs_root = fresh_config / "runs"
    run_id = "20260925T000000Z_argdead4"
    log_dir = runs_root / run_id / "lh_harness"
    log_dir.mkdir(parents=True)
    (log_dir / "report.json").write_text(
        json.dumps({"schema_version": 2, "status": "completed"}),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit):
        cli.main(_run_argv(runs_root, run_id, "bogus-mode"))

    assert json.loads((log_dir / "report.json").read_text(encoding="utf-8")) == {
        "schema_version": 2,
        "status": "completed",
    }