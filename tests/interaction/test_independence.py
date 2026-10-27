from __future__ import annotations

from pathlib import Path

from test_local import BlockingExecutor, _PRINCIPAL, _body, _service, agents, repo


def test_interaction_plane_absence_does_not_pause_or_resume_execution(tmp_path: Path, repo: Path, agents) -> None:
    blocking = BlockingExecutor()
    agents["executor"] = blocking
    service = _service(tmp_path, repo, agents)
    service.create_run(_body(repo), principal=_PRINCIPAL)
    service.decide("run-1", "estimate-001", {"decision": "allowed"}, principal=_PRINCIPAL)
    assert blocking.entered.wait(5)
    service.request_pause("run-1", principal=_PRINCIPAL, command_id="durable-hold")
    blocking.release.set()
    deadline = 5
    import time
    started = time.monotonic()
    while service.projection("run-1")["status"] != "paused" and time.monotonic() - started < deadline:
        time.sleep(0.02)
    assert service.projection("run-1")["status"] == "paused"

    reconnected = _service(tmp_path, repo, agents)
    assert reconnected.projection("run-1")["status"] == "paused"
    assert service.request_resume is not None
    service.request_resume("run-1", principal=_PRINCIPAL, command_id="explicit-resume")
    completed = service.wait_for_idle("run-1")
    assert completed["status"] == "completed"

    reconnected_without_plane = _service(tmp_path, repo, agents)
    assert reconnected_without_plane.projection("run-1")["status"] == "completed"
    assert reconnected_without_plane.projection("run-1")["customer"]["done"] is False
