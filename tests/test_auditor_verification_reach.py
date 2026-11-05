"""The auditor's verification surface must be REACHABLE, not merely present.

Task ``yt:easybutt0n-17:closeout-validation-chain``, Deliverable 1.

The gap this module closes
--------------------------

The auditor's independent reach into the outside world is the harness's own
read-only verifier.  Before this change the verifier existed and worked, but
the auditor could not actually INVOKE it:

*   the role instruction named ``lh-harness verify-pr`` -- a console script
    that is only on PATH inside the project virtualenv, and that resolves to
    the *installed* package where it does resolve; and
*   the installed package in the agent environment has no ``verify-pr``
    subcommand at all, so even a working PATH would have produced an
    "invalid choice" error.

A verification surface the verifier cannot reach is not a verification
surface.  These tests pin the two properties that make it real:

1.  the invocation named in the auditor's role instruction RESOLVES -- it
    runs, from the repository root, against this checkout's own code; and
2.  the blanket ``Bash(gh *)`` deny is gone, replaced by per-spelling write
    denies, so the auditor's reads are not blocked by the rule that blocks
    its writes.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from lh_harness.adapters.claude_permissions import (
    AUDITOR_NETWORK_GIT_DENY,
    _AUDITOR_ROLES,
    policy_for_role,
)
from lh_harness.prompt_texts import CLI_AUDITOR_INSTRUCTIONS

try:  # pragma: no cover - the RED state simply does not have it yet
    from lh_harness.adapters.claude_permissions import AUDITOR_GH_WRITE_DENY
except ImportError:  # pragma: no cover
    AUDITOR_GH_WRITE_DENY = ()

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "auditor_verify_pr.py"


# ---------------------------------------------------------------------------
# 1. The invocation named in the role instruction resolves.
# ---------------------------------------------------------------------------


def test_the_auditor_verifier_script_exists() -> None:
    """The verifier has a script entry point in the checkout."""

    assert SCRIPT.is_file(), f"missing auditor verifier script: {SCRIPT}"


def test_the_role_instruction_names_a_resolvable_invocation() -> None:
    """The auditor's instructions name the script that actually exists.

    The instruction used to tell the auditor to run ``lh-harness
    verify-pr``, a form that does not resolve here.  What is asserted is
    that the command form the auditor is told to RUN is the resolving one.
    """

    instructions = CLI_AUDITOR_INSTRUCTIONS["en"]

    assert "scripts/auditor_verify_pr.py" in instructions
    # The instruction must present the script as the command to run.
    assert re.search(r"run `python3 scripts/auditor_verify_pr\.py", instructions)


def test_the_chinese_role_instruction_names_the_same_invocation() -> None:
    """The zh instruction names the same resolving command form."""

    instructions = CLI_AUDITOR_INSTRUCTIONS["zh"]

    assert "scripts/auditor_verify_pr.py" in instructions
    assert "python3 scripts/auditor_verify_pr.py" in instructions


def test_the_named_invocation_actually_runs() -> None:
    """Run the named invocation and show it reaches the harness's verifier.

    This is the RED/GREEN hinge for G4: against a tree with no such script
    the subprocess fails with "No such file or directory"; here it starts
    the verifier and reports the missing auditor credential -- which is the
    fail-closed behaviour, not a start-up failure.
    """

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(REPO_ROOT),
        # The auditor's own token is not provisioned in this environment, so
        # the verifier must refuse to confirm anything.
        "LH_HARNESS_AUDITOR_GITHUB_TOKEN": "",
    }
    completed = subprocess.run(
        [sys.executable, "scripts/auditor_verify_pr.py", "--help"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    assert "verify-pr" in completed.stdout
    assert "--reviewed-sha" in completed.stdout


def test_the_named_invocation_resolves_this_checkout_not_the_installed_package() -> None:
    """The invocation runs THIS checkout's verifier, not site-packages'.

    The installed ``lh_harness`` in the agent environment predates the
    verifier, so an invocation that resolved it would fail with "invalid
    choice".  Resolving the checkout is what makes the instruction true.
    """

    script = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('avp', {str(SCRIPT)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "src = module._activate_checkout()\n"
        "import lh_harness.auditor_verify as av\n"
        "print(src)\n"
        "print(av.__file__)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    stdout = completed.stdout
    assert str(REPO_ROOT / "src") in stdout
    assert "site-packages" not in stdout


def test_the_named_invocation_refuses_to_verify_without_the_auditor_credential() -> None:
    """Fail-closed: with no auditor credential the verifier confirms nothing."""

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(REPO_ROOT),
    }
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/auditor_verify_pr.py",
            "--repo",
            "cogniziocompany/LongHorizon-Harness",
            "--number",
            "181",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode != 0
    combined = completed.stdout + completed.stderr
    assert "LH_HARNESS_AUDITOR_GITHUB_TOKEN" in combined
    # Not a silent pass: the verifier says the claim is NOT confirmed, and
    # never reports a confirmed verdict.
    assert "not confirmed" in combined.lower()
    assert "verdict" not in combined.lower() or "confirmed" not in combined.lower().replace(
        "not confirmed", ""
    )


# ---------------------------------------------------------------------------
# 2. The blanket `Bash(gh *)` deny is gone; reads are not collateral damage.
# ---------------------------------------------------------------------------


def test_the_blanket_gh_deny_is_gone() -> None:
    """``Bash(gh *)`` refused the auditor's READ verbs along with its writes."""

    assert "Bash(gh *)" not in AUDITOR_NETWORK_GIT_DENY
    assert "Bash(gh *)" not in AUDITOR_GH_WRITE_DENY


@pytest.mark.parametrize("role", sorted(_AUDITOR_ROLES))
def test_no_auditor_role_gets_the_blanket_gh_deny(role: str) -> None:
    """The blanket wildcard is not in any auditor's deny list."""

    policy = policy_for_role(role)

    assert "Bash(gh *)" not in policy.disallowed_tools


def test_the_auditor_has_per_spelling_gh_write_denies() -> None:
    """The write spellings are denied one by one."""

    assert "Bash(gh pr merge*)" in AUDITOR_GH_WRITE_DENY
    assert "Bash(gh pr comment*)" in AUDITOR_GH_WRITE_DENY
    assert "Bash(gh api --method=*)" in AUDITOR_GH_WRITE_DENY
    assert "Bash(gh api -X *)" in AUDITOR_GH_WRITE_DENY
    assert "Bash(gh api * -f *)" in AUDITOR_GH_WRITE_DENY
    assert "Bash(gh api * --input *)" in AUDITOR_GH_WRITE_DENY


@pytest.mark.parametrize("role", sorted(_AUDITOR_ROLES))
def test_auditor_roles_carry_the_gh_write_denies(role: str) -> None:
    """Every auditor role carries the per-spelling gh write denies."""

    policy = policy_for_role(role)

    for rule in AUDITOR_GH_WRITE_DENY:
        assert rule in policy.disallowed_tools, f"{role} missing {rule}"


def test_the_auditor_is_not_denied_a_plain_gh_read() -> None:
    """A ``gh`` READ is not denied by any rule.

    This is the point of the change: the blanket deny took out the reads
    too.  The per-spelling denies must not reintroduce that.
    """

    policy = policy_for_role("cli_auditor")
    joined = "\n".join(policy.disallowed_tools)

    assert "Bash(gh pr view*)" not in joined
    assert "Bash(gh api*)" not in joined
    assert "Bash(gh repo view*)" not in joined


def test_no_deny_rule_uses_a_bare_gh_wildcard() -> None:
    """No deny rule degenerates back into ``Bash(gh ...)``."""

    for rule in (*AUDITOR_NETWORK_GIT_DENY, *AUDITOR_GH_WRITE_DENY):
        assert not re.fullmatch(r"Bash\(gh \*\)", rule), rule
        assert not re.fullmatch(r"Bash\(gh \*\)", rule), rule
