"""Per-role model/agent overrides on queue trios and the optional ``zaiq`` mixed trio.

Paxton 2026-10-11: design tasks run their manager and auditor on z.ai and the
executor on the local qwen lane. A trio carried one model for all three roles,
so this adds ``<role>_agent`` / ``<role>_model`` keys and the ``zaiq`` trio name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lh_harness.config import ProjectConfigError, load_run_defaults
from lh_harness.launcher import _role_configs, _trio_role_overrides
from lh_harness.queue import _normalize_request, default_queue_config, queue_config_from_config

ZAIQ = {"agent": "claude_code", "model": "glm-5.3:zai", "executor_model": "qwen3.8", "mcp_profile": "ops"}


def test_zaiq_is_optional_with_a_default_cap() -> None:
    config = default_queue_config()
    assert "zaiq" not in config["trios"]
    assert config["capacity"]["zaiq_max"] == 1


def test_enqueue_accepts_zaiq() -> None:
    params = _normalize_request(
        {"name": "n", "task": "t", "workspace": "w", "trio": "zaiq", "requested_by": "ci"}
    )
    assert params["trio"] == "zaiq"


def test_queue_config_keeps_per_role_keys() -> None:
    config = queue_config_from_config({"queue": {"trios": {"zaiq": dict(ZAIQ)}, "capacity": {"zaiq_max": 2}}})
    trio = config["trios"]["zaiq"]
    assert trio["model"] == "glm-5.3:zai"
    assert trio["executor_model"] == "qwen3.8"
    assert config["capacity"]["zaiq_max"] == 2


def test_project_config_accepts_zaiq_and_rejects_unknown_role_keys(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join([
            "[queue.trios.zaiq]",
            'agent = "claude_code"',
            'model = "glm-5.3:zai"',
            'executor_model = "qwen3.8"',
            'mcp_profile = "ops"',
            "[queue.capacity]",
            "zaiq_max = 1",
        ]),
        encoding="utf-8",
    )
    queue = load_run_defaults(config)["queue"]
    assert queue["trios"]["zaiq"]["executor_model"] == "qwen3.8"
    assert queue["trios"]["zaiq"]["model"] == "glm-5.3:zai"
    assert set(queue["trios"]) == {"kimi", "qwen", "zaiq"}

    bad = tmp_path / "bad.toml"
    bad.write_text('[queue.trios.zaiq]\nagent = "claude_code"\nreviewer_model = "x"\n', encoding="utf-8")
    with pytest.raises(ProjectConfigError):
        load_run_defaults(bad)


def test_role_configs_apply_overrides_per_role_only() -> None:
    overrides = _trio_role_overrides(dict(ZAIQ))
    assert overrides == {"executor": {"model": "qwen3.8"}}
    configs = _role_configs("claude_code", "glm-5.3:zai", role_overrides=overrides)
    assert configs["manager"] == {"agent": "claude_code", "model": "glm-5.3:zai"}
    assert configs["auditor"] == {"agent": "claude_code", "model": "glm-5.3:zai"}
    assert configs["executor"] == {"agent": "claude_code", "model": "qwen3.8"}


def test_role_configs_without_overrides_are_unchanged() -> None:
    assert _trio_role_overrides({"agent": "codex", "model": "qwen3.8"}) == {}
    configs = _role_configs("codex", "qwen3.8")
    assert all(spec == {"agent": "codex", "model": "qwen3.8"} for spec in configs.values())


def test_role_agent_override_and_blank_values_ignored() -> None:
    overrides = _trio_role_overrides({"agent": "claude_code", "model": "m", "auditor_agent": "codex", "manager_model": "  "})
    assert overrides == {"auditor": {"agent": "codex"}}
    configs = _role_configs("claude_code", "m", role_overrides=overrides)
    assert configs["auditor"] == {"agent": "codex", "model": "m"}
    assert configs["manager"] == {"agent": "claude_code", "model": "m"}
