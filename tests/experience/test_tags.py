"""Tag extraction fixtures: repo, branch, next_step, tool names, error
signature, and the per-role model trio — all read-only, all run-dir scoped."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from lh_harness.experience.tags import (
    ERROR_SIGNATURE_MAX_CHARS,
    branch_from_run_dir_copy,
    branch_from_task_text,
    detect_branch,
    error_signature,
    first_blocker_line,
    next_step_tag,
    repo_from_owner,
    repo_from_workspace_path,
    roles_from_owner,
    tool_names_from_round_dir,
    tool_names_from_steps,
    tool_names_from_trajectory_jsonl,
)

GIT = shutil.which("git")


def _git(*args: str, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [GIT, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )


def _init_repo_on_branch(path, branch: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", branch, str(path))
    _git("-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "init", cwd=str(path))


# --- repo -----------------------------------------------------------------


def test_repo_from_owner_uses_workspace_basename():
    assert repo_from_owner({"workspace": "/home/harness/work/LongHorizon-Harness"}) == (
        "LongHorizon-Harness"
    )
    assert repo_from_owner({"workspace": "/home/harness/work/repo/"}) == "repo"
    assert repo_from_owner({"workspace": ""}) == ""
    assert repo_from_owner({}) == ""
    assert repo_from_owner(None) == ""
    assert repo_from_workspace_path("C:\\Users\\x\\checkout") == "checkout"


# --- branch from task text --------------------------------------------------


def test_branch_from_task_text_variants():
    assert branch_from_task_text("Work ONLY in branch feat/experience-layer; do not push") == (
        "feat/experience-layer"
    )
    assert branch_from_task_text("branch: main\ndo the thing") == "main"
    assert branch_from_task_text("on branch release/cta-9 run e2e") == "release/cta-9"
    assert branch_from_task_text("quoted branch: `feat/x`") == "feat/x"
    assert branch_from_task_text("no branches here at all") == ""
    assert branch_from_task_text("") == ""


def test_branch_from_task_text_rejects_grammar_false_positive():
    assert branch_from_task_text("branch and merge the docs after review") == ""


# --- branch from a run-dir repo copy (never the workspace) ------------------


@pytest.mark.skipif(GIT is None, reason="git binary unavailable")
def test_branch_probe_reads_branch_inside_run_dir(tmp_path):
    run_dir = tmp_path / "runs" / "run_x"
    copy = run_dir / "workspace_copy"
    _init_repo_on_branch(copy, "feat/trace-9")
    assert branch_from_run_dir_copy(run_dir) == "feat/trace-9"


@pytest.mark.skipif(GIT is None, reason="git binary unavailable")
def test_branch_probe_refuses_paths_outside_run_dir(tmp_path):
    """The never-the-workspace rule: a copy outside the run dir is invisible."""
    outside = tmp_path / "live_workspace"
    _init_repo_on_branch(outside, "forbidden-branch")
    run_dir = tmp_path / "runs" / "run_y"
    run_dir.mkdir(parents=True)
    # The default candidates do not exist inside the run dir: no probe runs.
    assert branch_from_run_dir_copy(run_dir) == ""
    # An escape attempt via a relative path is refused, not resolved onto the
    # workspace.
    assert branch_from_run_dir_copy(run_dir, relpaths=("../live_workspace",)) == ""


def test_branch_probe_without_any_copy_is_absent(tmp_path):
    run_dir = tmp_path / "runs" / "run_z"
    run_dir.mkdir(parents=True)
    assert branch_from_run_dir_copy(run_dir) == ""


def test_detect_branch_prefers_task_text(tmp_path):
    assert detect_branch("branch: feat/wins", tmp_path) == "feat/wins"
    assert detect_branch("", None) == ""


# --- next_step ---------------------------------------------------------------


def test_next_step_tag():
    assert next_step_tag("cli") == "cli"
    assert next_step_tag("GUI") == "gui"
    assert next_step_tag(" gui ") == "gui"
    assert next_step_tag("done") == ""
    assert next_step_tag("ask") == ""
    assert next_step_tag("") == ""
    assert next_step_tag(None) == ""


# --- tool names ----------------------------------------------------------------


def _steps():
    return [
        {"kind": "text", "content": "hi"},
        {"kind": "tool_use", "id": "t1", "name": "Bash"},
        {"kind": "tool_use", "id": "t2", "name": "Read"},
        # The streamed writer can duplicate a tool_use in the completion
        # record; the tag keeps the first occurrence only.
        {"kind": "tool_use", "id": "t1", "name": "Bash"},
        {"kind": "tool_use", "id": "t3", "name": ""},
        {"kind": "tool_result", "name": "Ignored"},
    ]


def test_tool_names_from_steps_first_use_order():
    assert tool_names_from_steps(_steps()) == ["Bash", "Read"]
    assert tool_names_from_steps([]) == []
    assert tool_names_from_steps(None) == []


def test_tool_names_from_trajectory_jsonl_tolerates_malformed_tail(tmp_path):
    path = tmp_path / "executor_trajectory.jsonl"
    lines = [json.dumps(step) for step in _steps()]
    path.write_text("\n".join(lines) + "\n" + '{"kind": "tool_use", "na')  # cut mid-line
    assert tool_names_from_trajectory_jsonl(path) == ["Bash", "Read"]
    assert tool_names_from_trajectory_jsonl(tmp_path / "missing.jsonl") == []


def test_tool_names_from_round_dir_unions_roles_and_skips_raw(tmp_path):
    round_dir = tmp_path / "rounds" / "round_001"
    round_dir.mkdir(parents=True)
    (round_dir / "manager_trajectory.jsonl").write_text(
        json.dumps({"kind": "tool_use", "name": "Task"}) + "\n"
    )
    (round_dir / "executor_trajectory.jsonl").write_text(
        json.dumps({"kind": "tool_use", "name": "Bash"})
        + "\n"
        + json.dumps({"kind": "tool_use", "name": "Edit"})
        + "\n"
    )
    (round_dir / "auditor_trajectory.jsonl").write_text(
        json.dumps({"kind": "tool_use", "name": "Read"}) + "\n"
    )
    # Raw provider streams are never the normalized vocabulary.
    (round_dir / "executor_raw_trajectory.jsonl").write_text(
        json.dumps({"kind": "tool_use", "name": "ProviderInternal"}) + "\n"
    )
    assert tool_names_from_round_dir(round_dir) == ["Task", "Bash", "Edit", "Read"]
    assert tool_names_from_round_dir(tmp_path / "nope") == []


# --- error signature ------------------------------------------------------------


MANAGER_REPORT = (
    "Status: incomplete\n"
    "Integrity: clean\n"
    "Contract audit: aligned\n\n"
    "Blocking constraints:\n"
    "- none.\n"
    "Next step: keep going\n"
)

BLOCKED_REPORT_LIST = (
    "Status: blocked\n"
    "Integrity: clean\n"
    "Contract audit: aligned\n\n"
    "Blocking constraints:\n"
    "- owner.json workspace field is missing\n"
    "- second item\n"
    "\n"
    "Next step: restore the field\n"
)

BLOCKED_REPORT_INLINE = (
    "Status: blocked\n"
    "Integrity: clean\n"
    "Contract audit: aligned\n\n"
    "Blocking constraints: provider refused the plan schema\n"
)


def test_first_blocker_line_inline_and_list_forms():
    assert first_blocker_line(BLOCKED_REPORT_LIST) == "owner.json workspace field is missing"
    assert first_blocker_line(BLOCKED_REPORT_INLINE) == "provider refused the plan schema"
    assert first_blocker_line(MANAGER_REPORT) == ""
    assert first_blocker_line("Status: complete\nIntegrity: clean\nContract audit: aligned") == ""


def test_first_blocker_line_rejects_none_and_stops_at_next_section():
    report = (
        "Blocking constraints: none\n"
        "Next step: keep going\n"
    )
    assert first_blocker_line(report) == ""
    zh = "状态: blocked\n完整性: clean\n契约审计: aligned\n\n阻断约束:\n- 工作区写入被拒绝\n\n下一步: 重试\n"
    assert first_blocker_line(zh) == "工作区写入被拒绝"


def test_error_signature_prefers_blockers_then_provider():
    assert error_signature({"auditor_report": BLOCKED_REPORT_LIST}) == (
        "owner.json workspace field is missing"
    )
    assert error_signature(
        {"auditor_report": MANAGER_REPORT}, abort_reason="provider_timeout"
    ) == "provider_timeout"
    assert error_signature({"auditor_report": MANAGER_REPORT}) is None
    assert error_signature(None, abort_reason="provider_quota") == "provider_quota"
    # A non-provider abort reason is not an error signature.
    assert error_signature(None, abort_reason="max_rounds_exhausted") is None


def test_error_signature_redacts_and_bounds_the_blocker_line():
    key = "sk-ant-api03-" + "K" * 24
    report = f"Blocking constraints: leaked ANTHROPIC_API_KEY={key} in config\n"
    signature = error_signature({"auditor_report": report})
    assert signature is not None
    assert key not in signature
    assert "***REDACTED***" in signature

    long_line = "Blocking constraints: " + "x" * (ERROR_SIGNATURE_MAX_CHARS + 200)
    signature = error_signature({"auditor_report": long_line})
    assert len(signature) == ERROR_SIGNATURE_MAX_CHARS


def test_error_signature_accepts_dataclass_rounds():
    from dataclasses import dataclass, field

    @dataclass
    class Round:
        auditor_report: str = ""
        extra: dict = field(default_factory=dict)

    assert error_signature(Round(auditor_report=BLOCKED_REPORT_INLINE)) == (
        "provider refused the plan schema"
    )


# --- model trio per role ---------------------------------------------------------


def test_roles_from_owner_route_bound_shape():
    owner = {
        "route": {
            "bound": {
                "roles": {
                    "manager": {"model": "kimi-k3", "backend": "synthetic",
                                 "tier": "ideal", "rationale": "top-level invariant"},
                    "executor": {"model": "kimi-k3", "backend": "synthetic",
                                  "tier": "adequate", "rationale": "claude is busy"},
                    "auditor": {"model": "kimi-k3", "backend": "synthetic",
                                 "tier": "adequate", "rationale": "cheap enough"},
                }
            }
        },
        "role_configs": {
            "manager": {"agent": "claude_code", "model": "claude-sonnet-5"},
            "executor": {"agent": "deepseek_harness", "model": "deepseek-v4"},
            "auditor": {"agent": "codex", "model": "gpt-5.3"},
        },
    }
    roles = roles_from_owner(owner)
    assert list(roles) == ["manager", "executor", "auditor"]
    manager = roles["manager"]
    # The route re-keys the model; the agent still comes from role_configs.
    assert manager.model == "kimi-k3"
    assert manager.agent == "claude_code"
    assert manager.route_tier == "ideal"
    assert manager.route_rationale == "top-level invariant"


def test_roles_from_owner_rationale_is_redacted_and_capped():
    owner = {
        "route": {
            "bound": {
                "roles": {
                    "manager": {
                        "model": "kimi-k3",
                        "tier": "override",
                        "rationale": "fallback after ANTHROPIC_API_KEY=sk-ant-api03-" + "R" * 24,
                    }
                }
            }
        }
    }
    roles = roles_from_owner(owner)
    rationale = roles["manager"].route_rationale
    assert rationale is not None
    assert "R" * 24 not in rationale
    assert "***REDACTED***" in rationale


def test_roles_from_owner_role_configs_without_route():
    owner = {
        "role_configs": {
            "manager": {"agent": "codex", "model": "gpt-5.3-codex"},
            "executor": {"agent": "claude_code", "model": "claude-sonnet-5"},
            "auditor": {"agent": "opencode", "model": "qwen3-coder"},
        },
        "workspace": "/home/harness/work/repo",
    }
    roles = roles_from_owner(owner)
    assert roles["executor"].agent == "claude_code"
    assert roles["executor"].model == "claude-sonnet-5"
    assert roles["executor"].route_tier is None
    assert roles["executor"].route_rationale is None


def test_roles_from_owner_legacy_top_level_agent_model():
    owner = {"agent": "codex", "model": "gpt-5.3-codex", "workspace": "/r"}
    roles = roles_from_owner(owner)
    assert list(roles) == ["manager", "executor", "auditor"]
    assert all(role.agent == "codex" and role.model == "gpt-5.3-codex" for role in roles.values())


def test_roles_from_owner_without_anything_is_empty():
    assert roles_from_owner({}) == {}
    assert roles_from_owner(None) == {}
    assert roles_from_owner("not a mapping") == {}
