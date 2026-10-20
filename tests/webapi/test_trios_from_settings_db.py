"""Write tests/webapi/test_trios_from_settings_db.py.

LHH-TRIOS-FROM-SETTINGS-DB: the effective valid-trio set is the hardcoded
required/optional names plus every *enabled* ``queue_trio`` row of the
settings store.  A trio that exists only in the store is accepted; a row
that is disabled is not; an unknown name still gets the same 422 message;
and a store that cannot be read falls back to the hardcoded set with a
logged warning.

Every test uses a settings store on a tmp_path database -- never the live
settings DB, never the harness-owned run paths.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from lh_harness import settings_store
from lh_harness.queue import _normalize_request, _validate_trio, valid_trios
from lh_harness.settings_store import SettingsRuntime, SettingsStore

KEY = "k" * 40


def _store(tmp_path: Path) -> SettingsStore:
    """A settings store on a temp database (never the live settings DB)."""
    return SettingsStore(tmp_path / "db" / "settings.db", KEY)


def _runtime(store: SettingsStore) -> SettingsRuntime:
    return SettingsRuntime(store=store, error=None)


def _add_trio(store: SettingsStore, name: str, *, enabled: bool = True) -> None:
    """Insert one queue_trio row straight through the store's own table."""
    with store._db() as db:
        db.execute(
            """INSERT OR REPLACE INTO queue_trio
               (name, agent, model, mcp_profile, enabled, updated_by, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                name,
                "claude_code",
                "some-model:cloud",
                None,
                1 if enabled else 0,
                "test",
                "2026-10-20T00:00:00Z",
            ),
        )


@pytest.fixture(autouse=True)
def _clear_trio_cache():
    """The trio set is cached process-wide; each test starts clean."""
    import lh_harness.queue as queue_module

    queue_module._trio_set_cache = None
    queue_module._trio_set_cache_at = 0.0
    yield
    queue_module._trio_set_cache = None
    queue_module._trio_set_cache_at = 0.0


def test_a_trio_only_in_the_settings_store_is_accepted(tmp_path: Path) -> None:
    """A5: a trio that exists only as an enabled queue_trio row is accepted."""
    store = _store(tmp_path)
    _add_trio(store, "dbonly")
    runtime = _runtime(store)

    assert "dbonly" in valid_trios(settings_runtime=runtime)
    params = _normalize_request(
        {"name": "n", "task": "t", "workspace": "w", "trio": "dbonly", "requested_by": "ci"},
        settings_runtime=runtime,
    )
    assert params["trio"] == "dbonly"


def test_a_disabled_settings_store_trio_is_rejected(tmp_path: Path) -> None:
    """A6: the same name with its row disabled is refused, with the 422 text."""
    store = _store(tmp_path)
    _add_trio(store, "offonly", enabled=False)
    runtime = _runtime(store)

    assert "offonly" not in valid_trios(settings_runtime=runtime)
    with pytest.raises(ValueError) as excinfo:
        _validate_trio("offonly", settings_runtime=runtime)
    assert "trio must be one of: " in str(excinfo.value)


def test_an_unknown_trio_is_rejected_with_the_same_422_message(tmp_path: Path) -> None:
    """A7: an unknown name is refused with the same message template."""
    store = _store(tmp_path)
    _add_trio(store, "dbonly")
    runtime = _runtime(store)

    with pytest.raises(ValueError) as excinfo:
        _validate_trio("nosuchtrio", settings_runtime=runtime)
    message = str(excinfo.value)
    assert message.startswith("trio must be one of: ")
    # The enumerated list is the effective valid set: the hardcoded names
    # plus the enabled store row.
    assert "dbonly" in message
    assert "kimi" in message and "qwen" in message


def test_a_settings_store_read_failure_falls_back_to_the_hardcoded_set(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A2: a store that cannot be read logs a warning and falls back."""
    store = _store(tmp_path)
    _add_trio(store, "dbonly")
    runtime = _runtime(store)

    def _boom():
        raise RuntimeError("the settings database is unreadable")

    store.queue_trios = _boom  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        resolved = valid_trios(settings_runtime=runtime)

    assert "dbonly" not in resolved
    assert "kimi" in resolved and "qwen" in resolved
    assert any(
        "settings store" in record.getMessage() for record in caplog.records
    ), caplog.records


def test_the_built_in_trio_set_still_holds_mm31f() -> None:
    """A4: "mm31f" is one of the hardcoded optional trios."""
    from lh_harness.queue import _OPTIONAL_TRIOS, _REQUIRED_TRIOS, _VALID_TRIOS

    assert "mm31f" in _OPTIONAL_TRIOS
    assert _VALID_TRIOS == _REQUIRED_TRIOS | _OPTIONAL_TRIOS
    assert "mm31f" in valid_trios(settings_runtime=SettingsRuntime(error="not configured"))


def test_the_active_runtime_supplies_the_store_when_none_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process-wide active runtime is the default source of trio names."""
    store = _store(tmp_path)
    _add_trio(store, "dbonly")
    monkeypatch.setattr(settings_store, "_ACTIVE", _runtime(store))

    assert "dbonly" in valid_trios()
