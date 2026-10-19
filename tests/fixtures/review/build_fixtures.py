#!/usr/bin/env python3
"""Build the hermetic PR fixtures used by tests/test_review_fixtures.py.

Each fixture is a tiny self-contained git repo tarball: ``main`` plus a PR
branch whose head is also exported as ``refs/pull/N/head`` so the review
workspace fetcher (``refs/pull/N/head``) works without any network access.
Regenerate with:  python3 tests/fixtures/review/build_fixtures.py
"""

from __future__ import annotations

import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

FIXTURES = ("pr-pass", "pr-fail")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _build_pr_pass(repo: Path) -> None:
    """A clean, reviewable change: the PR adds one documentation file."""

    (repo / "README.md").write_text("# fixture-pr-pass\n\nA tiny repo used to review a clean PR.\n")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "checkout", "-q", "-b", "pr-1")
    (repo / "docs").mkdir()
    (repo / "docs" / "note.md").write_text("# Note\n\nDocuments the add helper.\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "docs: describe add")
    _git(repo, "update-ref", "refs/pull/1/head", "HEAD")
    _git(repo, "checkout", "-q", "main")


def _build_pr_fail(repo: Path) -> None:
    """A PR that breaks a covered behaviour; the gate report names the test."""

    (repo / "README.md").write_text("# fixture-pr-fail\n\nA tiny repo whose PR breaks a test.\n")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text(
        "from src.app import add\n\n\ndef test_add_returns_sum():\n    assert add(2, 3) == 5\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "checkout", "-q", "-b", "pr-2")
    # The PR swaps the operator, breaking test_add_returns_sum.
    (repo / "src" / "app.py").write_text("def add(a: int, b: int) -> int:\n    return a - b\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "change add")
    _git(repo, "update-ref", "refs/pull/2/head", "HEAD")
    _git(repo, "checkout", "-q", "main")


def main() -> int:
    here = Path(__file__).resolve().parent
    for name in FIXTURES:
        with tempfile.TemporaryDirectory(prefix="review-fixture-") as tmp:
            repo = Path(tmp) / name
            repo.mkdir()
            _git(repo, "init", "-q", "-b", "main")
            _git(repo, "config", "user.email", "fixtures@test")
            _git(repo, "config", "user.name", "Fixtures")
            {"pr-pass": _build_pr_pass, "pr-fail": _build_pr_fail}[name](repo)
            tarball = here / f"{name}.tar.gz"
            with tarfile.open(tarball, "w:gz") as archive:
                archive.add(str(repo), arcname=name)
            print(f"wrote {tarball}")
    return 0


if __name__ == "__main__":
    sys.exit(main())