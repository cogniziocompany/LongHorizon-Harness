"""Unit tests for single-role PR review runs (kind="review").

Covers the request contract (``max_rounds`` forced to 1, timeout budget 900,
rejected caller knobs), the review.json schema, the seat trio resolution, the
gate/PR-body fetch boundaries, and the harness-level guarantee that a killed
or erroring review run can only end in ``cannot_review``.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from lh_harness.review import (
    REVIEWER_TIMEOUT_SECONDS,
    REVIEW_MAX_ROUNDS,
    VERDICTS,
    build_reviewer_prompt,
    collect_review_inputs,
    parse_review_output,
    parse_review_spec,
    prepare_review_workspace,
    read_review_report,
    resolve_reviewer_candidates,
    resolve_reviewer_timeout,
    repo_remote_url,
    review_from_episode_result,
    review_report,
    review_workspace_path,
    ReviewSpecError,
    ReviewWorkspaceError,
)
from lh_harness.supervisor.service import RunSupervisor
from lh_harness.types import EpisodeResult


# --- request validation -----------------------------------------------------


def test_parse_review_spec_accepts_the_contract_body() -> None:
    spec = parse_review_spec(
        {
            "repo": "cogniziocompany/LongHorizon-Harness",
            "pr_number": 186,
            "head_sha": "0123ABCDEF",
            "base_ref": "main",
            "gate_results_url": "https://ci.example.com/gate/186.json",
        }
    )
    assert spec.repo == "cogniziocompany/LongHorizon-Harness"
    assert spec.pr_number == 186
    assert spec.head_sha == "0123abcdef"
    assert spec.base_ref == "main"
    assert spec.gate_results_url == "https://ci.example.com/gate/186.json"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        "not a dict",
        {"repo": "a/b", "pr_number": 1, "head_sha": "abc123", "base_ref": "main"},
        {
            "repo": "a/b",
            "pr_number": 1,
            "head_sha": "abc123",
            "base_ref": "main",
            "gate_results_url": "https://x/gate.json",
            # Caller-controlled run knobs must never reshape a review run.
            "max_rounds": 4,
        },
        {
            "repo": "a/b",
            "pr_number": 1,
            "head_sha": "abc123",
            "base_ref": "main",
            "gate_results_url": "https://x/gate.json",
            "roles": {"reviewer": {"model": "x"}},
        },
        {
            "repo": "a/b",
            "pr_number": 0,
            "head_sha": "abc123",
            "base_ref": "main",
            "gate_results_url": "https://x/gate.json",
        },
        {
            "repo": "a/b",
            "pr_number": True,
            "head_sha": "abc123",
            "base_ref": "main",
            "gate_results_url": "https://x/gate.json",
        },
        {
            "repo": "a/b",
            "pr_number": 1,
            "head_sha": "zzzz12",
            "base_ref": "main",
            "gate_results_url": "https://x/gate.json",
        },
        {
            "repo": "a/b",
            "pr_number": 1,
            "head_sha": "abc123",
            "base_ref": "main",
            "gate_results_url": "ftp://x/gate.json",
        },
        {
            "repo": "../../etc",
            "pr_number": 1,
            "head_sha": "abc123",
            "base_ref": "main",
            "gate_results_url": "https://x/gate.json",
        },
        {
            "repo": "a/b",
            "pr_number": 1,
            "head_sha": "abc123",
            "base_ref": "-u",
            "gate_results_url": "https://x/gate.json",
        },
    ],
)
def test_parse_review_spec_rejects_invalid_bodies(payload: Any) -> None:
    with pytest.raises(ValueError):
        parse_review_spec(payload)


def test_review_workspace_path_is_under_runs_root_review() -> None:
    spec = parse_review_spec(
        {
            "repo": "org/repo",
            "pr_number": 7,
            "head_sha": "abc123",
            "base_ref": "main",
            "gate_results_url": "https://x/gate.json",
        }
    )
    path = review_workspace_path("/tmp/runs", spec)
    assert path == Path("/tmp/runs/review/org/repo/7")
    assert "workspace" not in str(path)


def test_repo_remote_url_rejects_file_repos() -> None:
    with pytest.raises(ValueError):
        repo_remote_url("file:///home/harness/work/LongHorizon-Harness")
    assert repo_remote_url("org/repo") == "https://github.com/org/repo.git"
    assert (
        repo_remote_url("org/repo", host="https://git.example.com")
        == "https://git.example.com/org/repo.git"
    )


# --- timeout + seat resolution ----------------------------------------------


def test_reviewer_timeout_defaults_to_900_and_config_only_shortens() -> None:
    assert resolve_reviewer_timeout({}) == REVIEWER_TIMEOUT_SECONDS
    assert resolve_reviewer_timeout({"reviewer_timeout": 120}) == 120
    # A larger configured value is still clamped to the 900 budget.
    assert resolve_reviewer_timeout({"reviewer_timeout": 10_000}) == REVIEWER_TIMEOUT_SECONDS
    assert resolve_reviewer_timeout({"reviewer_timeout": "not a number"}) == 900
    assert resolve_reviewer_timeout(None) == 900


def test_reviewer_seat_trio_order_and_synthetic_down() -> None:
    names = [seat.model for seat in resolve_reviewer_candidates({})]
    assert names == ["kimi-k3:synthetic-anthropic", "nemotron-3-super:synthetic-anthropic"]
    down = resolve_reviewer_candidates({}, synthetic_up=False)
    assert [seat.model for seat in down] == ["ornith-1.5:pool"]
    configured = resolve_reviewer_candidates(
        {"reviewer_agent": "codex", "reviewer_model": ["m1", "m2"], "reviewer_mcp_profile": "audit"}
    )
    assert [(seat.agent, seat.model, seat.mcp_profile) for seat in configured] == [
        ("codex", "m1", "audit"),
        ("codex", "m2", "audit"),
    ]


# --- prompt + output contract ------------------------------------------------


def _inputs(**overrides: Any):
    from lh_harness.review import ReviewInputs

    base = {
        "workspace": "/tmp/runs/review/org/repo/1",
        "diff": "diff --git a/x b/x\n+code\n",
        "changed_files": ["src/a.py"],
        "gate_results": {"tests": {"failed": []}},
        "gate_results_error": None,
        "pr_body": "Add a thing.",
        "head_sha": "abc123",
        "base_ref": "main",
        "diff_error": None,
    }
    base.update(overrides)
    return ReviewInputs(**base)


def test_reviewer_prompt_carries_diff_gate_and_body() -> None:
    prompt = build_reviewer_prompt(_inputs(pr_body="Fixes the gate."))
    assert "read-only" in prompt
    assert "FIX" not in prompt or "Fixes" in prompt
    assert "src/a.py" in prompt
    assert "Fixes the gate" in prompt
    assert "```review" in prompt
    assert '"verdict": "pass" | "fail" | "cannot_review"' in prompt


def test_parse_review_output_pass_and_fail() -> None:
    raw = (
        "Some prose.\n"
        "```review\n"
        + json.dumps(
            {
                "verdict": "fail",
                "blocking": [{"file": "src/a.py", "line": 3, "why": "test_x fails"}],
                "findings": [{"file": "src/a.py", "line": 9, "severity": "low", "note": "nit"}],
                "evidence": ["gate report names test_x"],
            }
        )
        + "\n```"
    )
    review = parse_review_output(raw, served_model="kimi-k3:synthetic-anthropic")
    assert review["verdict"] == "fail"
    assert review["blocking"] == [{"file": "src/a.py", "line": 3, "why": "test_x fails"}]
    assert review["findings"][0]["severity"] == "low"
    assert review["model"] == "kimi-k3:synthetic-anthropic"

    nested = 'prefix {"verdict": "pass", "blocking": [], "findings": [{"file": "b.py", "line": 1, "severity": "low", "note": "n"}], "evidence": ["ok"]}'
    parsed = parse_review_output(nested, served_model="m")
    assert parsed["verdict"] == "pass"
    assert parsed["findings"][0]["file"] == "b.py"


def test_parse_review_output_degrades_to_cannot_review_never_pass() -> None:
    for raw in ("", "no verdict here", "```review\nnot json\n```", '{"verdict": "maybe"}'):
        review = parse_review_output(raw, served_model="m")
        assert review["verdict"] == "cannot_review"
        assert review["model"] == "m"
    inconsistent = parse_review_output(
        json.dumps({"verdict": "pass", "blocking": [{"file": "a", "line": 1, "why": "b"}]}),
        served_model="m",
    )
    assert inconsistent["verdict"] == "fail"
    hollow_fail = parse_review_output(
        json.dumps({"verdict": "fail", "blocking": []}), served_model="m"
    )
    assert hollow_fail["verdict"] == "cannot_review"


def test_review_report_matches_the_exact_contract_schema() -> None:
    report = review_report(
        {
            "verdict": "pass",
            "blocking": [],
            "findings": [],
            "evidence": ["diff reviewed"],
            "model": "kimi-k3:synthetic-anthropic",
            "duration_s": 12.3,
        },
        served_model="kimi-k3:synthetic-anthropic",
    )
    assert set(report) == {"verdict", "blocking", "findings", "evidence", "model", "duration_s"}
    assert report["model"] == "kimi-k3:synthetic-anthropic"
    assert report["duration_s"] == 12.3


# --- killed / failed runs -----------------------------------------------------


@pytest.mark.parametrize(
    "status,error",
    [
        ("timeout", None),
        ("error", "provider 500"),
        ("cancelled", None),
        ("cancelled", "SIGKILL"),
    ],
)
def test_killed_or_failed_episodes_are_cannot_review(status: str, error: str | None) -> None:
    result = EpisodeResult(status=status, actions_log="", error=error, duration_ms=45_000)
    review = review_from_episode_result(result, served_model="ornith-1.5:pool")
    assert review["verdict"] == "cannot_review"
    assert review["model"] == "ornith-1.5:pool"
    assert review["blocking"] == []
    assert review["duration_s"] == 45.0
    # The verdict must never be pass by default.
    assert review["verdict"] != "pass"


def test_done_episode_with_garbage_output_is_cannot_review() -> None:
    result = EpisodeResult(status="done", actions_log="trashed output", duration_ms=900)
    review = review_from_episode_result(result, served_model="m")
    assert review["verdict"] == "cannot_review"


def test_done_episode_with_real_verdict_passes_through() -> None:
    result = EpisodeResult(
        status="done",
        actions_log="noise\n```review\n"
        + json.dumps(
            {
                "verdict": "fail",
                "blocking": [{"file": "a.py", "line": 2, "why": "test_y fails"}],
                "findings": [],
                "evidence": [],
            }
        )
        + "\n```",
        duration_ms=1500,
    )
    review = review_from_episode_result(result, served_model="m")
    assert review["verdict"] == "fail"
    assert review["blocking"][0]["why"] == "test_y fails"
    assert review["duration_s"] == 1.5


# --- inputs + workspace -------------------------------------------------------


def test_collect_review_inputs_surfaces_unreadable_diff() -> None:
    spec = parse_review_spec(
        {
            "repo": "org/repo",
            "pr_number": 1,
            "head_sha": "abc123",
            "base_ref": "main",
            "gate_results_url": "https://x/gate.json",
        }
    )
    from lh_harness.review import ReviewCheckout

    checkout = ReviewCheckout(workspace=Path("/tmp/nowhere"), head_commit="abc", base_commit="def")

    def failing_runner(argv: list[str]) -> str:
        raise RuntimeError("git diff exploded")

    inputs = collect_review_inputs(
        spec,
        checkout,
        gate_results_url="",
        diff_runner=failing_runner,
    )
    assert inputs.diff_error is not None
    assert "git diff" in inputs.diff_error or "diff" in inputs.diff_error


def test_gate_fetch_failure_is_recorded_not_invented(monkeypatch: pytest.MonkeyPatch) -> None:
    from lh_harness.review import _fetch_gate_results

    def boom(url: str, timeout: int) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr("lh_harness.review.urllib.request.urlopen", boom)
    data, error = _fetch_gate_results("https://gate.example.com/pr/1.json")
    assert data == {}
    assert error is not None
    assert "could not be fetched" in error


def test_gate_fetch_parses_json(monkeypatch: pytest.MonkeyPatch) -> None:
    from lh_harness.review import _fetch_gate_results

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({"tests": {"failed": ["test_x"]}}).encode("utf-8")

    monkeypatch.setattr(
        "lh_harness.review.urllib.request.urlopen", lambda url, timeout: FakeResponse()
    )
    data, error = _fetch_gate_results("https://gate/gate.json")
    assert error is None
    assert data == {"tests": {"failed": ["test_x"]}}


def test_prepare_review_workspace_requires_real_fetch(tmp_path: Path) -> None:
    spec = parse_review_spec(
        {
            "repo": "org/repo",
            "pr_number": 1,
            "head_sha": "0123456789abcdef",
            "base_ref": "main",
            "gate_results_url": "https://x/gate.json",
        }
    )
    with pytest.raises(ReviewWorkspaceError):
        prepare_review_workspace(
            tmp_path / "runs", spec, remote_url="/nonexistent-origin.git"
        )


def test_read_review_report_roundtrip(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "review-x"
    (run_dir / "lh_harness").mkdir(parents=True)
    (run_dir / "lh_harness" / "review.json").write_text(
        json.dumps(review_report({"verdict": "pass"}, served_model="m"))
    )
    assert read_review_report(tmp_path / "runs", "review-x")["verdict"] == "pass"
    assert read_review_report(tmp_path / "runs", "missing") is None


# --- supervisor launch contract ------------------------------------------------


class _FakeProcess:
    pid = 4242
    returncode = None

    def __init__(self) -> None:
        self.wait_called = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_called = True
        return 0


@pytest.fixture()
def review_supervisor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> RunSupervisor:
    process: _FakeProcess = _FakeProcess()

    class _FakePopen:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.pid = 4321

        def poll(self) -> int | None:
            return process.returncode

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("lh_harness.supervisor.service.os.killpg", lambda *a, **k: None)
    root = tmp_path / "runs"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return RunSupervisor(root, workspace_root=workspace)


def test_create_review_run_forces_one_round_and_nine_hundred_seconds(
    review_supervisor: RunSupervisor,
) -> None:
    created = review_supervisor.create_review_run(
        spec={
            "repo": "org/repo",
            "pr_number": 186,
            "head_sha": "0123456789ab",
            "base_ref": "main",
            "gate_results_url": "https://gate/gate.json",
        }
    )
    owner = created["owner"]
    assert owner["max_rounds"] == REVIEW_MAX_ROUNDS == 1
    assert owner["reviewer_timeout_s"] == REVIEWER_TIMEOUT_SECONDS == 900
    assert created["run_kind"] == "review"
    assert created["owner"]["workspace"].endswith("/review/org/repo/186")
    command = json.loads(
        (review_supervisor._run_dir(created["id"]) / "control" / "owner.json").read_text()
    )["command"]
    assert "lh_harness.review" in " ".join(command)
    # The review worker command must never carry push/comment plumbing.
    joined = " ".join(command)
    assert "--subscription" not in joined
    assert "comment" not in joined
    assert "push" not in joined


def test_create_review_run_rejects_caller_knobs(
    review_supervisor: RunSupervisor,
) -> None:
    with pytest.raises(ValueError):
        review_supervisor.create_review_run(
            spec={
                "repo": "org/repo",
                "pr_number": 1,
                "head_sha": "0123456789ab",
                "base_ref": "main",
                "gate_results_url": "https://gate/gate.json",
                "max_rounds": 7,
            }
        )