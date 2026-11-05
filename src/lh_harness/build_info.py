"""Build identity of the installed lh-harness: which commit the wheel was built from.

Every CI build carries the same pyproject version (0.1.7 for many commits), so
the version string cannot tell two deploys apart.  The deploy workflow's build
job writes ``_build_info.json`` (``{"commit": "<full sha>"}``) into the package
before ``uv build``; pyproject.toml packs it as a build artifact (the file is
git-ignored, like the Web bundle).  ``GET /api/meta`` reports it under
``build`` so the deploy can prove the RESTARTED service runs the target commit.

A wheel built without that step (a dev checkout, the PyPI release) has no
file and reports ``commit: None`` -- never a guessed value.

The node deploy pipeline (``scripts/deploy-node.sh``) installs straight from a
git ref rather than from a CI-built wheel, so there is no stamped
``_build_info.json`` to carry the commit.  That script therefore writes a
DEPLOY RECEIPT to ``~/.lh-harness/deployed.json`` (``{"commit": ..., "version":
..., "at": ..., "by": ..., "previous": ...}``) and this module prefers the
receipt's ``commit`` when it is present.  The receipt is what makes the
deployed commit readable WITHOUT a shell: ``GET /api/meta`` reports it under
``build.commit``, so an operator (or Overseer1) can prove which commit a node
runs from the API alone.

The receipt is read fresh on every call (not cached with the package build
info) so a receipt written by a deploy is visible to the NEXT process, and a
stale receipt never outlives the package value it was written next to.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

BUILD_INFO_FILE = "_build_info.json"

# Name of the deploy receipt the node deploy script writes.  Only the NAME is
# fixed; the directory is the harness home (the same place the settings store
# keeps ``settings.db``), so the receipt is found the same way on every node.
DEPLOYED_RECEIPT_FILE = "deployed.json"


def _deployed_receipt_paths() -> list[Path]:
    """Candidate receipt paths, most specific first.

    ``LH_HARNESS_DEPLOYED_JSON`` overrides the location (used by the deploy
    script itself and by tests); otherwise the receipt is looked up in
    ``LH_HARNESS_HOME`` when set and in the harness home directory, which is
    where ``settings_store.DEFAULT_DB_PATH`` (``~/.lh-harness/settings.db``)
    puts it.
    """

    override = os.environ.get("LH_HARNESS_DEPLOYED_JSON", "").strip()
    if override:
        return [Path(override).expanduser()]
    home = os.environ.get("LH_HARNESS_HOME", "").strip()
    roots = [Path(home).expanduser()] if home else []
    roots.append(Path.home())
    return [root / ".lh-harness" / DEPLOYED_RECEIPT_FILE for root in roots]


def _read_deployed_commit() -> str | None:
    """Return the ``commit`` from the deploy receipt, or None when there is none.

    A missing, unreadable or malformed receipt is NOT an error: the caller
    falls back to the package build value.  A receipt with an empty commit is
    likewise ignored -- an empty receipt must never blank out a good value.
    """

    for path in _deployed_receipt_paths():
        try:
            text = path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError, OSError):
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        commit = data.get("commit") if isinstance(data, dict) else None
        commit = str(commit).strip() if commit else ""
        if commit:
            return commit
    return None


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
def package_build_info() -> dict[str, Any]:
    """Return ``{"version": ..., "commit": <sha or None>}`` from the PACKAGE.

    Cached on first call so the value describes the code this process loaded,
    not a wheel installed underneath it later.
    """

    from . import __version__

    return {"version": __version__, "commit": _read_commit()}


def build_info() -> dict[str, Any]:
    """Return ``{"version": ..., "commit": <sha or None>}`` for ``GET /api/meta``.

    The deploy receipt wins over the package value: the node deploy installs
    straight from a git ref, so the package carries no stamped commit while
    the receipt names exactly what was deployed.  With no receipt (a CI-built
    wheel, a dev checkout) the package value is reported unchanged.
    """

    info = dict(package_build_info())
    deployed = _read_deployed_commit()
    if deployed:
        info["commit"] = deployed
    return info


if __name__ == "__main__":  # used by the deploy scripts: prints the commit or ""
    print(build_info()["commit"] or "")
