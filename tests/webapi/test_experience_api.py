"""Read-only /api/experience/* surface: seeded L3, addressable L2, run L1.

Covers the Paxton 2026-09-08 instruction: the three levels are viewable,
redaction applies to every served field, the bearer boundary is the shared
/api/* middleware, and reading never writes to any run dir or workspace.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.dashboard.state import DashboardState
from lh_harness.experience import POLICY_ITEM_SCHEMA, REDACTED
from lh_harness.webapi.server import create_app


# Planted secrets (fixed-width test fixtures, never real credentials). Each
# matches exactly one redaction rule; none may survive to a response body.
_BEARER_TOKEN = "Bearer abcdefghijklmnop"
_SK_TOKEN = "sk-ABCDEFGHIJKLMNOPQRST"
_GHP_TOKEN = "ghp_" + "B" * 36
_PASSWORD = "password=hunter2"
_HEX_32 = "0123456789abcdef" * 2
_ENV_NAME_ASSIGNMENT = "LH_HARNESS_WEB_TOKEN=supersecretvalue"
_ALL_SECRETS = (
    _BEARER_TOKEN,
    _SK_TOKEN,
    _GHP_TOKEN,
    _PASSWORD,
    _HEX_32,
    _ENV_NAME_ASSIGNMENT,
)


def _tree_fingerprint(root: Path) -> dict[str, str]:
    """Content hash of every file below ``root`` (write-detection fixture)."""
    digest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return digest


def _make_run(
    runs_root: Path,
    run_id: str,
    records: list[dict] | None = None,
    *,
    tail: str = "",
    legacy: bool = False,
) -> Path:
    """A minimal boundary-valid run dir with an experience ledger."""
    if legacy:
        role_dir = runs_root / run_id / "logs" / "role_management"
    else:
        role_dir = runs_root / run_id / "lh_harness" / "role_orchestration"
    role_dir.mkdir(parents=True)
    if records is not None or tail:
        lines = [json.dumps(record) for record in records or []]
        body = ("\n".join(lines) + "\n" if lines else "") + tail
        (role_dir / "experience.jsonl").write_text(body, encoding="utf-8")
    return role_dir


def _client(root: Path, *, auth_token: str | None = None) -> TestClient:
    app = create_app(runs_root=root, auth_token=auth_token)
    return TestClient(app)


def test_environment_serves_seeded_l3_with_full_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # chdir keeps the cwd-relative project-config fallback off the developer's
    # (or harness's) real .lh-harness/config.toml: pure built-in seeds here.
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path / "runs")
    response = client.get("/api/experience/environment")
    assert response.status_code == 200
    payload = response.json()
    assert payload["level"] == "L3"
    assert payload["kind"] == "environment"

    items = payload["items"]
    assert payload["count"] == len(items)
    by_kind: dict[str, list[dict]] = {}
    for item in items:
        by_kind.setdefault(item["kind"], []).append(item)
    assert len(by_kind["host"]) == 9
    assert len(by_kind["constraint"]) == 6
    assert len(by_kind["routing_backend"]) == 3

    ids = {item["id"] for item in items}
    assert len(ids) == len(items)  # stable, unique, quotable
    assert "l3.host.ct110" in ids
    assert "l3.constraint.runner-restart-from-outside" in ids
    assert "l3.backend.ollama-cloud" in ids

    for item in items:
        # origin/seeded_at/source/supersede envelope on every item.
        assert item["level"] == "L3"
        assert item["origin"] == "seeded"
        assert item["seeded_at"]
        assert item["source"] in {"code", "config"}
        assert "superseded_by" in item
        assert "superseded_at" in item
        assert item["summary"]

    # Honesty check carried into the API: no invented concurrency cap.
    ollama_cloud = next(i for i in items if i["id"] == "l3.backend.ollama-cloud")
    assert ollama_cloud["detail"]["max_concurrent"] is None


def test_policies_empty_but_addressable_with_final_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path / "runs")
    response = client.get("/api/experience/policies", params={"offset": 0, "limit": 10})
    assert response.status_code == 200
    payload = response.json()
    assert payload["level"] == "L2"
    assert payload["kind"] == "policy"
    assert payload["total"] == 0
    assert payload["items"] == []
    assert payload["offset"] == 0
    assert payload["limit"] == 10
    # The empty collection still advertises its final shape (not a placeholder).
    assert payload["schema"] == POLICY_ITEM_SCHEMA
    assert "origin" in payload["schema"]
    assert "seeded_at" in payload["schema"]
    assert "detail.device_requirement" in payload["schema"]


def test_traces_paginated_in_ledger_order(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    records = [
        {"run_id": "run-1", "round_index": index, "value": 0.1 * index}
        for index in (1, 2, 3)
    ]
    _make_run(runs_root, "run-1", records)
    client = _client(runs_root)

    response = client.get(
        "/api/experience/runs/run-1/traces", params={"offset": 1, "limit": 1}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["level"] == "L1"
    assert payload["kind"] == "trace"
    assert payload["run_id"] == "run-1"
    assert payload["captured"] is True
    assert payload["ledger"] == "role_orchestration/experience.jsonl"
    assert payload["total"] == 3
    assert payload["offset"] == 1
    assert payload["limit"] == 1
    assert len(payload["items"]) == 1
    assert payload["items"][0]["round_index"] == 2

    full = client.get("/api/experience/runs/run-1/traces").json()
    assert [item["round_index"] for item in full["items"]] == [1, 2, 3]


def test_traces_redacted_at_serve_even_for_foreign_ledgers(tmp_path: Path) -> None:
    """A ledger written by another tool (no capture-time redaction) is still
    served with every redaction applied — the serve boundary does the strip."""
    runs_root = tmp_path / "runs"
    records = [
        {
            "run_id": "run-1",
            "round_index": 1,
            "state_summary": f"context {_BEARER_TOKEN} and {_SK_TOKEN}",
            "action": f"echo {_PASSWORD}; export {_ENV_NAME_ASSIGNMENT}",
            "observation": f"raw {_GHP_TOKEN} plus {_HEX_32} tail",
            "value": 0.5,
        }
    ]
    _make_run(runs_root, "run-1", records)
    client = _client(runs_root)
    response = client.get("/api/experience/runs/run-1/traces")
    assert response.status_code == 200
    body = response.text
    for secret in (
        "abcdefghijklmnop",
        _SK_TOKEN,
        "hunter2",
        "supersecretvalue",
        _GHP_TOKEN,
        _HEX_32,
    ):
        assert secret not in body
    assert REDACTED in body
    # The env-var NAME is not secret and survives; only its value was masked.
    item = response.json()["items"][0]
    assert "LH_HARNESS_WEB_TOKEN" in item["action"]


def test_traces_serve_device_fields_verbatim(tmp_path: Path) -> None:
    """Device identifiers pass through unchanged: identical strings, never
    re-keyed or truncated, so a trace joins to Hydra and the fleet surfaces."""
    runs_root = tmp_path / "runs"
    device = {
        "device_id": "ptait-desk03:head-7",
        "terminal_id": "term-9f31aa06",
        "hydra_node": "hydra-01",
    }
    records = [{"run_id": "run-1", "round_index": 1, **device}]
    _make_run(runs_root, "run-1", records)
    client = _client(runs_root)
    item = client.get("/api/experience/runs/run-1/traces").json()["items"][0]
    for name, value in device.items():
        assert item[name] == value


def test_traces_missing_ledger_and_malformed_tail(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    # Run with no ledger at all (experience capture was off).
    _make_run(runs_root, "run-empty")
    client = _client(runs_root)
    payload = client.get("/api/experience/runs/run-empty/traces").json()
    assert payload["captured"] is False
    assert payload["total"] == 0
    assert payload["items"] == []

    # A finalization cut mid-line leaves a truncated tail; it is skipped.
    _make_run(
        runs_root,
        "run-torn",
        [{"run_id": "run-torn", "round_index": 1}],
        tail='{"run_id": "run-torn", "round_in',
    )
    torn = client.get("/api/experience/runs/run-torn/traces").json()
    assert torn["captured"] is True
    assert torn["total"] == 1
    assert torn["items"][0]["round_index"] == 1


def test_traces_legacy_layout_and_unknown_run(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    _make_run(
        runs_root,
        "run-legacy",
        [{"run_id": "run-legacy", "round_index": 1}],
        legacy=True,
    )
    client = _client(runs_root)
    payload = client.get("/api/experience/runs/run-legacy/traces").json()
    assert payload["total"] == 1
    assert payload["ledger"] == "role_management/experience.jsonl"

    unknown = client.get("/api/experience/runs/no-such-run/traces")
    assert unknown.status_code == 404
    too_long = client.get(f"/api/experience/runs/{'x' * 200}/traces")
    assert too_long.status_code == 404


def test_bearer_boundary_is_the_shared_api_middleware(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    _make_run(runs_root, "run-1", [{"run_id": "run-1", "round_index": 1}])
    client = _client(runs_root, auth_token="secret")
    for path in (
        "/api/experience/environment",
        "/api/experience/policies",
        "/api/experience/runs/run-1/traces",
    ):
        denied = client.get(path)
        assert denied.status_code == 401, path
        allowed = client.get(path, headers={"Authorization": "Bearer secret"})
        assert allowed.status_code == 200, path


def test_reads_write_nothing(tmp_path: Path) -> None:
    """Serving any of the three endpoints never writes: the run dir tree (the
    only writable-looking surface these routes touch) is byte-identical."""
    runs_root = tmp_path / "runs"
    _make_run(
        runs_root,
        "run-1",
        [{"run_id": "run-1", "round_index": index} for index in (1, 2)],
    )
    client = _client(runs_root, auth_token="secret")
    before = _tree_fingerprint(tmp_path)
    headers = {"Authorization": "Bearer secret"}
    for path in (
        "/api/experience/environment",
        "/api/experience/policies",
        "/api/experience/policies?offset=0&limit=1",
        "/api/experience/runs/run-1/traces",
        "/api/experience/runs/run-1/traces?offset=1&limit=1",
    ):
        assert client.get(path, headers=headers).status_code == 200, path
    assert _tree_fingerprint(tmp_path) == before


def test_embedded_single_run_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No runs root (an attached/single-run server): L3/L2 still serve, and a
    run the registry was bound to serves its ledger from the state's log dir."""
    monkeypatch.chdir(tmp_path)
    role_dir = tmp_path / "run-x" / "lh_harness" / "role_orchestration"
    role_dir.mkdir(parents=True)
    (role_dir / "experience.jsonl").write_text(
        json.dumps({"run_id": "run-x", "round_index": 1}) + "\n", encoding="utf-8"
    )
    app = create_app(
        state=DashboardState(tmp_path / "run-x" / "lh_harness"),
        run_id="run-x",
    )
    client = TestClient(app)
    assert client.get("/api/experience/environment").status_code == 200
    assert client.get("/api/experience/policies").status_code == 200
    traces = client.get("/api/experience/runs/run-x/traces")
    assert traces.status_code == 200
    assert traces.json()["total"] == 1
    # A foreign run id has no registry entry.
    assert client.get("/api/experience/runs/other/traces").status_code == 404
