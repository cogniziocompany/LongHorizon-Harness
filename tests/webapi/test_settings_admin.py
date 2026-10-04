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
from lh_harness.settings_store import SettingsRuntime, SettingsStore
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
