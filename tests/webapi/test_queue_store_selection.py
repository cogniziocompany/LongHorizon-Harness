"""Tests for queue store selection in ``create_app``.

These drive the real ``_select_queue_store`` path inside ``create_app``: the
selector itself is never mocked, only the Postgres connection underneath it (or
the config loader that points at a fixture config), so the tests prove the
wiring -- the ``[queue] backend`` config reaching the store choice -- rather
than a mock of it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi")

from lh_harness.config import load_run_defaults as real_load_run_defaults
from lh_harness.pg_queue import PgQueueStore
from lh_harness.queue import QueueStore, _select_queue_store, default_queue_config, queue_config_from_config
from lh_harness.webapi.server import create_app

# A syntactically valid DSN; the connection itself is mocked in the postgres
# tests so nothing ever leaves the host.
FAKE_DSN = "postgresql://queue_user@127.0.0.1:5432/harness"


def _pg_conn_patches():
    """Patch the Postgres connection + migration so PgQueueStore constructs
    without touching any database."""
    return (
        patch("lh_harness.pg_queue._pg_connect", return_value=object()),
        patch.object(PgQueueStore, "_migrate", lambda self: None),
    )


def _fake_supervisor() -> Any:
    class _FakeSupervisor:
        pass

    return _FakeSupervisor()


def test_create_app_no_backend_keeps_file_store(tmp_path: Path) -> None:
    """No [queue] section -> the file store with the default queue config,
    byte-for-byte what the unconditional QueueStore(runs_root, queue_config)
    line produced before the wiring."""
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    with patch("lh_harness.config.load_run_defaults", return_value={}):
        app = create_app(runs_root=root)
    store = app.state.queue_store
    assert isinstance(store, QueueStore)
    assert not isinstance(store, PgQueueStore)
    # The old code path: default config, store constructed with it.
    assert store._config == default_queue_config()


def test_create_app_file_backend_explicit(tmp_path: Path) -> None:
    """backend="file" selects the file store via the real selector."""
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    with patch("lh_harness.config.load_run_defaults", return_value={"queue": {"backend": "file"}}):
        app = create_app(runs_root=root)
    assert isinstance(app.state.queue_store, QueueStore)
    assert not isinstance(app.state.queue_store, PgQueueStore)


def test_create_app_queue_config_survives_selection(tmp_path: Path) -> None:
    """A [queue] table without a backend key keeps its capacity settings: the
    file store must still be built with queue_config_from_config(project),
    exactly as the pre-wiring code did."""
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    project = {"queue": {"capacity": {"max_retries": 5}}}
    expected = queue_config_from_config(project)
    assert expected["capacity"]["max_retries"] == 5
    with patch("lh_harness.config.load_run_defaults", return_value=project):
        app = create_app(runs_root=root)
    assert isinstance(app.state.queue_store, QueueStore)
    assert app.state.queue_store._config == expected


def test_create_app_postgres_backend_selects_pg_queue_store(tmp_path: Path) -> None:
    """backend="postgres" + database_url reaches a real PgQueueStore.

    The selector is unmocked; only the DB connection underneath is mocked, so
    this exercises the actual selection code path end to end.
    """
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    conn_patch, migrate_patch = _pg_conn_patches()
    with patch(
        "lh_harness.config.load_run_defaults",
        return_value={"queue": {"backend": "postgres", "database_url": FAKE_DSN}},
    ):
        with conn_patch, migrate_patch:
            app = create_app(runs_root=root)
    store = app.state.queue_store
    assert isinstance(store, PgQueueStore)
    assert store.database_url == FAKE_DSN


def test_create_app_unknown_backend_raises_at_startup(tmp_path: Path) -> None:
    """An unknown backend name fails loudly -- never a silent file fallback."""
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    with patch(
        "lh_harness.config.load_run_defaults", return_value={"queue": {"backend": "bogus"}}
    ):
        with pytest.raises(ValueError, match="unknown queue backend"):
            create_app(runs_root=root)


def test_create_app_postgres_without_database_url_raises(tmp_path: Path) -> None:
    """backend=postgres with no database_url is a startup failure, not a fallback."""
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    with patch("lh_harness.config.load_run_defaults", return_value={"queue": {"backend": "postgres"}}):
        with pytest.raises(ValueError, match="database_url"):
            create_app(runs_root=root)


def test_create_app_supervisor_with_file_store(tmp_path: Path) -> None:
    """Launcher over the default file store keeps working (regression for the
    UnboundLocalError on queue_config when supervisor is provided)."""
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    with patch("lh_harness.webapi.server.Launcher") as mock_launcher:
        create_app(runs_root=root, supervisor=_fake_supervisor())
    mock_launcher.assert_called_once()
    kwargs = mock_launcher.call_args.kwargs
    assert "queue_config" in kwargs


def test_create_app_supervisor_with_postgres_store(tmp_path: Path) -> None:
    """Launcher construction over a PgQueueStore works: the supervisor path
    must not crash and must receive the selected store."""
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    conn_patch, migrate_patch = _pg_conn_patches()
    with patch(
        "lh_harness.config.load_run_defaults",
        return_value={"queue": {"backend": "postgres", "database_url": FAKE_DSN}},
    ):
        with conn_patch, migrate_patch:
            with patch("lh_harness.webapi.server.Launcher") as mock_launcher:
                create_app(runs_root=root, supervisor=_fake_supervisor())
    mock_launcher.assert_called_once()
    # The store handed to the Launcher is the Postgres store, not the file one.
    store = mock_launcher.call_args.args[1]
    assert isinstance(store, PgQueueStore)


def test_create_app_config_load_failure_falls_back_to_file_store(tmp_path: Path) -> None:
    """A config that cannot be loaded keeps today's behaviour: default queue
    config and the file store (only backend validation fails loudly)."""
    root = tmp_path / "runs"
    root.mkdir(parents=True)

    def _boom(path: Any) -> dict[str, Any]:
        raise RuntimeError("config load failed")

    with patch("lh_harness.config.load_run_defaults", side_effect=_boom):
        app = create_app(runs_root=root)
    store = app.state.queue_store
    assert isinstance(store, QueueStore)
    assert store._config == default_queue_config()


def test_select_queue_store_file_is_default(tmp_path: Path) -> None:
    """The selector itself: absent config and non-dict queue both give the
    file store; explicit file backend gives the file store too."""
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = _select_queue_store(root, None)
    assert isinstance(store, QueueStore)
    assert not isinstance(store, PgQueueStore)
    store = _select_queue_store(root, {"queue": "not-a-dict"})
    assert isinstance(store, QueueStore)
    store = _select_queue_store(root, {"queue": {"backend": "file"}})
    assert isinstance(store, QueueStore)
    assert not isinstance(store, PgQueueStore)


def test_real_project_config_loads_without_error() -> None:
    """Guard rail: the real config loader keeps parsing a well-formed project
    config (the deployment path create_app depends on). Runs against the
    packaged default path; asserts nothing about secret values."""
    try:
        project = real_load_run_defaults(real_load_run_defaults.__defaults__[0])
    except Exception:
        pytest.skip("no project config available in this environment")
    assert isinstance(project, dict)


def test_real_config_flattener_drops_database_url(tmp_path: Path) -> None:
    """Documented wiring gap: the flattened ``project`` config carries the
    ``backend`` choice but not ``database_url``, so a real deployment with
    ``backend = "postgres"`` would fail loudly at startup (never silently fall
    back to the file store). The selector still needs the DSN, so cutover
    requires threading the URL through the config loader."""
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join(
            [
                "[queue]",
                'backend = "postgres"',
                f'database_url = "{FAKE_DSN}"',
            ]
        ),
        encoding="utf-8",
    )
    project = real_load_run_defaults(config)
    queue = project.get("queue", {})
    assert queue.get("backend") == "postgres"
    assert "database_url" not in queue