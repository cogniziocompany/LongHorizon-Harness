"""easybutt0n-304: tests for scripts/deploy/retention/lhh_retention.py.

Every fixture is synthesized under pytest's tmp_path and LHH_WORK_ROOT is
pointed there before the module is (re)imported, so nothing here ever
touches the real /home/harness/work tree.  The module captures NOW and
APPLY at import time, so each scenario loads a fresh copy of the module
with sys.argv patched for that scenario (--apply present or absent), then
calls main() and asserts on the resulting filesystem state.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy" / "retention" / "lhh_retention.py"
DAY = 86400


def age(path, days):
    """os.utime a dir/file (and everything under it) back by ``days`` days."""
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp))
    if os.path.isdir(path) and not os.path.islink(path):
        for root, dirs, files in os.walk(path):
            for name in dirs + files:
                try:
                    os.utime(os.path.join(root, name), (stamp, stamp))
                except OSError:
                    pass


def run_retention(root, argv, capsys):
    """Import lhh_retention with LHH_WORK_ROOT=root and sys.argv=argv, run main()."""
    prev_env = os.environ.get("LHH_WORK_ROOT")
    prev_argv = sys.argv
    os.environ["LHH_WORK_ROOT"] = str(root)
    sys.argv = ["lhh_retention.py", *argv]
    try:
        sys.modules.pop("lhh_retention", None)
        spec = importlib.util.spec_from_file_location("lhh_retention", str(SCRIPT))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.main()
    finally:
        sys.argv = prev_argv
        if prev_env is None:
            os.environ.pop("LHH_WORK_ROOT", None)
        else:
            os.environ["LHH_WORK_ROOT"] = prev_env
    return capsys.readouterr().out


def git(path, *args):
    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


def make_git_copy(path, dirty):
    path.mkdir(parents=True)
    (path / "tracked.txt").write_text("committed\n")
    git(path, "init", "-q")
    git(path, "config", "user.name", "Retention Test")
    git(path, "config", "user.email", "retention-test@example.invalid")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-q", "-m", "initial commit")
    if dirty:
        (path / "tracked.txt").write_text("committed\nplus an uncommitted tracked change\n")


def delete_lines(out):
    return [line for line in out.splitlines() if line.startswith("DELETE ")]


def test_run_record_30d_deleted_29d_kept(tmp_path, capsys):
    runs = tmp_path / ".lh-harness" / "runs"
    old_run = runs / "20260101-000000-old"
    recent_run = runs / "20260120-000000-recent"
    old_run.mkdir(parents=True)
    recent_run.mkdir(parents=True)
    age(old_run, 40)
    age(recent_run, 29)

    out = run_retention(tmp_path, ["--apply"], capsys)

    assert not old_run.exists()
    assert recent_run.exists()
    assert len(delete_lines(out)) == 1
    assert "run-record>30d" in delete_lines(out)[0]
    assert str(old_run) in delete_lines(out)[0]


def test_live_run_named_by_queue_is_kept(tmp_path, capsys):
    runs = tmp_path / ".lh-harness" / "runs"
    queue = runs / "queue"
    live_run = runs / "20260101-000000-live"
    live_run.mkdir(parents=True)
    queue.mkdir(parents=True)
    (queue / "entry.json").write_text(
        json.dumps({"status": "launched", "run_id": "20260101-000000-live"})
    )
    age(live_run, 40)

    out = run_retention(tmp_path, ["--apply"], capsys)

    assert live_run.exists()
    assert all(str(live_run) not in line for line in delete_lines(out))
    assert "protected live runs 1" in out


def test_non_base_workspace_deleted_base_kept(tmp_path, capsys):
    stale_ws = tmp_path / "old-deploy-copy"
    base_ws = tmp_path / "LongHorizon-Harness"
    stale_ws.mkdir()
    base_ws.mkdir()
    (stale_ws / "file.txt").write_text("data\n")
    (base_ws / "file.txt").write_text("data\n")
    age(stale_ws, 20)
    age(base_ws, 20)

    out = run_retention(tmp_path, ["--apply"], capsys)

    assert not stale_ws.exists()
    assert base_ws.exists()
    assert len(delete_lines(out)) == 1
    assert "workspace>14d" in delete_lines(out)[0]
    assert str(stale_ws) in delete_lines(out)[0]


def test_dirty_git_copy_flagged_not_deleted(tmp_path, capsys):
    dirty_copy = tmp_path / "git-dirty-copy"
    make_git_copy(dirty_copy, dirty=True)
    age(dirty_copy, 20)

    out = run_retention(tmp_path, ["--apply"], capsys)

    assert dirty_copy.exists()
    assert (dirty_copy / "tracked.txt").exists()
    assert all(str(dirty_copy) not in line for line in delete_lines(out))
    flag_lines = [line for line in out.splitlines() if line.startswith("FLAG-KEEP")]
    assert len(flag_lines) == 1
    assert "uncommitted-changes" in flag_lines[0]
    assert str(dirty_copy) in flag_lines[0]


def test_dry_run_deletes_nothing(tmp_path, capsys):
    runs = tmp_path / ".lh-harness" / "runs"
    old_run = runs / "20260101-000000-old"
    old_run.mkdir(parents=True)
    (old_run / "record.json").write_text("{}")
    stale_ws = tmp_path / "old-workspace"
    stale_ws.mkdir()
    (stale_ws / "file.txt").write_text("data\n")
    loose = tmp_path / "old-loose.txt"
    loose.write_text("scratch\n")
    age(old_run, 40)
    age(stale_ws, 20)
    age(loose, 20)

    out = run_retention(tmp_path, [], capsys)

    assert old_run.exists()
    assert stale_ws.exists()
    assert loose.exists()
    assert delete_lines(out) == []
    assert any(line.startswith("WOULD-DELETE ") for line in out.splitlines())
    assert "would delete" in out


def test_loose_file_older_than_14d_deleted(tmp_path, capsys):
    loose = tmp_path / "old-notes.txt"
    loose.write_text("scratch\n")
    age(loose, 20)

    out = run_retention(tmp_path, ["--apply"], capsys)

    assert not loose.exists()
    assert len(delete_lines(out)) == 1
    assert "loose-file>14d" in delete_lines(out)[0]
    assert str(loose) in delete_lines(out)[0]
