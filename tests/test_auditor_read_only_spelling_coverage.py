"""Per-spelling proof that the auditor's verification surface refuses writes.

Task ``yt:easybutt0n-17:closeout-validation-chain``, Deliverable 1.

Why this module is separate from ``test_auditor_verify.py``
-----------------------------------------------------------

The auditor's reach into the outside world is read-only, and that has to be
*proven*, not asserted.  A single parametrized test over a list of command
spellings proves only that SOME spelling was refused: the list can lose an
entry, a spelling can move to the wrong group, and a generic assertion
("the message mentions read-only") still passes while the refusal names
nothing about the operation it refused.

So every write spelling below gets its OWN named test, and each test
asserts three things about the refusal:

1.  the write is refused at all;
2.  the refusal names the attempted operation, so an auditor reading the
    refusal can tell *what* was refused; and
3.  the refusal never echoes a credential value.

The spellings covered here are exactly the ones a read allowlist would
otherwise let through -- in particular ``gh api --method=POST`` and a
field-supplying ``gh api`` body, which a bare ``Bash(gh *)`` deny rule
leaves unmatched.

Every refusal here is a refusal of a WRITE.  The read path is pinned in
``test_auditor_verify.py``; the read spellings stay available, and
``test_the_accepted_read_spelling`` below re-checks the boundary from this
side so a write refusal can never be mistaken for "everything is refused".
"""

from __future__ import annotations

import pytest

from lh_harness.auditor_verify import (
    AUDITOR_GITHUB_TOKEN_ENV,
    ReadOnlyGitHubClient,
    ReadOnlyViolation,
    assert_read_only_command,
)

REPO = "cogniziocompany/LongHorizon-Harness"


def _refusal(argv: list[str]) -> str:
    """Assert *argv* is refused and return the refusal message.

    Every refusal must:
      * raise :class:`ReadOnlyViolation` (never pass silently);
      * name the read-only surface, so the reason is obvious; and
      * never carry a credential value.
    """

    with pytest.raises(ReadOnlyViolation) as excinfo:
        assert_read_only_command(argv)

    message = str(excinfo.value)
    assert "read-only" in message.lower()
    # A refusal never echoes a credential.
    assert "ghp_" not in message
    return message


# ---------------------------------------------------------------------------
# 1. `gh api` with an explicit write method.
# ---------------------------------------------------------------------------


def test_gh_api_with_method_post_is_refused() -> None:
    """``gh api --method=POST`` -- a write spelled with the long flag."""

    message = _refusal(["gh", "api", "--method=POST", f"repos/{REPO}/pulls/181/comments"])
    assert "POST" in message


def test_gh_api_with_x_post_is_refused() -> None:
    """``gh api -X POST`` -- a write spelled with the short flag."""

    message = _refusal(["gh", "api", "-X", "POST", f"repos/{REPO}/pulls/181/comments"])
    assert "POST" in message


def test_gh_api_with_x_post_no_space_is_refused() -> None:
    """``gh api -XPOST`` -- a write spelled with the joined short flag."""

    message = _refusal(["gh", "api", "-XPOST", f"repos/{REPO}/pulls/181"])
    assert "POST" in message


def test_gh_api_with_lowercase_method_post_is_refused() -> None:
    """``gh api --method=post`` -- a write spelled in lowercase."""

    message = _refusal(["gh", "api", "--method=post", f"repos/{REPO}/pulls/181"])
    assert "POST" in message


def test_gh_api_with_method_put_is_refused() -> None:
    """``gh api --method=PUT``."""

    message = _refusal(["gh", "api", "--method=PUT", f"repos/{REPO}/pulls/181"])
    assert "PUT" in message


def test_gh_api_with_x_put_is_refused() -> None:
    """``gh api -X PUT``."""

    message = _refusal(["gh", "api", "-X", "PUT", f"repos/{REPO}/pulls/181"])
    assert "PUT" in message


def test_gh_api_with_method_patch_is_refused() -> None:
    """``gh api --method=PATCH``."""

    message = _refusal(["gh", "api", "--method=PATCH", f"repos/{REPO}/pulls/181"])
    assert "PATCH" in message


def test_gh_api_with_x_patch_is_refused() -> None:
    """``gh api -X PATCH``."""

    message = _refusal(["gh", "api", "-X", "PATCH", f"repos/{REPO}/pulls/181"])
    assert "PATCH" in message


def test_gh_api_with_method_delete_is_refused() -> None:
    """``gh api --method=DELETE``."""

    message = _refusal(["gh", "api", "--method=DELETE", f"repos/{REPO}/pulls/181"])
    assert "DELETE" in message


def test_gh_api_with_x_delete_is_refused() -> None:
    """``gh api -X DELETE``."""

    message = _refusal(["gh", "api", "-X", "DELETE", f"repos/{REPO}/pulls/181"])
    assert "DELETE" in message


def test_gh_api_with_two_conflicting_methods_is_refused() -> None:
    """A conflicting method specification is refused rather than guessed."""

    message = _refusal(["gh", "api", "--method=POST", "-X", "GET", f"repos/{REPO}/pulls/181"])
    assert "conflicting" in message.lower()


# ---------------------------------------------------------------------------
# 2. `gh api` with a field-supplying body -- a write with no explicit method.
# ---------------------------------------------------------------------------


def test_gh_api_with_dash_f_field_is_refused() -> None:
    """``gh api <path> -f body=...`` -- a body makes it a write."""

    message = _refusal(["gh", "api", f"repos/{REPO}/issues/1/comments", "-f", "body=lgtm"])
    assert "body" in message.lower()


def test_gh_api_with_dash_capital_f_field_is_refused() -> None:
    """``gh api <path> -F body=...`` -- the typed-field spelling."""

    message = _refusal(["gh", "api", f"repos/{REPO}/issues/1/comments", "-F", "body=lgtm"])
    assert "body" in message.lower()


def test_gh_api_with_field_flag_is_refused() -> None:
    """``gh api <path> --field body=...``."""

    message = _refusal(["gh", "api", f"repos/{REPO}/issues/1/comments", "--field", "body=lgtm"])
    assert "body" in message.lower()


def test_gh_api_with_raw_field_flag_is_refused() -> None:
    """``gh api <path> --raw-field body=...``."""

    message = _refusal(["gh", "api", f"repos/{REPO}/issues/1/comments", "--raw-field", "body=lgtm"])
    assert "body" in message.lower()


def test_gh_api_with_input_flag_is_refused() -> None:
    """``gh api <path> --input payload.json`` -- a body read from a file."""

    message = _refusal(["gh", "api", f"repos/{REPO}/issues/1/comments", "--input", "payload.json"])
    assert "body" in message.lower()


def test_gh_api_with_input_equals_flag_is_refused() -> None:
    """``gh api <path> --input=payload.json`` -- the joined spelling."""

    message = _refusal(["gh", "api", f"repos/{REPO}/issues/1/comments", "--input=payload.json"])
    assert "body" in message.lower()


def test_gh_api_with_method_post_and_a_field_is_refused() -> None:
    """A method AND a body: refused, and still named as a write."""

    message = _refusal(
        ["gh", "api", "-X", "POST", f"repos/{REPO}/issues/1/comments", "-f", "body=lgtm"]
    )
    assert "read-only" in message.lower()


# ---------------------------------------------------------------------------
# 3. `gh pr` / `gh issue` write leaves.
# ---------------------------------------------------------------------------


def test_gh_pr_merge_is_refused() -> None:
    """``gh pr merge`` -- the merge path."""

    message = _refusal(["gh", "pr", "merge", "181"])
    assert "merge" in message.lower()


def test_gh_pr_close_is_refused() -> None:
    """``gh pr close``."""

    message = _refusal(["gh", "pr", "close", "181"])
    assert "close" in message.lower()


def test_gh_pr_ready_is_refused() -> None:
    """``gh pr ready``."""

    message = _refusal(["gh", "pr", "ready", "181"])
    assert "ready" in message.lower()


def test_gh_pr_edit_is_refused() -> None:
    """``gh pr edit`` -- a field write on the pull request."""

    message = _refusal(["gh", "pr", "edit", "181", "--title", "x"])
    assert "edit" in message.lower()


def test_gh_pr_comment_is_refused() -> None:
    """``gh pr comment`` -- a comment write."""

    message = _refusal(["gh", "pr", "comment", "181", "--body", "lgtm"])
    assert "comment" in message.lower()


def test_gh_pr_create_is_refused() -> None:
    """``gh pr create``."""

    message = _refusal(["gh", "pr", "create", "--title", "x"])
    assert "create" in message.lower()


def test_gh_pr_review_is_refused() -> None:
    """``gh pr review`` -- an approval is a write."""

    message = _refusal(["gh", "pr", "review", "--approve", "181"])
    assert "review" in message.lower()


def test_gh_issue_comment_is_refused() -> None:
    """``gh issue comment``."""

    message = _refusal(["gh", "issue", "comment", "1", "--body", "x"])
    assert "comment" in message.lower()


def test_gh_issue_create_is_refused() -> None:
    """``gh issue create`` -- a ticket create."""

    message = _refusal(["gh", "issue", "create", "--title", "x"])
    assert "create" in message.lower()


def test_gh_release_create_is_refused() -> None:
    """``gh release create``."""

    message = _refusal(["gh", "release", "create", "v1"])
    assert "create" in message.lower()


def test_gh_secret_set_is_refused() -> None:
    """``gh secret set`` -- a write that hides under a read group."""

    message = _refusal(["gh", "secret", "set", "K", "V"])
    assert "set" in message.lower()


# ---------------------------------------------------------------------------
# 5b. The credential-revealing spellings.
# ---------------------------------------------------------------------------
#
# These are not WRITES: they PRINT a credential.  A read allowlist that
# admitted them would put a write-capable token into the auditor's context,
# so they are refused just the same.  Each gets its OWN named test, like
# every other spelling in this module.


def test_gh_auth_token_is_refused() -> None:
    """``gh auth token`` -- prints the credential in the clear."""

    message = _refusal(["gh", "auth", "token"])
    assert "auth" in message.lower()


def test_gh_auth_status_show_token_is_refused() -> None:
    """``gh auth status --show-token`` -- a read that reveals the token."""

    message = _refusal(["gh", "auth", "status", "--show-token"])
    assert "auth" in message.lower()


def test_gh_auth_status_short_token_flag_is_refused() -> None:
    """``gh auth status -t`` -- the short form of the same reveal."""

    message = _refusal(["gh", "auth", "status", "-t"])
    assert "auth" in message.lower()


def test_gh_auth_status_show_token_behind_another_flag_is_refused() -> None:
    """``gh auth status --hostname github.com --show-token``.

    The flag may sit behind another one, so a check that only looked at the
    first argument would let this through.
    """

    message = _refusal(["gh", "auth", "status", "--hostname", "github.com", "--show-token"])
    assert "auth" in message.lower()


def test_gh_auth_login_is_refused() -> None:
    """``gh auth login`` -- changes the credential."""

    message = _refusal(["gh", "auth", "login"])
    assert "auth" in message.lower()


def test_gh_auth_refresh_is_refused() -> None:
    """``gh auth refresh`` -- changes the credential."""

    message = _refusal(["gh", "auth", "refresh"])
    assert "auth" in message.lower()


def test_gh_auth_setup_git_is_refused() -> None:
    """``gh auth setup-git`` -- changes the credential's use."""

    message = _refusal(["gh", "auth", "setup-git"])
    assert "auth" in message.lower()


def test_gh_auth_switch_is_refused() -> None:
    """``gh auth switch`` -- changes which credential is in use."""

    message = _refusal(["gh", "auth", "switch"])
    assert "auth" in message.lower()


def test_gh_config_get_oauth_token_is_refused() -> None:
    """``gh config get oauth_token`` -- prints the credential in the clear."""

    message = _refusal(["gh", "config", "get", "oauth_token"])
    assert "config" in message.lower()


def test_gh_config_list_is_refused() -> None:
    """``gh config list`` -- can list the credential keys."""

    message = _refusal(["gh", "config", "list"])
    assert "config" in message.lower()


def test_gh_secret_list_is_refused() -> None:
    """``gh secret list`` -- names the repository's secrets."""

    message = _refusal(["gh", "secret", "list"])
    assert "secret" in message.lower()


def test_plain_gh_auth_status_is_not_refused() -> None:
    """The one credential-free ``gh auth`` leaf stays admissible.

    ``gh auth status`` reports the account in use, not the token it would
    use.  The refusal of the ``gh auth`` group must not take it with it.
    """

    assert_read_only_command(["gh", "auth", "status"])
    assert_read_only_command(["gh", "auth", "status", "--hostname", "github.com"])


def test_gh_variable_set_is_refused() -> None:
    """``gh variable set`` -- a write that hides under a read group."""

    message = _refusal(["gh", "variable", "set", "K", "V"])
    assert "set" in message.lower()


def test_gh_workflow_run_is_refused() -> None:
    """``gh workflow run`` -- a dispatch, spelled like a read."""

    message = _refusal(["gh", "workflow", "run", "ci.yml"])
    assert "run" in message.lower()


def test_gh_workflow_dispatch_is_refused() -> None:
    """``gh workflow dispatch``."""

    message = _refusal(["gh", "workflow", "dispatch", "ci.yml"])
    assert "dispatch" in message.lower()


def test_gh_run_rerun_is_refused() -> None:
    """``gh run rerun``."""

    message = _refusal(["gh", "run", "rerun", "1"])
    assert "rerun" in message.lower()


def test_gh_run_cancel_is_refused() -> None:
    """``gh run cancel``."""

    message = _refusal(["gh", "run", "cancel", "1"])
    assert "cancel" in message.lower()


def test_gh_run_delete_is_refused() -> None:
    """``gh run delete``."""

    message = _refusal(["gh", "run", "delete", "1"])
    assert "delete" in message.lower()


def test_gh_repo_edit_is_refused() -> None:
    """``gh repo edit``."""

    message = _refusal(["gh", "repo", "edit", "owner/repo"])
    assert "edit" in message.lower()


# ---------------------------------------------------------------------------
# 4. `git` writes.
# ---------------------------------------------------------------------------


def test_git_push_with_a_remote_is_refused() -> None:
    """``git push origin main`` -- the push path."""

    message = _refusal(["git", "push", "origin", "main"])
    assert "push" in message.lower()


def test_git_push_without_a_remote_is_refused() -> None:
    """``git push`` -- no remote named, still a push."""

    message = _refusal(["git", "push"])
    assert "push" in message.lower()


def test_git_push_with_a_force_flag_is_refused() -> None:
    """``git push --force`` -- the force-push path."""

    message = _refusal(["git", "push", "--force", "origin", "main"])
    assert "push" in message.lower()


# ---------------------------------------------------------------------------
# 5. A direct write call into the harness's own GitHub client.
# ---------------------------------------------------------------------------


def test_client_write_pull_request_method_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct call to the client's write method is refused, not absent."""

    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "test-auditor-readonly-token-000")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    client = ReadOnlyGitHubClient(opener=lambda *a, **k: None)

    with pytest.raises(ReadOnlyViolation) as excinfo:
        client.write_pull_request(REPO, 181, {"title": "x"})

    assert "read-only" in str(excinfo.value).lower()


def test_client_request_with_a_post_verb_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A POST through the client's single request surface is refused."""

    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "test-auditor-readonly-token-000")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    client = ReadOnlyGitHubClient(opener=lambda *a, **k: None)

    with pytest.raises(ReadOnlyViolation):
        client.request("POST", f"/repos/{REPO}/pulls/181")


def test_client_request_with_a_patch_verb_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PATCH through the client's single request surface is refused."""

    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "test-auditor-readonly-token-000")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    client = ReadOnlyGitHubClient(opener=lambda *a, **k: None)

    with pytest.raises(ReadOnlyViolation):
        client.request("PATCH", f"/repos/{REPO}/pulls/181")


def test_client_request_with_a_put_verb_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PUT through the client's single request surface is refused."""

    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "test-auditor-readonly-token-000")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    client = ReadOnlyGitHubClient(opener=lambda *a, **k: None)

    with pytest.raises(ReadOnlyViolation):
        client.request("PUT", f"/repos/{REPO}/pulls/181")


def test_client_request_with_a_delete_verb_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DELETE through the client's single request surface is refused."""

    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "test-auditor-readonly-token-000")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    client = ReadOnlyGitHubClient(opener=lambda *a, **k: None)

    with pytest.raises(ReadOnlyViolation):
        client.request("DELETE", f"/repos/{REPO}/pulls/181")


def test_client_unknown_write_attribute_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown ``write_*`` attribute is refused, not an opaque AttributeError."""

    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "test-auditor-readonly-token-000")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    client = ReadOnlyGitHubClient(opener=lambda *a, **k: None)

    with pytest.raises(ReadOnlyViolation):
        client.write_anything_at_all(REPO, 181)


def test_client_unknown_ticket_write_attribute_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown ticket-write attribute is refused too."""

    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "test-auditor-readonly-token-000")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    client = ReadOnlyGitHubClient(opener=lambda *a, **k: None)

    with pytest.raises(ReadOnlyViolation):
        client.create_issue(REPO, "title")


# ---------------------------------------------------------------------------
# 6. The boundary: reads stay available.
# ---------------------------------------------------------------------------


def test_the_accepted_read_spelling() -> None:
    """A read is NOT refused -- the refusals above are not "everything fails"."""

    assert_read_only_command(["gh", "pr", "view", "181", "--json", "number,state,headRefOid"])
    assert_read_only_command(["gh", "api", f"repos/{REPO}/pulls/181"])
    assert_read_only_command(["gh", "api", "--method=GET", f"repos/{REPO}/pulls/181"])
    assert_read_only_command(["gh", "repo", "view", REPO])
    assert_read_only_command(["git", "log", "--oneline", "-1"])
