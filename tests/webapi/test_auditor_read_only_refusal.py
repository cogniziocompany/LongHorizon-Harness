"""Task 234: the auditor read-only refusal, end to end.

Covers the exact refusal measured on CT110 (2026-09-23): an auditor role bound
to the non-read-only ``default`` profile was refused by
``supervisor/service.py::_normalise_role_configs``, but the queue launcher let
that ``ValueError`` escape into ``create_run`` and burn an attempt via
``mark_failed``.  These tests pin (1) the refusal text, (2) the launcher's
pre-burn eligibility check, and (3) the worker-side precedence that makes an
explicit ``[run.roles.auditor] mcp_profile`` binding the fix.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from lh_harness.launcher import Launcher, _role_configs
from lh_harness.mcp_profiles import resolve_profile
from lh_harness.queue import QueueStore, default_queue_config
from lh_harness.supervisor.service import _normalise_role_configs, auditor_read_only_violation

from .test_launcher import FakeSupervisor, _base_entry, _fixture


@pytest.fixture
def clean_profile_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LH_HARNESS_WEB_DEFAULT_MCP_PROFILE",
        "LH_HARNESS_WEB_DEFAULT_AUDITOR_MCP_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_normalise_role_configs_refuses_non_read_only_auditor_profile(
    clean_profile_env,
) -> None:
    with pytest.raises(ValueError) as raised:
        _normalise_role_configs(
            {"auditor": {"agent": "claude_code", "model": "m", "mcp_profile": "default"}},
            agent="claude_code",
            model="m",
        )
    assert (
        str(raised.value)
        == "roles.auditor.mcp_profile 'default' is not read-only; "
        "auditor roles require a read-only MCP profile"
    )


def test_normalise_role_configs_accepts_read_only_auditor_profile(
    clean_profile_env,
) -> None:
    resolved = _normalise_role_configs(
        {"auditor": {"agent": "claude_code", "model": "m", "mcp_profile": "audit"}},
        agent="claude_code",
        model="m",
    )
    assert resolved["auditor"]["mcp_profile"] == "audit"


def test_launcher_pre_burn_refusal_leaves_attempt_and_status_untouched(
    tmp_path: Path,
    clean_profile_env,
) -> None:
    """The exact CT110 shape: trio profile 'default' + no explicit auditor binding.

    The refusal must be recorded as a skip reason on the pre-burn path — status
    stays pending, attempt count unchanged, no failed attempt, no create_run
    call — instead of reaching ``mark_failed``.
    """
    root = tmp_path / "runs"
    root.mkdir()
    store = QueueStore(root)
    supervisor = FakeSupervisor(root)
    config = default_queue_config()
    config["trios"]["kimi"] = {
        "agent": "claude_code",
        "model": "kimi-k3",
        "mcp_profile": "default",
    }
    launcher = Launcher(supervisor, store, queue_config=config)
    entry = store.create(_base_entry(trio="kimi"))
    initial_attempt = entry.attempt

    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "pending"
    assert updated.attempt == initial_attempt
    assert updated.reason is None
    assert any(
        "roles.auditor.mcp_profile" in reason and "not read-only" in reason
        for reason in updated.skip_reasons
    )
    assert not supervisor.created


def test_launcher_accepts_config_bound_read_only_auditor_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_profile_env,
) -> None:
    """An explicit read-only [run.roles.auditor] mcp_profile lifts the refusal.

    Worker-side precedence (mcp_profiles.resolve_profile, step 3 over 4): the
    role binding outranks the run-wide profile.  The launcher's pre-burn check
    honours the same precedence, so a deployment can bind the auditor to a
    read-only profile in config while the trio keeps its own profile.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[run.roles.auditor]\nmcp_profile = "audit"\n', encoding="utf-8"
    )
    monkeypatch.setattr("lh_harness.launcher.PROJECT_CONFIG_PATH", config_path)
    monkeypatch.setattr("lh_harness.config.PROJECT_CONFIG_PATH", config_path)

    root = tmp_path / "runs"
    root.mkdir()
    store = QueueStore(root)
    supervisor = FakeSupervisor(root)
    config = default_queue_config()
    config["trios"]["kimi"] = {
        "agent": "claude_code",
        "model": "kimi-k3",
        "mcp_profile": "default",
    }
    launcher = Launcher(supervisor, store, queue_config=config)
    entry = store.create(_base_entry(trio="kimi"))

    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "launched"
    owner = supervisor.created[-1]["owner"]
    # Launched specs stay stripped of mcp_profile (cutover 168 reservation rule).
    assert all(
        spec == {"agent": "claude_code", "model": "kimi-k3"}
        for spec in owner["role_configs"].values()
    )


def test_worker_side_precedence_role_binding_beats_run_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_profile_env,
) -> None:
    """Worker-side: [run.roles.auditor] mcp_profile outranks --mcp-profile.

    This is what makes the consumed-config fix load-bearing on the worker: the
    supervisor's validation may accept the auditor, but the worker re-resolves
    from the CLI's effective precedence, and the role binding wins there too.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[run.roles.auditor]\nmcp_profile = "audit"\n', encoding="utf-8"
    )
    monkeypatch.setenv("LH_HARNESS_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr("lh_harness.mcp_profiles._state_root", lambda: tmp_path / "state")
    (tmp_path / "state").mkdir()
    resolved = resolve_profile(
        "auditor",
        role_profile="audit",
        run_profile="default",
        gateway_key="test",
        project_config_path=config_path,
        allow_auditor_write_mcp=False,
    )
    assert resolved.name == "audit"
    assert resolved.read_only is True
    # And with no role binding, the run-wide profile reaches the auditor and
    # violates read-only — the shape the launcher must refuse pre-burn.
    violated = resolve_profile(
        "auditor",
        run_profile="default",
        gateway_key="test",
        allow_auditor_write_mcp=False,
    )
    assert violated.name == "default"
    assert violated.read_only is False


def test_auditor_read_only_violation_is_none_for_valid_launches() -> None:
    # The stripped role specs the launcher actually sends (no mcp_profile) and
    # a read-only trio profile both validate.
    stripped = _role_configs("claude_code", "m", None, None)
    assert auditor_read_only_violation(
        {"auditor": dict(stripped["auditor"])}, agent="claude_code", model="m"
    ) is None
    read_only = _role_configs("claude_code", "m", "audit", None)
    assert auditor_read_only_violation(
        {"auditor": dict(read_only["auditor"])}, agent="claude_code", model="m"
    ) is None