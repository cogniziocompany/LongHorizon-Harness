"""Startup tests for the Postgres cutover path in ``create_app``.

These drive the real config loader (fixture ``config.toml`` in ``tmp_path``)
and the real ``_select_queue_store`` inside ``create_app``; only the Postgres
connection underneath the store is mocked, so no test ever contacts a live
database. The DSN is synthetic loopback -- passwords come from the
``LH_HARNESS_DB_PASSWORD`` environment NAME at runtime, never from config or
from any value written here.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi")

from lh_harness.config import load_run_defaults as real_load_run_defaults
from lh_harness.pg_queue import PgQueueStore
from lh_harness.queue import QueueStore
from lh_harness.webapi.server import create_app

# Synthetic loopback DSN; never a real host and never carrying a password.
FAKE_DSN = "postgresql://queue_user@127.0.0.1:5432/harness"


def _pg_conn_patches():
    """Patch the Postgres connection + migration so PgQueueStore constructs
    without touching any database."""
    return (
        patch("lh_harness.pg_queue._pg_connect", return_value=object()),
        patch.object(PgQueueStore, "_migrate", lambda self: None),
    )


def _write_config(tmp_path: Path, lines: list[str]) -> Path:
    config = tmp_path / "config.toml"
    config.write_text("\n".join(lines), encoding="utf-8")
    return config


def test_create_app_real_config_postgres_builds_app_and_selects_pg_store(
    tmp_path: Path,
) -> None:
    """The documented cutover config ([queue] backend="postgres" +
    database_url) loaded by the REAL config loader flows through
    _flatten_queue_table into _select_queue_store, so create_app builds and
    selects a PgQueueStore instead of crashing or falling back to the file
    store."""
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    config = _write_config(
        tmp_path,
        ["[queue]", 'backend = "postgres"', f'database_url = "{FAKE_DSN}"'],
    )
    conn_patch, migrate_patch = _pg_conn_patches()
    with patch("lh_harness.config.PROJECT_CONFIG_PATH", config):
        with conn_patch, migrate_patch:
            app = create_app(runs_root=root)
    store = app.state.queue_store
    assert isinstance(store, PgQueueStore)
    assert not isinstance(store, QueueStore)
    assert store.database_url == FAKE_DSN


def test_create_app_real_config_postgres_without_database_url_raises(
    tmp_path: Path,
) -> None:
    """backend=postgres with no database_url must still fail loudly at startup
    (ValueError re-raised out of create_app) -- that half of the cutover
    behaviour is correct and must not regress into a silent file fallback."""
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    config = _write_config(tmp_path, ["[queue]", 'backend = "postgres"'])
    with patch("lh_harness.config.PROJECT_CONFIG_PATH", config):
        with pytest.raises(ValueError, match="database_url"):
            create_app(runs_root=root)


def test_create_app_real_config_unknown_queue_key_still_raises(tmp_path: Path) -> None:
    """Unknown [queue] keys still raise ProjectConfigError from the real
    loader, so the cutover fix does not loosen validation."""
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    config = _write_config(
        tmp_path, ["[queue]", 'backend = "postgres"', "bogus_key = true"]
    )
    with patch("lh_harness.config.PROJECT_CONFIG_PATH", config):
        with pytest.raises(ValueError, match="unknown \\[queue\\] key"):
            create_app(runs_root=root)


def test_create_app_real_config_unknown_backend_still_raises(tmp_path: Path) -> None:
    """A bad backend name still raises at startup through the real loader."""
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    config = _write_config(tmp_path, ["[queue]", 'backend = "bogus"'])
    with patch("lh_harness.config.PROJECT_CONFIG_PATH", config):
        with pytest.raises(ValueError, match="unknown \\[queue\\.backend\\]"):
            create_app(runs_root=root)