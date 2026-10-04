"""scripts/deploy/ct110/verify_meta.py: bounded /api/meta wait plus the build-commit check.

Deploy run 37162090599 failed its verify because the one-shot curl hit
/api/meta 1.3 s after ``systemctl restart`` (curl exit 7: nothing listening
yet).  The verify must wait for the port, then prove the running commit.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deploy" / "ct110" / "verify_meta.py"
SHA = "b6f51f95fe8758176162b7d7c6750527b5e4928f"


def _load():
    spec = importlib.util.spec_from_file_location("verify_meta_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _meta(commit: str | None, service: str = "lh-harness") -> dict[str, Any]:
    return {"service": service, "build": {"version": "0.1.7", "commit": commit}}


def _verify(answers: list[tuple[int, dict[str, Any] | None, str]], **kwargs: Any) -> tuple[int, _Clock, int]:
    module = _load()
    clock = _Clock()
    calls = {"n": 0}

    def fake_get(url: str, token: str, timeout: float):
        index = min(calls["n"], len(answers) - 1)
        calls["n"] += 1
        return answers[index]

    options = {"expect_commit": SHA, "allow_unknown_commit": False,
               "timeout_seconds": 90, "interval_seconds": 5, "request_timeout": 10}
    options.update(kwargs)
    code = module.verify("http://ct110.test", "test-token", get_meta=fake_get,
                         sleep=clock.sleep, clock=clock.time, **options)
    return code, clock, calls["n"]


REFUSED = (0, None, "no answer ([Errno 111] Connection refused)")


def test_waits_for_the_restarted_service_then_verifies_the_commit() -> None:
    code, clock, calls = _verify([REFUSED, REFUSED, (503, None, "HTTP 503"), (200, _meta(SHA), "")])
    assert code == 0
    assert calls == 4
    assert clock.sleeps == [5, 5, 5]


def test_gives_up_after_the_bounded_wait() -> None:
    code, clock, calls = _verify([REFUSED], timeout_seconds=90, interval_seconds=5)
    assert code == 1
    assert clock.now == pytest.approx(90)
    assert calls == 19  # t=0,5,...,90


def test_a_different_running_commit_fails_at_once() -> None:
    code, _, calls = _verify([(200, _meta("0" * 40), "")])
    assert code == 1
    assert calls == 1


def test_a_build_without_commit_fails_unless_explicitly_allowed() -> None:
    assert _verify([(200, _meta(None), "")])[0] == 1
    assert _verify([(200, _meta(None), "")], allow_unknown_commit=True)[0] == 3


def test_wrong_service_fails() -> None:
    assert _verify([(200, _meta(SHA, service="something-else"), "")])[0] == 1


def test_without_an_expected_commit_a_200_is_enough() -> None:
    assert _verify([REFUSED, (200, _meta(None), "")], expect_commit=None)[0] == 0


def test_main_requires_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CT110_API_TOKEN", raising=False)
    assert _load().main(["--url", "http://ct110.test"]) == 1


def test_main_reports_commit_to_github_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load()
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("CT110_API_TOKEN", "test-token")
    monkeypatch.setattr(module, "_get_meta", lambda url, token, timeout: (200, _meta(SHA), ""))
    assert module.main(["--url", "http://ct110.test", "--expect-commit", SHA]) == 0
    assert f"commit={SHA}" in out.read_text(encoding="utf-8")
