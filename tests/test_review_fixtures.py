"""Fixture-PR review tests: hermetic end-to-end verdict paths.

Each fixture is a tiny repo tarball under ``tests/fixtures/review`` whose PR
head is exported as ``refs/pull/N/head`` on a local mirror "origin", so the
real ``prepare_review_workspace`` fetch path runs without any network. The
gate results and the PR body come from a loopback HTTP server, and the
reviewer seat is a scripted reviewer that behaves like the real one: it runs
the fixture's tests inside the read-only checkout and derives its verdict
from what it observes (never a canned verdict unrelated to the inputs).

Covers the three verdict paths:
- pass  (fixture ``pr-pass``: docs-only change, clean tests, passing gate)
- fail  (fixture ``pr-fail``: broken ``add``, failing test named in blocking)
- cannot_review (killed reviewer episode — never ``pass``)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from lh_harness.review import parse_review_spec, read_review_report, run_review_session
from lh_harness.types import EpisodeResult

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "review"


# --- loopback gate + PR body server -------------------------------------------


class _GateHandler(BaseHTTPRequestHandler):
    """Serves canned gate-results JSON and PR bodies for fixture repos."""

    gate_payload: dict[str, Any] = {}
    pr_payload: dict[str, Any] = {}

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path.startswith("/gate/"):
            body = json.dumps(self.gate_payload).encode("utf-8")
        elif re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/\d+", self.path):
            body = json.dumps(self.pr_payload).encode("utf-8")
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture()
def gate_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GateHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


# --- fixture helpers -----------------------------------------------------------


def _extract_fixture(name: str, tmp_path: Path) -> Path:
    """Extract a fixture tarball into ``tmp_path`` and return the repo dir."""

    repo = tmp_path / name
    with tarfile.open(FIXTURES_DIR / f"{name}.tar.gz", "r:gz") as archive:
        archive.extractall(tmp_path, filter="data")
    return repo


def _mirror_origin(repo: Path, tmp_path: Path, pr_number: int) -> Path:
    """Mirror-clone the fixture repo so its ``refs/pull/N/head`` is fetchable.

    The mirror is named ``<repo>.git`` so that ``repo_remote_url``'s join
    (``<host>/<repo>.git``) resolves to it when the review git host env var
    points at the mirror's parent directory.
    """

    origin = tmp_path / f"{repo.name}.git"
    subprocess.run(
        ["git", "clone", "-q", "--mirror", str(repo), str(origin)],
        check=True,
        capture_output=True,
        text=True,
    )
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(origin), "update-ref", f"refs/pull/{pr_number}/head", head],
        check=True,
        capture_output=True,
        text=True,
    )
    return origin


def _head_sha(repo: Path, pr_number: int) -> str:
    """The PR head sha: refs/pull/N/head, i.e. the tip of the PR branch."""

    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"refs/pull/{pr_number}/head"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _verdict_block(review: dict[str, Any]) -> str:
    return "```review\n" + json.dumps(review) + "\n```"


def _scripted_reviewer(prompts: list[str], *, run_tests: bool):
    """A reviewer that decides from observation, like the real seat.

    It runs the fixture's own test suite inside the read-only checkout and
    fails the PR when a test fails, naming the failing test in the blocking
    entry. Nothing about the verdict is canned: a fixture whose tests pass
    reviews as ``pass``, a fixture with a failing test as ``fail``.
    """

    async def _reviewer(prompt: str, timeout_s: int) -> EpisodeResult:
        prompts.append(prompt)
        match = re.search(r"checkout of the PR head at (\S+?)\.?\s*\n", prompt)
        assert match, "reviewer prompt must carry the checkout path"
        workspace = match.group(1)
        evidence: list[str] = ["read the diff and changed files"]
        if run_tests:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q"],
                cwd=workspace,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": workspace},
                capture_output=True,
                text=True,
                timeout=120,
            )
            failed = [
                line.split("FAILED ", 1)[1].split(" ", 1)[0]
                for line in proc.stdout.splitlines()
                if line.startswith("FAILED ")
            ]
            if proc.returncode == 1 and failed:
                return EpisodeResult(
                    status="done",
                    actions_log=_verdict_block(
                        {
                            "verdict": "fail",
                            "blocking": [
                                {
                                    "file": "src/app.py",
                                    "line": 2,
                                    "why": f"{failed[0]} fails in the PR checkout",
                                }
                            ],
                            "findings": [],
                            "evidence": ["pytest inside the checkout failed"],
                        }
                    ),
                    duration_ms=1_000,
                )
            evidence.append(f"pytest clean (exit {proc.returncode})")
        return EpisodeResult(
            status="done",
            actions_log=_verdict_block(
                {"verdict": "pass", "blocking": [], "findings": [], "evidence": evidence}
            ),
            duration_ms=1_000,
        )

    return _reviewer


def _spec(repo: str, pr_number: int, head_sha: str, gate_url: str) -> dict[str, Any]:
    return {
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "base_ref": "main",
        "gate_results_url": gate_url,
    }


async def _run_fixture_review(
    fixture: str,
    pr_number: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_url: str,
    pr_body: str,
    *,
    run_tests: bool,
    prompts: list[str],
) -> dict[str, Any]:
    repo = _extract_fixture(fixture, tmp_path)
    origin = _mirror_origin(repo, tmp_path, pr_number)
    monkeypatch.setenv("LH_HARNESS_REVIEW_GIT_HOST", f"file://{origin.parent}")
    monkeypatch.setenv("LH_HARNESS_REVIEW_API_HOST", gate_url)
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    config = {"reviewer_timeout": 120}
    report = await run_review_session(
        spec=parse_review_spec(_spec(fixture, pr_number, _head_sha(repo, pr_number), gate_url)),
        run_dir=runs_root / f"run-{fixture}",
        runs_root=runs_root,
        config=config,
        episode_runner=_scripted_reviewer(prompts, run_tests=run_tests),
    )
    return report


# --- the three verdict paths ----------------------------------------------------


@pytest.mark.asyncio
async def test_fixture_pr_pass_reviews_as_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate_server: str
) -> None:
    _GateHandler.gate_payload = {"gate": {"verdict": "pass", "tests": {"failed": []}}}
    _GateHandler.pr_payload = {"body": "Documents the add helper."}
    prompts: list[str] = []
    report = await _run_fixture_review(
        "pr-pass", 1, tmp_path, monkeypatch, f"{gate_server}/gate/pr-pass.json",
        "Documents the add helper.", run_tests=True, prompts=prompts,
    )
    assert report["verdict"] == "pass"
    assert report["blocking"] == []
    assert report["model"]  # the served model is recorded
    assert isinstance(report["duration_s"], (int, float))
    # The reviewer saw the whole pipeline: diff, gate JSON, PR body.
    assert "docs/note.md" in prompts[0]
    assert '"gate"' in prompts[0]
    assert "Documents the add helper." in prompts[0]
    # review.json is on disk under the run's report dir with the exact schema.
    stored = read_review_report(tmp_path / "runs", f"run-pr-pass")
    assert stored is not None
    assert set(stored) == {"verdict", "blocking", "findings", "evidence", "model", "duration_s"}
    assert stored["verdict"] == "pass"


@pytest.mark.asyncio
async def test_fixture_pr_fail_names_the_failing_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate_server: str
) -> None:
    _GateHandler.gate_payload = {
        "gate": {
            "verdict": "fail",
            "tests": {"failed": ["tests/test_app.py::test_add_returns_sum"]},
        }
    }
    _GateHandler.pr_payload = {"body": "Swaps the operator in add."}
    prompts: list[str] = []
    report = await _run_fixture_review(
        "pr-fail", 2, tmp_path, monkeypatch, f"{gate_server}/gate/pr-fail.json",
        "Swaps the operator in add.", run_tests=True, prompts=prompts,
    )
    assert report["verdict"] == "fail"
    assert report["blocking"], "a fail must carry at least one blocking entry"
    assert "test_add_returns_sum" in report["blocking"][0]["why"]
    assert report["model"]
    # The gate report naming the failing test reached the reviewer prompt too.
    assert "test_add_returns_sum" in prompts[0]
    stored = read_review_report(tmp_path / "runs", f"run-pr-fail")
    assert stored is not None and stored["verdict"] == "fail"


@pytest.mark.asyncio
async def test_killed_fixture_run_is_cannot_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate_server: str
) -> None:
    _GateHandler.gate_payload = {"gate": {"verdict": "pass", "tests": {"failed": []}}}
    _GateHandler.pr_payload = {"body": ""}

    async def killed_reviewer(prompt: str, timeout_s: int) -> EpisodeResult:
        # The reviewer seat is killed mid-episode; no verdict text exists.
        return EpisodeResult(status="cancelled", actions_log="", duration_ms=2_000)

    repo = _extract_fixture("pr-pass", tmp_path)
    origin = _mirror_origin(repo, tmp_path, 1)
    monkeypatch.setenv("LH_HARNESS_REVIEW_GIT_HOST", f"file://{origin.parent}")
    monkeypatch.setenv("LH_HARNESS_REVIEW_API_HOST", gate_server)
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    report = await run_review_session(
        spec=parse_review_spec(_spec("pr-pass", 1, _head_sha(repo, 1), f"{gate_server}/gate/pr-pass.json")),
        run_dir=runs_root / "run-killed",
        runs_root=runs_root,
        config={"reviewer_timeout": 120},
        episode_runner=killed_reviewer,
    )
    # A killed run can only ever be cannot_review - never pass.
    assert report["verdict"] == "cannot_review"
    assert report["blocking"] == []
    assert report["model"]
    stored = read_review_report(tmp_path / "runs", "run-killed")
    assert stored is not None and stored["verdict"] == "cannot_review"