#!/usr/bin/env python3
"""Hermetic real-HTTP Interactive Office control proof; never contacts production."""
from __future__ import annotations

import json
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn

from lh_harness.interaction.auth import issue_interaction_assertion
from lh_harness.interaction.local import LocalInteractiveService
from lh_harness.types import EpisodeResult
from lh_harness.webapi.server import create_app

SECRET = "local-e2e-assertion-secret-at-least-32-bytes"
TOKEN = "local-e2e-service-token"
PRINCIPAL = {"sub": "local-e2e-user", "tenant_id": "local-e2e-tenant", "authorization_epoch": 1, "credential_id": "local-e2e-session", "policy": "local-e2e"}
PLAN = "Next: cli\n\nCurrent Task State:\nbuilding\n\nCurrent Task Contract:\nwrite the report\n\nPlan:\nimplement it"
AUDIT = "Status: complete\nIntegrity: clean\nContract audit: aligned\n\nSummary:\nverified"


class RoleAgent:
    def __init__(self, role: str) -> None:
        self.role = role

    async def run_episode(self, prompt, env, budget, live_trajectory_path=None):
        if "Answer the canonical customer turn" in prompt:
            text = f"{self.role} grounded reply"
        else:
            text = {"manager": PLAN, "executor": "implemented", "auditor": AUDIT, "final_response": "Review the result and choose DONE, changes, or hold."}[self.role]
        if self.role == "executor" and "Answer the canonical customer turn" not in prompt:
            (env.workspace / "task_report.py").write_text("def report():\n    return 'TASK REPORT\\n'\n", encoding="utf-8")
        return EpisodeResult(status="done", actions_log=text, metadata={"usage": {"input_tokens": 4, "output_tokens": 6}})


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(base: str, method: str, path: str, body: dict | None = None, action: str = "read", role: str | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    if method == "POST":
        headers["Idempotency-Key"] = f"e2e-{action}-{time.time_ns()}"
        headers["X-Interaction-Assertion"] = issue_interaction_assertion(SECRET, {**PRINCIPAL, "run_id": "interactive-office-e2e", "action": action, "body_sha256": __import__("lh_harness.interaction.auth", fromlist=["canonical_body_digest"]).canonical_body_digest(body or {}), **({"role": role} if role else {})})
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read())


def wait_status(base: str, expected: set[str], attempt: int | None = None) -> dict:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        value = request(base, "GET", "/api/interactive/runs/interactive-office-e2e/evidence")
        if value["status"] in expected and (attempt is None or value["attempt_id"] == f"attempt-{attempt:03d}"):
            return value
        if value["status"] == "failed":
            raise AssertionError(value.get("failure_reason"))
        time.sleep(0.05)
    raise TimeoutError(expected)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="interactive-office-e2e-") as temporary:
        root = Path(temporary)
        source = root / "sources" / "demo"
        source.mkdir(parents=True)
        subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
        (source / "README.md").write_text("local e2e\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.email=poc@example.invalid", "-c", "user.name=PoC", "commit", "-m", "initial"], check=True, capture_output=True)
        agents = {role: RoleAgent(role) for role in ("manager", "executor", "auditor", "final_response")}
        service = LocalInteractiveService(root / "interactive", source_root=source.parent, adapter_factory=agents.__getitem__)
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        config = uvicorn.Config(create_app(runs_root=root / "api", interactive_service=service, auth_token=TOKEN, interaction_assertion_secret=SECRET), host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        try:
            task = "Build a plain-text task report with unit tests"
            created = request(base, "POST", "/api/interactive/runs", {"run_id": "interactive-office-e2e", "task": task, "approved_criteria": ["text report", "unit tests"], "source_repo": str(source), "estimate": {"minutes": 5}}, "create")
            assert created["status"] == "awaiting_decision"
            request(base, "POST", "/api/interactive/runs/interactive-office-e2e/estimates/estimate-001/decision", {"decision": "allowed"}, "decide")
            first = wait_status(base, {"completed"})
            assert first["customer"] == {"done": False, "stars": None}
            assert any(event["event"] == "customer_review_requested" for event in first["events"])

            covered = request(base, "POST", "/api/interactive/runs/interactive-office-e2e/changes", {"request": "Sort the text report", "criteria": ["text report"]}, "change")
            assert covered["classification"] == "IN_SCOPE_ADJUSTMENT"
            assert covered["estimate_usage"] == covered["replan_usage"] == covered["bmad_usage"] == 0
            adjusted = wait_status(base, {"completed"})
            assert adjusted["attempt_id"] == first["attempt_id"] and len(adjusted["estimates"]) == 1

            revised = request(base, "POST", "/api/interactive/runs/interactive-office-e2e/changes", {"request": "Add CSV export", "criteria": ["CSV export"], "selected_stage": "executor.build", "estimate": {"minutes": 8}}, "change")
            assert revised["classification"] == "OUT_OF_SCOPE"
            request(base, "POST", f"/api/interactive/runs/interactive-office-e2e/estimates/{revised['estimate_id']}/decision", {"decision": "allowed"}, "decide")
            second = wait_status(base, {"completed"}, attempt=2)
            assert second["restarts"][0]["estimate_id"] == revised["estimate_id"]
            assert second["attempts"][0]["status"] == "superseded"

            request(base, "POST", "/api/interactive/runs/interactive-office-e2e/customer/rating", {"stars": 5}, "customer_rating")
            request(base, "POST", "/api/interactive/runs/interactive-office-e2e/customer/done", {"done": True}, "customer_done")
            final = request(base, "GET", "/api/interactive/runs/interactive-office-e2e/evidence")
            assert final["customer"] == {"done": True, "stars": 5}
            assert "task_report.py" in final["workspace"]["files"]
            assert final["usage"]["roles"]["manager"]["input_tokens"] > 0
            print(json.dumps({"ok": True, "mode": "hermetic-real-http", "attempts": 2, "customer": final["customer"], "stages": [item["id"] for item in request(base, "GET", "/api/interactive/stages")["stages"]]}, indent=2))
        finally:
            server.should_exit = True
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
