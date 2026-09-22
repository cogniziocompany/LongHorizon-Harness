#!/usr/bin/env python3
"""End-to-end retry/requeue case: a 429 on round 1 ends in a successor.

Drives the same hermetic service stack as ``happy_path.py`` (a local Web API,
a throwaway git workspace, a fake ``codex`` agent on PATH) but forces the
synthetic provider (task 47) to answer the round-1 executor with a 429
rate-limit error instead of a completion, which is the transport-class
provider fault the launcher classifies as retryable.

The agent script stands in for the Synthetic LLM provider behind the real
Codex CLI: round 1 of the executor emits a ``turn.failed`` record whose error
message is the provider's ``429 rate limit exceeded`` body, exactly the shape
``codex exec --json`` produces when the provider answers HTTP 429 and the
CLI turns it into a failed turn.

The assertions are the three the task contract requires:
1. the original queue entry ends ``failed`` with ``failure_cause`` recorded
   (a retryable provider_rate_limit cause),
2. a successor entry is ``pending`` with ``retry_of`` pointing at the
   original and ``attempt`` one higher,
3. no queue entry is ever ``done`` while its run is non-terminal.

Everything is hermetic: no Postgres, no live CT110 service, no network.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


FAKE_CODEX_VERSION = "codex-cli 1.0.0"
SERVICE_START_TIMEOUT = 30.0
QUEUE_TIMEOUT = 90.0
TOKEN = "e2e-429-token"

MANAGER_INSTRUCTIONS_TEXT = (
    "You are the LongHorizon-Harness manager agent. Your only responsibilities "
    "are task decomposition and next-step scheduling."
)

RATE_LIMIT_BODY = json.dumps(
    {
        "type": "turn.failed",
        "error": {
            "message": (
                "429 rate limit exceeded: synthetic provider returned HTTP 429 "
                "for model hf:zai-org/GLM-4.7-Flash (syn:small:text)"
            )
        },
    }
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_project_config(root: Path, poll_seconds: float = 1.0) -> None:
    """Write a minimal .lh-harness/config.toml so the launcher uses fast polling.

    ``poll_seconds = 1`` keeps the launcher tick tight; ``max_retries = 2``
    leaves one retry below the cap so the forced-429 requeue is accepted,
    not refused by the ``exceeded max_retries`` guard.
    """
    config_dir = root / ".lh-harness"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(
        f"""\
[queue]
[queue.capacity]
poll_seconds = {poll_seconds}
qwen_max = 1
kimi_max = 1
min_healthy_keys = 0
max_retries = 2
""",
        encoding="utf-8",
    )


def _fake_codex_source(version: str) -> str:
    """A deterministic Codex stand-in whose round-1 executor hits the 429.

    Roles are recognized by the same prompt headers the real roles carry
    (src/lh_harness/prompt_texts.py).  The manager routes round 1 to the CLI
    executor and only ever emits completion after an auditor report; the
    executor's round-1 episode is the forced 429, so the run aborts before
    any auditor is ever scheduled.
    """
    manager_round_1 = (
        "Current task state:\n"
        "verified workspace and fake agent setup\n"
        "\n"
        "Task contract:\n"
        "Repo: LongHorizon-Harness. Branch: main. Deliverables: prove queue "
        "retry on provider 429. Acceptance: original failed, successor pending.\n"
        "\n"
        "Next: cli"
    )
    return f'''#!/usr/bin/env python3
import json
import sys

if "--version" in sys.argv:
    print({version!r})
    sys.exit(0)

prompt = sys.stdin.read()
role = "unknown"
if {MANAGER_INSTRUCTIONS_TEXT!r} in prompt:
    role = "manager"
elif "You are the LongHorizon-Harness CLI executor" in prompt:
    role = "executor"
elif "You are the read-only LongHorizon-Harness CLI auditor" in prompt:
    role = "auditor"
elif "Write the reply to the person who asked for this task" in prompt:
    role = "final_response"

if role == "executor":
    # The synthetic provider answers HTTP 429: the Codex CLI surfaces it as a
    # failed turn and exits nonzero, so the episode carries a hard runtime
    # signal and classifies as provider_rate_limit.  The error object is
    # flat message object with a "message" field, matching how the
    # embedded in a real turn.failed record.
    print(json.dumps({{"type": "thread.started", "thread_id": "synthetic-429"}}))
    print(json.dumps({{"type": "turn.started"}}))
    print({RATE_LIMIT_BODY!r})
    sys.exit(1)

print(json.dumps({{"type": "thread.started", "thread_id": "stub"}}))

if role == "manager":
    if "Current management round: 1" in prompt and "do not emit" not in prompt:
        text = {manager_round_1!r}
    else:
        text = "Next: done"
elif role == "auditor":
    text = (
        "Status: complete\\n"
        "Integrity: clean\\n"
        "Contract audit: aligned\\n"
        "\\n"
        "Audit facts: the fake agent reported completion; no blocking constraints."
    )
elif role == "final_response":
    text = "The requested work is complete."
else:
    text = "done"

print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": text}}}}))
print(json.dumps({{"type": "turn.completed"}}))
'''


def _install_fake_codex(bin_dir: Path) -> None:
    fake = bin_dir / "codex"
    fake.write_text(_fake_codex_source(FAKE_CODEX_VERSION), encoding="utf-8")
    fake.chmod(0o755)


def _api(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    token: str = TOKEN,
    base_url: str = "",
) -> dict[str, Any]:
    url = f"{base_url}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(data))
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(payload)
        except json.JSONDecodeError:
            detail = payload
        raise RuntimeError(f"{method} {path} -> {exc.code}: {detail}") from exc


def _wait_for_service(base_url: str, deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            meta = _api("GET", "/api/meta", base_url=base_url)
            if meta.get("ok", True) and meta.get("capabilities", {}).get("fleet_mcp_tools"):
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError("service did not become ready")


def _all_entries(base_url: str) -> list[dict[str, Any]]:
    return list(_api("GET", "/api/queue", base_url=base_url).get("entries", []))


def _wait_until(condition, deadline: float, description: str, poll: float = 0.3) -> Any:
    while time.monotonic() < deadline:
        value = condition()
        if value is not None and value is not False:
            return value
        time.sleep(poll)
    raise RuntimeError(f"timed out waiting for {description}")


def _run_429_case() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    python = Path(sys.executable)

    tmp = tempfile.mkdtemp(prefix="lhh_e2e_429_")
    tmp_path = Path(tmp)
    try:
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _install_fake_codex(bin_dir)

        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        subprocess.run(["git", "-C", str(workspace), "config", "user.email", "e2e@test"], check=True)
        subprocess.run(["git", "-C", str(workspace), "config", "user.name", "E2E"], check=True)
        (workspace / "README.md").write_text("# test\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(workspace), "add", "."], check=True)
        subprocess.run(["git", "-C", str(workspace), "commit", "-q", "-m", "init"], check=True)
        base_check = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYTHONDONTWRITEBYTECODE": "1",
            # src must win for lh_harness, but the inherited PYTHONPATH (the
            # environment's own dependency venv) has to survive: dropping it
            # would hide pydantic/uvicorn from the service subprocess.
            "PYTHONPATH": os.pathsep.join(
                part
                for part in (
                    str(repo_root / "src"),
                    os.environ.get("PYTHONPATH", ""),
                )
                if part
            ),
            "LH_HARNESS_WEB_TOKEN": TOKEN,
        }
        # The launcher prefers <runs-root>/.lh-harness/config.toml, so write
        # the fast-poll config next to the runs root and start the service
        # inside the temp dir as well (cwd fallback).
        (runs_root / ".lh-harness").mkdir()
        _write_project_config(runs_root)
        _write_project_config(tmp_path)
        service_cwd = str(tmp_path)
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"

        service = subprocess.Popen(
            [
                str(python),
                "-m",
                "lh_harness",
                "web",
                f"--runs-root={runs_root}",
                f"--workspace-root={workspace}",
                "--host=127.0.0.1",
                f"--port={port}",
                f"--auth-token={TOKEN}",
                "--no-open",
            ],
            env=env,
            cwd=service_cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        try:
            _wait_for_service(base_url, time.monotonic() + SERVICE_START_TIMEOUT)

            enqueue_body = {
                "name": "queue 429 retry",
                "task": "Repo: LongHorizon-Harness. Branch: main. Deliverables: prove queue retry on provider 429. Acceptance: original failed, successor pending.",
                "workspace": str(workspace),
                "trio": "qwen",
                "max_rounds": 3,
                "priority": 0,
                "base_check": base_check,
                "requested_by": "e2e-429",
            }
            created = _api("POST", "/api/queue", body=enqueue_body, base_url=base_url)
            queue_id = created["queue_id"]

            # The launcher launches the entry; its round-1 executor hits the
            # forced 429, the run aborts with provider_rate_limit, and the
            # launcher marks the entry failed and requeues it.
            def _original_failed():
                entry = next(
                    (
                        e
                        for e in _all_entries(base_url)
                        if e.get("queue_id") == queue_id
                    ),
                    None,
                )
                if entry is not None and entry.get("status") == "failed":
                    return entry
                if entry is not None and entry.get("status") == "done":
                    # The contract's third assertion caught a real violation:
                    # surface it immediately instead of timing out.
                    raise RuntimeError(
                        "original entry reached done while its run was "
                        "non-terminal; retry path broken"
                    )
                return None

            original = _wait_until(
                _original_failed,
                time.monotonic() + QUEUE_TIMEOUT,
                "original entry to fail",
            )
            run_id = original.get("run_id") or ""
            assert run_id, f"failed entry lost its run_id: {original}"

            # Assertion 1: failed with a retryable failure_cause recorded.
            reason = str(original.get("reason") or "")
            assert reason, "failed entry has no reason recorded"
            assert (
                "rate" in reason.lower() or "429" in reason
            ), f"failure_cause is not the provider 429: {reason!r}"

            # Assertion 2: a successor is pending with retry_of/attempt set.
            def _successor_pending():
                entries = _all_entries(base_url)
                successors = [
                    e
                    for e in entries
                    if e.get("retry_of") == queue_id and e.get("status") == "pending"
                ]
                return successors[0] if successors else None

            successor = _wait_until(
                _successor_pending,
                time.monotonic() + QUEUE_TIMEOUT,
                "pending successor",
            )
            assert successor.get("retry_of") == queue_id
            assert successor.get("attempt") == 2, (
                f"successor attempt is {successor.get('attempt')!r}, expected 2"
            )
            successor_cause = str(successor.get("failure_cause") or "")
            assert successor_cause and (
                "rate" in successor_cause.lower() or "429" in successor_cause
            ), f"successor failure_cause missing the 429 cause: {successor_cause!r}"
            # A retry must not collide with the original's dedup id.
            assert successor.get("dedup_key") is None, (
                f"successor inherited dedup_key {successor.get('dedup_key')!r}"
            )

            # Assertion 3: no entry is ever done while its run is non-terminal.
            # The original run's only role episode failed, so the run is
            # terminal-failed and nothing in this queue ever satisfied
            # completion. If any entry shows done here, the promotion path
            # fired on a non-terminal run.
            final_entries = _all_entries(base_url)
            done_entries = [e for e in final_entries if e.get("status") == "done"]
            assert not done_entries, (
                f"queue entries reached done on a failed run: {done_entries}"
            )

            # The durable run report records the provider rate-limit abort.
            report_path = runs_root / run_id / "lh_harness" / "report.json"
            assert report_path.is_file(), f"report.json missing: {report_path}"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            assert (
                report.get("completion_satisfied") is not True
            ), "run claimed completion despite the forced 429"
            assert "provider_rate_limit" in str(report.get("abort_reason") or ""), (
                f"report abort_reason is not provider_rate_limit: "
                f"{report.get('abort_reason')!r}"
            )

            # The service event log carries queue.requeued with the contract
            # fields, mirroring the launcher's audit-tested emission.
            events_path = runs_root / "queue" / "service_events.jsonl"
            requeued: list[dict[str, Any]] = []
            if events_path.is_file():
                requeued = [
                    json.loads(line)
                    for line in events_path.read_text(encoding="utf-8").splitlines()
                    if '"queue.requeued"' in line
                ]
            assert requeued, "queue.requeued event missing from service events"
            payload = requeued[-1]["payload"]
            assert payload.get("original") == queue_id
            assert payload.get("successor") == successor.get("queue_id")
            assert payload.get("attempt") == 2
            assert "rate" in str(payload.get("cause", "")).lower() or "429" in str(
                payload.get("cause", "")
            ), f"requeued cause is not the 429: {payload.get('cause')!r}"

            print("OK: queue 429 retry path passed")
            print(
                f"  original={queue_id} successor={successor.get('queue_id')} "
                f"run_id={run_id}"
            )
        finally:
            service.terminate()
            try:
                service.wait(timeout=5)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait(timeout=5)
            stdout = service.stdout.read() if service.stdout else ""
            if stdout:
                print("--- service stdout ---")
                print(stdout)
    finally:
        # Keep the temp dir on failure for post-mortem inspection.
        pass


def main() -> int:
    try:
        _run_429_case()
        return 0
    except Exception as exc:  # pragma: no cover - surfaced to shell exit code
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())