"""TASK 218: fixture-based tests for lh_harness.validate_queue.

Every test runs against an in-repo fixture queue built in tmp_path.  No test may
touch the live queue: the module's QUEUE_DIR/HARNESS_SRC are pinned to fixture
paths for the duration of each test, and module-level findings state is reset
between tests because the prototype accumulates findings in a global list.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import lh_harness.validate_queue as vq

SRC_DIR = Path(vq.__file__).resolve().parent


def _write_entry(queue: Path, filename: str, payload: dict) -> Path:
    path = queue / filename
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    q = tmp_path / "queue"
    (q / "blocked").mkdir(parents=True)
    (q / "done").mkdir(parents=True)
    return q


@pytest.fixture
def harness_src(tmp_path: Path) -> Path:
    """A minimal harness-source dir so rule 4 runs instead of skipping."""
    src = tmp_path / "harness_src"
    src.mkdir()
    (src / "code.py").write_text('X = "LH_HARNESS_DB_PASSWORD"\n', encoding="utf-8")
    return src


@pytest.fixture
def run(queue, harness_src, monkeypatch):
    """Run the validator against fixture dirs; returns (exit_code, findings snapshot)."""

    def _run() -> tuple[int, list[tuple[str, str, str]], str]:
        monkeypatch.setattr(vq, "QUEUE", str(queue))
        monkeypatch.setattr(vq, "HARNESS_SRC", str(harness_src))
        vq.findings.clear()
        code = vq.main()
        return code, list(vq.findings)

    return _run


def _rules(rules_and_sevs, findings):
    return {(sev, rule) for sev, rule, _ in findings if (sev, rule) in rules_and_sevs}


# ---- rule 1: duplicate-task-number ----
def test_duplicate_task_number_is_error(run, queue):
    _write_entry(queue, "strand-201-alpha.json", {"name": "strand-201-alpha", "workspace": "w", "max_rounds": 1})
    _write_entry(queue, "strand-201-beta.json", {"name": "strand-201-beta", "workspace": "w", "max_rounds": 1})
    code, findings = run()
    assert ("ERROR", "duplicate-task-number") in {(s, r) for s, r, _ in findings}
    assert code == 1


# ---- rule 2: task-file-not-found / missing-task-file-field ----
def test_task_file_not_found_is_error(run, queue):
    _write_entry(
        queue,
        "202-file-check.json",
        {"name": "202-file-check", "workspace": "w", "max_rounds": 1,
         "task_source": "task_file", "task_file": "C:/definitely/not/here.md"},
    )
    code, findings = run()
    assert ("ERROR", "task-file-not-found") in {(s, r) for s, r, _ in findings}
    assert code == 1


def test_task_source_without_task_file_is_error(run, queue):
    _write_entry(
        queue,
        "203-file-check.json",
        {"name": "203-file-check", "workspace": "w", "max_rounds": 1, "task_source": "task_file"},
    )
    code, findings = run()
    assert ("ERROR", "missing-task-file-field") in {(s, r) for s, r, _ in findings}
    assert code == 1


def test_existing_task_file_is_clean(run, queue, tmp_path):
    task_file = tmp_path / "task.md"
    task_file.write_text("do the thing", encoding="utf-8")
    _write_entry(
        queue,
        "204-file-check.json",
        {"name": "204-file-check", "workspace": "w", "max_rounds": 1,
         "task_source": "task_file", "task_file": str(task_file)},
    )
    code, findings = run()
    assert not findings
    assert code == 0


# ---- rule 3: blocker-target-does-not-exist / blocker-may-be-satisfied ----
def test_blocker_target_dangling_is_error(run, queue):
    _write_entry(
        queue,
        "205-blocked.json",
        {"name": "205-blocked", "workspace": "w", "max_rounds": 1,
         "note": "BLOCKED ON TASK 999 which does not exist"},
    )
    code, findings = run()
    assert ("ERROR", "blocker-target-does-not-exist") in {(s, r) for s, r, _ in findings}
    assert code == 1


def test_blocker_in_done_is_warn_only_exit_zero(run, queue):
    (queue / "done" / "fleet-998-done-entry.json").write_text("{}", encoding="utf-8")
    _write_entry(
        queue,
        "206-blocked.json",
        {"name": "206-blocked", "workspace": "w", "max_rounds": 1,
         "note": "BLOCKED ON TASK 998 which has landed"},
    )
    code, findings = run()
    assert ("WARN", "blocker-may-be-satisfied") in {(s, r) for s, r, _ in findings}
    # HARD RULE: exit non-zero only on ERROR - warn-only must exit 0.
    assert code == 0


# ---- rule 4: env-name-not-in-source (+ env-check-skipped when source is missing) ----
def test_env_name_not_in_source_is_warn(run, queue, harness_src, monkeypatch):
    (harness_src / "code.py").write_text('X = "LH_HARNESS_DB_PASSWORD"\n', encoding="utf-8")
    task_file = harness_src.parent / "task.md"
    task_file.write_text("set LH_HARNESS_DATABASE_URL for the run", encoding="utf-8")
    _write_entry(
        queue,
        "207-env-check.json",
        {"name": "207-env-check", "workspace": "w", "max_rounds": 1, "task_file": str(task_file)},
    )
    code, findings = run()
    assert ("WARN", "env-name-not-in-source") in {(s, r) for s, r, _ in findings}
    assert ("ERROR", "env-name-not-in-source") not in {(s, r) for s, r, _ in findings}
    assert code == 0


def test_env_check_skipped_when_no_source(run, queue, harness_src):
    shutil.rmtree(harness_src)  # no harness source -> rule 4 must skip with a WARN, not fail
    _write_entry(queue, "208-env-check.json", {"name": "208-env-check", "workspace": "w", "max_rounds": 1})
    code, findings = run()
    assert ("WARN", "env-check-skipped") in {(s, r) for s, r, _ in findings}
    assert code == 0


# ---- rule 5: schema ----
def test_missing_required_field_is_error(run, queue):
    _write_entry(queue, "209-schema.json", {"name": "209-schema"})  # no workspace/max_rounds
    code, findings = run()
    assert ("ERROR", "missing-required-field") in {(s, r) for s, r, _ in findings}
    assert code == 1


def test_unknown_trio_is_error(run, queue):
    _write_entry(
        queue,
        "210-schema.json",
        {"name": "210-schema", "workspace": "w", "max_rounds": 1, "trio": "gpt"},
    )
    code, findings = run()
    assert ("ERROR", "unknown-trio") in {(s, r) for s, r, _ in findings}
    assert code == 1


def test_name_filename_mismatch_is_warn_only(run, queue):
    _write_entry(
        queue,
        "747-114-fleet-heartbeat-413.json",
        {"name": "fleet-heartbeat-413", "workspace": "w", "max_rounds": 1},
    )
    code, findings = run()
    assert ("WARN", "name-filename-mismatch") in {(s, r) for s, r, _ in findings}
    assert code == 0


def test_continuation_without_branch_is_error(run, queue):
    _write_entry(
        queue,
        "211-continuation.json",
        {"name": "211-continuation", "workspace": "w", "max_rounds": 1, "continue_branch": True},
    )
    code, findings = run()
    assert ("ERROR", "continuation-without-branch") in {(s, r) for s, r, _ in findings}
    assert code == 1


def test_continue_branch_with_branch_is_clean(run, queue):
    _write_entry(
        queue,
        "212-continuation.json",
        {"name": "212-continuation", "workspace": "w", "max_rounds": 1,
         "continue_branch": True, "branch": "feat/some-branch"},
    )
    code, findings = run()
    assert findings == []
    assert code == 0


# ---- unparseable entry ----
def test_unparseable_entry_is_error(run, queue):
    (queue / "213-broken.json").write_text("{not json", encoding="utf-8")
    code, findings = run()
    assert ("ERROR", "unparseable-entry") in {(s, r) for s, r, _ in findings}
    assert code == 1


# ---- HARD RULE: no writes anywhere in the code path ----
def test_validator_never_writes_to_queue(run, queue, monkeypatch):
    _write_entry(queue, "214-clean.json", {"name": "214-clean", "workspace": "w", "max_rounds": 1})
    (queue / "done" / "fleet-997-done.json").write_text("{}", encoding="utf-8")
    (queue / "blocked" / "215-blocked.json").write_text("{}", encoding="utf-8")

    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in queue.rglob("*") if p.is_file()}

    # Fail loudly if anything opens a queue path for writing during the run.
    real_open = open

    def guarded_open(file, *a, **kw):
        mode = a[0] if a else kw.get("mode", "r")
        try:
            resolved = Path(file).resolve()
        except (TypeError, ValueError, OSError):
            return real_open(file, *a, **kw)
        if str(resolved).startswith(str(queue.resolve())) and any(c in str(mode) for c in "wax+"):
            pytest.fail(f"validator opened queue file for writing: {resolved} mode={mode!r}")
        return real_open(file, *a, **kw)

    monkeypatch.setattr("builtins.open", guarded_open)
    code, _ = run()

    after = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in queue.rglob("*") if p.is_file()}
    assert before == after
    # No new files (e.g. seen-findings state) appeared either.
    assert set(before) == set(after)


# ---- HARD RULE: no secret values in output, env-var names only ----
def test_output_contains_no_secret_values(run, queue, capsys, monkeypatch):
    secret_value = "super-secret-value-42"
    monkeypatch.setenv("LH_HARNESS_DB_PASSWORD", secret_value)
    task_file = queue.parent / "task.md"
    task_file.write_text("set LH_HARNESS_DB_PASSWORD to the right value", encoding="utf-8")
    _write_entry(
        queue,
        "216-secrets.json",
        {"name": "216-secrets", "workspace": "w", "max_rounds": 1, "task_file": str(task_file)},
    )
    code, findings = run()
    out = capsys.readouterr().out
    # The env-var NAME may appear; its VALUE must not.
    assert secret_value not in out
    assert "LH_HARNESS_DB_PASSWORD" in out or code == 0
    for _, _, msg in findings:
        assert secret_value not in msg


# ---- console entry point behaves the same as in-process main ----
def test_module_entry_point_exit_semantics(tmp_path: Path):
    queue = tmp_path / "queue"
    queue.mkdir(parents=True)
    (queue / "blocked").mkdir()
    (queue / "done").mkdir()
    _write_entry(queue, "217-clean.json", {"name": "217-clean", "workspace": "w", "max_rounds": 1})
    env = {
        "QUEUE_DIR": str(queue),
        "HARNESS_SRC": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        # Point the child interpreter at this checkout's src tree so it imports
        # the same module the tests do, not a possibly-stale installed copy.
        "PYTHONPATH": str(SRC_DIR.parent),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "lh_harness.validate_queue"],
        capture_output=True, text=True, env=env, cwd=str(SRC_DIR.parent.parent),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 live entries checked" in proc.stdout