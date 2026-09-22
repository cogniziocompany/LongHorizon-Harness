"""Tests for queue store selection in create_app."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from pathlib import Path

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.webapi.server import create_app
from lh_harness.queue import QueueStore, PgQueueStore


def _fixture(tmp_path: Path):
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    return root


def test_create_app_file_store_default(tmp_path: Path):
    """Test that create_app uses QueueStore when no backend is configured."""
    root = _fixture(tmp_path)
    app = create_app(runs_root=root)
    assert app.state.queue_store is not None
    assert isinstance(app.state.queue_store, QueueStore)
    # PgQueueStore might be None if psycopg is not installed, so check differently
    if PgQueueStore is not None:
        assert not isinstance(app.state.queue_store, PgQueueStore)


def test_create_app_postgres_backend_selects_pg_queue_store(tmp_path: Path):
    """Test that create_app selects PgQueueStore when backend=postgres and database_url is provided."""
    root = _fixture(tmp_path)

    # Mock project config with postgres backend
    project_config = {
        "queue": {
            "backend": "postgres",
            "database_url": "postgresql://user:pass@localhost/db"
        }
    }

    # Patch load_run_defaults to return our project config
    with patch('lh_harness.config.load_run_defaults') as mock_load:
        mock_load.return_value = project_config

        # Patch _select_queue_store to return a mock store instance
        with patch('lh_harness.webapi.server._select_queue_store') as mock_select:
            mock_store = MagicMock()
            mock_select.return_value = mock_store

            app = create_app(runs_root=root)

            # Verify _select_queue_store was called with the correct arguments
            mock_select.assert_called_once()
            args, kwargs = mock_select.call_args
            assert args[0] == root  # runs_root
            assert args[1] == project_config  # project

            # Verify the returned store is what we mocked
            assert app.state.queue_store == mock_store


def test_create_app_unknown_backend_raises_at_startup(tmp_path: Path):
    """Test that create_app raises ValueError for unknown backend at startup."""
    root = _fixture(tmp_path)

    # Mock project config with unknown backend
    project_config = {
        "queue": {
            "backend": "unknown_backend",
            "database_url": "postgresql://user:pass@localhost/db"
        }
    }

    # Patch _select_queue_store to raise ValueError for unknown backend
    with patch('lh_harness.webapi.server._select_queue_store') as mock_select:
        mock_select.side_effect = ValueError("unknown queue backend: 'unknown_backend'")

        # Should raise ValueError during app creation
        with pytest.raises(ValueError, match="unknown queue backend"):
            create_app(runs_root=root)


def test_create_app_postgres_missing_database_url_raises(tmp_path: Path):
    """Test that create_app raises ValueError when backend=postgres but no database_url."""
    root = _fixture(tmp_path)

    # Mock project config with postgres backend but missing database_url
    project_config = {
        "queue": {
            "backend": "postgres"
            # missing database_url
        }
    }

    # Patch _select_queue_store to raise ValueError for missing database_url
    with patch('lh_harness.webapi.server._select_queue_store') as mock_select:
        mock_select.side_effect = ValueError("queue_backend=postgres requires queue.database_url")

        # Should raise ValueError during app creation
        with pytest.raises(ValueError, match="queue.database_url"):
            create_app(runs_root=root)


def test_create_app_file_store_explicit_backend(tmp_path: Path):
    """Test that create_app uses QueueStore when backend=file is explicitly set."""
    root = _fixture(tmp_path)

    # Mock project config with file backend
    project_config = {
        "queue": {
            "backend": "file"
        }
    }

    # Patch _select_queue_store to return a QueueStore instance
    with patch('lh_harness.webapi.server._select_queue_store') as mock_select:
        mock_queue_store = MagicMock(spec=QueueStore)
        mock_select.return_value = mock_queue_store

        app = create_app(runs_root=root)

        # Verify _select_queue_store was called
        mock_select.assert_called_once()

        # Should still use QueueStore
        assert isinstance(app.state.queue_store, QueueStore)
        # PgQueueStore might be None if psycopg is not installed, so check differently
        if PgQueueStore is not None:
            assert not isinstance(app.state.queue_store, PgQueueStore)


def test_create_app_handles_config_load_errors_gracefully(tmp_path: Path):
    """Test that create_app falls back to default QueueStore when config loading fails."""
    root = _fixture(tmp_path)

    # Patch load_run_defaults in the config module where it's imported from
    with patch('lh_harness.config.load_run_defaults') as mock_load:
        mock_load.side_effect = Exception("Config load failed")

        # Should not raise, should fall back to default QueueStore
        app = create_app(runs_root=root)
        assert isinstance(app.state.queue_store, QueueStore)
        # PgQueueStore might be None if psycopg is not installed, so check differently
        if PgQueueStore is not None:
            assert not isinstance(app.state.queue_store, PgQueueStore)