"""GET /api/meta reports the build identity (version + commit) the deploy verifies.

Every CI build of lh-harness is version 0.1.7, so the deploy proves the
restarted service runs the target commit through ``meta.build.commit``
(src/lh_harness/build_info.py, stamped into the wheel by the build job).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import lh_harness
from lh_harness import build_info as build_info_module
from lh_harness.webapi.protocol import build_meta
from lh_harness.webapi.server import create_app


@pytest.fixture(autouse=True)
def _fresh_cache():
    build_info_module.build_info.cache_clear()
    yield
    build_info_module.build_info.cache_clear()


def test_build_meta_omits_build_unless_given() -> None:
    assert "build" not in build_meta(endpoint="http://x")
    meta = build_meta(endpoint="http://x", build={"version": "0.1.7", "commit": "abc"})
    assert meta["build"] == {"version": "0.1.7", "commit": "abc"}


def test_build_info_reads_the_stamped_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_info_module, "_read_commit", lambda: "b6f51f95fe8758176162b7d7c6750527b5e4928f")
    info = build_info_module.build_info()
    assert info == {"version": lh_harness.__version__, "commit": "b6f51f95fe8758176162b7d7c6750527b5e4928f"}


def test_read_commit_parses_the_packaged_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / build_info_module.BUILD_INFO_FILE).write_text(json.dumps({"commit": " abc123 "}), encoding="utf-8")
    monkeypatch.setattr(build_info_module.resources, "files", lambda package: tmp_path)
    assert build_info_module._read_commit() == "abc123"


@pytest.mark.parametrize("content", [None, "not json", json.dumps({"commit": ""}), json.dumps(["x"])])
def test_missing_or_bad_build_file_reports_no_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str | None
) -> None:
    if content is not None:
        (tmp_path / build_info_module.BUILD_INFO_FILE).write_text(content, encoding="utf-8")
    monkeypatch.setattr(build_info_module.resources, "files", lambda package: tmp_path)
    assert build_info_module._read_commit() is None


def test_api_meta_carries_the_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(build_info_module, "_read_commit", lambda: "deadbeef")
    client = TestClient(create_app(runs_root=tmp_path / "runs"))
    meta = client.get("/api/meta").json()
    assert meta["build"] == {"version": lh_harness.__version__, "commit": "deadbeef"}


def test_api_meta_build_is_read_once_at_startup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    commits = iter(["first", "second"])
    monkeypatch.setattr(build_info_module, "_read_commit", lambda: next(commits))
    client = TestClient(create_app(runs_root=tmp_path / "runs"))
    assert client.get("/api/meta").json()["build"]["commit"] == "first"
    assert client.get("/api/meta").json()["build"]["commit"] == "first"
