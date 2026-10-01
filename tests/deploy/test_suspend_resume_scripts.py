"""fc-H4b deploy scripts: suspend_runs.py / resume_runs.py + workflow wiring.

The two CLI scripts are driven as real subprocesses against a stdlib
``http.server`` fake of the CT110 web API (suspend ok; suspend HTTP failure;
resume ok; resume where one run never reports ACTIVE again must exit 1 and
name that run), so the tests exercise exactly the argv/env contract the
deploy workflow uses (``--url`` argument, bearer token from
``CT110_API_TOKEN``).

The workflow-side tests parse ``.github/workflows/deploy-ct110.yml`` and pin
the fc-H4b wiring: the ``active_runs`` input, every step that invokes
suspend/resume being gated on ``inputs.active_runs == 'suspend'``, the
resume step running before the drain clear on every path once the suspend
step succeeded, the wait-mode steps preserving the pre-fc-H4b behaviour, and
the read-only post-restart drain check staying read-only.  The repo has no
other YAML-parsing workflow test (test_deploy_node_targets.py works on raw
text), so these use pyyaml from the workspace venv and skip when it is not
installed — it is not a declared ``test`` extra in pyproject.toml.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts" / "deploy" / "ct110"
SUSPEND = SCRIPTS / "suspend_runs.py"
RESUME = SCRIPTS / "resume_runs.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy-ct110.yml"

TEST_TOKEN = "test-token-not-a-real-secret"


class _FakeCt110:
    """A stdlib http.server fake of the CT110 maintenance/runs endpoints."""

    def __init__(self) -> None:
        self.state: dict[str, object] = {
            "suspend_status": 200,  # HTTP code POST suspend answers with
            "suspend_ids": [],      # run ids POST suspend reports as stopped
            "suspend_errors": [],   # per-run stop errors POST suspend reports
            "resume_runs": [],      # per-run results POST resume reports
            "runs": [],             # entries GET /api/runs reports
        }
        self.posts: list[dict[str, object]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _json(self, payload: dict, code: int = 200) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.posts.append({
                    "path": self.path,
                    "body": json.loads(body or b"{}"),
                    "authorization": self.headers.get("Authorization"),
                })
                if self.path == "/api/maintenance/suspend":
                    status = int(outer.state["suspend_status"])
                    run_ids = list(outer.state["suspend_ids"])
                    if status != 200:
                        # The drain gate answers 409 without an enabled drain.
                        self._json({"detail": "queue drain must be enabled first"},
                                   code=status)
                        return
                    self._json({
                        "ok": True,
                        "stopped": [
                            {"run_id": run_id, "queue_id": None, "resume_epoch_before": 0}
                            for run_id in run_ids
                        ],
                        "errors": list(outer.state["suspend_errors"]),
                        "manifest": {"runs": [{"run_id": run_id} for run_id in run_ids]},
                    })
                elif self.path == "/api/maintenance/resume":
                    self._json({"ok": True, "runs": list(outer.state["resume_runs"])})
                else:
                    self._json({"error": "no such route"}, code=404)

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                if self.path.startswith("/api/runs"):
                    self._json({"runs": list(outer.state["runs"])})
                else:
                    self._json({"error": "no such route"}, code=404)

            def log_message(self, *args: object) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=10)
        self.server.server_close()


@pytest.fixture()
def fake() -> _FakeCt110:
    server = _FakeCt110()
    try:
        yield server
    finally:
        server.close()


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["CT110_API_TOKEN"] = TEST_TOKEN
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, timeout=120, env=env,
    )


# --- suspend_runs.py ---------------------------------------------------------

def test_suspend_posts_reason_and_prints_stopped_run_ids(fake: _FakeCt110) -> None:
    fake.state["suspend_ids"] = ["run-aaa", "run-bbb"]
    result = _run(SUSPEND, "--url", fake.url, "--reason", "deploy test window")
    assert result.returncode == 0, result.stderr
    assert "run-aaa" in result.stdout and "run-bbb" in result.stdout
    assert len(fake.posts) == 1
    post = fake.posts[0]
    assert post["path"] == "/api/maintenance/suspend"
    assert post["body"] == {"reason": "deploy test window"}
    assert post["authorization"] == f"Bearer {TEST_TOKEN}"


def test_suspend_http_failure_exits_nonzero_with_clear_error(fake: _FakeCt110) -> None:
    fake.state["suspend_status"] = 409  # drain gate: suspend without a drain
    result = _run(SUSPEND, "--url", fake.url)
    assert result.returncode != 0
    assert "POST /api/maintenance/suspend failed" in result.stderr


# --- resume_runs.py ----------------------------------------------------------

def test_resume_prints_per_run_results_and_exits_zero_when_active(fake: _FakeCt110) -> None:
    fake.state["resume_runs"] = [
        {"run_id": "run-aaa", "ok": True, "error": None},
    ]
    fake.state["runs"] = [{"id": "run-aaa", "status": "running"}]
    result = _run(RESUME, "--url", fake.url,
                  "--interval-seconds", "0.05", "--timeout-minutes", "1")
    assert result.returncode == 0, result.stderr
    assert "resume accepted: run-aaa" in result.stdout
    assert fake.posts[0]["path"] == "/api/maintenance/resume"
    assert fake.posts[0]["authorization"] == f"Bearer {TEST_TOKEN}"


def test_resume_waits_for_a_run_that_turns_active_mid_poll(fake: _FakeCt110) -> None:
    fake.state["resume_runs"] = [
        {"run_id": "run-aaa", "ok": True, "error": None},
        {"run_id": "run-bbb", "ok": True, "error": None},
    ]
    fake.state["runs"] = [
        {"id": "run-aaa", "status": "running"},
        {"id": "run-bbb", "status": "stopped"},
    ]

    def flip_to_active() -> None:
        fake.state["runs"] = [
            {"id": "run-aaa", "status": "running"},
            {"id": "run-bbb", "status": "running"},
        ]

    timer = threading.Timer(0.3, flip_to_active)
    timer.start()
    try:
        result = _run(RESUME, "--url", fake.url,
                      "--interval-seconds", "0.05", "--timeout-minutes", "1")
    finally:
        timer.cancel()
    assert result.returncode == 0, result.stderr
    assert "run-bbb is active" in result.stdout


def test_resume_with_one_run_never_active_exits_1_and_names_it(fake: _FakeCt110) -> None:
    fake.state["resume_runs"] = [
        {"run_id": "run-aaa", "ok": True, "error": None},
        {"run_id": "run-bbb", "ok": True, "error": None},
    ]
    fake.state["runs"] = [{"id": "run-aaa", "status": "running"}]
    result = _run(RESUME, "--url", fake.url,
                  "--interval-seconds", "0.05", "--timeout-minutes", "0.01")
    assert result.returncode == 1
    assert "run-bbb" in result.stderr
    assert "run-aaa" not in result.stderr


# --- workflow wiring ---------------------------------------------------------

def _workflow_doc() -> dict:
    yaml = pytest.importorskip(
        "yaml", reason="pyyaml is not a declared test extra; the workspace venv provides it")
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def _deploy_steps(doc: dict) -> list[dict]:
    return doc["jobs"]["deploy"]["steps"]


def _step_index(steps: list[dict], fragment: str) -> int:
    matches = [i for i, step in enumerate(steps) if fragment in step.get("name", "")]
    assert len(matches) == 1, f"expected exactly one step containing {fragment!r}"
    return matches[0]


def test_workflow_yaml_parses_and_declares_the_active_runs_input() -> None:
    doc = _workflow_doc()
    assert doc["name"] == "Deploy CT110 harness"
    # PyYAML (YAML 1.1) reads the bare key `on:` as the boolean True.
    on_block = doc["on"] if "on" in doc else doc[True]
    active_runs = on_block["workflow_dispatch"]["inputs"]["active_runs"]
    assert active_runs["type"] == "choice"
    assert active_runs["options"] == ["suspend", "wait"]
    assert active_runs["default"] == "suspend"


def test_every_suspend_resume_step_is_gated_on_the_input() -> None:
    steps = _deploy_steps(_workflow_doc())
    referencing = [
        step for step in steps
        if "suspend_runs.py" in step.get("run", "") or "resume_runs.py" in step.get("run", "")
    ]
    invoked = {script for step in referencing for script in
               ("suspend_runs.py", "resume_runs.py") if script in step["run"]}
    # The test cannot pass vacuously: both scripts must appear.
    assert invoked == {"suspend_runs.py", "resume_runs.py"}
    for step in referencing:
        condition = step.get("if")
        assert condition, f"step {step.get('name')!r} has no if-guard"
        assert "inputs.active_runs == 'suspend'" in condition, (
            f"step {step.get('name')!r} is not gated on the active_runs input")


def test_suspend_mode_order_and_resume_guard() -> None:
    steps = _deploy_steps(_workflow_doc())
    # Suspend mode: set drain -> suspend -> short zero-active wait -> deploy
    # -> verifies -> resume -> clear drain.  The drain clear keeps its
    # pre-fc-H4b place ahead of the rollback block.
    assert _step_index(steps, "Set drain") < _step_index(steps, "Suspend active runs")
    assert _step_index(steps, "Suspend active runs") < _step_index(steps, "suspended runs to stop")
    assert _step_index(steps, "suspended runs to stop") < _step_index(steps, "Deploy (bytes-safe")
    assert _step_index(steps, "Deploy (bytes-safe") < _step_index(steps, "Resume suspended runs")
    assert _step_index(steps, "Resume suspended runs") < _step_index(steps, "Clear drain")
    assert _step_index(steps, "Clear drain") < _step_index(steps, "Automatic rollback")

    resume = next(step for step in steps if "resume_runs.py" in step.get("run", ""))
    # always() so the automatic-rollback path resumes too, and whenever the
    # suspend step RAN (not only when it succeeded): a partial suspend that
    # then failed must still put the parked runs back.
    assert "always()" in resume["if"]
    assert "steps.suspend_runs.outcome != 'skipped'" in resume["if"]
    assert "steps.suspend_runs.outcome == 'success'" not in resume["if"]

    rollback = steps[_step_index(steps, "Automatic rollback")]
    assert "failure()" in rollback["if"]
    assert "steps.deploy.outcome" in rollback["if"]


def test_wait_mode_and_existing_guards_are_unchanged() -> None:
    steps = _deploy_steps(_workflow_doc())
    waits = [step for step in steps if "wait_zero_active.py" in step.get("run", "")]
    assert len(waits) == 2
    by_timeout = {
        step["run"].split("--timeout-minutes ", 1)[1].split("\n")[0].strip(): step
        for step in waits
    }
    # Suspend mode: a fixed 10-minute wait (only the stops have to land).
    assert "inputs.active_runs == 'suspend'" in by_timeout["10"]["if"]
    # Wait mode: the legacy step, same run body, simply gated on 'wait'.
    legacy = by_timeout['"$WAIT_TIMEOUT_MINUTES"']
    assert legacy["if"] == "${{ !inputs.dry_run && inputs.active_runs == 'wait' }}"

    # The post-restart drain check stays read-only (no --enable) and the
    # always() drain clear is intact.
    verify = next(step for step in steps
                  if "Verify drain survived the restart" in step.get("name", ""))
    assert "--verify-state enabled" in verify["run"]
    assert "--enable" not in verify["run"]
    clear = steps[_step_index(steps, "Clear drain")]
    assert clear["if"] == ("${{ !inputs.dry_run && always() && "
                           "steps.set_drain.outcome == 'success' }}")
    assert "--disable" in clear["run"]
