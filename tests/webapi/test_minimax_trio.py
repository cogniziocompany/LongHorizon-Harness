"""The optional ``minimax`` trio: dev work on the MiniMax M Plan subscription.

An additional trio, defined per node like ``glm``. It never replaces
another trio: ``kimi``, ``qwen`` and ``glm`` keep their own models.
"""

from __future__ import annotations

from pathlib import Path

from lh_harness.config import load_run_defaults
from lh_harness.queue import _normalize_request, default_queue_config, queue_config_from_config

MINIMAX_TRIO = {"agent": "claude_code", "model": "minimax-m3:minimax", "mcp_profile": "ops"}


def test_minimax_is_not_a_default_trio_but_has_a_default_cap() -> None:
    config = default_queue_config()
    assert "minimax" not in config["trios"]
    assert config["capacity"]["minimax_max"] == 2


def test_enqueue_accepts_zai() -> None:
    params = _normalize_request(
        {"name": "n", "task": "t", "workspace": "w", "trio": "minimax", "requested_by": "ci"}
    )
    assert params["trio"] == "minimax"


def test_queue_config_carries_minimax_trio_and_cap() -> None:
    config = queue_config_from_config(
        {"queue": {"trios": {"minimax": dict(MINIMAX_TRIO)}, "capacity": {"minimax_max": 2}}}
    )
    assert config["trios"]["minimax"]["model"] == "minimax-m3:minimax"
    assert config["capacity"]["minimax_max"] == 2
    assert {"kimi", "qwen"} <= set(config["trios"])


def test_project_config_accepts_minimax_trio(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join([
            "[queue.trios.minimax]",
            'agent = "claude_code"',
            'model = "minimax-m3:minimax"',
            'mcp_profile = "ops"',
            "[queue.capacity]",
            "minimax_max = 1",
        ]),
        encoding="utf-8",
    )
    queue = load_run_defaults(config)["queue"]
    assert queue["trios"]["minimax"] == MINIMAX_TRIO
    assert queue["capacity"]["minimax_max"] == 1
    assert set(queue["trios"]) == {"kimi", "qwen", "minimax"}


def test_minimax_sits_beside_glm_without_replacing_it(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join([
            "[queue.trios.glm]",
            'agent = "claude_code"',
            'model = "glm-5.3:cloud"',
            'mcp_profile = "ops"',
            "[queue.trios.minimax]",
            'agent = "claude_code"',
            'model = "minimax-m3:minimax"',
            'mcp_profile = "ops"',
        ]),
        encoding="utf-8",
    )
    queue = load_run_defaults(config)["queue"]
    assert queue["trios"]["glm"]["model"] == "glm-5.3:cloud"
    assert queue["trios"]["minimax"]["model"] == "minimax-m3:minimax"
    assert set(queue["trios"]) == {"kimi", "qwen", "glm", "minimax"}
