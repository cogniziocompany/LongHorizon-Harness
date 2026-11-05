from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


ClaudeRole = Literal[
    "manager",
    "gui_executor",
    "cli_executor",
    "gui_auditor",
    "cli_auditor",
    "auditor_format_repair",
    "final_response",
]

_WRITE_TOOLS = ("Write", "Edit", "NotebookEdit")
_AUDITOR_ROLES = {"gui_auditor", "cli_auditor", "auditor_format_repair"}


# Deny rules for auditor network git operations. These prevent an auditor from
# running commands that rewrite .git/objects or fetch refs over the network,
# which would cause the workspace guard to flag an integrity violation.
AUDITOR_NETWORK_GIT_DENY: tuple[str, ...] = (
    "Bash(git fetch*)",
    "Bash(git pull*)",
    "Bash(git push*)",
    "Bash(git remote*)",
    "Bash(git clone*)",
    "Bash(git submodule*)",
    "Bash(git lfs*)",
    # Variants that point git at a different directory via -C or --git-dir.
    "Bash(git -C * fetch*)",
    "Bash(git -C * pull*)",
    "Bash(git -C * push*)",
    "Bash(git -C * remote*)",
    "Bash(git -C * clone*)",
    "Bash(git -C * submodule*)",
    "Bash(git -C * lfs*)",
    "Bash(git --git-dir* fetch*)",
    "Bash(git --git-dir* pull*)",
    "Bash(git --git-dir* push*)",
    "Bash(git --git-dir* remote*)",
    "Bash(git --git-dir* clone*)",
    "Bash(git --git-dir* submodule*)",
    "Bash(git --git-dir* lfs*)",
)

# Deny rules for the auditor's `gh` WRITE spellings.
#
# The auditor used to be denied `Bash(gh *)` wholesale.  That was the safe
# choice at the time, but it was a blunt one: it took out the local `gh`
# CLI's READ verbs along with the write ones, so an auditor that wanted to
# look at a pull request had to shell out to something else.  The auditor's
# independent reach is now the harness's own read-only verifier
# (``scripts/auditor_verify_pr.py``), and this deny list is spelled per
# WRITE so that a read is not blocked by the same rule that blocks a write.
#
# The spellings below are the write paths that a `gh` read allowlist would
# otherwise let through.  They are the same spellings the in-process
# enforcement layer refuses (``lh_harness.auditor_verify.assert_read_only_command``),
# which is the belt to this deny list's braces: the deny list stops the
# auditor from *shelling out* to a write, and the enforcement layer refuses a
# write that arrives by any other route (including a direct call into the
# harness's GitHub client).
AUDITOR_GH_WRITE_DENY: tuple[str, ...] = (
    # A write verb on the raw API.
    "Bash(gh api --method=*)",
    "Bash(gh api -X *)",
    "Bash(gh api -X*)",
    "Bash(gh api --method *)",
    # A field-supplying API body: a write even without an explicit method.
    "Bash(gh api * -f *)",
    "Bash(gh api * -F *)",
    "Bash(gh api * --field *)",
    "Bash(gh api * --raw-field *)",
    "Bash(gh api * --input *)",
    "Bash(gh api * --input=*)",
    "Bash(gh api * -i *)",
    # The same body flags with the flag BEFORE the endpoint.  `gh api`
    # parses its flags with Go's flag package, so the endpoint is not
    # position-locked: `gh api -f body=x repos/o/r/issues/1/comments` is
    # the same write as `gh api repos/o/r/issues/1/comments -f body=x`.
    # The rules above are anchored on the path coming first, so without
    # these the flag-first spelling matched NO rule and the permission
    # layer let a write through.
    "Bash(gh api -f *)",
    "Bash(gh api -F *)",
    "Bash(gh api --field *)",
    "Bash(gh api --raw-field *)",
    "Bash(gh api --input *)",
    "Bash(gh api --input=*)",
    "Bash(gh api -i *)",
    # Pull-request and issue writes.
    "Bash(gh pr merge*)",
    "Bash(gh pr close*)",
    "Bash(gh pr reopen*)",
    "Bash(gh pr ready*)",
    "Bash(gh pr edit*)",
    "Bash(gh pr comment*)",
    "Bash(gh pr create*)",
    "Bash(gh pr review*)",
    "Bash(gh issue comment*)",
    "Bash(gh issue create*)",
    "Bash(gh issue close*)",
    "Bash(gh issue reopen*)",
    "Bash(gh issue delete*)",
    "Bash(gh issue edit*)",
    "Bash(gh release create*)",
    "Bash(gh release edit*)",
    "Bash(gh release upload*)",
    "Bash(gh release delete*)",
    "Bash(gh secret set*)",
    "Bash(gh secret delete*)",
    "Bash(gh variable set*)",
    "Bash(gh variable delete*)",
    "Bash(gh workflow run*)",
    "Bash(gh workflow dispatch*)",
    "Bash(gh workflow enable*)",
    "Bash(gh run rerun*)",
    "Bash(gh run cancel*)",
    "Bash(gh run delete*)",
    "Bash(gh repo edit*)",
    "Bash(gh repo create*)",
    "Bash(gh repo delete*)",
    # Conversation locking is a write on the pull request, like a comment.
    "Bash(gh pr lock*)",
    "Bash(gh pr unlock*)",
    "Bash(gh gist create*)",
    "Bash(gh gist delete*)",
    "Bash(gh gist edit*)",
    "Bash(gh gist rename*)",
    # The gist group is a WRITE group for the in-process classifier, so the
    # permission layer denies the whole of it -- including the verbs that
    # read (`gh gist view`, `gh gist list`).  The two layers must agree
    # about what a write is, or one of them is a hole.
    "Bash(gh gist view*)",
    "Bash(gh gist list*)",
    "Bash(gh gist clone*)",
    "Bash(gh label create*)",
    "Bash(gh label edit*)",
    "Bash(gh label delete*)",
    # Conversation locking is a write on the issue, like on the PR.
    "Bash(gh issue lock*)",
    "Bash(gh issue unlock*)",
    # Project writes, and the local cache / config writes.
    "Bash(gh project create*)",
    "Bash(gh project edit*)",
    "Bash(gh project close*)",
    "Bash(gh project delete*)",
    "Bash(gh cache delete*)",
    "Bash(gh config set*)",
    # The credential groups are denied as GROUPS, exactly like the write-only
    # groups below: a leaf-by-leaf list goes stale, and every leaf of
    # ``gh auth`` / ``gh config`` / ``gh secret`` either PRINTS a credential
    # or changes one.  ``gh auth token``, ``gh auth status --show-token`` and
    # ``gh config get oauth_token`` would put a write-capable credential in
    # the auditor's context in the clear.
    #
    # A plain ``gh auth status`` -- which reports the account in use and not
    # the token it would use -- is the ONE leaf that stays admissible, so the
    # ``gh auth`` rules below are spelled leaf by leaf and the two
    # token-revealing flags are matched in ANY position.
    "Bash(gh auth token*)",
    "Bash(gh auth login*)",
    "Bash(gh auth logout*)",
    "Bash(gh auth refresh*)",
    "Bash(gh auth setup-git*)",
    "Bash(gh auth switch*)",
    "Bash(gh auth status -t*)",
    "Bash(gh auth * --show-token*)",
    "Bash(gh config get*)",
    "Bash(gh config list*)",
    "Bash(gh secret list*)",
    "Bash(gh api-write*)",
    "Bash(gh codespace*)",
    "Bash(gh ruleset*)",
    "Bash(gh ssh-key*)",
    "Bash(gh gpg-key*)",
    # These groups are write-ONLY: every verb they offer changes state on
    # GitHub, so they are denied as a GROUP.  A verb-by-verb list is a list
    # that goes stale -- `gh gist` was denied only for `create`, so `gh gist
    # delete` and `gh gist edit` matched no rule at all.  Groups that also
    # offer READ verbs (`gh label list`, `gh gist view`) are spelled per
    # verb above, so a legitimate read is not taken away with the write.
    "Bash(gh alias*)",
    "Bash(gh extension*)",
    "Bash(gh org*)",
    "Bash(gh team*)",
    "Bash(gh sso*)",
    # A write flag on an otherwise-read verb.
    "Bash(gh pr * --approve*)",
    "Bash(gh pr * --request-changes*)",
    "Bash(gh pr * --comment*)",
    "Bash(gh pr * --delete-branch*)",
    "Bash(gh pr * --merge*)",
    "Bash(gh pr * --squash*)",
    "Bash(gh pr * --rebase*)",
    "Bash(gh pr * --ready*)",
    "Bash(gh pr * --draft*)",
)


def uncovered_auditor_write_spellings() -> tuple[str, ...]:
    """Return the write spellings NO deny rule refuses.

    The permission layer is one of two defences, and it is the one that
    stops the auditor from shelling out to a write at all.  A rule that
    sits in the deny list but matches nothing is not a defence, so the
    coverage of the deny list is computed rather than assumed.

    The spellings come from
    :data:`lh_harness.auditor_verify.AUDITOR_WRITE_SPELLINGS` -- the same
    list the per-spelling tests walk -- so a spelling that gains a
    classifier but no rule shows up here as uncovered.
    """

    from lh_harness.auditor_verify import (  # local import: avoids a cycle
        AUDITOR_WRITE_SPELLINGS,
        command_string,
        is_denied_by_auditor_policy,
    )

    rules = (*AUDITOR_GH_WRITE_DENY, *AUDITOR_NETWORK_GIT_DENY)
    uncovered = []
    for spelling in AUDITOR_WRITE_SPELLINGS:
        command = command_string(spelling)
        if is_denied_by_auditor_policy(command, rules) is None:
            uncovered.append(command)
    return tuple(uncovered)

# Environment variables that make any network git operation fail fast for the
# auditor role. GIT_CONFIG_COUNT overrides are injected via the env and cause git
# to reject credential helpers and redirect helpers, preventing any network op.
AUDITOR_NETWORK_GIT_ENV: dict[str, str | None] = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "/bin/false",
    "SSH_AUTH_SOCK": None,
    "GIT_CONFIG_COUNT": "4",
    "GIT_CONFIG_KEY_0": "credential.helper",
    "GIT_CONFIG_VALUE_0": "",
    "GIT_CONFIG_KEY_1": "credential.helper",
    "GIT_CONFIG_VALUE_1": "",
    "GIT_CONFIG_KEY_2": "url.https://.insteadOf",
    "GIT_CONFIG_VALUE_2": "",
    "GIT_CONFIG_KEY_3": "url.ssh://.insteadOf",
    "GIT_CONFIG_VALUE_3": "",
}


@dataclass(frozen=True)
class ClaudeRolePolicy:
    role: ClaudeRole
    permission_mode: str
    disallowed_tools: tuple[str, ...]
    load_computer_mcp: bool = False
    workspace_read_only: bool = False
    env_overrides: dict[str, str | None] = field(default_factory=dict)


def policy_for_role(role: str) -> ClaudeRolePolicy:
    """Return the role deny-list used with Claude's unrestricted mode.

    Claude's interactive approval system and native sandbox are deliberately
    bypassed.  The remaining deny-list expresses harness role separation, not
    a filesystem or process sandbox.
    """
    if role in {"manager", "final_response"}:
        # The reply role only rewrites evidence it is given, so it needs no tools
        # at all; it shares the manager's no-side-effect deny list.
        return ClaudeRolePolicy(
            role=role,
            permission_mode="bypassPermissions",
            disallowed_tools=(
                "Bash",
                *_WRITE_TOOLS,
                "Agent",
                "mcp__*",
            ),
            workspace_read_only=True,
        )
    if role in {"gui_executor", "cli_executor"}:
        return ClaudeRolePolicy(
            role=role,
            permission_mode="bypassPermissions",
            disallowed_tools=("Agent",),
            load_computer_mcp=True,
        )
    if role in _AUDITOR_ROLES:
        return ClaudeRolePolicy(
            role=role,
            permission_mode="bypassPermissions",
            disallowed_tools=(
                *_WRITE_TOOLS,
                "Agent",
                *AUDITOR_NETWORK_GIT_DENY,
                *AUDITOR_GH_WRITE_DENY,
            ),
            load_computer_mcp=True,
            workspace_read_only=True,
            env_overrides=AUDITOR_NETWORK_GIT_ENV,
        )
    raise ValueError(f"Unknown Claude Code role: {role}")


def is_auditor_role(role: str) -> bool:
    return role in _AUDITOR_ROLES


def path_deny_rules(paths: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Build `Read`/`Edit` deny rules that hide harness-owned paths from Claude.

    Deny rules still apply under `--dangerously-skip-permissions`, and a `Read`
    deny also blocks the Edit tool. `//` anchors the pattern at the filesystem
    root; anything else would resolve against the settings source.
    """
    rules: list[str] = []
    for raw in paths:
        resolved = Path(raw).expanduser().resolve().as_posix().lstrip("/")
        if not resolved:
            continue
        for tool in ("Read", "Edit"):
            for pattern in (f"//{resolved}", f"//{resolved}/**"):
                rule = f"{tool}({pattern})"
                if rule not in rules:
                    rules.append(rule)
    return tuple(rules)


@dataclass(frozen=True)
class WorkspaceSnapshot:
    records: dict[str, tuple[object, ...]]
    errors: tuple[str, ...] = ()


# Git metadata that read-only commands rewrite despite GIT_OPTIONAL_LOCKS=0
# (e.g. `git diff` refreshing the stat cache on filesystems with unstable
# stat info, like WSL DrvFS mounts). Tracking these makes every honest
# audit self-invalidate. History-bearing paths (.git/HEAD, refs, objects)
# stay fully tracked.
_GIT_METADATA_NOISE = frozenset({".git", ".git/index", ".git/FETCH_HEAD"})


def is_git_internal_metadata_path(relative: str) -> bool:
    """Git-internal bookkeeping that read-only git commands refresh.

    Covers the stat-cache/admin files under ``.git/`` — the index,
    ``*.lock`` side files, and the whole ``.git/worktrees/<name>/`` admin
    subtree of linked worktrees, whose ``index``/``HEAD``/``ORIG_HEAD``/
    ``logs`` entries live in the MAIN repo's git dir but churn when any
    read-only git command runs inside the linked (audited) workspace.
    Churn here is git bookkeeping, never an auditor working-file write.
    History-bearing paths (``.git/objects``, refs, top-level ``.git/HEAD``)
    are NOT covered, so a forbidden network git operation still trips the
    guard.
    """
    if relative in _GIT_METADATA_NOISE:
        return True
    if relative.startswith(".git/") and relative.endswith(".lock"):
        return True
    return relative == ".git/worktrees" or relative.startswith(".git/worktrees/")


def snapshot_workspace(
    workspace_path: str,
    hidden_paths: tuple[str, ...] | list[str] = (),
) -> WorkspaceSnapshot:
    """Take a bounded-content workspace manifest for auditor mutation checks.

    ``hidden_paths`` skips harness-owned trees (logs, prompts, harness state)
    that keep changing while the auditor runs.
    """
    root = Path(workspace_path).expanduser().resolve()
    excluded = {Path(item).expanduser().resolve() for item in hidden_paths}
    records: dict[str, tuple[object, ...]] = {}
    errors: list[str] = []
    if not root.exists():
        return WorkspaceSnapshot(records={}, errors=(f"workspace does not exist: {root}",))
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            errors.append(f"{directory}: {type(exc).__name__}: {exc}")
            continue
        for entry in entries:
            path = Path(entry.path)
            if path in excluded:
                continue
            try:
                relative = path.relative_to(root).as_posix()
                if is_git_internal_metadata_path(relative):
                    # Keep descending into `.git` itself (objects/refs stay
                    # tracked) and into `*.lock` dirs as before, but skip the
                    # `.git/worktrees/` admin subtree entirely.
                    if entry.is_dir(follow_symlinks=False) and not (
                        relative == ".git/worktrees"
                        or relative.startswith(".git/worktrees/")
                    ):
                        stack.append(path)
                    continue
                stat = entry.stat(follow_symlinks=False)
                if entry.is_symlink():
                    records[relative] = (
                        "symlink",
                        stat.st_mode,
                        stat.st_mtime_ns,
                        os.readlink(path),
                    )
                elif entry.is_dir(follow_symlinks=False):
                    records[relative] = ("dir", stat.st_mode, stat.st_mtime_ns)
                    stack.append(path)
                elif entry.is_file(follow_symlinks=False):
                    digest = _small_file_digest(path, stat.st_size)
                    records[relative] = (
                        "file",
                        stat.st_mode,
                        stat.st_size,
                        stat.st_mtime_ns,
                        digest,
                    )
                else:
                    records[relative] = ("other", stat.st_mode, stat.st_size, stat.st_mtime_ns)
            except OSError as exc:
                errors.append(f"{path}: {type(exc).__name__}: {exc}")
    return WorkspaceSnapshot(records=records, errors=tuple(errors[:100]))


def workspace_snapshot_diff(
    before: WorkspaceSnapshot,
    after: WorkspaceSnapshot,
) -> dict[str, Any]:
    before_paths = set(before.records)
    after_paths = set(after.records)
    added = sorted(after_paths - before_paths)
    deleted = sorted(before_paths - after_paths)
    changed: list[str] = []
    type_changed: list[str] = []
    for path in sorted(before_paths & after_paths):
        old = before.records[path]
        new = after.records[path]
        if old == new:
            continue
        if old and new and old[0] != new[0]:
            type_changed.append(path)
        else:
            changed.append(path)
    return {
        "verifier_workspace_guard": True,
        "verifier_workspace_restore_on_mutation": True,
        "verifier_workspace_restored": False,
        "verifier_workspace_mutation_detected": bool(added or deleted or changed or type_changed),
        "verifier_workspace_mutations": {
            "added": added,
            "changed": changed,
            "deleted": deleted,
            "type_changed": type_changed,
        },
        "verifier_workspace_mutation_counts": {
            "added": len(added),
            "changed": len(changed),
            "deleted": len(deleted),
            "type_changed": len(type_changed),
        },
        "verifier_workspace_snapshot_errors": [*before.errors, *after.errors][:100],
    }


def _small_file_digest(path: Path, size: int) -> str | None:
    if size > 4 * 1024 * 1024:
        return None
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(128 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
