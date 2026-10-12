"""DR-05a rejection-path suite: approved_by + channel on every gate resolve.

Every gate/approval resolution records WHO approved (``approved_by``) and
through WHICH channel (``channel``).  Both are required inputs on the REST
resolve route and the MCP resolve tool; a resolve without them is a 422, a
resolve whose persona channel rides no verified identity is a 403 relayed
approval, and the unattributed ``service:`` marker is accepted only from an
authorized service caller (A3d signed caller or a gateway key alias) -- never
from caller payload text.

The tests here use stubs only: no network, no live gate, no real secret.
Caller secrets are generated per test process by ``caller_util``; the admin
identity headers are plain strings the SSO edge would produce.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.dashboard.state import ApprovalOption, DashboardState
from lh_harness.webapi.server import create_app

from .caller_util import caller_args, caller_headers

TOKEN = "bearer-" + "a" * 32
UPN = "upn:dev@corp.example"
UPN_2 = "upn:other@corp.example"
SLACK = "slack:U123ABCDEF"
TERMINAL = "terminal:deploy@ct110"


def _fixture(tmp_path: Path, *, caller_configs: Any = None) -> tuple[Path, DashboardState, TestClient]:
    root = tmp_path / "runs"
    run = root / "run-1"
    role_dir = run / "logs" / "role_management"
    role_dir.mkdir(parents=True)
    (run / "control").mkdir(parents=True)
    (role_dir / "rounds").mkdir(parents=True)
    (run / "logs" / "report.json").write_text(
        json.dumps({"task": "fixture", "status": "running"}), encoding="utf-8"
    )
    state = DashboardState(
        run / "logs",
        runs_root=root,
        control_enabled=True,
    )
    app = create_app(
        state=state,
        runs_root=root,
        control_enabled=True,
        auth_token=TOKEN,
        bind_host="testserver",
        caller_configs=caller_configs,
    )
    return root, state, TestClient(app)


def _auth_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    h = {"Authorization": f"Bearer {TOKEN}"}
    if extra:
        h.update(extra)
    return h


def _post(
    client: TestClient,
    path: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> Any:
    return client.post(path, json=payload, headers=_auth_headers(headers))


def _mcp(client: TestClient, arguments: dict[str, Any]) -> Any:
    return _post(client, "/api/mcp/fleet/harness_resolve_gate", {"arguments": arguments})


RESOLVE = "/api/runs/run-1/approvals"


# --------------------------------------------------------------------------
# Required field enforcement on the REST resolve route (422 paths).
# --------------------------------------------------------------------------
def test_resolve_without_approved_by_is_422(tmp_path: Path) -> None:
    root, state, client = _fixture(tmp_path)
    approval = state.create_approval(title="gate")
    r = _post(client, f"{RESOLVE}/{approval.approval_id}/resolve", {"action": "continue", "channel": "terminal",
                                                                 "approved_by": None})
    assert r.status_code == 422
    assert r.json()["error"] == "approved_by and channel are required"


def test_resolve_with_blank_approved_by_is_422(tmp_path: Path) -> None:
    root, state, client = _fixture(tmp_path)
    approval = state.create_approval(title="gate")
    r = _post(client, f"{RESOLVE}/{approval.approval_id}/resolve",
              {"action": "continue", "channel": "terminal", "approved_by": "   "})
    assert r.status_code == 422
    assert r.json()["error"] == "approved_by and channel are required"


def test_resolve_without_channel_is_422(tmp_path: Path) -> None:
    root, state, client = _fixture(tmp_path)
    approval = state.create_approval(title="gate")
    r = _post(client, f"{RESOLVE}/{approval.approval_id}/resolve", {"action": "continue", "approved_by": TERMINAL})
    assert r.status_code == 422
    assert r.json()["error"] == "approved_by and channel are required"


def test_resolve_with_bad_channel_is_422(tmp_path: Path) -> None:
    root, state, client = _fixture(tmp_path)
    approval = state.create_approval(title="gate")
    for bad in ("pigeon", "", "FLEET-CHAT", "service ", "web-sso\n"):
        r = _post(client, f"{RESOLVE}/{approval.approval_id}/resolve",
                  {"action": "continue", "approved_by": UPN, "channel": bad})
        assert r.status_code == 422, bad
        assert r.json()["error"] == "approved_by and channel are required"


def test_resolve_with_approved_by_too_long_is_422(tmp_path: Path) -> None:
    root, state, client = _fixture(tmp_path)
    approval = state.create_approval(title="gate")
    local = "u" * 240
    tail = "@example.com"
    long_upn = "upn:" + local + tail
    assert len(long_upn) > 254
    r = _post(client, f"{RESOLVE}/{approval.approval_id}/resolve",
              {"action": "continue", "approved_by": long_upn, "channel": "web-sso"})
    assert r.status_code == 422
    assert r.json()["error"] == "approved_by and channel are required"


def test_resolve_with_wrong_shape_approved_by_is_422(tmp_path: Path) -> None:
    root, state, client = _fixture(tmp_path)
    approval = state.create_approval(title="gate")
    for bad in ("upn:nodomain", "slack:not-U-prefixed", "terminal:no-at-sign", "random-text", "upn:"):
        r = _post(client, f"{RESOLVE}/{approval.approval_id}/resolve",
                  {"action": "continue", "approved_by": bad, "channel": "web-sso"})
        assert r.status_code == 422, bad
        assert r.json()["error"] == "approved_by and channel are required"


def test_resolve_with_prefix_that_disagrees_with_channel_is_422(tmp_path: Path) -> None:
    """A slack-dm resolve cannot carry an upn: identity (and vice versa)."""
    root, state, client = _fixture(tmp_path)
    approval = state.create_approval(title="gate")
    r = _post(client, f"{RESOLVE}/{approval.approval_id}/resolve",
              {"action": "continue", "approved_by": UPN, "channel": "slack-dm"})
    assert r.status_code == 422
    r2 = _post(client, f"{RESOLVE}/{approval.approval_id}/resolve",
               {"action": "continue", "approved_by": SLACK, "channel": "terminal"})
    assert r2.status_code == 422


# --------------------------------------------------------------------------
# Relay refusal: persona channels need a verified identity (403 + WARNING).
# --------------------------------------------------------------------------
def test_fleet_chat_with_no_identity_source_is_relayed_and_refused(caplog: Any, tmp_path: Path) -> None:
    root, state, client = _fixture(tmp_path)
    approval = state.create_approval(title="gate")
    with caplog.at_level(logging.WARNING, logger="lh_harness.webapi.server"):
        r = _post(client, f"{RESOLVE}/{approval.approval_id}/resolve",
                  {"action": "continue", "approved_by": UPN, "channel": "fleet-chat"})
    assert r.status_code == 403
    assert r.json()["error"] == "relayed approval refused"
    relayed = [rec for rec in caplog.records if "relayed approval refused" in rec.getMessage()]
    assert relayed, "expected a WARNING containing 'relayed approval refused'"
    assert "none" in relayed[0].getMessage()  # attempted identity source logged


def test_web_sso_with_no_identity_source_is_relayed_and_refused(caplog: Any, tmp_path: Path) -> None:
    root, state, client = _fixture(tmp_path)
    approval = state.create_approval(title="gate")
    with caplog.at_level(logging.WARNING, logger="lh_harness.webapi.server"):
        r = _post(client, f"{RESOLVE}/{approval.approval_id}/resolve",
                  {"action": "continue", "approved_by": UPN, "channel": "web-sso"})
    assert r.status_code == 403
    assert r.json()["error"] == "relayed approval refused"
    assert any("relayed approval refused" in rec.getMessage() for rec in caplog.records)


def test_service_channel_with_no_identity_source_is_refused(caplog: Any, tmp_path: Path) -> None:
    """service: marker can only ride an authorized service caller, never none."""
    root, state, client = _fixture(tmp_path)
    approval = state.create_approval(title="gate")
    with caplog.at_level(logging.WARNING, logger="lh_harness.webapi.server"):
        r = _post(client, f"{RESOLVE}/{approval.approval_id}/resolve",
                  {"action": "continue", "approved_by": "service:hydra-mcp", "channel": "service"})
    assert r.status_code == 403
    assert r.json()["error"] == "relayed approval refused"
    assert any("relayed approval refused" in rec.getMessage() for rec in caplog.records)


# --------------------------------------------------------------------------
# The unattributed service marker: accepted only from an authorized caller.
# --------------------------------------------------------------------------
def test_service_channel_from_signed_caller_is_accepted_and_flagged(tmp_path: Path) -> None:
    configs = {"hydra": {"secret_env": "LH_HARNESS_CALLER_HYDRA_SECRET", "tools": ["harness_resolve_gate"]}}
    root, state, client = _fixture(tmp_path, caller_configs=configs)
    from .caller_util import install_caller_secret, clear_caller_secrets

    install_caller_secret("hydra")
    approval = state.create_approval(title="gate")
    r = _mcp(client, caller_args(
        {"run_id": "run-1", "approval_id": approval.approval_id, "action": "continue",
         "approved_by": "service:hydra-mcp", "channel": "service"},
        "hydra",
    ))
    assert r.status_code == 200, r.text
    ok = r.json()
    assert ok["ok"] is True
    resolved = state.get_approval(approval.approval_id)
    assert resolved is not None and resolved.status == "resolved"
    assert resolved.approved_by == "service:hydra-mcp"
    assert resolved.channel == "service"
    assert resolved.unattributed is True
    clear_caller_secrets()


def test_service_channel_via_rest_caller_accepted(tmp_path: Path) -> None:
    configs = {"operator": {"secret_env": "LH_HARNESS_CALLER_OPERATOR_SECRET", "tools": ["harness_resolve_gate"]}}
    root, state, client = _fixture(tmp_path, caller_configs=configs)
    from .caller_util import install_caller_secret, clear_caller_secrets

    install_caller_secret("operator")
    approval = state.create_approval(title="gate")
    r = _post(client, f"{RESOLVE}/{approval.approval_id}/resolve",
              {"action": "continue", "approved_by": "service:overseer1", "channel": "service"},
              headers=caller_headers("operator"))
    assert r.status_code == 200, r.text
    resolved = state.get_approval(approval.approval_id)
    assert resolved is not None
    assert resolved.approved_by == "service:overseer1" and resolved.channel == "service"
    assert resolved.unattributed is True
    clear_caller_secrets()


def test_service_channel_cannot_be_forged_through_the_payload(tmp_path: Path) -> None:
    """A bare (no caller signature) dispatch has identity_source "none" -- a
    payload-supplied service: alias must not resolve."""
    root, state, client = _fixture(tmp_path, caller_configs={})
    approval = state.create_approval(title="gate")
    r = _mcp(client, {"run_id": "run-1", "approval_id": approval.approval_id, "action": "continue",
                      "approved_by": "service:hydra-mcp", "channel": "service"})
    assert r.status_code == 401  # anon: no signature at all


# --------------------------------------------------------------------------
# slack-dm / terminal accept path: record + resolve-time run event fields.
# --------------------------------------------------------------------------
def _enable_reporter(monkeypatch: Any) -> None:
    """Point the fleet reporter at a fake URL so local run-event writes happen."""
    monkeypatch.setenv("LH_HARNESS_FLEET_URL", "http://localhost:0")
    monkeypatch.setenv("LH_HARNESS_FLEET_NODE", "dr05a-test")
    monkeypatch.setenv("LH_HARNESS_FLEET_KEY", "a" * 64)
    from lh_harness.fleet import reporter

    rep = reporter.get_reporter(reset=True)
    if rep is not None and rep.enabled:
        rep._post = lambda *args, **kwargs: None  # type: ignore[method-assign]


def test_slack_dm_from_valid_caller_is_accepted_and_recorded(monkeypatch: Any, caplog: Any,
                                                              tmp_path: Path) -> None:
    _enable_reporter(monkeypatch)
    configs = {"chat-agent": {"secret_env": "LH_HARNESS_CALLER_CHAT_AGENT_SECRET", "tools": ["harness_resolve_gate"]}}
    root, state, client = _fixture(tmp_path, caller_configs=configs)
    from .caller_util import install_caller_secret, clear_caller_secrets

    install_caller_secret("chat-agent")
    approval = state.create_approval(title="gate", options=[ApprovalOption(value="continue", label="Continue")])
    r = _mcp(client, caller_args(
        {"run_id": "run-1", "approval_id": approval.approval_id, "action": "continue",
         "approved_by": SLACK, "channel": "slack-dm", "user_input": "approved over dm"},
        "chat-agent",
    ))
    assert r.status_code == 200, r.text
    resolved = state.get_approval(approval.approval_id)
    assert resolved is not None and resolved.status == "resolved"
    assert resolved.approved_by == SLACK
    assert resolved.channel == "slack-dm"
    # The resolve-time run event carries both fields plus the report clause.
    events_path = root / "run-1" / "logs" / "role_management" / "events.jsonl"
    if not events_path.exists():
        state._apply_pending_resolutions()
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    approval_resolved = [e for e in events if e.get("event") == "approval_resolved"]
    assert approval_resolved, "expected an approval_resolved run event"
    payload = approval_resolved[-1]
    assert payload.get("approved_by") == SLACK
    assert payload.get("channel") == "slack-dm"
    assert payload.get("approved_via") == f"approved by {SLACK} via slack-dm"
    assert payload.get("unattributed") is False
    clear_caller_secrets()


def test_terminal_channel_from_valid_caller_is_accepted_and_recorded(tmp_path: Path) -> None:
    configs = {"operator": {"secret_env": "LH_HARNESS_CALLER_OPERATOR_SECRET", "tools": ["harness_resolve_gate"]}}
    root, state, client = _fixture(tmp_path, caller_configs=configs)
    from .caller_util import install_caller_secret, clear_caller_secrets

    install_caller_secret("operator")
    approval = state.create_approval(title="gate", options=[ApprovalOption(value="continue", label="Continue")])
    r = _post(client, f"{RESOLVE}/{approval.approval_id}/resolve",
              {"action": "continue", "approved_by": TERMINAL, "channel": "terminal"},
              headers=caller_headers("operator"))
    assert r.status_code == 200, r.text
    resolved = state.get_approval(approval.approval_id)
    assert resolved is not None
    assert resolved.approved_by == TERMINAL and resolved.channel == "terminal"
    assert resolved.unattributed is False
    clear_caller_secrets()


def test_old_records_without_identity_still_load(tmp_path: Path) -> None:
    """Additive fields: a pre-DR-05a record loads with empty identity."""
    root, state, client = _fixture(tmp_path)
    role_dir = root / "run-1" / "logs" / "role_management"
    record = {
        "approval_id": "legacy-1",
        "title": "old gate",
        "options": [{"value": "continue", "label": "Continue"}],
        "status": "pending",
    }
    with (role_dir / "approvals.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    approval = state.get_approval("legacy-1")
    assert approval is not None
    assert approval.approved_by == "" and approval.channel == "" and approval.unattributed is False


# --------------------------------------------------------------------------
# /admin identity fallback chain (UPN / preferred_username headers).
# --------------------------------------------------------------------------
PROXY = "proxy-" + "p" * 32


def _admin_client(tmp_path: Path, admins: tuple[str, ...]) -> TestClient:
    from lh_harness.ask_store import AskRuntime
    from lh_harness.settings_store import SettingsRuntime, SettingsStore
    from lh_harness.webapi.server import create_app as _create

    store = SettingsStore(tmp_path / "settings.db", "k" * 40)
    rt = SettingsRuntime(store=store, admins=tuple(a.lower() for a in admins), proxy_auth=PROXY, error=None)
    runs = tmp_path / "admin-runs"
    runs.mkdir(exist_ok=True)
    ask = AskRuntime(str(tmp_path / "asks"), grants={"fleet-admin": ["overseer:write"]})
    app = _create(runs_root=runs, auth_token=TOKEN, bind_host="testserver", probe_open_pr=None,
                  settings_runtime=rt, ask_runtime=ask)
    return TestClient(app)


def _admin_headers(
    *,
    email: str | None = None,
    preferred: str | None = None,
    upn: str | None = None,
) -> dict[str, str]:
    h = {"Authorization": f"Bearer {TOKEN}", "X-LH-Proxy-Auth": PROXY}
    if email:
        h["X-Auth-Request-Email"] = email
    if preferred:
        h["X-Forwarded-Preferred-Username"] = preferred
    if upn:
        h["X-Forwarded-User"] = upn
    return h


def test_admin_empty_email_with_preferred_username_is_admin(tmp_path: Path) -> None:
    client = _admin_client(tmp_path, ("dev@corp.example",))
    r = client.get("/api/admin/whoami", headers=_admin_headers(email="", preferred="dev@corp.example"))
    assert r.status_code == 200
    assert r.json()["is_admin"] is True
    assert r.json()["identity"] == "dev@corp.example"


def test_admin_empty_email_with_upn_header_is_admin(tmp_path: Path) -> None:
    client = _admin_client(tmp_path, ("dev@corp.example",))
    r = client.get("/api/admin/whoami", headers=_admin_headers(email="", upn="dev@corp.example"))
    assert r.status_code == 200
    assert r.json()["is_admin"] is True
    assert r.json()["identity"] == "dev@corp.example"


def test_admin_preferred_username_wins_over_upn(tmp_path: Path) -> None:
    client = _admin_client(tmp_path, ("first@corp.example",))
    r = client.get("/api/admin/whoami",
                   headers=_admin_headers(email="", preferred="first@corp.example", upn="other@corp.example"))
    assert r.status_code == 200
    assert r.json()["is_admin"] is True
    assert r.json()["identity"] == "first@corp.example"


def test_admin_no_identity_header_at_all_is_not_admin(tmp_path: Path) -> None:
    client = _admin_client(tmp_path, ("dev@corp.example",))
    r = client.get("/api/admin/whoami", headers=_admin_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["is_admin"] is False and body["reason"]


def test_admin_case_insensitive_match(tmp_path: Path) -> None:
    client = _admin_client(tmp_path, ("Dev@Corp.Example",))
    r = client.get("/api/admin/whoami", headers=_admin_headers(email="", preferred="dev@CORP.example"))
    assert r.status_code == 200
    assert r.json()["is_admin"] is True


def test_admin_preferred_username_not_on_list_is_not_admin(tmp_path: Path) -> None:
    client = _admin_client(tmp_path, ("dev@corp.example",))
    r = client.get("/api/admin/whoami",
                   headers=_admin_headers(email="", preferred="someone@else.example"))
    assert r.status_code == 200
    body = r.json()
    assert body["is_admin"] is False and body["reason"]
