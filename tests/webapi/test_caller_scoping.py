"""Task 174: per-caller tool scoping and budget ceilings at the harness.

Covers the scope items end to end:

- caller identity: ``X-Harness-Caller`` REST header / ``caller`` MCP field,
  HMAC over ``(caller, ts)``; missing/unknown/bad signature -> ``anon`` -> 401;
- the per-tool allowlist on all five dispatch tools and their REST twins,
  with the migration-doc defaults (chat-agent: enqueue/list/status only);
- ``harness_resolve_gate`` + the REST resolve route refusing non-allowlisted
  callers with 403 and an audit event ``{caller, tool, run_id, decision}``;
- requested_by stamped from the verified caller and round-tripping through
  list output;
- the ceilings: hourly enqueue cap -> 429 with Retry-After, ``max_rounds_clamp``
  -> 422, never a silent truncation;
- chat-agent DELETE restricted to entries it created.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
import starlette.testclient as starlette_testclient
import fastapi.testclient as fastapi_testclient


class _Loopback(starlette_testclient.TestClient):
    def __init__(self, app: Any, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("base_url", "http://127.0.0.1")
        super().__init__(app, *args, **kwargs)


starlette_testclient.TestClient = _Loopback
fastapi_testclient.TestClient = _Loopback

from fastapi.testclient import TestClient  # noqa: E402

from lh_harness.caller_auth import (  # noqa: E402
    ANON_CALLER,
    RESOLVE_TOOL,
    canonical_timestamp,
    compute_signature,
    emit_caller_audit,
    enqueue_rate_violation,
    may_delete_entry,
    resolve_mcp_caller,
    resolve_rest_caller,
    rounds_clamp_violation,
)
from lh_harness.config import _caller_secret_env  # noqa: E402
from lh_harness.queue import QueueStore  # noqa: E402
from lh_harness.webapi.server import create_app  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import caller_util as cu  # noqa: E402


def _runs_root(tmp_path: Path) -> Path:
    root = tmp_path / "runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _app(
    tmp_path: Path,
    *,
    caller_configs: dict[str, dict[str, Any]] | None = None,
) -> TestClient:
    """Hermetic app: injected caller table, no CWD config lookup."""
    root = _runs_root(tmp_path)
    app = create_app(
        runs_root=root,
        caller_configs=cu.build_specs(caller_configs),
    )
    return TestClient(app)


def _enqueue_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "scoped task",
        "task": "do the scoped thing",
        "workspace": str(Path(tempfile.gettempdir()) / "ws"),
        "trio": "kimi",
        "priority": 0,
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def _secrets():
    cu.install_caller_secret("chat-agent")
    cu.install_caller_secret("operator")
    cu.install_caller_secret("overseer")
    cu.install_caller_secret("hydra")
    yield
    cu.clear_caller_secrets()


# --------------------------------------------------------------- identity --


class TestTimestampCanonicalization:
    def test_canonicalizes_int_and_float(self) -> None:
        assert canonical_timestamp(1_700_000_000) == "1700000000"
        assert canonical_timestamp(1_700_000_000.0) == "1700000000"
        assert canonical_timestamp("1700000000") == "1700000000"
        assert canonical_timestamp(" 1700000000 ") == "1700000000"

    def test_rejects_fractional_and_garbage(self) -> None:
        assert canonical_timestamp(1_700_000_000.5) is None
        assert canonical_timestamp("1700000000.5") is None
        assert canonical_timestamp("abc") is None
        assert canonical_timestamp("") is None
        assert canonical_timestamp(None) is None
        assert canonical_timestamp(True) is None
        assert canonical_timestamp(float("nan")) is None

    def test_signed_via_canonical_form_is_honored(self) -> None:
        ts = str(int(time.time()))
        secret = "s3cret-value-never-committed"
        headers = {
            "x-harness-caller": "operator",
            "x-harness-timestamp": ts,
            "x-harness-signature": compute_signature("operator", ts, secret),
        }
        specs = {
            "operator": {"secret_env": "TEST_SIG_ENV", "tools": [], "max_entries_per_hour": None, "max_rounds_clamp": None, "rest_run_control": False}
        }
        os.environ["TEST_SIG_ENV"] = secret
        try:
            assert resolve_rest_caller(headers, specs) == "operator"
        finally:
            os.environ.pop("TEST_SIG_ENV")

    def test_stale_timestamp_is_anon(self) -> None:
        old = str(int(time.time()) - 4000)
        specs = {
            "operator": {"secret_env": "TEST_SIG_ENV", "tools": [], "max_entries_per_hour": None, "max_rounds_clamp": None, "rest_run_control": False}
        }
        os.environ["TEST_SIG_ENV"] = "whatever"
        headers = {
            "x-harness-caller": "operator",
            "x-harness-timestamp": old,
            "x-harness-signature": compute_signature("operator", old, "whatever"),
        }
        try:
            assert resolve_rest_caller(headers, specs) == ANON_CALLER
        finally:
            os.environ.pop("TEST_SIG_ENV")


class TestCallerIdentity:
    def test_valid_hmac_rest_caller_is_honored(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        response = client.post(
            "/api/queue", json=_enqueue_payload(), headers=cu.caller_headers("operator")
        )
        assert response.status_code == 200, response.text
        assert response.json()["requested_by"] == "operator"

    def test_missing_headers_are_anon_401(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        assert client.post("/api/queue", json=_enqueue_payload()).status_code == 401
        assert client.get("/api/queue").status_code == 401

    def test_bare_caller_name_without_signature_is_anon(self, tmp_path: Path) -> None:
        # A leaked bearer plus a bare name is not an identity: the HMAC pair
        # is required, so a header-only spoof cannot act as operator.
        client = _app(tmp_path)
        response = client.post(
            "/api/queue", json=_enqueue_payload(), headers=cu.unsigned_headers("operator")
        )
        assert response.status_code == 401

    def test_bad_signature_is_anon(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        headers = cu.caller_headers("operator")
        headers["X-Harness-Signature"] = "0" * 64
        assert client.post("/api/queue", json=_enqueue_payload(), headers=headers).status_code == 401

    def test_wrong_secret_is_anon(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        ts = str(int(time.time()))
        headers = {
            "X-Harness-Caller": "operator",
            "X-Harness-Timestamp": ts,
            "X-Harness-Signature": compute_signature("operator", ts, "not-the-real-secret"),
        }
        assert client.post("/api/queue", json=_enqueue_payload(), headers=headers).status_code == 401

    def test_unknown_caller_name_is_anon(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        headers = cu.caller_headers("operator")
        headers["X-Harness-Caller"] = "impostor"
        assert client.post("/api/queue", json=_enqueue_payload(), headers=headers).status_code == 401

    def test_stale_timestamp_is_anon_rest(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        headers = cu.rest_headers_with_ts("operator", str(int(time.time()) - 4000))
        assert client.post("/api/queue", json=_enqueue_payload(), headers=headers).status_code == 401

    def test_mcp_missing_caller_is_anon_401(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        response = client.post(
            "/api/mcp/fleet/harness_list_queue", json={"arguments": {}}
        )
        assert response.status_code == 401
        assert response.json()["code"] == 401

    def test_mcp_bad_signature_is_anon_401(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        arguments = cu.caller_args({}, "operator")
        arguments["caller_sig"] = "ff" * 32
        response = client.post(
            "/api/mcp/fleet/harness_list_queue", json={"arguments": arguments}
        )
        assert response.status_code == 401

    def test_caller_fields_are_stripped_before_tool_arguments(self, tmp_path: Path) -> None:
        # strip_caller_arguments is what keeps caller/caller_ts/caller_sig from
        # reaching the tool handlers (and the queue payload).
        client = _app(tmp_path)
        response = client.post(
            "/api/mcp/fleet/harness_enqueue_task",
            json={"arguments": cu.caller_args(_enqueue_payload(), "operator")},
        )
        assert response.status_code == 200
        store = QueueStore(_runs_root(tmp_path))
        entries = store.list()
        assert len(entries) == 1
        assert entries[0].requested_by == "operator"


# -------------------------------------------------------------- allowlist --


class TestAllowlistDefaults:
    def test_chat_agent_allowed_tools(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        # The three queue tools work.
        assert (
            client.post(
                "/api/queue", json=_enqueue_payload(), headers=cu.caller_headers("chat-agent")
            ).status_code
            == 200
        )
        assert client.get("/api/queue", headers=cu.caller_headers("chat-agent")).status_code == 200
        # ...and resolve is refused with 403 (not 401): identity verified,
        # tool not allowlisted.
        response = client.post(
            "/api/runs/run-1/approvals/ap-1/resolve",
            json={"action": "continue"},
            headers=cu.caller_headers("chat-agent"),
        )
        assert response.status_code == 403
        assert "harness_resolve_gate" in response.json()["detail"]

    def test_chat_agent_refused_on_run_control(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        response = client.post(
            "/api/runs", json={"task": "x"}, headers=cu.caller_headers("chat-agent")
        )
        assert response.status_code == 403

    def test_chat_agent_resolve_audit_event_written(self, tmp_path: Path) -> None:
        root = _runs_root(tmp_path)
        app = create_app(runs_root=root, caller_configs={})
        client = TestClient(app)
        client.post(
            "/api/runs/run-9/approvals/ap-2/resolve",
            json={"action": "continue"},
            headers=cu.caller_headers("chat-agent"),
        )
        audit_path = root / "queue" / "caller_audit.jsonl"
        assert audit_path.is_file()
        events = [json.loads(line) for line in audit_path.read_text().splitlines()]
        assert events[-1]["caller"] == "chat-agent"
        assert events[-1]["tool"] == RESOLVE_TOOL
        assert events[-1]["run_id"] == "run-9"
        assert events[-1]["decision"] == "denied"
        assert "ts" in events[-1]

    def test_anon_resolve_attempt_also_audited(self, tmp_path: Path) -> None:
        # A bare bearer with no caller headers trying to resolve a gate is the
        # leak scenario from migration doc 3.4 -- it must leave a trace.
        root = _runs_root(tmp_path)
        app = create_app(runs_root=root, caller_configs={})
        client = TestClient(app)
        response = client.post(
            "/api/runs/run-1/approvals/ap-1/resolve", json={"action": "continue"}
        )
        assert response.status_code == 401
        events = [
            json.loads(line)
            for line in (root / "queue" / "caller_audit.jsonl").read_text().splitlines()
        ]
        assert events[-1]["caller"] == ANON_CALLER
        assert events[-1]["tool"] == RESOLVE_TOOL
        assert events[-1]["decision"] == "denied"

    def test_operator_can_resolve_rest(self, tmp_path: Path) -> None:
        root = _runs_root(tmp_path)
        (root / "run-1" / "control").mkdir(parents=True)
        app = create_app(runs_root=root, caller_configs={})
        client = TestClient(app)
        response = client.post(
            "/api/runs/run-1/approvals/ap-x/resolve",
            json={"action": "continue"},
            headers=cu.caller_headers("operator"),
        )
        # Identity + allowlist pass (409 is the run-state layer's answer, not
        # an authorization refusal).
        assert response.status_code in (200, 404, 409)
        assert response.status_code != 403
        assert not (root / "queue" / "caller_audit.jsonl").exists()

    def test_operator_all_tools_via_mcp(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        response = client.post(
            "/api/mcp/fleet/harness_list_contentions",
            json={"arguments": cu.caller_args({}, "operator")},
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_mcp_chat_agent_refused_on_resolve(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        response = client.post(
            "/api/mcp/fleet/harness_resolve_gate",
            json={
                "arguments": cu.caller_args(
                    {"run_id": "run-1", "approval_id": "ap", "action": "continue"},
                    "chat-agent",
                )
            },
        )
        assert response.status_code == 403

    def test_mcp_chat_agent_resolve_audit_event_written(self, tmp_path: Path) -> None:
        root = _runs_root(tmp_path)
        app = create_app(runs_root=root, caller_configs={})
        client = TestClient(app)
        client.post(
            "/api/mcp/fleet/harness_resolve_gate",
            json={
                "arguments": cu.caller_args(
                    {"run_id": "run-4", "approval_id": "ap", "action": "continue"},
                    "chat-agent",
                )
            },
        )
        events = [
            json.loads(line)
            for line in (root / "queue" / "caller_audit.jsonl").read_text().splitlines()
        ]
        assert events[-1] == {
            **{key: events[-1][key] for key in ("ts",)},
            "caller": "chat-agent",
            "tool": RESOLVE_TOOL,
            "run_id": "run-4",
            "decision": "denied",
        }

    def test_anon_mcp_dispatch_401_on_every_tool(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        for tool in (
            "harness_enqueue_task",
            "harness_list_queue",
            "harness_run_status",
            "harness_resolve_gate",
            "harness_list_contentions",
        ):
            response = client.post(
                f"/api/mcp/fleet/{tool}", json={"arguments": {"run_id": "r"}}
            )
            assert response.status_code == 401, tool

    def test_no_chat_agent_resolve_path_in_caller_table(self) -> None:
        from lh_harness.config import load_caller_configs

        chat_agent = load_caller_configs()["chat-agent"]
        assert RESOLVE_TOOL not in chat_agent["tools"]
        assert chat_agent["rest_run_control"] is False


# ----------------------------------------------------------- requested_by --


class TestRequestedByStamping:
    def test_rest_request_body_cannot_forged_requested_by(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        response = client.post(
            "/api/queue",
            json=_enqueue_payload(requested_by="someone-else"),
            headers=cu.caller_headers("chat-agent"),
        )
        assert response.status_code == 200
        store = QueueStore(_runs_root(tmp_path))
        assert store.list()[0].requested_by == "chat-agent"

    def test_requested_by_round_trips_in_list_output(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        created = client.post(
            "/api/queue", json=_enqueue_payload(), headers=cu.caller_headers("chat-agent")
        ).json()
        listed = client.get("/api/queue", headers=cu.caller_headers("chat-agent")).json()
        entry = next(
            item for item in listed["entries"] if item["queue_id"] == created["queue_id"]
        )
        assert entry["requested_by"] == "chat-agent"

    def test_mcp_requested_by_overridden_by_caller(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        response = client.post(
            "/api/mcp/fleet/harness_enqueue_task",
            json={
                "arguments": cu.caller_args(
                    _enqueue_payload(requested_by="forged"), "operator"
                )
            },
        )
        assert response.status_code == 200
        store = QueueStore(_runs_root(tmp_path))
        assert store.list()[0].requested_by == "operator"


# --------------------------------------------------------------- ceilings --


class TestEnqueueRateCeiling:
    def test_chat_agent_429_after_ten_entries_with_retry_after(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        headers = cu.caller_headers("chat-agent")
        for _ in range(10):
            assert (
                client.post("/api/queue", json=_enqueue_payload(), headers=headers).status_code
                == 200
            )
        response = client.post("/api/queue", json=_enqueue_payload(), headers=headers)
        assert response.status_code == 429
        assert "max_entries_per_hour" in response.json()["detail"]
        retry_after = response.headers["Retry-After"]
        assert retry_after.isdigit() and int(retry_after) >= 1

    def test_operator_unlimited_by_default(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        headers = cu.caller_headers("operator")
        for _ in range(12):
            assert (
                client.post("/api/queue", json=_enqueue_payload(), headers=headers).status_code
                == 200
            )

    def test_ceiling_window_uses_requested_by(self, tmp_path: Path) -> None:
        # Entries created by another caller do not consume chat-agent's budget.
        client = _app(tmp_path)
        for _ in range(10):
            assert (
                client.post(
                    "/api/queue", json=_enqueue_payload(), headers=cu.caller_headers("chat-agent")
                ).status_code
                == 200
            )
        # operator still fine
        assert (
            client.post(
                "/api/queue", json=_enqueue_payload(), headers=cu.caller_headers("operator")
            ).status_code
            == 200
        )

    def test_mcp_429_carries_retry_after_header(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        for _ in range(10):
            client.post(
                "/api/mcp/fleet/harness_enqueue_task",
                json={"arguments": cu.caller_args(_enqueue_payload(), "chat-agent")},
            )
        response = client.post(
            "/api/mcp/fleet/harness_enqueue_task",
            json={"arguments": cu.caller_args(_enqueue_payload(), "chat-agent")},
        )
        assert response.status_code == 429
        assert response.json()["code"] == 429
        assert response.headers["Retry-After"].isdigit()

    def test_config_can_raise_ceiling(self, tmp_path: Path) -> None:
        client = _app(
            tmp_path,
            caller_configs={
                "chat-agent": {"max_entries_per_hour": 2},
            },
        )
        headers = cu.caller_headers("chat-agent")
        assert client.post("/api/queue", json=_enqueue_payload(), headers=headers).status_code == 200
        assert client.post("/api/queue", json=_enqueue_payload(), headers=headers).status_code == 200
        assert client.post("/api/queue", json=_enqueue_payload(), headers=headers).status_code == 429

    def test_zero_ceiling_blocks_every_enqueue(self, tmp_path: Path) -> None:
        # 0 is a legal configured ceiling: zero entries per hour.
        client = _app(
            tmp_path,
            caller_configs={"chat-agent": {"max_entries_per_hour": 0}},
        )
        headers = cu.caller_headers("chat-agent")
        assert client.post("/api/queue", json=_enqueue_payload(), headers=headers).status_code == 429


class TestRoundsClamp:
    def test_chat_agent_422_over_clamp(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        response = client.post(
            "/api/queue",
            json=_enqueue_payload(max_rounds=51),
            headers=cu.caller_headers("chat-agent"),
        )
        assert response.status_code == 422
        assert "max_rounds_clamp" in response.json()["detail"]
        # Nothing was written.
        assert QueueStore(_runs_root(tmp_path)).list() == []

    def test_chat_agent_422_on_omitted_max_rounds_when_below_default(
        self, tmp_path: Path
    ) -> None:
        # The store default (25) is over a clamp of 20, so omitting the field
        # must refuse too -- the clamp cannot be bypassed by omission.
        client = _app(tmp_path, caller_configs={"chat-agent": {"max_rounds_clamp": 20}})
        response = client.post(
            "/api/queue", json=_enqueue_payload(), headers=cu.caller_headers("chat-agent")
        )
        assert response.status_code == 422

    def test_chat_agent_within_clamp_ok(self, tmp_path: Path) -> None:
        client = _app(tmp_path, caller_configs={"chat-agent": {"max_rounds_clamp": 50}})
        response = client.post(
            "/api/queue",
            json=_enqueue_payload(max_rounds=50),
            headers=cu.caller_headers("chat-agent"),
        )
        assert response.status_code == 200

    def test_operator_unlimited(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        response = client.post(
            "/api/queue",
            json=_enqueue_payload(max_rounds=1000),
            headers=cu.caller_headers("operator"),
        )
        assert response.status_code == 200

    def test_mcp_clamp_422(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        response = client.post(
            "/api/mcp/fleet/harness_enqueue_task",
            json={
                "arguments": cu.caller_args(_enqueue_payload(max_rounds=51), "chat-agent")
            },
        )
        assert response.status_code == 422
        assert response.json()["code"] == 422

    def test_rounds_never_silently_truncated(self, tmp_path: Path) -> None:
        # Any request that succeeds stores the requested max_rounds verbatim.
        client = _app(tmp_path)
        client.post(
            "/api/queue",
            json=_enqueue_payload(max_rounds=50),
            headers=cu.caller_headers("chat-agent"),
        )
        entry = QueueStore(_runs_root(tmp_path)).list()[0]
        assert entry.max_rounds == 50


class TestClampHelperUnit:
    def test_omitted_max_rounds_checked_against_default(self) -> None:
        spec = {"max_rounds_clamp": 20}
        assert rounds_clamp_violation(spec, None) is not None
        assert rounds_clamp_violation(spec, 20) is None
        assert rounds_clamp_violation(spec, 21) is not None

    def test_none_clamp_allows_anything(self) -> None:
        assert rounds_clamp_violation({"max_rounds_clamp": None}, 1000) is None
        assert rounds_clamp_violation(None, 1000) is None

    def test_non_int_max_rounds_left_to_store_validation(self) -> None:
        assert rounds_clamp_violation({"max_rounds_clamp": 20}, "ten") is None


class TestRateHelperUnit:
    def _store_with_entries(self, tmp_path: Path, requested_by: str, count: int, *, age: float = 0.0) -> QueueStore:
        store = QueueStore(_runs_root(tmp_path))
        for index in range(count):
            entry = store.create(
                {
                    "name": f"e{index}",
                    "task": "t",
                    "workspace": "w",
                    "trio": "kimi",
                    "requested_by": requested_by,
                }
            )
            if age:
                path = store._path(entry.queue_id)
                data = json.loads(path.read_text())
                data["created_at"] = time.time() - age
                path.write_text(json.dumps(data))
        return store

    def test_violation_includes_retry_after(self, tmp_path: Path) -> None:
        store = self._store_with_entries(tmp_path, "chat-agent", 10)
        spec = {"max_entries_per_hour": 10}
        violation = enqueue_rate_violation(spec, store, "chat-agent")
        assert violation is not None
        assert violation["code"] == 429
        assert 1 <= violation["retry_after"] <= 3600

    def test_under_limit_no_violation(self, tmp_path: Path) -> None:
        store = self._store_with_entries(tmp_path, "chat-agent", 9)
        assert enqueue_rate_violation({"max_entries_per_hour": 10}, store, "chat-agent") is None

    def test_window_expires(self, tmp_path: Path) -> None:
        store = self._store_with_entries(tmp_path, "chat-agent", 10, age=3601)
        assert enqueue_rate_violation({"max_entries_per_hour": 10}, store, "chat-agent") is None

    def test_other_callers_entries_do_not_count(self, tmp_path: Path) -> None:
        self._store_with_entries(tmp_path, "operator", 25)
        store = QueueStore(_runs_root(tmp_path))
        assert enqueue_rate_violation({"max_entries_per_hour": 10}, store, "chat-agent") is None

    def test_unlimited_spec(self, tmp_path: Path) -> None:
        self._store_with_entries(tmp_path, "chat-agent", 30)
        store = QueueStore(_runs_root(tmp_path))
        assert enqueue_rate_violation({"max_entries_per_hour": None}, store, "chat-agent") is None


# ---------------------------------------------------------------- delete ---


class TestDeleteOwnership:
    def test_chat_agent_deletes_own_entry(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        headers = cu.caller_headers("chat-agent")
        created = client.post("/api/queue", json=_enqueue_payload(), headers=headers).json()
        response = client.delete(f"/api/queue/{created['queue_id']}", headers=headers)
        assert response.status_code == 200

    def test_chat_agent_cannot_delete_foreign_entry(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        created = client.post(
            "/api/queue", json=_enqueue_payload(), headers=cu.caller_headers("operator")
        ).json()
        response = client.delete(
            f"/api/queue/{created['queue_id']}", headers=cu.caller_headers("chat-agent")
        )
        assert response.status_code == 403
        # The entry survives.
        assert QueueStore(_runs_root(tmp_path)).get(created["queue_id"]) is not None

    def test_operator_deletes_any_entry(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        chat = cu.caller_headers("chat-agent")
        created = client.post("/api/queue", json=_enqueue_payload(), headers=chat).json()
        response = client.delete(
            f"/api/queue/{created['queue_id']}", headers=cu.caller_headers("operator")
        )
        assert response.status_code == 200

    def test_anon_delete_401(self, tmp_path: Path) -> None:
        client = _app(tmp_path)
        created = client.post(
            "/api/queue", json=_enqueue_payload(), headers=cu.caller_headers("operator")
        ).json()
        assert client.delete(f"/api/queue/{created['queue_id']}").status_code == 401


# ------------------------------------------------------------ ownership unit


class TestMayDeleteEntryUnit:
    def _entry(self, requested_by: str) -> Any:
        from lh_harness.queue import QueueEntry

        return QueueEntry(
            queue_id="q-1",
            name="n",
            task="t",
            workspace="w",
            max_rounds=25,
            trio="kimi",
            priority=0,
            requested_by=requested_by,
        )

    def test_resolver_caller_class_manages_whole_queue(self) -> None:
        spec = {"tools": [RESOLVE_TOOL]}
        assert may_delete_entry("operator", spec, self._entry("chat-agent")) is True

    def test_plain_caller_only_own_entries(self) -> None:
        spec = {"tools": ["harness_list_queue"]}
        assert may_delete_entry("chat-agent", spec, self._entry("chat-agent")) is True
        assert may_delete_entry("chat-agent", spec, self._entry("operator")) is False

    def test_anon_never(self) -> None:
        assert may_delete_entry(ANON_CALLER, {"tools": [RESOLVE_TOOL]}, self._entry("x")) is False


# ------------------------------------------------------------ config file --


class TestCallerConfigFromFile:
    def test_load_caller_configs_defaults_match_doc(self, tmp_path: Path) -> None:
        from lh_harness.config import load_caller_configs

        specs = load_caller_configs(Path("/nonexistent/path/config.toml"))
        assert set(specs) == {"chat-agent", "operator", "overseer", "hydra"}
        chat_agent = specs["chat-agent"]
        assert chat_agent["tools"] == [
            "harness_enqueue_task",
            "harness_list_queue",
            "harness_run_status",
        ]
        assert chat_agent["max_entries_per_hour"] == 10
        assert chat_agent["max_rounds_clamp"] == 50
        assert specs["overseer"]["rest_run_control"] is True
        assert specs["operator"]["rest_run_control"] is False
        assert specs["hydra"]["max_entries_per_hour"] is None
        # Env NAMES only.
        assert chat_agent["secret_env"] == "LH_HARNESS_CALLER_CHAT_AGENT_SECRET"

    def test_create_app_with_caller_configs_is_hermetic(self, tmp_path: Path) -> None:
        # create_app with an injected table must not consult the CWD config.
        cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            client = _app(tmp_path, caller_configs={"operator": {"max_entries_per_hour": 1}})
            headers = cu.caller_headers("operator")
            assert client.post("/api/queue", json=_enqueue_payload(), headers=headers).status_code == 200
            assert client.post("/api/queue", json=_enqueue_payload(), headers=headers).status_code == 429
        finally:
            os.chdir(cwd)

    def test_reserved_anon_rejected_in_config(self, tmp_path: Path) -> None:
        from lh_harness.config import ProjectConfigError, _flatten_callers_table

        with pytest.raises(ProjectConfigError, match="reserved"):
            _flatten_callers_table({"anon": {}}, tmp_path / "config.toml")


class TestAuditSink:
    def test_emit_caller_audit_appends_jsonl(self, tmp_path: Path) -> None:
        root = _runs_root(tmp_path)
        assert emit_caller_audit(root, {"caller": "c", "tool": "t", "run_id": "r", "decision": "denied"})
        assert emit_caller_audit(root, {"caller": "c2", "tool": "t", "run_id": "r", "decision": "denied"})
        lines = (root / "queue" / "caller_audit.jsonl").read_text().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[1])["caller"] == "c2"

    def test_emit_caller_audit_tolerates_bad_root(self, tmp_path: Path) -> None:
        assert emit_caller_audit(None, {"caller": "c"}) is False
        assert emit_caller_audit("", {"caller": "c"}) is False