#!/usr/bin/env python3
"""End-to-end happy path for the harness service queue.

This script starts a local lh-harness Web API on a free port, installs a fake
``codex`` agent on PATH, enqueues a trivial task via ``POST /api/queue``,
waits for the service launcher to start the run, resolves the completion gate
through the ASCII-only REST API, and asserts that the run report is produced,
the queue entry is ``done``, and a ``queue.launched`` event appears in the run
events.

The whole test is hermetic: a throwaway temp directory serves as the git
workspace and runs root, and the fake agent never touches the live CT110
service.
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
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


FAKE_CODEX_VERSION = "codex-cli 1.0.0"
SERVICE_START_TIMEOUT = 30.0
RUN_TIMEOUT = 90.0
RESOLVE_TIMEOUT = 30.0
TOKEN = "e2e-happy-token"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_project_config(root: Path, poll_seconds: float = 2.0) -> None:
    """Write a minimal .lh-harness/config.toml so the launcher uses fast polling."""
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
""",
        encoding="utf-8",
    )


def _fake_codex_source(version: str) -> str:
    """Return the source text of a deterministic Codex stand-in."""
    text_blocks = {
        "manager_round_1": (
            "Current task state:\n"
            "verified workspace and fake agent setup\n"
            "\n"
            "Task contract:\n"
            "Repo: LongHorizon-Harness. Branch: main. Deliverables: prove queue happy path. Acceptance: report.json produced and queue entry done.\n"
            "\n"
            "Next: cli"
        ),
        "manager_round_n": (
            "Current task state:\n"
            "work complete\n"
            "\n"
            "Task contract:\n"
            "Repo: LongHorizon-Harness. Branch: main. Deliverables: prove queue happy path. Acceptance: report.json produced and queue entry done.\n"
            "\n"
            "Next: done"
        ),
        "auditor": (
            "Status: complete\n"
            "Integrity: clean\n"
            "Contract audit: aligned\n"
            "\n"
            "Audit facts: the fake agent reported completion; no blocking acceptance constraints."
        ),
    }
    return (
        f"""\
#!/usr/bin/env python3
import json
import re
import sys

if "--version" in sys.argv:
    print({version!r})
    sys.exit(0)

prompt = sys.stdin.read()
role = "unknown"
if "You are the LongHorizon-Harness manager agent" in prompt:
    role = "manager"
elif "You are the LongHorizon-Harness CLI executor" in prompt:
    role = "executor"
elif "You are the read-only LongHorizon-Harness CLI auditor" in prompt:
    role = "auditor"
elif "LongHorizon-Harness final response agent" in prompt or "Write the reply now" in prompt:
    role = "final_response"

MANAGER_ROUND_1 = {text_blocks['manager_round_1']!r}
MANAGER_ROUND_N = {text_blocks['manager_round_n']!r}
AUDITOR_TEXT = {text_blocks['auditor']!r}

m = re.search(r"Current management round:\\s*(\\d+)", prompt)
round_index = int(m.group(1)) if m else 1

print(json.dumps({{"type": "thread.started", "thread_id": "stub"}}))

if role == "manager":
    text = MANAGER_ROUND_1 if round_index == 1 else MANAGER_ROUND_N
elif role == "executor":
    text = "All requested work is complete."
elif role == "auditor":
    text = AUDITOR_TEXT
elif role == "final_response":
    text = "The requested work is complete."
else:
    text = "done"

print(json.dumps({{
    "type": "item.completed",
    "item": {{"type": "agent_message", "text": text}},
}}))
print(json.dumps({{"type": "turn.completed"}}))
"""
    )


def _install_fake_codex(bin_dir: Path) -> None:
    """Write a deterministic Codex stand-in that drives the role loop to completion."""
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


def _queue_entry(queue_id: str, base_url: str) -> dict[str, Any] | None:
    data = _api("GET", "/api/queue", base_url=base_url)
    for entry in data.get("entries", []):
        if entry.get("queue_id") == queue_id:
            return entry
    return None


def _poll_queue_entry_status(
    queue_id: str,
    base_url: str,
    deadline: float,
    statuses: set[str],
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        entry = _queue_entry(queue_id, base_url)
        if entry is not None and entry.get("status") in statuses:
            return entry
        time.sleep(0.3)
    entry = _queue_entry(queue_id, base_url)
    raise RuntimeError(
        f"queue entry {queue_id} did not reach {statuses}; "
        f"last status={entry.get('status') if entry else 'missing'}"
    )


def _find_pending_approval(run_id: str, base_url: str, deadline: float) -> dict[str, Any] | None:
    while time.monotonic() < deadline:
        snapshot = _api("GET", f"/api/runs/{run_id}/snapshot", base_url=base_url)
        for approval in snapshot.get("approvals", []):
            if approval.get("status") == "pending":
                return approval
        status = _api("GET", f"/api/runs/{run_id}/status", base_url=base_url)
        if not status.get("alive", True) and status.get("status") in {"completed", "cancelled", "failed", "blocked"}:
            # The run may finish without a gate if the human hook is absent.
            return None
        time.sleep(0.3)
    raise RuntimeError("no pending approval and run is still alive")


def _wait_for_terminal_status(run_id: str, base_url: str, deadline: float) -> dict[str, Any]:
    terminal = {"completed", "cancelled", "failed", "blocked", "incomplete"}
    while time.monotonic() < deadline:
        status = _api("GET", f"/api/runs/{run_id}/status", base_url=base_url)
        if status.get("status") in terminal or not status.get("alive", True):
            return status
        time.sleep(0.3)
    raise RuntimeError("run did not reach terminal status")


def _events_contain_queue_launched(run_id: str, runs_root: Path) -> bool:
    events_path = runs_root / run_id / "lh_harness" / "role_orchestration" / "events.jsonl"
    if not events_path.is_file():
        return False
    for line in events_path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") == "queue.launched":
            return True
    return False


def _run_happy_path() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    # Use the venv python symlink as-is (not the resolved system binary) so that
    # Python still discovers the venv's site-packages via pyvenv.cfg.
    python = Path(sys.executable)

    tmp = tempfile.mkdtemp(prefix="lhh_e2e_")
    tmp_path = Path(tmp)
    try:
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _install_fake_codex(bin_dir)

        # Make the workspace a git repo so base_check has meaning.
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
            "PYTHONPATH": str(repo_root / "src"),
            "LH_HARNESS_WEB_TOKEN": TOKEN,
        }
        # The launcher reads PROJECT_CONFIG_PATH from cwd, so start the web
        # server inside the temp directory where our fast-poll config lives.
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
                "name": "queue happy path",
                "task": "Repo: LongHorizon-Harness. Branch: main. Deliverables: prove queue happy path. Acceptance: report.json produced and queue entry done.",
                "workspace": str(workspace),
                "trio": "qwen",
                "max_rounds": 5,
                "priority": 0,
                "base_check": base_check,
                "requested_by": "e2e-happy",
            }
            created = _api("POST", "/api/queue", body=enqueue_body, base_url=base_url)
            queue_id = created["queue_id"]

            # Wait for the launcher to start the run.
            entry = _poll_queue_entry_status(
                queue_id, base_url, time.monotonic() + RUN_TIMEOUT, {"launched", "done", "failed"}
            )
            if entry.get("status") == "failed":
                raise RuntimeError(f"queue entry failed: {entry.get('reason')}")
            run_id = entry.get("run_id")
            if not run_id:
                raise RuntimeError("queue entry launched but no run_id")

            # Resolve the completion gate if the run is waiting; stop is ASCII-only.
            approval = _find_pending_approval(run_id, base_url, time.monotonic() + RESOLVE_TIMEOUT)
            while approval is not None:
                resolve_body = {
                    "action": "stop",
                    "reason": "e2e happy path complete",
                    "user_input": "stop confirmed",
                }
                _api(
                    "POST",
                    f"/api/runs/{run_id}/approvals/{approval['approval_id']}/resolve",
                    body=resolve_body,
                    base_url=base_url,
                )
                # The run may create a fresh post-stop approval; drain any that
                # appear until it reaches a terminal status.
                status = _api("GET", f"/api/runs/{run_id}/status", base_url=base_url)
                if status.get("status") in {"completed", "failed", "cancelled", "blocked", "incomplete"} or not status.get("alive", True):
                    break
                approval = _find_pending_approval(run_id, base_url, time.monotonic() + RESOLVE_TIMEOUT)

            # Wait for the run to finish and the launcher to mark the entry done.
            _poll_queue_entry_status(
                queue_id, base_url, time.monotonic() + RUN_TIMEOUT, {"done", "failed"}
            )

            _wait_for_terminal_status(run_id, base_url, time.monotonic() + RUN_TIMEOUT)

            # Final assertions.
            final_entry = _api("GET", "/api/queue", base_url=base_url)
            matches = [e for e in final_entry.get("entries", []) if e.get("queue_id") == queue_id]
            if not matches:
                raise RuntimeError("queue entry disappeared")
            final = matches[0]
            assert final.get("status") == "done", f"queue entry status is {final.get('status')!r}"

            report_path = runs_root / run_id / "lh_harness" / "report.json"
            assert report_path.is_file(), f"report.json missing: {report_path}"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            assert report.get("completion_satisfied") is True, "completion_satisfied is false"

            owner_path = runs_root / run_id / "control" / "owner.json"
            assert owner_path.is_file(), "owner.json missing"
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
            assert owner.get("base_check") == base_check, f"base_check not wired: {owner.get('base_check')!r}"

            assert _events_contain_queue_launched(run_id, runs_root), "queue.launched event missing"

            print("OK: queue happy path passed")
            print(f"  queue_id={queue_id} run_id={run_id} base_check={base_check[:8]}")
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
        # Do not clean up on failure so post-mortem inspection is possible.
        pass


def main() -> int:
    try:
        _run_happy_path()
        return 0
    except Exception as exc:  # pragma: no cover - surfaced to shell exit code
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
