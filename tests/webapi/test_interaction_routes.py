from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.interaction.auth import canonical_body_digest, issue_interaction_assertion
from lh_harness.webapi.server import create_app


class StubInteractiveService:
    def __init__(self) -> None:
        self.pause_calls = []

    def recover(self) -> None:
        pass

    def projection(self, run_id: str):
        return {"run_id": run_id, "status": "paused"}

    def request_pause(self, run_id: str, *, principal, command_id=None):
        self.pause_calls.append((run_id, principal, command_id))
        return {"action_id": command_id, "status": "accepted", "deferred": True}


def _assertion(body: dict, *, action: str = "pause") -> str:
    return issue_interaction_assertion(
        "assertion-secret",
        {
            "sub": "object-id",
            "tenant_id": "tenant",
            "authorization_epoch": 1,
            "credential_id": "credential",
            "policy": "local",
            "run_id": "run-1",
            "action": action,
            "body_sha256": canonical_body_digest(body),
        },
    )


def test_interaction_action_requires_bearer_and_request_bound_assertion() -> None:
    service = StubInteractiveService()
    client = TestClient(create_app(auth_token="service-token", interactive_service=service, interaction_assertion_secret="assertion-secret"))
    body = {}
    url = "/api/interactive/runs/run-1/pause"

    assert client.post(url, json=body).status_code == 401
    assert client.post(url, json=body, headers={"Authorization": "Bearer service-token"}).status_code == 401
    mismatched = client.post(
        url,
        json=body,
        headers={
            "Authorization": "Bearer service-token",
            "X-Interaction-Assertion": _assertion({"different": True}),
        },
    )
    assert mismatched.status_code == 401
    accepted = client.post(
        url,
        json=body,
        headers={
            "Authorization": "Bearer service-token",
            "X-Interaction-Assertion": _assertion(body),
            "Idempotency-Key": "pause-1",
        },
    )
    assert accepted.status_code == 200
    assert service.pause_calls[0][0] == "run-1"
    assert service.pause_calls[0][1]["sub"] == "object-id"
    assert service.pause_calls[0][2] == "pause-1"


def test_interaction_projection_and_manifest_are_additive() -> None:
    client = TestClient(create_app(interactive_service=StubInteractiveService()))
    assert client.get("/api/interactive/runs/run-1").json()["status"] == "paused"
    stages = client.get("/api/interactive/stages").json()["stages"]
    assert [item["id"] for item in stages] == ["manager.plan", "executor.build", "auditor.verify", "final_response"]
