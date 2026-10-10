"""fc-H4b deploy scripts: suspend_runs.py / resume_runs.py + workflow wiring.

Since LHH-SUSPEND-TIMEOUT both scripts speak the ASYNC maintenance API:
``suspend_runs.py`` POSTs, accepts the 202 + suspend id, then polls
``GET /api/maintenance/suspend/{id}`` until a terminal state (never a
synchronous wait-for-stop behind the facade); ``resume_runs.py`` keys its
resume to exactly this deploy's suspend id (``--suspend-id``) so an older
suspend's manifest can never be consumed, and clears the launch pause
server-side via the same call.

The two CLI scripts are driven as real subprocesses against a stdlib
``http.server`` fake of the CT110 web API (202+poll happy path; suspend POST
failure; suspend job failed with auto-resume cleanup; poll-budget
exhaustion; status 404; resume keyed to the given id; empty-manifest no-op;
repeated resume idempotency; a resumed run that never reports ACTIVE exits
1 and names that run), so the tests exercise exactly the argv/env contract
the deploy workflow uses (``--url``/``--suspend-id`` arguments, bearer
token from ``CT110_API_TOKEN``, id export to ``$GITHUB_OUTPUT``).

The workflow-side tests parse ``.github/workflows/deploy-ct110.yml`` and pin
the fc-H4b wiring unchanged by the async-API work: the ``active_runs``
input, every step that invokes suspend/resume being gated on
``inputs.active_runs == 'suspend'``, the resume step running before the
drain clear on every path once the suspend step succeeded, the wait-mode
steps preserving the pre-fc-H4b behaviour, and the read-only post-restart
drain check staying read-only.  The repo has no other YAML-parsing workflow
test (test_deploy_node_targets.py works on raw text), so these use pyyaml
from the workspace venv and skip when it is not installed — it is not a
declared ``test`` extra in pyproject.toml.
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
SUSPEND_ID = "sus-20261010T000000Z-test0001"


def _snapshot(state: str, *, runs: list[dict] | None = None,
              errors: list[dict] | None = None, cleanup: list[dict] | None = None,
              launch_pause: dict | None = None) -> dict[str, object]:
    """One GET /api/maintenance/suspend/{id} answer, shaped like the real API."""

    return {
        "ok": True,
        "suspend_id": SUSPEND_ID,
        "state": state,
        "reason": "deploy test window",
        "created_at": 1_760_000_000.0,
        "finished_at": None if state == "running" else 1_760_000_012.0,
        "grace_seconds": 30.0,
        "deadline_seconds": 120.0,
        "deadline_at": 1_760_000_120.0,
        "runs": list(runs or []),
        "errors": list(errors or []),
        "cleanup_done": bool(cleanup),
        "cleanup": list(cleanup or []),
        "launch_pause": launch_pause if launch_pause is not None else {
            "enabled": state == "suspended", "reason": "deploy test window",
            "suspend_id": SUSPEND_ID if state == "running" else None,
            "set_at": 1_760_000_000.0,
        },
    }


class _FakeCt110:
    """A stdlib http.server fake of the CT110 async maintenance/runs endpoints."""

    def __init__(self) -> None:
        self.state: dict[str, object] = {
            "suspend_post_status": 202,   # HTTP code POST suspend answers with
            "suspend_snapshots": [],      # GET status answers, served in order (last repeats)
            "resume_responses": [],       # POST resume answers, one per call (last repeats)
            "resume_post_status": 200,    # HTTP code POST resume answers with
            "resume_post_detail": "suspend is still pausing runs",
            "runs": [],                   # entries GET /api/runs reports
        }
        self._snapshot_index = 0
        self._resume_index = 0
        self.posts: list[dict[str, object]] = []
        self.gets: list[str] = []
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
                    status = int(outer.state["suspend_post_status"])
                    if status != 202:
                        # The drain/overlap gates answer 409 with a detail.
                        self._json({"detail": "queue drain must be enabled before maintenance suspend"},
                                   code=status)
                        return
                    self._json({
                        "ok": True,
                        "suspend_id": SUSPEND_ID,
                        "state": "running",
                        "reason": json.loads(body or b"{}").get("reason"),
                        "grace_seconds": 30.0,
                        "deadline_seconds": 120.0,
                        "status_url": f"/api/maintenance/suspend/{SUSPEND_ID}",
                    }, code=202)
                elif self.path == "/api/maintenance/resume":
                    status = int(outer.state["resume_post_status"])
                    if status != 200:
                        self._json({"detail": str(outer.state["resume_post_detail"])}, code=status)
                        return
                    responses = list(outer.state["resume_responses"])
                    if responses:
                        index = min(outer._resume_index, len(responses) - 1)
                        outer._resume_index += 1
                        payload = dict(responses[index])
                    else:
                        payload = {"ok": True, "runs": []}
                    payload.setdefault("suspend_id",
                                       json.loads(body or b"{}").get("suspend_id"))
                    self._json(payload)
                else:
                    self._json({"error": "no such route"}, code=404)

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                outer.gets.append(self.path)
                if self.path.startswith("/api/maintenance/suspend/"):
                    snapshots = list(outer.state["suspend_snapshots"])
                    if not snapshots:
                        self._json({"detail": f"unknown suspend id: {SUSPEND_ID}"}, code=404)
                        return
                    index = min(outer._snapshot_index, len(snapshots) - 1)
                    outer._snapshot_index += 1
                    self._json(dict(snapshots[index]))
                elif self.path.startswith("/api/runs"):
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

    def status_polls(self) -> int:
        return sum(1 for path in self.gets if path.startswith("/api/maintenance/suspend/"))

    def runs_polls(self) -> int:
        return sum(1 for path in self.gets if path.startswith("/api/runs"))

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


def _run(script: Path, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["CT110_API_TOKEN"] = TEST_TOKEN
    env.pop("GITHUB_OUTPUT", None)  # hermetic: only set when a test asks for it
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, timeout=120, env=env,
    )


# --- suspend_runs.py ---------------------------------------------------------

def test_suspend_posts_reason_polls_until_suspended_and_exports_id(
        fake: _FakeCt110, tmp_path: Path) -> None:
    github_output = tmp_path / "github_output"
    fake.state["suspend_snapshots"] = [
        _snapshot("running", runs=[
            {"run_id": "run-aaa", "queue_id": None, "resume_epoch_before": 0,
             "state": "stopping", "detail": None},
        ]),
        _snapshot("suspended", runs=[
            {"run_id": "run-aaa", "queue_id": None, "resume_epoch_before": 0,
             "state": "paused", "detail": None},
            {"run_id": "run-bbb", "queue_id": "q-7", "resume_epoch_before": 1,
             "state": "paused", "detail": None},
        ]),
    ]
    result = _run(SUSPEND, "--url", fake.url, "--reason", "deploy test window",
                  "--interval-seconds", "0.05",
                  extra_env={"GITHUB_OUTPUT": str(github_output)})
    assert result.returncode == 0, result.stderr
    assert "run-aaa" in result.stdout and "run-bbb" in result.stdout
    assert f"suspend id: {SUSPEND_ID}" in result.stdout
    # The async contract in one assertion: exactly one POST, then polled GETs.
    assert len(fake.posts) == 1
    post = fake.posts[0]
    assert post["path"] == "/api/maintenance/suspend"
    assert post["body"] == {"reason": "deploy test window"}
    assert post["authorization"] == f"Bearer {TEST_TOKEN}"
    assert fake.status_polls() == 2
    assert all(path.startswith(f"/api/maintenance/suspend/{SUSPEND_ID}")
               for path in fake.gets)
    assert f"suspend_id={SUSPEND_ID}\n" in github_output.read_text(encoding="utf-8")


def test_suspend_http_failure_exits_nonzero_with_clear_error(fake: _FakeCt110) -> None:
    fake.state["suspend_post_status"] = 409  # drain gate: suspend without a drain
    result = _run(SUSPEND, "--url", fake.url, "--interval-seconds", "0.05")
    assert result.returncode != 0
    assert "POST /api/maintenance/suspend failed" in result.stderr
    assert "queue drain" in result.stderr  # the API's detail is surfaced
    assert fake.status_polls() == 0  # no id, no polling


def test_suspend_failed_state_reports_auto_resume_and_exits_nonzero(fake: _FakeCt110) -> None:
    # The background job hit its per-run grace / deadline; the SERVICE must
    # already have auto-resumed what it paused (cleanup) — the script only
    # reports and exits non-zero so the always() cleanup path runs.
    fake.state["suspend_snapshots"] = [
        _snapshot("running", runs=[
            {"run_id": "run-aaa", "queue_id": None, "resume_epoch_before": 0,
             "state": "stopping", "detail": None},
            {"run_id": "run-bbb", "queue_id": None, "resume_epoch_before": 0,
             "state": "stopping", "detail": None},
        ]),
        _snapshot("failed",
                  runs=[
                      {"run_id": "run-aaa", "queue_id": None, "resume_epoch_before": 0,
                       "state": "resumed", "detail": None},
                      {"run_id": "run-bbb", "queue_id": None, "resume_epoch_before": 0,
                       "state": "active", "detail": "grace period exceeded; left running"},
                  ],
                  errors=[{"run_id": "run-bbb",
                           "error": "safe-checkpoint grace 30.0s exceeded; leaving run active"}],
                  cleanup=[{"run_id": "run-aaa", "ok": True, "error": None}]),
    ]
    result = _run(SUSPEND, "--url", fake.url, "--interval-seconds", "0.05")
    assert result.returncode == 1
    assert SUSPEND_ID in result.stderr
    assert "did not complete" in result.stderr
    assert "run-bbb" in result.stderr  # the run that escaped its window is named
    assert "run-aaa" in result.stderr  # and the auto-resume evidence is shown
    assert fake.runs_polls() == 0


def test_suspend_client_poll_budget_exhaustion_exits_nonzero(fake: _FakeCt110) -> None:
    # The server keeps reporting "running" (never terminal): the client must
    # give up inside its own bounded budget instead of hanging the deploy.
    fake.state["suspend_snapshots"] = [_snapshot("running", runs=[
        {"run_id": "run-aaa", "queue_id": None, "resume_epoch_before": 0,
         "state": "stopping", "detail": None},
    ])]
    result = _run(SUSPEND, "--url", fake.url,
                  "--timeout-seconds", "0.3", "--interval-seconds", "0.05")
    assert result.returncode == 1
    assert "did not reach a terminal state" in result.stderr
    assert SUSPEND_ID in result.stderr
    assert fake.status_polls() >= 2  # it really polled, then gave up


def test_suspend_status_404_exits_nonzero(fake: _FakeCt110) -> None:
    # No snapshots -> the fake answers 404; an id the server no longer knows
    # cannot be trusted to still hold a window, so fail loudly.
    result = _run(SUSPEND, "--url", fake.url, "--interval-seconds", "0.05")
    assert result.returncode == 1
    assert "unknown suspend id" in result.stderr


# --- resume_runs.py ----------------------------------------------------------

def test_resume_keys_manifest_to_this_deploys_suspend_id(fake: _FakeCt110) -> None:
    fake.state["resume_responses"] = [{
        "ok": True,
        "runs": [{"run_id": "run-aaa", "ok": True, "error": None}],
    }]
    fake.state["runs"] = [{"id": "run-aaa", "status": "running"}]
    result = _run(RESUME, "--url", fake.url, "--suspend-id", SUSPEND_ID,
                  "--interval-seconds", "0.05", "--timeout-minutes", "1")
    assert result.returncode == 0, result.stderr
    assert SUSPEND_ID in result.stdout
    assert "resume accepted: run-aaa" in result.stdout
    assert fake.posts[0]["path"] == "/api/maintenance/resume"
    # The provenance pin: the ONLY manifest this resume can consume is the
    # one named by the deploy's own suspend id.
    assert fake.posts[0]["body"] == {"suspend_id": SUSPEND_ID}
    assert fake.posts[0]["authorization"] == f"Bearer {TEST_TOKEN}"


def test_resume_without_suspend_id_is_refused(fake: _FakeCt110) -> None:
    # The id must be explicit — falling back to "whatever manifest exists"
    # is exactly the stale-manifest revival path.
    result = _run(RESUME, "--url", fake.url)
    assert result.returncode != 0
    assert "suspend-id" in result.stderr


def test_resume_empty_suspend_id_is_a_noop(fake: _FakeCt110) -> None:
    # Deploy whose suspend step never produced an id: nothing to resume, and
    # crucially no POST (nothing may touch an unrelated suspend's manifest).
    result = _run(RESUME, "--url", fake.url, "--suspend-id", "")
    assert result.returncode == 0, result.stderr
    assert "nothing to resume" in result.stdout
    assert fake.posts == []


def test_resume_empty_manifest_is_a_noop_without_polling(fake: _FakeCt110) -> None:
    fake.state["resume_responses"] = [{"ok": True, "runs": []}]
    result = _run(RESUME, "--url", fake.url, "--suspend-id", SUSPEND_ID,
                  "--interval-seconds", "0.05", "--timeout-minutes", "1")
    assert result.returncode == 0, result.stderr
    assert "no parked runs" in result.stdout
    assert fake.runs_polls() == 0


def test_resume_repeated_call_is_idempotent(fake: _FakeCt110) -> None:
    # First call consumes the manifest; the service deletes it once every
    # run resumed, so the identical second call is a clean no-op — never an
    # error, never a re-resume.
    fake.state["resume_responses"] = [
        {"ok": True, "runs": [{"run_id": "run-aaa", "ok": True, "error": None}]},
        {"ok": True, "runs": []},
    ]
    fake.state["runs"] = [{"id": "run-aaa", "status": "running"}]
    first = _run(RESUME, "--url", fake.url, "--suspend-id", SUSPEND_ID,
                 "--interval-seconds", "0.05", "--timeout-minutes", "1")
    second = _run(RESUME, "--url", fake.url, "--suspend-id", SUSPEND_ID,
                  "--interval-seconds", "0.05", "--timeout-minutes", "1")
    assert first.returncode == 0, first.stderr
    assert "all 1 resumed run(s) active" in first.stdout
    assert second.returncode == 0, second.stderr
    assert "no parked runs" in second.stdout
    posts = [post for post in fake.posts if post["path"] == "/api/maintenance/resume"]
    assert [post["body"] for post in posts] == [
        {"suspend_id": SUSPEND_ID}, {"suspend_id": SUSPEND_ID}]


def test_resume_refuses_an_answer_keyed_to_a_different_suspend(fake: _FakeCt110) -> None:
    # Defence in depth: even if a server answered with another suspend's id
    # (crossed windows), the client must not trust per-run results under the
    # wrong provenance.
    fake.state["resume_responses"] = [{
        "ok": True,
        "suspend_id": "sus-20100101T000000Z-stale000",
        "runs": [{"run_id": "run-zzz", "ok": True, "error": None}],
    }]
    result = _run(RESUME, "--url", fake.url, "--suspend-id", SUSPEND_ID,
                  "--interval-seconds", "0.05", "--timeout-minutes", "1")
    assert result.returncode == 1
    assert "sus-20100101T000000Z-stale000" in result.stderr
    assert SUSPEND_ID in result.stderr


def test_resume_server_refusal_exits_nonzero_with_detail(fake: _FakeCt110) -> None:
    # 409: the named suspend is still pausing runs — a resume racing its own
    # suspend window fails loudly instead of consuming anything.
    fake.state["resume_post_status"] = 409
    fake.state["resume_post_detail"] = f"suspend {SUSPEND_ID} is still pausing runs"
    result = _run(RESUME, "--url", fake.url, "--suspend-id", SUSPEND_ID)
    assert result.returncode == 1
    assert "POST /api/maintenance/resume failed" in result.stderr
    assert "still pausing runs" in result.stderr


def test_resume_waits_for_a_run_that_turns_active_mid_poll(fake: _FakeCt110) -> None:
    fake.state["resume_responses"] = [{
        "ok": True,
        "runs": [
            {"run_id": "run-aaa", "ok": True, "error": None},
            {"run_id": "run-bbb", "ok": True, "error": None},
        ],
    }]
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
        result = _run(RESUME, "--url", fake.url, "--suspend-id", SUSPEND_ID,
                      "--interval-seconds", "0.05", "--timeout-minutes", "1")
    finally:
        timer.cancel()
    assert result.returncode == 0, result.stderr
    assert "run-bbb is active" in result.stdout


def test_resume_with_one_run_never_active_exits_1_and_names_it(fake: _FakeCt110) -> None:
    fake.state["resume_responses"] = [{
        "ok": True,
        "runs": [
            {"run_id": "run-aaa", "ok": True, "error": None},
            {"run_id": "run-bbb", "ok": True, "error": None},
        ],
    }]
    fake.state["runs"] = [{"id": "run-aaa", "status": "running"}]
    result = _run(RESUME, "--url", fake.url, "--suspend-id", SUSPEND_ID,
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
    # -> verifies -> [failure: rollback -> verify rollback] -> resume -> clear
    # drain.  The rollback restarts the service, so it must come BEFORE the
    # resume (its restart would kill the runs just put back) and before the
    # drain clear (no launches may start under it).
    assert _step_index(steps, "Set drain") < _step_index(steps, "Suspend active runs")
    assert _step_index(steps, "Suspend active runs") < _step_index(steps, "suspended runs to stop")
    assert _step_index(steps, "suspended runs to stop") < _step_index(steps, "Deploy (bytes-safe")
    assert _step_index(steps, "Deploy (bytes-safe") < _step_index(steps, "Automatic rollback")
    assert _step_index(steps, "Automatic rollback") < _step_index(steps, "Verify the rollback")
    assert _step_index(steps, "Verify the rollback") < _step_index(steps, "Resume suspended runs")
    assert _step_index(steps, "Resume suspended runs") < _step_index(steps, "Clear drain")

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


def test_verify_steps_wait_and_check_the_commit_not_the_version() -> None:
    """Run 37162090599 (b6f51f9): one-shot /api/meta + version-keyed rollback."""
    doc = _workflow_doc()
    steps = _deploy_steps(doc)
    assert doc["jobs"]["deploy"]["env"]["EXPECTED_COMMIT"] == "${{ needs.preflight.outputs.sha }}"
    # No step may probe /api/meta with a one-shot curl any more.
    assert not any("curl" in step.get("run", "") and "/api/meta" in step.get("run", "") for step in steps)
    meta = steps[_step_index(steps, "Verify GET /api/meta answers")]
    assert "verify_meta.py" in meta["run"]
    assert '--expect-commit "$EXPECTED_COMMIT"' in meta["run"]
    assert "--timeout-seconds 90" in meta["run"] and "--interval-seconds 5" in meta["run"]
    installed = steps[_step_index(steps, "Verify installed lh_harness version and build commit")]
    assert "lh_harness.build_info" in installed["run"] and "$EXPECTED_COMMIT" in installed["run"]
    verify_rollback = steps[_step_index(steps, "Verify the rollback")]
    assert "steps.rollback.outcome == 'success'" in verify_rollback["if"]
    assert '--expect-commit "$prev_commit"' in verify_rollback["run"]
    assert "CT110_DEPLOY_ROLLBACK_FAILED" in verify_rollback["run"]
    report = steps[_step_index(steps, "Report rolled-back outcome")]
    assert "steps.verify_rollback.outcome == 'success'" in report["if"]
    assert "COMMIT_UNVERIFIED" in report["run"]

    build = doc["jobs"]["build"]["steps"]
    stamp = build[_step_index(build, "Stamp the build commit")]
    assert "src/lh_harness/_build_info.json" in stamp["run"]
    assert _step_index(build, "Stamp the build commit") < _step_index(build, "Build wheel")
    check = build[_step_index(build, "build commit match preflight")]
    assert "lh_harness/_build_info.json" in check["run"]


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
