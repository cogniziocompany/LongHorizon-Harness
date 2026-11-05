"""Auditor read-only GitHub verification surface (task yt:easybutt0n-17:closeout-validation-chain).

The harness is a validation chain: each step validates the last in its process
to achieve its tasks.  The auditor is the last independent verification step,
so it must be able to CONFIRM or REFUTE an executor's claim about the outside
world -- not merely restate it.

Before this module the auditor had no GitHub reach at all: its MCP profile
lists only inherently non-mutating servers and its tool deny-list refused
``Bash(gh *)`` wholesale.  An auditor with no independent evidence could only
restate executor claims or refuse them.

These tests pin the read-only properties that make the surface usable:

*   a real read confirms a PR's number / state / head sha;
*   a head-sha mismatch against the sha the auditor reviewed is caught;
*   a read failure yields an explicit UNVERIFIABLE result that blocks
    delivery -- never a silent pass and never a confirmed value;
*   a write intent is REFUSED by the enforcement layer, including the
    ``gh api`` paths a bare ``Bash(gh *)`` deny would leave unmatched
    (``gh api --method=POST``, a field-supplying ``gh api`` body);
*   the auditor's credential is its OWN fine-grained read-only token and is
    refused when it collides with the executor's ``GH_TOKEN`` or a fleet
    admin token.

The per-spelling proof that every write spelling is refused INDIVIDUALLY --
one named test per spelling, so no single generic test can stand in for the
set -- lives in ``test_auditor_read_only_spelling_coverage.py``.  The proof
that the auditor can actually INVOKE the surface lives in
``test_auditor_verification_reach.py``.

No test here reads a real network: the HTTP seam is injected.  No test prints
a credential value.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lh_harness.auditor_verify import (
    AUDITOR_GITHUB_TOKEN_ENV,
    VERDICT_CONFIRMED,
    VERDICT_REFUTED,
    VERDICT_UNVERIFIABLE,
    AuditorCredentialError,
    ReadOnlyViolation,
    ReadOnlyGitHubClient,
    resolve_auditor_credential,
    verify_pull_request,
)

REPO = "cogniziocompany/LongHorizon-Harness"
PR_NUMBER = 181
REVIEWED_SHA = "359dba7e0000000000000000000000000000abcd"


# ---------------------------------------------------------------------------
# A tiny fake for the HTTP seam.
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
    """Records every request so a test can assert nothing was written."""

    def __init__(self, response: _FakeResponse | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def open(self, request: Any, timeout: float | None = None) -> _FakeResponse:
        self.calls.append(
            {
                "method": getattr(request, "method", None),
                "full_url": getattr(request, "full_url", None),
                "headers": dict(getattr(request, "headers", {}) or {}),
                "data": getattr(request, "data", None),
            }
        )
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    @property
    def methods(self) -> list[str | None]:
        return [call["method"] for call in self.calls]

    @property
    def urls(self) -> list[str | None]:
        return [call["full_url"] for call in self.calls]


def _pr_payload(
    *,
    number: int = PR_NUMBER,
    state: str = "OPEN",
    base: str = "main",
    head_sha: str = REVIEWED_SHA,
    additions: int = 10,
    deletions: int = 2,
) -> dict[str, Any]:
    return {
        "number": number,
        "state": state,
        "base": {"ref": base, "repo": {"full_name": REPO}},
        "head": {"sha": head_sha, "ref": "feat/close-out-validation-chain"},
        "merged": False,
        "additions": additions,
        "deletions": deletions,
        "changed_files": 3,
    }


def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: _FakeResponse | Exception | None = None,
    token: str = "test-auditor-readonly-token-000",
) -> tuple[ReadOnlyGitHubClient, _FakeOpener]:
    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, token)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    opener = _FakeOpener(response if response is not None else _FakeResponse(200, _pr_payload()))
    client = ReadOnlyGitHubClient(opener=opener.open)
    return client, opener


# ---------------------------------------------------------------------------
# 1. A real read confirms number / state / head sha.
# ---------------------------------------------------------------------------


def test_read_pull_request_confirms_number_state_and_head_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, opener = _client(monkeypatch)

    facts = client.read_pull_request(REPO, PR_NUMBER)

    assert facts.number == PR_NUMBER
    assert facts.state == "OPEN"
    assert facts.head_sha == REVIEWED_SHA
    assert facts.base_ref == "main"
    assert facts.repo == REPO
    assert facts.additions == 10
    assert facts.deletions == 2
    assert facts.changed_files == 3
    # The read was a GET against the pull-request read path.
    assert opener.methods == ["GET"]
    assert opener.urls == [
        f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}"
    ]


def test_read_uses_only_the_auditor_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, opener = _client(monkeypatch, token="test-auditor-readonly-token-000")

    client.read_pull_request(REPO, PR_NUMBER)

    headers = {k.lower(): v for k, v in opener.calls[0]["headers"].items()}
    authorization = headers.get("authorization", "")
    assert authorization.startswith("Bearer ")
    assert "test-auditor-readonly-token-000" in authorization
    assert headers.get("accept") == "application/vnd.github+json"


# ---------------------------------------------------------------------------
# 2. A head-sha mismatch against the reviewed sha is caught.
# ---------------------------------------------------------------------------


def test_verify_refutes_a_head_sha_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other_sha = "0000000000000000000000000000000000000000"
    client, _ = _client(
        monkeypatch, response=_FakeResponse(200, _pr_payload(head_sha=other_sha))
    )

    result = verify_pull_request(
        client,
        repo=REPO,
        number=PR_NUMBER,
        reviewed_sha=REVIEWED_SHA,
    )

    assert result.verdict == VERDICT_REFUTED
    assert result.reviewed_sha == REVIEWED_SHA
    assert result.observed_sha == other_sha
    assert result.observed_sha != REVIEWED_SHA
    assert "sha" in result.reason.lower()
    # A refutation blocks delivery just as an unverifiable does.
    assert result.blocks_delivery is True


def test_verify_confirms_a_matching_head_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _client(monkeypatch)

    result = verify_pull_request(
        client,
        repo=REPO,
        number=PR_NUMBER,
        reviewed_sha=REVIEWED_SHA,
    )

    assert result.verdict == VERDICT_CONFIRMED
    assert result.blocks_delivery is False
    assert result.observed_sha == REVIEWED_SHA


def test_verify_refutes_a_closed_pull_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _client(
        monkeypatch, response=_FakeResponse(200, _pr_payload(state="CLOSED"))
    )

    result = verify_pull_request(
        client,
        repo=REPO,
        number=PR_NUMBER,
        reviewed_sha=REVIEWED_SHA,
    )

    assert result.verdict == VERDICT_REFUTED
    assert result.blocks_delivery is True
    assert "closed" in result.reason.lower()


# ---------------------------------------------------------------------------
# 3. A read failure yields an explicit UNVERIFIABLE result that blocks
#    delivery -- never a silent pass, never a confirmed value.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        OSError("network unreachable"),
        _FakeResponse(404, {"message": "Not Found"}),
        _FakeResponse(403, {"message": "Forbidden"}),
        _FakeResponse(500, {"message": "server error"}),
    ],
)
def test_read_failure_is_explicitly_unverifiable_and_blocks_delivery(
    monkeypatch: pytest.MonkeyPatch, failure: Any
) -> None:
    client, _ = _client(monkeypatch, response=failure)

    result = verify_pull_request(
        client,
        repo=REPO,
        number=PR_NUMBER,
        reviewed_sha=REVIEWED_SHA,
    )

    assert result.verdict == VERDICT_UNVERIFIABLE
    assert result.blocks_delivery is True
    # Never a silent pass: the observed sha is unknown, not assumed equal.
    assert result.observed_sha is None
    assert result.reason


def test_unverifiable_is_distinct_from_refuted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _client(monkeypatch, response=OSError("dns failure"))

    result = verify_pull_request(
        client,
        repo=REPO,
        number=PR_NUMBER,
        reviewed_sha=REVIEWED_SHA,
    )

    assert result.verdict not in (VERDICT_CONFIRMED, VERDICT_REFUTED)
    assert result.verdict == VERDICT_UNVERIFIABLE


# ---------------------------------------------------------------------------
# 4. A write intent is REFUSED by the enforcement layer.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["gh", "pr", "merge", "181"],
        ["gh", "pr", "comment", "181", "--body", "lgtm"],
        ["gh", "pr", "close", "181"],
        ["gh", "pr", "edit", "181", "--title", "x"],
        ["gh", "pr", "create", "--title", "x"],
        ["gh", "issue", "create", "--title", "x"],
        ["gh", "api", "--method=POST", "repos/o/r/issues/1/comments"],
        ["gh", "api", "-X", "POST", "repos/o/r/issues/1/comments"],
        ["gh", "api", "--method", "PATCH", "repos/o/r/pulls/181"],
        ["gh", "api", "-X", "PUT", "repos/o/r/pulls/181/merge"],
        ["gh", "api", "-X", "DELETE", "repos/o/r/pulls/181"],
        ["gh", "api", "-X", "POST", "repos/o/r/issues/1/comments", "-f", "body=lgtm"],
        ["gh", "api", "repos/o/r/issues/1/comments", "-f", "body=lgtm"],
        ["gh", "api", "repos/o/r/issues/1/comments", "-F", "body=lgtm"],
        ["gh", "api", "repos/o/r/issues/1/comments", "--input", "payload.json"],
        ["git", "push", "origin", "feat/x"],
        ["gh", "release", "create", "v1"],
        # Write leaves hiding under a read group.
        ["gh", "pr", "review", "--approve", "1"],
        ["gh", "pr", "review", "--request-changes", "1"],
        ["gh", "secret", "set", "K", "V"],
        ["gh", "variable", "set", "K", "V"],
        ["gh", "workflow", "run", "ci.yml"],
        ["gh", "workflow", "dispatch", "ci.yml"],
        ["gh", "run", "rerun", "1"],
        ["gh", "repo", "edit", "o/r"],
        ["gh", "issue", "comment", "1", "--body", "x"],
        ["gh", "run", "cancel", "1"],
        ["gh", "run", "delete", "1"],
        ["gh", "api", "-XPOST", "repos/o/r/pulls/1"],
        ["gh", "api", "--method", "post", "repos/o/r/pulls/1"],
        ["git", "config", "user.name", "x"],
        ["git", "remote", "add", "x", "y"],
        ["git", "reset", "--hard"],
        ["curl", "-X", "POST", "https://api.github.com/x"],
        ["gh", "unknowncmd"],
        # A conflicting method specification is refused rather than guessed.
        ["gh", "api", "--method=POST", "-X", "GET", "repos/o/r/pulls/1"],
        # The credential groups are refused WHOLE, including their read leaves:
        # ``gh secret list`` names the repository's secrets, and the ticket
        # (easybutt0n-17, PR #187) requires every ``gh secret`` leaf to be
        # refused rather than only the writing ones.
        ["gh", "secret", "list"],
    ],
)
def test_write_intents_are_refused(argv: list[str]) -> None:
    from lh_harness.auditor_verify import assert_read_only_command

    with pytest.raises(ReadOnlyViolation) as excinfo:
        assert_read_only_command(argv)

    message = str(excinfo.value).lower()
    assert "read-only" in message
    # The refusal names the offending intent without echoing any credential.
    assert "token" not in message or "ghp_" not in message


@pytest.mark.parametrize(
    "argv",
    [
        ["gh", "pr", "view", "181", "--json", "number,state,headRefOid"],
        ["gh", "pr", "list", "--state", "open", "--json", "number"],
        ["gh", "api", "repos/o/r/pulls/181"],
        ["gh", "api", "repos/o/r/pulls/181", "--method=GET"],
        ["gh", "api", "-X", "GET", "repos/o/r/pulls/181"],
        ["gh", "api", "repos/o/r/pulls/181/files"],
        ["gh", "repo", "view", "o/r"],
        ["git", "status", "--porcelain"],
        ["git", "log", "--oneline", "-1"],
        ["gh", "run", "view", "1"],
        ["gh", "run", "list"],
        ["gh", "workflow", "view", "ci.yml"],
        ["gh", "variable", "list"],
        ["gh", "issue", "view", "1"],
        ["git", "rev-parse", "HEAD"],
        ["git", "diff", "origin/main...HEAD"],
    ],
)
def test_read_intents_are_allowed(argv: list[str]) -> None:
    from lh_harness.auditor_verify import assert_read_only_command

    assert_read_only_command(argv)  # must not raise


def test_write_attempt_through_the_client_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client itself has no write method; a dynamic lookup is refused."""

    client, _ = _client(monkeypatch)

    with pytest.raises(ReadOnlyViolation):
        client.write_pull_request(REPO, PR_NUMBER, {"title": "x"})


def test_client_refuses_a_write_verb_on_its_request_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _client(monkeypatch)

    with pytest.raises(ReadOnlyViolation):
        client.request("POST", f"/repos/{REPO}/pulls/{PR_NUMBER}")


# ---------------------------------------------------------------------------
# 5. The auditor credential is its OWN read-only token.
# ---------------------------------------------------------------------------


def test_auditor_credential_comes_from_its_own_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "test-dedicated-auditor-token-00")
    monkeypatch.setenv("GH_TOKEN", "test-executor-token-000000000000000")

    credential = resolve_auditor_credential()

    assert credential.token == "test-dedicated-auditor-token-00"
    assert credential.source == AUDITOR_GITHUB_TOKEN_ENV
    assert credential.present is True


def test_auditor_credential_is_refused_when_it_is_the_executor_gh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = "test-shared-token-000000000000000000"
    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, shared)
    monkeypatch.setenv("GH_TOKEN", shared)

    with pytest.raises(AuditorCredentialError) as excinfo:
        resolve_auditor_credential()

    message = str(excinfo.value)
    assert AUDITOR_GITHUB_TOKEN_ENV in message
    assert "GH_TOKEN" in message
    # The offending VALUE is never echoed.
    assert shared not in message


def test_auditor_credential_is_refused_when_it_is_the_executor_github_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = "test-shared-token-000000000000000000"
    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, shared)
    monkeypatch.setenv("GITHUB_TOKEN", shared)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    with pytest.raises(AuditorCredentialError):
        resolve_auditor_credential()


def test_auditor_credential_is_refused_when_it_is_the_fleet_admin_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = "test-fleet-admin-token-000000000000"
    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, admin)
    monkeypatch.setenv("GH_TOKEN", admin)
    monkeypatch.setenv("GITHUB_TOKEN", admin)
    monkeypatch.setenv("LH_HARNESS_FLEET_GITHUB_TOKEN", admin)

    with pytest.raises(AuditorCredentialError):
        resolve_auditor_credential()


def test_absent_auditor_credential_is_reported_as_absent_not_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(AUDITOR_GITHUB_TOKEN_ENV, raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("LH_HARNESS_FLEET_GITHUB_TOKEN", raising=False)

    credential = resolve_auditor_credential()

    assert credential.present is False
    assert credential.token == ""


def test_a_write_capable_credential_source_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auditor credential must be a read-only token, not a write-capable one."""

    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "test-write-capable-token-000000")
    monkeypatch.setenv("LH_HARNESS_AUDITOR_GITHUB_TOKEN_SCOPE", "contents:write,repo")

    with pytest.raises(AuditorCredentialError) as excinfo:
        resolve_auditor_credential()

    message = str(excinfo.value).lower()
    assert "write" in message or "read-only" in message


def test_credential_never_prints_its_value(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "test-printable-secret-000000000"
    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, secret)

    credential = resolve_auditor_credential()

    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert secret not in repr(credential)
    assert secret not in str(credential)


# ---------------------------------------------------------------------------
# 5b. The CLIENT refuses a shared credential, and refuses to exist without
#     its own.
# ---------------------------------------------------------------------------


def test_client_construction_refuses_a_shared_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = "test-shared-token-000000000000000000"
    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, shared)
    monkeypatch.setenv("GH_TOKEN", shared)

    with pytest.raises(AuditorCredentialError):
        ReadOnlyGitHubClient(opener=lambda request, timeout=None: None)


def test_client_from_environment_requires_the_auditor_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(AUDITOR_GITHUB_TOKEN_ENV, raising=False)

    with pytest.raises(AuditorCredentialError) as excinfo:
        ReadOnlyGitHubClient.from_environment()

    message = str(excinfo.value)
    assert AUDITOR_GITHUB_TOKEN_ENV in message
    assert "unverifiable" in message.lower()


def test_client_from_environment_uses_the_auditor_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "test-dedicated-auditor-token-00")
    monkeypatch.setenv("GH_TOKEN", "test-executor-token-000000000000000")

    client = ReadOnlyGitHubClient.from_environment()

    assert client._token == "test-dedicated-auditor-token-00"  # noqa: SLF001


def test_client_never_exposes_its_token_in_its_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "test-repr-secret-0000000000000"
    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, secret)

    client = ReadOnlyGitHubClient(opener=lambda request, timeout=None: None)

    assert secret not in repr(client)
    assert secret not in str(client)


# ---------------------------------------------------------------------------
# 6. The CLI surface the auditor actually reaches.
# ---------------------------------------------------------------------------


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: _FakeResponse | Exception | None = None,
) -> _FakeOpener:
    """Point the CLI command at a fake HTTP seam.

    The seam is the HTTP opener, not the client class: the CLI builds the
    client itself (so the credential resolution under test stays real), and
    only the network call is replaced.
    """

    opener = _FakeOpener(response if response is not None else _FakeResponse(200, _pr_payload()))
    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "test-cli-auditor-token-00000000")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    import lh_harness.auditor_verify as verify_module

    real_init = verify_module.ReadOnlyGitHubClient.__init__

    def _init(self: Any, **kwargs: Any) -> None:
        if kwargs.get("opener") is None:
            kwargs["opener"] = opener.open
        real_init(self, **kwargs)

    monkeypatch.setattr(verify_module.ReadOnlyGitHubClient, "__init__", _init)
    return opener


def test_cli_verify_pr_confirms_a_matching_sha(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from lh_harness import cli as cli_module

    _install_fake_client(monkeypatch)

    exit_code = cli_module.main(
        [
            "verify-pr",
            "--repo",
            REPO,
            "--number",
            str(PR_NUMBER),
            "--reviewed-sha",
            REVIEWED_SHA,
        ]
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert VERDICT_CONFIRMED in out


def test_cli_verify_pr_exits_non_zero_on_a_sha_mismatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from lh_harness import cli as cli_module

    other_sha = "0000000000000000000000000000000000000000"
    _install_fake_client(
        monkeypatch, response=_FakeResponse(200, _pr_payload(head_sha=other_sha))
    )

    exit_code = cli_module.main(
        [
            "verify-pr",
            "--repo",
            REPO,
            "--number",
            str(PR_NUMBER),
            "--reviewed-sha",
            REVIEWED_SHA,
        ]
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert VERDICT_REFUTED in out


def test_cli_verify_pr_exits_non_zero_when_the_read_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unreachable verifier must not exit 0 -- that would be a silent pass."""

    from lh_harness import cli as cli_module

    _install_fake_client(monkeypatch, response=OSError("network unreachable"))

    exit_code = cli_module.main(
        [
            "verify-pr",
            "--repo",
            REPO,
            "--number",
            str(PR_NUMBER),
            "--reviewed-sha",
            REVIEWED_SHA,
        ]
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert VERDICT_UNVERIFIABLE in out


def test_cli_verify_pr_without_the_auditor_credential_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from lh_harness import cli as cli_module

    monkeypatch.delenv(AUDITOR_GITHUB_TOKEN_ENV, raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    exit_code = cli_module.main(
        [
            "verify-pr",
            "--repo",
            REPO,
            "--number",
            str(PR_NUMBER),
            "--reviewed-sha",
            REVIEWED_SHA,
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert AUDITOR_GITHUB_TOKEN_ENV in captured.err
    assert "unverifiable" in captured.err.lower()


def test_cli_verify_pr_json_output_carries_the_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from lh_harness import cli as cli_module

    _install_fake_client(monkeypatch)

    cli_module.main(
        [
            "verify-pr",
            "--repo",
            REPO,
            "--number",
            str(PR_NUMBER),
            "--reviewed-sha",
            REVIEWED_SHA,
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == VERDICT_CONFIRMED
    assert payload["blocks_delivery"] is False
    assert payload["observed_sha"] == REVIEWED_SHA


# ---------------------------------------------------------------------------
# 7. The auditor's contract header and the close-out vocabulary are untouched.
# ---------------------------------------------------------------------------


def test_auditor_contract_header_set_is_unchanged() -> None:
    from lh_harness.auditor_agent import _CONTROL_HEADER

    assert _CONTROL_HEADER["en"] == ("Status", "Integrity", "Contract audit")


def test_no_new_close_out_outcome_vocabulary_is_introduced() -> None:
    """The close-out outcome vocabulary belongs to the sibling task.

    Prose may MENTION these words (the module docstring explains what a
    delivered claim is), but no IDENTIFIER may define one: no constant, no
    exported name, no function argument.  A new constant named after an
    outcome would be the sibling task re-implemented here.
    """

    import lh_harness.auditor_verify as module

    outcome_words = (
        "delivered",
        "delivered_partial",
        "blocked",
        "failed",
        "aborted",
        "incomplete",
    )

    for name in dir(module):
        if name.startswith("__"):
            continue
        lowered = name.lower()
        for word in outcome_words:
            assert word not in lowered, (
                f"{name!r} looks like a close-out outcome name; the outcome "
                "vocabulary belongs to yt:easybutt0n-17:harness-closeout-contract"
            )

    import ast

    tree = ast.parse(open(module.__file__, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                for word in outcome_words:
                    assert word not in target.id.lower(), (
                        f"constant {target.id!r} looks like a close-out outcome "
                        "name; the vocabulary belongs to the sibling task"
                    )


def test_the_verifier_has_no_issue_tracker_surface() -> None:
    """GitHub only: no issue-tracker capability is added here."""

    import lh_harness.auditor_verify as module

    source = open(module.__file__, encoding="utf-8").read().lower()
    assert "youtrack" not in source
    assert "issue_tracker" not in source


# ---------------------------------------------------------------------------
# 8. The gh credential leak (easybutt0n-17, PR #187).
# ---------------------------------------------------------------------------
#
# Overseer1's blocker (PR comment 6664047687): ``gh auth token``,
# ``gh auth status --show-token`` and ``gh config get oauth_token`` were
# ALLOWED, because ``auth`` / ``config`` / ``secret`` sat in
# ``_GH_READ_SUBCOMMANDS``.  With an inherited ``gh`` login or an executor
# ``GH_TOKEN``, those print a WRITE-CAPABLE credential into the auditor's
# context.
#
# Two halves to the remedy, and both are proved here:
#
#   *  the COMMAND surface refuses every credential-revealing or
#      credential-changing leaf; and
#   *  the auditor's SUBPROCESS ENVIRONMENT is built from scratch, so the
#      auditor has no credential to reveal in the first place.
#
# Every token value in this section is FAKE.  Nothing here prints a value,
# and no real credential is used anywhere.


#: Every argv that reveals or changes a credential.  Each one must be
#: refused by the in-process layer.
CREDENTIAL_REVEALING_ARGV: tuple[tuple[str, ...], ...] = (
    # ``gh auth``: the token itself, and the token under a status leaf.
    ("gh", "auth", "token"),
    ("gh", "auth", "status", "--show-token"),
    ("gh", "auth", "status", "-t"),
    ("gh", "auth", "status", "--show-token=true"),
    ("gh", "auth", "status", "--hostname", "github.com", "--show-token"),
    # The token-revealing flag BEHIND another flag: a deny rule anchored on
    # the flag coming first would let this through.
    ("gh", "auth", "status", "--hostname", "github.com", "-t"),
    ("gh", "auth", "status", "--hostname", "github.com", "--show-token"),
    # ``gh auth``: the leaves that change the credential.
    ("gh", "auth", "login"),
    ("gh", "auth", "refresh"),
    ("gh", "auth", "setup-git"),
    ("gh", "auth", "switch"),
    ("gh", "auth", "logout"),
    # ``gh config``: reading, listing and writing the token keys.
    ("gh", "config", "get", "oauth_token"),
    ("gh", "config", "get", "git_protocol"),
    ("gh", "config", "get", "hosts"),
    ("gh", "config", "list"),
    ("gh", "config", "set", "oauth_token", "x"),
    # ``gh secret``: every leaf, reading or writing.
    ("gh", "secret", "list"),
    ("gh", "secret", "set", "K", "V"),
    ("gh", "secret", "delete", "K"),
    ("gh", "secret", "delete", "--org", "o", "K"),
)


@pytest.mark.parametrize("argv", CREDENTIAL_REVEALING_ARGV)
def test_every_credential_revealing_gh_command_is_refused(argv: tuple[str, ...]) -> None:
    """Every credential-revealing or credential-changing ``gh`` leaf is refused."""

    from lh_harness.auditor_verify import assert_read_only_command

    with pytest.raises(ReadOnlyViolation) as excinfo:
        assert_read_only_command(list(argv))

    message = str(excinfo.value)
    assert "read-only" in message.lower()
    # The refusal names what was refused, and never a credential value.
    assert argv[1] in message


def test_plain_gh_auth_status_remains_allowed() -> None:
    """The one credential-free ``gh auth`` leaf stays allowed.

    ``gh auth status`` reports WHICH account is logged in, not the token it
    would use.  The refusal of the ``gh auth`` group must not take that leaf
    with it.
    """

    from lh_harness.auditor_verify import assert_read_only_command

    assert_read_only_command(["gh", "auth", "status"])
    assert_read_only_command(["gh", "auth", "status", "--hostname", "github.com"])


@pytest.mark.parametrize("argv", CREDENTIAL_REVEALING_ARGV)
def test_the_permission_layer_denies_the_same_credential_commands(
    argv: tuple[str, ...],
) -> None:
    """The deny-rule layer refuses the credential commands too.

    The auditor's real protection is the ``--disallowedTools`` deny list the
    harness hands to the agent CLI.  If only the in-process layer refused
    these, the auditor's shell-out would still be free to ask ``gh`` for a
    credential.  So both layers have to refuse the same commands.
    """

    from lh_harness.adapters.claude_permissions import (
        AUDITOR_GH_WRITE_DENY,
        AUDITOR_NETWORK_GIT_DENY,
    )
    from lh_harness.auditor_verify import command_string, is_denied_by_auditor_policy

    rules = (*AUDITOR_GH_WRITE_DENY, *AUDITOR_NETWORK_GIT_DENY)
    command = command_string(list(argv))

    assert is_denied_by_auditor_policy(command, rules) is not None, (
        f"{command!r} is refused by the in-process layer but not by the "
        "permission layer"
    )


def test_the_permission_layer_still_admits_plain_gh_auth_status() -> None:
    """The deny list does not take the admissible leaf with it."""

    from lh_harness.adapters.claude_permissions import (
        AUDITOR_GH_WRITE_DENY,
        AUDITOR_NETWORK_GIT_DENY,
    )
    from lh_harness.auditor_verify import is_denied_by_auditor_policy

    rules = (*AUDITOR_GH_WRITE_DENY, *AUDITOR_NETWORK_GIT_DENY)

    assert is_denied_by_auditor_policy("gh auth status", rules) is None
    assert (
        is_denied_by_auditor_policy("gh auth status --hostname github.com", rules)
        is None
    )


def test_the_credential_groups_are_not_on_the_read_allowlist() -> None:
    """``auth`` / ``config`` / ``secret`` are not read groups any more.

    The refusal above is enforced by a leaf rule, not by an accident of the
    allowlist, so the allowlist itself is checked too: a future edit that
    put ``secret`` back would otherwise silently re-open the hole.
    """

    from lh_harness.auditor_verify import _GH_READ_SUBCOMMANDS

    for group in ("auth", "config", "secret"):
        assert group not in _GH_READ_SUBCOMMANDS


def test_a_refusal_never_carries_a_credential_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal of a credential command still names no credential value."""

    from lh_harness.auditor_verify import assert_read_only_command

    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "fake-auditor-token-value")
    monkeypatch.setenv("GH_TOKEN", "fake-executor-token-value")

    for argv in CREDENTIAL_REVEALING_ARGV:
        with pytest.raises(ReadOnlyViolation) as excinfo:
            assert_read_only_command(list(argv))
        message = str(excinfo.value)
        assert "fake-auditor-token-value" not in message
        assert "fake-executor-token-value" not in message


# ---------------------------------------------------------------------------
# 8b. The auditor subprocess environment is built FROM SCRATCH.
# ---------------------------------------------------------------------------


def _child_env(env: dict[str, str]) -> dict[str, str]:
    """Spawn a REAL child with ``env`` and read back its environment.

    The assertion is made on the CHILD's environment, not on the mapping the
    builder returned: a builder that forgot a key but a wrapper that removed
    it later would pass a mapping-only test and still leak.
    """

    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, "-c", "import os, json; print(json.dumps(dict(os.environ)))"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_the_auditor_child_does_not_see_the_executor_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An executor ``GH_TOKEN`` in the parent env does not reach the child."""

    from lh_harness.auditor_env import build_auditor_subprocess_env

    monkeypatch.setenv("GH_TOKEN", "fake-executor-token")
    monkeypatch.setenv("GITHUB_TOKEN", "fake-executor-github-token")
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "fake-enterprise-token")
    monkeypatch.setenv("GITHUB_ENTERPRISE_TOKEN", "fake-enterprise-github-token")
    monkeypatch.setenv("LH_HARNESS_FLEET_GITHUB_TOKEN", "fake-fleet-token")
    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "fake-auditor-token")

    child_env = _child_env(build_auditor_subprocess_env())

    assert "fake-executor-token" not in child_env.values()
    assert "fake-executor-github-token" not in child_env.values()
    assert "fake-enterprise-token" not in child_env.values()
    assert "fake-enterprise-github-token" not in child_env.values()
    assert "fake-fleet-token" not in child_env.values()
    for name in (
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "LH_HARNESS_FLEET_GITHUB_TOKEN",
        "LH_HARNESS_FLEET_TOKEN",
    ):
        assert name not in child_env, f"{name} reached the auditor's child"


def test_the_auditor_child_gh_config_dir_is_the_fresh_empty_temp_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GH_CONFIG_DIR`` points at a fresh EMPTY directory.

    An empty directory means ``gh`` finds no stored credential to fall back
    on, so it can only authenticate with the environment's ``GH_TOKEN``.
    """

    from lh_harness.auditor_env import build_auditor_subprocess_env

    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "fake-auditor-token")

    built = build_auditor_subprocess_env()
    child_env = _child_env(built)

    config_dir = child_env.get("GH_CONFIG_DIR")
    assert config_dir, "the auditor's child has no GH_CONFIG_DIR"
    assert config_dir == built["GH_CONFIG_DIR"]
    assert Path(config_dir).is_dir()
    assert list(Path(config_dir).iterdir()) == [], "the gh config dir is not empty"


def test_the_auditor_token_is_the_only_gh_token_the_child_sees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child's only ``GH_TOKEN`` is the auditor's own token."""

    from lh_harness.auditor_env import build_auditor_subprocess_env

    monkeypatch.setenv("GH_TOKEN", "fake-executor-token")
    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "fake-auditor-token")

    child_env = _child_env(build_auditor_subprocess_env())

    assert child_env.get("GH_TOKEN") == "fake-auditor-token"
    assert "fake-executor-token" not in child_env.values()


def test_the_auditor_child_environment_is_built_not_filtered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The environment is BUILT FROM SCRATCH, not the parent's minus names.

    A filter can only remove what it knows about.  So a variable the harness
    has never heard of, carrying a credential, must not survive either --
    which is what "built from scratch" buys.
    """

    from lh_harness.auditor_env import build_auditor_subprocess_env

    monkeypatch.setenv("SOME_UNKNOWN_FUTURE_CREDENTIAL", "fake-unknown-token")
    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "fake-auditor-token")

    built = build_auditor_subprocess_env()
    child_env = _child_env(built)

    assert "SOME_UNKNOWN_FUTURE_CREDENTIAL" not in child_env
    assert "fake-unknown-token" not in child_env.values()
    # The base is small and explicit, not the parent's environment.  The
    # built mapping is checked exactly; the child may carry extra variables
    # the interpreter itself adds (LC_CTYPE and friends), never one the
    # harness put there.
    assert set(built) <= {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "TMPDIR",
        "SHELL",
        "TERM",
        "GH_TOKEN",
        "GH_CONFIG_DIR",
        "GIT_TERMINAL_PROMPT",
        "GIT_ASKPASS",
        "GIT_PAGER",
        "PAGER",
    }
    assert set(child_env) - set(built) <= {"LC_CTYPE"}


def test_the_auditor_child_gets_no_gh_login_to_fall_back_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No inherited ``gh`` configuration directory reaches the child.

    ``gh`` reads its stored credentials from ``$GH_CONFIG_DIR`` or
    ``$HOME/.config/gh``.  The child's ``HOME`` is the harness user's, so
    the empty ``GH_CONFIG_DIR`` is what keeps the stored login out.
    """

    from lh_harness.auditor_env import build_auditor_subprocess_env

    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "fake-auditor-token")

    child_env = _child_env(build_auditor_subprocess_env())

    assert "GH_CONFIG_DIR" in child_env
    # The inherited HOME's gh directory is not what the child is pointed at.
    assert child_env["GH_CONFIG_DIR"] != str(
        Path(child_env.get("HOME", "/nonexistent")) / ".config" / "gh"
    )


def test_a_shared_auditor_credential_is_refused_by_the_env_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The builder refuses to install a shared credential as the auditor's."""

    from lh_harness.auditor_env import build_auditor_subprocess_env

    monkeypatch.setenv("GH_TOKEN", "fake-shared-token")
    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "fake-shared-token")

    with pytest.raises(AuditorCredentialError):
        build_auditor_subprocess_env()


def test_the_auditor_env_builder_never_prints_a_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Building the environment prints nothing at all."""

    from lh_harness.auditor_env import build_auditor_subprocess_env

    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "fake-auditor-token")

    build_auditor_subprocess_env()

    captured = capsys.readouterr()
    assert "fake-auditor-token" not in captured.out
    assert "fake-auditor-token" not in captured.err


@pytest.mark.asyncio
async def test_the_auditor_subprocess_really_runs_with_the_scrubbed_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The REAL spawn path gives the auditor child the scrubbed environment.

    The other tests in this section exercise the builder.  This one exercises
    the ADAPTER: the auditor role's subprocess is launched through
    ``LocalEnvironment.exec`` wrapped by the auditor environment wrapper, and
    the child prints its own ``os.environ``.  So the assertion is made on the
    environment a real auditor subprocess saw -- not on a mapping, and not on
    a mock.
    """

    from lh_harness.adapters.claude_code import _AuditorEnvWrapper
    from lh_harness.environment.local import LocalEnvironment

    monkeypatch.setenv("GH_TOKEN", "fake-executor-token")
    monkeypatch.setenv("GITHUB_TOKEN", "fake-executor-github-token")
    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "fake-auditor-token")

    local = LocalEnvironment(tmp_dir=str(tmp_path))
    wrapped = _AuditorEnvWrapper(local)

    command = (
        "python3 -c 'import os, json; print(json.dumps(dict(os.environ)))'"
    )
    result = await wrapped.exec(command, timeout=30)

    assert result.exit_code == 0, result.stderr
    child_env = json.loads(result.stdout.strip().splitlines()[-1])

    # The executor's tokens are NOT in the auditor child's environment.
    assert "fake-executor-token" not in child_env.values()
    assert "fake-executor-github-token" not in child_env.values()
    assert "GITHUB_TOKEN" not in child_env
    # The auditor's own token is the child's only GH_TOKEN.
    assert child_env.get("GH_TOKEN") == "fake-auditor-token"
    # And gh has an empty configuration directory to fall back on.
    config_dir = child_env.get("GH_CONFIG_DIR")
    assert config_dir
    assert Path(config_dir).is_dir()
    assert list(Path(config_dir).iterdir()) == []
