from __future__ import annotations

import pytest

from lh_harness.adapters.claude_code import ClaudeCodeAdapter
from lh_harness.adapters.claude_permissions import (
    AUDITOR_NETWORK_GIT_DENY,
    AUDITOR_NETWORK_GIT_ENV,
    _AUDITOR_ROLES,
    policy_for_role,
)
from lh_harness.auditor_agent import audit_report_from_episode_result


def test_auditor_policy_contains_network_git_deny_rules() -> None:
    for role in _AUDITOR_ROLES:
        policy = policy_for_role(role)
        for rule in AUDITOR_NETWORK_GIT_DENY:
            assert rule in policy.disallowed_tools, f"{role} missing {rule}"


@pytest.mark.parametrize("role", ["manager", "gui_executor", "cli_executor"])
def test_non_auditor_roles_do_not_get_network_git_deny_rules(role: str) -> None:
    policy = policy_for_role(role)
    for rule in AUDITOR_NETWORK_GIT_DENY:
        assert rule not in policy.disallowed_tools


@pytest.mark.parametrize("role", _AUDITOR_ROLES)
def test_auditor_env_contains_network_git_overrides(role: str) -> None:
    policy = policy_for_role(role)
    assert policy.env_overrides["GIT_TERMINAL_PROMPT"] == "0"
    assert policy.env_overrides["GIT_ASKPASS"] == "/bin/false"
    assert policy.env_overrides["SSH_AUTH_SOCK"] is None


@pytest.mark.parametrize("role", _AUDITOR_ROLES)
def test_auditor_command_template_injects_git_overrides(role: str) -> None:
    adapter = ClaudeCodeAdapter(role=role, run_id="r")
    template = adapter.command_template
    assert "GIT_TERMINAL_PROMPT=0" in template
    assert "GIT_ASKPASS=/bin/false" in template
    assert "--unsetenvvar=SSH_AUTH_SOCK" in template
    assert "GIT_CONFIG_COUNT=4" in template
    assert "GIT_CONFIG_KEY_0=credential.helper" in template
    assert "GIT_CONFIG_VALUE_0=" in template


@pytest.mark.parametrize("role", ["cli_executor", "gui_executor"])
def test_executor_command_template_does_not_inject_git_overrides(role: str) -> None:
    adapter = ClaudeCodeAdapter(role=role, run_id="r")
    template = adapter.command_template
    assert "GIT_TERMINAL_PROMPT=0" not in template
    assert "GIT_ASKPASS=/bin/false" not in template
    assert "--unsetenvvar=SSH_AUTH_SOCK" not in template


def _episode_result(status: str, mutations: dict[str, list[str]]) -> object:
    class FakeResult:
        pass

    result = FakeResult()
    result.status = status
    result.error = ""
    result.actions_log = "Status: blocked\nIntegrity: violation\nContract audit: unknown\n"
    result.output = ""
    result.metadata = {
        "verifier_workspace_mutation_detected": True,
        "verifier_workspace_mutations": mutations,
        "verifier_workspace_restored": False,
        "verifier_workspace_restore_on_mutation": True,
        "runtime_signals": [],
    }
    return result


def test_git_only_guard_mutation_adds_network_git_op_hint() -> None:
    result = _episode_result("done", {"added": [".git/objects/abc"], "changed": [], "deleted": [], "type_changed": []})
    report = audit_report_from_episode_result(result, 1, language="en")
    types = [f["type"] for f in report.integrity_findings]
    assert "network_git_op" in types


def test_mixed_guard_mutation_does_not_add_network_git_op_hint() -> None:
    result = _episode_result(
        "done",
        {"added": [".git/objects/abc", "src/file.py"], "changed": [], "deleted": [], "type_changed": []},
    )
    report = audit_report_from_episode_result(result, 1, language="en")
    types = [f["type"] for f in report.integrity_findings]
    assert "network_git_op" not in types
