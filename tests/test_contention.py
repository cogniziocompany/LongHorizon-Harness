from __future__ import annotations

from lh_harness.contention import ContentionPeer, detect_contention, groups_to_json
from lh_harness.workspace_identity import WorkspaceIdentity


def identity(path: str, **kwargs) -> WorkspaceIdentity:
    return WorkspaceIdentity(path=path, key=path, is_git=True, **kwargs)


def test_no_contention_for_unrelated_paths() -> None:
    active = {
        "run-a": identity("/a", repo_id="remote:host/org-a/repo", branch="main"),
        "run-b": identity("/b", repo_id="remote:host/org-b/repo", branch="main"),
    }
    groups = detect_contention(active)
    assert groups == []


def test_same_repo_only() -> None:
    active = {
        "run-a": identity("/a", repo_id="remote:host/org/repo", branch="main"),
        "run-b": identity("/b", repo_id="remote:host/org/repo", branch="feat-x"),
    }
    groups = detect_contention(active)
    assert len(groups) == 1
    assert groups[0].severity == "same_repo"


def test_same_repo_same_branch() -> None:
    active = {
        "run-a": identity("/a", repo_id="remote:host/org/repo", branch="main"),
        "run-b": identity("/b", repo_id="remote:host/org/repo", branch="main"),
    }
    groups = detect_contention(active)
    assert len(groups) == 1
    assert groups[0].severity == "same_repo_same_branch"


def test_shared_git_dir_trumps_same_repo() -> None:
    active = {
        "run-a": identity("/a", repo_id="remote:host/org/repo", branch="main", git_common_dir="/common"),
        "run-b": identity("/b", repo_id="remote:host/org/repo", branch="main", git_common_dir="/common"),
    }
    groups = detect_contention(active)
    assert len(groups) == 1
    assert groups[0].severity == "shared_git_dir"


def test_same_tree_is_strongest() -> None:
    active = {
        "run-a": identity("/same", repo_id="remote:host/org/repo", branch="main", git_common_dir="/common"),
        "run-b": identity("/same", repo_id="remote:host/org/repo", branch="main", git_common_dir="/common"),
    }
    groups = detect_contention(active)
    assert len(groups) == 1
    assert groups[0].severity == "same_tree"


def test_min_emit_severity_filters_lower_tiers() -> None:
    active = {
        "run-a": identity("/a", repo_id="remote:host/org/repo", branch="main"),
        "run-b": identity("/b", repo_id="remote:host/org/repo", branch="feat-x"),
    }
    groups = detect_contention(active, min_emit_severity="same_repo_same_branch")
    assert groups == []


def test_grouping_is_one_group_per_real_situation() -> None:
    active = {
        "run-a": identity("/a", repo_id="remote:host/org/repo", branch="main"),
        "run-b": identity("/b", repo_id="remote:host/org/repo", branch="main"),
        "run-c": identity("/c", repo_id="remote:host/org/repo", branch="main"),
    }
    groups = detect_contention(active)
    assert len(groups) == 1
    assert len(groups[0].members) == 3


def test_id_stable_across_ticks() -> None:
    active = {
        "run-a": identity("/a", repo_id="remote:host/org/repo", branch="main"),
        "run-b": identity("/b", repo_id="remote:host/org/repo", branch="main"),
    }
    first = detect_contention(active)
    second = detect_contention(active)
    assert first[0].contention_id == second[0].contention_id


def test_id_changes_on_membership() -> None:
    base = {
        "run-a": identity("/a", repo_id="remote:host/org/repo", branch="main"),
        "run-b": identity("/b", repo_id="remote:host/org/repo", branch="main"),
    }
    first = detect_contention(base)
    extended = {**base, "run-c": identity("/c", repo_id="remote:host/org/repo", branch="main")}
    second = detect_contention(extended)
    assert first[0].contention_id != second[0].contention_id


def test_id_changes_on_branch_severity() -> None:
    base = {
        "run-a": identity("/a", repo_id="remote:host/org/repo", branch="main"),
        "run-b": identity("/b", repo_id="remote:host/org/repo", branch="feat-x"),
    }
    first = detect_contention(base)
    changed = {
        "run-a": identity("/a", repo_id="remote:host/org/repo", branch="main"),
        "run-b": identity("/b", repo_id="remote:host/org/repo", branch="main"),
    }
    second = detect_contention(changed)
    assert first[0].contention_id != second[0].contention_id


def test_capping_max_members() -> None:
    active: dict[str, WorkspaceIdentity] = {}
    for i in range(25):
        active[f"run-{i}"] = identity(f"/{i}", repo_id="remote:host/org/repo", branch="main")
    groups = detect_contention(active, max_members=5)
    assert len(groups[0].members) == 5
    assert groups[0].truncated is True


def test_capping_max_groups() -> None:
    active: dict[str, WorkspaceIdentity] = {}
    for i in range(30):
        # Two runs per repo = one group per repo = 30 groups total.
        active[f"run-{i}a"] = identity(f"/{i}a", repo_id=f"remote:host/org{i}/repo", branch="main")
        active[f"run-{i}b"] = identity(f"/{i}b", repo_id=f"remote:host/org{i}/repo", branch="main")
    groups = detect_contention(active, max_groups=10)
    assert len(groups) == 10


def test_groups_to_json_round_trip() -> None:
    active = {
        "run-a": identity("/a", repo_id="remote:host/org/repo", branch="main"),
        "run-b": identity("/b", repo_id="remote:host/org/repo", branch="main"),
    }
    groups = detect_contention(active)
    payload = groups_to_json(groups)
    assert len(payload) == 1
    assert payload[0]["severity"] == "same_repo_same_branch"
    assert "contention_id" in payload[0]
    assert payload[0]["members"][0]["run_id"] == "run-a"
