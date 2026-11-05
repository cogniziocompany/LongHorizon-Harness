#!/usr/bin/env python3
"""Run the harness's own read-only PR verifier -- the auditor's own reach.

Why this script exists
-----------------------

The harness is a validation chain: each step validates the last in its
process to achieve its tasks.  The auditor is the last independent
verification step, so it must be able to CONFIRM or REFUTE an executor's
claim about the outside world -- not merely restate it.

For that to be real, the auditor needs an invocation that actually WORKS in
the agent environment.  The obvious ones do not:

*   ``lh-harness verify-pr`` -- the console script is only on PATH inside the
    project virtualenv, and the auditor's shell does not have that venv on
    PATH; where it does resolve, it resolves to whatever ``lh_harness`` is
    *installed* in site-packages, which for a checkout that has not been
    reinstalled since a change landed is an OLDER copy with no ``verify-pr``
    subcommand at all.
*   ``python -m lh_harness ...`` -- the installed package shadows the
    checkout, so this runs the INSTALLED code, not the code in this repo.

This script fixes that: it resolves the checkout's own ``src`` directory and
puts it at the FRONT of ``sys.path`` BEFORE importing the verifier, so it
always executes the code in this working tree.

Usage (from the repository root):

    python3 scripts/auditor_verify_pr.py --repo owner/name --number 181 \
        [--reviewed-sha <sha>] [--json]

Exit code 0 means CONFIRMED.  A refutation, and an unverifiable read (the
verifier could not reach the evidence), both exit non-zero: an auditor that
could not reach the evidence has no basis to call the claim true.

Environment (name only; the value never appears in output):
    LH_HARNESS_AUDITOR_GITHUB_TOKEN   the auditor's OWN fine-grained
                                      read-only token.  The executor's
                                      GH_TOKEN / GITHUB_TOKEN are refused.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _checkout_src(start: Path | None = None) -> Path | None:
    """Find the ``src`` directory of the harness checkout containing *start*.

    The checkout root is identified by the package itself
    (``src/lh_harness/__init__.py``), not by a marker file, so this works in
    a plain clone, a worktree, and a nested task directory alike.
    """

    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        src = candidate / "src"
        if (src / "lh_harness" / "__init__.py").is_file():
            return src
    return None


def _activate_checkout() -> Path:
    """Put the checkout's ``src`` at the FRONT of ``sys.path``.

    Returns the resolved ``src`` directory.  Raises ``SystemExit`` with a
    clear message when no checkout is found, because running the INSTALLED
    package would silently verify against the wrong code.
    """

    src = _checkout_src()
    if src is None:
        sys.stderr.write(
            "auditor verifier: no harness checkout found from "
            f"{Path.cwd()}. Run this script from the repository root.\n"
        )
        raise SystemExit(2)

    entry = str(src)
    if entry in sys.path:
        sys.path.remove(entry)
    sys.path.insert(0, entry)
    return src


def main(argv: list[str] | None = None) -> int:
    """Delegate to the harness's ``verify-pr`` subcommand.

    There is exactly ONE implementation of the verifier and exactly ONE
    verdict vocabulary (``confirmed`` / ``refuted`` / ``unverifiable``); this
    script only makes it reachable.
    """

    _activate_checkout()

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "verify-pr":
        args = ["verify-pr", *args]

    from lh_harness.cli import main as cli_main

    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
