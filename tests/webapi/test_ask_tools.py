"""Task A3d: the ask-store MCP tools, their scopes and the list_open_asks merge."""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lh_harness.caller_auth import compute_signature
from lh_harness.mcp_tools import dispatch

from .caller_util import caller_args, clear_caller_secrets, install_caller_secret

TARGET = "ct202-mcp-tools-env:GITHUB_MCP_TOKEN"
GRANTS = {"fleet-admin": ["overseer:write"], "overseer": ["overseer", "overseer:apply"]}
ARCHIVE_ROW = (
    "| 279-gateway-github-token | Fine-grained PAT for github_mcp | CREDENTIAL | placeholder is 11 chars"
    " | mint a PAT | merges stay manual | open |\n"
)


@pytest.fixture(autouse=True)
def _secrets() -> Any:
    for name in ("fleet-admin", "overseer", "hydra", "operator"):
        install_caller_secret(name)
    yield
    clear_caller_secrets()
    import os

    os.environ.pop("LH_HARNESS_CALLER_FLEET_ADMIN_SECRET", None)


class _FakeState:
    def __init__(self, approvals: list[dict[str, Any]]) -> None:
        self._approvals = approvals
        self.resolved: list[Any] = []

    def list_approvals(self) -> list[dict[str, Any]]:
        return list(self._approvals)

    def resolve_approval(self, *args: Any, **kwargs: Any) -> bool:  # pragma: no cover - must not run
        self.resolved.append((args, kwargs))
        return True


class _World:
    def __init__(self, tmp_path: Path, *, gated: bool = True) -> None:
        self.runs = tmp_path / "runs"
        self.runs.mkdir()
        self.archive = tmp_path / "archive"
        for sub in ("tasks", "queue", "docs"):
            (self.archive / sub).mkdir(parents=True)
        (self.archive / "queue" / "OPEN-ASKS.md").write_text(
            "Updated 2026-10-01\n\n| id | ask | kind | evidence | recommended | default if silent | state |\n"
            "|---|---|---|---|---|---|---|\n" + ARCHIVE_ROW,
            encoding="utf-8",
        )
        self.state = _FakeState([{"approval_id": "ap1", "status": "pending", "title": "Push?"}])
        self.gated = gated
        self.registry = SimpleNamespace(state_for=lambda run_id: self.state if run_id == "run-g" else None, runs_root=None)
        self.supervisor = SimpleNamespace(
            list_run_summaries=lambda statuses=None: [{"id": "run-g", "status": "waiting_approval"}] if self.gated else [],
            runs_root=None,
        )
        self.queue = SimpleNamespace(list=lambda: [])

    def call(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        caller: str | None = None,
        grants: dict[str, list[str]] | None = GRANTS,
        writer: Any = None,
        caller_configs: Any = None,
    ) -> dict[str, Any]:
        args = caller_args(arguments, caller=caller) if caller else dict(arguments)
        return dispatch(
            tool,
            args,
            queue_store=self.queue,
            registry=self.registry,
            supervisor=self.supervisor,
            auth_token=None,
            request_token=None,
            caller_configs=caller_configs,
            overseer_root=str(self.archive),
            ask_grants=grants,
            ask_root=str(self.runs),
            ask_apply_writer=writer,
        )

    def asks(self, include_closed: bool = False) -> dict[str, Any]:
        return self.call("list_open_asks", {"include_closed": include_closed})


def _pat() -> str:
    return "github_pat_" + secrets.token_hex(30)


# --------------------------------------------------------------------- scopes --
WRITE_CALLS = {
    "raise_open_ask": ({"id": "x-1", "ask": "q", "kind": "DECISION"}, "overseer"),
    "declare_ask_fields": ({"id": "x-1", "fields": [{"name": "a", "type": "body"}]}, "overseer"),
    "respond_open_ask": ({"id": "x-1", "actor": "p@example.com", "fields": {"response": "y"}}, "fleet-admin"),
    "clear_ask_secret": ({"id": "x-1", "field": "secret", "actor": "p@example.com"}, "fleet-admin"),
    "apply_ask_secret": ({"id": "x-1", "field": "secret", "target": TARGET}, "overseer"),
}


@pytest.mark.parametrize("tool", sorted(WRITE_CALLS))
def test_every_ask_tool_refuses_anon_and_missing_scope(tmp_path: Path, tool: str) -> None:
    world = _World(tmp_path)
    arguments, _ = WRITE_CALLS[tool]
    assert world.call(tool, arguments)["code"] == 401  # no identity at all
    forged = dict(arguments, caller="overseer", caller_ts=str(int(time.time())), caller_sig="ab" * 32)
    assert world.call(tool, forged)["code"] == 401  # bad signature
    # A caller that is not in [asks.grants] and not in [callers] has no
    # identity for ask tools at all: 401.
    assert world.call(tool, arguments, caller="hydra")["code"] == 401
    # Verified through [callers] but holding no ask grant: 403.
    hydra_cfg = {"hydra": {"secret_env": "LH_HARNESS_CALLER_HYDRA_SECRET", "tools": ["harness_resolve_gate"]}}
    assert world.call(tool, arguments, caller="hydra", caller_configs=hydra_cfg)["code"] == 403
    # No [asks.grants] at all (the default): nobody holds an ask identity.
    assert world.call(tool, arguments, caller="overseer", grants={})["code"] == 401
    audit = (world.runs / "queue" / "caller_audit.jsonl").read_text(encoding="utf-8")
    assert tool in audit and "denied" in audit


def test_scopes_are_not_interchangeable(tmp_path: Path) -> None:
    world = _World(tmp_path)
    # fleet-admin (overseer:write) cannot raise, declare or apply.
    for tool in ("raise_open_ask", "declare_ask_fields", "apply_ask_secret"):
        assert world.call(tool, WRITE_CALLS[tool][0], caller="fleet-admin")["code"] == 403
    # the overseer (overseer + overseer:apply) cannot respond or clear.
    for tool in ("respond_open_ask", "clear_ask_secret"):
        assert world.call(tool, WRITE_CALLS[tool][0], caller="overseer")["code"] == 403


def test_task174_tools_lists_do_not_grant_ask_scopes(tmp_path: Path) -> None:
    world = _World(tmp_path)
    configs = {"overseer": {"secret_env": "LH_HARNESS_CALLER_OVERSEER_SECRET", "tools": ["harness_resolve_gate"]}}
    result = world.call("raise_open_ask", WRITE_CALLS["raise_open_ask"][0], caller="overseer", grants={}, caller_configs=configs)
    assert result["code"] == 403


def test_unknown_argument_named(tmp_path: Path) -> None:
    world = _World(tmp_path)
    result = world.call("raise_open_ask", {"id": "x", "ask": "q", "kind": "DECISION", "value": "s"}, caller="overseer")
    assert result["code"] == 400 and "value" in result["error"]


# ----------------------------------------------------- round trip, no leakage --
def test_respond_round_trip_never_returns_the_secret(tmp_path: Path) -> None:
    world = _World(tmp_path)
    value = _pat()
    raised = world.call(
        "raise_open_ask", {"id": "cred-1", "ask": "token please", "kind": "CREDENTIAL"}, caller="overseer"
    )
    assert raised["ok"] and [f["name"] for f in raised["row"]["fields"]] == ["response", "secret"]
    resp = world.call(
        "respond_open_ask",
        {"id": "cred-1", "actor": "paxton@example.com", "fields": {"response": "done", "secret": value}},
        caller="fleet-admin",
    )
    assert resp["ok"] and resp["row"]["state"].endswith("by paxton@example.com (web)")
    listed = world.asks(include_closed=True)
    blob = json.dumps([raised, resp, listed, world.asks()])
    assert value not in blob
    row = next(r for r in listed["rows"] if r["id"] == "cred-1")
    assert row["response"]["secret"]["length"] == len(value)
    assert all(value not in p.read_text(encoding="utf-8") for p in (world.runs / "asks").glob("*.json"))
    audit = world.runs / "queue" / "caller_audit.jsonl"
    assert not audit.exists() or value not in audit.read_text(encoding="utf-8")


def test_closed_rows_hidden_unless_include_closed_and_edit_reopens(tmp_path: Path) -> None:
    world = _World(tmp_path, gated=False)
    world.call("raise_open_ask", {"id": "dec-1", "ask": "pick one", "kind": "DECISION"}, caller="overseer")
    assert "dec-1" in [r["id"] for r in world.asks()["rows"]]
    world.call("respond_open_ask", {"id": "dec-1", "actor": "a@example.com", "fields": {"response": "A"}}, caller="fleet-admin")
    assert "dec-1" not in [r["id"] for r in world.asks()["rows"]]
    world.call("respond_open_ask", {"id": "dec-1", "actor": "b@example.com", "fields": {"response": "B"}}, caller="fleet-admin")
    row = next(r for r in world.asks(include_closed=True)["rows"] if r["id"] == "dec-1")
    assert row["response"]["response"] == "B" and row["state"].endswith("by b@example.com (web)")


# --------------------------------------------------------------------- apply --
def test_apply_off_allow_list_refused_and_audited(tmp_path: Path) -> None:
    world = _World(tmp_path)
    world.call("raise_open_ask", {"id": "cred-1", "ask": "t", "kind": "CREDENTIAL"}, caller="overseer")
    world.call("respond_open_ask", {"id": "cred-1", "actor": "a@example.com", "fields": {"secret": _pat()}}, caller="fleet-admin")
    result = world.call(
        "apply_ask_secret",
        {"id": "cred-1", "field": "secret", "target": "ct202-mcp-tools-env:LITELLM_MASTER_KEY"},
        caller="overseer",
        writer=lambda *a: pytest.fail("writer must not run"),
    )
    assert result["code"] == 403
    row = next(r for r in world.asks(include_closed=True)["rows"] if r["id"] == "cred-1")
    assert row["state"].startswith("CLOSED")


def test_apply_success_and_failure_through_dispatch(tmp_path: Path) -> None:
    world = _World(tmp_path)
    value = _pat()
    world.call("raise_open_ask", {"id": "cred-1", "ask": "t", "kind": "CREDENTIAL"}, caller="overseer")
    world.call("respond_open_ask", {"id": "cred-1", "actor": "a@example.com", "fields": {"secret": value}}, caller="fleet-admin")

    def failing(_t: str, _s: Any, v: str) -> None:
        raise RuntimeError(f"connection reset while sending {v}")

    failed = world.call("apply_ask_secret", {"id": "cred-1", "field": "secret", "target": TARGET}, caller="overseer", writer=failing)
    assert failed["ok"] is False and failed["code"] == 502 and value not in json.dumps(failed)
    open_rows = world.asks()["rows"]
    row = next(r for r in open_rows if r["id"] == "cred-1")  # back in the default view
    assert "apply failed" in row["evidence"] and value not in row["evidence"]

    got: list[str] = []
    ok = world.call(
        "apply_ask_secret",
        {"id": "cred-1", "field": "secret", "target": TARGET},
        caller="overseer",
        writer=lambda _t, _s, v: got.append(v),
    )
    assert ok == {"ok": True, "applied": True, "target": TARGET, "at": ok["at"]}
    assert got == [value]
    audit = (world.runs / "queue" / "caller_audit.jsonl").read_text(encoding="utf-8")
    assert "applied" in audit and value not in audit


# --------------------------------------------------------- live/archive rows --
def test_archive_row_copied_into_store_on_first_response(tmp_path: Path) -> None:
    world = _World(tmp_path, gated=False)
    before = world.asks()["rows"]
    archive_row = next(r for r in before if r["id"] == "279-gateway-github-token")
    assert archive_row["origin"] == "archive"
    assert [f["name"] for f in archive_row["fields"]] == ["response", "secret"]
    resp = world.call(
        "respond_open_ask",
        {"id": "279-gateway-github-token", "actor": "p@example.com", "fields": {"secret": _pat()}},
        caller="fleet-admin",
    )
    assert resp["ok"] and resp["row"]["origin"] == "archive"
    assert (world.runs / "asks" / "279-gateway-github-token.json").is_file()
    # Store wins: the file row is gone from the default view, the store row is closed.
    assert "279-gateway-github-token" not in [r["id"] for r in world.asks()["rows"]]
    rows = [r for r in world.asks(include_closed=True)["rows"] if r["id"] == "279-gateway-github-token"]
    assert len(rows) == 1 and rows[0]["state"].startswith("CLOSED")
    # Nothing is written back to OPEN-ASKS.md.
    assert ARCHIVE_ROW in (world.archive / "queue" / "OPEN-ASKS.md").read_text(encoding="utf-8")


def test_response_to_gate_row_never_resolves_the_gate(tmp_path: Path) -> None:
    world = _World(tmp_path)
    gate_id = "gate-run-g-ap1"
    assert gate_id in [r["id"] for r in world.asks()["rows"]]
    resp = world.call(
        "respond_open_ask", {"id": gate_id, "actor": "p@example.com", "fields": {"response": "go ahead"}}, caller="fleet-admin"
    )
    assert resp["ok"] and resp["row"]["origin"] == "live"
    assert world.state.resolved == []
    assert gate_id not in [r["id"] for r in world.asks()["rows"]]
    shown = next(r for r in world.asks(include_closed=True)["rows"] if r["id"] == gate_id)
    assert shown["response"]["response"] == "go ahead"
    # Once the gate is gone, its store row is dropped.
    world.gated = False
    assert gate_id not in [r["id"] for r in world.asks(include_closed=True)["rows"]]


def test_raise_refuses_live_prefix_and_existing_ids(tmp_path: Path) -> None:
    world = _World(tmp_path)
    assert world.call("raise_open_ask", {"id": "gate-x", "ask": "q", "kind": "DECISION"}, caller="overseer")["code"] == 400
    assert world.call(
        "raise_open_ask", {"id": "279-gateway-github-token", "ask": "q", "kind": "CREDENTIAL"}, caller="overseer"
    )["code"] == 409
    assert world.call("respond_open_ask", {"id": "nope", "actor": "a@example.com", "fields": {"response": "x"}}, caller="fleet-admin")["code"] == 404


# ------------------------------------------------------- HTTP + client script --
def test_http_route_and_signing_client(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from lh_harness.webapi.server import create_app

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "ask_tool", Path(__file__).resolve().parents[2] / "scripts" / "ask-apply" / "ask_tool.py"
    )
    ask_tool = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(ask_tool)

    runs = tmp_path / "runs"
    runs.mkdir()
    app = create_app(runs_root=runs, auth_token="tok", bind_host="testserver", ask_grants=GRANTS, probe_open_pr=None)
    client = TestClient(app)
    import os

    secret = os.environ["LH_HARNESS_CALLER_OVERSEER_SECRET"]
    signed = ask_tool.build_arguments({"id": "h-1", "ask": "q", "kind": "DECISION"}, "overseer", secret)
    assert signed["caller_sig"] == compute_signature("overseer", signed["caller_ts"], secret)
    headers = {"Authorization": "Bearer tok"}
    ok = client.post("/api/mcp/fleet/raise_open_ask", json={"arguments": signed}, headers=headers)
    assert ok.status_code == 200, ok.text
    unsigned = client.post(
        "/api/mcp/fleet/respond_open_ask",
        json={"arguments": {"id": "h-1", "actor": "x@example.com", "fields": {"response": "y"}}},
        headers=headers,
    )
    assert unsigned.status_code == 401
    no_bearer = client.post("/api/mcp/fleet/raise_open_ask", json={"arguments": signed})
    assert no_bearer.status_code == 401
    assert ask_tool.main(["ask_tool.py", "respond_open_ask", "{}"]) == 2
