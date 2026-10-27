#!/usr/bin/env python3
"""Hermetic real-HTTP control resilience proof; no inference or production services."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn

from lh_harness.interaction.auth import canonical_body_digest, issue_interaction_assertion
from lh_harness.interaction.local import LocalInteractiveService
from lh_harness.types import EpisodeResult
from lh_harness.webapi.server import create_app

SECRET = "local-resilience-assertion-secret-at-least-32-bytes"
TOKEN = "local-resilience-service-token"
PRINCIPAL = {"sub": "local-resilience-user", "tenant_id": "local-resilience-tenant", "authorization_epoch": 1, "credential_id": "local-resilience-session", "policy": "local-resilience"}
RUN_ID = "interactive-office-resilience"
PLAN = "Next: cli\n\nCurrent Task State:\nbuilding\n\nCurrent Task Contract:\nwrite the report\n\nPlan:\nimplement it"
AUDIT = "Status: complete\nIntegrity: clean\nContract audit: aligned\n\nSummary:\nverified"


class RoleAgent:
    def __init__(self, role: str) -> None:
        self.role = role
        self.entered = threading.Event()
        self.release = threading.Event()

    async def run_episode(self, prompt, env, budget, live_trajectory_path=None):
        if self.role == "executor" and "Answer the canonical customer turn" not in prompt:
            self.entered.set()
            if not await __import__("asyncio").to_thread(self.release.wait, 10):
                raise TimeoutError("hermetic executor release was not supplied")
            (env.workspace / "task_report.py").write_text("def report():\n    return 'TASK REPORT\\n'\n", encoding="utf-8")
        text = {"manager": PLAN, "executor": "implemented", "auditor": AUDIT, "final_response": "Review the result and choose DONE, changes, or hold."}[self.role]
        return EpisodeResult(status="done", actions_log=text, metadata={"usage": {"input_tokens": 4, "output_tokens": 6}})


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(base: str, method: str, path: str, body: dict | None = None, action: str = "read", command_id: str | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    if method == "POST":
        headers["Idempotency-Key"] = command_id or f"resilience-{action}-{time.time_ns()}"
        headers["X-Interaction-Assertion"] = issue_interaction_assertion(SECRET, {**PRINCIPAL, "run_id": RUN_ID, "action": action, "body_sha256": canonical_body_digest(body or {})})
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read())


def evidence(base: str) -> dict:
    return request(base, "GET", f"/api/interactive/runs/{RUN_ID}/evidence")


def wait_status(base: str, expected: str) -> dict:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        value = evidence(base)
        if value["status"] == expected:
            return value
        if value["status"] == "failed":
            raise AssertionError(value.get("failure_reason"))
        time.sleep(0.05)
    raise TimeoutError(expected)


class HttpProjection:
    def __init__(self, service: LocalInteractiveService, root: Path, port: int) -> None:
        self.service = service
        self.root = root
        self.port = port
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        app = create_app(runs_root=self.root / "api", interactive_service=self.service, auth_token=TOKEN, interaction_assertion_secret=SECRET)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.server.started

    def stop(self) -> None:
        assert self.server is not None and self.thread is not None
        self.server.should_exit = True
        self.thread.join(timeout=10)
        assert not self.thread.is_alive()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="interactive-office-resilience-") as temporary:
        root = Path(temporary)
        source = root / "sources" / "demo"
        source.mkdir(parents=True)
        subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
        (source / "README.md").write_text("local resilience\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.email=poc@example.invalid", "-c", "user.name=PoC", "commit", "-m", "initial"], check=True, capture_output=True)
        agents = {role: RoleAgent(role) for role in ("manager", "executor", "auditor", "final_response")}
        service = LocalInteractiveService(root / "interactive", source_root=source.parent, adapter_factory=agents.__getitem__)
        projection = HttpProjection(service, root, free_port())
        projection.start()
        try:
            created = request(projection.base, "POST", "/api/interactive/runs", {"run_id": RUN_ID, "task": "Build a text task report", "approved_criteria": ["text report"], "source_repo": str(source), "estimate": {"minutes": 5}}, "create")
            assert created["status"] == "awaiting_decision"
            request(projection.base, "POST", f"/api/interactive/runs/{RUN_ID}/estimates/estimate-001/decision", {"decision": "allowed"}, "decide")
            assert agents["executor"].entered.wait(5)
            pause = request(projection.base, "POST", f"/api/interactive/runs/{RUN_ID}/pause", {}, "pause", "hold-once")
            pause_replay = request(projection.base, "POST", f"/api/interactive/runs/{RUN_ID}/pause", {}, "pause", "hold-once")
            assert pause["action_id"] == pause_replay["action_id"] == "hold-once"
            assert pause_replay["idempotent"] is True
            agents["executor"].release.set()
            assert wait_status(projection.base, "paused")["attempt_id"] == "attempt-001"

            projection.stop()
            reloaded = LocalInteractiveService(root / "interactive", source_root=source.parent, adapter_factory=agents.__getitem__)
            assert reloaded.projection(RUN_ID)["status"] == "paused"
            projection.service = reloaded
            projection.start()
            assert evidence(projection.base)["status"] == "paused"
            resume = request(projection.base, "POST", f"/api/interactive/runs/{RUN_ID}/resume", {}, "resume", "resume-once")
            resume_replay = request(projection.base, "POST", f"/api/interactive/runs/{RUN_ID}/resume", {}, "resume", "resume-once")
            assert resume["action_id"] == resume_replay["action_id"] == "resume-once"
            assert resume_replay["idempotent"] is True
            completed = wait_status(projection.base, "completed")
            receipt_commands = [item["command_id"] for item in completed["receipts"]]
            assert receipt_commands.count("hold-once") == 1
            assert receipt_commands.count("resume-once") == 1
            service = reloaded

            for agent in agents.values():
                agent.entered = threading.Event()
                agent.release = threading.Event()
            request(projection.base, "POST", f"/api/interactive/runs/{RUN_ID}/changes", {"request": "Regenerate the covered text report", "criteria": ["text report"]}, "change", "covered-outage")
            assert agents["executor"].entered.wait(5)
            projection.stop()
            try:
                evidence(projection.base)
                raise AssertionError("projection endpoint remained reachable during outage")
            except urllib.error.URLError:
                pass
            livekit_compose = os.environ.get("INTERACTIVE_OFFICE_TEST_LIVEKIT_COMPOSE")
            if livekit_compose:
                subprocess.run([os.environ.get("INTERACTIVE_OFFICE_TEST_DOCKER", "docker"), "compose", "-f", livekit_compose, "stop"], check=True)
                with socket.socket() as sock:
                    sock.settimeout(2)
                    assert sock.connect_ex(("127.0.0.1", 17880)) != 0, "LiveKit remained reachable during outage"
            agents["executor"].release.set()
            assert service.wait_for_idle(RUN_ID)["status"] == "completed"
            projection.start()
            assert evidence(projection.base)["status"] == "completed"
            print(json.dumps({"ok": True, "mode": "hermetic-real-http", "duplicate_hold_and_resume": True, "durable_hold_reconnect": True, "projection_outage_authorized_completion": True, "livekit_outage_authorized_completion": bool(livekit_compose), "livekit_device_proof": "not_claimed"}, indent=2))
        finally:
            if projection.thread is not None and projection.thread.is_alive():
                projection.stop()


if __name__ == "__main__":
    main()
