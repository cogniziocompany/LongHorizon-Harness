"""Per-spelling proof that the auditor's read-only reach refuses writes.

Task ``yt:easybutt0n-17:closeout-validation-chain``, Deliverable 1.

Why this module exists
-----------------------

The auditor's independent reach into the outside world is read-only, and
that has to be PROVEN, not asserted.  Two layers enforce it, and until now
only one of them was proven:

1.  the IN-PROCESS layer -- ``lh_harness.auditor_verify.assert_read_only_command``
    -- refuses a write by the SHAPE of the invocation (program, subcommand
    and leaf, HTTP method, presence of a request body); and
2.  the PERMISSION layer -- the ``Bash(...)`` rules the harness hands to
    Claude Code as ``--disallowedTools`` -- stops the auditor from shelling
    out to a write at all.

The permission layer was previously checked by string MEMBERSHIP: the tests
asserted that the literal rule ``"Bash(gh pr merge*)"`` was IN the deny
list.  That proves the rule exists.  It proves nothing about whether the
rule DENIES the command it is written for -- a rule can sit in the list
and match nothing, and the shell-out is then refused by nothing at all.

So every write spelling below is checked against BOTH layers, and each
spelling gets its OWN named test.  A single parametrized test proves only
that SOME spelling was refused: the list can lose an entry, a spelling can
move to the wrong group, and a generic assertion ("the message mentions
read-only") still passes while the refusal names nothing about the
operation it refused.

Each per-spelling test asserts four things:

1.  the in-process layer refuses the spelling;
2.  the permission layer denies the spelling -- the command string matches a
    deny rule, and the rule is named;
3.  the refusal names the attempted operation, so an auditor reading it can
    tell *what* was refused; and
4.  the refusal never echoes a credential value.

The module also carries the HTTP-layer proof that NO write request is ever
issued: the client is pointed at a recording transport and every request
it makes is inspected, so "read-only" is shown at the wire, not only at the
command surface.

Scope notes:

*   GitHub only.  No issue-tracker surface of any kind is added here.
*   The auditor's contract header is untouched: still exactly
    ``Status`` / ``Integrity`` / ``Contract audit``.
"""

from __future__ import annotations

import io
import json
import urllib.request

import pytest

from lh_harness.adapters.claude_permissions import (
    AUDITOR_GH_WRITE_DENY,
    AUDITOR_NETWORK_GIT_DENY,
)
from lh_harness.auditor_verify import (
    AUDITOR_GITHUB_TOKEN_ENV,
    ReadOnlyGitHubClient,
    ReadOnlyViolation,
    assert_read_only_command,
)

# The matcher, the classifier and the canonical spelling list are NEW in this
# change.  They are imported defensively so that this module, run against a
# tree that does not have them yet, FAILS PER TEST with a clear message
# rather than dying at import time: a collection error would prove only that
# a name is missing, not that the enforcement gap was real.
try:  # pragma: no cover - the RED state simply does not have it yet
    from lh_harness.adapters.claude_permissions import (
        uncovered_auditor_write_spellings,
    )
except ImportError:  # pragma: no cover
    uncovered_auditor_write_spellings = None  # type: ignore[assignment]

try:  # pragma: no cover - the RED state simply does not have it yet
    from lh_harness.auditor_verify import (
        AUDITOR_WRITE_SPELLINGS,
        command_string,
        deny_rule_matches,
        is_denied_by_auditor_policy,
        write_intent_reason,
    )
except ImportError:  # pragma: no cover
    AUDITOR_WRITE_SPELLINGS = ()
    command_string = None  # type: ignore[assignment]
    deny_rule_matches = None  # type: ignore[assignment]
    is_denied_by_auditor_policy = None  # type: ignore[assignment]
    write_intent_reason = None  # type: ignore[assignment]

REPO = "cogniziocompany/LongHorizon-Harness"

#: Every deny rule the auditor role carries.
ALL_DENY_RULES: tuple[str, ...] = (*AUDITOR_GH_WRITE_DENY, *AUDITOR_NETWORK_GIT_DENY)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _refusal(argv: list[str]) -> str:
    """Assert *argv* is refused by the in-process layer; return the message.

    Every refusal must:
      * raise :class:`ReadOnlyViolation` (never pass silently);
      * name the read-only surface, so the reason is obvious;
      * name the attempted operation; and
      * never carry a credential value.
    """

    with pytest.raises(ReadOnlyViolation) as excinfo:
        assert_read_only_command(argv)

    message = str(excinfo.value)
    assert "read-only" in message.lower()
    assert "ghp_" not in message
    return message


def _denied_by_policy(argv: list[str]) -> str:
    """Assert the PERMISSION layer denies *argv*; return the rule that does.

    This is the proof that was missing: the deny list is not merely
    carrying the rule, the rule MATCHES this command.
    """

    assert is_denied_by_auditor_policy is not None, (
        "the harness has no deny-rule matcher: the auditor's permission "
        "layer cannot be proven to refuse a write"
    )
    command = command_string(argv)
    rule = is_denied_by_auditor_policy(command, ALL_DENY_RULES)
    assert rule is not None, (
        f"the auditor's permission layer does not deny {command!r}: no deny "
        "rule matches it"
    )
    assert deny_rule_matches(command, rule)
    return rule


def _refused_at_both_layers(argv: list[str]) -> tuple[str, str]:
    """Assert the spelling is refused by BOTH layers; return both reasons."""

    message = _refusal(argv)
    rule = _denied_by_policy(argv)
    return message, rule


# ---------------------------------------------------------------------------
# 1. `gh api` with an explicit write method.
# ---------------------------------------------------------------------------


def test_gh_api_with_method_post_is_refused() -> None:
    """``gh api --method=POST`` -- a write spelled with the long flag."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "--method=POST", f"repos/{REPO}/pulls/181/comments"]
    )
    assert "POST" in message
    assert "gh api" in rule


def test_gh_api_with_x_post_is_refused() -> None:
    """``gh api -X POST`` -- a write spelled with the short flag."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "-X", "POST", f"repos/{REPO}/pulls/181/comments"]
    )
    assert "POST" in message
    assert "gh api" in rule


def test_gh_api_with_x_post_joined_is_refused() -> None:
    """``gh api -XPOST`` -- a write spelled with the joined short flag."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "-XPOST", f"repos/{REPO}/pulls/181"]
    )
    assert "POST" in message
    assert "gh api" in rule


def test_gh_api_with_lowercase_method_post_is_refused() -> None:
    """``gh api --method=post`` -- a write spelled in lowercase."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "--method=post", f"repos/{REPO}/pulls/181"]
    )
    assert "POST" in message
    assert "gh api" in rule


def test_gh_api_with_method_put_is_refused() -> None:
    """``gh api --method=PUT``."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "--method=PUT", f"repos/{REPO}/pulls/181"]
    )
    assert "PUT" in message
    assert "gh api" in rule


def test_gh_api_with_x_put_is_refused() -> None:
    """``gh api -X PUT``."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "-X", "PUT", f"repos/{REPO}/pulls/181"]
    )
    assert "PUT" in message
    assert "gh api" in rule


def test_gh_api_with_method_patch_is_refused() -> None:
    """``gh api --method=PATCH``."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "--method=PATCH", f"repos/{REPO}/pulls/181"]
    )
    assert "PATCH" in message
    assert "gh api" in rule


def test_gh_api_with_x_patch_is_refused() -> None:
    """``gh api -X PATCH``."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "-X", "PATCH", f"repos/{REPO}/pulls/181"]
    )
    assert "PATCH" in message
    assert "gh api" in rule


def test_gh_api_with_method_delete_is_refused() -> None:
    """``gh api --method=DELETE``."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "--method=DELETE", f"repos/{REPO}/pulls/181"]
    )
    assert "DELETE" in message
    assert "gh api" in rule


def test_gh_api_with_x_delete_is_refused() -> None:
    """``gh api -X DELETE``."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "-X", "DELETE", f"repos/{REPO}/pulls/181"]
    )
    assert "DELETE" in message
    assert "gh api" in rule


def test_gh_api_with_two_conflicting_methods_is_refused() -> None:
    """A conflicting method specification is refused rather than guessed."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "--method=POST", "-X", "GET", f"repos/{REPO}/pulls/181"]
    )
    assert "conflicting" in message.lower()
    assert "gh api" in rule


def test_gh_api_with_space_separated_method_post_is_refused() -> None:
    """``gh api --method POST <path>`` -- the long flag with a separate value.

    ``--method=POST`` and ``--method POST`` are the same flag spelled two
    ways.  The deny list carries a rule for the joined spelling, so the
    space-separated spelling has to be proven separately rather than
    assumed to be covered by it.
    """

    message, rule = _refused_at_both_layers(
        ["gh", "api", "--method", "POST", f"repos/{REPO}/pulls/181/comments"]
    )
    assert "POST" in message
    assert "gh api" in rule


def test_gh_api_with_space_separated_method_patch_is_refused() -> None:
    """``gh api --method PATCH <path>`` -- the space-separated long flag."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "--method", "PATCH", f"repos/{REPO}/pulls/181"]
    )
    assert "PATCH" in message
    assert "gh api" in rule


def test_gh_api_with_space_separated_method_put_is_refused() -> None:
    """``gh api --method PUT <path>`` -- the space-separated long flag."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "--method", "PUT", f"repos/{REPO}/pulls/181"]
    )
    assert "PUT" in message
    assert "gh api" in rule


def test_gh_api_with_space_separated_method_delete_is_refused() -> None:
    """``gh api --method DELETE <path>`` -- the space-separated long flag."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "--method", "DELETE", f"repos/{REPO}/pulls/181"]
    )
    assert "DELETE" in message
    assert "gh api" in rule


def test_gh_api_with_method_patch_and_a_field_is_refused() -> None:
    """A PATCH method AND a body: refused, and still named as a write.

    The two reasons are independent -- a write method is a write on its
    own, and a body is a write on its own -- so the combination has to be
    refused for a reason that survives either one being removed.
    """

    message, rule = _refused_at_both_layers(
        ["gh", "api", "--method=PATCH", f"repos/{REPO}/pulls/181", "-f", "state=closed"]
    )
    assert "PATCH" in message
    assert "gh api" in rule


def test_gh_api_with_method_put_and_a_field_is_refused() -> None:
    """A PUT method AND a body: refused at both layers."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "--method=PUT", f"repos/{REPO}/pulls/181", "-F", "title=x"]
    )
    assert "PUT" in message
    assert "gh api" in rule


def test_gh_api_with_method_delete_and_a_field_is_refused() -> None:
    """A DELETE method AND a body: refused at both layers."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "--method=DELETE", f"repos/{REPO}/pulls/181", "--field", "x=1"]
    )
    assert "DELETE" in message
    assert "gh api" in rule


# ---------------------------------------------------------------------------
# 2. `gh api` with a field-supplying body -- a write with no explicit method.
# ---------------------------------------------------------------------------


def test_gh_api_with_dash_f_field_is_refused() -> None:
    """``gh api <path> -f body=...`` -- a body makes it a write."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", f"repos/{REPO}/issues/1/comments", "-f", "body=lgtm"]
    )
    assert "body" in message.lower()
    assert "gh api" in rule


def test_gh_api_with_dash_capital_f_field_is_refused() -> None:
    """``gh api <path> -F body=...`` -- the typed-field spelling."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", f"repos/{REPO}/issues/1/comments", "-F", "body=lgtm"]
    )
    assert "body" in message.lower()
    assert "gh api" in rule


def test_gh_api_with_field_flag_is_refused() -> None:
    """``gh api <path> --field body=...``."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", f"repos/{REPO}/issues/1/comments", "--field", "body=lgtm"]
    )
    assert "body" in message.lower()
    assert "gh api" in rule


def test_gh_api_with_raw_field_flag_is_refused() -> None:
    """``gh api <path> --raw-field body=...``."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", f"repos/{REPO}/issues/1/comments", "--raw-field", "body=lgtm"]
    )
    assert "body" in message.lower()
    assert "gh api" in rule


def test_gh_api_with_input_flag_is_refused() -> None:
    """``gh api <path> --input payload.json`` -- a body read from a file."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", f"repos/{REPO}/issues/1/comments", "--input", "payload.json"]
    )
    assert "body" in message.lower()
    assert "gh api" in rule


def test_gh_api_with_input_equals_flag_is_refused() -> None:
    """``gh api <path> --input=payload.json`` -- the joined spelling."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", f"repos/{REPO}/issues/1/comments", "--input=payload.json"]
    )
    assert "body" in message.lower()
    assert "gh api" in rule


def test_gh_api_with_dash_i_input_flag_is_refused() -> None:
    """``gh api <path> -i payload.json`` -- the short body-from-file spelling."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", f"repos/{REPO}/issues/1/comments", "-i", "payload.json"]
    )
    assert "body" in message.lower()
    assert "gh api" in rule


# ---------------------------------------------------------------------------
# 2b. The same body flags with the flag BEFORE the endpoint.
#
# `gh api` parses its flags with Go's flag package, so the endpoint is NOT
# position-locked: `gh api -f body=x repos/o/r/issues/1/comments` is the
# same write as `gh api repos/o/r/issues/1/comments -f body=x`.  The deny
# rules for the path-first spelling are anchored on the path coming first,
# so without a rule for the flag-first ordering the PERMISSION layer
# matched nothing and the write was let through.  Each ordering gets its
# own named test at both layers.
# ---------------------------------------------------------------------------


def test_gh_api_with_dash_f_field_before_the_path_is_refused() -> None:
    """``gh api -f body=... <path>`` -- the body flag first."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "-f", "body=lgtm", f"repos/{REPO}/issues/1/comments"]
    )
    assert "body" in message.lower()
    assert "gh api" in rule


def test_gh_api_with_dash_capital_f_field_before_the_path_is_refused() -> None:
    """``gh api -F body=... <path>`` -- the typed-field spelling, flag first."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "-F", "body=lgtm", f"repos/{REPO}/issues/1/comments"]
    )
    assert "body" in message.lower()
    assert "gh api" in rule


def test_gh_api_with_field_flag_before_the_path_is_refused() -> None:
    """``gh api --field body=... <path>``."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "--field", "body=lgtm", f"repos/{REPO}/issues/1/comments"]
    )
    assert "body" in message.lower()
    assert "gh api" in rule


def test_gh_api_with_raw_field_flag_before_the_path_is_refused() -> None:
    """``gh api --raw-field body=... <path>``."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "--raw-field", "body=lgtm", f"repos/{REPO}/issues/1/comments"]
    )
    assert "body" in message.lower()
    assert "gh api" in rule


def test_gh_api_with_input_flag_before_the_path_is_refused() -> None:
    """``gh api --input payload.json <path>`` -- a body read from a file."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "--input", "payload.json", f"repos/{REPO}/issues/1/comments"]
    )
    assert "body" in message.lower()
    assert "gh api" in rule


def test_gh_api_with_input_equals_flag_before_the_path_is_refused() -> None:
    """``gh api --input=payload.json <path>`` -- the joined spelling, flag first."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "--input=payload.json", f"repos/{REPO}/issues/1/comments"]
    )
    assert "body" in message.lower()
    assert "gh api" in rule


def test_gh_api_with_dash_i_input_flag_before_the_path_is_refused() -> None:
    """``gh api -i payload.json <path>`` -- the short body-from-file spelling."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "-i", "payload.json", f"repos/{REPO}/issues/1/comments"]
    )
    assert "body" in message.lower()
    assert "gh api" in rule


def test_gh_api_with_a_mutation_field_before_the_graphql_endpoint_is_refused() -> None:
    """``gh api -f query=mutation{...} graphql`` -- a mutation, flag first."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "-f", "query=mutation{addComment}", "graphql"]
    )
    assert "gh api" in rule


def test_gh_api_with_x_post_and_a_field_before_the_path_is_refused() -> None:
    """``gh api -X POST -f body=... <path>`` -- a method AND a body, flag first."""

    message, rule = _refused_at_both_layers(
        [
            "gh",
            "api",
            "-X",
            "POST",
            "-f",
            "body=lgtm",
            f"repos/{REPO}/issues/1/comments",
        ]
    )
    assert "gh api" in rule


def test_gh_api_with_method_post_and_a_field_before_the_path_is_refused() -> None:
    """``gh api --method=POST -f body=... <path>`` -- the long-flag spelling."""

    message, rule = _refused_at_both_layers(
        [
            "gh",
            "api",
            "--method=POST",
            "-f",
            "body=lgtm",
            f"repos/{REPO}/issues/1/comments",
        ]
    )
    assert "gh api" in rule


def test_gh_api_with_method_post_and_a_field_is_refused() -> None:
    """A method AND a body: refused, and still named as a write."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "-X", "POST", f"repos/{REPO}/issues/1/comments", "-f", "body=lgtm"]
    )
    assert "read-only" in message.lower()
    assert "gh api" in rule


def test_gh_api_graphql_with_a_mutation_field_is_refused() -> None:
    """``gh api graphql -f query=mutation{...}`` -- a write through GraphQL."""

    message, rule = _refused_at_both_layers(
        ["gh", "api", "graphql", "-f", "query=mutation{addComment}"]
    )
    assert "body" in message.lower()
    assert "gh api" in rule


# ---------------------------------------------------------------------------
# 3. `gh pr` write leaves.
# ---------------------------------------------------------------------------


def test_gh_pr_merge_is_refused() -> None:
    """``gh pr merge`` -- the merge path."""

    message, rule = _refused_at_both_layers(["gh", "pr", "merge", "181"])
    assert "merge" in message.lower()
    assert "gh pr" in rule


def test_gh_pr_merge_with_flags_is_refused() -> None:
    """``gh pr merge 181 --squash --delete-branch`` -- merge plus flags."""

    message, rule = _refused_at_both_layers(
        ["gh", "pr", "merge", "181", "--squash", "--delete-branch"]
    )
    assert "merge" in message.lower()
    assert "gh pr" in rule


def test_gh_pr_merge_with_squash_is_refused() -> None:
    """``gh pr merge 181 --squash`` -- merge spelled as a squash.

    The combined ``--squash --delete-branch`` spelling is covered by
    ``test_gh_pr_merge_with_flags_is_refused``; the bare squash flag is
    proven on its own so that neither spelling leans on the other.
    """

    message, rule = _refused_at_both_layers(["gh", "pr", "merge", "181", "--squash"])
    assert "merge" in message.lower()
    assert "gh pr" in rule


def test_gh_pr_merge_with_admin_is_refused() -> None:
    """``gh pr merge 181 --admin`` -- merge forced through the admin path.

    ``--admin`` is the spelling that bypasses the branch protection rules,
    so it is the one merge flag that must never be waved through.
    """

    message, rule = _refused_at_both_layers(["gh", "pr", "merge", "181", "--admin"])
    assert "merge" in message.lower()
    assert "gh pr" in rule


def test_gh_pr_merge_with_rebase_is_refused() -> None:
    """``gh pr merge 181 --rebase`` -- merge spelled as a rebase."""

    message, rule = _refused_at_both_layers(["gh", "pr", "merge", "181", "--rebase"])
    assert "merge" in message.lower()
    assert "gh pr" in rule


def test_gh_pr_merge_with_auto_merge_is_refused() -> None:
    """``gh pr merge 181 --auto`` -- the auto-merge spelling."""

    message, rule = _refused_at_both_layers(["gh", "pr", "merge", "181", "--auto"])
    assert "merge" in message.lower()
    assert "gh pr" in rule


def test_gh_pr_close_is_refused() -> None:
    """``gh pr close``."""

    message, rule = _refused_at_both_layers(["gh", "pr", "close", "181"])
    assert "close" in message.lower()
    assert "gh pr" in rule


def test_gh_pr_reopen_is_refused() -> None:
    """``gh pr reopen`` -- the reopen path."""

    message, rule = _refused_at_both_layers(["gh", "pr", "reopen", "181"])
    assert "reopen" in message.lower()
    assert "gh pr" in rule


def test_gh_pr_ready_is_refused() -> None:
    """``gh pr ready``."""

    message, rule = _refused_at_both_layers(["gh", "pr", "ready", "181"])
    assert "ready" in message.lower()
    assert "gh pr" in rule


def test_gh_pr_edit_is_refused() -> None:
    """``gh pr edit`` -- a field write on the pull request."""

    message, rule = _refused_at_both_layers(["gh", "pr", "edit", "181", "--title", "x"])
    assert "edit" in message.lower()
    assert "gh pr" in rule


def test_gh_pr_comment_is_refused() -> None:
    """``gh pr comment`` -- a comment write."""

    message, rule = _refused_at_both_layers(["gh", "pr", "comment", "181", "--body", "lgtm"])
    assert "comment" in message.lower()
    assert "gh pr" in rule


def test_gh_pr_create_is_refused() -> None:
    """``gh pr create``."""

    message, rule = _refused_at_both_layers(["gh", "pr", "create", "--title", "x"])
    assert "create" in message.lower()
    assert "gh pr" in rule


def test_gh_pr_review_approve_is_refused() -> None:
    """``gh pr review --approve`` -- an approval is a write."""

    message, rule = _refused_at_both_layers(["gh", "pr", "review", "181", "--approve"])
    assert "review" in message.lower()
    assert "gh pr" in rule


def test_gh_pr_review_request_changes_is_refused() -> None:
    """``gh pr review --request-changes``."""

    message, rule = _refused_at_both_layers(
        ["gh", "pr", "review", "181", "--request-changes"]
    )
    assert "review" in message.lower()
    assert "gh pr" in rule


def test_gh_pr_review_comment_is_refused() -> None:
    """``gh pr review --comment`` -- a review comment is a write."""

    message, rule = _refused_at_both_layers(
        ["gh", "pr", "review", "181", "--comment", "lgtm"]
    )
    assert "review" in message.lower()
    assert "gh pr" in rule


# ---------------------------------------------------------------------------
# 4. `gh issue` write leaves.
# ---------------------------------------------------------------------------


def test_gh_issue_comment_is_refused() -> None:
    """``gh issue comment``."""

    message, rule = _refused_at_both_layers(["gh", "issue", "comment", "1", "--body", "x"])
    assert "comment" in message.lower()
    assert "gh issue" in rule


def test_gh_issue_create_is_refused() -> None:
    """``gh issue create`` -- a ticket create."""

    message, rule = _refused_at_both_layers(["gh", "issue", "create", "--title", "x"])
    assert "create" in message.lower()
    assert "gh issue" in rule


def test_gh_issue_close_is_refused() -> None:
    """``gh issue close``."""

    message, rule = _refused_at_both_layers(["gh", "issue", "close", "1"])
    assert "close" in message.lower()
    assert "gh issue" in rule


def test_gh_issue_reopen_is_refused() -> None:
    """``gh issue reopen``."""

    message, rule = _refused_at_both_layers(["gh", "issue", "reopen", "1"])
    assert "reopen" in message.lower()
    assert "gh issue" in rule


def test_gh_issue_delete_is_refused() -> None:
    """``gh issue delete``."""

    message, rule = _refused_at_both_layers(["gh", "issue", "delete", "1"])
    assert "delete" in message.lower()
    assert "gh issue" in rule


def test_gh_issue_edit_is_refused() -> None:
    """``gh issue edit`` -- a field write."""

    message, rule = _refused_at_both_layers(["gh", "issue", "edit", "1", "--title", "x"])
    assert "edit" in message.lower()
    assert "gh issue" in rule


# ---------------------------------------------------------------------------
# 5. `gh release` / `secret` / `variable` / `workflow` / `run` writes.
# ---------------------------------------------------------------------------


def test_gh_release_create_is_refused() -> None:
    """``gh release create``."""

    message, rule = _refused_at_both_layers(["gh", "release", "create", "v1"])
    assert "create" in message.lower()
    assert "gh release" in rule


def test_gh_release_edit_is_refused() -> None:
    """``gh release edit``."""

    message, rule = _refused_at_both_layers(["gh", "release", "edit", "v1"])
    assert "edit" in message.lower()
    assert "gh release" in rule


def test_gh_release_upload_is_refused() -> None:
    """``gh release upload``."""

    message, rule = _refused_at_both_layers(["gh", "release", "upload", "v1", "asset.tar.gz"])
    assert "upload" in message.lower()
    assert "gh release" in rule


def test_gh_release_delete_is_refused() -> None:
    """``gh release delete``."""

    message, rule = _refused_at_both_layers(["gh", "release", "delete", "v1"])
    assert "delete" in message.lower()
    assert "gh release" in rule


def test_gh_secret_set_is_refused() -> None:
    """``gh secret set`` -- a write that hides under a read group."""

    message, rule = _refused_at_both_layers(["gh", "secret", "set", "K", "V"])
    assert "set" in message.lower()
    assert "gh secret" in rule


def test_gh_secret_delete_is_refused() -> None:
    """``gh secret delete`` -- a write that hides under a read group."""

    message, rule = _refused_at_both_layers(["gh", "secret", "delete", "K"])
    assert "delete" in message.lower()
    assert "gh secret" in rule


def test_gh_variable_set_is_refused() -> None:
    """``gh variable set`` -- a write that hides under a read group."""

    message, rule = _refused_at_both_layers(["gh", "variable", "set", "K", "V"])
    assert "set" in message.lower()
    assert "gh variable" in rule


def test_gh_variable_delete_is_refused() -> None:
    """``gh variable delete`` -- a write that hides under a read group."""

    message, rule = _refused_at_both_layers(["gh", "variable", "delete", "K"])
    assert "delete" in message.lower()
    assert "gh variable" in rule


def test_gh_workflow_run_is_refused() -> None:
    """``gh workflow run`` -- a dispatch, spelled like a read."""

    message, rule = _refused_at_both_layers(["gh", "workflow", "run", "ci.yml"])
    assert "run" in message.lower()
    assert "gh workflow" in rule


def test_gh_workflow_dispatch_is_refused() -> None:
    """``gh workflow dispatch``."""

    message, rule = _refused_at_both_layers(["gh", "workflow", "dispatch", "ci.yml"])
    assert "dispatch" in message.lower()
    assert "gh workflow" in rule


def test_gh_workflow_enable_is_refused() -> None:
    """``gh workflow enable``."""

    message, rule = _refused_at_both_layers(["gh", "workflow", "enable", "ci.yml"])
    assert "enable" in message.lower()
    assert "gh workflow" in rule


def test_gh_run_rerun_is_refused() -> None:
    """``gh run rerun``."""

    message, rule = _refused_at_both_layers(["gh", "run", "rerun", "1"])
    assert "rerun" in message.lower()
    assert "gh run" in rule


def test_gh_run_cancel_is_refused() -> None:
    """``gh run cancel``."""

    message, rule = _refused_at_both_layers(["gh", "run", "cancel", "1"])
    assert "cancel" in message.lower()
    assert "gh run" in rule


def test_gh_run_delete_is_refused() -> None:
    """``gh run delete``."""

    message, rule = _refused_at_both_layers(["gh", "run", "delete", "1"])
    assert "delete" in message.lower()
    assert "gh run" in rule


# ---------------------------------------------------------------------------
# 6. `gh repo` / `gist` / `label` writes.
# ---------------------------------------------------------------------------


def test_gh_repo_edit_is_refused() -> None:
    """``gh repo edit``."""

    message, rule = _refused_at_both_layers(["gh", "repo", "edit", "owner/repo"])
    assert "edit" in message.lower()
    assert "gh repo" in rule


def test_gh_repo_create_is_refused() -> None:
    """``gh repo create``."""

    message, rule = _refused_at_both_layers(["gh", "repo", "create", "x"])
    assert "create" in message.lower()
    assert "gh repo" in rule


def test_gh_repo_delete_is_refused() -> None:
    """``gh repo delete``."""

    message, rule = _refused_at_both_layers(["gh", "repo", "delete", "owner/repo"])
    assert "delete" in message.lower()
    assert "gh repo" in rule


def test_gh_gist_create_is_refused() -> None:
    """``gh gist create``."""

    message, rule = _refused_at_both_layers(["gh", "gist", "create", "notes.txt"])
    assert "gist" in message.lower()
    assert "gh gist" in rule


def test_gh_label_create_is_refused() -> None:
    """``gh label create``."""

    message, rule = _refused_at_both_layers(["gh", "label", "create", "x"])
    assert "create" in message.lower()
    assert "gh label" in rule


def test_gh_label_edit_is_refused() -> None:
    """``gh label edit``."""

    message, rule = _refused_at_both_layers(["gh", "label", "edit", "x"])
    assert "edit" in message.lower()
    assert "gh label" in rule


def test_gh_label_delete_is_refused() -> None:
    """``gh label delete``."""

    message, rule = _refused_at_both_layers(["gh", "label", "delete", "x"])
    assert "delete" in message.lower()
    assert "gh label" in rule


def test_gh_gist_delete_is_refused() -> None:
    """``gh gist delete`` -- the deny list had only ``gh gist create``."""

    message, rule = _refused_at_both_layers(["gh", "gist", "delete", "id"])
    assert "gist" in message.lower()
    assert "gh gist" in rule


def test_gh_gist_edit_is_refused() -> None:
    """``gh gist edit`` -- the deny list had only ``gh gist create``."""

    message, rule = _refused_at_both_layers(["gh", "gist", "edit", "id"])
    assert "gist" in message.lower()
    assert "gh gist" in rule


def test_gh_pr_lock_is_refused() -> None:
    """``gh pr lock`` -- locking a conversation is a write on the PR."""

    message, rule = _refused_at_both_layers(["gh", "pr", "lock", "181"])
    assert "lock" in message.lower()
    assert "gh pr" in rule


def test_gh_pr_unlock_is_refused() -> None:
    """``gh pr unlock`` -- the write leaf, in the PR group."""

    message, rule = _refused_at_both_layers(["gh", "pr", "unlock", "181"])
    assert "unlock" in message.lower()
    assert "gh pr" in rule


def test_gh_alias_set_is_refused() -> None:
    """``gh alias set`` -- the alias group is write-only."""

    message, rule = _refused_at_both_layers(["gh", "alias", "set", "x", "y"])
    assert "gh alias" in rule


def test_gh_alias_delete_is_refused() -> None:
    """``gh alias delete``."""

    message, rule = _refused_at_both_layers(["gh", "alias", "delete", "x"])
    assert "gh alias" in rule


def test_gh_extension_install_is_refused() -> None:
    """``gh extension install`` -- the extension group is write-only."""

    message, rule = _refused_at_both_layers(["gh", "extension", "install", "x"])
    assert "gh extension" in rule


def test_gh_extension_remove_is_refused() -> None:
    """``gh extension remove``."""

    message, rule = _refused_at_both_layers(["gh", "extension", "remove", "x"])
    assert "gh extension" in rule


def test_gh_gpg_key_delete_is_refused() -> None:
    """``gh gpg-key delete`` -- a credential on the account is removed."""

    message, rule = _refused_at_both_layers(["gh", "gpg-key", "delete", "k"])
    assert "gh gpg-key" in rule


def test_gh_org_create_is_refused() -> None:
    """``gh org create`` -- the org group is write-only."""

    message, rule = _refused_at_both_layers(["gh", "org", "create", "x"])
    assert "gh org" in rule


def test_gh_org_add_member_is_refused() -> None:
    """``gh org add-member``."""

    message, rule = _refused_at_both_layers(["gh", "org", "add-member", "x"])
    assert "gh org" in rule


def test_gh_team_create_is_refused() -> None:
    """``gh team create`` -- the team group is write-only."""

    message, rule = _refused_at_both_layers(["gh", "team", "create", "x"])
    assert "gh team" in rule


def test_gh_sso_create_is_refused() -> None:
    """``gh sso create`` -- the sso group is write-only."""

    message, rule = _refused_at_both_layers(["gh", "sso", "create", "x"])
    assert "gh sso" in rule


def test_gh_gist_rename_is_refused() -> None:
    """``gh gist rename`` -- renaming a file in a gist is a write."""

    message, rule = _refused_at_both_layers(["gh", "gist", "rename", "id", "new.txt"])
    assert "gist" in message.lower()
    assert "gh gist" in rule


def test_gh_gist_view_is_refused() -> None:
    """``gh gist view`` -- the gist group is a WRITE group for both layers.

    The two layers must agree: the in-process classifier refuses the whole
    gist group, so the permission layer denies it too.  A read the auditor
    can perform is served by the harness's own verifier, not by ``gh``.
    """

    message, rule = _refused_at_both_layers(["gh", "gist", "view", "id"])
    assert "gist" in message.lower()
    assert "gh gist" in rule


def test_gh_gist_list_is_refused() -> None:
    """``gh gist list`` -- the gist group is a WRITE group for both layers."""

    message, rule = _refused_at_both_layers(["gh", "gist", "list"])
    assert "gist" in message.lower()
    assert "gh gist" in rule


def test_gh_gist_clone_is_refused() -> None:
    """``gh gist clone`` -- the gist group is a WRITE group for both layers."""

    message, rule = _refused_at_both_layers(["gh", "gist", "clone", "id"])
    assert "gist" in message.lower()
    assert "gh gist" in rule


def test_gh_issue_lock_is_refused() -> None:
    """``gh issue lock`` -- locking a conversation is a write."""

    message, rule = _refused_at_both_layers(["gh", "issue", "lock", "1"])
    assert "lock" in message.lower()
    assert "gh issue" in rule


def test_gh_issue_unlock_is_refused() -> None:
    """``gh issue unlock`` -- the write leaf, in the issue group."""

    message, rule = _refused_at_both_layers(["gh", "issue", "unlock", "1"])
    assert "unlock" in message.lower()
    assert "gh issue" in rule


def test_gh_project_create_is_refused() -> None:
    """``gh project create``."""

    message, rule = _refused_at_both_layers(["gh", "project", "create", "x"])
    assert "gh project" in rule


def test_gh_project_edit_is_refused() -> None:
    """``gh project edit``."""

    message, rule = _refused_at_both_layers(["gh", "project", "edit", "1"])
    assert "gh project" in rule


def test_gh_project_close_is_refused() -> None:
    """``gh project close``."""

    message, rule = _refused_at_both_layers(["gh", "project", "close", "1"])
    assert "gh project" in rule


def test_gh_project_delete_is_refused() -> None:
    """``gh project delete``."""

    message, rule = _refused_at_both_layers(["gh", "project", "delete", "1"])
    assert "gh project" in rule


def test_gh_cache_delete_is_refused() -> None:
    """``gh cache delete`` -- removing a cache entry is a write."""

    message, rule = _refused_at_both_layers(["gh", "cache", "delete", "x"])
    assert "gh cache" in rule


def test_gh_config_set_is_refused() -> None:
    """``gh config set`` -- writing the local gh configuration is a write."""

    message, rule = _refused_at_both_layers(["gh", "config", "set", "x", "y"])
    assert "gh config" in rule


# ---------------------------------------------------------------------------
# 7. `git` writes.
# ---------------------------------------------------------------------------


def test_git_push_with_a_remote_is_refused() -> None:
    """``git push origin main`` -- the push path."""

    message, rule = _refused_at_both_layers(["git", "push", "origin", "main"])
    assert "push" in message.lower()
    assert "git push" in rule


def test_git_push_without_a_remote_is_refused() -> None:
    """``git push`` -- no remote named, still a push."""

    message, rule = _refused_at_both_layers(["git", "push"])
    assert "push" in message.lower()
    assert "git push" in rule


def test_git_push_with_a_force_flag_is_refused() -> None:
    """``git push --force`` -- the force-push path."""

    message, rule = _refused_at_both_layers(["git", "push", "--force", "origin", "main"])
    assert "push" in message.lower()
    assert "git push" in rule


def test_git_push_with_force_with_lease_is_refused() -> None:
    """``git push --force-with-lease`` -- the polite force-push path."""

    message, rule = _refused_at_both_layers(
        ["git", "push", "--force-with-lease", "origin", "main"]
    )
    assert "push" in message.lower()
    assert "git push" in rule


# ---------------------------------------------------------------------------
# 8. The shape, not the literal string.
# ---------------------------------------------------------------------------


def test_a_renamed_write_is_refused_by_shape_not_by_literal() -> None:
    """The refusal follows the SHAPE, so a re-spelled write is refused too.

    ``gh pr merge 181 --squash`` is a different string from
    ``gh pr merge 181``; a layer that matched one literal command string
    would let this through.
    """

    assert write_intent_reason is not None, (
        "the harness has no write-intent classifier: the refusal cannot be "
        "shown to follow the shape of the invocation"
    )
    reason = write_intent_reason(["gh", "pr", "merge", "999", "--squash"])

    assert reason is not None
    assert "merge" in reason.lower()


def test_a_parameterised_write_is_refused_by_shape() -> None:
    """A write with a different argument value has the same shape."""

    assert write_intent_reason is not None
    reason = write_intent_reason(["gh", "pr", "merge", "12345"])

    assert reason is not None
    assert "merge" in reason.lower()


def test_a_write_method_with_a_different_path_is_refused_by_shape() -> None:
    """The path does not change the shape of the write."""

    assert write_intent_reason is not None
    reason = write_intent_reason(["gh", "api", "--method=POST", "repos/other/repo/issues/7/comments"])

    assert reason is not None
    assert "POST" in reason


def test_the_classifier_reads_are_not_writes() -> None:
    """The classifier distinguishes a read from a write on shape alone."""

    assert write_intent_reason is not None
    assert write_intent_reason(["gh", "pr", "view", "181"]) is None
    assert write_intent_reason(["gh", "api", f"repos/{REPO}/pulls/181"]) is None
    assert write_intent_reason(["gh", "api", "--method=GET", f"repos/{REPO}/pulls/181"]) is None
    assert write_intent_reason(["git", "log", "--oneline", "-1"]) is None


# ---------------------------------------------------------------------------
# 9. The deny list has no hole.
# ---------------------------------------------------------------------------


def test_every_write_spelling_is_refused_by_the_classifier() -> None:
    """Every spelling in the canonical list is classified as a write."""

    assert write_intent_reason is not None
    for spelling in AUDITOR_WRITE_SPELLINGS:
        reason = write_intent_reason(spelling)
        assert reason is not None, (
            f"{command_string(spelling)!r} was not classified as a write"
        )


def test_every_write_spelling_is_denied_by_the_permission_layer() -> None:
    """No spelling is left uncovered by the deny rules.

    This is the invariant that closes the gap the previous round recorded:
    a write spelling that the classifier refuses but NO deny rule matches is
    a shell-out the permission layer would have allowed.
    """

    assert uncovered_auditor_write_spellings is not None, (
        "the harness does not compute the deny list's coverage: a write "
        "spelling with no matching rule cannot be detected"
    )
    uncovered = uncovered_auditor_write_spellings()

    assert uncovered == (), f"write spellings with no matching deny rule: {uncovered}"


def test_every_write_spelling_is_refused_at_both_layers() -> None:
    """Both layers refuse every spelling in the canonical list."""

    assert is_denied_by_auditor_policy is not None
    for spelling in AUDITOR_WRITE_SPELLINGS:
        argv = list(spelling)
        with pytest.raises(ReadOnlyViolation):
            assert_read_only_command(argv)
        assert is_denied_by_auditor_policy(command_string(argv), ALL_DENY_RULES) is not None


def test_the_deny_list_has_no_unmatched_rule() -> None:
    """Every deny rule matches at least one known write spelling.

    A rule that matches nothing is dead weight: it looks like a defence and
    is not one.
    """

    assert deny_rule_matches is not None
    commands = [command_string(spelling) for spelling in AUDITOR_WRITE_SPELLINGS]
    for rule in ALL_DENY_RULES:
        assert any(deny_rule_matches(command, rule) for command in commands), (
            f"deny rule {rule!r} matches no known write spelling"
        )


# ---------------------------------------------------------------------------
# 9c. The permission layer must not depend on FLAG ORDER.
# ---------------------------------------------------------------------------
#
# `gh api` parses its flags with Go's flag package, so the endpoint is not
# position-locked.  A deny rule anchored on the path coming first therefore
# covers only ONE of the two orderings, and the other ordering is a shell-out
# the permission layer would have allowed.  The canonical list carries the
# path-first spelling; this test walks the flag-first spelling of every one
# of them and requires that BOTH layers refuse each.
#
# This is the invariant that keeps the hole closed: it fails whenever a new
# body flag is added to the classifier without a matching deny rule, whatever
# order the flag and the endpoint are given in.

_GH_API_BODY_FLAGS: tuple[tuple[str, ...], ...] = (
    ("-f", "body=lgtm"),
    ("-F", "body=lgtm"),
    ("--field", "body=lgtm"),
    ("--raw-field", "body=lgtm"),
    ("--input", "payload.json"),
    ("--input=payload.json",),
    ("-i", "payload.json"),
)


def test_every_gh_api_body_flag_is_refused_whatever_its_position() -> None:
    """The body flag's POSITION must not change whether the write is refused.

    For every body flag, and for every position the flag can take relative
    to the endpoint, the write must be refused by the in-process layer AND
    denied by the permission layer.
    """

    assert is_denied_by_auditor_policy is not None
    endpoints = (
        f"repos/{REPO}/issues/1/comments",
        f"repos/{REPO}/pulls/181/comments",
        "graphql",
    )

    uncovered: list[str] = []
    for flag in _GH_API_BODY_FLAGS:
        for endpoint in endpoints:
            # The flag may come before the endpoint, after it, or (for the
            # two-token flags) split around it.
            positions = [
                ["gh", "api", *flag, endpoint],
                ["gh", "api", endpoint, *flag],
            ]
            if len(flag) == 2:
                positions.append(["gh", "api", flag[0], endpoint, flag[1]])

            for argv in positions:
                command = command_string(argv)
                with pytest.raises(ReadOnlyViolation):
                    assert_read_only_command(argv)
                if is_denied_by_auditor_policy(command, ALL_DENY_RULES) is None:
                    uncovered.append(command)

    assert uncovered == [], (
        "write spellings the permission layer would have allowed because the "
        f"body flag's position changed: {uncovered}"
    )


# ---------------------------------------------------------------------------
# 9d. The two layers must AGREE about what a write is.
# ---------------------------------------------------------------------------
#
# The in-process classifier and the permission layer are separate defences,
# and each is proven on its own.  The proof is only meaningful if the two
# layers agree on WHICH commands are writes: a command the classifier
# refuses but no deny rule matches is a shell-out the permission layer would
# have allowed, and a command a deny rule matches but the classifier calls a
# read is a read the auditor can no longer perform.
#
# The table below is the `gh`/`git` command surface.  It is walked here so
# that a write verb added to one layer without the other is a test failure
# rather than a hole discovered in an audit.

_LAYER_AGREEMENT_COMMANDS: tuple[tuple[str, ...], ...] = (
    # gh api, by method.
    ("gh", "api", "--method=POST", "repos/o/r/pulls/1/comments"),
    ("gh", "api", "-X", "POST", "repos/o/r/pulls/1/comments"),
    ("gh", "api", "-XPOST", "repos/o/r/pulls/1/comments"),
    ("gh", "api", "--method", "PATCH", "repos/o/r/pulls/1"),
    ("gh", "api", "--method=PUT", "repos/o/r/pulls/1"),
    ("gh", "api", "--method=DELETE", "repos/o/r/pulls/1"),
    # gh api, by body.
    ("gh", "api", "repos/o/r/issues/1/comments", "-f", "body=x"),
    ("gh", "api", "-f", "body=x", "repos/o/r/issues/1/comments"),
    ("gh", "api", "repos/o/r/issues/1/comments", "--input=payload.json"),
    ("gh", "api", "--input=payload.json", "repos/o/r/issues/1/comments"),
    # gh pr.
    ("gh", "pr", "merge", "181"),
    ("gh", "pr", "merge", "181", "--squash", "--delete-branch"),
    ("gh", "pr", "merge", "181", "--admin"),
    ("gh", "pr", "close", "181"),
    ("gh", "pr", "reopen", "181"),
    ("gh", "pr", "ready", "181"),
    ("gh", "pr", "lock", "181"),
    ("gh", "pr", "unlock", "181"),
    ("gh", "pr", "edit", "181", "--title", "x"),
    ("gh", "pr", "comment", "181", "--body", "x"),
    ("gh", "pr", "create", "--title", "x"),
    ("gh", "pr", "review", "181", "--approve"),
    ("gh", "pr", "review", "181", "--request-changes"),
    ("gh", "pr", "review", "181", "--comment", "x"),
    # gh issue.
    ("gh", "issue", "comment", "1", "--body", "x"),
    ("gh", "issue", "create", "--title", "x"),
    ("gh", "issue", "close", "1"),
    ("gh", "issue", "reopen", "1"),
    ("gh", "issue", "delete", "1"),
    ("gh", "issue", "edit", "1", "--title", "x"),
    # gh release / secret / variable / workflow / run.
    ("gh", "release", "create", "v1"),
    ("gh", "release", "edit", "v1"),
    ("gh", "release", "upload", "v1", "a.tar.gz"),
    ("gh", "release", "delete", "v1"),
    ("gh", "secret", "set", "K", "V"),
    ("gh", "secret", "delete", "K"),
    ("gh", "variable", "set", "K", "V"),
    ("gh", "variable", "delete", "K"),
    ("gh", "workflow", "run", "ci.yml"),
    ("gh", "workflow", "dispatch", "ci.yml"),
    ("gh", "workflow", "enable", "ci.yml"),
    ("gh", "run", "rerun", "1"),
    ("gh", "run", "cancel", "1"),
    ("gh", "run", "delete", "1"),
    # gh repo / gist / label.
    ("gh", "repo", "edit", "o/r"),
    ("gh", "repo", "create", "x"),
    ("gh", "repo", "delete", "o/r"),
    ("gh", "gist", "create", "notes.txt"),
    ("gh", "gist", "delete", "id"),
    ("gh", "gist", "edit", "id"),
    ("gh", "gist", "rename", "id", "new.txt"),
    ("gh", "gist", "view", "id"),
    ("gh", "gist", "list"),
    ("gh", "gist", "clone", "id"),
    ("gh", "label", "create", "x"),
    ("gh", "label", "edit", "x"),
    ("gh", "label", "delete", "x"),
    # gh issue locking, gh project, gh cache, gh config.
    ("gh", "issue", "lock", "1"),
    ("gh", "issue", "unlock", "1"),
    ("gh", "project", "create", "x"),
    ("gh", "project", "edit", "1"),
    ("gh", "project", "close", "1"),
    ("gh", "project", "delete", "1"),
    ("gh", "cache", "delete", "x"),
    ("gh", "config", "set", "x", "y"),
    # The write-only groups.
    ("gh", "codespace", "code"),
    ("gh", "ruleset", "create"),
    ("gh", "ssh-key", "add", "k"),
    ("gh", "gpg-key", "delete", "k"),
    ("gh", "api-write", "repos/o/r/issues/1/comments"),
    ("gh", "alias", "set", "x", "y"),
    ("gh", "alias", "delete", "x"),
    ("gh", "extension", "install", "x"),
    ("gh", "extension", "remove", "x"),
    ("gh", "org", "create", "x"),
    ("gh", "org", "add-member", "x"),
    ("gh", "team", "create", "x"),
    ("gh", "sso", "create", "x"),
    # git.
    ("git", "push", "origin", "main"),
    ("git", "push"),
    ("git", "push", "--force", "origin", "main"),
    ("git", "push", "--force-with-lease", "origin", "main"),
    ("git", "fetch", "origin"),
    ("git", "pull", "origin", "main"),
    ("git", "remote", "add", "u", "https://x.invalid/r.git"),
    ("git", "clone", "https://x.invalid/r.git"),
    ("git", "submodule", "update", "--init"),
    ("git", "lfs", "pull"),
    ("git", "-C", "/tmp/checkout", "push", "origin", "main"),
    ("git", "-C", "/tmp/checkout", "fetch", "origin"),
    ("git", "--git-dir=/tmp/checkout/.git", "push", "origin", "main"),
    ("git", "--git-dir=/tmp/checkout/.git", "fetch", "origin"),
)


def test_the_two_layers_agree_on_every_write_in_the_command_surface() -> None:
    """A write the classifier refuses must be DENIED by a rule, and vice versa.

    The two layers are separate defences; a disagreement is a hole in one
    of them, whichever way it points.
    """

    assert is_denied_by_auditor_policy is not None

    classifier_only: list[str] = []
    policy_only: list[str] = []

    for argv in _LAYER_AGREEMENT_COMMANDS:
        argv = list(argv)
        command = command_string(argv)

        classifier_refuses = False
        try:
            assert_read_only_command(argv)
        except ReadOnlyViolation:
            classifier_refuses = True

        policy_denies = is_denied_by_auditor_policy(command, ALL_DENY_RULES) is not None

        if classifier_refuses and not policy_denies:
            classifier_only.append(command)
        elif policy_denies and not classifier_refuses:
            policy_only.append(command)

    assert classifier_only == [], (
        "writes the in-process layer refuses but no deny rule matches: "
        f"{classifier_only}"
    )
    assert policy_only == [], (
        "commands a deny rule matches that the in-process layer calls a read: "
        f"{policy_only}"
    )


# ---------------------------------------------------------------------------
# 9b. The holes this change closes, one test each.
# ---------------------------------------------------------------------------
#
# These six spellings were refused by the in-process layer but matched NO
# deny rule: the permission layer would have allowed the shell-out.  Each
# gets its own named test so the specific hole cannot be reopened by
# editing a list.
#
# The matching here is done with the stdlib glob, not with the harness's
# matcher, so the test states the fact about the deny LIST itself and does
# not depend on the matcher being present.


def _deny_rule_matches_any(command: str) -> bool:
    """True when any deny rule matches *command* (stdlib glob semantics)."""

    import fnmatch

    for rule in ALL_DENY_RULES:
        pattern = rule[len("Bash(") : -1]
        if fnmatch.fnmatchcase(command, pattern):
            return True
    return False


def test_gh_issue_delete_is_denied_by_a_deny_rule() -> None:
    """``gh issue delete`` -- previously matched by no deny rule at all."""

    assert _deny_rule_matches_any("gh issue delete 1"), (
        "no deny rule matches 'gh issue delete 1': the permission layer "
        "would allow the shell-out"
    )


def test_gh_issue_reopen_is_denied_by_a_deny_rule() -> None:
    """``gh issue reopen`` -- previously matched by no deny rule at all."""

    assert _deny_rule_matches_any("gh issue reopen 1"), (
        "no deny rule matches 'gh issue reopen 1': the permission layer "
        "would allow the shell-out"
    )


def test_gh_release_delete_is_denied_by_a_deny_rule() -> None:
    """``gh release delete`` -- previously matched by no deny rule at all."""

    assert _deny_rule_matches_any("gh release delete v1"), (
        "no deny rule matches 'gh release delete v1': the permission layer "
        "would allow the shell-out"
    )


def test_gh_secret_delete_is_denied_by_a_deny_rule() -> None:
    """``gh secret delete`` -- previously matched by no deny rule at all."""

    assert _deny_rule_matches_any("gh secret delete K"), (
        "no deny rule matches 'gh secret delete K': the permission layer "
        "would allow the shell-out"
    )


def test_gh_variable_delete_is_denied_by_a_deny_rule() -> None:
    """``gh variable delete`` -- previously matched by no deny rule at all."""

    assert _deny_rule_matches_any("gh variable delete K"), (
        "no deny rule matches 'gh variable delete K': the permission layer "
        "would allow the shell-out"
    )


def test_gh_api_with_input_equals_is_denied_by_a_deny_rule() -> None:
    """``gh api <path> --input=payload.json`` -- the joined body spelling."""

    assert _deny_rule_matches_any(
        f"gh api repos/{REPO}/issues/1/comments --input=payload.json"
    ), (
        "no deny rule matches the joined '--input=' body spelling: the "
        "permission layer would allow the shell-out"
    )


# ---------------------------------------------------------------------------
# 10. The boundary: reads stay available.
# ---------------------------------------------------------------------------


def test_the_accepted_read_spelling() -> None:
    """A read is NOT refused -- the refusals above are not "everything fails"."""

    assert_read_only_command(["gh", "pr", "view", "181", "--json", "number,state,headRefOid"])
    assert_read_only_command(["gh", "api", f"repos/{REPO}/pulls/181"])
    assert_read_only_command(["gh", "api", "--method=GET", f"repos/{REPO}/pulls/181"])
    assert_read_only_command(["gh", "repo", "view", REPO])
    assert_read_only_command(["git", "log", "--oneline", "-1"])


def test_a_read_is_not_denied_by_the_permission_layer() -> None:
    """The permission layer denies the writes and leaves the reads alone."""

    assert is_denied_by_auditor_policy is not None
    for command in (
        "gh pr view 181 --json number,state,headRefOid",
        f"gh api repos/{REPO}/pulls/181",
        "gh repo view owner/repo",
        "git log --oneline -1",
    ):
        assert is_denied_by_auditor_policy(command, ALL_DENY_RULES) is None, command


# ---------------------------------------------------------------------------
# 11. The HTTP layer: no write request is ever issued.
# ---------------------------------------------------------------------------


class _RecordingTransport:
    """A URL opener that records every request it is handed.

    The response body is chosen by the request path so that a read of the
    pull request and a read of its file list each return the shape the
    client expects.
    """

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, timeout: float | None = None):
        self.requests.append(request)
        url = getattr(request, "full_url", "")
        if url.endswith("/files"):
            body = json.dumps([{"filename": "src/x.py", "additions": 10, "deletions": 2}]).encode()
        else:
            body = json.dumps(self.payload).encode()
        return _FakeResponse(body)


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = io.BytesIO(body)
        self.status = 200

    def read(self) -> bytes:
        return self._body.read()

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


_PULL_REQUEST_PAYLOAD: dict[str, object] = {
    "number": 181,
    "state": "open",
    "merged": False,
    "base": {"ref": "main", "repo": {"full_name": REPO}},
    "head": {"sha": "359dba7e" + "0" * 24, "ref": "feat/x"},
    "additions": 10,
    "deletions": 2,
    "changed_files": 3,
}


def test_the_http_layer_issues_no_write_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading a pull request issues GETs and NOTHING else.

    The command-surface refusals above are about what the auditor may
    SHELL OUT to.  This is the other side: what the auditor's own client
    puts on the wire.  The transport records every request, and the test
    asserts that none of them is a non-GET.
    """

    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "test-auditor-readonly-token-000")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    transport = _RecordingTransport(_PULL_REQUEST_PAYLOAD)
    client = ReadOnlyGitHubClient(opener=transport)

    facts = client.read_pull_request(REPO, 181)

    assert facts.number == 181
    assert facts.head_sha == "359dba7e" + "0" * 24
    assert transport.requests, "the read issued no request at all"

    for request in transport.requests:
        assert request.get_method() == "GET", (
            f"the auditor's client issued a {request.get_method()} request"
        )
        assert request.data is None, "a read request carried a body"


def test_the_http_layer_records_zero_non_get_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Across every read the auditor performs, no write verb is used.

    This is the explicit count: the number of non-GET requests the
    auditor's client put on the wire is ZERO.
    """

    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "test-auditor-readonly-token-000")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    transport = _RecordingTransport(_PULL_REQUEST_PAYLOAD)
    client = ReadOnlyGitHubClient(opener=transport)

    client.read_pull_request(REPO, 181)
    client.read_pull_request_files(REPO, 181)

    verbs = [request.get_method() for request in transport.requests]
    non_get = [verb for verb in verbs if verb != "GET"]

    assert non_get == [], f"the auditor issued non-GET requests: {non_get}"
    assert len(verbs) == 2


def test_the_http_layer_refuses_a_write_verb_before_it_reaches_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write verb is refused WITHOUT a request being issued."""

    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "test-auditor-readonly-token-000")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    transport = _RecordingTransport(_PULL_REQUEST_PAYLOAD)
    client = ReadOnlyGitHubClient(opener=transport)

    with pytest.raises(ReadOnlyViolation):
        client.request("POST", f"/repos/{REPO}/pulls/181/comments")

    assert transport.requests == [], "a refused write still reached the wire"


def test_the_write_helpers_of_the_client_issue_no_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client's write helpers are refused without touching the wire."""

    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "test-auditor-readonly-token-000")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    transport = _RecordingTransport(_PULL_REQUEST_PAYLOAD)
    client = ReadOnlyGitHubClient(opener=transport)

    with pytest.raises(ReadOnlyViolation):
        client.write_pull_request(REPO, 181, {"title": "x"})
    with pytest.raises(ReadOnlyViolation):
        client.create_issue(REPO, "title")
    with pytest.raises(ReadOnlyViolation):
        client.write_anything_at_all(REPO, 181)

    assert transport.requests == [], "a refused write still reached the wire"


# ---------------------------------------------------------------------------
# 12. Credential hygiene.
# ---------------------------------------------------------------------------


def test_no_refusal_carries_a_credential_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not one refusal message echoes a credential value."""

    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "auditor-secret-value-should-not-appear")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    spellings = AUDITOR_WRITE_SPELLINGS or (
        ("gh", "pr", "merge", "181"),
        ("gh", "api", "--method=POST", "repos/owner/repo/issues/1/comments"),
    )
    for spelling in spellings:
        with pytest.raises(ReadOnlyViolation) as excinfo:
            assert_read_only_command(list(spelling))
        assert "auditor-secret-value-should-not-appear" not in str(excinfo.value)


def test_the_auditor_credential_is_not_the_executor_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auditor's own credential source is distinct from the executor's.

    Only the VARIABLE NAMES appear here; no value is printed.
    """

    from lh_harness.auditor_verify import (
        SHARED_CREDENTIAL_ENV_VARS,
        resolve_auditor_credential,
    )

    assert AUDITOR_GITHUB_TOKEN_ENV not in SHARED_CREDENTIAL_ENV_VARS
    assert "GH_TOKEN" in SHARED_CREDENTIAL_ENV_VARS

    monkeypatch.setenv(AUDITOR_GITHUB_TOKEN_ENV, "auditor-own-token-value")
    monkeypatch.setenv("GH_TOKEN", "executor-token-value")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    credential = resolve_auditor_credential()

    assert credential.present
    assert credential.source == AUDITOR_GITHUB_TOKEN_ENV
    assert "auditor-own-token-value" not in repr(credential)
    assert "executor-token-value" not in repr(credential)
