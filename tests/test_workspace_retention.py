"""Terminal-run clone-workspace retention (easybutt0n-17).

Pins the documented behaviour of ``lh_harness.disk_retention.workspaces``:

* the selection predicate is pure and deny-by-default;
* the dry-run lists candidates with a size and removes nothing, whatever
  the enforcement setting is;
* enforcement is behind an explicit setting that defaults OFF, and OFF
  means no deletion path is reachable at all;
* the age threshold defaults to 336 h (14 days) and comes from the settings DB.

Every filesystem test builds its tree under ``tmp_path``; the real work
root is never touched.  Synchronous, pytest + stdlib only.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lh_harness import disk_retention as dr
from lh_harness.disk_retention import (
    DEFAULT_CONFIG,
    DEFAULT_WORKSPACE_MAX_AGE_HOURS,
    DEFAULT_WORKSPACE_RETENTION_ENABLED,
    DiskRetentionConfig,
    RetainMode,
    config_from_values,
    should_retain_workspace,
    sweep_workspaces,
    sweep_workspaces_report,
)
from lh_harness.disk_retention import config as cfg_mod
from lh_harness.disk_retention.workspaces import (
    GIT_PROBE_TIMEOUT_SECONDS,
    KEEP_STATES,
    QUEUE_TERMINAL_STATUSES,
    STATE_ERROR,
    STATE_KEPT_ACTIVE,
    STATE_KEPT_HARNESS_OWNED,
    STATE_KEPT_NO_REFS,
    STATE_KEPT_RECENT,
    STATE_KEPT_UNCOMMITTED,
    STATE_REMOVED,
    STATE_SELECTABLE,
    collect_workspace_refs,
    human_size,
    inspect_clone_git_state,
    is_terminal_reference,
)

HOUR = 3600.0
THRESHOLD = float(DEFAULT_WORKSPACE_MAX_AGE_HOURS) * HOUR  # 336 h
# A fixed wall-clock anchor (2026-11-27T00:00:00Z) used as ``now=`` for the sweep
# calls that accept it.  The launcher wiring path uses the real clock, so the
# anchor must sit in the past relative to it; the predicate itself never
# reads the clock at all (pinned by test_predicate_is_pure_no_fs_or_clock_access).
NOW = 1_792_752_000.0


# ------------------------------------------------------------------ helpers
# "Old" means comfortably older than the DEFAULT threshold, whatever it is:
# derived from ``THRESHOLD`` (336 h) so the default-age change (72 -> 336,
# easybutt0n-304) did not silently turn every "old" fixture young.
OLD_MARGIN_HOURS = 100.0


def _old(hours: float | None = None) -> float:
    """An age in seconds that is older than the default threshold.

    With no argument: ``THRESHOLD + 100 h``.  With an argument, that many
    hours (used by tests that need a fixed age regardless of the default).
    """

    if hours is None:
        return THRESHOLD + OLD_MARGIN_HOURS * HOUR
    return hours * HOUR


def _young(hours: float = 1.0) -> float:
    return hours * HOUR


def _tree(root: Path, rel: str, files: dict[str, bytes] | None = None) -> Path:
    """Create ``root/rel`` (with parents) and write ``files`` into its leaf."""

    target = root / rel
    target.mkdir(parents=True, exist_ok=True)
    for name, data in (files or {"keep.txt": b"k"}).items():
        leaf = target / name
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.write_bytes(data)
    return target


def _snapshot(root: Path) -> set[str]:
    """Every path under ``root``, relative — the 'nothing was deleted' oracle."""

    out: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        out.add(str(base.relative_to(root)))
        out.update(str((base / n).relative_to(root)) for n in filenames)
        out.update(str((base / d).relative_to(root)) for d in dirnames)
    return out


def _entry(workspace: str, status: str, run_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(workspace=workspace, status=status, run_id=run_id)


# ------------------------------------------------------------ git fixtures
# Dirty guard (easybutt0n-304): a terminal-and-old clone is a candidate only
# when it is PROVED clean and pushed, so a fixture that models a removable
# clone must be a real git repository with an upstream whose remote carries
# HEAD.  Every helper here builds its repository under ``tmp_path`` (the
# bare "origin" is a sibling directory of the clone, never a network URL),
# and the mutating git they run touches only those fixture trees.
_GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def _git(cwd: Path, *args: str) -> str:
    """Run git in ``cwd`` (a tmp_path fixture tree) and return stdout; fail loudly."""

    import subprocess

    proc = subprocess.run(["git", *_GIT_ID, "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return proc.stdout


def _make_clone(
    root: Path,
    name: str,
    files: dict[str, bytes] | None = None,
    *,
    pushed: bool = True,
) -> Path:
    """A clean git clone at ``root/name`` with its own local bare origin.

    With ``pushed=True`` the branch has an upstream and the remote carries
    HEAD — the one shape the dirty guard lets through.  ``pushed=False``
    adds the origin but never pushes: the branch then has no upstream and
    the commit is on no remote branch.

    The bare origin is kept OUTSIDE the work root (``<root>/../origins/``):
    the sweep visits every direct child of the root, so an origin placed
    beside the clone would show up as a second entry and pollute the
    listing under test.
    """

    ws = root / name
    origins = root.parent / "origins"
    origins.mkdir(parents=True, exist_ok=True)
    origin = origins / f"{name}.origin"
    ws.mkdir(parents=True)
    _git(root, "init", "-q", "--bare", str(origin))
    _git(ws, "init", "-q", "-b", "main")
    for rel, data in (files or {"src/main.py": b"print('hi')\n"}).items():
        leaf = ws / rel
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.write_bytes(data)
    _git(ws, "add", ".")
    _git(ws, "commit", "-q", "-m", "init")
    _git(ws, "remote", "add", "origin", str(origin))
    if pushed:
        _git(ws, "push", "-q", "-u", "origin", "main")
    return ws


def _commit(ws: Path, msg: str = "local") -> None:
    """One more empty commit on the fixture clone's checked-out branch."""

    _git(ws, "commit", "-q", "--allow-empty", "-m", msg)


# =========================================================== predicate matrix
class TestPredicateStatesXAges:
    WS = "/home/harness/work/lhh-e17-workspace-retention"

    def test_terminal_old_selects(self) -> None:
        for state in ("completed", "failed", "cancelled", "blocked", "incomplete"):
            assert should_retain_workspace(self.WS, [state], _old(), THRESHOLD) is True, state

    def test_terminal_young_keeps(self) -> None:
        assert should_retain_workspace(self.WS, ["completed"], _young(), THRESHOLD) is False

    def test_every_non_terminal_state_keeps(self) -> None:
        for state in ("running", "waiting_approval", "queued", "pending", "creating", "starting", "stopping"):
            assert should_retain_workspace(self.WS, [state], _old(), THRESHOLD) is False, state

    def test_unknown_state_keeps(self) -> None:
        for state in ("zapped", "", "unknown", "done-ish"):
            assert should_retain_workspace(self.WS, [state], _old(), THRESHOLD) is False, repr(state)

    def test_mixed_terminal_and_non_terminal_keeps(self) -> None:
        assert should_retain_workspace(self.WS, ["completed", "running"], _old(), THRESHOLD) is False
        assert should_retain_workspace(self.WS, ["pending", "failed"], _old(), THRESHOLD) is False

    def test_orphan_keeps(self) -> None:
        assert should_retain_workspace(self.WS, [], _old(), THRESHOLD) is False
        assert should_retain_workspace(self.WS, None, _old(), THRESHOLD) is False

    def test_age_exactly_equal_to_threshold_keeps(self) -> None:
        assert should_retain_workspace(self.WS, ["completed"], THRESHOLD, THRESHOLD) is False
        # One second older selects: the rule is strictly older.
        assert should_retain_workspace(self.WS, ["completed"], THRESHOLD + 1.0, THRESHOLD) is True

    def test_lh_harness_path_never_selects(self) -> None:
        for path in (
            "/home/harness/work/.lh-harness/runs/20261101-abc",
            "/home/harness/work/.lh-harness/runs",
            f"{self.WS}/.lh-harness",
            f"{self.WS}/.lh-harness/runs/20261101-abc/clone",
            "/home/harness/work/.lh-harness",
            "/x/.lh-harness/queue",
        ):
            assert should_retain_workspace(path, ["completed"], _old(10_000), THRESHOLD) is False, path

    def test_lh_harness_wins_over_every_other_input(self) -> None:
        # Even a terminal+old reference with a huge age never selects a
        # harness-owned path; the rule is checked first.
        assert should_retain_workspace("/w/.lh-harness/runs/x", ["completed"], 10**9, 0.0) is False

    def test_negative_or_non_finite_age_keeps(self) -> None:
        for age in (-1.0, float("nan"), float("inf"), float("-inf"), "x", None):
            assert should_retain_workspace(self.WS, ["completed"], age, THRESHOLD) is False, repr(age)

    def test_negative_threshold_disables(self) -> None:
        assert should_retain_workspace(self.WS, ["completed"], _old(10_000), -1.0) is False

    @pytest.mark.parametrize("threshold", [0.0, -1.0, -72.0 * HOUR])
    def test_zero_or_negative_threshold_disables(self, threshold: float) -> None:
        """``0`` disables the rule just like a negative value (LHH#222 review).

        Before the fix a threshold of ``0`` fell through to ``age > limit``
        and selected EVERY terminal workspace, however young -- a 5 s old
        clone was removed with the flag on -- while config.py and the docs
        promised "0 disables the rule".  Only a strictly positive threshold
        can select anything.
        """
        for age in (_old(10_000), _old(), THRESHOLD + 1.0, _young(), 5.0, 0.0):
            assert should_retain_workspace(self.WS, ["completed"], age, threshold) is False, (age, threshold)
            assert should_retain_workspace(self.WS, ["queue:done"], age, threshold) is False, (age, threshold)

    def test_positive_threshold_still_selects(self) -> None:
        # The fix must not touch the positive path: a tiny positive threshold
        # still selects a positive age, and the 72 h default behaves as before.
        assert should_retain_workspace(self.WS, ["completed"], 1.0, 0.5) is True
        assert should_retain_workspace(self.WS, ["completed"], _old(), THRESHOLD) is True
        assert should_retain_workspace(self.WS, ["completed"], _young(), THRESHOLD) is False

    def test_predicate_is_pure_no_fs_or_clock_access(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The predicate must not touch the filesystem, the clock, or the store."""

        import builtins

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("predicate performed I/O")

        # Patch the open door and the clock the module would use.
        monkeypatch.setattr(builtins, "open", _boom)
        monkeypatch.setattr(os, "stat", _boom)
        monkeypatch.setattr(os, "lstat", _boom)
        monkeypatch.setattr(os, "scandir", _boom)
        monkeypatch.setattr(os, "listdir", _boom)
        monkeypatch.setattr("time.time", _boom)
        monkeypatch.setattr("lh_harness.disk_retention.workspaces.time.time", _boom)
        monkeypatch.setattr(
            "lh_harness.disk_retention.workspaces._dir_size_bytes", _boom
        )
        monkeypatch.setattr(
            "lh_harness.disk_retention.workspaces.collect_workspace_refs", _boom
        )
        assert should_retain_workspace(self.WS, ["completed"], _old(), THRESHOLD) is True
        assert should_retain_workspace(self.WS, ["running"], _old(), THRESHOLD) is False


# ================================================================= collector
def test_collector_groups_runs_and_queue_entries_by_normalized_path() -> None:
    refs = collect_workspace_refs(
        [
            {"workspace": "/work/a/", "status": "completed", "finished_at": 10.0},
            {"workspace": "/work/a", "status": "failed", "finished_at": 20.0},
            {"workspace": "/work/b", "status": "running", "finished_at": 99.0},
            {"id": "r3", "status": "completed"},  # no workspace: ignored
        ],
        [_entry("/work/a", "launched"), _entry("/work/b", "pending")],
    )
    assert set(refs) == {"/work/a", "/work/b"}
    # References are tagged by source: runs as ``run:``, the queue row as ``queue:``.
    assert sorted(refs["/work/a"].states) == ["queue:launched", "run:completed", "run:failed"]
    # Most recent terminal finished_at wins.
    assert refs["/work/a"].finished_at == 20.0
    assert refs["/work/a"].age_source == "run_finished_at"
    # An active run never lends its (possibly stale) finished_at.
    assert refs["/work/b"].states == ["run:running", "queue:pending"]
    assert refs["/work/b"].finished_at is None
    assert refs["/work/b"].age_source == "workspace_mtime"


def test_collector_accepts_mapping_queue_entries_and_skips_blank() -> None:
    refs = collect_workspace_refs([], [{"workspace": "  ", "status": "pending"}, {"workspace": "/w", "status": "blocked"}])
    assert set(refs) == {"/w"}
    assert refs["/w"].states == ["queue:blocked"]


# ============================================== queue references (deny by default)
# A queue entry's status is NOT a run lifecycle status.  ``blocked`` is a
# terminal *run* outcome but a NON-terminal *queue* state (queue.py
# _NON_TERMINAL_STATUS): the entry is still parked work.  These tests pin that
# a queue reference is judged only through the explicit queue allowlist.
QUEUE_STATUSES = ("pending", "launched", "blocked", "done", "failed")


def _queue_vocabulary() -> tuple[frozenset[str], frozenset[str]]:
    """The real queue vocabulary, imported (never copied) from queue.py."""

    from lh_harness import queue as queue_mod

    return queue_mod._NON_TERMINAL_STATUS, queue_mod._VALID_STATUS


class TestQueueReferenceTerminalAllowlist:
    def test_queue_allowlist_is_exactly_the_queue_modules_terminal_statuses(self) -> None:
        """The allowlist mirrors ``_VALID_STATUS - _NON_TERMINAL_STATUS``; drift fails here."""

        non_terminal, valid = _queue_vocabulary()
        assert QUEUE_TERMINAL_STATUSES == frozenset(valid - non_terminal)
        assert not (QUEUE_TERMINAL_STATUSES & non_terminal)

    def test_every_non_terminal_queue_status_keeps(self) -> None:
        from lh_harness.disk_retention.workspaces import SOURCE_QUEUE

        non_terminal, _ = _queue_vocabulary()
        assert {"blocked", "pending", "launched"} <= set(non_terminal)
        for status in sorted(non_terminal):
            assert should_retain_workspace("/w/ws", [f"queue:{status}"], _old(), THRESHOLD) is False, status
            assert is_terminal_reference(SOURCE_QUEUE, status) is False, status

    def test_unknown_or_missing_queue_status_keeps(self) -> None:
        from lh_harness.disk_retention.workspaces import SOURCE_QUEUE

        # Unknown words, terminal *run* words, near-misses, empty, None and non-string values
        # are all non-terminal for a queue entry.  (Case/whitespace is normalised, so
        # ``"Done"``/``"done "`` are terminal — pinned in the sibling test.)
        for status in ("zapped", "", "completed", "cancelled", "incomplete", "blocked", "DONE?", "don", None, 7, True):
            assert should_retain_workspace("/w/ws", [f"queue:{status}"], _old(), THRESHOLD) is False, repr(status)
            assert is_terminal_reference(SOURCE_QUEUE, status) is False, repr(status)
        # A queue status that *is* a terminal run word must not be terminal for a queue entry.
        assert is_terminal_reference(SOURCE_QUEUE, "blocked") is False
        assert is_terminal_reference(SOURCE_QUEUE, "completed") is False

    def test_terminal_queue_statuses_select(self) -> None:
        from lh_harness.disk_retention.workspaces import SOURCE_QUEUE

        for status in ("done", "failed", "Done", " FAILED "):
            assert is_terminal_reference(SOURCE_QUEUE, status) is True, repr(status)
        for status in ("done", "failed"):
            assert should_retain_workspace("/w/ws", [f"queue:{status}"], _old(), THRESHOLD) is True, status
            # Young keeps, exactly like a young run.
            assert should_retain_workspace("/w/ws", [f"queue:{status}"], _young(), THRESHOLD) is False, status

    def test_queue_blocked_is_not_a_run_blocked(self) -> None:
        """Same word, two vocabularies: the tag decides, never the spelling."""

        assert should_retain_workspace("/w/ws", ["run:blocked"], _old(), THRESHOLD) is True
        assert should_retain_workspace("/w/ws", ["blocked"], _old(), THRESHOLD) is True  # bare = run
        assert should_retain_workspace("/w/ws", ["queue:blocked"], _old(), THRESHOLD) is False

    def test_run_states_are_still_judged_by_the_lifecycle_vocabulary(self) -> None:
        from lh_harness.disk_retention.workspaces import SOURCE_RUN

        for state in ("completed", "failed", "cancelled", "blocked", "incomplete", "done"):
            assert is_terminal_reference(SOURCE_RUN, state) is True, state
        for state in ("running", "waiting_approval", "queued", "pending", "creating", "starting", "stopping", "zapped", None, ""):
            assert is_terminal_reference(SOURCE_RUN, state) is False, repr(state)

    def test_unknown_source_is_never_terminal(self) -> None:
        assert is_terminal_reference("cron", "done") is False
        assert is_terminal_reference("", "completed") is False
        # An unrecognised tag is treated as a run status spelled "cron:done", which is unknown -> keep.
        assert should_retain_workspace("/w/ws", ["cron:done"], _old(), THRESHOLD) is False

    def test_queue_non_terminal_reference_wins_over_a_terminal_run(self) -> None:
        """Mixed: a finished run plus a still-parked queue entry keeps the workspace."""

        for status in ("blocked", "pending", "launched"):
            assert should_retain_workspace("/w/ws", ["run:completed", f"queue:{status}"], _old(), THRESHOLD) is False, status
        assert should_retain_workspace("/w/ws", ["run:completed", "queue:done"], _old(), THRESHOLD) is True

    def test_predicate_is_still_pure_with_tagged_states(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builtins

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("predicate performed I/O")

        monkeypatch.setattr(builtins, "open", _boom)
        monkeypatch.setattr(os, "stat", _boom)
        monkeypatch.setattr(os, "scandir", _boom)
        monkeypatch.setattr("time.time", _boom)
        assert should_retain_workspace("/w/ws", ["queue:blocked"], _old(), THRESHOLD) is False
        assert should_retain_workspace("/w/ws", ["queue:done"], _old(), THRESHOLD) is True


class TestCollectorTagsQueueReferences:
    def test_queue_statuses_are_tagged_with_their_source(self) -> None:
        refs = collect_workspace_refs([], [_entry("/w/ws", s) for s in QUEUE_STATUSES])
        assert refs["/w/ws"].states == [f"queue:{s}" for s in QUEUE_STATUSES]

    def test_run_statuses_are_tagged_as_runs(self) -> None:
        refs = collect_workspace_refs([{"workspace": "/w/ws", "status": "blocked", "finished_at": 5.0}], [])
        assert refs["/w/ws"].states == ["run:blocked"]
        assert refs["/w/ws"].finished_at == 5.0

    def test_queue_entry_with_no_status_still_counts_as_a_reference(self) -> None:
        """A nameless-status queue entry is not dropped (that would orphan-keep by accident, not by rule)."""

        entry = SimpleNamespace(workspace="/w/ws")
        refs = collect_workspace_refs([], [entry])
        assert refs["/w/ws"].states == ["queue:"]
        assert should_retain_workspace("/w/ws", refs["/w/ws"].states, _old(), THRESHOLD) is False
        # Mapping form with a missing/None status too.
        refs = collect_workspace_refs([], [{"workspace": "/w/ws"}, {"workspace": "/w/ws", "status": None}])
        assert refs["/w/ws"].states == ["queue:", "queue:"]

    def test_run_and_queue_references_are_never_conflated(self) -> None:
        refs = collect_workspace_refs(
            [{"workspace": "/w/ws", "status": "completed", "finished_at": 10.0}],
            [_entry("/w/ws", "blocked")],
        )
        assert sorted(refs["/w/ws"].states) == ["queue:blocked", "run:completed"]
        assert should_retain_workspace("/w/ws", refs["/w/ws"].states, _old(), THRESHOLD) is False


# ============================================================== sweep: queue-only
class TestSweepQueueOnlyReferences:
    """Enforcement ON, live mode, temp tree: a queue-only non-terminal workspace survives."""

    @pytest.mark.parametrize("status", ["blocked", "pending", "launched"])
    def test_queue_only_non_terminal_workspace_survives_enforcement(
        self, tmp_path: Path, status: str
    ) -> None:
        root = tmp_path / "work"
        ws = _tree(root, "ws-queue-only", {"src/main.py": b"x" * 2048})
        os.utime(ws, (NOW - _old(), NOW - _old()))  # dir looks old; must not matter
        before = _snapshot(root)
        reports = sweep_workspaces(
            root,
            run_items=[],
            queue_entries=[_entry(str(ws), status)],
            config=DiskRetentionConfig(workspace_retention_enabled=True),
            mode=RetainMode.live(),
            now=NOW,
        )
        got = _by_path(reports)
        assert got["ws-queue-only"].state == STATE_KEPT_ACTIVE
        assert got["ws-queue-only"].states == [f"queue:{status}"]
        assert got["ws-queue-only"].size_bytes == 0  # never measured for removal
        assert _snapshot(root) == before
        assert (ws / "src" / "main.py").is_file()

    @pytest.mark.parametrize("status", ["done", "failed"])
    def test_queue_only_terminal_workspace_is_removed_under_enforcement(
        self, tmp_path: Path, status: str
    ) -> None:
        root = tmp_path / "work"
        ws = _make_clone(root, "ws-queue-done")
        os.utime(ws, (NOW - _old(), NOW - _old()))
        reports = sweep_workspaces(
            root,
            run_items=[],
            queue_entries=[_entry(str(ws), status)],
            config=DiskRetentionConfig(workspace_retention_enabled=True),
            mode=RetainMode.live(),
            now=NOW,
        )
        assert _by_path(reports)["ws-queue-done"].state == STATE_REMOVED
        assert not ws.exists()

    def test_enforcement_on_removes_terminal_old_but_keeps_every_non_terminal_queue_workspace(
        self, tmp_path: Path
    ) -> None:
        """The mixed sweep: one candidate goes, every queue-parked sibling stays."""

        root = tmp_path / "work"
        ws_old = _make_clone(root, "ws-old-terminal")
        # The two queue-terminal clones (done/failed) are removal candidates, so
        # each must be a real clean pushed clone the dirty guard lets through;
        # the non-terminal/unknown ones are kept before the guard is reached.
        for name in ("ws-done", "ws-failed"):
            _make_clone(root, name)
        names = [f"ws-{s}" for s in QUEUE_STATUSES] + ["ws-queue-unknown"]
        for name in names:
            if not (root / name).exists():
                _tree(root, name, {".git/HEAD": b"ref" * 100})
        ws_harness = _tree(root, ".lh-harness/runs/x", {"status.json": b"{}"})
        for ws in [root / n for n in names] + [ws_old]:
            os.utime(ws, (NOW - _old(), NOW - _old()))
        run_items = [{"id": "r-old", "workspace": str(ws_old), "status": "completed", "finished_at": NOW - _old()}]
        queue_entries = [_entry(str(root / f"ws-{s}"), s) for s in QUEUE_STATUSES]
        queue_entries.append(_entry(str(root / "ws-queue-unknown"), "wat"))
        before = _snapshot(root)

        reports = sweep_workspaces(
            root,
            run_items=run_items,
            queue_entries=queue_entries,
            config=DiskRetentionConfig(workspace_retention_enabled=True),
            mode=RetainMode.live(),
            now=NOW,
        )
        got = _by_path(reports)
        # Removals are exactly the terminal-and-old workspaces: the finished run's clone
        # and the two clones named only by terminal (done/failed) queue entries.
        assert got["ws-old-terminal"].state == STATE_REMOVED
        assert not ws_old.exists()
        assert sorted(n for n, r in got.items() if r.state == STATE_REMOVED) == [
            "ws-done",
            "ws-failed",
            "ws-old-terminal",
        ]
        # Every non-terminal / unknown queue-only workspace survives, reported as active.
        for status in ("blocked", "pending", "launched"):
            assert got[f"ws-{status}"].state == STATE_KEPT_ACTIVE, status
            assert (root / f"ws-{status}").is_dir(), status
        assert got["ws-queue-unknown"].state == STATE_KEPT_ACTIVE
        assert (root / "ws-queue-unknown").is_dir()
        # Terminal queue entries name old workspaces -> removed too (rule, not accident).
        for status in ("done", "failed"):
            assert got[f"ws-{status}"].state == STATE_REMOVED, status
        # Run evidence is never touched.
        assert got[".lh-harness"].state == STATE_KEPT_HARNESS_OWNED
        assert (ws_harness / "status.json").is_file()
        removed = before - _snapshot(root)
        assert all(
            p.split("/")[0] in {"ws-old-terminal", "ws-done", "ws-failed"} for p in removed
        ), sorted(removed)

    def test_dry_run_with_queue_only_blocked_lists_sizes_and_removes_nothing(self, tmp_path: Path) -> None:
        root = tmp_path / "work"
        ws_blocked = _tree(root, "ws-blocked", {".git/HEAD": b"b" * 4096})
        ws_old = _make_clone(root, "ws-old", {"payload.bin": b"o" * 4096})
        for ws in (ws_blocked, ws_old):
            os.utime(ws, (NOW - _old(), NOW - _old()))
        before = _snapshot(root)
        payload = sweep_workspaces_report(
            root,
            run_items=[{"workspace": str(ws_old), "status": "completed", "finished_at": NOW - _old()}],
            queue_entries=[_entry(str(ws_blocked), "blocked")],
            config=DiskRetentionConfig(workspace_retention_enabled=True),
            mode=RetainMode.dry(),
            now=NOW,
        )
        by_name = {os.path.basename(e["path"]): e for e in payload["entries"]}
        assert payload["mode"] == "dry-run"
        assert by_name["ws-old"]["state"] == STATE_SELECTABLE
        # The committed payload is exactly the 4 KiB fixture file; ``.git`` is
        # part of the clone's own size, so the candidate is at least that.
        assert by_name["ws-old"]["size_bytes"] >= 4096
        assert by_name["ws-old"]["size_human"].endswith(("B", "KiB", "MiB", "GiB"))
        assert payload["totals"]["candidate_bytes"] == by_name["ws-old"]["size_bytes"]
        # The blocked one is listed with its tagged state, kept, and carries no candidate size.
        assert by_name["ws-blocked"]["state"] == STATE_KEPT_ACTIVE
        assert by_name["ws-blocked"]["states"] == ["queue:blocked"]
        assert payload["totals"]["candidates"] == 1
        assert _snapshot(root) == before


# ================================================================ human_size
def test_human_size_is_du_style() -> None:
    assert human_size(0) == "0B"
    assert human_size(1023) == "1023B"
    assert human_size(1024) == "1.0KiB"
    assert human_size(1536) == "1.5KiB"
    assert human_size(10 * 1024) == "10KiB"
    assert human_size(50 * 1024**2) == "50MiB"
    assert human_size(3 * 1024**3) == "3.0GiB"


# =============================================================== fixtures
@pytest.fixture
def work_tree(tmp_path: Path) -> dict[str, Any]:
    """A work root with one candidate and every keep reason represented.

    Layout (all under ``tmp_path``, never the real work root)::

        work/
          ws-old-terminal/     referenced only by a terminal run, finished 100 h ago  -> selectable
          ws-young-terminal/   terminal run finished 1 h ago                             -> kept_recent
          ws-running/          run still running                                          -> kept_active
          ws-queued/           queue entry pending                                         -> kept_active
          ws-mixed/            one terminal + one running run                              -> kept_active
          ws-orphan/           nothing references it                                      -> kept_no_references
          .lh-harness/runs/<id>/   harness-owned run evidence                              -> kept_harness_owned
          a-file               not a directory: ignored
    """

    root = tmp_path / "work"
    # The one candidate is a real clean clone with an upstream whose remote
    # carries HEAD: the dirty guard must let it through.  Its bare origin
    # is a sibling under tmp_path.  Every other entry is kept for a reason
    # that is checked before the guard (age, active reference, orphan,
    # harness-owned), so a plain tree still models them.
    _make_clone(root, "ws-old-terminal")
    for name in (
        "ws-young-terminal",
        "ws-running",
        "ws-queued",
        "ws-mixed",
        "ws-orphan",
    ):
        _tree(root, name, {".gitignore": b"x" * 10, "src/main.py": b"print('hi')\n"})
    _tree(root, ".lh-harness/runs/20261101-abc/logs", {"report.json": b"{}"})
    (root / "a-file").write_text("not a workspace")

    def ws(name: str) -> str:
        return str(root / name)

    run_items = [
        {"id": "r-old", "workspace": ws("ws-old-terminal"), "status": "completed", "finished_at": NOW - _old()},
        {"id": "r-young", "workspace": ws("ws-young-terminal"), "status": "failed", "finished_at": NOW - _young()},
        {"id": "r-run", "workspace": ws("ws-running"), "status": "running"},
        {"id": "r-wait", "workspace": ws("ws-running"), "status": "waiting_approval"},
        {"id": "r-mixed-1", "workspace": ws("ws-mixed"), "status": "completed", "finished_at": NOW - _old()},
        {"id": "r-mixed-2", "workspace": ws("ws-mixed"), "status": "starting"},
    ]
    queue_entries = [
        _entry(ws("ws-queued"), "pending"),
        _entry(ws("ws-old-terminal"), "done"),
    ]
    return {
        "root": root,
        "ws": ws,
        "run_items": run_items,
        "queue_entries": queue_entries,
    }


def _by_path(reports: list[Any]) -> dict[str, Any]:
    return {os.path.basename(r.path): r for r in reports}


# =================================================================== dry-run
class TestDryRun:
    @pytest.mark.parametrize("enabled", [False, True])
    def test_lists_candidates_with_sizes_and_removes_nothing(
        self, work_tree: dict[str, Any], enabled: bool
    ) -> None:
        root: Path = work_tree["root"]
        before = _snapshot(root)
        cfg = DiskRetentionConfig(workspace_retention_enabled=enabled)
        reports = sweep_workspaces(
            root,
            run_items=work_tree["run_items"],
            queue_entries=work_tree["queue_entries"],
            config=cfg,
            mode=RetainMode.dry(),
            now=NOW,
        )
        got = _by_path(reports)
        assert got["ws-old-terminal"].state == STATE_SELECTABLE
        assert got["ws-old-terminal"].size_bytes > 0
        assert got["ws-old-terminal"].size_human.endswith(("B", "KiB", "MiB", "GiB"))
        assert got["ws-old-terminal"].age_source == "run_finished_at"
        assert got["ws-old-terminal"].age_seconds == pytest.approx(_old())
        # Every keep reason is reported with its state.
        assert got["ws-young-terminal"].state == STATE_KEPT_RECENT
        assert got["ws-running"].state == STATE_KEPT_ACTIVE
        assert got["ws-queued"].state == STATE_KEPT_ACTIVE
        assert got["ws-mixed"].state == STATE_KEPT_ACTIVE
        assert got["ws-orphan"].state == STATE_KEPT_NO_REFS
        assert got[".lh-harness"].state == STATE_KEPT_HARNESS_OWNED
        # Nothing was removed, and nothing was created either.
        assert _snapshot(root) == before
        assert not any(r.state == STATE_REMOVED for r in reports)
        # Plain files directly under the root are not workspaces.
        assert "a-file" not in got

    def test_live_mode_with_flag_off_still_removes_nothing(
        self, work_tree: dict[str, Any]
    ) -> None:
        root: Path = work_tree["root"]
        before = _snapshot(root)
        reports = sweep_workspaces(
            root,
            run_items=work_tree["run_items"],
            queue_entries=work_tree["queue_entries"],
            config=DiskRetentionConfig(workspace_retention_enabled=False),
            mode=RetainMode.live(),
            now=NOW,
        )
        got = _by_path(reports)
        # The candidate is still only listed, never removed.
        assert got["ws-old-terminal"].state == STATE_SELECTABLE
        assert _snapshot(root) == before

    def test_live_mode_with_flag_off_cannot_reach_rmtree(
        self, work_tree: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import lh_harness.disk_retention.workspaces as mod

        called: list[str] = []
        monkeypatch.setattr(mod, "_delete_workspace", lambda p: called.append(str(p)))
        monkeypatch.setattr(mod.shutil, "rmtree", lambda *a, **k: called.append("rmtree"))
        sweep_workspaces(
            work_tree["root"],
            run_items=work_tree["run_items"],
            queue_entries=work_tree["queue_entries"],
            config=DiskRetentionConfig(workspace_retention_enabled=False),
            mode=RetainMode.live(),
            now=NOW,
        )
        assert called == []

    def test_report_is_jsonable_and_carries_sizes_and_flag(
        self, work_tree: dict[str, Any]
    ) -> None:
        payload = sweep_workspaces_report(
            work_tree["root"],
            run_items=work_tree["run_items"],
            queue_entries=work_tree["queue_entries"],
            config=DiskRetentionConfig(workspace_retention_enabled=True),
            mode=RetainMode.dry(),
            now=NOW,
        )
        decoded = json.loads(json.dumps(payload, sort_keys=True))
        assert decoded["mode"] == "dry-run"
        assert decoded["enabled"] is True
        assert decoded["max_age_hours"] == DEFAULT_WORKSPACE_MAX_AGE_HOURS
        assert decoded["totals"]["candidates"] == 1
        assert decoded["totals"]["candidate_bytes"] > 0
        assert decoded["totals"]["by_state"][STATE_SELECTABLE] == 1
        entry = next(e for e in decoded["entries"] if e["state"] == STATE_SELECTABLE)
        assert entry["size_bytes"] > 0 and entry["size_human"]
        assert set(entry["states"]) == {"run:completed", "queue:done"}

    def test_dry_run_with_default_config_lists_and_removes_nothing(
        self, work_tree: dict[str, Any]
    ) -> None:
        root: Path = work_tree["root"]
        before = _snapshot(root)
        reports = sweep_workspaces(
            root,
            run_items=work_tree["run_items"],
            queue_entries=work_tree["queue_entries"],
            now=NOW,
        )
        assert _by_path(reports)["ws-old-terminal"].state == STATE_SELECTABLE
        assert _snapshot(root) == before


# ================================================================ enforcement
class TestEnforcement:
    def test_flag_off_selectable_workspace_survives(self, work_tree: dict[str, Any]) -> None:
        root: Path = work_tree["root"]
        before = _snapshot(root)
        reports = sweep_workspaces(
            root,
            run_items=work_tree["run_items"],
            queue_entries=work_tree["queue_entries"],
            config=DiskRetentionConfig(workspace_retention_enabled=False),
            mode=RetainMode.live(),
            now=NOW,
        )
        assert _by_path(reports)["ws-old-terminal"].state == STATE_SELECTABLE
        assert _snapshot(root) == before
        assert (root / "ws-old-terminal" / "src" / "main.py").exists()

    def test_flag_on_removes_only_selectable_workspaces(self, work_tree: dict[str, Any]) -> None:
        root: Path = work_tree["root"]
        before = _snapshot(root)
        reports = sweep_workspaces(
            root,
            run_items=work_tree["run_items"],
            queue_entries=work_tree["queue_entries"],
            config=DiskRetentionConfig(workspace_retention_enabled=True),
            mode=RetainMode.live(),
            now=NOW,
        )
        got = _by_path(reports)
        assert got["ws-old-terminal"].state == STATE_REMOVED
        assert not (root / "ws-old-terminal").exists()
        after = _snapshot(root)
        removed = before - after
        # Exactly the one selectable tree disappeared — nothing else.
        assert all(p == "ws-old-terminal" or p.startswith("ws-old-terminal/") for p in removed)
        # Every other workspace, including run evidence, survives intact.
        for name in (
            "ws-young-terminal",
            "ws-running",
            "ws-queued",
            "ws-mixed",
            "ws-orphan",
            ".lh-harness",
        ):
            assert (root / name).is_dir(), name
            assert got[name].state != STATE_REMOVED
        assert (root / ".lh-harness" / "runs" / "20261101-abc" / "logs" / "report.json").is_file()
        assert (root / "a-file").is_file()

    def test_flag_on_but_dry_run_removes_nothing(self, work_tree: dict[str, Any]) -> None:
        root: Path = work_tree["root"]
        before = _snapshot(root)
        reports = sweep_workspaces(
            root,
            run_items=work_tree["run_items"],
            queue_entries=work_tree["queue_entries"],
            config=DiskRetentionConfig(workspace_retention_enabled=True),
            mode=RetainMode.dry(),
            now=NOW,
        )
        assert _by_path(reports)["ws-old-terminal"].state == STATE_SELECTABLE
        assert _snapshot(root) == before

    def test_harness_owned_work_root_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / ".lh-harness"
        _tree(root, "runs/x")
        assert sweep_workspaces(root, run_items=[], config=DiskRetentionConfig(workspace_retention_enabled=True), mode=RetainMode.live()) == []
        assert (root / "runs" / "x").is_dir()

    def test_symlink_child_is_never_followed_or_removed(self, tmp_path: Path) -> None:
        root = tmp_path / "work"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "precious.txt").write_text("keep")
        link = root / "ws-link"
        link.symlink_to(outside, target_is_directory=True)
        reports = sweep_workspaces(
            root,
            run_items=[{"workspace": str(link), "status": "completed", "finished_at": NOW - _old()}],
            config=DiskRetentionConfig(workspace_retention_enabled=True),
            mode=RetainMode.live(),
            now=NOW,
        )
        assert reports == []
        assert link.is_symlink()
        assert (outside / "precious.txt").read_text() == "keep"

    def test_missing_or_empty_work_root_is_a_noop(self, tmp_path: Path) -> None:
        assert sweep_workspaces(None, run_items=[], mode=RetainMode.live()) == []
        assert sweep_workspaces("", run_items=[], mode=RetainMode.live()) == []
        assert sweep_workspaces(tmp_path / "nope", run_items=[], mode=RetainMode.live()) == []

    def test_only_direct_children_are_visited(self, tmp_path: Path) -> None:
        root = tmp_path / "work"
        _tree(root, "ws-a/nested-ws")
        reports = sweep_workspaces(
            root,
            run_items=[{"workspace": str(root / "ws-a" / "nested-ws"), "status": "completed", "finished_at": NOW - _old()}],
            config=DiskRetentionConfig(workspace_retention_enabled=True),
            mode=RetainMode.live(),
            now=NOW,
        )
        # ws-a itself is an orphan (the reference names the nested dir), so it is kept.
        assert [os.path.basename(r.path) for r in reports] == ["ws-a"]
        assert reports[0].state == STATE_KEPT_NO_REFS
        assert (root / "ws-a" / "nested-ws").is_dir()

    def test_age_falls_back_to_workspace_mtime_when_run_left_no_timestamp(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "work"
        ws = _make_clone(root, "ws-mtime")
        target = NOW - _old()
        os.utime(ws, (target, target))
        reports = sweep_workspaces(
            root,
            run_items=[{"workspace": str(ws), "status": "completed"}],  # no finished_at
            config=DiskRetentionConfig(workspace_retention_enabled=True),
            mode=RetainMode.live(),
            now=NOW,
        )
        assert reports[0].state == STATE_REMOVED
        assert not ws.exists()

    def test_age_uses_run_finished_at_not_dir_mtime(self, tmp_path: Path) -> None:
        """A clone re-created yesterday but finished a week ago is a week old."""
        root = tmp_path / "work"
        ws = _make_clone(root, "ws")
        os.utime(ws, (NOW - _young(), NOW - _young()))  # dir looks young
        reports = sweep_workspaces(
            root,
            run_items=[{"workspace": str(ws), "status": "completed", "finished_at": NOW - _old()}],
            config=DiskRetentionConfig(workspace_retention_enabled=False),
            mode=RetainMode.dry(),
            now=NOW,
        )
        assert reports[0].state == STATE_SELECTABLE
        assert reports[0].age_source == "run_finished_at"

    def test_age_exactly_at_threshold_is_kept(self, tmp_path: Path) -> None:
        root = tmp_path / "work"
        ws = _tree(root, "ws")
        reports = sweep_workspaces(
            root,
            run_items=[{"workspace": str(ws), "status": "completed", "finished_at": NOW - THRESHOLD}],
            config=DiskRetentionConfig(workspace_retention_enabled=True),
            mode=RetainMode.live(),
            now=NOW,
        )
        assert reports[0].state == STATE_KEPT_RECENT
        assert ws.is_dir()

    def test_rmtree_failure_is_reported_as_error(self, work_tree: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
        import lh_harness.disk_retention.workspaces as mod

        def _fail(p: Any) -> None:
            raise OSError("disk says no")

        monkeypatch.setattr(mod, "_delete_workspace", _fail)
        reports = sweep_workspaces(
            work_tree["root"],
            run_items=work_tree["run_items"],
            queue_entries=work_tree["queue_entries"],
            config=DiskRetentionConfig(workspace_retention_enabled=True),
            mode=RetainMode.live(),
            now=NOW,
        )
        got = _by_path(reports)
        assert got["ws-old-terminal"].state == STATE_ERROR
        assert "disk says no" in got["ws-old-terminal"].reason

    def test_threshold_from_config_is_honoured(self, tmp_path: Path) -> None:
        root = tmp_path / "work"
        ws = _make_clone(root, "ws")
        run_items = [{"workspace": str(ws), "status": "completed", "finished_at": NOW - 2 * HOUR}]
        # 1 h threshold: 2 h old selects.
        assert sweep_workspaces(root, run_items=run_items, config=DiskRetentionConfig(workspace_max_age_hours=1), now=NOW)[0].state == STATE_SELECTABLE
        # Default threshold: 2 h old is kept.
        assert sweep_workspaces(root, run_items=run_items, config=DiskRetentionConfig(), now=NOW)[0].state == STATE_KEPT_RECENT


# =============================================== threshold 0 / negative: disabled
# LHH#222 review: ``LH_HARNESS_DISK_WORKSPACE_MAX_AGE_HOURS=0`` selected EVERY
# terminal workspace (a 5 s old one was removed with the flag on) although
# config.py and docs/disk-retention.md said "0 disables the rule".  These pin
# the safe reading: 0 and negatives disable the rule, nothing is selected and
# nothing removed, with enforcement ON.  ``work_tree`` already holds a clone
# that IS selected under the 72 h default (``ws-old-terminal``) plus a fresh
# one, so it is exactly the fixture these cases need.
DISABLED_THRESHOLDS = [0, -1]


class TestZeroOrNegativeThresholdDisables:
    @pytest.mark.parametrize("hours", DISABLED_THRESHOLDS)
    def test_live_mode_with_flag_on_selects_and_removes_nothing(
        self, work_tree: dict[str, Any], hours: int
    ) -> None:
        root: Path = work_tree["root"]
        before = _snapshot(root)
        reports = sweep_workspaces(
            root,
            run_items=work_tree["run_items"],
            queue_entries=work_tree["queue_entries"],
            config=DiskRetentionConfig(workspace_retention_enabled=True, workspace_max_age_hours=hours),
            mode=RetainMode.live(),
            now=NOW,
        )
        # Nothing is a candidate, nothing was removed, nothing changed on disk.
        assert [r for r in reports if r.state in (STATE_SELECTABLE, STATE_REMOVED)] == []
        assert all(r.state in KEEP_STATES for r in reports)
        assert _snapshot(root) == before
        # Every directory still exists -- including the one the 72 h default removes.
        for name in ("ws-old-terminal", "ws-young-terminal", "ws-running", "ws-queued", "ws-mixed", "ws-orphan"):
            assert (root / name).is_dir(), name
        assert (root / ".lh-harness" / "runs" / "20261101-abc" / "logs" / "report.json").is_file()
        # The old clone is reported as kept, and the reason says the rule is disabled.
        got = _by_path(reports)
        assert got["ws-old-terminal"].state == STATE_KEPT_RECENT
        assert "disabled" in got["ws-old-terminal"].reason
        assert got["ws-old-terminal"].size_bytes == 0  # never measured for removal

    @pytest.mark.parametrize("hours", DISABLED_THRESHOLDS)
    def test_same_tree_is_selected_and_removed_under_the_default_threshold(
        self, work_tree: dict[str, Any], hours: int
    ) -> None:
        """Control: the very same fixture loses ``ws-old-terminal`` at 72 h, so the
        empty selection above is the disabled rule, not an unselectable fixture."""
        root: Path = work_tree["root"]
        reports = sweep_workspaces(
            root,
            run_items=work_tree["run_items"],
            queue_entries=work_tree["queue_entries"],
            config=DiskRetentionConfig(workspace_retention_enabled=True, workspace_max_age_hours=DEFAULT_WORKSPACE_MAX_AGE_HOURS),
            mode=RetainMode.live(),
            now=NOW,
        )
        assert _by_path(reports)["ws-old-terminal"].state == STATE_REMOVED
        assert not (root / "ws-old-terminal").exists()
        assert hours in DISABLED_THRESHOLDS  # the parametrisation is only for the report id

    @pytest.mark.parametrize("hours", DISABLED_THRESHOLDS)
    def test_dry_run_listing_is_empty_too(
        self, work_tree: dict[str, Any], hours: int
    ) -> None:
        root: Path = work_tree["root"]
        before = _snapshot(root)
        payload = sweep_workspaces_report(
            root,
            run_items=work_tree["run_items"],
            queue_entries=work_tree["queue_entries"],
            config=DiskRetentionConfig(workspace_retention_enabled=True, workspace_max_age_hours=hours),
            mode=RetainMode.dry(),
            now=NOW,
        )
        assert payload["max_age_hours"] == hours
        assert payload["totals"]["candidates"] == 0
        assert payload["totals"]["candidate_bytes"] == 0
        assert STATE_SELECTABLE not in payload["totals"]["by_state"]
        assert _snapshot(root) == before

    @pytest.mark.parametrize("hours", DISABLED_THRESHOLDS)
    def test_five_second_old_clone_is_not_removed(self, tmp_path: Path, hours: int) -> None:
        """The exact review scenario: a clone whose run finished 5 s ago survives enforcement."""
        root = tmp_path / "work"
        ws = _tree(root, "ws-just-finished", {".git/HEAD": b"ref" * 100})
        run_items = [{"id": "r-new", "workspace": str(ws), "status": "completed", "finished_at": NOW - 5.0}]
        reports = sweep_workspaces(
            root,
            run_items=run_items,
            config=DiskRetentionConfig(workspace_retention_enabled=True, workspace_max_age_hours=hours),
            mode=RetainMode.live(),
            now=NOW,
        )
        assert _by_path(reports)["ws-just-finished"].state == STATE_KEPT_RECENT
        assert ws.is_dir()
        assert (ws / ".git" / "HEAD").is_file()

    @pytest.mark.parametrize("hours", DISABLED_THRESHOLDS)
    def test_delete_path_is_unreachable(self, work_tree: dict[str, Any], hours: int, monkeypatch: pytest.MonkeyPatch) -> None:
        import lh_harness.disk_retention.workspaces as mod

        called: list[str] = []
        monkeypatch.setattr(mod, "_delete_workspace", lambda p: called.append(str(p)))
        monkeypatch.setattr(mod.shutil, "rmtree", lambda *a, **k: called.append("rmtree"))
        sweep_workspaces(
            work_tree["root"],
            run_items=work_tree["run_items"],
            queue_entries=work_tree["queue_entries"],
            config=DiskRetentionConfig(workspace_retention_enabled=True, workspace_max_age_hours=hours),
            mode=RetainMode.live(),
            now=NOW,
        )
        assert called == []

    @pytest.mark.parametrize("raw", ["0", "-1", "-72"])
    def test_setting_value_reaches_the_predicate_as_disabled(self, tmp_path: Path, raw: str) -> None:
        """End to end through the settings loader: ``"0"``/``"-1"`` in the store -> rule off."""
        cfg = config_from_values(
            {cfg_mod.SETTING_WORKSPACE_RETENTION_ENABLED: "true", cfg_mod.SETTING_WORKSPACE_MAX_AGE_HOURS: raw}
        )
        assert cfg.workspace_retention_enabled is True
        assert cfg.workspace_max_age_hours == int(raw)
        root = tmp_path / "work"
        ws = _tree(root, "ws-old-terminal")
        run_items = [{"id": "r-old", "workspace": str(ws), "status": "completed", "finished_at": NOW - _old()}]
        reports = sweep_workspaces(root, run_items=run_items, config=cfg, mode=RetainMode.live(), now=NOW)
        assert reports[0].state == STATE_KEPT_RECENT
        assert ws.is_dir()


# =================================================================== settings
class TestSettings:
    def test_defaults_are_off_and_336h(self) -> None:
        assert DEFAULT_WORKSPACE_RETENTION_ENABLED is False
        assert DEFAULT_WORKSPACE_MAX_AGE_HOURS == 336
        assert DEFAULT_CONFIG.workspace_retention_enabled is False
        assert DEFAULT_CONFIG.workspace_max_age_hours == 336

    def test_config_from_values_without_settings_yields_defaults(self) -> None:
        cfg = config_from_values({})
        assert cfg.workspace_retention_enabled is False
        assert cfg.workspace_max_age_hours == 336
        assert config_from_values(None).workspace_retention_enabled is False

    def test_settings_are_registered_in_catalog_like_neighbours(self) -> None:
        from lh_harness.settings_store import CATALOG, CATALOG_BY_NAME, SettingSpec

        names = {s.name for s in CATALOG}
        for setting in (cfg_mod.SETTING_WORKSPACE_RETENTION_ENABLED, cfg_mod.SETTING_WORKSPACE_MAX_AGE_HOURS):
            assert setting in cfg_mod.SETTING_NAMES
            assert setting in names, setting
            spec = CATALOG_BY_NAME[setting]
            assert isinstance(spec, SettingSpec)
            assert spec.secret is False
            assert spec.restart is False

    def test_enabled_flag_requires_an_explicit_truthy_value(self) -> None:
        on = config_from_values({cfg_mod.SETTING_WORKSPACE_RETENTION_ENABLED: "true"})
        assert on.workspace_retention_enabled is True
        for raw in ("1", "true", "yes", "on", "enabled", "TRUE"):
            assert config_from_values({cfg_mod.SETTING_WORKSPACE_RETENTION_ENABLED: raw}).workspace_retention_enabled is True, raw
        for raw in ("", "0", "false", "no", "off", "disabled", None, "yep", "true!"):
            assert config_from_values({cfg_mod.SETTING_WORKSPACE_RETENTION_ENABLED: raw}).workspace_retention_enabled is False, repr(raw)

    def test_max_age_is_read_from_the_setting(self) -> None:
        cfg = config_from_values({cfg_mod.SETTING_WORKSPACE_MAX_AGE_HOURS: "6"})
        assert cfg.workspace_max_age_hours == 6
        # A mis-set value falls back to the default rather than 0 (which
        # would make everything selectable).
        assert config_from_values({cfg_mod.SETTING_WORKSPACE_MAX_AGE_HOURS: "soon"}).workspace_max_age_hours == 336

    def test_config_from_runtime_reads_store(self) -> None:
        runtime = SimpleNamespace(
            store=SimpleNamespace(values=lambda: {cfg_mod.SETTING_WORKSPACE_RETENTION_ENABLED: "true", cfg_mod.SETTING_WORKSPACE_MAX_AGE_HOURS: "12"})
        )
        cfg = dr.config_from_runtime(runtime)
        assert cfg.workspace_retention_enabled is True
        assert cfg.workspace_max_age_hours == 12


# ============================================================ dirty guard
# easybutt0n-304: a terminal-and-old clone is a candidate only when it is
# PROVED clean and pushed.  Every case below builds a REAL git repository
# under ``tmp_path`` (bare origin included, never a network URL) and drives
# the public sweep, so the probes under test are the real ``git`` calls.
# Each kept case pins all three faces of the rule: the inspection says keep,
# the dry-run listing carries the reason ``kept_uncommitted``, and an
# enforcement sweep (flag on, live mode) leaves the clone in place with the
# delete path never reached.
ENFORCE = dict(config=DiskRetentionConfig(workspace_retention_enabled=True), mode=RetainMode.live())


def _terminal_old_run(ws: Path) -> list[dict]:
    """One ``completed`` run that finished ``_old()`` ago and names ``ws``."""

    return [{"id": "r-old", "workspace": str(ws), "status": "completed", "finished_at": NOW - _old()}]


def _assert_kept_uncommitted(tmp_path: Path, ws: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Assert the three faces of a ``kept_uncommitted`` clone and return its report."""

    import lh_harness.disk_retention.workspaces as mod

    called: list[str] = []
    monkeypatch.setattr(mod, "_delete_workspace", lambda p: called.append(str(p)))
    monkeypatch.setattr(mod.shutil, "rmtree", lambda *a, **k: called.append("rmtree"))

    run_items = _terminal_old_run(ws)
    # 1. The inspection itself says keep.
    state = inspect_clone_git_state(ws)
    assert state.kept is True and state.disposable is False
    # 2. Dry-run lists it with the reason, and removes nothing.
    before = _snapshot(tmp_path)
    dry = sweep_workspaces(tmp_path / "work", run_items=run_items, config=DiskRetentionConfig(workspace_retention_enabled=True), mode=RetainMode.dry(), now=NOW)
    assert [r.state for r in dry] == [STATE_KEPT_UNCOMMITTED]
    assert dry[0].reason.startswith(f"{STATE_KEPT_UNCOMMITTED}: ")
    assert STATE_KEPT_UNCOMMITTED in dry[0].reason
    assert dry[0].size_bytes == 0  # never measured for removal
    assert dry[0].age_source == "run_finished_at"
    assert _snapshot(tmp_path) == before
    # 3. Enforcement (flag on, live) still keeps it; the delete path is unreachable.
    live = sweep_workspaces(tmp_path / "work", run_items=run_items, **ENFORCE, now=NOW)
    assert [r.state for r in live] == [STATE_KEPT_UNCOMMITTED]
    assert STATE_KEPT_UNCOMMITTED in live[0].reason
    assert ws.is_dir()
    assert _snapshot(tmp_path) == before
    assert called == []
    return live[0]


class TestDirtyGuardKeepsUncommittedClones:
    def test_uncommitted_tracked_change_is_kept(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "work"
        ws = _make_clone(root, "ws-dirty")
        (ws / "src" / "main.py").write_bytes(b"print('edited')\n")  # tracked, unstaged
        report = _assert_kept_uncommitted(tmp_path, ws, monkeypatch)
        assert "uncommitted" in report.reason

    def test_untracked_file_is_kept(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "work"
        ws = _make_clone(root, "ws-untracked")
        (ws / "notes.txt").write_text("scratch\n")  # never added
        report = _assert_kept_uncommitted(tmp_path, ws, monkeypatch)
        assert "uncommitted" in report.reason

    def test_unpushed_commit_is_kept(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "work"
        ws = _make_clone(root, "ws-unpushed")  # pushed, then one more local commit
        _commit(ws, "local only")
        report = _assert_kept_uncommitted(tmp_path, ws, monkeypatch)
        assert "not on any remote branch" in report.reason

    @pytest.mark.parametrize("shape", ["no_upstream", "detached_head"])
    def test_branch_without_upstream_is_kept(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str) -> None:
        """``@{upstream}`` unresolvable — a branch never pushed -u, or a detached HEAD."""
        root = tmp_path / "work"
        if shape == "no_upstream":
            ws = _make_clone(root, "ws-no-upstream", pushed=False)  # origin set, never -u pushed
            # Push the commit through the back door so ONLY the upstream is missing:
            # the branch itself carries no tracking config, so "pushed" cannot be proved.
            _git(ws, "push", "-q", "origin", "main")
            _git(ws, "fetch", "-q", "origin")
            assert _git(ws, "rev-list", "--count", "HEAD", "--not", "--remotes").strip() == "0"
        else:
            ws = _make_clone(root, "ws-detached")  # fully pushed, then HEAD detached
            _git(ws, "checkout", "-q", "--detach", "HEAD")
        report = _assert_kept_uncommitted(tmp_path, ws, monkeypatch)
        assert "no upstream" in report.reason

    @pytest.mark.parametrize("how", ["nonzero_exit", "timeout"])
    def test_git_failure_is_kept(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str) -> None:
        """A git that ACTUALLY fails — non-zero exit, or hangs past the bounded timeout — reads as keep, never as clean."""
        import subprocess

        import lh_harness.disk_retention.workspaces as mod

        root = tmp_path / "work"
        ws = _make_clone(root, "ws-git-fails")
        # A git shim first on PATH: ``git_argv`` resolves bare ``git`` and
        # ``child_env()`` copies ``os.environ`` at call time, so it is honoured.
        shim = tmp_path / "shim"
        shim.mkdir()
        if how == "nonzero_exit":
            (shim / "git").write_text("#!/bin/sh\necho 'shim: git is broken' >&2\nexit 128\n")
        else:
            # Absolute sleep: with no PATH a bare ``sleep`` exits 127 instead
            # of hanging, and the probe would report exit 127, not a timeout.
            (shim / "git").write_text(f"#!/bin/sh\n{shutil.which('sleep') or '/bin/sleep'} 60\n")
        (shim / "git").chmod(0o755)
        # ``os.environ['PATH']`` is absent in some environments (the PR-gate
        # runner runs without one); a keep-only guard must not depend on it.
        monkeypatch.setenv("PATH", os.pathsep.join(p for p in (str(shim), os.environ.get("PATH", "")) if p))
        if how == "timeout":
            monkeypatch.setattr(mod, "GIT_PROBE_TIMEOUT_SECONDS", 0.5)
        # The shim really is the git the probe runs, and it really fails.
        if how == "nonzero_exit":
            proc = subprocess.run(["git", "--version"], capture_output=True, text=True, env=dict(os.environ))
            assert proc.returncode == 128 and "broken" in proc.stderr
        run_items = _terminal_old_run(ws)
        called: list[str] = []
        monkeypatch.setattr(mod, "_delete_workspace", lambda p: called.append(str(p)))
        monkeypatch.setattr(mod.shutil, "rmtree", lambda *a, **k: called.append("rmtree"))
        before = _snapshot(tmp_path)
        # Dry-run lists it with the reason; enforcement keeps it; delete is unreachable.
        dry = sweep_workspaces(tmp_path / "work", run_items=run_items, config=DiskRetentionConfig(workspace_retention_enabled=True), mode=RetainMode.dry(), now=NOW)
        live = sweep_workspaces(tmp_path / "work", run_items=run_items, **ENFORCE, now=NOW)
        for report in (dry[0], live[0]):
            assert report.state == STATE_KEPT_UNCOMMITTED
            assert report.reason.startswith(f"{STATE_KEPT_UNCOMMITTED}: ")
            assert "git probe failed" in report.reason
        if how == "nonzero_exit":
            assert "exited 128" in live[0].reason
        else:
            assert "timeout" in live[0].reason
        assert ws.is_dir()
        assert _snapshot(tmp_path) == before
        assert called == []

    def test_non_repo_directory_is_kept(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "work"
        ws = _tree(root, "ws-not-a-repo")  # a plain directory: no ``.git`` at all
        report = _assert_kept_uncommitted(tmp_path, ws, monkeypatch)
        assert "not a git repository" in report.reason

    def test_ignored_file_is_kept(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Plain ``git status --porcelain`` does not list ignored files: a clone
        whose only work sits in an ignored path reads clean and would be removed.
        Any ignored entry is a keep, reason ``ignored files present``."""
        root = tmp_path / "work"
        # The .gitignore is COMMITTED (part of the initial commit), so the only
        # thing in the tree that is not pushed is the ignored file itself.
        ws = _make_clone(root, "ws-ignored", files={"src/main.py": b"print('hi')\n", ".gitignore": b"*.log\n"})
        (ws / "debug.log").write_text("ignored work\n")
        # Isolation of the trigger: plain porcelain is empty (not dirty), and
        # the file is listed only under --ignored.
        assert _git(ws, "status", "--porcelain") == ""
        assert _git(ws, "status", "--porcelain", "--ignored=matching").splitlines() == ["!! debug.log"]
        assert _git(ws, "ls-files", "--others", "--ignored", "--exclude-standard").split() == ["debug.log"]
        report = _assert_kept_uncommitted(tmp_path, ws, monkeypatch)
        assert report.reason == f"{STATE_KEPT_UNCOMMITTED}: ignored files present; kept"

    def test_clean_pushed_old_clone_is_still_removed_under_enforcement(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Control: the one shape the guard lets through is removed exactly as before.

        Also pins the shape of the probes that let it through: three read-only
        git calls, each with ``--no-optional-locks`` and the hardened argv, each
        bounded by ``GIT_PROBE_TIMEOUT_SECONDS`` (<= 10 s).
        """
        import subprocess

        root = tmp_path / "work"
        ws = _make_clone(root, "ws-clean-pushed")  # clean, upstream set, remote carries HEAD
        assert GIT_PROBE_TIMEOUT_SECONDS <= 10.0
        seen: list[tuple[list[str], float]] = []
        real = subprocess.run

        def _spy(argv: Any, **kw: Any) -> Any:
            # Only the probe calls pass a timeout; the test's own ``_git``
            # helper (no timeout) is passed straight through, unrecorded.
            if kw.get("timeout") is not None:
                seen.append((list(argv), float(kw["timeout"])))
            return real(argv, **kw)

        monkeypatch.setattr(subprocess, "run", _spy)
        assert inspect_clone_git_state(ws).disposable is True
        assert len(seen) == 3
        for argv, timeout in seen:
            assert argv[0] == "git"
            assert "--no-optional-locks" in argv
            assert "-c" in argv and "core.fsmonitor=" in argv and "core.hooksPath=/dev/null" in argv
            assert timeout == GIT_PROBE_TIMEOUT_SECONDS
        assert [argv[argv.index("-C") + 2] for argv, _ in seen] == ["status", "rev-list", "rev-parse"]  # all read-only
        monkeypatch.setattr(subprocess, "run", real)
        run_items = _terminal_old_run(ws)
        # Dry-run: still the one candidate, with a size.
        dry = sweep_workspaces(root, run_items=run_items, config=DiskRetentionConfig(workspace_retention_enabled=True), mode=RetainMode.dry(), now=NOW)
        assert [r.state for r in dry] == [STATE_SELECTABLE]
        assert dry[0].size_bytes > 0
        # Enforcement: removed.
        reports = sweep_workspaces(root, run_items=run_items, **ENFORCE, now=NOW)
        assert [r.state for r in reports] == [STATE_REMOVED]
        assert not ws.exists()


def test_workspace_max_age_default_is_336() -> None:
    """``LH_HARNESS_DISK_WORKSPACE_MAX_AGE_HOURS`` defaults to 336 (14 days), not 72."""
    assert DEFAULT_WORKSPACE_MAX_AGE_HOURS == 336
    assert DEFAULT_WORKSPACE_MAX_AGE_HOURS == 14 * 24
    assert DEFAULT_CONFIG.workspace_max_age_hours == 336
    assert config_from_values({}).workspace_max_age_hours == 336
    assert config_from_values(None).workspace_max_age_hours == 336
    # Unset in the store -> 336; the setting still overrides when present.
    assert config_from_values({cfg_mod.SETTING_WORKSPACE_MAX_AGE_HOURS: None}).workspace_max_age_hours == 336
    assert config_from_values({cfg_mod.SETTING_WORKSPACE_MAX_AGE_HOURS: "6"}).workspace_max_age_hours == 6
    # The admin help string states the default and the 0/negative disable.
    from lh_harness.settings_store import CATALOG_BY_NAME

    spec = CATALOG_BY_NAME[cfg_mod.SETTING_WORKSPACE_MAX_AGE_HOURS]
    assert "336" in spec.description and "14 days" in spec.description
    assert "0 or a negative" in spec.description and "disables" in spec.description


# =============================================================== wiring
class TestWiring:
    def test_package_exports_the_surface(self) -> None:
        for name in ("should_retain_workspace", "sweep_workspaces", "sweep_workspaces_report", "WorkspaceRetentionReport"):
            assert name in dr.__all__, name
            assert getattr(dr, name) is not None

    def test_cli_retention_has_work_root_flag(self) -> None:
        from lh_harness.cli import main as cli_main

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
            cli_main(["retention", "--help"])
        assert exc.value.code == 0
        assert "--work-root" in buf.getvalue()

    def test_cli_command_passes_flag_and_max_age_in_report(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`lh-harness retention --work-root` lists candidates with the flag OFF and deletes nothing."""
        from lh_harness import cli as cli_mod

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        root = tmp_path / "work"
        ws = _make_clone(root, "ws-old")
        (root / ".lh-harness" / "runs").mkdir(parents=True)
        before = _snapshot(root)
        monkeypatch.setattr(cli_mod, "_workspace_retention_inputs", lambda r: ([{"workspace": str(ws), "status": "completed", "finished_at": NOW - _old()}], []))
        monkeypatch.setattr("lh_harness.settings_store.active_runtime", lambda: SimpleNamespace(store=None))
        report = tmp_path / "report.json"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli_mod._retention_command(
                SimpleNamespace(apply=True, runs_root=str(runs_root), worktree_dirs=[], npm_cache=None, pip_cache=None, report=str(report), work_root=str(root))
            )
        assert code == 0
        payload = json.loads(report.read_text())
        ws_payload = payload["workspaces"]
        # --apply was given but the flag is off by default: listing only.
        assert payload["config"]["workspace_retention_enabled"] is False
        assert ws_payload["mode"] == "apply"
        assert ws_payload["enabled"] is False
        assert ws_payload["totals"]["candidates"] == 1
        by_name = {os.path.basename(e["path"]): e for e in ws_payload["entries"]}
        # The run-evidence sibling is listed as harness-owned (never a
        # candidate); the old terminal clone is the one candidate.
        assert by_name[".lh-harness"]["state"] == STATE_KEPT_HARNESS_OWNED
        assert by_name["ws-old"]["state"] == STATE_SELECTABLE
        assert by_name["ws-old"]["size_bytes"] > 0
        assert _snapshot(root) == before

    def test_cli_command_without_work_root_has_no_workspace_section(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from lh_harness import cli as cli_mod

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        monkeypatch.setattr("lh_harness.settings_store.active_runtime", lambda: SimpleNamespace(store=None))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli_mod._retention_command(
                SimpleNamespace(apply=False, runs_root=str(runs_root), worktree_dirs=[], npm_cache=None, pip_cache=None, report=str(tmp_path / "r.json"), work_root=None)
            )
        payload = json.loads((tmp_path / "r.json").read_text())
        assert "workspaces" not in payload

    def test_launcher_sweep_runs_each_pass_and_fails_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The launcher pass sweeps supervisor.workspace_root only when the flag is on; a broken sweep never raises."""
        from lh_harness.launcher import Launcher

        root = tmp_path / "work"
        ws = _make_clone(root, "ws-old")
        events: list[tuple[str, dict]] = []
        touched: list[str] = []
        run_items = [{"workspace": str(ws), "status": "completed", "finished_at": NOW - _old()}]

        def _list_run_items() -> list[dict]:
            touched.append("list_run_items")
            return run_items

        supervisor = SimpleNamespace(workspace_root=str(root), runs_root=str(tmp_path / "runs"), list_run_items=_list_run_items)
        store = SimpleNamespace(list=lambda: (touched.append("queue.list") or []), runs_root=None)

        def _launcher() -> Any:
            launcher = Launcher.__new__(Launcher)
            launcher.supervisor = supervisor
            launcher.queue_store = store
            launcher._emit_service_event = lambda et, p: events.append((et, p))
            return launcher

        # Flag off (the default): the launcher never touches the work root
        # or the state sources at all, and nothing is removed.
        monkeypatch.setattr("lh_harness.settings_store.active_runtime", lambda: SimpleNamespace(store=None))
        Launcher._sweep_terminal_workspaces(_launcher())
        assert ws.is_dir()
        assert touched == []
        assert events == []

        # Flag on -> live mode removes the candidate and emits one event.
        monkeypatch.setattr(
            "lh_harness.settings_store.active_runtime",
            lambda: SimpleNamespace(store=SimpleNamespace(values=lambda: {cfg_mod.SETTING_WORKSPACE_RETENTION_ENABLED: "true"})),
        )
        Launcher._sweep_terminal_workspaces(_launcher())
        assert not ws.exists()
        assert touched == ["list_run_items", "queue.list"]
        assert events and events[0][0] == "workspace.retention"
        assert events[0][1]["removed"] == 1
        assert events[0][1]["max_age_hours"] == DEFAULT_WORKSPACE_MAX_AGE_HOURS

        # A sweep that raises must not propagate into the pass.
        broken = Launcher.__new__(Launcher)
        broken.supervisor = SimpleNamespace(workspace_root=str(root), list_run_items=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        broken.queue_store = store
        broken._emit_service_event = lambda et, p: events.append((et, p))
        Launcher._sweep_terminal_workspaces(broken)  # no exception

    def test_launcher_without_workspace_root_is_a_noop(self) -> None:
        from lh_harness.launcher import Launcher

        launcher = Launcher.__new__(Launcher)
        launcher.supervisor = SimpleNamespace(runs_root="/x")  # no workspace_root
        launcher.queue_store = SimpleNamespace(list=lambda: (_ for _ in ()).throw(AssertionError("must not be called")))
        Launcher._sweep_terminal_workspaces(launcher)  # returns before touching anything

    def test_supervisor_list_run_items_carries_finished_at(self) -> None:
        """The run listing (the collector's input) carries the run's terminal timestamp."""
        import inspect

        from lh_harness.supervisor import service

        source = inspect.getsource(service.RunSupervisor.list_run_items)
        assert '"finished_at"' in source
