"""Pure functions for detecting workspace contention among active harness runs.

Contention tiers (ascending severity):
  same_repo           = same repo_id only (intended throughput pattern)
  same_repo_same_branch = same repo_id and same non-null branch (-b mid-PR hazard)
  shared_git_dir      = same git_common_dir, different key (worktree siblings)
  same_tree           = same identity.key (doctrine violation)

Nothing in this module performs I/O; it consumes already-resolved identities.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .workspace_identity import WorkspaceIdentity


CONTIERS = ["same_repo", "same_repo_same_branch", "shared_git_dir", "same_tree"]
TIER_ORDER: dict[str, int] = {name: index for index, name in enumerate(CONTIERS)}


@dataclass(frozen=True)
class ContentionPeer:
    run_id: str
    workspace: str
    branch: str | None


@dataclass(frozen=True)
class ContentionGroup:
    contention_id: str
    severity: str
    group_key: str
    members: tuple[ContentionPeer, ...]
    truncated: bool = False


def _tier_for(
    left: WorkspaceIdentity,
    right: WorkspaceIdentity,
) -> str | None:
    """Return the strongest applicable tier between two identities, or None."""

    if left.key and left.key == right.key:
        return "same_tree"
    if left.git_common_dir and left.git_common_dir == right.git_common_dir:
        return "shared_git_dir"
    if left.repo_id and left.repo_id == right.repo_id:
        if left.branch and left.branch == right.branch:
            return "same_repo_same_branch"
        return "same_repo"
    return None


def _group_key(repo_id: str | None, key: str | None, common_dir: str | None) -> str:
    if common_dir:
        return f"common:{common_dir}"
    if repo_id:
        return f"repo:{repo_id}"
    if key:
        return f"path:{key}"
    return "unknown"


def detect_contention(
    active: dict[str, WorkspaceIdentity],
    *,
    min_emit_severity: str = "same_repo",
    max_groups: int = 50,
    max_members: int = 20,
) -> list[ContentionGroup]:
    """Group active runs by contention and emit one group per real situation.

    ``active`` maps run_id to identity.  Only active runs are members; a run
    that is merely queued has not started and cannot collide.  Severity is the
    maximum tier found over member pairs.  A standing contention yields the same
    contention_id as long as its canonical representation is unchanged, so a tick
    that sees no change does not re-emit.
    """

    min_index = TIER_ORDER.get(min_emit_severity, 0)
    ids = sorted(active)
    # Build groups by key, recording the maximum tier seen within each group.
    raw_groups: dict[str, dict[str, Any]] = {}
    for i, left_id in enumerate(ids):
        left = active[left_id]
        for right_id in ids[i + 1 :]:
            right = active[right_id]
            tier = _tier_for(left, right)
            if tier is None:
                continue
            if TIER_ORDER[tier] < min_index:
                continue
            # Group by repo_id first, then by path key for non-git paths.
            key = _group_key(
                left.repo_id if left.repo_id else right.repo_id,
                None,
                left.git_common_dir
                if left.git_common_dir
                else right.git_common_dir,
            )
            entry = raw_groups.setdefault(
                key,
                {
                    "run_ids": set(),
                    "tier_index": TIER_ORDER[tier],
                },
            )
            entry["run_ids"].add(left_id)
            entry["run_ids"].add(right_id)
            entry["tier_index"] = max(entry["tier_index"], TIER_ORDER[tier])

    groups: list[ContentionGroup] = []
    for key, data in raw_groups.items():
        run_ids = sorted(data["run_ids"])
        truncated = len(run_ids) > max_members
        visible_ids = run_ids[:max_members]
        members: list[ContentionPeer] = []
        for run_id in visible_ids:
            identity = active[run_id]
            members.append(
                ContentionPeer(
                    run_id=run_id,
                    workspace=identity.path,
                    branch=identity.branch,
                )
            )
        severity = CONTIERS[data["tier_index"]]
        canonical = {
            "group_key": key,
            "severity": severity,
            "members": [
                {"run_id": peer.run_id, "workspace": peer.workspace, "branch": peer.branch}
                for peer in members
            ],
        }
        contention_id = hashlib.sha256(
            json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        groups.append(
            ContentionGroup(
                contention_id=contention_id,
                severity=severity,
                group_key=key,
                members=tuple(members),
                truncated=truncated,
            )
        )

    # Stable deterministic order: severity desc, then group key.
    groups.sort(key=lambda g: (-TIER_ORDER[g.severity], g.group_key))
    return groups[:max_groups]


def groups_to_json(groups: list[ContentionGroup]) -> list[dict[str, Any]]:
    """Serialise groups for persistence and API responses."""

    return [
        {
            "contention_id": group.contention_id,
            "severity": group.severity,
            "group_key": group.group_key,
            "members": [
                {
                    "run_id": member.run_id,
                    "workspace": member.workspace,
                    "branch": member.branch,
                }
                for member in group.members
            ],
            "truncated": group.truncated,
        }
        for group in groups
    ]
