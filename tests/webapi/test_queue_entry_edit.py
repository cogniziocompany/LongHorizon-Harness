"""Pending-task override edit: ``PUT /api/queue/{queue_id}/model`` (R7).

LHH-QUEUE-HOTRELOAD (Paxton 2026-10-11): a queued task's model (and its
trio/roles) stays editable while the entry is pending; at launch the resolved
model is written onto the run and locked, so the edit is refused with 409
once the entry has launched (or is terminal).  The model is validated
against the backend's LIVE model list (unknown -> 422, provider down ->
503), a roles string is the legacy trio alias, a roles object is the
create-run-shaped per-role configuration, and every accepted change writes
one before/after settings-audit row keyed to the admin identity.  Every
test mocks the live model list -- no network, no provider probes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")
from fastapi.testclient import TestClient

import lh_harness.model_catalog as lh_model_catalog
from lh_harness.queue import QueueStore
from lh_harness.settings_store import SettingsRuntime, SettingsStore
from lh_harness.webapi.server import create_app

TOKEN = "bearer-" + "b" * 32
PROXY = "proxy-" + "p" * 32
ADMIN = "<EMAIL_ADDRESS_3YO3S_4>"

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


def _broken_fetch(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("provider unreachable")


def _client(tmp_path: Path):
    store = SettingsStore(tmp_path / "settings.db", "k" * 40)
    runtime = SettingsRuntime(store=store, admins=(ADMIN.lower(),), proxy_auth=PROXY, error=None)
    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    app = create_app(
        runs_root=runs,
        auth_token=TOKEN,
        bind_host="testserver",
        probe_open_pr=None,
        settings_runtime=runtime,
    )
    return TestClient(app), runtime


def _h(identity: str | None = ADMIN, proxy: str | None = PROXY, write: bool = True, bearer: str | None = TOKEN) -> dict[str, str]:
    headers: dict[str, str] = {}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if proxy:
        headers["X-LH-Proxy-Auth"] = proxy
    if identity:
        headers["X-Auth-Request-Email"] = identity
    if write:
        headers["X-Requested-With"] = "lh-harness"
    return headers


def _enqueue(client: TestClient, **overrides: Any) -> str:
    """One plain pending entry through the pre-R6 enqueue path (no overrides)."""
    body = {"name": "n", "task": "t", "workspace": "w", "requested_by": "api-test", **overrides}
    response = client.post("/api/queue", json=body, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200, response.text
    return response.json()["queue_id"]


def _store(tmp_path: Path) -> QueueStore:
    return QueueStore(tmp_path / "runs")


@pytest.fixture(autouse=True)
def _live_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lh_model_catalog, "discover_model_catalog", _fake_model_catalog)


def test_edit_pending_model_persists_and_is_visible(tmp_path: Path) -> None:
    client, rt = _client(tmp_path)
    queue_id = _enqueue(client)
    response = client.put(f"/api/queue/{queue_id}/model", json={"model": "sonnet"}, headers=_h())
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True and payload["changed"] is True and payload["model"] == "sonnet"
    # Visible through the store the launcher re-reads every pass ...
    entry = _store(tmp_path).get(queue_id)
    assert entry is not None and entry.model == "sonnet" and entry.status == "pending"
    # ... and through the queue API state a caller pages.
    got = client.get(f"/api/queue/{queue_id}", headers={"Authorization": f"Bearer {TOKEN}"})
    assert got.status_code == 200 and got.json()["model"] == "sonnet"


def test_edit_unknown_model_422_changes_nothing(tmp_path: Path) -> None:
    client, rt = _client(tmp_path)
    queue_id = _enqueue(client)
    before_audit = rt.store.audit()
    response = client.put(f"/api/queue/{queue_id}/model", json={"model": "not-a-real-model"}, headers=_h())
    assert response.status_code == 422, response.text
    assert "unknown model" in response.json()["detail"] and "not-a-real-model" in response.json()["detail"]
    entry = _store(tmp_path).get(queue_id)
    assert entry is not None and entry.model is None and entry.roles is None
    assert rt.store.audit() == before_audit


def test_edit_provider_down_503_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rt = _client(tmp_path)
    queue_id = _enqueue(client)
    monkeypatch.setattr(lh_model_catalog, "discover_model_catalog", _broken_fetch)
    response = client.put(f"/api/queue/{queue_id}/model", json={"model": "sonnet"}, headers=_h())
    assert response.status_code == 503, response.text
    assert "live model list is unavailable" in response.json()["detail"]
    entry = _store(tmp_path).get(queue_id)
    assert entry is not None and entry.model is None
    assert rt.store.audit() == []


def test_edit_after_launch_refused_409_and_run_unchanged(tmp_path: Path) -> None:
    client, rt = _client(tmp_path)
    queue_id = _enqueue(client)
    store = _store(tmp_path)
    launched = store.mark_launched(queue_id, "run-1")
    assert launched is not None and launched.status == "launched"
    response = client.put(f"/api/queue/{queue_id}/model", json={"model": "sonnet"}, headers=_h())
    assert response.status_code == 409, response.text
    assert "launched" in response.json()["detail"]
    entry = store.get(queue_id)
    assert entry is not None
    assert entry.status == "launched" and entry.run_id == "run-1" and entry.model is None
    assert rt.store.audit() == []


def test_edit_terminal_entry_refused_409(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    queue_id = _enqueue(client)
    store = _store(tmp_path)
    launched = store.mark_launched(queue_id, "run-2")
    assert launched is not None
    assert store.mark_done(queue_id) is not None
    response = client.put(f"/api/queue/{queue_id}/model", json={"roles": "kimi"}, headers=_h())
    assert response.status_code == 409, response.text
    entry = store.get(queue_id)
    assert entry is not None and entry.status == "done" and entry.trio == ""


def test_edit_unknown_and_malformed_ids_404(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert client.put("/api/queue/q-deadbeef/model", json={"model": "sonnet"}, headers=_h()).status_code == 404
    assert client.put("/api/queue/not-an-id/model", json={"model": "sonnet"}, headers=_h()).status_code == 404


def test_roles_string_edits_the_trio_while_pending(tmp_path: Path) -> None:
    client, rt = _client(tmp_path)
    queue_id = _enqueue(client, trio="glm")
    # Set a model override first so the trio-only edit below also proves a
    # partial edit leaves the other override untouched.
    first = client.put(f"/api/queue/{queue_id}/model", json={"model": "opus"}, headers=_h())
    assert first.status_code == 200, first.text
    response = client.put(f"/api/queue/{queue_id}/model", json={"roles": "kimi"}, headers=_h())
    assert response.status_code == 200, response.text
    assert response.json()["trio"] == "kimi"
    entry = _store(tmp_path).get(queue_id)
    assert entry is not None and entry.trio == "kimi" and entry.model == "opus" and entry.roles is None
    audit = rt.store.audit()
    assert audit[0]["detail"]["before"] == {"trio": "glm"} and audit[0]["detail"]["after"] == {"trio": "kimi"}


def test_roles_dict_edits_the_per_role_configuration(tmp_path: Path) -> None:
    client, rt = _client(tmp_path)
    queue_id = _enqueue(client)
    response = client.put(
        f"/api/queue/{queue_id}/model",
        json={"roles": {"executor": {"model": "codex-mini"}}},
        headers=_h(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["roles"] == {"executor": {"model": "codex-mini"}}
    entry = _store(tmp_path).get(queue_id)
    assert entry is not None and entry.roles == {"executor": {"model": "codex-mini"}}
    # Clearing the configuration is the same endpoint with null.
    cleared = client.put(f"/api/queue/{queue_id}/model", json={"roles": None}, headers=_h())
    assert cleared.status_code == 200, cleared.text
    entry = _store(tmp_path).get(queue_id)
    assert entry is not None and entry.roles is None


def test_roles_dict_malformed_422_and_unknown_trio_string_422(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    queue_id = _enqueue(client)
    bad_roles = client.put(f"/api/queue/{queue_id}/model", json={"roles": {"boss": {}}}, headers=_h())
    assert bad_roles.status_code == 422 and "unknown role configuration: boss" in bad_roles.json()["detail"]
    bad_trio = client.put(f"/api/queue/{queue_id}/model", json={"roles": "nope"}, headers=_h())
    assert bad_trio.status_code == 422 and "trio must be one of" in bad_trio.json()["detail"]
    entry = _store(tmp_path).get(queue_id)
    assert entry is not None and entry.trio == "" and entry.roles is None


def test_model_null_clears_the_override(tmp_path: Path) -> None:
    client, rt = _client(tmp_path)
    queue_id = _enqueue(client, model=None)
    # Direct store write seeds an override the edit then clears.
    store = _store(tmp_path)
    entry = store.get(queue_id)
    assert entry is not None
    entry.model = "opus"
    store.update(entry)
    response = client.put(f"/api/queue/{queue_id}/model", json={"model": None}, headers=_h())
    assert response.status_code == 200, response.text
    assert response.json()["model"] is None
    entry = store.get(queue_id)
    assert entry is not None and entry.model is None
    audit = rt.store.audit()
    assert audit[0]["detail"]["before"] == {"model": "opus"} and audit[0]["detail"]["after"] == {"model": None}


def test_audit_row_keyed_to_admin_identity_per_change(tmp_path: Path) -> None:
    client, rt = _client(tmp_path)
    queue_id = _enqueue(client)
    first = client.put(f"/api/queue/{queue_id}/model", json={"model": "opus"}, headers=_h())
    assert first.status_code == 200, first.text
    second = client.put(f"/api/queue/{queue_id}/model", json={"model": "sonnet"}, headers=_h())
    assert second.status_code == 200, second.text
    audit = rt.store.audit()
    assert len(audit) == 2
    top = audit[0]
    assert top["action"] == "edit" and top["name"] == f"queue_entry:{queue_id}" and top["actor"] == ADMIN
    assert top["detail"]["before"] == {"model": "opus"} and top["detail"]["after"] == {"model": "sonnet"}


def test_no_op_edit_writes_no_audit_row(tmp_path: Path) -> None:
    client, rt = _client(tmp_path)
    queue_id = _enqueue(client)
    first = client.put(f"/api/queue/{queue_id}/model", json={"model": "opus"}, headers=_h())
    assert first.status_code == 200, first.text
    again = client.put(f"/api/queue/{queue_id}/model", json={"model": "opus"}, headers=_h())
    assert again.status_code == 200, again.text
    assert again.json()["changed"] is False
    assert len(rt.store.audit()) == 1


def test_body_validation(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    queue_id = _enqueue(client)
    assert client.put(f"/api/queue/{queue_id}/model", json={}, headers=_h()).status_code == 422
    assert client.put(f"/api/queue/{queue_id}/model", json={"shade": 1}, headers=_h()).status_code == 400
    wrong_type = client.put(f"/api/queue/{queue_id}/model", json={"model": 7}, headers=_h())
    assert wrong_type.status_code == 422
    wrong_roles = client.put(f"/api/queue/{queue_id}/model", json={"roles": ["glm"]}, headers=_h())
    assert wrong_roles.status_code == 422


def test_only_admins_can_edit_pending_overrides(tmp_path: Path) -> None:
    client, rt = _client(tmp_path)
    queue_id = _enqueue(client)
    cases = [
        (_h(identity="<EMAIL_ADDRESS_3YO3S_2>"), 403),  # signed in, not an admin
        (_h(proxy="forged"), 403),  # not via the SSO edge
        (_h(proxy=None), 403),
        (_h(identity=None), 403),
        (_h(write=False), 403),  # no X-Requested-With on a write
    ]
    for headers, code in cases:
        response = client.put(f"/api/queue/{queue_id}/model", json={"model": "sonnet"}, headers=headers)
        assert response.status_code == code, response.text
    entry = _store(tmp_path).get(queue_id)
    assert entry is not None and entry.model is None
    assert rt.store.audit() == []


def test_legacy_enqueue_without_overrides_unaffected(tmp_path: Path) -> None:
    client, rt = _client(tmp_path)
    queue_id = _enqueue(client, trio="glm")
    entry = _store(tmp_path).get(queue_id)
    assert entry is not None
    assert entry.model is None and entry.roles is None and entry.trio == "glm" and entry.status == "pending"
    # A legacy enqueue takes none of the override paths: no fetch (the
    # autouse fake would answer, but nothing calls it) and no audit row.
    assert rt.store.audit() == []
    got = client.get(f"/api/queue/{queue_id}", headers={"Authorization": f"Bearer {TOKEN}"})
    assert got.status_code == 200 and got.json()["model"] is None
