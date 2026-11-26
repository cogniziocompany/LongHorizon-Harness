"""Run the harness's own read-only PR verifier -- by the INSTALLED path.

Why this module exists
----------------------

The auditor's shell starts in the TASK workspace, and its environment carries
neither ``PYTHONPATH`` nor ``VIRTUAL_ENV``.  So every "obvious" way to reach
the verifier failed for the auditor in a real run:

*   ``scripts/auditor_verify_pr.py`` is relative to the current directory, so
    from a task workspace it does not exist -- and on a wheel install it
    exists nowhere on the host at all, because the wheel ships only
    ``src/lh_harness``;
*   ``lh-harness verify-pr`` is a console script that is not on the auditor's
    PATH;
*   ``python -m lh_harness verify-pr`` resolves ``lh_harness`` through the
    interpreter's own ``sys.path`` -- which is whatever copy of the package
    that interpreter imports (a stale editable install, an older wheel), NOT
    necessarily the harness that is building the auditor's prompt.

This module is the entry point that is always right.  It lives INSIDE the
package, so its own location IS the installed location of the harness that
shipped it.  Invoked by absolute path, it puts its own package root at the
front of ``sys.path`` before importing the CLI, so the verifier that runs is
the one belonging to this harness -- in a checkout (``src/``) and in a wheel
install (``site-packages/``) alike -- and nothing under the task workspace,
and nothing on the auditor's cwd, can shadow it.

The command that names it is rendered by
:func:`lh_harness.auditor_verify.auditor_verifier_command` and stated
verbatim in the auditor's prompt.

Usage (from ANY directory)::

    <python> /.../lh_harness/verify_pr.py verify-pr --repo owner/name --number 181 \\
        [--reviewed-sha <sha>] [--json]

The credential is read from the environment by the verifier itself
(:data:`lh_harness.auditor_verify.AUDITOR_GITHUB_TOKEN_ENV`); this entry
point takes no token on its command line and prints none.

Exit code 0 means CONFIRMED.  A refutation, and an unverifiable read, both
exit non-zero: an auditor that could not reach the evidence has no basis to
call the claim true.
"""

from __future__ import annotations

import sys


def _pin_own_package_root() -> str:
    """Make THIS harness the one that ``import lh_harness`` resolves to.

    Two adjustments to ``sys.path``, both derived from this file's own
    location and from nothing else:

    *   the directory CONTAINING the ``lh_harness`` package -- ``src`` in a
        checkout, ``site-packages`` in a wheel install -- goes to the front,
        so the harness that shipped this module wins over any copy the
        interpreter would otherwise import; and
    *   this module's OWN directory is removed.  When Python runs a script it
        puts the script's directory first on ``sys.path``, and this script
        lives inside the package -- whose ``types.py`` would then shadow the
        standard library's ``types`` and break the interpreter's own imports
        before the verifier is ever reached.  That directory is never the
        package root, so removing it loses nothing.

    Only ``sys`` and string operations are used here, deliberately: this must
    run before ANY other import, because that import may itself pull in the
    standard library module being shadowed.
    """

    own_dir = __file__.rsplit("/", 1)[0] if "/" in __file__ else "."
    package_root = own_dir.rsplit("/", 1)[0] if "/" in own_dir else "."

    sys.path[:] = [entry for entry in sys.path if entry not in ("", ".", own_dir)]
    if package_root in sys.path:
        sys.path.remove(package_root)
    sys.path.insert(0, package_root)
    return package_root


def main(argv: list[str] | None = None) -> int:
    """Delegate to the harness's ``verify-pr`` subcommand.

    There is exactly ONE implementation of the verifier and exactly ONE
    verdict vocabulary (``confirmed`` / ``refuted`` / ``unverifiable``); this
    module only makes it reachable by its installed path.
    """

    # Importing this package may write .pyc caches into its own tree; the
    # auditor's window is read-only, so bytecode writing is off first.
    sys.dont_write_bytecode = True
    _pin_own_package_root()

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "verify-pr":
        args = ["verify-pr", *args]

    from lh_harness.cli import main as cli_main

    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
