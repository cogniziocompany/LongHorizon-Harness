"""Tests for the tev1-powered default trio router (LHH-TEV1-DEFAULT-TRIO-ROUTER).

The router asks Ollama at /v1/systemone for a typed decision when a queue
entry arrives without a trio.  We stub the HTTP call here so the tests are
hermetic and never reach the 2060 lane.  Every test exercises the public
``route_trio`` entry point and asserts the decision struct Overseer1 reads
back off the queue entry.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from lh_harness import trio_router
from lh_harness.queue import QueueEntry


# A scheduler-ready trios dict that mirrors what the launcher's hot-reload
# builds out of the settings store + config.toml.  ``zaiq`` carries a per-role
# override so the test that probes a local 9B pick has a schedulable spec.
SCHEDULABLE_TRIOS: dict[str, dict[str, Any]] = {
    "kimi": {"agent": "claude_code", "model": "sonnet"},
    "qwen": {"agent": "codex", "model": "qwen3.8", "executor_model": "qwen3.8"},
    "glm": {"agent": "claude_code", "model": "glm-5.3"},
    "zai": {"agent": "claude_code", "model": "glm-4.6"},
    "zaiq": {"agent": "claude_code", "executor_model": "qwen3.8"},
    "minimax": {"agent": "codex", "model": "MiniMax-M3"},
}


def _entry(*, name: str = "n", task: str = "t", trio: str = "", priority: int = 1) -> QueueEntry:
    return QueueEntry(
        queue_id="q-1",
        name=name,
        task=task,
        workspace="/tmp/work",
        max_rounds=3,
        trio=trio,
        priority=priority,
        requested_by="ci",
    )


def _stub_call_factory(responses: list[dict[str, Any]]):
    """Build a fake ``http_call`` that yields canned answers in order.

    Every call appends the captured payload/args to a side list so tests can
    assert on what the router actually sent (URL, model, body shape).
    """

    calls: list[dict[str, Any]] = []

    def _fake(url: str, model: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        calls.append({"url": url, "model": model, "payload": payload, "timeout": timeout})
        if not responses:
            raise AssertionError("trio router called /v1/systemone more times than expected")
        return responses.pop(0)

    return _fake, calls


def _settings_store_proxy(values: dict[str, Any]):
    """A minimal stand-in for the SettingsStore metadata() API."""

    class _Proxy:
        def metadata(self, name: str) -> dict[str, Any]:
            if name in values:
                return {"name": name, "secret": False, "set": True, "value": values[name]}
            raise AssertionError(f"unexpected settings read: {name}")

    return _Proxy()


def _stub_settings(**overrides: Any) -> Any:
    base = {
        "TRIO_ROUTER_ENABLED": "true",
        "TRIO_ROUTER_URL": "http://192.168.0.42:11436",
        "TRIO_ROUTER_MODEL": "tev1:4b",
        "TRIO_ROUTER_MIN_CONFIDENCE": "0.5",
    }
    base.update({k: str(v) for k, v in overrides.items()})
    return _settings_store_proxy(base)


# ----------------------------------------------------------------- top trio --
def test_picks_top_trio_when_confidence_meets_threshold() -> None:
    """The router picks the model's top trio when confidence >= floor."""

    entry = _entry(task="add a footer link to the docs")
    response = {
        "answers": {
            "trio": {
                "choice": "kimi",
                "probabilities": {"kimi": 0.76, "qwen": 0.14, "gemma": 0.08, "zai": 0.02},
                "confidence": 0.68,
            },
            "small": {"noul": 0.7},
        }
    }
    http_call, calls = _stub_call_factory([response])
    store = _stub_settings()

    chosen = trio_router.route_trio(
        entry, store, config_trios=SCHEDULABLE_TRIOS, http_call=http_call
    )

    assert chosen == "kimi"
    assert entry.trio_router is not None
    record = entry.trio_router
    assert record["chosen"] == "kimi"
    assert record["confidence"] == 0.68
    assert record["small_noul"] == 0.7
    assert record["probabilities"]["kimi"] == 0.76
    assert record["reason"] == "router_picked"
    # The router actually used the configured URL and model.
    assert calls[0]["url"] == "http://192.168.0.42:11436"
    assert calls[0]["model"] == "tev1:4b"
    assert calls[0]["payload"]["state"]["task"] == "add a footer link to the docs"


# -------------------------------------------------------------- low confidence --
def test_low_confidence_falls_back_and_records_reason() -> None:
    """Confidence below the configured floor -> None + audit."""

    entry = _entry()
    response = {
        "answers": {
            "trio": {
                "choice": "kimi",
                "probabilities": {"kimi": 0.4, "qwen": 0.3, "glm": 0.3},
                "confidence": 0.3,
            },
            "small": {"noul": 0.7},
        }
    }
    http_call, _calls = _stub_call_factory([response])
    store = _stub_settings(TRIO_ROUTER_MIN_CONFIDENCE="0.5")

    chosen = trio_router.route_trio(
        entry, store, config_trios=SCHEDULABLE_TRIOS, http_call=http_call
    )

    assert chosen is None
    record = entry.trio_router
    assert record is not None
    assert record["chosen"] is None
    assert record["reason"] == "router_low_confidence"
    assert record["confidence"] == 0.3
    assert record["choice"] == "kimi"


# ----------------------------------------------------------- small=0.3 / qwen --
def test_local_trio_refused_when_small_below_floor() -> None:
    """small=0.3 must NEVER pick a local 9B trio, even when top probability."""

    entry = _entry(task="refactor the entire settings module")
    response = {
        "answers": {
            "trio": {
                "choice": "qwen",
                "probabilities": {"qwen": 0.65, "kimi": 0.20, "zaiq": 0.10, "glm": 0.05},
                "confidence": 0.7,
            },
            "small": {"noul": 0.3},
        }
    }
    http_call, _calls = _stub_call_factory([response])
    store = _stub_settings()

    chosen = trio_router.route_trio(
        entry, store, config_trios=SCHEDULABLE_TRIOS, http_call=http_call
    )

    assert chosen is None
    record = entry.trio_router
    assert record is not None
    assert record["chosen"] is None
    assert record["reason"] == "router_local_too_big"
    assert record["choice"] == "qwen"
    assert record["small_noul"] == 0.3


# --------------------------------------------------------------- timeout/error --
def test_router_timeout_falls_back_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    """A /v1/systemone timeout falls back, records the error, and logs."""

    entry = _entry()

    def _timeout(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("connect timeout after 30s")

    store = _stub_settings()
    with caplog.at_level("WARNING", logger=trio_router.logger.name):
        chosen = trio_router.route_trio(
            entry, store, config_trios=SCHEDULABLE_TRIOS, http_call=_timeout
        )

    assert chosen is None
    record = entry.trio_router
    assert record is not None
    assert record["chosen"] is None
    assert record["reason"] == "router_error"
    assert "connect timeout after 30s" in record["error"]
    # The router logged a WARNING so an operator sees the failure without
    # digging through queue JSONs.
    assert any("trio router" in record.message for record in caplog.records)


# --------------------------------------------------------- explicit trio passed --
def test_existing_trio_left_untouched_and_router_not_called() -> None:
    """Entries that SET a trio are never changed -- the router is a no-op."""

    entry = _entry(trio="kimi")

    def _must_not_run(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("router must not be called when entry.trio is set")

    chosen = trio_router.route_trio(
        entry,
        _stub_settings(),
        config_trios=SCHEDULABLE_TRIOS,
        http_call=_must_not_run,
    )

    assert chosen is None
    assert entry.trio == "kimi"  # untouched
    # Hard rule: no router call, no audit row -- the runner would be a
    # separate entry on the queue, not a row the operator has to inspect.
    assert entry.trio_router is None


# ------------------------------------------------------------------- disabled --
def test_router_disabled_when_setting_is_false() -> None:
    """TRIO_ROUTER_ENABLED=False -> immediate fallback, no HTTP call."""

    entry = _entry()

    def _must_not_run(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("router must not call /v1/systemone when disabled")

    chosen = trio_router.route_trio(
        entry,
        _settings_store_proxy({"TRIO_ROUTER_ENABLED": "false"}),
        config_trios=SCHEDULABLE_TRIOS,
        http_call=_must_not_run,
    )

    assert chosen is None
    record = entry.trio_router
    assert record is not None
    assert record["enabled"] is False
    assert record["reason"] == "router_disabled"
    assert record["chosen"] is None


# ----------------------------------------------------- decision recorded on entry --
def test_decision_struct_round_trips_via_to_dict_from_dict() -> None:
    """The struct the router writes must persist through to_dict/from_dict."""

    entry = _entry()
    response = {
        "answers": {
            "trio": {
                "choice": "glm",
                "probabilities": {"glm": 0.6, "kimi": 0.4},
                "confidence": 0.55,
            },
            "small": {"noul": 0.9},
        }
    }
    http_call, _calls = _stub_call_factory([response])
    store = _stub_settings()

    chosen = trio_router.route_trio(
        entry, store, config_trios=SCHEDULABLE_TRIOS, http_call=http_call
    )

    assert chosen == "glm"
    serialised = entry.to_dict()
    assert "trio_router" in serialised and isinstance(serialised["trio_router"], dict)
    reloaded = QueueEntry.from_dict(serialised)
    assert reloaded.trio_router == entry.trio_router
    assert reloaded.trio_router["choice"] == "glm"


# -------------------------------------------------- schedulability gate --
def test_picked_trio_must_exist_in_merged_trio_config() -> None:
    """A pick the launcher cannot schedule is recorded and rejected."""

    entry = _entry()
    response = {
        "answers": {
            "trio": {
                "choice": "gemma",
                "probabilities": {"gemma": 0.9, "kimi": 0.1},
                "confidence": 0.8,
            },
            "small": {"noul": 0.9},
        }
    }
    http_call, _calls = _stub_call_factory([response])

    # Strip ``gemma`` from the scheduler; the router must refuse.
    trios = {name: spec for name, spec in SCHEDULABLE_TRIOS.items() if name != "gemma"}
    store = _stub_settings()

    chosen = trio_router.route_trio(entry, store, config_trios=trios, http_call=http_call)

    assert chosen is None
    record = entry.trio_router
    assert record is not None
    assert record["reason"] == "router_pick_not_schedulable"
    assert record["choice"] == "gemma"


# -------------------------------------------------- default criteria include
def test_default_criteria_cover_known_trio_names() -> None:
    """The router builds criteria from settings + defaults so /v1/systemone has choices."""

    captured: dict[str, Any] = {}

    def _capture_call(url: str, model: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        captured["url"] = url
        captured["payload"] = payload
        return {
            "answers": {
                "trio": {"choice": "kimi", "confidence": 0.7, "probabilities": {"kimi": 1.0}},
                "small": {"noul": 0.7},
            }
        }

    entry = _entry()
    store = _stub_settings()

    chosen = trio_router.route_trio(
        entry,
        store,
        config_trios=SCHEDULABLE_TRIOS,
        http_call=_capture_call,
    )

    assert chosen == "kimi"
    criteria = captured["payload"]["questions"]["trio"]["criteria"]
    for required in ("kimi", "qwen", "glm", "zai", "zaiq", "minimax", "gemma"):
        assert required in criteria, criteria


# ---------------------------------------------- env fallback when no store --
def test_environment_overrides_when_settings_store_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No settings store -> the env vars drive the router (admin bootstrap)."""

    monkeypatch.setenv("TRIO_ROUTER_ENABLED", "true")
    monkeypatch.setenv("TRIO_ROUTER_URL", "http://192.168.21.138:11436")
    monkeypatch.setenv("TRIO_ROUTER_MODEL", "tev1:4b")
    monkeypatch.setenv("TRIO_ROUTER_MIN_CONFIDENCE", "0.5")
    # Make sure the env really is what the router sees.
    for stale in ("TRIO_ROUTER_ENABLED", "TRIO_ROUTER_URL", "TRIO_ROUTER_MODEL", "TRIO_ROUTER_MIN_CONFIDENCE"):
        monkeypatch.delitem(os.environ, stale, raising=False)
    monkeypatch.setenv("TRIO_ROUTER_ENABLED", "true")
    monkeypatch.setenv("TRIO_ROUTER_URL", "http://192.168.21.138:11436")
    monkeypatch.setenv("TRIO_ROUTER_MODEL", "tev1:4b")
    monkeypatch.setenv("TRIO_ROUTER_MIN_CONFIDENCE", "0.5")

    captured: dict[str, Any] = {}

    def _capture_call(url: str, model: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        captured["url"] = url
        captured["model"] = model
        return {
            "answers": {
                "trio": {"choice": "minimax", "confidence": 0.7, "probabilities": {"minimax": 1.0}},
                "small": {"noul": 0.9},
            }
        }

    entry = _entry()
    chosen = trio_router.route_trio(
        entry, None, config_trios=SCHEDULABLE_TRIOS, http_call=_capture_call
    )

    assert chosen == "minimax"
    assert captured["url"] == "http://192.168.21.138:11436"
    assert captured["model"] == "tev1:4b"


# --------------------------------------------- task is truncated to 4000 chars --
def test_task_text_is_truncated_to_4000_chars() -> None:
    entry = _entry(task="x" * 8_000)
    captured: dict[str, Any] = {}

    def _capture_call(url: str, model: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        captured["payload"] = payload
        return {
            "answers": {
                "trio": {"choice": "kimi", "confidence": 0.7, "probabilities": {"kimi": 1.0}},
                "small": {"noul": 0.9},
            }
        }

    trio_router.route_trio(
        entry, _stub_settings(), config_trios=SCHEDULABLE_TRIOS, http_call=_capture_call
    )
    assert len(captured["payload"]["state"]["task"]) == trio_router.TRIO_ROUTER_MAX_TASK_CHARS


# --------------------------------------------- malformed response -> fallback --
def test_malformed_response_falls_back() -> None:
    entry = _entry()
    response = {"not_answers": True}  # missing the answers block entirely

    chosen = trio_router.route_trio(
        entry,
        _stub_settings(),
        config_trios=SCHEDULABLE_TRIOS,
        http_call=_stub_call_factory([response])[0],
    )

    assert chosen is None
    record = entry.trio_router
    assert record is not None
    assert record["reason"] == "router_response_invalid"


# -------------------------------------------------- 30 s timeout constant --
def test_default_timeout_is_30_seconds() -> None:
    assert trio_router.TRIO_ROUTER_TIMEOUT_SECONDS == 30.0