"""Admin settings screen API and key restriction view: admins only, no values."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")
from fastapi.testclient import TestClient

from lh_harness.ask_store import AskRuntime
import lh_harness.model_catalog as lh_model_catalog
from lh_harness.settings_store import SettingsRuntime, SettingsStore
import lh_harness.webapi.server as lh_server
from lh_harness.webapi.server import create_app

TOKEN = "bearer-" + "b" * 32
PROXY = "proxy-" + "p" * 32
ADMIN = "paxton@example.com"


def _client(tmp_path: Path, *, admins=(ADMIN,), proxy: str | None = PROXY, store: bool = True, token: str | None = TOKEN):
    st = SettingsStore(tmp_path / "settings.db", "k" * 40) if store else None
    rt = SettingsRuntime(store=st, admins=tuple(a.lower() for a in admins), proxy_auth=proxy,
                         error=None if store else "the settings store is not configured")
    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    ask = AskRuntime(str(tmp_path / "asks"), grants={"fleet-admin": ["overseer:write"], "overseer": ["overseer", "overseer:apply"]})
    app = create_app(runs_root=runs, auth_token=token, bind_host="testserver", probe_open_pr=None,
                     settings_runtime=rt, ask_runtime=ask)
    return TestClient(app), rt


def _client_with_protected(tmp_path: Path, *, protected_trios=(), admins=(ADMIN,), proxy: str | None = PROXY, store: bool = True, token: str | None = TOKEN):
    """Variant of ``_client`` that writes a project config under ``runs_root``
    with the requested ``[queue].protected_trios`` set, so the loader sees
    the rule (or no rule, when the list is empty). Used by the
    LHH-QUEUE-PROTECT-DOCS F3 tests; the older tests stay on ``_client``
    because they cover behaviour that should not depend on the protected list
    (the loader's default config lookup will find no file and return ``()``
    for the protected set).
    """

    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    cfg_path = runs / ".lh-harness" / "config.toml"
    cfg_path.parent.mkdir(exist_ok=True)
    if protected_trios:
        cfg_path.write_text(
            "[queue]\nprotected_trios = ["
            + ", ".join(f'"{name}"' for name in protected_trios)
            + "]\n",
            encoding="utf-8",
        )
    else:
        # Empty list explicitly disables the rule; ``load_run_defaults`` seeds
        # the default tuple when the key is missing, so to opt out we must
        # write the key with an empty list.
        cfg_path.write_text("[queue]\nprotected_trios = []\n", encoding="utf-8")
    st = SettingsStore(tmp_path / "settings.db", "k" * 40) if store else None
    rt = SettingsRuntime(store=st, admins=tuple(a.lower() for a in admins), proxy_auth=proxy,
                         error=None if store else "the settings store is not configured",
                         protected_trios=tuple(protected_trios))
    ask = AskRuntime(str(tmp_path / "asks"), grants={"fleet-admin": ["overseer:write"], "overseer": ["overseer", "overseer:apply"]})
    app = create_app(runs_root=runs, auth_token=token, bind_host="testserver", probe_open_pr=None,
                     settings_runtime=rt, ask_runtime=ask)
    return TestClient(app), rt


def _h(identity: str | None = ADMIN, proxy: str | None = PROXY, write: bool = False, bearer: str | None = TOKEN) -> dict[str, str]:
    h: dict[str, str] = {}
    if bearer:
        h["Authorization"] = f"Bearer {bearer}"
    if proxy:
        h["X-LH-Proxy-Auth"] = proxy
    if identity:
        h["X-Auth-Request-Email"] = identity
    if write:
        h["X-Requested-With"] = "lh-harness"
    return h


def test_admin_can_set_rotate_list_and_values_never_come_back(tmp_path: Path) -> None:
    client, rt = _client(tmp_path)
    value = "tok-" + secrets.token_hex(24)
    r = client.put("/api/admin/settings/GH_TOKEN", json={"value": value, "note": "repo-scoped", "expires_at": "2027-01-01"}, headers=_h(write=True))
    assert r.status_code == 200, r.text
    assert value not in r.text and r.json()["restart_required"] is True
    r2 = client.put("/api/admin/settings/LH_HARNESS_FLEET_URL", json={"value": "https://fleet.example"}, headers=_h(write=True))
    assert r2.json()["setting"]["value"] == "https://fleet.example"
    listed = client.get("/api/admin/settings", headers=_h())
    assert listed.status_code == 200 and value not in listed.text
    rows = {s["name"]: s for s in listed.json()["settings"]}
    assert rows["GH_TOKEN"]["set"] and rows["GH_TOKEN"]["source"] == "db" and "value" not in rows["GH_TOKEN"]
    assert rows["GH_TOKEN"]["length"] == len(value) and rows["GH_TOKEN"]["updated_by"] == ADMIN
    assert rows["GH_TOKEN"]["changed_since_start"] is True
    assert rows["LH_HARNESS_WEB_TOKEN"]["source"] in {"unset", "env"}  # catalogued even while unset
    audit = client.get("/api/admin/audit", headers=_h())
    assert value not in audit.text
    assert [e["action"] for e in audit.json()["entries"]][:2] == ["set", "set"]
    keys = client.get("/api/admin/keys", headers=_h())
    assert keys.status_code == 200 and value not in keys.text
    by = {k["name"]: k for k in keys.json()["keys"]}
    assert by["GH_TOKEN"]["expires_at"] == "2027-01-01" and by["GH_TOKEN"]["note"] == "repo-scoped"
    assert by["LH_HARNESS_CALLER_OVERSEER_SECRET"]["restrictions"]["ask_scopes"] == ["overseer", "overseer:apply"]
    assert by["LH_HARNESS_CALLER_FLEET_ADMIN_SECRET"]["restrictions"]["ask_scopes"] == ["overseer:write"]
    assert any(p["name"] == "default" for p in by["LH_HARNESS_MCP_GATEWAY_KEY"]["restrictions"]["mcp_profiles"])
    assert by["LH_HARNESS_WEB_TOKEN"]["restrictions"]["grants"].startswith("every /api route")
    assert client.delete("/api/admin/settings/GH_TOKEN", headers=_h(write=True)).status_code == 200
    assert rt.store.audit()[0]["action"] == "unset"


@pytest.mark.parametrize(
    "headers,code",
    [
        (lambda: _h(identity="intruder@example.com", write=True), 403),  # signed in, not an admin
        (lambda: _h(proxy="forged", write=True), 403),  # bearer + claimed identity, not via the SSO edge
        (lambda: _h(proxy=None, write=True), 403),
        (lambda: _h(identity=None, write=True), 403),
        (lambda: _h(write=False), 403),  # no X-Requested-With on a write
        (lambda: _h(bearer=None, write=True), 401),  # existing web auth
    ],
)
def test_only_admins_can_edit(tmp_path: Path, headers: Any, code: int) -> None:
    client, rt = _client(tmp_path)
    r = client.put("/api/admin/settings/GH_TOKEN", json={"value": "x" * 20}, headers=headers())
    assert r.status_code == code, r.text
    assert rt.store.list_metadata() == [] and rt.store.audit() == []


def test_reads_are_admin_only_too(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    for path in ("/api/admin/settings", "/api/admin/audit", "/api/admin/keys"):
        assert client.get(path, headers=_h(identity="intruder@example.com")).status_code == 403


@pytest.mark.parametrize("kwargs", [{"admins": ()}, {"proxy": None}, {"token": None}])
def test_admin_fails_closed_without_configuration(tmp_path: Path, kwargs: dict[str, Any]) -> None:
    client, _ = _client(tmp_path, **kwargs)
    r = client.put("/api/admin/settings/GH_TOKEN", json={"value": "x" * 20}, headers=_h(write=True))
    assert r.status_code == 403
    who = client.get("/api/admin/whoami", headers=_h()).json()
    assert who["is_admin"] is False and who["reason"]


def test_store_not_configured_answers_503_and_whoami_says_why(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, store=False)
    assert client.get("/api/admin/settings", headers=_h()).status_code == 503
    who = client.get("/api/admin/whoami", headers=_h()).json()
    assert who["is_admin"] is False and "not configured" in who["reason"]


def test_whoami_for_admin_and_bad_input(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    who = client.get("/api/admin/whoami", headers=_h(identity="PAXTON@example.com")).json()
    assert who["is_admin"] is True and who["identity"] == "PAXTON@example.com"
    assert client.put("/api/admin/settings/lower", json={"value": "x"}, headers=_h(write=True)).status_code == 400
    assert client.put("/api/admin/settings/LH_HARNESS_SETTINGS_KEY", json={"value": "x" * 40}, headers=_h(write=True)).status_code == 400
    assert client.put("/api/admin/settings/GH_TOKEN", json={"value": "x", "extra": 1}, headers=_h(write=True)).status_code == 400
    assert client.delete("/api/admin/settings/GH_TOKEN", headers=_h(write=True)).status_code == 404


def test_admin_pages_serve_the_app_shell_when_the_ui_is_built(tmp_path: Path) -> None:
    from lh_harness.webapi.server import _STATIC_DIR

    if not (_STATIC_DIR / "index.html").is_file():
        pytest.skip("web UI not built in this checkout")
    client, _ = _client(tmp_path)
    for path in ("/admin", "/admin/keys", "/admin/audit"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")


# --------------------------------- queue trios + capacity admin API (R4) --
# LHH-QUEUE-HOTRELOAD: trio defaults and capacity live in the settings DB; a
# PUT is visible to the launcher's per-pass store reads with no restart.
# Every test mocks the live model list -- no network, no provider probes.

_FAKE_CATALOG = {
    "agents": [],
    "models": {
        "claude_code": [
            {"id": "opus", "label": "opus · default", "availability": "detected"},
            {"id": "sonnet", "label": "sonnet", "availability": "detected"},
        ],
        "codex": [{"id": "codex-mini", "label": "codex-mini", "availability": "detected"}],
    },
    "model_discovery": {},
}


def _fake_model_catalog(*, force: bool = False, **_kwargs: Any) -> dict[str, Any]:
    return json.loads(json.dumps(_FAKE_CATALOG))


def _seeded_client(tmp_path: Path):
    # The default ``[queue].protected_trios`` is ``("kimi", "glm", "qwen")``;
    # the loader seeds that tuple when no project config sets the key, so a
    # plain ``_client`` would 409 every ``PUT /api/agent__trios`` we exercise
    # here on the seeded ``glm`` row.  Writing an empty protected list keeps
    # the rule off for this fixture without touching the global config or
    # other tests' behaviour.
    client, rt = _client_with_protected(tmp_path, protected_trios=())
    seeded = rt.store.seed_queue_defaults(
        {"glm": {"agent": "claude_code", "model": "opus", "mcp_profile": "default"}},
        {"glm_max": 2},
    )
    assert seeded == {"trios": ["glm"], "capacity": ["glm"]}
    return client, rt


def test_get_queue_trios_and_capacity(tmp_path: Path) -> None:
    # 200 here also proves the exact routes outrank the server's earlier
    # /api/queue/{queue_id} capture -- a shadowed route would answer 404.
    client, _ = _seeded_client(tmp_path)
    r = client.get("/api/queue/trios", headers=_h())
    assert r.status_code == 200, r.text
    trios = r.json()["trios"]
    assert [t["name"] for t in trios] == ["glm"]
    assert trios[0]["model"] == "opus" and trios[0]["agent"] == "claude_code" and trios[0]["enabled"] is True
    r = client.get("/api/queue/capacity", headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["capacity"] == {"glm": 2}


def test_put_trio_updates_store_audits_and_feeds_launcher_readers(tmp_path: Path, monkeypatch: Any) -> None:
    client, rt = _seeded_client(tmp_path)
    monkeypatch.setattr(lh_model_catalog, "discover_model_catalog", _fake_model_catalog)
    audit_before = len(rt.store.audit())
    r = client.put(
        "/api/queue/trios",
        json={"trios": [{"name": "glm", "agent": "claude_code", "model": "sonnet", "mcp_profile": "network"}]},
        headers=_h(write=True),
    )
    assert r.status_code == 200, r.text
    assert {t["name"]: t for t in r.json()["trios"]}["glm"]["model"] == "sonnet"
    # the launcher re-reads these store readers on every scheduling pass, so
    # the change applies to the next launch with no restart -- a wholly
    # separate store connection sees it too.  PUT replaces the whole row:
    # fields the request omits do not keep their old values.
    trios = {t["name"]: t for t in rt.store.queue_trios()}
    assert trios["glm"]["model"] == "sonnet" and trios["glm"]["enabled"] is True and trios["glm"]["mcp_profile"] == "network"
    mirror = SettingsStore(rt.store.path, "k" * 40)
    assert {t["name"]: t for t in mirror.queue_trios()}["glm"]["model"] == "sonnet"
    entries = rt.store.audit()
    assert len(entries) == audit_before + 1
    top = entries[0]
    assert top["action"] == "set" and top["name"] == "queue_trio:glm" and top["actor"] == ADMIN
    assert top["detail"]["before"]["model"] == "opus" and top["detail"]["after"]["model"] == "sonnet"


def test_put_trio_creates_a_new_trio(tmp_path: Path, monkeypatch: Any) -> None:
    client, rt = _seeded_client(tmp_path)
    monkeypatch.setattr(lh_model_catalog, "discover_model_catalog", _fake_model_catalog)
    r = client.put(
        "/api/queue/trios",
        json={"trios": [{"name": "codex-fast", "agent": "codex", "model": "codex-mini", "mcp_profile": None, "enabled": True}]},
        headers=_h(write=True),
    )
    assert r.status_code == 200, r.text
    trios = {t["name"]: t for t in rt.store.queue_trios()}
    assert trios["codex-fast"]["model"] == "codex-mini" and trios["codex-fast"]["agent"] == "codex"
    top = rt.store.audit()[0]
    assert top["name"] == "queue_trio:codex-fast" and top["detail"]["before"] is None


def test_put_trio_disable_and_profile_round_trip(tmp_path: Path, monkeypatch: Any) -> None:
    client, rt = _seeded_client(tmp_path)
    monkeypatch.setattr(lh_model_catalog, "discover_model_catalog", _fake_model_catalog)
    r = client.put(
        "/api/queue/trios",
        json={"trios": [{"name": "glm", "agent": "claude_code", "model": None, "mcp_profile": "  ", "enabled": False}]},
        headers=_h(write=True),
    )
    assert r.status_code == 200, r.text
    row = {t["name"]: t for t in rt.store.queue_trios()}["glm"]
    assert row["model"] is None and row["mcp_profile"] is None and row["enabled"] is False


def test_put_trio_refuses_unknown_model_and_changes_nothing(tmp_path: Path, monkeypatch: Any) -> None:
    client, rt = _seeded_client(tmp_path)
    monkeypatch.setattr(lh_model_catalog, "discover_model_catalog", _fake_model_catalog)
    before_trios = rt.store.queue_trios()
    before_audit = rt.store.audit()
    r = client.put(
        "/api/queue/trios",
        json={"trios": [{"name": "glm", "agent": "claude_code", "model": "not-a-real-model"}]},
        headers=_h(write=True),
    )
    assert r.status_code == 422, r.text
    assert "not-a-real-model" in r.json()["detail"] and "gl" in r.json()["detail"]
    assert rt.store.queue_trios() == before_trios and rt.store.audit() == before_audit


def test_put_trio_is_all_or_nothing(tmp_path: Path, monkeypatch: Any) -> None:
    client, rt = _seeded_client(tmp_path)
    monkeypatch.setattr(lh_model_catalog, "discover_model_catalog", _fake_model_catalog)
    before_audit = rt.store.audit()
    r = client.put(
        "/api/queue/trios",
        json={"trios": [
            {"name": "kimi", "agent": "claude_code", "model": "opus"},
            {"name": "glm", "agent": "claude_code", "model": "nope"},
        ]},
        headers=_h(write=True),
    )
    assert r.status_code == 422, r.text
    assert {t["name"] for t in rt.store.queue_trios()} == {"glm"}
    assert rt.store.audit() == before_audit


@pytest.mark.parametrize(
    "row,why",
    [
        ({"name": "glm", "agent": "claude_code", "model": "opus", "bogus": 1}, "unknown field(s): bogus"),
        ({"agent": "claude_code"}, "trio name must be a non-empty string"),
        ({"name": "glm", "agent": "x-agent"}, "unknown agent 'x-agent'"),
        ({"name": "glm", "agent": "claude_code", "enabled": "yes"}, "enabled must be true or false"),
        ("glm", "each trio row must be an object"),
    ],
)
def test_put_trio_rejects_bad_rows(tmp_path: Path, monkeypatch: Any, row: Any, why: str) -> None:
    client, rt = _seeded_client(tmp_path)
    monkeypatch.setattr(lh_model_catalog, "discover_model_catalog", _fake_model_catalog)
    before_audit = rt.store.audit()
    r = client.put("/api/queue/trios", json={"trios": [row]}, headers=_h(write=True))
    assert r.status_code in (400, 422), r.text
    assert why in r.json()["detail"]
    assert {t["name"]: t for t in rt.store.queue_trios()}["glm"]["model"] == "opus"
    assert rt.store.audit() == before_audit
    assert client.put("/api/queue/trios", json={"trios": [], "extra": 1}, headers=_h(write=True)).status_code == 400
    assert client.put("/api/queue/trios", json={"trios": [{}]}, headers=_h(write=True)).status_code == 422


def test_put_trio_refuses_when_the_live_model_list_is_unavailable(tmp_path: Path, monkeypatch: Any) -> None:
    client, rt = _seeded_client(tmp_path)

    def boom(**kwargs: Any) -> None:
        raise RuntimeError("gateway down")

    monkeypatch.setattr(lh_model_catalog, "discover_model_catalog", boom)
    before = rt.store.queue_trios()
    r = client.put(
        "/api/queue/trios",
        json={"trios": [{"name": "glm", "agent": "claude_code", "model": "sonnet"}]},
        headers=_h(write=True),
    )
    assert r.status_code == 503, r.text
    assert rt.store.queue_trios() == before


def test_put_capacity_updates_and_audits(tmp_path: Path) -> None:
    client, rt = _seeded_client(tmp_path)
    r = client.put("/api/queue/capacity", json={"capacity": {"glm": 5}}, headers=_h(write=True))
    assert r.status_code == 200, r.text
    assert r.json()["capacity"] == {"glm": 5} and rt.store.queue_capacity() == {"glm": 5}
    top = rt.store.audit()[0]
    assert top["action"] == "set" and top["name"] == "queue_capacity:glm" and top["actor"] == ADMIN
    assert top["detail"] == {"after": {"max": 5, "max_peak": None}, "before": {"max": 2, "max_peak": None}}


@pytest.mark.parametrize("payload", [{"zzz": 1}, {"glm": -1}, {"glm": 3.5}, {"glm": True}])
def test_put_capacity_refuses_unknown_trio_and_bad_values(tmp_path: Path, payload: dict[str, Any]) -> None:
    client, rt = _seeded_client(tmp_path)
    before_audit = rt.store.audit()
    r = client.put("/api/queue/capacity", json={"capacity": payload}, headers=_h(write=True))
    assert r.status_code == 422, r.text
    assert rt.store.queue_capacity() == {"glm": 2} and rt.store.audit() == before_audit
    assert client.put("/api/queue/capacity", json={"capacity": "glm"}, headers=_h(write=True)).status_code == 422


@pytest.mark.parametrize(
    "headers,code",
    [
        (lambda: _h(identity="<EMAIL_ADDRESS_G2LRS_10>", write=True), 403),  # signed in, not an admin
        (lambda: _h(proxy="forged", write=True), 403),
        (lambda: _h(proxy=None, write=True), 403),
        (lambda: _h(identity=None, write=True), 403),
        (lambda: _h(write=False), 403),  # no X-Requested-With on a write
        (lambda: _h(bearer=None, write=True), 401),
    ],
)
def test_only_admins_can_change_queue_trios_and_capacity(tmp_path: Path, headers: Any, code: int) -> None:
    client, rt = _seeded_client(tmp_path)
    before_trios = rt.store.queue_trios()
    before_capacity = rt.store.queue_capacity()
    before_audit = rt.store.audit()
    r = client.put(
        "/api/queue/trios",
        json={"trios": [{"name": "glm", "agent": "claude_code", "model": "sonnet"}]},
        headers=headers(),
    )
    assert r.status_code == code, r.text
    r = client.put("/api/queue/capacity", json={"capacity": {"glm": 9}}, headers=headers())
    assert r.status_code == code, r.text
    assert rt.store.queue_trios() == before_trios and rt.store.queue_capacity() == before_capacity
    assert rt.store.audit() == before_audit


def test_queue_reads_are_admin_only_too(tmp_path: Path) -> None:
    client, _ = _seeded_client(tmp_path)
    for path in ("/api/queue/trios", "/api/queue/capacity"):
        assert client.get(path, headers=_h(identity="<EMAIL_ADDRESS_G2LRS_11>")).status_code == 403


def test_queue_endpoints_answer_503_without_a_store(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, store=False)
    assert client.get("/api/queue/trios", headers=_h()).status_code == 503
    assert client.put("/api/queue/capacity", json={"capacity": {}}, headers=_h(write=True)).status_code == 503


def test_models_refresh_re_fetches_the_live_list_now(tmp_path: Path, monkeypatch: Any) -> None:
    calls: list[bool] = []

    def fake(*args: Any, force: bool = False, **kwargs: Any) -> dict[str, Any]:
        calls.append(force)
        return json.loads(json.dumps(_FAKE_CATALOG))

    monkeypatch.setattr(lh_server, "discover_model_catalog", fake)
    client, _ = _client(tmp_path)
    r = client.post("/api/models/refresh", json={}, headers=_h())
    assert r.status_code == 200, r.text
    assert calls == [True]


# ----------------------------------------- LHH-QUEUE-PROTECT-DOCS F3 --
# Paxton's standing rule: the kimi (synthetic), glm (cloud) and qwen (QA)
# trios are never modified at runtime.  PUT /api/queue/trios refuses any
# write to a row whose ``name`` is on the project config's
# ``[queue].protected_trios`` list with HTTP 409 (and a clear message
# naming the trio and the rule), unless the request carries
# ``force: true`` AND the caller is a verified SSO admin (the audit row
# then carries ``protected_override: true`` and the response names the
# overridden trios).  Capacity edits on protected trios stay allowed --
# caps are operational, not modelling choices.  The default list is
# ``["kimi", "glm", "qwen"]``; an empty list disables the rule.

def _protected_client(tmp_path: Path, *, protected_trios=("kimi", "glm", "qwen")):
    client, rt = _client_with_protected(tmp_path, protected_trios=protected_trios)
    seeded = rt.store.seed_queue_defaults(
        {
            "glm": {"agent": "claude_code", "model": "opus", "mcp_profile": "default"},
            "zai": {"agent": "claude_code", "model": "opus", "mcp_profile": "default"},
            "kimi": {"agent": "claude_code", "model": "opus", "mcp_profile": "default"},
        },
        {"glm_max": 2, "zai_max": 1, "kimi_max": 1},
    )
    assert seeded == {"trios": ["glm", "zai", "kimi"], "capacity": ["glm", "zai", "kimi"]}
    return client, rt


def test_put_trio_on_protected_kimi_without_force_refuses_409(tmp_path: Path, monkeypatch: Any) -> None:
    client, rt = _protected_client(tmp_path)
    monkeypatch.setattr(lh_model_catalog, "discover_model_catalog", _fake_model_catalog)
    before = rt.store.queue_trios()
    before_audit = rt.store.audit()
    r = client.put(
        "/api/queue/trios",
        json={"trios": [{"name": "kimi", "agent": "claude_code", "model": "sonnet"}]},
        headers=_h(write=True),
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "kimi" in detail and "protected" in detail and "Paxton" in detail
    # All-or-nothing: the row did not change AND no audit row was written.
    assert rt.store.queue_trios() == before
    assert rt.store.audit() == before_audit


def test_put_trio_on_protected_kimi_with_force_from_admin_succeeds(tmp_path: Path, monkeypatch: Any) -> None:
    client, rt = _protected_client(tmp_path)
    monkeypatch.setattr(lh_model_catalog, "discover_model_catalog", _fake_model_catalog)
    r = client.put(
        "/api/queue/trios",
        json={
            "trios": [{"name": "kimi", "agent": "claude_code", "model": "sonnet"}],
            "force": True,
        },
        headers=_h(write=True),
    )
    assert r.status_code == 200, r.text
    assert r.json().get("protected_override") == ["kimi"]
    kimi = {t["name"]: t for t in rt.store.queue_trios()}["kimi"]
    assert kimi["model"] == "sonnet" and kimi["updated_by"] == ADMIN
    top = rt.store.audit()[0]
    assert top["action"] == "set" and top["name"] == "queue_trio:kimi" and top["actor"] == ADMIN
    assert top["detail"].get("protected_override") is True


def test_put_trio_on_protected_kimi_with_force_from_non_admin_refuses(tmp_path: Path, monkeypatch: Any) -> None:
    # A non-admin caller (signed in, but not on the SSO admin list) cannot
    # bypass the rule even with ``force: true`` -- they hit 403 at the
    # ``require_admin`` gate before the protected check runs.
    client, rt = _protected_client(tmp_path)
    monkeypatch.setattr(lh_model_catalog, "discover_model_catalog", _fake_model_catalog)
    before = rt.store.queue_trios()
    r = client.put(
        "/api/queue/trios",
        json={
            "trios": [{"name": "kimi", "agent": "claude_code", "model": "sonnet"}],
            "force": True,
        },
        headers=_h(identity="<EMAIL_ADDRESS_G2LRS_12>", write=True),
    )
    assert r.status_code == 403, r.text
    assert rt.store.queue_trios() == before


def test_put_trio_on_zai_not_protected_succeeds(tmp_path: Path, monkeypatch: Any) -> None:
    client, rt = _protected_client(tmp_path)
    monkeypatch.setattr(lh_model_catalog, "discover_model_catalog", _fake_model_catalog)
    r = client.put(
        "/api/queue/trios",
        json={"trios": [{"name": "zai", "agent": "claude_code", "model": "sonnet"}]},
        headers=_h(write=True),
    )
    assert r.status_code == 200, r.text
    # ``zai`` is not on the default protected list, so no override marker
    # is added and the audit row reflects a normal write.
    assert "protected_override" not in r.json()
    zai = {t["name"]: t for t in rt.store.queue_trios()}["zai"]
    assert zai["model"] == "sonnet"
    top = rt.store.audit()[0]
    assert top["name"] == "queue_trio:zai"
    assert "protected_override" not in top["detail"]


def test_put_capacity_on_protected_kimi_stays_allowed(tmp_path: Path) -> None:
    # Capacity edits bypass the protected rule: caps are operational, so a
    # pause-on-kimi or a kimi_max lift has to go through.  We confirm the
    # rule is OFF on /api/queue/capacity by sending a kimi-only capacity
    # edit and watching it land with an audit row.
    client, rt = _protected_client(tmp_path)
    r = client.put(
        "/api/queue/capacity",
        json={"capacity": {"kimi": 4}},
        headers=_h(write=True),
    )
    assert r.status_code == 200, r.text
    assert r.json()["capacity"]["kimi"] == 4
    top = rt.store.audit()[0]
    assert top["action"] == "set" and top["name"] == "queue_capacity:kimi" and top["actor"] == ADMIN


def test_put_trio_empty_protected_list_disables_the_rule(tmp_path: Path, monkeypatch: Any) -> None:
    # An empty ``[queue].protected_trios`` list disables the rule entirely
    # -- the same way a missing project config does.  This is how a node
    # opts out without renaming the legacy trios.
    client, rt = _protected_client(tmp_path, protected_trios=())
    monkeypatch.setattr(lh_model_catalog, "discover_model_catalog", _fake_model_catalog)
    r = client.put(
        "/api/queue/trios",
        json={"trios": [{"name": "kimi", "agent": "claude_code", "model": "sonnet"}]},
        headers=_h(write=True),
    )
    assert r.status_code == 200, r.text
    kimi = {t["name"]: t for t in rt.store.queue_trios()}["kimi"]
    assert kimi["model"] == "sonnet"
    assert r.json().get("protected_override") is None


def test_put_trio_with_force_and_no_protected_rows_returns_no_override_marker(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, rt = _protected_client(tmp_path)
    monkeypatch.setattr(lh_model_catalog, "discover_model_catalog", _fake_model_catalog)
    r = client.put(
        "/api/queue/trios",
        json={
            "trios": [{"name": "zai", "agent": "claude_code", "model": "sonnet"}],
            "force": True,
        },
        headers=_h(write=True),
    )
    assert r.status_code == 200, r.text
    assert "protected_override" not in r.json()

