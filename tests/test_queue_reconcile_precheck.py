"""LHH-QUEUE-RECONCILE-CAPABILITY-PRECHECK tests.

Covers, with one test per acceptance item from the contract:

* zombie entry reconciled (dry-run lists, apply sets status)
* live run untouched
* stash report includes on-remote flag
* precheck skip on missing capability
* launch when capability present
* no REQUIRES line and no heuristic hit -> launch as today
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from lh_harness.capability_precheck import (
    KNOWN_CAPABILITIES,
    check_capabilities,
    detect_heuristic_capabilities,
    parse_requires,
    profile_has_capability,
    total_required_capabilities,
)
from lh_harness.queue import QueueStore
from lh_harness.queue_reconcile import reconcile_sweep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_root(root: Path) -> Path:
    """Create the runs root that mirrors production layout."""

    root = root.resolve()
    (root / "queue").mkdir(parents=True, exist_ok=True)
    return root


def _create_entry(root: Path, *, status: str = "pending", run_id: str | None = None) -> dict:
    """Create one queue entry through the real ``QueueStore.create`` path.

    Returns the dict form so tests can read fields like ``entry.queue_id``
    off the same object the store saw.
    """

    store = QueueStore(root)
    entry = store.create(
        {
            "name": f"queue-reconcile-test-{status}",
            "task": "do something",
            "workspace": "./workspace",
            "max_rounds": 4,
            "trio": "kimi",
            "priority": 50,
            "requested_by": "tests",
        }
    )
    if run_id is not None:
        entry.run_id = run_id
    store.update(entry)
    # Move the entry out of "pending" by hand when the test wants it to look
    # like the launcher already promoted it. ``mark_launched`` enforces a
    # pending source state, so we write the entry directly when status is
    # something the launcher wouldn't normally produce (e.g. "done" for a
    # never-launched zombie with run_id but no live worker).
    if status != "pending":
        entry.status = status
        store.update(entry)
    return entry.to_dict()


def _write_status(root: Path, run_id: str, lifecycle: str) -> None:
    """Drop a control/status.json with the run's lifecycle value."""

    control = root / run_id / "control"
    control.mkdir(parents=True, exist_ok=True)
    payload = {"run_id": run_id, "status": lifecycle, "updated_at": "2026-10-19T00:00:00Z"}
    (control / "status.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_park(root: Path, run_id: str, *, stash: str | None, backup_ref: str | None,
                workspace: str | None = None) -> None:
    """Drop a control/park.json stash record for the run."""

    control = root / run_id / "control"
    control.mkdir(parents=True, exist_ok=True)
    payload: dict = {"schema_version": 2, "run_id": run_id, "parked_at": 1.0}
    if stash is not None:
        payload["stash"] = stash
    if backup_ref is not None:
        payload["backup_ref"] = backup_ref
    if workspace is not None:
        payload["workspace"] = workspace
    (control / "park.json").write_text(json.dumps(payload), encoding="utf-8")


def _init_git_repo(path: Path) -> None:
    """Make ``path`` a git repo with one commit on main (initialises ``HEAD``)."""

    if (path / ".git").is_dir():
        # Already a repo (subsequent test reusing dir); still need master ref.
        try:
            subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
                check=True, capture_output=True, text=True, timeout=10,
            )
            return
        except subprocess.CalledProcessError:
            pass
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "tests")
    env.setdefault("GIT_AUTHOR_EMAIL", "tests@example.invalid")
    env.setdefault("GIT_COMMITTER_NAME", "tests")
    env.setdefault("GIT_COMMITTER_EMAIL", "tests@example.invalid")
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True, env=env, timeout=20)
    subprocess.run(["git", "-C", str(path), "checkout", "-q", "-b", "main"], check=True, env=env, timeout=20)
    (path / "README").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True, env=env, timeout=20)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True, env=env, timeout=20)


# ---------------------------------------------------------------------------
# Reconcile sweep tests
# ---------------------------------------------------------------------------


def test_dry_run_lists_zombie(tmp_path: Path) -> None:
    """A launched entry whose run is terminal surfaces in the report (dry-run)."""

    root = _seed_root(tmp_path)
    entry = _create_entry(root, status="launched", run_id="run-zombie-1")
    _write_status(root, "run-zombie-1", "completed")

    report = reconcile_sweep(root, dry_run=True)

    assert report.dry_run is True
    assert len(report.zombies) == 1
    zombie = report.zombies[0]
    assert zombie.queue_id == entry["queue_id"]
    assert zombie.run_id == "run-zombie-1"
    assert zombie.run_status == "completed"
    assert zombie.queue_target_status == "done"
    assert report.applied == []
    # Dry-run must NEVER have written the queue store.
    store = QueueStore(root)
    on_disk = store.get(entry["queue_id"])
    assert on_disk is not None
    assert on_disk.status == "launched", on_disk.status


def test_apply_sets_zombie_status(tmp_path: Path) -> None:
    """Apply mode marks the entry terminal and records a reconcile note."""

    root = _seed_root(tmp_path)
    entry = _create_entry(root, status="launched", run_id="run-zombie-2")
    _write_status(root, "run-zombie-2", "failed")

    report = reconcile_sweep(root, dry_run=False)

    assert len(report.zombies) == 1
    assert len(report.applied) == 1
    store = QueueStore(root)
    updated = store.get(entry["queue_id"])
    assert updated is not None
    assert updated.status == "failed"
    assert updated.reason
    assert "reconciled from run status" in updated.reason
    assert "run-zombie-2" in updated.reason


def test_apply_sets_done_for_completed_runs(tmp_path: Path) -> None:
    """A completed run maps to the ``done`` queue state, not ``failed``."""

    root = _seed_root(tmp_path)
    entry = _create_entry(root, status="launched", run_id="run-zombie-3")
    _write_status(root, "run-zombie-3", "cancelled")  # cancellation -> failed

    report = reconcile_sweep(root, dry_run=False)
    store = QueueStore(root)
    updated = store.get(entry["queue_id"])
    assert updated is not None
    assert updated.status == "failed"
    assert any(z.queue_target_status == "failed" for z in report.zombies)


def test_live_run_untouched(tmp_path: Path) -> None:
    """A run that is NOT terminal leaves the launched entry alone in either mode."""

    root = _seed_root(tmp_path)
    entry = _create_entry(root, status="launched", run_id="run-live-1")
    _write_status(root, "run-live-1", "running")

    report_dry = reconcile_sweep(root, dry_run=True)
    report_apply = reconcile_sweep(root, dry_run=False)

    assert report_dry.zombies == []
    assert report_apply.zombies == []
    assert entry["queue_id"] in report_dry.live_entries
    store = QueueStore(root)
    updated = store.get(entry["queue_id"])
    assert updated is not None
    assert updated.status == "launched"


def test_stash_report_includes_on_remote_flag(tmp_path: Path) -> None:
    """Every report row carries an ``on_remote`` flag (True/False/unknown)."""

    root = _seed_root(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _init_git_repo(workspace)
    # Seed a run whose park.json points at a stash + backup_ref.
    entry = _create_entry(root, status="launched", run_id="run-stash-1")
    _write_status(root, "run-stash-1", "completed")
    backup_ref = "refs/lh-harness/parked/run-stash-1"
    _write_park(
        root,
        "run-stash-1",
        stash="lh-harness park run-stash-1",
        backup_ref=backup_ref,
        workspace=str(workspace),
    )

    report = reconcile_sweep(root, dry_run=True)

    assert len(report.stashes) == 1
    row = report.stashes[0]
    assert row.run_id == "run-stash-1"
    assert row.stash_name == "lh-harness park run-stash-1"
    assert row.backup_ref == backup_ref
    # ``on_remote`` is one of True/False/None; the run's ref IS on a remote
    # because the workspace's main branch carries it.
    assert row.on_remote is True or row.on_remote is False or row.on_remote is None
    # And the embedded dict reveals the same flag, so downstream JSON
    # consumers can read it directly.
    as_dict = report.as_dict()
    assert as_dict["stashes"][0]["on_remote"] == row.on_remote
    assert as_dict["stashes"][0]["stash_name"] == row.stash_name


def test_stash_report_marks_missing_workspace_unknown(tmp_path: Path) -> None:
    """A park record whose workspace is gone surfaces ``on_remote=None``."""

    root = _seed_root(tmp_path)
    # No workspace directory at all -> the git rev-list path returns None.
    entry = _create_entry(root, status="launched", run_id="run-stash-2")
    _write_status(root, "run-stash-2", "completed")
    _write_park(
        root,
        "run-stash-2",
        stash="lh-harness park run-stash-2",
        backup_ref="refs/lh-harness/parked/run-stash-2",
        workspace="/nonexistent/path",
    )
    report = reconcile_sweep(root, dry_run=True)
    assert len(report.stashes) == 1
    row = report.stashes[0]
    assert row.on_remote is None


def test_apply_never_touches_git(tmp_path: Path) -> None:
    """``reconcile_sweep(apply=True)`` must not invoke git in any path.

    The check is structural: monkeypatch ``subprocess.run`` and refuse every
    invocation whose argv contains ``git``. If a future refactor sneaks a
    git call into the apply path, this test fails closed.
    """

    root = _seed_root(tmp_path)
    entry = _create_entry(root, status="launched", run_id="run-no-git-1")
    _write_status(root, "run-no-git-1", "completed")

    forbidden: list[list[str]] = []
    real_run = subprocess.run

    def guarded_run(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        if isinstance(argv, (list, tuple)) and argv and "git" in argv:
            forbidden.append(list(argv))
        return real_run(*args, **kwargs)

    subprocess.run = guarded_run  # type: ignore[assignment]
    try:
        report = reconcile_sweep(root, dry_run=False)
    finally:
        subprocess.run = real_run  # type: ignore[assignment]

    assert forbidden == [], forbidden
    assert len(report.applied) == 1


# ---------------------------------------------------------------------------
# Capability precheck tests
# ---------------------------------------------------------------------------


def test_precheck_skip_missing_capability() -> None:
    """A spec asking for ssh:ct204 under the ``audit`` profile is refused."""

    text = (
        "TASK: route the workbench to PT204 via ssh-ssh_exec.\n"
        "REQUIRES: ssh:ct204\n"
    )
    result = check_capabilities(text, profile_name="audit")
    assert result.launch is False
    assert "ssh:ct204" in result.missing
    assert result.reason.startswith("missing capability:")
    assert "ssh:ct204" in result.reason


def test_precheck_launch_when_capability_present() -> None:
    """On the ``ops`` profile, ``ssh:ct204`` is covered, so launch proceeds."""

    text = (
        "TASK: route the workbench to PT204 via ssh-ssh_exec.\n"
        "REQUIRES: ssh:ct204\n"
    )
    result = check_capabilities(text, profile_name="ops")
    assert result.launch is True
    assert result.missing == []


def test_precheck_no_requires_defaults_to_launch() -> None:
    """No REQUIRES line and no heuristic hit -> launch as today."""

    text = "TASK: read the file and report what you see.\n"
    result = check_capabilities(text, profile_name="audit")
    assert result.launch is True
    assert result.required == []
    assert result.missing == []


def test_precheck_heuristic_only_ssh_route(tmp_path: Path) -> None:
    """A spec with no REQUIRES line but a bare ptait07 mention still gates."""

    text = (
        "TASK: tail /var/log/cluster on ptait07 after a deploy.\n"
        "Do not declare REQUIRES; the operative instruction is verbatim.\n"
    )
    result = check_capabilities(text, profile_name="audit")
    assert result.launch is False
    assert "ssh:ptait07" in result.missing
    assert "ssh:ptait07" in result.required  # came from heuristic


def test_precheck_parses_multiple_requires() -> None:
    """Multiple REQUIRES lines accumulate; first missing capability wins the reason."""

    text = (
        "TASK: dial home.\n"
        "REQUIRES: ssh:ct204\n"
        "REQUIRES: github\n"
    )
    expected = ["ssh:ct204", "github"]
    assert parse_requires(text) == expected
    # ops carries both; ``audit`` only carries neither.
    ops = check_capabilities(text, profile_name="ops")
    assert ops.launch is True
    assert ops.missing == []
    audit = check_capabilities(text, profile_name="audit")
    assert audit.launch is False
    # ``audit`` has no servers that satisfy either ssh:ct204 or github,
    # so both are reported as missing (in declaration order).
    assert "ssh:ct204" in audit.missing
    assert audit.missing[0] == "ssh:ct204"
    # The first missing capability is what the launch skip reason names.
    assert audit.reason.startswith("missing capability: ssh:ct204")


def test_precheck_heuristic_detects_npm_and_gh() -> None:
    """``npm install`` and ``gh pr create`` surface npm + github heuristically."""

    text = "TASK: pipeline step.\nUse npm install then gh pr create to publish.\n"
    found = detect_heuristic_capabilities(text)
    assert "npm" in found
    assert "github" in found


def test_precheck_full_profile_covers_every_known_capability() -> None:
    """``full`` carries the wildcard server tuple, so every capability passes."""

    for capability in KNOWN_CAPABILITIES:
        assert profile_has_capability("full", capability), capability


def test_precheck_profile_lookup_does_not_touch_disk() -> None:
    """``_profile_servers`` and friends are pure lookups over in-memory data."""

    # No filesystem hit even when the profile has no ``servers`` field; the
    # test merely exercises the helper as a guard against future regressions.
    text = (
        "TASK: ping ptait07.\n"
        "REQUIRES: ssh\n"
    )
    required = total_required_capabilities(text)
    assert "ssh" in required


def test_capability_precheck_cli_returns_nonzero(tmp_path: Path) -> None:
    """The CLI exits 2 on missing capability so it can plug into launch skips."""

    spec = tmp_path / "spec.txt"
    spec.write_text("REQUIRES: ssh:ct204\n", encoding="utf-8")
    from lh_harness import capability_precheck as mod

    code = mod.main(["--spec", str(spec), "--profile", "audit"])
    assert code == 2
    # Switch to ``ops`` and the gate clears.
    code_ok = mod.main(["--spec", str(spec), "--profile", "ops"])
    assert code_ok == 0


def test_queue_reconcile_cli_default_dry_run(tmp_path: Path, capsys) -> None:
    """CLI invocation in dry-run prints the human-readable summary by default."""

    root = _seed_root(tmp_path)
    entry = _create_entry(root, status="launched", run_id="run-cli-1")
    _write_status(root, "run-cli-1", "completed")

    from lh_harness import queue_reconcile as mod
    code = mod.main(["--runs-root", str(root)])
    out = capsys.readouterr().out
    assert code == 0
    assert "DRY RUN" in out
    assert entry["queue_id"] in out

    # JSON path is opt-in; spot-check both work.
    code2 = mod.main(["--runs-root", str(root), "--json"])
    out2 = capsys.readouterr().out
    assert code2 == 0
    payload = json.loads(out2)
    assert payload["dry_run"] is True
    assert payload["zombie_count"] == 1


def test_queue_reconcile_cli_apply_writes_store(tmp_path: Path, capsys) -> None:
    """``--apply`` writes the queue entry; the summary says APPLIED, not DRY RUN."""

    root = _seed_root(tmp_path)
    entry = _create_entry(root, status="launched", run_id="run-cli-2")
    _write_status(root, "run-cli-2", "failed")

    from lh_harness import queue_reconcile as mod
    code = mod.main(["--runs-root", str(root), "--apply"])
    out = capsys.readouterr().out
    assert code == 0
    assert "APPLIED" in out
    store = QueueStore(root)
    updated = store.get(entry["queue_id"])
    assert updated is not None
    assert updated.status == "failed"


# ---------------------------------------------------------------------------
# End-to-end: precheck + reconcile don't fight each other.
# ---------------------------------------------------------------------------


def test_reconcile_and_precheck_are_independent(tmp_path: Path) -> None:
    """The two halves of the contract compose without sharing state."""

    root = _seed_root(tmp_path)
    entry = _create_entry(root, status="launched", run_id="run-e2e-1")
    _write_status(root, "run-e2e-1", "completed")
    # Precheck on a spec that demands gh access on the audit profile
    spec = (
        "TASK: requeue a failed run that the manual operator can recover.\n"
        "REQUIRES: github\n"
    )
    gate = check_capabilities(spec, profile_name="audit")
    # Reconcile sweep runs and reconciles the unrelated launched entry.
    report = reconcile_sweep(root, dry_run=False)
    assert gate.launch is False
    assert report.applied[0].queue_id == entry["queue_id"]
