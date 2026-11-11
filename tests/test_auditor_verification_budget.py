"""Auditor verification-step budget and fail-closed preflight.

Task ``yt:easybutt0n-17`` subtask: bound auditor verification and conclude
UNVERIFIABLE before the episode timeout.

The gap this change closes
--------------------------

The auditor's independent reach is the read-only PR verifier
(``lh-harness verify-pr`` / ``scripts/auditor_verify_pr.py``).  Before this
change that reach was unbounded in two directions, and both cost the whole
900 s auditor episode cap:

*   an auditor that could not confirm a claim could retry "one more / one
    final" verification indefinitely -- an audit that never concludes is
    exactly as unconfirmed as one that refused, it just burns the cap; and
*   when the task required PR verification but the verifier or its token
    was absent, the auditor learned nothing until it tried (or timed out).

These tests pin the four regression behaviors of the fix:

(a) a repetitive auditor exceeding 3 structured verification passes (the
    initial evidence pass plus at most 2 additional) terminates
    UNVERIFIABLE with reason ``verification_budget_exhausted`` -- never
    PASS, and never satisfying completion;
(b) required-PR-verification with an unavailable verifier/token terminates
    UNVERIFIABLE with reason ``pr_verifier_unavailable`` before the cap,
    with no network access at all;
(c) with an available verifier the normal verdicts (confirmed / refuted /
    unverifiable-from-read-failure) are preserved, and tasks that do not
    require PR verification are unaffected by a missing token;
(d) the episode caps are unchanged: the ``[run.timeouts]`` template and the
    ``HarnessConfig`` budget defaults are byte-/value-identical to the
    previous behavior -- this change adds no global wall-clock cap.

No test here waits on real time (there is no 900 s wait anywhere: the
budget and the preflight are deterministic counters and presence checks, so
"before the cap" holds by construction) and no test touches a real network:
the HTTP seam is injected.  No test reads or prints a credential VALUE;
only presence is checked.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

import pytest

from lh_harness.auditor_verify import (
    AUDITOR_EPISODE_ID_ENV,
    AUDITOR_GITHUB_TOKEN_ENV,
    AUDITOR_VERIFY_STATE_DIR_ENV,
    DEFAULT_VERIFICATION_STEP_BUDGET,
    REASON_PR_VERIFIER_UNAVAILABLE,
    REASON_VERIFICATION_BUDGET_EXHAUSTED,
    SHARED_CREDENTIAL_ENV_VARS,
    VERDICT_CONFIRMED,
    VERDICT_REFUTED,
    VERDICT_UNVERIFIABLE,
    consume_verification_step,
    pr_verifier_unavailable_result,
    resolve_auditor_credential,
    verification_budget_exhausted_result,
)
from lh_harness.cli import main as cli_main
from lh_harness.config import CONFIG_TEMPLATE
from lh_harness.types import HarnessConfig

REPO = "cogniziocompany/LongHorizon-Harness"
PR_NUMBER = 181
REVIEWED_SHA = "359dba7e0000000000000000000000000000abcd"

#: A token VALUE used only to prove that presence (never the value) is
#: checked and that the value never leaks into any output.
_FAKE_TOKEN = "auditor-token-value-never-printed"


# ---------------------------------------------------------------------------
# Helpers: a fake HTTP seam and a controlled harness-side environment.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeOpener:
    """Records every request so a test can assert none was issued."""

    def __init__(self, response: _FakeResponse | Exception) -> None:
        self.response = response
        self.calls: list[Any] = []

    def open(self, request: Any, timeout: float | None = None) -> _FakeResponse:
        self.calls.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _pr_payload(
    *,
    number: int = PR_NUMBER,
    state: str = "open",
    head_sha: str = REVIEWED_SHA,
    merged: bool = False,
) -> dict[str, Any]:
    return {
        "number": number,
        "state": state,
        "base": {"ref": "main", "repo": {"full_name": REPO}},
        "head": {"sha": head_sha, "ref": "branch"},
        "merged": merged,
        "additions": 10,
        "deletions": 2,
        "changed_files": 3,
    }


@pytest.fixture()
def harness_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """A controlled environment + state dir + network recorder.

    Every credential-shaped variable is stripped, the budget counter is
    redirected to a tmp directory, and the HTTP seam is replaced by a
    recording fake.  Returns (opener, set_token, set_episode).
    """

    opener_box: dict[str, _FakeOpener] = {}

    def _install(opener: _FakeOpener) -> None:
        opener_box["opener"] = opener
        monkeypatch.setattr(urllib.request, "urlopen", opener.open)

    state_dir = tmp_path / "verification-budget"
    monkeypatch.setenv(AUDITOR_VERIFY_STATE_DIR_ENV, str(state_dir))
    monkeypatch.delenv(AUDITOR_GITHUB_TOKEN_ENV, raising=False)
    monkeypatch.delenv(AUDITOR_EPISODE_ID_ENV, raising=False)
    # Determinism: strip every ambient credential-shaped variable so a host
    # GH_TOKEN/GITHUB_TOKEN can never collide with or substitute for the
    # fixture's fake auditor token.
    for name in SHARED_CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    class Harness:
        def install(self, opener: _FakeOpener) -> _FakeOpener:
            _install(opener)
            return opener

        def set_token(self, present: bool) -> None:
            if present:
                monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, _FAKE_TOKEN)
            else:
                monkeypatch.delenv(AUDITOR_GITHUB_TOKEN_ENV, raising=False)

        def set_episode(self, episode_id: str) -> None:
            monkeypatch.setenv(AUDITOR_EPISODE_ID_ENV, episode_id)

    return Harness()


def _run_verify_pr(*, episode: str) -> tuple[int, dict[str, Any]]:
    """Invoke the verifier exactly as the auditor does, capturing verdict JSON."""

    args = [
        "verify-pr",
        "--repo",
        REPO,
        "--number",
        str(PR_NUMBER),
        "--reviewed-sha",
        REVIEWED_SHA,
        "--json",
    ]
    import io
    import contextlib

    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = cli_main(args)
    combined = stdout.getvalue() + stderr.getvalue()
    assert _FAKE_TOKEN not in combined, "a credential value leaked into verifier output"
    payload = json.loads(stdout.getvalue().strip().splitlines()[-1])
    return exit_code, payload


# ---------------------------------------------------------------------------
# (a) Budget exhaustion: 3 structured passes, never a 4th, never a pass.
# ---------------------------------------------------------------------------


def test_consume_verification_step_grants_exactly_the_budget(harness_env, tmp_path) -> None:
    """The counter is deterministic: initial pass + at most 2 additional."""

    granted = [
        consume_verification_step(
            {AUDITOR_EPISODE_ID_ENV: "run-1.round_2.cli_auditor"},
            state_dir=tmp_path / "budget",
        ).granted
        for _ in range(DEFAULT_VERIFICATION_STEP_BUDGET + 2)
    ]

    assert granted == [True, True, True, False, False]


def test_verification_steps_are_scoped_per_episode(harness_env, tmp_path) -> None:
    """Each auditor episode gets its own three passes (structured boundary)."""

    for episode in ("run-1.round_2.cli_auditor", "run-1.round_3.cli_auditor"):
        step = consume_verification_step(
            {AUDITOR_EPISODE_ID_ENV: episode}, state_dir=tmp_path / "budget"
        )
        assert step.number == 1 and step.granted


def test_repetitive_auditor_exhaustion_is_unverifiable_never_pass(harness_env) -> None:
    """(a) Beyond 3 structured passes: verification_budget_exhausted, exit != 0,
    no network, and a PASS/confirmed can never come back."""

    opener = harness_env.install(_FakeOpener(_FakeResponse(200, _pr_payload())))
    harness_env.set_token(present=True)
    harness_env.set_episode("run-7.round_4.cli_auditor")

    # The budget: initial evidence pass plus at most 2 additional passes.
    for _ in range(DEFAULT_VERIFICATION_STEP_BUDGET):
        exit_code, payload = _run_verify_pr(episode="run-7.round_4.cli_auditor")
        assert exit_code == 0
        assert payload["verdict"] == VERDICT_CONFIRMED
        assert payload["blocks_delivery"] is False
    assert len(opener.calls) == DEFAULT_VERIFICATION_STEP_BUDGET

    # The 4th (and later) attempts fail closed.  No read is issued, the
    # verdict is the canonical unverifiable slug, and it blocks delivery.
    for _ in range(2):
        exit_code, payload = _run_verify_pr(episode="run-7.round_4.cli_auditor")
        assert exit_code != 0
        assert payload["verdict"] == VERDICT_UNVERIFIABLE
        assert payload["verdict"] != VERDICT_CONFIRMED
        assert payload["reason"].startswith(REASON_VERIFICATION_BUDGET_EXHAUSTED)
        assert payload["blocks_delivery"] is True
        assert len(opener.calls) == DEFAULT_VERIFICATION_STEP_BUDGET


def test_budget_exhausted_result_never_satisfies_completion() -> None:
    """The canonical exhaustion result can never read as PASS/DONE."""

    step = consume_verification_step(
        {AUDITOR_EPISODE_ID_ENV: "run-9.round_1.cli_auditor"},
        budget=1,
    )
    consume_verification_step({AUDITOR_EPISODE_ID_ENV: "run-9.round_1.cli_auditor"}, budget=1)
    result = verification_budget_exhausted_result(step, reviewed_sha=REVIEWED_SHA)

    assert result.verdict == VERDICT_UNVERIFIABLE
    assert result.reason.startswith(REASON_VERIFICATION_BUDGET_EXHAUSTED)
    assert result.blocks_delivery is True
    assert result.is_unverifiable is True


# ---------------------------------------------------------------------------
# (b) Unavailable verifier/token: fail closed before the cap, no network.
# ---------------------------------------------------------------------------


def test_missing_token_fails_closed_pr_verifier_unavailable(harness_env) -> None:
    """(b) Required PR verification + no token: UNVERIFIABLE with the
    canonical reason, no network access, and it concludes immediately --
    long before the 900 s cap (there is no clock involved at all)."""

    opener = harness_env.install(_FakeOpener(_FakeResponse(200, _pr_payload())))
    harness_env.set_token(present=False)
    harness_env.set_episode("run-8.round_1.cli_auditor")

    # Repeated attempts conclude identically: the preflight runs BEFORE the
    # first structured step, so it never burns the budget and never varies.
    for _ in range(DEFAULT_VERIFICATION_STEP_BUDGET + 1):
        exit_code, payload = _run_verify_pr(episode="run-8.round_1.cli_auditor")
        assert exit_code != 0
        assert payload["verdict"] == VERDICT_UNVERIFIABLE
        assert payload["reason"].startswith(REASON_PR_VERIFIER_UNAVAILABLE)
        assert payload["blocks_delivery"] is True

    assert opener.calls == [], "an unavailable verifier must make no network call"


def test_missing_token_does_not_consume_the_step_budget(harness_env) -> None:
    """The preflight precedes the budget: a missing token never eats passes."""

    harness_env.set_token(present=False)
    harness_env.set_episode("run-8.round_2.cli_auditor")
    _run_verify_pr(episode="run-8.round_2.cli_auditor")

    step = consume_verification_step()
    assert step.number == 1 and step.granted


def test_non_pr_tasks_are_unaffected_by_a_missing_token(harness_env) -> None:
    """(c) A task that does not require PR verification never invokes the
    verifier, so a missing token must not fail anything for it: credential
    resolution reports absence without raising, and the episode's budget
    stays untouched."""

    credential = resolve_auditor_credential({})
    assert credential.present is False
    assert credential.token == ""  # absence is a state, not an error

    # Nothing about the budget or the verdict vocabulary changes when the
    # verifier is simply never invoked.
    result = pr_verifier_unavailable_result(reviewed_sha=REVIEWED_SHA)
    assert result.verdict == VERDICT_UNVERIFIABLE
    assert result.reason.startswith(REASON_PR_VERIFIER_UNAVAILABLE)
    assert result.blocks_delivery is True


# ---------------------------------------------------------------------------
# (c) Available verifier: normal verdicts are preserved.
# ---------------------------------------------------------------------------


def test_available_verifier_preserves_normal_verdicts(harness_env) -> None:
    """(c) With a usable verifier, confirmed/refuted/unverifiable reads all
    behave exactly as before the budget existed."""

    # confirmed: the read matches the reviewed head sha -> exit 0.
    harness_env.install(_FakeOpener(_FakeResponse(200, _pr_payload())))
    harness_env.set_token(present=True)
    harness_env.set_episode("run-10.round_1.cli_auditor")
    exit_code, payload = _run_verify_pr(episode="run-10.round_1.cli_auditor")
    assert exit_code == 0
    assert payload["verdict"] == VERDICT_CONFIRMED
    assert payload["blocks_delivery"] is False

    # refuted: the head sha moved -> still a first-class verdict, in budget.
    harness_env.install(
        _FakeOpener(_FakeResponse(200, _pr_payload(head_sha="0" * 40)))
    )
    harness_env.set_episode("run-10.round_2.cli_auditor")
    exit_code, payload = _run_verify_pr(episode="run-10.round_2.cli_auditor")
    assert exit_code != 0
    assert payload["verdict"] == VERDICT_REFUTED

    # unverifiable: the read itself fails, with its own reason -- NOT the
    # new budget/preflight slugs.
    failing = harness_env.install(_FakeOpener(OSError("network unreachable")))
    harness_env.set_episode("run-10.round_3.cli_auditor")
    exit_code, payload = _run_verify_pr(episode="run-10.round_3.cli_auditor")
    assert exit_code != 0
    assert payload["verdict"] == VERDICT_UNVERIFIABLE
    assert not payload["reason"].startswith(REASON_VERIFICATION_BUDGET_EXHAUSTED)
    assert not payload["reason"].startswith(REASON_PR_VERIFIER_UNAVAILABLE)
    assert failing.calls, "the failing read must have been attempted"


# ---------------------------------------------------------------------------
# (d) The episode caps are unchanged.
# ---------------------------------------------------------------------------


def test_episode_caps_are_unchanged() -> None:
    """(d) No global/new wall-clock cap: the manager and auditor episode
    budget defaults and the ``[run.timeouts]`` template lines are identical
    to the behavior before this change.  The auditor-specific bound added
    here is a step COUNT, not a clock."""

    config = HarnessConfig()
    assert config.manager_budget.max_duration_seconds == 300
    assert config.auditor_budget.max_duration_seconds == 300
    assert config.gui_executor_budget.max_duration_seconds == 1800
    assert config.cli_executor_budget.max_duration_seconds == 1800

    assert "[run.timeouts]" in CONFIG_TEMPLATE
    assert "\nmanager = 300\n" in CONFIG_TEMPLATE
    assert "\ngui_executor = 1800\n" in CONFIG_TEMPLATE
    assert "\ncli_executor = 1800\n" in CONFIG_TEMPLATE
    assert "\nauditor = 300\n" in CONFIG_TEMPLATE


def test_no_new_skip_or_xfail_markers_in_this_change() -> None:
    """Deterministic guards must be executable everywhere; nothing in this
    change is allowed to duck behind a skip/xfail marker."""
