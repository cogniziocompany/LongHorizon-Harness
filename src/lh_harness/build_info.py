"""Build identity of the installed lh-harness: which commit the wheel was built from.

Every CI build carries the same pyproject version (0.1.7 for many commits), so
the version string cannot tell two deploys apart.  The deploy workflow's build
job writes ``_build_info.json`` (``{"commit": "<full sha>"}``) into the package
before ``uv build``; pyproject.toml packs it as a build artifact (the file is
git-ignored, like the Web bundle).  ``GET /api/meta`` reports it under
``build`` so the deploy can prove the RESTARTED service runs the target commit.

A wheel built without that step (a dev checkout, the PyPI release) has no
file and reports ``commit: None`` -- never a guessed value.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any

BUILD_INFO_FILE = "_build_info.json"


def _read_commit() -> str | None:
    try:
        text = (resources.files("lh_harness") / BUILD_INFO_FILE).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, ModuleNotFoundError):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    commit = data.get("commit") if isinstance(data, dict) else None
    commit = str(commit).strip() if commit else ""
    return commit or None


@lru_cache(maxsize=1)
def build_info() -> dict[str, Any]:
    """Return ``{"version": ..., "commit": <sha or None>}``, read once per process.

    Cached on first call so the value describes the code this process loaded,
    not a wheel installed underneath it later.
    """

    from . import __version__

    return {"version": __version__, "commit": _read_commit()}


if __name__ == "__main__":  # used by the deploy scripts: prints the commit or ""
    print(build_info()["commit"] or "")
