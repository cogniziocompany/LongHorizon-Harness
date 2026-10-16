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
    "Bash(gh *)",
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

# Deny rules for auditor local git operations that can rewrite the executor's
# branch, refs, or working tree (task: HARNESS-AUDITOR-ISOLATION, incident
# 20261016T164706Z_5c063e3e).  The executor's HEAD is recorded into a local
# ref before the auditor launches and the auditor runs on a read-only
# snapshot, so any of these in the executor workspace are denied outright.
# The list mirrors the network deny tuple's -C / --git-dir variants so the
# auditor cannot pivot the operation at another working tree either.
#
# ``update-ref`` (and the ``--stdin`` / ``-d`` / ``--no-deref`` variants) are
# the reference-writing family that the operator correction flagged as
# missing from the round-001 list.  An auditor that bypasses Write/Edit
# but still has Bash can otherwise rewrite any ref (HEAD, branch,
# refs/lh-harness/...) without going through checkout/reset.
AUDITOR_LOCAL_GIT_DENY: tuple[str, ...] = (
    "Bash(git checkout*)",
    "Bash(git reset*)",
    "Bash(git switch*)",
    "Bash(git clean*)",
    "Bash(git stash*)",
    "Bash(git restore*)",
    "Bash(git commit*)",
    "Bash(git rebase*)",
    "Bash(git merge*)",
    # Reference-writing family (covers update-ref, update-ref --stdin,
    # update-ref -d, symbolic-ref, etc.).  ``git symbolic-ref`` is included
    # because it rewrites HEAD-style refs without going through checkout.
    "Bash(git update-ref*)",
    "Bash(git symbolic-ref*)",
    # Variants that point git at a different directory via -C or --git-dir.
    "Bash(git -C * checkout*)",
    "Bash(git -C * reset*)",
    "Bash(git -C * switch*)",
    "Bash(git -C * clean*)",
    "Bash(git -C * stash*)",
    "Bash(git -C * restore*)",
    "Bash(git -C * commit*)",
    "Bash(git -C * rebase*)",
    "Bash(git -C * merge*)",
    "Bash(git -C * update-ref*)",
    "Bash(git -C * symbolic-ref*)",
    "Bash(git --git-dir* checkout*)",
    "Bash(git --git-dir* reset*)",
    "Bash(git --git-dir* switch*)",
    "Bash(git --git-dir* clean*)",
    "Bash(git --git-dir* stash*)",
    "Bash(git --git-dir* restore*)",
    "Bash(git --git-dir* commit*)",
    "Bash(git --git-dir* rebase*)",
    "Bash(git --git-dir* merge*)",
    "Bash(git --git-dir* update-ref*)",
    "Bash(git --git-dir* symbolic-ref*)",
)

# Deny rules for filesystem mutation channels that could reverse the
# snapshot's chmod a-w protection (the executor workspace must stay
# untouched even if a Bash subprocess ignores the audit's tool deny layer).
# These complement ``_WRITE_TOOLS`` (Write/Edit/NotebookEdit) which already
# blocks Claude's own file-mutation tools.
AUDITOR_FS_DENY: tuple[str, ...] = (
    # chmod/chown would let the auditor reverse the read-only chmod applied
    # to the snapshot by ``auditor_snapshot._chmod_readonly``.
    "Bash(chmod*)",
    "Bash(chown*)",
    # rm -rf / unlink would let the auditor destroy the snapshot, the
    # pinned ref, or attempt to delete files inside the executor tree
    # (the executor path is not exposed to the auditor but defense in
    # depth still applies).  ``Bash(rm*)`` catches rm, rm -rf, etc.
    "Bash(rm*)",
    "Bash(unlink*)",
    # install / cp -r / mv would let it move or copy files into / out of
    # the snapshot path in ways the deny list does not anticipate.
    "Bash(install*)",
    "Bash(mv*)",
)

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
                *AUDITOR_LOCAL_GIT_DENY,
                *AUDITOR_FS_DENY,
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
