"""Unit tests for the disk-retention subsystem (CT110-DISK).

Every rule has at least one test that pins the documented behaviour:

* Run-folder age threshold = 14 d
* Failed-run report move before deletion
* Live runs (running / gated / queued) are never touched
* Work-tree idle threshold = 7 d
* Dirty work trees are listed, not deleted
* Cache cap default = 4 GiB each
* Dry-run mode is the default
* Warn at 80 %, pause at 90 %, resume at 85 %
* All thresholds are read from the settings DB
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lh_harness import disk_retention as dr
from lh_harness.disk_retention import (
    DEFAULT_CONFIG,
    DiskRetentionConfig,
    RetainMode,
    check_disk,
    config_from_runtime,
    config_from_values,
    disk_skip_reason,
    format_warning_event,
    prune_caches,
    prune_caches_report,
    prune_runs,
    prune_runs_report,
    prune_worktrees,
    prune_worktrees_report,
)
from lh_harness.disk_retention import config as cfg_mod
from lh_harness.disk_retention.caches import CacheReport
from lh_harness.disk_retention.disk_check import (
    SKIP_REASON_DISK_FULL,
    STATE_OK,
    STATE_PAUSED,
    STATE_RESUMED,
    STATE_WARN,
)
from lh_harness.disk_retention.runs import (
    FAILED_REPORT_FILES,
    RunRetentionReport,
)


# --------------------------------------------------------------------- helpers
def _make_old(path: Path, age_seconds: float) -> None:
    """Set the mtime of ``path`` to ``age_seconds`` ago."""

    target = time.time() - age_seconds
    os.utime(path, (target, target))


def _make_recent(path: Path, age_seconds: float = 60.0) -> None:
    _make_old(path, age_seconds)


def _write_config_file() -> None:  # placeholder, kept for symmetry
    pass


# ----------------------------------------------------------- run-folder tests
def test_run_folder_age_threshold_14_days(tmp_path: Path) -> None:
    """Runs older than 14 days are pruned; newer ones are kept."""

    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    old = runs_root / "20261001T000000Z_oldrun"
    old.mkdir()
    (old / "log.txt").write_text("x")
    _make_old(old, 15 * 86400.0)
    fresh = runs_root / "20261017T000000Z_fresh"
    fresh.mkdir()
    (fresh / "log.txt").write_text("x")
    _make_recent(fresh)

    reports = prune_runs(runs_root)
    by_id = {r.run_id: r for r in reports}
    assert by_id["20261001T000000Z_oldrun"].state == "pruned"
    assert by_id["20261017T000000Z_fresh"].state == "skipped_recent"
    # Dry-run keeps the folder on disk.
    assert old.exists()
    assert fresh.exists()


def test_run_folder_failed_reports_moved_before_deletion(tmp_path: Path) -> None:
    """Failed-run report/summary files are copied to _failed-reports/<id>/ before the folder is removed."""

    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    failed_run = runs_root / "20261001T000000Z_failed"
    failed_run.mkdir()
    (failed_run / "report.json").write_text(json.dumps({"status": "failed"}))
    (failed_run / "summary.json").write_text(json.dumps({"rounds_run": 1}))
    (failed_run / "log.txt").write_text("trail")
    (failed_run / "noise.bin").write_bytes(b"\x00\x01")  # not part of the report set
    _make_old(failed_run, 30 * 86400.0)

    reports = prune_runs(runs_root, mode=RetainMode.live())
    by_id = {r.run_id: r for r in reports}
    rep = by_id["20261001T000000Z_failed"]
    assert rep.state == "pruned"
    assert rep.moved_to.endswith("_failed-reports/20261001T000000Z_failed")
    # Folder gone, but the report files live in _failed-reports.
    assert not failed_run.exists()
    failed_dir = runs_root / "_failed-reports" / "20261001T000000Z_failed"
    assert (failed_dir / "report.json").exists()
    assert (failed_dir / "summary.json").exists()
    assert (failed_dir / "log.txt").exists()
    # The non-report file is not preserved.
    assert not (failed_dir / "noise.bin").exists()
    # And the report references the moved files.
    moved_names = {Path(p).name for p in rep.failed_report_paths}
    assert {"report.json", "summary.json", "log.txt"} <= moved_names


def test_run_folder_live_runs_are_never_touched(tmp_path: Path) -> None:
    """A run in state running/gated/queued is skipped regardless of age."""

    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    live = runs_root / "running-now"
    live.mkdir()
    (live / "log.txt").write_text("live")
    _make_old(live, 60 * 86400.0)
    live_lookup = {"running-now": "running", "gated-now": "gated", "queued-now": "queued"}

    for state in ("gated", "queued"):
        d = runs_root / f"{state}-now"
        d.mkdir()
        (d / "log.txt").write_text(state)
        _make_old(d, 60 * 86400.0)

    reports = prune_runs(runs_root, live_lookup=live_lookup, mode=RetainMode.live())
    by_id = {r.run_id: r for r in reports}
    assert by_id["running-now"].state == "skipped_live"
    assert by_id["gated-now"].state == "skipped_live"
    assert by_id["queued-now"].state == "skipped_live"
    # All three folders are still on disk.
    assert live.exists()
    assert (runs_root / "gated-now").exists()
    assert (runs_root / "queued-now").exists()


def test_run_folder_terminal_state_old_is_pruned(tmp_path: Path) -> None:
    """A terminal run (state = 'failed') past 14 days is pruned normally."""

    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    terminal = runs_root / "completed"
    terminal.mkdir()
    (terminal / "report.json").write_text(json.dumps({"status": "failed"}))
    _make_old(terminal, 30 * 86400.0)
    live_lookup = {"completed": "failed"}
    reports = prune_runs(runs_root, live_lookup=live_lookup, mode=RetainMode.live())
    assert reports[0].state == "pruned"
    assert not terminal.exists()


def test_run_folder_dry_run_default(tmp_path: Path) -> None:
    """prune_runs defaults to dry-run; no folders are deleted."""

    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    old = runs_root / "old"
    old.mkdir()
    (old / "report.json").write_text("x")
    _make_old(old, 30 * 86400.0)
    # Default mode (no mode= arg).
    reports = prune_runs(runs_root)
    by_id = {r.run_id: r for r in reports}
    assert by_id["old"].state == "pruned"
    # The folder survives dry-run.
    assert old.exists()
    # And the report file is NOT moved.
    assert (old / "report.json").exists()


def test_run_folder_apply_actually_deletes(tmp_path: Path) -> None:
    """``mode=RetainMode.live()`` (or apply=True) actually moves reports and deletes the folder."""

    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    old = runs_root / "old"
    old.mkdir()
    (old / "report.json").write_text("x")
    _make_old(old, 30 * 86400.0)
    prune_runs(runs_root, mode=RetainMode.live())
    assert not old.exists()
    assert (runs_root / "_failed-reports" / "old" / "report.json").exists()


# --------------------------------------------------------- work-tree tests
def test_worktree_idle_threshold_7_days(tmp_path: Path) -> None:
    """Trees idle more than 7 days are removed; newer ones are kept."""

    parent = tmp_path / "wts"
    parent.mkdir()
    old = parent / "wt-old"
    old.mkdir()
    (old / ".lh-park").write_text("parked")
    _make_old(old, 8 * 86400.0)
    fresh = parent / "wt-fresh"
    fresh.mkdir()
    (fresh / ".lh-park").write_text("parked")
    _make_recent(fresh)

    reports = prune_worktrees([parent], mode=RetainMode.live())
    by_path = {r.path: r for r in reports}
    assert by_path[str(old)].state == "removed"
    assert by_path[str(fresh)].state == "skipped_recent"
    assert not old.exists()
    assert fresh.exists()


def test_worktree_dirty_listed_not_deleted(tmp_path: Path) -> None:
    """A dirty work tree is reported as ``skipped_dirty`` and left on disk."""

    parent = tmp_path / "wts"
    parent.mkdir()
    tree = parent / "wt-dirty"
    tree.mkdir()
    (tree / ".lh-park").write_text("parked")
    (tree / "uncommitted.txt").write_text("work in progress")
    _make_old(tree, 30 * 86400.0)
    reports = prune_worktrees([parent], mode=RetainMode.live())
    by_path = {r.path: r for r in reports}
    rep = by_path[str(tree)]
    assert rep.state == "skipped_dirty"
    assert tree.exists()
    assert (tree / "uncommitted.txt").exists()


def test_worktree_no_marker_skipped(tmp_path: Path) -> None:
    """A directory without the .lh-park marker is left alone (not a parked tree)."""

    parent = tmp_path / "wts"
    parent.mkdir()
    plain = parent / "not-a-tree"
    plain.mkdir()
    (plain / "stuff.txt").write_text("x")
    _make_old(plain, 30 * 86400.0)
    reports = prune_worktrees([parent], mode=RetainMode.live())
    by_path = {r.path: r for r in reports}
    assert by_path[str(plain)].state == "skipped_no_marker"
    assert plain.exists()


def test_worktree_active_owner_kept(tmp_path: Path) -> None:
    """A work tree owned by a live run is never removed."""

    parent = tmp_path / "wts"
    parent.mkdir()
    tree = parent / "wt-active"
    tree.mkdir()
    (tree / ".lh-park").write_text("parked")
    _make_old(tree, 30 * 86400.0)
    active = {str(tree.resolve()): "run-1234"}
    reports = prune_worktrees([parent], active_workspaces=active, mode=RetainMode.live())
    by_path = {r.path: r for r in reports}
    assert by_path[str(tree)].state == "skipped_live"
    assert tree.exists()


# --------------------------------------------------------- cache cap tests
def test_cache_default_4gb_each(tmp_path: Path) -> None:
    """The default cap for both npm and pip is 4 GiB."""

    assert DEFAULT_CONFIG.npm_cache_max_bytes == 4 * 1024**3
    assert DEFAULT_CONFIG.pip_cache_max_bytes == 4 * 1024**3
    assert DEFAULT_CONFIG.cache_max_bytes == 4 * 1024**3


def test_cache_cap_enforced_with_oversize(tmp_path: Path) -> None:
    """An oversize cache is trimmed at or under the cap; oldest entries go first."""

    cap = 100  # bytes
    cache = tmp_path / "npm"
    cache.mkdir()
    files = []
    for i in range(5):
        f = cache / f"entry-{i}.tar"
        f.write_bytes(b"x" * 40)
        files.append(f)
    # Make entry-0 the oldest; entry-1 a bit newer; the rest "now".
    _make_old(files[0], 1000.0)
    _make_old(files[1], 500.0)
    _make_recent(files[2], 60.0)
    _make_recent(files[3], 30.0)
    _make_recent(files[4], 0.0)
    reports = prune_caches({"npm": str(cache)}, config=DiskRetentionConfig(npm_cache_max_bytes=cap), mode=RetainMode.live())
    assert len(reports) == 1
    rep = reports[0]
    assert rep.cap_bytes == cap
    assert rep.initial_bytes == 5 * 40
    assert rep.final_bytes <= cap
    # Oldest entry is gone.
    assert not files[0].exists()


def test_cache_cap_zero_disables(tmp_path: Path) -> None:
    """A cap of 0 means 'no cap' — nothing is removed."""

    cache = tmp_path / "npm"
    cache.mkdir()
    f = cache / "entry-0.tar"
    f.write_bytes(b"x" * 40)
    reports = prune_caches({"npm": str(cache)}, config=DiskRetentionConfig(npm_cache_max_bytes=0))
    assert reports[0].removed_count == 0
    assert f.exists()


def test_cache_cap_under_size_noop(tmp_path: Path) -> None:
    """A cache under the cap is left untouched."""

    cache = tmp_path / "npm"
    cache.mkdir()
    f = cache / "entry-0.tar"
    f.write_bytes(b"x" * 10)
    reports = prune_caches({"npm": str(cache)}, config=DiskRetentionConfig(npm_cache_max_bytes=100))
    assert reports[0].initial_bytes == 10
    assert reports[0].removed_count == 0
    assert f.exists()


def test_cache_per_cache_override(tmp_path: Path) -> None:
    """Per-cache settings override the umbrella cap when set."""

    cfg = DiskRetentionConfig(
        cache_max_bytes=10,
        npm_cache_max_bytes=200,
        pip_cache_max_bytes=20,
    )
    assert cfg.cache_max_bytes_for("npm") == 200
    assert cfg.cache_max_bytes_for("pip") == 20
    # An unknown cache name falls back to the umbrella cap.
    assert cfg.cache_max_bytes_for("pnpm") == 10


def test_cache_per_cache_fallback_when_unset() -> None:
    """A per-cache setting that was never set falls back to the umbrella cap."""

    cfg = config_from_values(
        {
            cfg_mod.SETTING_CACHE_MAX_BYTES: "500",
            cfg_mod.SETTING_NPM_CACHE_MAX_BYTES: "200",
            # pip setting is absent -> falls back to the umbrella.
        }
    )
    assert cfg.cache_max_bytes == 500
    assert cfg.npm_cache_max_bytes == 200
    assert cfg.pip_cache_max_bytes == 500


# --------------------------------------------------------- disk-check tests
def test_disk_state_warn_at_80_pct(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """80 % usage reports ``warn`` and no skip reason."""

    sv = os.statvfs(str(tmp_path))
    fake = _fake_statvfs(total=100, used=85, free=15)
    monkeypatch.setattr(
        "lh_harness.disk_retention.disk_check._statvfs", lambda path: fake
    )
    state = check_disk(str(tmp_path))
    assert state.percent_used == 85.0
    assert state.state == STATE_WARN
    assert state.skip_reason is None
    event = format_warning_event(state)
    assert event["kind"] == "disk.warning"
    assert event["percent_used"] == 85.0


def test_disk_state_pause_at_90_pct(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """90 % usage pauses the launcher with ``disk_full``."""

    fake = _fake_statvfs(total=100, used=92, free=8)
    monkeypatch.setattr(
        "lh_harness.disk_retention.disk_check._statvfs", lambda path: fake
    )
    state = check_disk(str(tmp_path))
    assert state.state == STATE_PAUSED
    assert state.skip_reason == SKIP_REASON_DISK_FULL
    assert disk_skip_reason(str(tmp_path)) == SKIP_REASON_DISK_FULL


def test_disk_state_resume_under_85_pct(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """After a pause, dropping below 85 % reports ``resumed`` (was previously paused)."""

    fake = _fake_statvfs(total=100, used=70, free=30)
    monkeypatch.setattr(
        "lh_harness.disk_retention.disk_check._statvfs", lambda path: fake
    )
    state = check_disk(str(tmp_path), previously_paused=True)
    assert state.state == STATE_RESUMED
    # ``disk_skip_reason`` honours the sticky-pause argument: still >=
    # resume_pct, the launcher would keep refusing; below it, it resumes.
    assert disk_skip_reason(str(tmp_path), sticky_pause=False) is None


def test_disk_skip_reason_hysteresis(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Sticky-pause only releases once usage is below resume_pct."""

    # 86 % is between resume_pct (85) and pause_pct (90) — sticky still holds.
    fake = _fake_statvfs(total=100, used=86, free=14)
    monkeypatch.setattr(
        "lh_harness.disk_retention.disk_check._statvfs", lambda path: fake
    )
    assert disk_skip_reason(str(tmp_path), sticky_pause=True) == SKIP_REASON_DISK_FULL
    # Drop to 80 % — sticky releases.
    fake = _fake_statvfs(total=100, used=80, free=20)
    monkeypatch.setattr(
        "lh_harness.disk_retention.disk_check._statvfs", lambda path: fake
    )
    assert disk_skip_reason(str(tmp_path), sticky_pause=True) is None


# --------------------------------------------------------- settings-DB tests
def test_settings_db_loader_returns_thresholds() -> None:
    """All eight disk-retention settings map to config fields with the right types."""

    values = {
        cfg_mod.SETTING_RUN_MAX_AGE_DAYS: "21",
        cfg_mod.SETTING_WORKTREE_MAX_AGE_DAYS: "5",
        cfg_mod.SETTING_CACHE_MAX_BYTES: "8589934592",  # 8 GiB
        cfg_mod.SETTING_NPM_CACHE_MAX_BYTES: "1G",
        cfg_mod.SETTING_PIP_CACHE_MAX_BYTES: "2G",
        cfg_mod.SETTING_WARN_PCT: "75",
        cfg_mod.SETTING_PAUSE_PCT: "88",
        cfg_mod.SETTING_RESUME_PCT: "82",
    }
    cfg = config_from_values(values)
    assert cfg.run_max_age_days == 21
    assert cfg.worktree_max_age_days == 5
    assert cfg.cache_max_bytes == 8 * 1024**3
    assert cfg.npm_cache_max_bytes == 1 * 1024**3
    assert cfg.pip_cache_max_bytes == 2 * 1024**3
    assert cfg.warn_pct == 75
    assert cfg.pause_pct == 88
    assert cfg.resume_pct == 82


def test_settings_db_loader_malformed_values_fall_back() -> None:
    """A bad value is logged and ignored; the default takes its place."""

    cfg = config_from_values(
        {
            cfg_mod.SETTING_RUN_MAX_AGE_DAYS: "not-a-number",
            cfg_mod.SETTING_CACHE_MAX_BYTES: "lots",
        }
    )
    assert cfg.run_max_age_days == cfg_mod.DEFAULT_RUN_MAX_AGE_DAYS
    assert cfg.cache_max_bytes == cfg_mod.DEFAULT_CACHE_MAX_BYTES


def test_settings_db_loader_uses_runtime() -> None:
    """config_from_runtime reads the store; a missing store yields the default config."""

    runtime = SimpleNamespace(store=None)
    assert config_from_runtime(runtime) == DEFAULT_CONFIG
    runtime = SimpleNamespace(store=SimpleNamespace(values=lambda: {}))
    assert config_from_runtime(runtime) == DEFAULT_CONFIG


def test_settings_catalog_registers_all_eight() -> None:
    """The eight disk-retention settings appear in the settings-DB catalog."""

    from lh_harness.settings_store import CATALOG, CATALOG_BY_NAME, SettingSpec

    names = {s.name for s in CATALOG}
    for setting in cfg_mod.SETTING_NAMES:
        assert setting in names, f"{setting} missing from CATALOG"
        spec = CATALOG_BY_NAME[setting]
        assert isinstance(spec, SettingSpec)
        # None of the disk-retention settings are secrets.
        assert spec.secret is False


# ------------------------------------------------------------ report shape ---
def test_prune_runs_report_is_jsonable(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    old = runs_root / "old"
    old.mkdir()
    _make_old(old, 30 * 86400.0)
    payload = prune_runs_report(runs_root)
    encoded = json.dumps(payload, sort_keys=True)
    decoded = json.loads(encoded)
    assert decoded["totals"]["by_state"]["pruned"] == 1
    assert decoded["entries"][0]["run_id"] == "old"


def test_prune_caches_report_is_jsonable(tmp_path: Path) -> None:
    cache = tmp_path / "npm"
    cache.mkdir()
    (cache / "a").write_bytes(b"x" * 50)
    payload = prune_caches_report({"npm": str(cache)}, config=DiskRetentionConfig(npm_cache_max_bytes=10))
    json.dumps(payload, sort_keys=True)


def test_prune_worktrees_report_is_jsonable(tmp_path: Path) -> None:
    parent = tmp_path / "wts"
    parent.mkdir()
    payload = prune_worktrees_report([parent])
    json.dumps(payload, sort_keys=True)


# --------------------------------------------------------- launcher wiring ---
class _StubLauncher:
    """A minimal stub that exposes only what the real ``Launcher._disk_full_skip_reason`` reads."""

    def __init__(self, runs_root: str | None = None, drain: str | None = None) -> None:
        self.supervisor = SimpleNamespace(runs_root=runs_root)
        self._drain_reason = drain
        self._disk_full_sticky = False

    def _disk_full_skip_reason(self):  # invoked by the real launcher
        from lh_harness.launcher import Launcher

        return Launcher._disk_full_skip_reason(self)


def test_launcher_disk_full_skip_reason(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The launcher's _disk_full_skip_reason returns 'disk_full' when the disk is over pause_pct."""

    fake = _fake_statvfs(total=100, used=95, free=5)
    monkeypatch.setattr(
        "lh_harness.disk_retention.disk_check._statvfs", lambda path: fake
    )
    # Patch the settings store to a no-op runtime.
    monkeypatch.setattr(
        "lh_harness.settings_store.active_runtime",
        lambda: SimpleNamespace(store=None),
    )
    launcher = _StubLauncher(runs_root=str(tmp_path))
    reason = launcher._disk_full_skip_reason()
    assert reason == SKIP_REASON_DISK_FULL
    # The sticky flag flipped on.
    assert launcher._disk_full_sticky is True


def test_launcher_disk_full_resume_clears_skip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """After disk drops below resume_pct, the launcher no longer refuses."""

    fake = _fake_statvfs(total=100, used=70, free=30)
    monkeypatch.setattr(
        "lh_harness.disk_retention.disk_check._statvfs", lambda path: fake
    )
    monkeypatch.setattr(
        "lh_harness.settings_store.active_runtime",
        lambda: SimpleNamespace(store=None),
    )
    launcher = _StubLauncher(runs_root=str(tmp_path))
    launcher._disk_full_sticky = True  # simulate a previous pause
    reason = launcher._disk_full_skip_reason()
    assert reason is None
    assert launcher._disk_full_sticky is False


def test_launcher_disk_full_missing_runtime_fails_open(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A broken disk-check must never freeze the launcher (fail-open)."""

    def _boom(path):  # noqa: ARG001
        raise RuntimeError("statvfs exploded")

    monkeypatch.setattr(
        "lh_harness.disk_retention.disk_check._statvfs", _boom
    )
    monkeypatch.setattr(
        "lh_harness.settings_store.active_runtime",
        lambda: SimpleNamespace(store=None),
    )
    launcher = _StubLauncher(runs_root=str(tmp_path))
    reason = launcher._disk_full_skip_reason()
    assert reason is None


# --------------------------------------------------------- helpers (private)
def _fake_statvfs(*, total: int, used: int, free: int) -> os.statvfs_result:
    """Build a statvfs result with the given total/used/free bytes.

    The real ``os.statvfs`` returns a 10-tuple in field order:
    ``f_bsize, f_frsize, f_blocks, f_bfree, f_bavail, f_files, f_ffree,
    f_favail, f_flag, f_namemax``.  The disk-check module only reads
    ``f_blocks`` and ``f_bavail`` (and ``f_frsize``), so we set those
    correctly and pad the rest with zeros.
    """

    frsize = 1
    blocks = total // frsize
    bavail = free // frsize
    return os.statvfs_result(
        (
            frsize,  # f_bsize
            frsize,  # f_frsize
            blocks,  # f_blocks
            0,  # f_bfree
            bavail,  # f_bavail
            0,  # f_files
            0,  # f_ffree
            0,  # f_favail
            0,  # f_flag
            255,  # f_namemax
        )
    )
