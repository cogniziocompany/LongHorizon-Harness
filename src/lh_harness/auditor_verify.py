"""Auditor read-only GitHub verification surface.

The harness is a validation chain: each step validates the last in its
process to achieve its tasks, so a step may only claim success if the step
before it actually validated the thing being claimed.  The auditor is the
last independent verification step in that chain, and until now it could not
verify anything about the outside world.

Its MCP profile (``audit``) lists only servers whose tools are inherently
non-mutating -- kb, gateway guides/skills, hivemind memory, langfuse -- and
the gateway cannot scope a server to read-only, so adding the ``github``
server there would have handed the auditor WRITE-capable tools.  Its tool
deny-list refused ``Bash(gh *)`` wholesale, which took out the local ``gh``
CLI's read verbs along with the write ones.  That blanket deny has since been
replaced by per-spelling write denies
(:data:`lh_harness.adapters.claude_permissions.AUDITOR_GH_WRITE_DENY`), so the
auditor's reads are no longer collateral damage -- and this module's
enforcement layer is the belt to that deny list's braces.

The consequence was structural.  An auditor with no independent evidence
could only RESTATE the executor's claims or refuse them; it could never
CONFIRM or REFUTE them.  A run whose auditor could not reach GitHub produced
an audit saying "any executor claim about the PR is untrusted in its
entirety" -- and that run was then recorded with no distinction from one
that had been delivered and audited cleanly.

This module gives the auditor a read-only reach of its own:

*   :class:`ReadOnlyGitHubClient` issues HTTP ``GET`` only.  Read-only is a
    property of the code, not a convention: the write verbs have no code
    path, and the one dynamic escape hatch (:meth:`ReadOnlyGitHubClient.request`)
    refuses anything that is not a read.
*   :func:`assert_read_only_command` is the enforcement layer for the ``gh``
    CLI surface the auditor may still be handed.  It refuses write verbs
    (``gh pr merge``, ``gh pr comment``, ``gh pr close``, ...), the
    ``gh api`` write methods (``--method=POST`` / ``-X PATCH`` / ...) and the
    field-supplying ``gh api`` bodies (``-f``/``-F``/``--input``) that a
    bare ``Bash(gh *)`` deny would leave unmatched.  Every one of those
    spellings is refused INDIVIDUALLY and asserted individually, in
    ``tests/test_auditor_read_only_spelling_coverage.py``.
*   :func:`resolve_auditor_credential` resolves the auditor's OWN
    fine-grained read-only token from a dedicated environment variable and
    REFUSES to proceed when that value collides with the executor's
    ``GH_TOKEN`` / ``GITHUB_TOKEN`` or with a fleet admin token.  The value is
    never printed, logged, or committed.

Fail-closed by construction: any read failure -- network error, missing PR,
permission error, unexpected status -- yields an explicit ``unverifiable``
verification result.  It never becomes a silent pass and never becomes a
confirmed value.

Scope notes:

*   GitHub only.  No issue tracker surface is added here.
*   The auditor's contract header semantics are untouched: still exactly
    ``Status`` / ``Integrity`` / ``Contract audit``.  The ``confirmed`` /
    ``refuted`` / ``unverifiable`` values below are this module's internal
    verification result, not a contract-header vocabulary.
*   The close-out outcome vocabulary belongs to the sibling task
    ``yt:easybutt0n-17:harness-closeout-contract`` and is neither defined nor
    renamed here.
*   PR #147 (auditor isolation / independent clone / fail-closed) and PR #159
    (queue outcomes) are design references; neither is re-implemented here.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "AUDITOR_GITHUB_TOKEN_ENV",
    "AUDITOR_GITHUB_SCOPE_ENV",
    "AUDITOR_WRITE_SPELLINGS",
    "VERDICT_CONFIRMED",
    "VERDICT_REFUTED",
    "VERDICT_UNVERIFIABLE",
    "AuditorCredential",
    "AuditorCredentialError",
    "PullRequestFacts",
    "ReadOnlyGitHubClient",
    "ReadOnlyViolation",
    "VerificationResult",
    "assert_read_only_command",
    "command_string",
    "deny_rule_matches",
    "is_denied_by_auditor_policy",
    "resolve_auditor_credential",
    "verify_pull_request",
    "write_intent_reason",
]


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: The auditor's own credential variable.  Deliberately NOT ``GH_TOKEN``:
#: the executor's token is a fleet-wide write-capable credential and reusing
#: it would hand the auditor the executor's authority.
AUDITOR_GITHUB_TOKEN_ENV = "LH_HARNESS_AUDITOR_GITHUB_TOKEN"

#: Optional declaration of the fine-grained token's scopes.  When present it
#: must describe a read-only token; a write scope is refused.
AUDITOR_GITHUB_SCOPE_ENV = "LH_HARNESS_AUDITOR_GITHUB_TOKEN_SCOPE"

#: Environment variables holding credentials the auditor must NOT reuse.
SHARED_CREDENTIAL_ENV_VARS: tuple[str, ...] = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "LH_HARNESS_FLEET_GITHUB_TOKEN",
    "LH_HARNESS_FLEET_TOKEN",
)

#: The verification result of ONE read.  This is the module's internal
#: vocabulary; it is deliberately NOT the auditor's contract header, which
#: remains exactly Status / Integrity / Contract audit.
VERDICT_CONFIRMED = "confirmed"
VERDICT_REFUTED = "refuted"
VERDICT_UNVERIFIABLE = "unverifiable"

GITHUB_API_ROOT = "https://api.github.com"
_DEFAULT_TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ReadOnlyViolation(RuntimeError):
    """Raised when a write intent reaches the read-only surface.

    The message names the refused intent but never echoes a credential.
    """


class AuditorCredentialError(RuntimeError):
    """Raised when the auditor's credential source is missing or unsuitable.

    The message names the VARIABLES involved; it never contains a value.
    """


class GitHubReadError(RuntimeError):
    """A read that could not be completed.

    Carries a short machine-readable reason.  The caller turns this into an
    explicit ``unverifiable`` result -- never into a pass.
    """

    def __init__(self, reason: str, *, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditorCredential:
    """The resolved auditor credential.

    ``token`` is carried because the client needs it, but the dataclass is
    deliberately hostile to accidental disclosure: ``__repr__`` and
    ``__str__`` render the presence, never the value.
    """

    source: str
    present: bool
    token: str = field(default="", repr=False)

    def __repr__(self) -> str:
        return f"AuditorCredential(source={self.source!r}, present={self.present!r})"

    __str__ = __repr__


def _write_scopes_are_forbidden(scope_text: str) -> bool:
    """True when a declared scope list contains a write capability."""

    read_only_scopes = {
        "",
        "contents:read",
        "pull_requests:read",
        "metadata:read",
        "repo:read",
        "read",
    }
    for raw in scope_text.replace(";", ",").split(","):
        scope = raw.strip().lower()
        if not scope:
            continue
        if scope in read_only_scopes:
            continue
        # Anything that is not an explicitly read-only scope is treated as a
        # write capability: an unknown scope must not be assumed harmless.
        return True
    return False


def _refuse_shared_credential(token: str, source: Mapping[str, str]) -> None:
    """Raise when ``token`` collides with a shared / fleet credential.

    Only the VARIABLE names appear in the message; the value never does.
    """

    for shared_name in SHARED_CREDENTIAL_ENV_VARS:
        shared = (source.get(shared_name) or "").strip()
        if shared and shared == token:
            raise AuditorCredentialError(
                f"{AUDITOR_GITHUB_TOKEN_ENV} resolves to the same value as "
                f"{shared_name}. The auditor must use its own fine-grained "
                "read-only token (contents:read, pull_requests:read); refusing "
                "to reuse a shared or write-capable credential."
            )


def resolve_auditor_credential(
    env: Mapping[str, str] | None = None,
) -> AuditorCredential:
    """Resolve the auditor's OWN fine-grained read-only GitHub token.

    The auditor must not borrow the executor's authority.  The token is
    read from :data:`AUDITOR_GITHUB_TOKEN_ENV` only.  When the resolved value
    equals the executor's ``GH_TOKEN`` / ``GITHUB_TOKEN`` or a fleet admin
    token, this raises :class:`AuditorCredentialError` -- the auditor refuses
    to act on a shared credential rather than silently narrowing it.

    The token VALUE never appears in the exception message, in the
    credential's repr, or in any log this module writes.
    """

    source = env if env is not None else os.environ
    token = (source.get(AUDITOR_GITHUB_TOKEN_ENV) or "").strip()
    if not token:
        return AuditorCredential(source=AUDITOR_GITHUB_TOKEN_ENV, present=False, token="")

    declared_scope = (source.get(AUDITOR_GITHUB_SCOPE_ENV) or "").strip()
    if declared_scope and _write_scopes_are_forbidden(declared_scope):
        raise AuditorCredentialError(
            f"{AUDITOR_GITHUB_TOKEN_ENV} must be a read-only fine-grained token; "
            f"{AUDITOR_GITHUB_SCOPE_ENV} declares a non-read-only scope. "
            "Refusing to use a write-capable credential for auditor verification."
        )

    _refuse_shared_credential(token, source)

    return AuditorCredential(
        source=AUDITOR_GITHUB_TOKEN_ENV, present=True, token=token
    )


# ---------------------------------------------------------------------------
# Read-only command enforcement
# ---------------------------------------------------------------------------

#: ``gh`` subcommands that only ever read.
_GH_READ_SUBCOMMANDS = frozenset(
    {
        "api",
        "auth",
        "browse",
        "cache",
        "config",
        "issue",
        "label",
        "list",
        "pr",
        "project",
        "release",
        "repo",
        "run",
        "search",
        "secret",
        "status",
        "variable",
        "workflow",
    }
)

#: ``gh`` subcommands that always write.  Groups whose read and write leaves
#: share a name (``gh run view`` reads, ``gh run rerun`` writes) are NOT
#: listed here; they are handled by the leaf checks below.
_GH_WRITE_SUBCOMMANDS = frozenset(
    {
        "annotate",
        "api-write",
        "codespace",
        "gist",
        "ruleset",
        "ssh-key",
        "ssh-key-add",
    }
)

#: ``gh <group> <leaf>`` leaves that WRITE, whatever group they sit under.
#:
#: The groups below are read groups -- ``gh pr``, ``gh issue``, ``gh repo``,
#: ``gh release``, ``gh secret``, ``gh variable``, ``gh workflow`` -- but a
#: write leaf under a read group is still a write.  ``gh pr review
#: --approve`` and ``gh secret set`` are the two that matter most: both look
#: like inspection on the surface and both change the outside world.
_GH_WRITE_LEAF_SUBCOMMANDS = frozenset(
    {
        "approve",
        "close",
        "comment",
        "create",
        "delete",
        "deploy",
        "dismiss",
        "edit",
        "enable",
        "lock",
        "merge",
        "publish",
        "ready",
        "reopen",
        "rerun",
        "review",
        "set",
        "submit",
        "unlock",
        "update",
        "upload",
    }
)

#: ``gh <group> <leaf>`` pairs that write, keyed by group.  A group whose
#: read and write leaves share a name (``gh run view`` reads, ``gh workflow
#: run`` writes) needs the pair spelled out rather than a bare leaf.
_GH_WRITE_GROUP_LEAVES: dict[str, frozenset[str]] = {
    "workflow": frozenset({"dispatch", "enable", "run"}),
    "run": frozenset({"cancel", "delete", "rerun"}),
}

#: ``gh`` flags that themselves express a write intent, independent of the
#: subcommand they appear under.
_GH_WRITE_FLAGS = frozenset(
    {
        "--approve",
        "--comment",
        "--delete-branch",
        "--draft",
        "--ready",
        "--request-changes",
        "--squash",
        "--rebase",
        "--merge",
    }
)

#: HTTP verbs that are reads.
_READ_VERBS = frozenset({"GET", "HEAD", "OPTIONS"})

#: HTTP verbs that are writes.
_WRITE_VERBS = frozenset(
    {"POST", "PUT", "PATCH", "DELETE", "CONNECT", "TRACE", "LOCK", "PROPFIND"}
)

#: ``gh api`` flags that supply a request body / fields.
_GH_API_FIELD_FLAGS = frozenset({"-f", "-F", "--field", "--raw-field", "--input", "-i"})


def _gh_api_write_intent(argv: Sequence[str]) -> str | None:
    """Return why an ``gh api`` invocation writes, or ``None`` when it reads."""

    index = 0
    methods: list[str] = []
    while index < len(argv):
        token = argv[index]
        if token in ("--method", "-X") and index + 1 < len(argv):
            methods.append(argv[index + 1])
            index += 2
            continue
        for prefix in ("--method=", "-X"):
            if token.startswith(prefix) and len(token) > len(prefix):
                methods.append(token[len(prefix) :])
                break
        index += 1

    # Two different method flags: which one wins is a ``gh`` detail, not
    # something this surface should guess at.  A conflicting method is a
    # refusal.
    distinct = {item.strip().upper() for item in methods if item.strip()}
    if len(distinct) > 1:
        return "a conflicting HTTP method specification"

    for method in distinct:
        if method not in _READ_VERBS:
            return f"an HTTP {method} request"

    # ``gh api`` defaults to GET, but a body-bearing invocation is a write
    # even without an explicit method: ``gh api <path> -f body=...``.
    for token in argv:
        if token in _GH_API_FIELD_FLAGS:
            return "a field-supplying request body"
        if token.startswith("--input="):
            return "a field-supplying request body"
    return None


#: ``git`` subcommands that write to the repository or reach the network.
_GIT_WRITE_SUBCOMMANDS = frozenset(
    {
        "push",
        "commit",
        "merge",
        "rebase",
        "reset",
        "checkout",
        "switch",
        "add",
        "rm",
        "mv",
        "tag",
        "branch",
        "pull",
        "fetch",
        "clone",
        "remote",
        "submodule",
        "config",
        "apply",
        "stash",
        "worktree",
        "notes",
        "am",
        "cherry-pick",
        "revert",
        "clean",
    }
)


def write_intent_reason(argv: Sequence[str]) -> str | None:
    """Return WHY an invocation writes, or ``None`` when it only reads.

    This is the SHAPE classifier the whole read-only surface is built on.
    It decides by the shape of the invocation -- the program, the subcommand
    and its leaf, the HTTP method, and whether the invocation supplies a
    request body -- and never by matching one literal command string.  A
    renamed, parameterised or reordered write has the same shape and is
    therefore refused the same way.

    The returned string names the write intent.  It never contains a
    credential value.
    """

    args = [str(token) for token in argv if str(token)]
    if not args:
        return "an empty command carries no read intent"

    program = os.path.basename(args[0])
    rest = args[1:]

    if program == "git":
        return _git_write_intent(rest)

    if program != "gh":
        return f"command {program!r} is not on the read allowlist"

    if not rest:
        return "'gh' without a subcommand carries no read intent"

    sub = rest[0]
    if sub in _GH_WRITE_SUBCOMMANDS:
        return f"'gh {sub}' writes to the outside world"
    if sub not in _GH_READ_SUBCOMMANDS:
        return f"'gh {sub}' is not on the read allowlist"

    if sub == "api":
        return _gh_api_write_intent(rest[1:])

    # ``gh <group> <leaf>``: a write leaf under a read group is still a write.
    # ``gh pr view`` reads; ``gh pr review --approve`` and ``gh secret set``
    # do not, and both live under groups the allowlist admits.
    if len(rest) >= 2 and rest[1] in _GH_WRITE_LEAF_SUBCOMMANDS:
        return f"'gh {sub} {rest[1]}' writes to the outside world"

    group_leaves = _GH_WRITE_GROUP_LEAVES.get(sub)
    if group_leaves and len(rest) >= 2 and rest[1] in group_leaves:
        return f"'gh {sub} {rest[1]}' writes to the outside world"

    # A write flag is a write intent whatever it is attached to.
    for token in rest[1:]:
        if token in _GH_WRITE_FLAGS:
            return f"'gh {sub}' with {token} writes to the outside world"

    return None


def assert_read_only_command(argv: Sequence[str]) -> None:
    """Refuse a write intent on the ``gh`` / ``git`` command surface.

    ``None`` is returned for a read; a write raises :class:`ReadOnlyViolation`.
    The check is an ALLOWLIST over read verbs and read-only API paths, so an
    unrecognised subcommand is refused rather than assumed harmless.
    """

    reason = write_intent_reason(argv)
    if reason is not None:
        raise ReadOnlyViolation(f"read-only surface: {reason} is refused")


def _git_write_intent(args: Sequence[str]) -> str | None:
    """Return why a git invocation writes, or ``None`` when it only reads."""

    for token in args:
        head = token.split("=", 1)[0]
        if head in _GIT_WRITE_SUBCOMMANDS:
            return f"'git {head}' writes to the repository"
    return None


# ---------------------------------------------------------------------------
# The permission deny-rule layer
# ---------------------------------------------------------------------------
#
# The auditor's read-only reach is enforced in TWO layers, and both have to
# hold:
#
# 1.  the IN-PROCESS layer (:func:`assert_read_only_command`), which refuses
#     a write by its shape -- program, subcommand and leaf, HTTP method, and
#     the presence of a request body; and
# 2.  the PERMISSION layer, the ``Bash(...)`` rules the harness hands to
#     Claude Code as ``--disallowedTools``.  These stop the auditor from
#     shelling out to a write at all.
#
# Until now only layer 1 was proven.  The permission layer was checked by
# STRING MEMBERSHIP -- "the rule ``Bash(gh pr merge*)`` is in the deny list"
# -- which proves the rule EXISTS and nothing about whether it denies the
# command it is supposed to deny.  A rule can sit in the list and match
# nothing, and the shell-out is then refused by nothing at all.
#
# So the permission layer needs its own matcher, and every write spelling
# needs to be checked against BOTH layers.  :func:`deny_rule_matches` is
# that matcher; :func:`is_denied_by_auditor_policy` applies a whole deny
# list to one command string.

#: The prefix Claude Code uses for a Bash permission rule.
_DENY_TOOL_PREFIX = "Bash("


def deny_rule_matches(command: str, rule: str) -> bool:
    """Return True when a ``Bash(...)`` deny rule matches a command.

    The rule syntax implemented here is the one the harness's own deny
    rules are written in:

    *   ``Bash(cmd)``  -- the command exactly; and
    *   ``Bash(pat)``  -- a glob, where ``*`` matches any run of characters
        inside the command string.

    ``Bash(gh pr merge*)`` therefore matches ``gh pr merge``, ``gh pr merge
    181`` and ``gh pr merge --squash`` alike, and a rule with a glob in the
    middle (``Bash(gh api * -f *)``) matches a body-supplying API call
    wherever the body flag sits.

    The matcher is deliberately simple and total: an unknown rule shape
    matches nothing, so a malformed rule can never be read as "everything
    is denied" nor as "nothing is denied".
    """

    text = str(rule or "").strip()
    if not text.startswith(_DENY_TOOL_PREFIX) or not text.endswith(")"):
        return False

    pattern = text[len(_DENY_TOOL_PREFIX) : -1]
    if not pattern:
        return False

    target = str(command or "").strip()
    if not target:
        return False

    if "*" not in pattern:
        return target == pattern

    # Glob semantics: ``*`` matches any run of characters.  The command is
    # compared as a whole, so a rule cannot match a suffix of a longer
    # command.
    return _glob_matches(pattern, target)


def _glob_matches(pattern: str, target: str) -> bool:
    """Match ``target`` against a ``*``-glob, without importing fnmatch.

    ``fnmatch`` is case-normalising and treats ``?`` and ``[]`` specially;
    the deny rules use only ``*``, so a direct implementation keeps the
    matching predictable and case-SENSITIVE (a command is spelled exactly).
    """

    parts = pattern.split("*")
    if not parts:
        return target == pattern

    if not target.startswith(parts[0]):
        return False

    remainder = target[len(parts[0]) :]
    for part in parts[1:-1]:
        index = remainder.find(part)
        if index < 0:
            return False
        remainder = remainder[index + len(part) :]

    last = parts[-1]
    if not last:
        return True
    return remainder.endswith(last)


def is_denied_by_auditor_policy(
    command: str,
    rules: Sequence[str],
) -> str | None:
    """Return the deny rule that refuses ``command``, or ``None``.

    This is the permission layer's answer to "is this shell-out refused?".
    It is deliberately independent of :func:`write_intent_reason`: the two
    layers are separate defences and each is proven on its own, so a hole in
    one is visible instead of being papered over by the other.
    """

    for rule in rules:
        if deny_rule_matches(command, rule):
            return rule
    return None


def command_string(argv: Sequence[str]) -> str:
    """Render an argv list as the command string a deny rule would see."""

    return " ".join(str(token) for token in argv if str(token))


# ---------------------------------------------------------------------------
# The canonical write spellings
# ---------------------------------------------------------------------------
#
# Every write spelling the auditor must never be able to execute, in one
# place.  This list is the source of truth for two separate obligations:
#
# *   the in-process layer must refuse each one (the shape classifier); and
# *   the permission layer must deny each one (a matching deny rule).
#
# It is a list of ARGV LISTS rather than command strings so that the same
# entry can be handed to the classifier and to the rule matcher without
# re-parsing, and so that a spelling with a space in an argument cannot be
# silently re-spelled into a different command.

#: ``gh``/``git`` write spellings, as argv lists.
AUDITOR_WRITE_SPELLINGS: tuple[tuple[str, ...], ...] = (
    # -- gh api: an explicit write method ---------------------------------
    ("gh", "api", "--method=POST", "repos/owner/repo/pulls/181/comments"),
    ("gh", "api", "-X", "POST", "repos/owner/repo/pulls/181/comments"),
    ("gh", "api", "-XPOST", "repos/owner/repo/pulls/181/comments"),
    ("gh", "api", "--method=post", "repos/owner/repo/pulls/181/reviews"),
    ("gh", "api", "--method", "PATCH", "repos/owner/repo/pulls/181"),
    ("gh", "api", "--method=PUT", "repos/owner/repo/pulls/181"),
    ("gh", "api", "--method=DELETE", "repos/owner/repo/pulls/181"),
    ("gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/181"),
    ("gh", "api", "-X", "PUT", "repos/owner/repo/pulls/181"),
    ("gh", "api", "-X", "DELETE", "repos/owner/repo/pulls/181"),
    ("gh", "api", "--method=POST", "-X", "GET", "repos/owner/repo/pulls/181"),
    # -- gh api: a field-supplying body, with no explicit method ---------
    ("gh", "api", "repos/owner/repo/issues/1/comments", "-f", "body=lgtm"),
    ("gh", "api", "repos/owner/repo/issues/1/comments", "-F", "body=lgtm"),
    ("gh", "api", "repos/owner/repo/issues/1/comments", "--field", "body=lgtm"),
    ("gh", "api", "repos/owner/repo/issues/1/comments", "--raw-field", "body=lgtm"),
    ("gh", "api", "repos/owner/repo/issues/1/comments", "--input", "payload.json"),
    ("gh", "api", "repos/owner/repo/issues/1/comments", "--input=payload.json"),
    ("gh", "api", "repos/owner/repo/issues/1/comments", "-i", "payload.json"),
    ("gh", "api", "repos/owner/repo/pulls/181/comments", "--method=POST", "-f", "body=lgtm"),
    ("gh", "api", "graphql", "-f", "query=mutation{addComment}"),
    # -- gh pr ------------------------------------------------------------
    ("gh", "pr", "merge", "181"),
    ("gh", "pr", "merge", "181", "--squash", "--delete-branch"),
    ("gh", "pr", "close", "181"),
    ("gh", "pr", "reopen", "181"),
    ("gh", "pr", "ready", "181"),
    ("gh", "pr", "edit", "181", "--title", "x"),
    ("gh", "pr", "comment", "181", "--body", "lgtm"),
    ("gh", "pr", "create", "--title", "x"),
    ("gh", "pr", "review", "181", "--approve"),
    ("gh", "pr", "review", "181", "--request-changes"),
    ("gh", "pr", "review", "181", "--comment", "lgtm"),
    # -- gh issue ---------------------------------------------------------
    ("gh", "issue", "comment", "1", "--body", "lgtm"),
    ("gh", "issue", "create", "--title", "x"),
    ("gh", "issue", "close", "1"),
    ("gh", "issue", "reopen", "1"),
    ("gh", "issue", "delete", "1"),
    ("gh", "issue", "edit", "1", "--title", "x"),
    # -- gh release / secret / variable / workflow / run ------------------
    ("gh", "release", "create", "v1"),
    ("gh", "release", "edit", "v1"),
    ("gh", "release", "upload", "v1", "asset.tar.gz"),
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
    # -- gh repo / gist / label / codespace --------------------------------
    ("gh", "repo", "edit", "owner/repo"),
    ("gh", "repo", "create", "x"),
    ("gh", "repo", "delete", "owner/repo"),
    ("gh", "gist", "create", "notes.txt"),
    ("gh", "label", "create", "x"),
    ("gh", "label", "edit", "x"),
    ("gh", "label", "delete", "x"),
    # -- git --------------------------------------------------------------
    ("git", "push", "origin", "main"),
    ("git", "push"),
    ("git", "push", "--force", "origin", "main"),
    ("git", "push", "--force-with-lease", "origin", "main"),
    ("git", "-C", "/tmp/checkout", "push", "origin", "main"),
    ("git", "--git-dir=/tmp/checkout/.git", "push", "origin", "main"),
    # The network git operations the auditor may not run: they rewrite
    # .git/ and break the integrity guard.
    ("git", "fetch", "origin"),
    ("git", "pull", "origin", "main"),
    ("git", "remote", "add", "upstream", "https://example.invalid/repo.git"),
    ("git", "clone", "https://example.invalid/repo.git"),
    ("git", "submodule", "update", "--init"),
    ("git", "lfs", "pull"),
    ("git", "-C", "/tmp/checkout", "fetch", "origin"),
    ("git", "-C", "/tmp/checkout", "pull", "origin", "main"),
    ("git", "-C", "/tmp/checkout", "remote", "add", "u", "https://example.invalid/repo.git"),
    ("git", "-C", "/tmp/checkout", "clone", "https://example.invalid/repo.git"),
    ("git", "-C", "/tmp/checkout", "submodule", "update", "--init"),
    ("git", "-C", "/tmp/checkout", "lfs", "pull"),
    ("git", "--git-dir=/tmp/checkout/.git", "fetch", "origin"),
    ("git", "--git-dir=/tmp/checkout/.git", "pull", "origin", "main"),
    ("git", "--git-dir=/tmp/checkout/.git", "remote", "add", "u", "https://example.invalid/repo.git"),
    ("git", "--git-dir=/tmp/checkout/.git", "clone", "https://example.invalid/repo.git"),
    ("git", "--git-dir=/tmp/checkout/.git", "submodule", "update", "--init"),
    ("git", "--git-dir=/tmp/checkout/.git", "lfs", "pull"),
    # -- the write-only groups the auditor may not shell out to at all ----
    ("gh", "api-write", "repos/owner/repo/issues/1/comments"),
    ("gh", "codespace", "code"),
    ("gh", "ruleset", "create"),
    ("gh", "ssh-key", "add", "k"),
    # -- write flags on an otherwise-read verb ----------------------------
    ("gh", "pr", "view", "181", "--merge"),
    ("gh", "pr", "view", "181", "--rebase"),
    ("gh", "pr", "view", "181", "--ready"),
    ("gh", "pr", "view", "181", "--draft"),
)


# ---------------------------------------------------------------------------
# Facts and verification result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PullRequestFacts:
    """What an independent read observed about a pull request."""

    repo: str
    number: int
    state: str
    base_ref: str
    head_sha: str
    head_ref: str = ""
    merged: bool = False
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    diff_stat: str = ""

    @property
    def is_open(self) -> bool:
        return self.state.upper() == "OPEN" and not self.merged

    @property
    def summary(self) -> str:
        """A one-line, credential-free summary of the observation."""

        return (
            f"{self.repo}#{self.number} {self.state} base={self.base_ref} "
            f"head={self.head_sha[:12]} "
            f"(+{self.additions}/-{self.deletions}, {self.changed_files} files)"
        )


@dataclass(frozen=True)
class VerificationResult:
    """The outcome of one independent verification attempt.

    ``blocks_delivery`` is True for every result that is not a confirmation.
    An ``unverifiable`` result is explicitly NOT a pass: the auditor could
    not reach the evidence, so it cannot claim the executor's assertion
    holds.
    """

    verdict: str
    reason: str
    reviewed_sha: str = ""
    observed_sha: str | None = None
    facts: PullRequestFacts | None = None

    @property
    def blocks_delivery(self) -> bool:
        return self.verdict != VERDICT_CONFIRMED

    @property
    def is_unverifiable(self) -> bool:
        return self.verdict == VERDICT_UNVERIFIABLE

    def summary(self) -> str:
        parts = [f"verdict={self.verdict}", f"reason={self.reason}"]
        if self.observed_sha:
            parts.append(f"observed_sha={self.observed_sha}")
        if self.reviewed_sha:
            parts.append(f"reviewed_sha={self.reviewed_sha}")
        return "; ".join(parts)


# ---------------------------------------------------------------------------
# The read-only client
# ---------------------------------------------------------------------------


class ReadOnlyGitHubClient:
    """A GitHub client that can only read.

    Every request this class issues is a ``GET``.  The write verbs have no
    code path here at all; :meth:`request` -- the one place a verb is
    supplied by the caller -- refuses anything that is not a read.
    """

    def __init__(
        self,
        *,
        token: str = "",
        timeout: float = _DEFAULT_TIMEOUT,
        opener: Callable[[Any], Any] | None = None,
        api_root: str = GITHUB_API_ROOT,
        user_agent: str = "LongHorizon-Harness-auditor",
        env: Mapping[str, str] | None = None,
    ) -> None:
        if token:
            # An explicitly supplied token is still checked against the
            # shared-credential rules: a caller must not hand the auditor
            # the executor's ``GH_TOKEN`` by another route.
            _refuse_shared_credential(token, env if env is not None else os.environ)
        else:
            credential = resolve_auditor_credential(env)
            token = credential.token
        self._token = token
        self._timeout = timeout
        self._api_root = api_root.rstrip("/")
        self._user_agent = user_agent
        self._opener = opener or urllib.request.urlopen

    # -- credential ------------------------------------------------------

    @classmethod
    def from_environment(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        opener: Callable[[Any], Any] | None = None,
    ) -> "ReadOnlyGitHubClient":
        """Build a client from the auditor's own credential source.

        Raises :class:`AuditorCredentialError` when the credential is absent
        or is a shared / write-capable token.
        """

        credential = resolve_auditor_credential(env)
        if not credential.present:
            raise AuditorCredentialError(
                f"{AUDITOR_GITHUB_TOKEN_ENV} is not set. The auditor needs its own "
                "fine-grained read-only token to verify anything; without it the "
                "verdict is unverifiable, not confirmed."
            )
        return cls(token=credential.token, timeout=timeout, opener=opener)

    # -- the single request surface --------------------------------------

    def request(self, method: str, path: str) -> Any:
        """Issue a read request.  Any write verb is refused."""

        verb = str(method or "").strip().upper()
        if verb not in _READ_VERBS:
            raise ReadOnlyViolation(
                f"read-only surface: HTTP {verb or '(empty)'} is not a read verb"
            )
        if not str(path).startswith("/"):
            raise ReadOnlyViolation(
                "read-only surface: an API path must be repository-relative"
            )

        url = f"{self._api_root}{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": self._user_agent,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        request = urllib.request.Request(url, method=verb, headers=headers)
        try:
            with self._opener(request, timeout=self._timeout) as response:
                status = int(getattr(response, "status", 200) or 200)
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise GitHubReadError(
                f"github read failed with HTTP {exc.code}", status=int(exc.code)
            ) from exc
        except urllib.error.URLError as exc:
            raise GitHubReadError(f"github read failed: {exc.reason}") from exc
        except OSError as exc:
            raise GitHubReadError(f"github read failed: {exc}") from exc

        if status >= 400:
            raise GitHubReadError(
                f"github read failed with HTTP {status}", status=status
            )
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubReadError("github read returned an unreadable body") from exc

    # -- the reads the auditor needs -------------------------------------

    def read_pull_request(self, repo: str, number: int) -> PullRequestFacts:
        """Read a pull request's independent facts.

        Raises :class:`GitHubReadError` when the PR cannot be read -- a
        missing PR, a permission error, a network failure, an unexpected
        status.  The caller turns that into an explicit ``unverifiable``.
        """

        if not repo or "/" not in repo:
            raise GitHubReadError(f"invalid repository slug: {repo!r}")
        try:
            pr_number = int(number)
        except (TypeError, ValueError) as exc:
            raise GitHubReadError(f"invalid pull request number: {number!r}") from exc
        if pr_number <= 0:
            raise GitHubReadError(f"invalid pull request number: {number!r}")

        payload = self.request("GET", f"/repos/{repo}/pulls/{pr_number}")
        if not isinstance(payload, dict):
            raise GitHubReadError("github read returned an unexpected body shape")

        base = payload.get("base") or {}
        head = payload.get("head") or {}
        base_repo = base.get("repo") or {}
        number_field = payload.get("number", pr_number)
        try:
            observed_number = int(number_field)
        except (TypeError, ValueError):
            observed_number = pr_number

        return PullRequestFacts(
            repo=str(base_repo.get("full_name") or repo),
            number=observed_number,
            state=str(payload.get("state") or ""),
            base_ref=str(base.get("ref") or ""),
            head_sha=str(head.get("sha") or ""),
            head_ref=str(head.get("ref") or ""),
            merged=bool(payload.get("merged")),
            additions=int(payload.get("additions") or 0),
            deletions=int(payload.get("deletions") or 0),
            changed_files=int(payload.get("changed_files") or 0),
            diff_stat=_diff_stat(payload),
        )

    def read_pull_request_files(self, repo: str, number: int) -> list[dict[str, Any]]:
        """Read the per-file diff stat of a pull request."""

        payload = self.request("GET", f"/repos/{repo}/pulls/{number}/files")
        if not isinstance(payload, list):
            raise GitHubReadError("github read returned an unexpected body shape")
        return [item for item in payload if isinstance(item, dict)]

    # -- write surface: absent by construction ---------------------------

    def write_pull_request(self, repo: str, number: int, fields: Mapping[str, Any]) -> None:
        """Always refused.  Present so a caller reaches a refusal, not a 404."""

        raise ReadOnlyViolation(
            "read-only surface: the auditor may not write to a pull request"
        )

    def __getattr__(self, name: str) -> Any:
        """Refuse an unknown attribute that looks like a write helper.

        Read helpers are explicit methods on this class; anything else that
        starts with ``write_``, ``create_``, ``delete_``, ``update_``,
        ``merge_``, ``comment_`` or ``push_`` is a write intent and is
        refused rather than raising an opaque ``AttributeError``.
        """

        if name.startswith(
            ("write_", "create_", "delete_", "update_", "merge_", "comment_", "push_", "post_")
        ):
            raise ReadOnlyViolation(
                f"read-only surface: {name!r} is a write intent and is refused"
            )
        raise AttributeError(name)


def _diff_stat(payload: Mapping[str, Any]) -> str:
    """Render the diff stat of a pull-request payload as one line."""

    additions = int(payload.get("additions") or 0)
    deletions = int(payload.get("deletions") or 0)
    changed = int(payload.get("changed_files") or 0)
    return f"{changed} files changed, {additions} insertions(+), {deletions} deletions(-)"


# ---------------------------------------------------------------------------
# The verification step
# ---------------------------------------------------------------------------


def verify_pull_request(
    client: ReadOnlyGitHubClient,
    *,
    repo: str,
    number: int,
    reviewed_sha: str,
) -> VerificationResult:
    """Independently confirm or refute an executor's claim about a PR.

    The claim under test is: "PR ``number`` of ``repo`` exists, is open, and
    its head sha is ``reviewed_sha``" -- the sha the auditor reviewed.

    *   ``confirmed``   -- the read observed exactly that.
    *   ``refuted``     -- the read observed something else (wrong sha, not
        open, merged).  The claim is false.
    *   ``unverifiable`` -- the evidence could not be reached at all.  The
        claim is NOT confirmed: an auditor that could not read the evidence
        has no basis to accept it.

    Every result except ``confirmed`` blocks a delivered claim.
    """

    wanted = str(reviewed_sha or "").strip()
    try:
        facts = client.read_pull_request(repo, number)
    except GitHubReadError as exc:
        return VerificationResult(
            verdict=VERDICT_UNVERIFIABLE,
            reason=f"the artifact could not be read independently: {exc.reason}",
            reviewed_sha=wanted,
            observed_sha=None,
        )
    except ReadOnlyViolation:
        raise
    except Exception as exc:  # defensive: an unexpected error is still a failure to verify
        return VerificationResult(
            verdict=VERDICT_UNVERIFIABLE,
            reason=f"the artifact could not be read independently: {exc}",
            reviewed_sha=wanted,
            observed_sha=None,
        )

    if facts.number != int(number):
        return VerificationResult(
            verdict=VERDICT_REFUTED,
            reason=(
                f"the read observed PR #{facts.number} where #{number} was claimed"
            ),
            reviewed_sha=wanted,
            observed_sha=facts.head_sha,
            facts=facts,
        )

    if not wanted:
        return VerificationResult(
            verdict=VERDICT_UNVERIFIABLE,
            reason="no reviewed sha was supplied, so the head sha cannot be checked",
            reviewed_sha="",
            observed_sha=facts.head_sha,
            facts=facts,
        )

    if facts.head_sha != wanted:
        return VerificationResult(
            verdict=VERDICT_REFUTED,
            reason=(
                f"the head sha moved: the reviewed sha {wanted[:12]} is not the "
                f"observed head sha {facts.head_sha[:12]}"
            ),
            reviewed_sha=wanted,
            observed_sha=facts.head_sha,
            facts=facts,
        )

    if facts.merged or not facts.is_open:
        return VerificationResult(
            verdict=VERDICT_REFUTED,
            reason=f"the pull request is not open (state={facts.state or 'unknown'})",
            reviewed_sha=wanted,
            observed_sha=facts.head_sha,
            facts=facts,
        )

    return VerificationResult(
        verdict=VERDICT_CONFIRMED,
        reason=(
            f"the read confirmed {facts.summary}; the head sha matches the "
            "reviewed sha"
        ),
        reviewed_sha=wanted,
        observed_sha=facts.head_sha,
        facts=facts,
    )
