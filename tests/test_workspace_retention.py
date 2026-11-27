"""Terminal-run clone-workspace retention (easybutt0n-17).

Pins the documented behaviour of ``lh_harness.disk_retention.workspaces``:

* the selection predicate is pure and deny-by-default;
* the dry-run lists candidates with a size and removes nothing, whatever
  the enforcement setting is;
* enforcement is behind an explicit setting that defaults OFF, and OFF
  means no deletion path is reachable at all;
* the age threshold defaults to 72 h and comes from the settings DB.

Every filesystem test builds its tree under ``tmp_path``; the real work
root is never touched.  Synchronous, pytest + stdlib only.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
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
    STATE_ERROR,
    STATE_KEPT_ACTIVE,
    STATE_KEPT_HARNESS_OWNED,
    STATE_KEPT_NO_REFS,
    STATE_KEPT_RECENT,
    STATE_REMOVED,
    STATE_SELECTABLE,
    collect_workspace_refs,
    human_size,
)

HOUR = 3600.0
THRESHOLD = float(DEFAULT_WORKSPACE_MAX_AGE_HOURS) * HOUR  # 72 h
# A fixed wall-clock anchor (2026-11-27T00:00:00Z) used as ``now=`` for the sweep
# calls that accept it.  The launcher wiring path uses the real clock, so the
# anchor must sit in the past relative to it; the predicate itself never
# reads the clock at all (pinned by test_predicate_is_pure_no_fs_or_clock_access).
NOW = 1_792_752_000.0


# ------------------------------------------------------------------ helpers
def _old(hours: float = 100.0) -> float:
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

    def test_zero_threshold_selects_any_positive_age(self) -> None:
        assert should_retain_workspace(self.WS, ["completed"], 1.0, 0.0) is True

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
    assert sorted(refs["/work/a"].states) == ["completed", "failed", "launched"]
    # Most recent terminal finished_at wins.
    assert refs["/work/a"].finished_at == 20.0
    assert refs["/work/a"].age_source == "run_finished_at"
    # An active run never lends its (possibly stale) finished_at.
    assert refs["/work/b"].states == ["running", "pending"]
    assert refs["/work/b"].finished_at is None
    assert refs["/work/b"].age_source == "workspace_mtime"


def test_collector_accepts_mapping_queue_entries_and_skips_blank() -> None:
    refs = collect_workspace_refs([], [{"workspace": "  ", "status": "pending"}, {"workspace": "/w", "status": "blocked"}])
    assert set(refs) == {"/w"}
    assert refs["/w"].states == ["blocked"]


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
    for name in (
        "ws-old-terminal",
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
        assert set(entry["states"]) == {"completed", "done"}

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
        ws = _tree(root, "ws-mtime")
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
        ws = _tree(root, "ws")
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
        ws = _tree(root, "ws")
        run_items = [{"workspace": str(ws), "status": "completed", "finished_at": NOW - 2 * HOUR}]
        # 1 h threshold: 2 h old selects.
        assert sweep_workspaces(root, run_items=run_items, config=DiskRetentionConfig(workspace_max_age_hours=1), now=NOW)[0].state == STATE_SELECTABLE
        # 72 h threshold: 2 h old is kept.
        assert sweep_workspaces(root, run_items=run_items, config=DiskRetentionConfig(), now=NOW)[0].state == STATE_KEPT_RECENT


# =================================================================== settings
class TestSettings:
    def test_defaults_are_off_and_72h(self) -> None:
        assert DEFAULT_WORKSPACE_RETENTION_ENABLED is False
        assert DEFAULT_WORKSPACE_MAX_AGE_HOURS == 72
        assert DEFAULT_CONFIG.workspace_retention_enabled is False
        assert DEFAULT_CONFIG.workspace_max_age_hours == 72

    def test_config_from_values_without_settings_yields_defaults(self) -> None:
        cfg = config_from_values({})
        assert cfg.workspace_retention_enabled is False
        assert cfg.workspace_max_age_hours == 72
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
        assert config_from_values({cfg_mod.SETTING_WORKSPACE_MAX_AGE_HOURS: "soon"}).workspace_max_age_hours == 72

    def test_config_from_runtime_reads_store(self) -> None:
        runtime = SimpleNamespace(
            store=SimpleNamespace(values=lambda: {cfg_mod.SETTING_WORKSPACE_RETENTION_ENABLED: "true", cfg_mod.SETTING_WORKSPACE_MAX_AGE_HOURS: "12"})
        )
        cfg = dr.config_from_runtime(runtime)
        assert cfg.workspace_retention_enabled is True
        assert cfg.workspace_max_age_hours == 12


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
        ws = _tree(root, "ws-old")
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
        ws = _tree(root, "ws-old")
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
