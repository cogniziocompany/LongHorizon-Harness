"""The optional ``glm`` trio: dev work on Ollama Cloud glm-5.3.

Like ``orfree`` it exists only when a node's config defines ``[queue.trios.glm]``;
it keeps glm-5.3 work off the ``kimi`` trio, which on CT110 is the synthetic provider.
"""

from __future__ import annotations

from pathlib import Path

from lh_harness.config import load_run_defaults
from lh_harness.queue import _normalize_request, default_queue_config, queue_config_from_config

GLM_TRIO = {"agent": "claude_code", "model": "glm-5.3:cloud", "mcp_profile": "ops"}


def test_glm_is_not_a_default_trio_but_has_a_default_cap() -> None:
    config = default_queue_config()
    assert "glm" not in config["trios"]
    assert config["capacity"]["glm_max"] == 1


def test_enqueue_accepts_glm() -> None:
    params = _normalize_request(
        {"name": "n", "task": "t", "workspace": "w", "trio": "glm", "requested_by": "ci"}
    )
    assert params["trio"] == "glm"


def test_queue_config_carries_glm_trio_and_cap() -> None:
    config = queue_config_from_config(
        {"queue": {"trios": {"glm": dict(GLM_TRIO)}, "capacity": {"glm_max": 2}}}
    )
    assert config["trios"]["glm"]["model"] == "glm-5.3:cloud"
    assert config["capacity"]["glm_max"] == 2
    assert {"kimi", "qwen"} <= set(config["trios"])


def test_project_config_accepts_glm_trio(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join([
            "[queue.trios.glm]",
            'agent = "claude_code"',
            'model = "glm-5.3:cloud"',
            'mcp_profile = "ops"',
            "[queue.capacity]",
            "glm_max = 1",
        ]),
        encoding="utf-8",
    )
    queue = load_run_defaults(config)["queue"]
    assert queue["trios"]["glm"] == GLM_TRIO
    assert queue["capacity"]["glm_max"] == 1
    assert set(queue["trios"]) == {"kimi", "qwen", "glm"}
