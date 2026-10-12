"""Hot reload on a real service start, and per-role trio models in the settings store.

CT110, 2026-10-12, first deploy of LHH-QUEUE-HOTRELOAD (#126):

* F1: ``queue_trio`` / ``queue_capacity`` stayed EMPTY after the restart.
  ``lh-harness web`` opens the store in ``apply_startup_settings()`` and then
  removes ``LH_HARNESS_SETTINGS_KEY`` from the environment; the launcher looked
  for the key in the environment, found none, and silently stayed file-only.
* F2: ``queue_trio`` had no per-role columns, so seeding (or any edit) would
  have collapsed the ``zai`` trio (flash manager/auditor) and the ``zaiq`` trio
  (local qwen executor) to one model for all three roles.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

import pytest

from lh_harness import settings_store
from lh_harness.launcher import Launcher
from lh_harness.queue import default_queue_config, queue_config_from_config
from lh_harness.config import load_run_defaults
from lh_harness.settings_store import SettingsStore, upsert_queue_trio

from .test_launcher import _SETTINGS_KEY, _base_entry, _fixture, _patch_project_config, _settings_env

CT110_QUEUE_TOML = """
[queue.trios.kimi]
agent = "claude_code"
model = "glm-5.2:synthetic-anthropic"
mcp_profile = "default"

[queue.trios.qwen]
agent = "claude_code"
model = "qwen3.8"
mcp_profile = "default"

[queue.trios.zai]
agent = "claude_code"
model = "glm-5.3:zai"
manager_model = "glm-5.3-flash:zai"
auditor_model = "glm-5.3-flash:zai"
mcp_profile = "default"

[queue.trios.zaiq]
agent = "claude_code"
model = "glm-5.3:zai"
executor_model = "qwen3.8"
mcp_profile = "default"

[queue.capacity]
kimi_max = 1
qwen_max = 2
zai_max = 5
zaiq_max = 1
"""


def _ct110_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    path = tmp_path / "config.toml"
    path.write_text(CT110_QUEUE_TOML, encoding="utf-8")
    _patch_project_config(tmp_path, monkeypatch, path)
    return queue_config_from_config(load_run_defaults(path))


def _launch_roles(launcher: Launcher, store, supervisor, trio: str) -> dict:
    before = len(supervisor.created)
    entry = store.create(_base_entry(trio=trio, workspace=f"./w-{trio}-{before}"))
    asyncio.run(launcher.tick())
    assert store.get(entry.queue_id).status == "launched", store.get(entry.queue_id).skip_reasons
    return supervisor.created[before]["owner"]["role_configs"]


def test_launcher_uses_the_store_the_web_app_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F1: after apply_startup_settings() the key is gone from the environment;
    the launcher must still get the store, and seed it."""
    root, store, supervisor = _fixture(tmp_path)
    db_path = _settings_env(tmp_path, monkeypatch)
    queue_config = _ct110_config(tmp_path, monkeypatch)
    monkeypatch.setattr(settings_store, "_ACTIVE", None)

    runtime = settings_store.apply_startup_settings()
    assert runtime.store is not None
    assert settings_store.KEY_ENV not in os.environ, "the web start removes the key from the environment"

    launcher = Launcher(supervisor, store, queue_config=queue_config)

    assert launcher._settings_store is runtime.store
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM queue_trio").fetchone()[0] == 4
        assert db.execute("SELECT count(*) FROM queue_capacity").fetchone()[0] == 4


def test_seed_keeps_per_role_models_and_launch_uses_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F2: the exact CT110 zai and zaiq blocks survive the seed and reach a launch."""
    root, store, supervisor = _fixture(tmp_path)
    db_path = _settings_env(tmp_path, monkeypatch)
    queue_config = _ct110_config(tmp_path, monkeypatch)

    launcher = Launcher(supervisor, store, queue_config=queue_config)

    rows = {row["name"]: row for row in SettingsStore(db_path, _SETTINGS_KEY).queue_trios()}
    assert rows["zai"]["manager_model"] == "glm-5.3-flash:zai"
    assert rows["zai"]["auditor_model"] == "glm-5.3-flash:zai"
    assert "executor_model" not in rows["zai"]
    assert rows["zaiq"]["executor_model"] == "qwen3.8"
    assert rows["kimi"]["role_models_set"] is True and "manager_model" not in rows["kimi"]

    zai = _launch_roles(launcher, store, supervisor, "zai")
    assert zai["manager"]["model"] == "glm-5.3-flash:zai"
    assert zai["executor"]["model"] == "glm-5.3:zai"
    assert zai["auditor"]["model"] == "glm-5.3-flash:zai"
    zaiq = _launch_roles(launcher, store, supervisor, "zaiq")
    assert zaiq["manager"]["model"] == "glm-5.3:zai"
    assert zaiq["executor"]["model"] == "qwen3.8"
    assert zaiq["auditor"]["model"] == "glm-5.3:zai"


def test_model_edit_keeps_role_models_and_role_edit_hot_reloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, store, supervisor = _fixture(tmp_path)
    db_path = _settings_env(tmp_path, monkeypatch)
    queue_config = _ct110_config(tmp_path, monkeypatch)
    launcher = Launcher(supervisor, store, queue_config=queue_config)
    settings = SettingsStore(db_path, _SETTINGS_KEY)
    base = {"name": "zaiq", "agent": "claude_code", "model": "glm-5.3:zai", "mcp_profile": "default", "enabled": 1}

    # A trio-wide model edit (set_trio_model / a PUT row without role keys)
    # must not reset the executor override.
    with settings._db() as db:
        upsert_queue_trio(db, {**base, "model": "glm-5.3-flash:zai"}, "admin@test", roles=None)
    row = next(r for r in settings.queue_trios() if r["name"] == "zaiq")
    assert row["model"] == "glm-5.3-flash:zai" and row["executor_model"] == "qwen3.8"
    roles = _launch_roles(launcher, store, supervisor, "zaiq")
    assert roles["manager"]["model"] == "glm-5.3-flash:zai" and roles["executor"]["model"] == "qwen3.8"

    # A role edit takes effect on the next pass, no restart.
    with settings._db() as db:
        upsert_queue_trio(db, base, "admin@test", roles={"executor_model": "glm-5.3-flash:zai"})
    monkeypatch.setattr(launcher, "_queue_cache_until", 0.0, raising=False)
    queue_config_cap = settings.queue_capacity()
    with settings._db() as db:
        db.execute("UPDATE queue_capacity SET max_concurrent = 5 WHERE trio = 'zaiq'")
    assert queue_config_cap["zaiq"] == 1
    roles = _launch_roles(launcher, store, supervisor, "zaiq")
    assert roles["executor"]["model"] == "glm-5.3-flash:zai"

    # An empty mapping clears the overrides: every role back on the trio model.
    with settings._db() as db:
        upsert_queue_trio(db, base, "admin@test", roles={})
    roles = _launch_roles(launcher, store, supervisor, "zaiq")
    assert {spec["model"] for spec in roles.values()} == {"glm-5.3:zai"}


def test_row_written_before_the_column_keeps_the_file_role_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A row with NULL role_models (seeded by a build without the column) must
    not collapse a mixed trio: the file's per-role keys still apply."""
    root, store, supervisor = _fixture(tmp_path)
    db_path = _settings_env(tmp_path, monkeypatch)
    queue_config = _ct110_config(tmp_path, monkeypatch)
    launcher = Launcher(supervisor, store, queue_config=queue_config)
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE queue_trio SET role_models = NULL WHERE name = 'zaiq'")
    row = next(r for r in SettingsStore(db_path, _SETTINGS_KEY).queue_trios() if r["name"] == "zaiq")
    assert row["role_models_set"] is False

    roles = _launch_roles(launcher, store, supervisor, "zaiq")

    assert roles["executor"]["model"] == "qwen3.8"
    assert roles["manager"]["model"] == "glm-5.3:zai"


def test_existing_store_gains_the_role_models_column(tmp_path: Path) -> None:
    db_path = tmp_path / "old.db"
    SettingsStore(db_path, _SETTINGS_KEY)
    with sqlite3.connect(db_path) as db:
        db.execute("ALTER TABLE queue_trio DROP COLUMN role_models")
        db.execute("PRAGMA user_version = 2")
        db.execute(
            "INSERT INTO queue_trio(name, agent, model, mcp_profile, enabled, updated_by, updated_at) "
            "VALUES ('kimi', 'claude_code', 'm', NULL, 1, 'seed', '2026-10-11T00:00:00Z')"
        )
    reopened = SettingsStore(db_path, _SETTINGS_KEY)
    row = reopened.queue_trios()[0]
    assert row["name"] == "kimi" and row["model"] == "m" and row["role_models_set"] is False
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == settings_store.SCHEMA_VERSION
