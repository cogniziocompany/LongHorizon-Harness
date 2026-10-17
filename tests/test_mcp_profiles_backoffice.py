"""Tests for backoffice / finance / CRM profile families (round 2).

Covers:
  - ``BACKOFFICE_PROFILES_ENABLED`` flag (env-var + project-config driven)
  - profile resolution for each backoffice family
  - ``list_available_profiles()`` visibility
  - auditor-role behaviour with ``finance`` (the only read_only family)
  - "optional-git workspace path" validation for the new-task form stage

No live credentials are referenced; the tests are pure and do not touch the
network.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from lh_harness.mcp_profiles import (
    BACKOFFICE_PROFILE_NAMES,
    list_available_profiles,
    resolve_profile,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clear_mcp_gateway_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop every env var that could leak into MCP profile resolution."""
    for name in (
        "LH_HARNESS_MCP_GATEWAY_KEY",
        "LH_HARNESS_MCP_GATEWAY_URL",
        "LH_HARNESS_MCP_GATEWAY_HEADERS_JSON",
        "LH_HARNESS_WEB_DEFAULT_MCP_PROFILE",
        "LH_HARNESS_WEB_DEFAULT_MANAGER_MCP_PROFILE",
        "LH_HARNESS_WEB_DEFAULT_EXECUTOR_MCP_PROFILE",
        "LH_HARNESS_WEB_DEFAULT_AUDITOR_MCP_PROFILE",
        "LH_HARNESS_BACKOFFICE_PROFILES_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fake_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = "test-key-do-not-use"
    monkeypatch.setenv("LH_HARNESS_MCP_GATEWAY_KEY", key)
    return key


@pytest.fixture
def backoffice_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LH_HARNESS_BACKOFFICE_PROFILES_ENABLED", "true")


# ---------------------------------------------------------------------------
# Flag behaviour
# ---------------------------------------------------------------------------


def test_backoffice_profiles_hidden_by_default(
    clear_mcp_gateway_env, fake_key
) -> None:
    """Without the flag the new families are not visible or resolvable."""
    names = {
        p["name"] for p in list_available_profiles()
    }
    for family in BACKOFFICE_PROFILE_NAMES:
        assert family not in names

    # Even if asked for explicitly, the resolver falls through to the
    # built-in default with the standard "unknown profile" reason.
    profile = resolve_profile("cli_executor", run_profile="backoffice")
    assert profile.name == "default"
    assert "unknown profile" in profile.reason


def test_backoffice_profiles_visible_when_env_flag_set(
    clear_mcp_gateway_env, fake_key, backoffice_enabled
) -> None:
    names = {p["name"] for p in list_available_profiles()}
    assert BACKOFFICE_PROFILE_NAMES.issubset(names)
    for profile in list_available_profiles():
        if profile["name"] in BACKOFFICE_PROFILE_NAMES:
            assert profile["source"] == "built-in"


def test_backoffice_flag_accepts_truthy_aliases(
    clear_mcp_gateway_env, fake_key, monkeypatch
) -> None:
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("LH_HARNESS_BACKOFFICE_PROFILES_ENABLED", value)
        profile = resolve_profile("cli_executor", run_profile="backoffice")
        assert profile.name == "backoffice", f"value {value!r} did not enable flag"


def test_backoffice_flag_rejects_falsy_aliases(
    clear_mcp_gateway_env, fake_key, monkeypatch
) -> None:
    for value in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("LH_HARNESS_BACKOFFICE_PROFILES_ENABLED", value)
        profile = resolve_profile("cli_executor", run_profile="backoffice")
        assert profile.name == "default", f"value {value!r} did not disable flag"


def test_backoffice_flag_read_from_project_config(
    clear_mcp_gateway_env, fake_key, tmp_path
) -> None:
    config_path = tmp_path / ".lh-harness" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "[run]\nbackoffice_profiles_enabled = true\n",
        encoding="utf-8",
    )
    profile = resolve_profile(
        "cli_executor", run_profile="finance", project_config_path=config_path
    )
    assert profile.name == "finance"
    assert profile.read_only is True


def test_backoffice_flag_env_overrides_project_config(
    clear_mcp_gateway_env, fake_key, tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / ".lh-harness" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "[run]\nbackoffice_profiles_enabled = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LH_HARNESS_BACKOFFICE_PROFILES_ENABLED", "false")
    profile = resolve_profile(
        "cli_executor", run_profile="backoffice", project_config_path=config_path
    )
    assert profile.name == "default"


# ---------------------------------------------------------------------------
# Profile resolution per family
# ---------------------------------------------------------------------------


_FAMILY_CASES: list[tuple[str, tuple[str, ...], bool]] = [
    # name, expected_servers, read_only
    (
        "dev",
        (
            "kb", "guides", "skills", "memory", "langfuse_mcp",
            "github", "youtrack",
        ),
        False,
    ),
    (
        "backoffice",
        (
            "kb", "guides", "skills", "memory", "langfuse_mcp",
            "ms365", "pptx", "htmlspec", "web", "research",
        ),
        False,
    ),
    (
        "finance",
        (
            "kb", "guides", "skills", "memory", "langfuse_mcp",
            "quickbooks", "reporting_db",
        ),
        True,
    ),
    (
        "crm_monday",
        (
            "kb", "guides", "skills", "memory", "langfuse_mcp", "monday",
        ),
        False,
    ),
    (
        "crm_dynamics",
        (
            "kb", "guides", "skills", "memory", "langfuse_mcp",
            "dataverse", "d365", "xrm",
        ),
        False,
    ),
]


@pytest.mark.parametrize(
    "family,expected_servers,expected_read_only",
    _FAMILY_CASES,
    ids=[case[0] for case in _FAMILY_CASES],
)
def test_resolve_each_backoffice_family(
    family: str,
    expected_servers: tuple[str, ...],
    expected_read_only: bool,
    clear_mcp_gateway_env,
    fake_key,
    backoffice_enabled,
) -> None:
    profile = resolve_profile("cli_executor", run_profile=family)
    assert profile.name == family
    assert profile.servers == expected_servers
    assert profile.read_only is expected_read_only
    assert profile.source == "built-in"


def test_backoffice_family_resolution_is_independent_of_role(
    clear_mcp_gateway_env, fake_key, backoffice_enabled
) -> None:
    """Backoffice families do not require a special role to resolve."""
    for role in ("cli_executor", "gui_executor", "manager"):
        profile = resolve_profile(role, run_profile="backoffice")
        assert profile.name == "backoffice"


# ---------------------------------------------------------------------------
# Auditor role interactions
# ---------------------------------------------------------------------------


def test_finance_profile_resolves_for_auditor_without_override(
    clear_mcp_gateway_env, fake_key, backoffice_enabled
) -> None:
    """``finance`` is the only backoffice family with read_only=True, so
    auditors can resolve it without an explicit write-MCP override."""
    for role in ("cli_auditor", "gui_auditor", "auditor_format_repair"):
        profile = resolve_profile(role, run_profile="finance")
        assert profile.name == "finance"
        assert profile.read_only is True


def test_write_backoffice_family_rejected_for_auditor(
    clear_mcp_gateway_env, fake_key, backoffice_enabled
) -> None:
    """``backoffice``, ``crm_monday``, ``crm_dynamics`` are write-capable,
    so the standard auditor read-only enforcement applies."""
    for family in ("backoffice", "crm_monday", "crm_dynamics", "dev"):
        with pytest.raises(
            ValueError, match="auditor roles require a read-only profile"
        ):
            resolve_profile("cli_auditor", run_profile=family)


def test_write_backoffice_family_allowed_for_auditor_with_override(
    clear_mcp_gateway_env, fake_key, backoffice_enabled
) -> None:
    profile = resolve_profile(
        "cli_auditor",
        run_profile="backoffice",
        allow_auditor_write_mcp=True,
    )
    assert profile.name == "backoffice"
    assert profile.read_only is False


# ---------------------------------------------------------------------------
# "Optional-git workspace path" — validation helper for the new-task form
# ---------------------------------------------------------------------------


def _validate_workspace_choice(
    profile_family: str,
    workspace_kind: str,
    has_git: bool,
    has_artifact: bool,
) -> tuple[bool, str]:
    """Pure re-implementation of the workspace validation rule.

    Mirrors the matrix in ``docs/design/agent-profiles-backoffice.md`` §4.2.
    Returns ``(ok, message)`` where ``message`` is non-empty when ``ok`` is
    False.  This is the executable form the API contract will call; the
    tests pin the contract so that the harness and the Web bundle agree.
    """
    if workspace_kind not in {"git_repo", "artifact_only"}:
        return False, f"unknown workspace_kind {workspace_kind!r}"
    if workspace_kind == "git_repo" and has_artifact:
        return False, "git_repo and artifact are mutually exclusive"
    if workspace_kind == "artifact_only" and has_git:
        return False, "artifact_only does not accept git fields"
    allowed: dict[str, frozenset[str]] = {
        "dev": frozenset({"git_repo"}),
        "backoffice": frozenset({"git_repo", "artifact_only"}),
        "finance": frozenset({"git_repo", "artifact_only"}),
        "crm_monday": frozenset({"git_repo", "artifact_only"}),
        "crm_dynamics": frozenset({"git_repo", "artifact_only"}),
        "audit": frozenset({"git_repo", "artifact_only"}),
        "none": frozenset({"artifact_only"}),
    }
    families = allowed.get(profile_family)
    if families is None:
        return False, f"unknown profile_family {profile_family!r}"
    if workspace_kind not in families:
        return (
            False,
            f"profile_family {profile_family!r} does not permit {workspace_kind!r}",
        )
    return True, ""


@pytest.mark.parametrize(
    "profile_family,workspace_kind",
    [
        ("dev", "git_repo"),
        ("backoffice", "artifact_only"),
        ("backoffice", "git_repo"),
        ("finance", "artifact_only"),
        ("finance", "git_repo"),
        ("crm_monday", "git_repo"),
        ("crm_dynamics", "artifact_only"),
        ("audit", "artifact_only"),
        ("none", "artifact_only"),
    ],
)
def test_workspace_choice_allowed(
    profile_family: str, workspace_kind: str
) -> None:
    ok, msg = _validate_workspace_choice(
        profile_family, workspace_kind, has_git=False, has_artifact=False
    )
    assert ok, msg


@pytest.mark.parametrize(
    "profile_family,workspace_kind",
    [
        ("dev", "artifact_only"),
        ("none", "git_repo"),
        ("audit", "nope"),
    ],
)
def test_workspace_choice_rejected(
    profile_family: str, workspace_kind: str
) -> None:
    ok, msg = _validate_workspace_choice(
        profile_family, workspace_kind, has_git=False, has_artifact=False
    )
    assert not ok
    assert msg


def test_workspace_choice_rejects_both_git_and_artifact() -> None:
    ok, msg = _validate_workspace_choice(
        "backoffice", "git_repo", has_git=True, has_artifact=True
    )
    assert not ok
    assert "mutually exclusive" in msg


def test_workspace_choice_rejects_git_fields_on_artifact_only() -> None:
    ok, msg = _validate_workspace_choice(
        "backoffice", "artifact_only", has_git=True, has_artifact=False
    )
    assert not ok
    assert "git fields" in msg


# ---------------------------------------------------------------------------
# "Git must stay optional" — the contract for CFO1 / artifact-only work
# ---------------------------------------------------------------------------


def test_backoffice_family_resolves_without_git(
    clear_mcp_gateway_env, fake_key, backoffice_enabled
) -> None:
    """CFO1 may run artifact-only with no repo; ``backoffice`` family is
    resolvable without any git fields being supplied."""
    profile = resolve_profile("cli_executor", run_profile="backoffice")
    assert profile.name == "backoffice"
    assert profile.servers is not None
    assert "github" not in profile.servers
    assert "ssh" not in profile.servers


def test_none_profile_requires_artifact_only() -> None:
    """``none`` is the only profile that does not need any server allow-list;
    the new-task form must require artifact_only for it."""
    ok, msg = _validate_workspace_choice(
        "none", "artifact_only", has_git=False, has_artifact=True
    )
    assert ok, msg
    ok, msg = _validate_workspace_choice(
        "none", "git_repo", has_git=True, has_artifact=False
    )
    assert not ok


# ---------------------------------------------------------------------------
# list_available_profiles() guards
# ---------------------------------------------------------------------------


def test_list_available_includes_backoffice_when_enabled(
    clear_mcp_gateway_env, fake_key, backoffice_enabled
) -> None:
    profiles = list_available_profiles()
    by_name = {p["name"]: p for p in profiles}
    for family in BACKOFFICE_PROFILE_NAMES:
        assert family in by_name
        assert "Authorization" not in str(by_name[family])


def test_list_available_does_not_leak_key(
    clear_mcp_gateway_env, fake_key, backoffice_enabled
) -> None:
    for profile in list_available_profiles():
        assert "Authorization" not in str(profile)
        assert "Bearer" not in str(profile)


def test_existing_builtins_unchanged_when_flag_off(
    clear_mcp_gateway_env, fake_key
) -> None:
    """Default-off behaviour must keep the legacy API surface intact."""
    names = {p["name"] for p in list_available_profiles()}
    assert names == {"none", "audit", "default", "ops", "full"}
    # And resolution still picks the legacy defaults.
    assert resolve_profile("manager").name == "default"
    assert resolve_profile("cli_auditor").name == "audit"
    assert resolve_profile("cli_executor").name == "default"