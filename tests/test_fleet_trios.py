"""Tests for the fleet trios payload builder and ``FleetReporter.send_trios``.

Slice B is *just* the payload builder, the reporter method, and tests — the
launcher hook that calls ``send_trios`` is wired in slice A.  These tests
therefore stub ``_post`` directly: no daemon thread, no fleet-admin HTTP
server, no enable/disable gymnastics beyond the constructor's missing-env
path.
"""

from __future__ import annotations

import copy
import json
from typing import Any
from unittest.mock import patch

import pytest

from lh_harness.fleet.reporter import FleetReporter
from lh_harness.fleet.trios import build_trios_payload

SCHEMA = "lh-trios/v1"


def _config() -> dict[str, Any]:
    """A two-trio queue config that exercises every payload knob."""
    return {
        "trios": {
            "lite": {
                "agent": "codex",
                "model": "sonnet",
                "mcp_profile": "lite-profile",
            },
            "mixed": {
                "agent": "codex",
                "model": "sonnet",
                "manager_model": "opus",
                "executor_model": "haiku",
            },
        },
        "capacity": {
            "lite_max": 1,
            "mixed_max": 0,
        },
    }


def test_payload_shape_basic() -> None:
    """Schema envelope has every documented key, including the per-trio rows."""

    payload = build_trios_payload(_config(), node="ct110", live_counts={})
    assert payload["schema"] == SCHEMA
    assert isinstance(payload["sentAt"], str) and payload["sentAt"].endswith("Z")
    assert payload["node"] == "ct110"
    assert isinstance(payload["configSha256"], str) and len(payload["configSha256"]) == 64
    assert {"name", "enabled", "capacity", "live", "manager", "executor", "auditor"} == (
        set(payload["trios"][0].keys())
    )
    # sorted by name -> lite before mixed
    names = [row["name"] for row in payload["trios"]]
    assert names == ["lite", "mixed"]
    # Drain = "every active trio has capacity 0"; lite has capacity 1, so false.
    assert payload["drain"] is False


def test_live_counts_and_drain() -> None:
    """Live counts land on the right rows and a fully-zero capacity drains."""

    payload = build_trios_payload(_config(), node="ct110", live_counts={"lite": 3, "mixed": 2})
    by_name = {row["name"]: row for row in payload["trios"]}
    assert by_name["lite"]["live"] == 3
    assert by_name["mixed"]["live"] == 2
    # mixed_max == 0 but lite_max == 1 -> at least one trio can run, so NOT drained.
    assert payload["drain"] is False

    # Force the lite capacity to 0 as well so both trios are clamped shut.
    drained_config = copy.deepcopy(_config())
    drained_config["capacity"]["lite_max"] = 0
    drained_payload = build_trios_payload(drained_config, node="ct110", live_counts={})
    by_name = {row["name"]: row for row in drained_payload["trios"]}
    assert by_name["lite"]["capacity"] == 0
    assert by_name["mixed"]["capacity"] == 0
    assert drained_payload["drain"] is True


def test_role_override_wins_over_trio_model() -> None:
    """Per-role ``<role>_model`` overrides beat the trio-wide ``model``."""

    payload = build_trios_payload(_config(), node="ct110", live_counts={})
    by_name = {row["name"]: row for row in payload["trios"]}

    # ``lite`` has no overrides -> every role gets the trio-wide model.
    lite = by_name["lite"]
    assert lite["manager"] == "sonnet"
    assert lite["executor"] == "sonnet"
    assert lite["auditor"] == "sonnet"

    # ``mixed`` overrides manager and executor, leaves auditor alone.
    mixed = by_name["mixed"]
    assert mixed["manager"] == "opus"
    assert mixed["executor"] == "haiku"
    assert mixed["auditor"] == "sonnet"


def test_sha_changes_when_a_model_changes() -> None:
    """``configSha256`` must flip the moment the underlying config moves."""

    baseline = build_trios_payload(_config(), node="ct110", live_counts={})
    mutated = copy.deepcopy(_config())
    mutated["trios"]["mixed"]["manager_model"] = "different-model"
    after_model_change = build_trios_payload(mutated, node="ct110", live_counts={})
    assert baseline["configSha256"] != after_model_change["configSha256"]

    # And flips for an executor-model change as well.
    mutated2 = copy.deepcopy(_config())
    mutated2["trios"]["mixed"]["executor_model"] = "another"
    after_executor_change = build_trios_payload(mutated2, node="ct110", live_counts={})
    assert baseline["configSha256"] != after_executor_change["configSha256"]
    assert after_model_change["configSha256"] != after_executor_change["configSha256"]

    # Pure capacity moves also change the digest (capacity is part of the
    # sha input so a fleet-admin diff sees capacity edits too).
    mutated3 = copy.deepcopy(_config())
    mutated3["capacity"]["lite_max"] = 99
    after_capacity_change = build_trios_payload(mutated3, node="ct110", live_counts={})
    assert baseline["configSha256"] != after_capacity_change["configSha256"]

    # Live counts and sentAt must NOT change the digest — liveness is the
    # caller's responsibility, not a configuration change.
    live_changed = build_trios_payload(_config(), node="ct110", live_counts={"lite": 99})
    assert baseline["configSha256"] == live_changed["configSha256"]


def test_sha_matches_documented_formula() -> None:
    """The digest is sha256(json.dumps(trios-and-capacity, sort_keys=True))."""

    config = _config()
    payload = build_trios_payload(config, node="ct110", live_counts={})
    trios_part = {
        row["name"]: {
            "enabled": row["enabled"],
            "manager": row["manager"],
            "executor": row["executor"],
            "auditor": row["auditor"],
        }
        for row in payload["trios"]
    }
    capacity_part = {
        "lite_max": 1,
        "mixed_max": 0,
    }
    expected = json.dumps(
        {"trios": trios_part, "capacity": capacity_part}, sort_keys=True
    ).encode("utf-8")
    import hashlib

    assert payload["configSha256"] == hashlib.sha256(expected).hexdigest()


def test_enabled_false_keeps_row_with_enabled_false() -> None:
    """A trio with ``enabled=False`` stays in the envelope but flags ``enabled``."""

    disabled = copy.deepcopy(_config())
    disabled["trios"]["lite"]["enabled"] = False
    payload = build_trios_payload(disabled, node="ct110", live_counts={})
    by_name = {row["name"]: row for row in payload["trios"]}
    assert "lite" in by_name
    assert by_name["lite"]["enabled"] is False
    assert by_name["mixed"]["enabled"] is True
    # The disabled trio's capacity does not drain the launcher — lite still
    # has capacity 1, even though it is disabled.
    assert payload["drain"] is False

    fully_disabled = copy.deepcopy(_config())
    fully_disabled["trios"]["lite"]["enabled"] = False
    fully_disabled["trios"]["mixed"]["enabled"] = False
    no_op = build_trios_payload(fully_disabled, node="ct110", live_counts={})
    # All rows still report; capacity 1 on lite keeps drain False even though
    # the launcher would not schedule it.
    assert no_op["drain"] is False


def test_tolerates_missing_and_malformed_inputs() -> None:
    """A blank / weird config emits a valid envelope, not an exception."""

    payload = build_trios_payload({}, node="ct110", live_counts=None)
    assert payload["schema"] == SCHEMA
    assert payload["trios"] == []
    assert payload["drain"] is True
    assert payload["configSha256"]

    # Non-dict trio entries are filtered out; "x": "not-a-dict" is dropped.
    payload2 = build_trios_payload({"trios": {"x": "not-a-dict"}}, node="ct110", live_counts={})
    assert payload2["trios"] == []

    # A real dict spec survives even when the capacity map is unrelated.
    payload3 = build_trios_payload({"trios": {"x": {}}, "capacity": {"weird": "x"}}, node="ct110", live_counts={})
    assert len(payload3["trios"]) == 1
    assert payload3["trios"][0]["name"] == "x"
    assert payload3["trios"][0]["capacity"] == 0


# ---------------------------------------------------------------------------
# Reporter integration
# ---------------------------------------------------------------------------


def _enabled_reporter() -> FleetReporter:
    """A reporter whose LH_HARNESS_FLEET_* env is fully set + isolated."""

    return FleetReporter(
        fleet_url="https://fleet.example.test",
        node="ct110",
        key="key",
        labels={"kind": "ct110"},
    )


def test_send_trios_dispatches_payload_when_enabled() -> None:
    """send_trios calls _post with the documented endpoint + gzip_body=False."""

    reporter = _enabled_reporter()
    payload = build_trios_payload(_config(), node="ct110", live_counts={"lite": 1})
    with patch.object(reporter, "_post") as post:
        reporter.send_trios(payload)
    post.assert_called_once()
    args, kwargs = post.call_args
    assert args == ("/harness/trios", payload)
    assert kwargs == {"gzip_body": False}


def test_send_trios_fails_open_on_post_raising() -> None:
    """A raising _post logs one warning and never bubbles into the caller."""

    reporter = _enabled_reporter()
    payload = build_trios_payload(_config(), node="ct110", live_counts={})

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("post blew up")

    with patch.object(reporter, "_post", side_effect=boom) as post:
        with patch("lh_harness.fleet.reporter.logger.warning") as warn:
            reporter.send_trios(payload)  # must NOT raise
    post.assert_called_once()
    assert warn.called
    assert "trios" in str(warn.call_args).lower()


def test_send_trios_noop_when_disabled() -> None:
    """A reporter that never had all the LH_HARNESS_FLEET_* env must skip _post."""

    # None of fleet_url / node / key -> constructor disables it.
    reporter = FleetReporter()  # type: ignore[call-arg]  # all args default
    assert reporter.enabled is False
    payload = build_trios_payload(_config(), node="ct110", live_counts={})
    with patch.object(reporter, "_post") as post:
        reporter.send_trios(payload)
    post.assert_not_called()


def test_send_trios_noop_when_only_url_set() -> None:
    """No ``LH_HARNESS_FLEET_*`` set at construction: reporter is disabled,
    ``send_trios`` is a no-op, and ``_post`` is never invoked."""

    reporter = FleetReporter()  # type: ignore[call-arg]  # all args default
    assert reporter.enabled is False
    payload = build_trios_payload(_config(), node="ct110", live_counts={})
    with patch.object(reporter, "_post") as post:
        reporter.send_trios(payload)
    post.assert_not_called()
