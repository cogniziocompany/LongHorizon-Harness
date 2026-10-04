"""Task A3d: the ask-store MCP tools, request signing, scopes and the list merge."""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lh_harness.ask_store import AskRuntime, LocalVault
from lh_harness.caller_auth import ask_signature
from lh_harness.mcp_tools import dispatch

from .caller_util import caller_secret, clear_caller_secrets, install_caller_secret

TARGET = "ct202-mcp-tools-env:GITHUB_MCP_TOKEN"
GRANTS = {"fleet-admin": ["overseer:write"], "overseer": ["overseer", "overseer:apply"]}
CRED_FIELDS = [{"name": "response", "type": "body"}, {"name": "secret", "type": "secret", "apply_target": TARGET}]
ARCHIVE_ROW = (
    "| 279-gateway-github-token | Fine-grained PAT for github_mcp | CREDENTIAL | placeholder is 11 chars"
    " | mint a PAT | merges stay manual | open |\n"
)


@pytest.fixture(autouse=True)
def _secrets() -> Any:
    for name in ("fleet-admin", "overseer", "hydra"):
        install_caller_secret(name)
    yield
    clear_caller_secrets()
    os.environ.pop("LH_HARNESS_CALLER_FLEET_ADMIN_SECRET", None)


def signed(tool: str, arguments: dict[str, Any], caller: str, *, ts: int | None = None, nonce: str | None = None) -> dict[str, Any]:
    ts_text = str(int(time.time()) if ts is None else ts)
    nonce = nonce or secrets.token_hex(16)
    sig = ask_signature(tool, caller, ts_text, nonce, arguments, caller_secret(caller))
    return {**arguments, "caller": caller, "caller_ts": ts_text, "caller_nonce": nonce, "caller_sig": sig}


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
    def __init__(self, tmp_path: Path, *, gated: bool = True, writer: Any = None, secrets_disabled: str | None = None) -> None:
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
            runs_root=str(self.runs),
        )
        self.queue = SimpleNamespace(list=lambda: [])
        self.runtime = AskRuntime(
            str(tmp_path / "store"),
            grants=dict(GRANTS),
            vault=LocalVault(tmp_path / "vault", writer=writer),
            secrets_disabled_reason=secrets_disabled,
        )

    def raw(self, tool: str, arguments: dict[str, Any], caller_configs: Any = None, runtime: Any = "default") -> dict[str, Any]:
        return dispatch(
            tool,
            arguments,
            queue_store=self.queue,
            registry=self.registry,
            supervisor=self.supervisor,
            auth_token=None,
            request_token=None,
            caller_configs=caller_configs,
            overseer_root=str(self.archive),
            ask_runtime=self.runtime if runtime == "default" else runtime,
        )

    def call(self, tool: str, arguments: dict[str, Any], caller: str, **kw: Any) -> dict[str, Any]:
        return self.raw(tool, signed(tool, arguments, caller), **kw)

    def asks(self, include_closed: Any = False) -> dict[str, Any]:
        return self.raw("list_open_asks", {"include_closed": include_closed})

    def audit(self) -> str:
        path = self.runs / "queue" / "caller_audit.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""


def _pat() -> str:
    return "github_pat_" + secrets.token_hex(30)


def _raise_cred(world: _World, ask_id: str = "cred-1") -> None:
    r = world.call("raise_open_ask", {"id": ask_id, "ask": "token please", "kind": "CREDENTIAL", "fields": CRED_FIELDS}, "overseer")
    assert r["ok"], r


# --------------------------------------------------------------- auth + scopes --
WRITE_CALLS = {
    "raise_open_ask": {"id": "x-1", "ask": "q", "kind": "DECISION"},
    "declare_ask_fields": {"id": "x-1", "fields": [{"name": "a", "type": "body"}]},
    "respond_open_ask": {"id": "x-1", "actor": "p@example.com", "fields": {"response": "y"}},
    "clear_ask_secret": {"id": "x-1", "field": "secret", "actor": "p@example.com"},
    "apply_ask_secret": {"id": "x-1", "field": "secret", "target": TARGET},
}


@pytest.mark.parametrize("tool", sorted(WRITE_CALLS))
def test_every_ask_tool_refuses_unsigned_bad_and_unscoped_callers(tmp_path: Path, tool: str) -> None:
    world = _World(tmp_path)
    arguments = WRITE_CALLS[tool]
    assert world.raw(tool, arguments)["code"] == 401
    forged = {**arguments, "caller": "overseer", "caller_ts": str(int(time.time())), "caller_nonce": "n" * 32, "caller_sig": "ab" * 32}
    assert world.raw(tool, forged)["code"] == 401
    # A caller absent from [asks.grants] and [callers] has no ask identity: 401.
    assert world.call(tool, arguments, "hydra")["code"] == 401
    # Verified through [callers] but holding no ask grant: 403.
    hydra_cfg = {"hydra": {"secret_env": "LH_HARNESS_CALLER_HYDRA_SECRET", "tools": ["harness_resolve_gate"]}}
    assert world.call(tool, arguments, "hydra", caller_configs=hydra_cfg)["code"] == 403
    audit = world.audit()
    assert '"decision": "unauthenticated"' in audit and '"decision": "denied"' in audit and tool in audit


def test_old_caller_ts_signature_is_not_accepted_for_ask_tools(tmp_path: Path) -> None:
    from .caller_util import caller_args

    world = _World(tmp_path)
    assert world.raw("raise_open_ask", caller_args(WRITE_CALLS["raise_open_ask"], caller="overseer"))["code"] == 401


def test_replay_tamper_and_skew_are_refused(tmp_path: Path) -> None:
    world = _World(tmp_path)
    args = {"id": "dec-1", "ask": "pick", "kind": "DECISION"}
    first = signed("raise_open_ask", args, "overseer")
    assert world.raw("raise_open_ask", first)["ok"] is True
    replay = world.raw("raise_open_ask", first)
    assert replay["code"] == 401 and "nonce" in replay["error"]
    tampered = signed("raise_open_ask", {"id": "dec-2", "ask": "pick", "kind": "DECISION"}, "overseer")
    tampered["ask"] = "something else"
    assert world.raw("raise_open_ask", tampered)["code"] == 401
    other_tool = signed("declare_ask_fields", {"id": "dec-1", "fields": [{"name": "a", "type": "body"}]}, "overseer")
    assert world.raw("raise_open_ask", other_tool)["code"] in (400, 401)
    future = signed("raise_open_ask", {"id": "dec-3", "ask": "x", "kind": "DECISION"}, "overseer", ts=int(time.time()) + 45)
    assert world.raw("raise_open_ask", future)["code"] == 401
    near_future = signed("raise_open_ask", {"id": "dec-4", "ask": "x", "kind": "DECISION"}, "overseer", ts=int(time.time()) + 20)
    assert world.raw("raise_open_ask", near_future)["ok"] is True
    stale = signed("raise_open_ask", {"id": "dec-5", "ask": "x", "kind": "DECISION"}, "overseer", ts=int(time.time()) - 301)
    assert world.raw("raise_open_ask", stale)["code"] == 401


def test_scopes_are_not_interchangeable(tmp_path: Path) -> None:
    world = _World(tmp_path)
    for tool in ("raise_open_ask", "declare_ask_fields", "apply_ask_secret"):
        assert world.call(tool, WRITE_CALLS[tool], "fleet-admin")["code"] == 403
    for tool in ("respond_open_ask", "clear_ask_secret"):
        assert world.call(tool, WRITE_CALLS[tool], "overseer")["code"] == 403


def test_disabled_runtime_answers_503(tmp_path: Path) -> None:
    world = _World(tmp_path)
    off = AskRuntime(None, disabled_reason="disabled: bad [asks] in x")
    r = world.call("raise_open_ask", WRITE_CALLS["raise_open_ask"], "overseer", runtime=off)
    assert r["code"] == 503 and "bad [asks]" in r["error"]
    assert world.call("raise_open_ask", WRITE_CALLS["raise_open_ask"], "overseer", runtime=None)["code"] == 503


def test_unknown_argument_named(tmp_path: Path) -> None:
    world = _World(tmp_path)
    r = world.call("raise_open_ask", {"id": "x", "ask": "q", "kind": "DECISION", "value": "s"}, "overseer")
    assert r["code"] == 400 and "value" in r["error"]


# ----------------------------------------------------- round trip, no leakage --
def test_respond_round_trip_never_returns_the_secret(tmp_path: Path) -> None:
    world = _World(tmp_path)
    value = _pat()
    _raise_cred(world)
    resp = world.call("respond_open_ask", {"id": "cred-1", "actor": "paxton@example.com", "fields": {"response": "done", "secret": value}, "secret_targets": {"secret": TARGET}}, "fleet-admin")
    assert resp["ok"] and resp["row"]["state"].endswith("by paxton@example.com (web)")
    listed = world.asks(include_closed=True)
    assert value not in json.dumps([resp, listed, world.asks()])
    row = next(r for r in listed["rows"] if r["id"] == "cred-1")
    assert row["response"]["secret"]["length"] == len(value)
    assert row["fields"][1]["apply_target"] == TARGET
    assert all(value not in p.read_text(encoding="utf-8") for p in (tmp_path / "store" / "asks").glob("*.json"))
    assert value not in world.audit()


def test_secret_tools_disabled_but_bodies_work(tmp_path: Path) -> None:
    world = _World(tmp_path, secrets_disabled="disabled: store readable by worker uid 1000 (/x)")
    _raise_cred(world)
    r = world.call("respond_open_ask", {"id": "cred-1", "actor": "a@example.com", "fields": {"secret": _pat()}, "secret_targets": {"secret": TARGET}}, "fleet-admin")
    assert r["code"] == 503 and r["error"].startswith("disabled: store readable by worker uid")
    ok = world.call("respond_open_ask", {"id": "cred-1", "actor": "a@example.com", "fields": {"response": "note"}}, "fleet-admin")
    assert ok["ok"] is True
    assert world.call("apply_ask_secret", {"id": "cred-1", "field": "secret", "target": TARGET}, "overseer")["code"] == 503


def test_closed_rows_hidden_unless_include_closed_and_edit_reopens(tmp_path: Path) -> None:
    world = _World(tmp_path, gated=False)
    world.call("raise_open_ask", {"id": "dec-1", "ask": "pick one", "kind": "DECISION"}, "overseer")
    assert "dec-1" in [r["id"] for r in world.asks()["rows"]]
    world.call("respond_open_ask", {"id": "dec-1", "actor": "a@example.com", "fields": {"response": "A"}}, "fleet-admin")
    assert "dec-1" not in [r["id"] for r in world.asks()["rows"]]
    world.call("respond_open_ask", {"id": "dec-1", "actor": "b@example.com", "fields": {"response": "B"}}, "fleet-admin")
    row = next(r for r in world.asks(include_closed=True)["rows"] if r["id"] == "dec-1")
    assert row["response"]["response"] == "B" and row["state"].endswith("by b@example.com (web)")


def test_include_closed_must_be_a_boolean(tmp_path: Path) -> None:
    world = _World(tmp_path)
    assert world.asks(include_closed="false")["code"] == 400
    assert world.asks(include_closed=False)["ok"] is True


# --------------------------------------------------------------------- apply --
def test_apply_off_list_and_mismatched_targets_refused(tmp_path: Path) -> None:
    world = _World(tmp_path, writer=lambda *a: pytest.fail("writer must not run"))
    _raise_cred(world)
    world.call("respond_open_ask", {"id": "cred-1", "actor": "a@example.com", "fields": {"secret": _pat()}, "secret_targets": {"secret": TARGET}}, "fleet-admin")
    r = world.call("apply_ask_secret", {"id": "cred-1", "field": "secret", "target": "ct202-mcp-tools-env:LITELLM_MASTER_KEY"}, "overseer")
    assert r["code"] == 403
    # The D2 default secret field declares no target: apply refuses (409).
    world.call("raise_open_ask", {"id": "cred-2", "ask": "t", "kind": "CREDENTIAL"}, "overseer")
    world.call("respond_open_ask", {"id": "cred-2", "actor": "a@example.com", "fields": {"secret": _pat()}, "secret_targets": {"secret": ""}}, "fleet-admin")
    assert world.call("apply_ask_secret", {"id": "cred-2", "field": "secret", "target": TARGET}, "overseer")["code"] == 409
    rows = {r["id"]: r for r in world.asks(include_closed=True)["rows"]}
    assert rows["cred-1"]["state"].startswith("CLOSED") and rows["cred-2"]["state"].startswith("CLOSED")


def test_apply_success_and_failure_through_dispatch(tmp_path: Path) -> None:
    value = _pat()
    got: list[str] = []
    mode = {"fail": True}

    def writer(_t: str, _s: Any, v: str) -> None:
        if mode["fail"]:
            raise RuntimeError(f"connection reset while sending {v}")
        got.append(v)

    world = _World(tmp_path, writer=writer)
    _raise_cred(world)
    world.call("respond_open_ask", {"id": "cred-1", "actor": "a@example.com", "fields": {"secret": value}, "secret_targets": {"secret": TARGET}}, "fleet-admin")
    failed = world.call("apply_ask_secret", {"id": "cred-1", "field": "secret", "target": TARGET}, "overseer")
    assert failed["code"] == 502 and value not in json.dumps(failed)
    row = next(r for r in world.asks()["rows"] if r["id"] == "cred-1")
    assert "apply failed" in row["evidence"] and value not in row["evidence"]
    mode["fail"] = False
    ok = world.call("apply_ask_secret", {"id": "cred-1", "field": "secret", "target": TARGET}, "overseer")
    assert ok == {"ok": True, "applied": True, "target": TARGET, "at": ok["at"]} and got == [value]
    assert '"decision": "applied"' in world.audit() and value not in world.audit()


# --------------------------------------------------------- live/archive rows --
def test_archive_row_copied_into_store_on_first_response(tmp_path: Path) -> None:
    world = _World(tmp_path, gated=False)
    archive_row = next(r for r in world.asks()["rows"] if r["id"] == "279-gateway-github-token")
    assert archive_row["origin"] == "archive" and [f["name"] for f in archive_row["fields"]] == ["response", "secret"]
    resp = world.call("respond_open_ask", {"id": "279-gateway-github-token", "actor": "p@example.com", "fields": {"secret": _pat()}, "secret_targets": {"secret": ""}}, "fleet-admin")
    assert resp["ok"] and resp["row"]["origin"] == "archive"
    assert "279-gateway-github-token" not in [r["id"] for r in world.asks()["rows"]]
    rows = [r for r in world.asks(include_closed=True)["rows"] if r["id"] == "279-gateway-github-token"]
    assert len(rows) == 1 and rows[0]["state"].startswith("CLOSED")
    assert ARCHIVE_ROW in (world.archive / "queue" / "OPEN-ASKS.md").read_text(encoding="utf-8")


def test_response_to_gate_row_never_resolves_or_hides_the_gate(tmp_path: Path) -> None:
    world = _World(tmp_path)
    gate_id = "gate-run-g-ap1"
    assert gate_id in [r["id"] for r in world.asks()["rows"]]
    resp = world.call("respond_open_ask", {"id": gate_id, "actor": "p@example.com", "fields": {"response": "go ahead"}}, "fleet-admin")
    assert resp["ok"] and resp["row"]["origin"] == "live"
    assert world.state.resolved == []
    shown = next(r for r in world.asks()["rows"] if r["id"] == gate_id)  # still in the DEFAULT view
    assert shown["state"] == "open"
    assert shown["response_state"] == "responded (context only), gate pending"
    assert shown["response"]["response"] == "go ahead" and "p@example.com" in shown["responded"]
    world.gated = False
    assert gate_id not in [r["id"] for r in world.asks(include_closed=True)["rows"]]


def test_raise_refuses_live_prefix_and_existing_ids(tmp_path: Path) -> None:
    world = _World(tmp_path)
    assert world.call("raise_open_ask", {"id": "gate-x", "ask": "q", "kind": "DECISION"}, "overseer")["code"] == 400
    assert world.call("raise_open_ask", {"id": "279-gateway-github-token", "ask": "q", "kind": "CREDENTIAL"}, "overseer")["code"] == 409
    assert world.call("respond_open_ask", {"id": "nope", "actor": "a@example.com", "fields": {"response": "x"}}, "fleet-admin")["code"] == 404


# ------------------------------------------------------- HTTP + client script --
def _ask_tool():
    spec = importlib.util.spec_from_file_location(
        "ask_tool", Path(__file__).resolve().parents[2] / "scripts" / "ask-apply" / "ask_tool.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_http_route_and_signing_client(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from lh_harness.webapi.server import create_app

    ask_tool = _ask_tool()
    runs = tmp_path / "runs"
    runs.mkdir()
    runtime = AskRuntime(str(tmp_path / "store"), grants=dict(GRANTS), vault=LocalVault(tmp_path / "vault"))
    client = TestClient(create_app(runs_root=runs, auth_token="tok", bind_host="testserver", ask_runtime=runtime, probe_open_pr=None))
    payload = ask_tool.build_arguments("raise_open_ask", {"id": "h-1", "ask": "q", "kind": "DECISION"}, "overseer", caller_secret("overseer"))
    headers = {"Authorization": "Bearer tok"}
    assert client.post("/api/mcp/fleet/raise_open_ask", json={"arguments": payload}, headers=headers).status_code == 200
    assert client.post("/api/mcp/fleet/raise_open_ask", json={"arguments": payload}, headers=headers).status_code == 401  # replay
    unsigned = client.post(
        "/api/mcp/fleet/respond_open_ask",
        json={"arguments": {"id": "h-1", "actor": "x@example.com", "fields": {"response": "y"}}},
        headers=headers,
    )
    assert unsigned.status_code == 401
    assert client.post("/api/mcp/fleet/raise_open_ask", json={"arguments": payload}).status_code == 401
    assert ask_tool.main(["ask_tool.py", "respond_open_ask", "{}"]) == 2


def test_create_app_survives_a_bad_asks_table(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from lh_harness.webapi.server import create_app

    runs = tmp_path / "runs"
    (runs / ".lh-harness").mkdir(parents=True)
    (runs / ".lh-harness" / "config.toml").write_text('[asks]\ngrnats = {x = 1}\n', encoding="utf-8")
    client = TestClient(create_app(runs_root=runs, auth_token="tok", bind_host="testserver", probe_open_pr=None))
    headers = {"Authorization": "Bearer tok"}
    assert client.get("/api/mcp/fleet/tools", headers=headers).status_code == 200
    r = client.post(
        "/api/mcp/fleet/raise_open_ask",
        json={"arguments": signed("raise_open_ask", {"id": "a", "ask": "q", "kind": "DECISION"}, "overseer")},
        headers=headers,
    )
    assert r.status_code == 503 and "bad [asks]" in r.json()["error"]


def test_cross_language_signature_vector() -> None:
    """Pinned vector; cognizioware-mcp-tools fleet-admin tests assert the same hex."""
    args = {"id": "279-gateway-github-token", "actor": "paxton@example.com", "fields": {"response": "ok\nnaïve – ✓", "secret": "ghp_x"}}
    sig = ask_signature("respond_open_ask", "fleet-admin", "1700000000", "0123456789abcdef0123456789abcdef", args, "test-secret")
    assert sig == VECTOR_SIG


VECTOR_SIG = "2078ce4ccb0136cb70e509a1a227885649683d96bb2b11df48015c8a2901993f"


def test_declare_refuses_to_retarget_a_sealed_field_through_dispatch(tmp_path: Path) -> None:
    world = _World(tmp_path)
    _raise_cred(world)
    world.call("respond_open_ask", {"id": "cred-1", "actor": "a@example.com", "fields": {"secret": _pat()}, "secret_targets": {"secret": TARGET}}, "fleet-admin")
    r = world.call("declare_ask_fields", {"id": "cred-1", "fields": [{"name": "secret", "type": "secret"}]}, "overseer")
    assert r["code"] == 409 and "sealed" in r["error"]


def test_respond_with_a_stale_target_is_refused(tmp_path: Path) -> None:
    world = _World(tmp_path)
    _raise_cred(world)
    stale = world.call("respond_open_ask", {"id": "cred-1", "actor": "a@example.com", "fields": {"secret": _pat()}, "secret_targets": {"secret": ""}}, "fleet-admin")
    assert stale["code"] == 409
    missing = world.call("respond_open_ask", {"id": "cred-1", "actor": "a@example.com", "fields": {"secret": _pat()}}, "fleet-admin")
    assert missing["code"] == 400


def test_dispatch_through_the_real_helper_end_to_end(tmp_path: Path) -> None:
    """The service forwards the exact signed request and the helper (same
    code that runs as the vault uid) re-verifies it, seals with the target the
    person saw, and applies only as the overseer."""
    import importlib.machinery
    import importlib.util
    import io

    from lh_harness.ask_store import HelperVault

    script = Path(__file__).resolve().parents[2] / "scripts" / "ask-apply" / "lh-ask-vault"
    loader = importlib.machinery.SourceFileLoader("lh_ask_vault_e2e", str(script))
    helper = importlib.util.module_from_spec(importlib.util.spec_from_loader("lh_ask_vault_e2e", loader))
    loader.exec_module(helper)
    (tmp_path / "hv").mkdir(mode=0o700)
    cfg = {
        "_path": str(tmp_path / "cfg.json"),
        "vault_dir": str(tmp_path / "hv"),
        "state_dir": str(tmp_path / "hstate"),
        "ssh_key": "/k",
        "ssh_dest": {TARGET: "lhapply@192.0.2.10"},
        "grants": GRANTS,
        "callers": {"fleet-admin": caller_secret("fleet-admin"), "overseer": caller_secret("overseer")},
    }
    sent: list[str] = []

    def ssh(argv, **kw):
        sent.append(kw["input"])
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    def run(argv, **kw):
        out, err = io.StringIO(), io.StringIO()
        import contextlib as _c

        with _c.redirect_stdout(out), _c.redirect_stderr(err):
            rc = helper.main(["lh-ask-vault", *argv[1:]], cfg=cfg, stdin=io.StringIO(kw.get("input") or ""), run=ssh)
        return SimpleNamespace(returncode=rc, stdout=out.getvalue(), stderr=err.getvalue())

    world = _World(tmp_path)
    world.runtime.vault = HelperVault(["/usr/local/sbin/lh-ask-vault"], run=run)
    value = _pat()
    _raise_cred(world)
    ok = world.call("respond_open_ask", {"id": "cred-1", "actor": "p@example.com", "fields": {"secret": value}, "secret_targets": {"secret": TARGET}}, "fleet-admin")
    assert ok["ok"], ok
    assert (tmp_path / "hv" / "cred-1" / "secret").read_text(encoding="utf-8") == value
    assert not (tmp_path / "vault").exists()  # nothing sealed service-side
    applied = world.call("apply_ask_secret", {"id": "cred-1", "field": "secret", "target": TARGET}, "overseer")
    assert applied["ok"] is True and sent == [value + "\n"]
    assert value not in json.dumps([ok, applied, world.asks(include_closed=True)])
