"""The optional ``zai`` trio: dev work on the direct z.ai coding plan.

An additional trio, defined per node like ``orfree`` and ``glm``. It never replaces
another trio: ``kimi``, ``qwen`` and ``glm`` keep their own models.
"""

from __future__ import annotations

from pathlib import Path

from lh_harness.config import load_run_defaults
from lh_harness.queue import _normalize_request, default_queue_config, queue_config_from_config

ZAI_TRIO = {"agent": "claude_code", "model": "glm-5.3:zai", "mcp_profile": "ops"}


def test_zai_is_not_a_default_trio_but_has_a_default_cap() -> None:
    config = default_queue_config()
    assert "zai" not in config["trios"]
    assert config["capacity"]["zai_max"] == 1


def test_enqueue_accepts_zai() -> None:
    params = _normalize_request(
        {"name": "n", "task": "t", "workspace": "w", "trio": "zai", "requested_by": "ci"}
    )
    assert params["trio"] == "zai"


def test_queue_config_carries_zai_trio_and_cap() -> None:
    config = queue_config_from_config(
        {"queue": {"trios": {"zai": dict(ZAI_TRIO)}, "capacity": {"zai_max": 2}}}
    )
    assert config["trios"]["zai"]["model"] == "glm-5.3:zai"
    assert config["capacity"]["zai_max"] == 2
    assert {"kimi", "qwen"} <= set(config["trios"])


def test_project_config_accepts_zai_trio(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join([
            "[queue.trios.zai]",
            'agent = "claude_code"',
            'model = "glm-5.3:zai"',
            'mcp_profile = "ops"',
            "[queue.capacity]",
            "zai_max = 1",
        ]),
        encoding="utf-8",
    )
    queue = load_run_defaults(config)["queue"]
    assert queue["trios"]["zai"] == ZAI_TRIO
    assert queue["capacity"]["zai_max"] == 1
    assert set(queue["trios"]) == {"kimi", "qwen", "zai"}


def test_zai_sits_beside_glm_without_replacing_it(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join([
            "[queue.trios.glm]",
            'agent = "claude_code"',
            'model = "glm-5.3:cloud"',
            'mcp_profile = "ops"',
            "[queue.trios.zai]",
            'agent = "claude_code"',
            'model = "glm-5.3:zai"',
            'mcp_profile = "ops"',
        ]),
        encoding="utf-8",
    )
    queue = load_run_defaults(config)["queue"]
    assert queue["trios"]["glm"]["model"] == "glm-5.3:cloud"
    assert queue["trios"]["zai"]["model"] == "glm-5.3:zai"
    assert set(queue["trios"]) == {"kimi", "qwen", "glm", "zai"}
