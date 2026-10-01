"""scripts/deploy/ct110/set_drain.py: write modes and the read-only verify mode.

The 2026-10-01 CT110 deploy (run 36905070048) rolled back because the
"Verify drain survived the restart" step called ``--verify-state enabled``
alone and argparse demanded ``--enable``/``--disable``.  Verify-only must be
a pure GET: re-posting the flag would hide a drain lost in the restart.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy" / "ct110" / "set_drain.py"


def _load():
    spec = importlib.util.spec_from_file_location("set_drain_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run(monkeypatch: pytest.MonkeyPatch, argv: list[str], meta_enabled: bool) -> tuple[int, list[dict[str, Any]]]:
    module = _load()
    posts: list[dict[str, Any]] = []

    def fake_post(url: str, token: str, payload: dict[str, Any], timeout: float = 15) -> dict[str, Any]:
        posts.append(payload)
        return {"drain": {"enabled": payload["enabled"]}}

    def fake_meta(url: str, token: str, timeout: float = 15) -> dict[str, Any]:
        return {"drain": {"enabled": meta_enabled}}

    monkeypatch.setattr(module, "_post", fake_post)
    monkeypatch.setattr(module, "_meta", fake_meta)
    monkeypatch.setenv("CT110_API_TOKEN", "test-token")
    monkeypatch.setattr(sys, "argv", ["set_drain.py", "--url", "http://ct110.test", *argv])
    return module.main(), posts


def test_verify_only_does_not_post_and_passes_when_drained(monkeypatch: pytest.MonkeyPatch) -> None:
    code, posts = _run(monkeypatch, ["--verify-state", "enabled"], meta_enabled=True)
    assert code == 0
    assert posts == []


def test_verify_only_fails_when_the_drain_was_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    code, posts = _run(
        monkeypatch,
        ["--verify-state", "enabled", "--verify-attempts", "2", "--verify-interval-seconds", "0"],
        meta_enabled=False,
    )
    assert code == 1
    assert posts == []


def test_enable_posts_then_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    code, posts = _run(monkeypatch, ["--enable", "--reason", "deploy x", "--verify-state", "enabled"], meta_enabled=True)
    assert code == 0
    assert posts == [{"enabled": True, "reason": "deploy x"}]


def test_disable_posts_without_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    code, posts = _run(monkeypatch, ["--disable", "--verify-state", "disabled"], meta_enabled=False)
    assert code == 0
    assert posts == [{"enabled": False}]


def test_no_mode_at_all_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, [], meta_enabled=True)
    assert exc.value.code == 2


def test_enable_and_disable_together_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, ["--enable", "--disable"], meta_enabled=True)
    assert exc.value.code == 2


def test_the_deploy_workflow_verify_step_is_read_only() -> None:
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "deploy-ct110.yml").read_text()
    step = workflow.split("- name: Verify drain survived the restart", 1)[1].split("- name:", 1)[0]
    assert "--verify-state enabled" in step
    assert "--enable" not in step.replace("--verify-state enabled", "")
    assert "--disable" not in step
