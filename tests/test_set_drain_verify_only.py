"""set_drain.py: the post-restart "drain survived" step is a read-only check.

The deploy workflow runs ``set_drain.py --verify-state enabled`` with neither
``--enable`` nor ``--disable``.  The script used to demand one of them, so that
step exited 2 on every deploy and rolled a healthy deploy back.  These tests
pin the read-only mode: no write is made, and the exit code follows /api/meta.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy" / "ct110" / "set_drain.py"


@pytest.fixture
def drain():
    spec = importlib.util.spec_from_file_location("_set_drain_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(drain, monkeypatch, argv, meta_drain):
    posts: list[dict] = []
    monkeypatch.setenv("CT110_API_TOKEN", "t")
    monkeypatch.setattr(drain, "_post", lambda url, token, payload, timeout=15: posts.append(payload) or {"drain": payload})
    monkeypatch.setattr(drain, "_meta", lambda url, token, timeout=15: {"drain": meta_drain})
    monkeypatch.setattr("sys.argv", ["set_drain.py", "--url", "http://x", *argv])
    return drain.main(), posts


def test_verify_only_makes_no_write_and_passes_when_state_matches(drain, monkeypatch) -> None:
    code, posts = _run(drain, monkeypatch, ["--verify-state", "enabled"], {"enabled": True})
    assert code == 0
    assert posts == []


def test_verify_only_fails_when_state_differs(drain, monkeypatch) -> None:
    code, posts = _run(
        drain, monkeypatch, ["--verify-state", "enabled", "--verify-attempts", "1"], {"enabled": False}
    )
    assert code == 1
    assert posts == []


def test_enable_still_writes_then_verifies(drain, monkeypatch) -> None:
    code, posts = _run(drain, monkeypatch, ["--enable", "--verify-state", "enabled"], {"enabled": True})
    assert code == 0
    assert posts and posts[0]["enabled"] is True


def test_disable_still_writes(drain, monkeypatch) -> None:
    code, posts = _run(drain, monkeypatch, ["--disable", "--verify-state", "disabled"], {"enabled": False})
    assert code == 0
    assert posts == [{"enabled": False}]


def test_no_action_and_no_verify_is_a_usage_error(drain, monkeypatch) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _run(drain, monkeypatch, [], {"enabled": False})
    assert excinfo.value.code == 2
