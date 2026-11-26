"""The auditor's verifier command must reach the HARNESS, not the task workspace.

Task ``easybutt0n-17/auditor-verify-reach``.

The gap this module closes
--------------------------

The auditor's shell starts in the TASK workspace (the harness hands the
auditor adapter ``config.workspace_path`` as its working directory).  The
role instruction told it to run ``python3 scripts/auditor_verify_pr.py``,
a path RELATIVE to that directory -- where no ``scripts/`` directory exists.
On a wheel install the script exists nowhere on the host at all, because the
wheel ships only ``src/lh_harness``.  So auditors gated real runs with
"scripts/auditor_verify_pr.py is absent" and "cannot reach GitHub"
(runs 20261126T080458Z_7b08989e and 20261126T073938Z_f59ea00a): a delivered
PR could never be self-verified.

The harness now renders the command at prompt-build time from its OWN
interpreter and installed package (``auditor_verifier_command``) and the
auditor prompt states that exact command.  These tests pin the three
properties that make the fix real:

1.  the command resolves to the harness's install path from an ARBITRARY
    cwd -- an empty stand-in task workspace with no ``scripts/`` directory --
    under the auditor's REAL child environment (no ``PYTHONPATH``, no
    ``VIRTUAL_ENV``), and it reaches ``verify-pr`` rather than failing to
    start;
2.  it renders identically whatever the cwd is, because nothing in it is
    derived from the cwd;
3.  the token is never echoed: with a sentinel value in the auditor's token
    variable, the sentinel appears in neither the command, the rendered
    prompt, the verifier's output on its fail-closed paths, nor the
    environment mapping's repr.  The variable's NAME may appear; its VALUE
    never does.

No test here touches a real network and no test prints a credential VALUE.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from lh_harness import auditor_verify, cli
from lh_harness.auditor_env import build_auditor_subprocess_env
from lh_harness.auditor_verify import (
    AUDITOR_GITHUB_TOKEN_ENV,
    AUDITOR_VERIFY_STATE_DIR_ENV,
    SHARED_CREDENTIAL_ENV_VARS,
    AuditorCredentialError,
    auditor_verifier_command,
)
from lh_harness.prompt_texts import CLI_AUDITOR_INSTRUCTIONS
from lh_harness.role_prompts import (
    MANAGER_NEXT_CLI,
    build_role_auditor_prompt,
)

# A sentinel shaped like a fine-grained GitHub token, so that any code path
# that treats "token-shaped" specially is exercised.  It belongs to no real
# account and must never leave the child's environment.
SENTINEL_TOKEN = "github_pat_SENTINELDO_NOT_ECHO_0123456789abcdefABCDEF"

# Matches any token-shaped value in a place that must carry none.
_TOKEN_SHAPE = re.compile(r"(ghp_|gho_|github_pat_)[A-Za-z0-9_]{8,}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _arbitrary_workspace(tmp_path: Path) -> Path:
    """A stand-in TASK workspace: a real directory with no harness in it.

    It contains a ``.git`` marker and a file, so it is plausibly the thing
    the auditor was told to audit, and it deliberately has NO ``scripts/``
    directory and NO ``src/`` directory.
    """

    workspace = tmp_path / "task-workspace"
    (workspace / ".git").mkdir(parents=True, exist_ok=True)
    (workspace / "README.md").write_text("a task workspace, not a harness\n", encoding="utf-8")
    return workspace


def _hermetic_source(token: str) -> dict[str, str]:
    """``os.environ`` minus every credential-named variable, plus *token*.

    A test must never inherit a real credential from the host it runs on:
    a host may export ``GH_TOKEN`` or a real auditor token, and either would
    change which fail-closed path the verifier takes.  So every name the
    harness treats as a credential is removed first, and only the test's own
    token is put back.
    """

    source = {
        key: value
        for key, value in os.environ.items()
        if key not in SHARED_CREDENTIAL_ENV_VARS and key != AUDITOR_GITHUB_TOKEN_ENV
    }
    source[AUDITOR_GITHUB_TOKEN_ENV] = token
    return source


def _auditor_env(token: str, tmp_path: Path) -> dict[str, str]:
    """The auditor's REAL child environment, with *token* as its credential.

    Built by the production builder, not hand-rolled, so the environment the
    command runs under is the one the auditor actually gets: a minimal base
    with no ``PYTHONPATH`` and no ``VIRTUAL_ENV``.  The verification budget
    counter is redirected under ``tmp_path`` so a run never touches shared
    state.
    """

    env = build_auditor_subprocess_env(
        _hermetic_source(token),
        config_dir=tmp_path / "gh-config",
        episode_id="reach-test",
    )
    env[AUDITOR_VERIFY_STATE_DIR_ENV] = str(tmp_path / "verify-state")
    return env


def _concrete_command(command: str) -> list[str]:
    """Turn the rendered command into a runnable argv.

    The placeholders are replaced with the arguments ``verify-pr`` accepts.
    Only the shape is filled in; the credential is NEVER put on the command
    line -- the verifier must read it from the environment.
    """

    concrete = (
        command.replace("<owner/name>", "example/absent")
        .replace("<n>", "1")
        .replace("<sha>", "0" * 40)
    )
    return shlex.split(concrete)


def _run(argv: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=120)


# ---------------------------------------------------------------------------
# 1. The command resolves to the harness install path from an arbitrary cwd.
# ---------------------------------------------------------------------------


def test_the_command_is_absolute_with_no_cwd_relative_segment() -> None:
    """The command is interpreter-absolute and names the entry by absolute path."""

    argv = shlex.split(auditor_verifier_command())

    assert Path(argv[0]).is_absolute(), argv[0]
    assert Path(argv[1]).is_absolute(), argv[1]
    assert argv[1].endswith("/lh_harness/verify_pr.py"), argv[1]
    assert argv[2] == "verify-pr", argv
    # Nothing that a shell would resolve against the current directory.
    assert not any(part.startswith(("./", "../", "scripts/")) for part in argv), argv


def test_the_command_is_the_running_harness_interpreter_and_package() -> None:
    """It is built from THIS harness: its interpreter, and its own package dir.

    The entry is the ``verify_pr.py`` that sits NEXT TO the
    ``auditor_verify`` module that rendered the command -- so the verifier
    the command reaches is, by construction, the one belonging to the harness
    that built the auditor's prompt, never a different installed copy.
    """

    argv = shlex.split(auditor_verifier_command())

    # Like for like: the command names sys.executable verbatim, so a symlinked
    # interpreter (/usr/bin/python3 -> python3.12) is still the same file.
    assert Path(argv[0]) == Path(sys.executable)
    assert os.path.samefile(argv[0], sys.executable)
    assert Path(argv[1]) == Path(auditor_verify.__file__).resolve().parent / "verify_pr.py"
    assert Path(argv[1]).is_file()


def test_the_command_renders_identically_from_an_arbitrary_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing in the command is derived from the current directory.

    Rendering from two unrelated cwds -- one of them a stand-in task
    workspace with no ``scripts/`` -- must give the same string.
    """

    harness_side = auditor_verifier_command()

    workspace = _arbitrary_workspace(tmp_path)
    monkeypatch.chdir(workspace)
    from_task_workspace = auditor_verifier_command()

    assert from_task_workspace == harness_side
    assert str(workspace) not in from_task_workspace


def test_the_command_runs_from_a_workspace_with_no_scripts_dir(tmp_path: Path) -> None:
    """The RED/GREEN hinge: the command reaches verify-pr from a task workspace.

    Run from a stand-in task workspace, under the auditor's REAL child
    environment, the command must start the harness's verifier.  The token is
    deliberately absent, so the verifier's fail-closed preflight is what
    proves the verifier was reached: it reports ``pr_verifier_unavailable``
    and exits non-zero.  The OLD instruction, by contrast, cannot even start
    from here.
    """

    workspace = _arbitrary_workspace(tmp_path)
    env = _auditor_env(token="", tmp_path=tmp_path)
    env.pop(AUDITOR_GITHUB_TOKEN_ENV, None)

    completed = _run(_concrete_command(auditor_verifier_command()), workspace, env)

    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "pr_verifier_unavailable" in combined
    assert "unverifiable" in combined.lower()
    # It was the VERIFIER that answered, not the shell failing to find a file.
    assert "can't open file" not in combined
    assert "No such file or directory" not in combined
    assert "No module named" not in combined


def test_the_command_needs_neither_pythonpath_nor_a_venv(tmp_path: Path) -> None:
    """It resolves under exactly the environment the auditor is given.

    The auditor env carries neither ``PYTHONPATH`` nor ``VIRTUAL_ENV``; a
    command that depended on either would resolve only by accident.
    """

    env = _auditor_env(token="", tmp_path=tmp_path)
    env.pop(AUDITOR_GITHUB_TOKEN_ENV, None)
    assert "PYTHONPATH" not in env
    assert "VIRTUAL_ENV" not in env

    completed = _run(_concrete_command(auditor_verifier_command()) + ["--help"], _arbitrary_workspace(tmp_path), env)

    assert completed.returncode == 0, completed.stderr
    assert "verify-pr" in completed.stdout


def test_the_verifier_ships_inside_the_installed_package() -> None:
    """The entry the command names exists in a wheel install.

    A wheel ships ``src/lh_harness`` and nothing else; ``scripts/`` is not
    part of it.  So the entry module, the verifier and the CLI subcommand it
    dispatches to must all live INSIDE the package.
    """

    package_dir = Path(auditor_verify.__file__).resolve().parent
    assert package_dir.name == "lh_harness"
    for module in ("verify_pr.py", "auditor_verify.py", "cli.py", "__main__.py"):
        assert (package_dir / module).is_file(), module

    assert callable(cli.main)
    source = (package_dir / "cli.py").read_text(encoding="utf-8")
    assert '"verify-pr"' in source


def test_the_entry_is_reachable_in_a_wheel_layout(tmp_path: Path) -> None:
    """A wheel puts the package under ``site-packages``; the entry must work there.

    The harness's own build configuration packages ``src/lh_harness`` and
    nothing else, so this copies exactly that package into a stand-in
    ``site-packages`` -- no ``scripts/``, no checkout -- and runs the entry
    by absolute path from a task workspace.  It must reach ``verify-pr``.
    """

    package_dir = Path(auditor_verify.__file__).resolve().parent
    site_packages = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    shutil.copytree(package_dir, site_packages / "lh_harness")

    # Prove the stand-in really is wheel-like: no scripts dir next to it.
    assert not (site_packages / "scripts").exists()

    env = _auditor_env(token="", tmp_path=tmp_path)
    env.pop(AUDITOR_GITHUB_TOKEN_ENV, None)
    entry = site_packages / "lh_harness" / "verify_pr.py"
    argv = [sys.executable, str(entry), "verify-pr", "--repo", "example/absent", "--number", "1"]

    completed = _run(argv, _arbitrary_workspace(tmp_path), env)

    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "pr_verifier_unavailable" in combined  # the verifier was reached
    assert "invalid choice" not in combined
    assert "No module named" not in combined


def test_the_entry_reaches_this_harness_even_when_a_stale_copy_is_installed(tmp_path: Path) -> None:
    """``-m lh_harness`` would reach a STALE installed copy; the entry must not.

    This is the hazard that motivated an entry-by-path over ``-m``: an
    interpreter may import an OLDER ``lh_harness`` (a stale editable install
    or an older wheel) that has no ``verify-pr`` at all, and ``-m`` resolves
    through that.  A stand-in stale package -- no ``verify-pr`` in its CLI --
    is put FIRST on the interpreter's path via ``PYTHONPATH``, exactly where
    ``-m`` would look first, and the entry is then run by absolute path.  It
    must still reach THIS harness's verifier.
    """

    stale_root = tmp_path / "stale" / "src"
    (stale_root / "lh_harness").mkdir(parents=True)
    (stale_root / "lh_harness" / "__init__.py").write_text("", encoding="utf-8")
    (stale_root / "lh_harness" / "__main__.py").write_text(
        "import sys; sys.exit('stale copy: no verify-pr here')\n", encoding="utf-8"
    )
    (stale_root / "lh_harness" / "cli.py").write_text(
        "def main(argv=None):\n    print('invalid choice: verify-pr')\n    return 2\n",
        encoding="utf-8",
    )

    # Control: with the stale copy first on the path, -m really does hit it.
    env = _auditor_env(token="", tmp_path=tmp_path)
    env["PYTHONPATH"] = str(stale_root)
    control = _run([sys.executable, "-m", "lh_harness", "verify-pr"], _arbitrary_workspace(tmp_path), env)
    assert control.returncode != 0
    assert "stale copy" in (control.stdout + control.stderr)

    # The entry, by absolute path, ignores that and reaches this harness.
    env.pop(AUDITOR_GITHUB_TOKEN_ENV, None)
    completed = _run(_concrete_command(auditor_verifier_command()), _arbitrary_workspace(tmp_path), env)

    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "pr_verifier_unavailable" in combined  # THIS harness answered
    assert "stale copy" not in combined
    assert "invalid choice" not in combined


def test_the_old_workspace_relative_form_really_does_fail_from_a_task_workspace(tmp_path: Path) -> None:
    """Document the defect: the relative script form cannot start there.

    This is what the auditor did on 2026-11-26.  Pinning it keeps the fix
    honest: if someone reintroduces the relative form, this is the failure
    they reintroduce.
    """

    workspace = _arbitrary_workspace(tmp_path)
    assert not (workspace / "scripts" / "auditor_verify_pr.py").exists()

    completed = _run(
        [sys.executable, "scripts/auditor_verify_pr.py", "--help"],
        workspace,
        _auditor_env(token="", tmp_path=tmp_path),
    )

    assert completed.returncode != 0
    assert "can't open file" in (completed.stdout + completed.stderr)


# ---------------------------------------------------------------------------
# 2. The auditor prompt states that exact command.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["en", "zh"])
def test_both_prompt_mirrors_state_the_exact_command(language: str) -> None:
    """The en and zh instructions each carry the rendered command verbatim."""

    assert f"`{auditor_verifier_command()}`" in CLI_AUDITOR_INSTRUCTIONS[language]


@pytest.mark.parametrize("language", ["en", "zh"])
def test_the_built_auditor_prompt_states_the_exact_command(language: str, tmp_path: Path) -> None:
    """The prompt the harness BUILDS for a CLI audit states the command.

    Built the way manager.py builds it, with the auditor's cwd set to a task
    workspace -- the exact situation the auditor is in.
    """

    prompt = build_role_auditor_prompt(
        task="deliver a PR",
        plan_text="push and open the PR",
        executor_output="PR #1 is open",
        next_step=MANAGER_NEXT_CLI,
        workspace_path=str(_arbitrary_workspace(tmp_path)),
        language=language,
    )

    assert auditor_verifier_command() in prompt


# ---------------------------------------------------------------------------
# 3. The token is never echoed.
# ---------------------------------------------------------------------------


def test_the_command_carries_no_token_value() -> None:
    """The command text names arguments only; the token is read from the env."""

    command = auditor_verifier_command()

    assert SENTINEL_TOKEN not in command
    assert AUDITOR_GITHUB_TOKEN_ENV not in command
    assert not _TOKEN_SHAPE.search(command)


@pytest.mark.parametrize("language", ["en", "zh"])
def test_the_prompt_names_the_token_variable_but_never_a_value(language: str) -> None:
    """The prompt states the variable NAME, and no token-shaped value."""

    instructions = CLI_AUDITOR_INSTRUCTIONS[language]

    assert AUDITOR_GITHUB_TOKEN_ENV in instructions
    assert SENTINEL_TOKEN not in instructions
    assert not _TOKEN_SHAPE.search(instructions)


def test_the_token_reaches_the_child_only_under_its_own_variable(tmp_path: Path) -> None:
    """The production env builder carries the token under ONE name only.

    The value must be in the child's environment for the verifier to read --
    that is the whole point -- but under the auditor's variable and nothing
    else: never mirrored into the executor's ``GH_TOKEN`` / ``GITHUB_TOKEN``,
    which would re-arm a write-capable credential.  A ``dict`` is not a log,
    so what is asserted here is where the value lives, not how the mapping
    prints.
    """

    env = _auditor_env(token=SENTINEL_TOKEN, tmp_path=tmp_path)

    assert env.get(AUDITOR_GITHUB_TOKEN_ENV) == SENTINEL_TOKEN  # it IS carried...
    for shared_name in SHARED_CREDENTIAL_ENV_VARS:
        assert shared_name not in env  # ...under its own name, and no other.
    # And nothing the harness renders for the auditor carries the value.
    assert SENTINEL_TOKEN not in auditor_verifier_command()
    for language in ("en", "zh"):
        assert SENTINEL_TOKEN not in CLI_AUDITOR_INSTRUCTIONS[language]


def test_the_credential_object_never_renders_its_token() -> None:
    """The in-process credential renders presence, never value."""

    credential = auditor_verify.resolve_auditor_credential({AUDITOR_GITHUB_TOKEN_ENV: SENTINEL_TOKEN})

    assert credential.present
    assert SENTINEL_TOKEN not in repr(credential)
    assert SENTINEL_TOKEN not in str(credential)


def test_the_verifier_output_never_echoes_the_token_when_the_pr_cannot_be_read(tmp_path: Path) -> None:
    """The fail-closed "cannot reach" path prints a verdict, never the token.

    Runs the REAL entry module in a child process with the sentinel in the
    auditor's token variable, against a repository that does not exist.  The
    HTTP seam (``urllib.request.urlopen``, the client's default opener) is
    replaced in the child by one raising a connection error, so no network is
    touched and the path taken is precisely the "cannot reach GitHub" one
    from the 2026-11-26 runs.  The sentinel must appear nowhere in the
    child's stdout or stderr.
    """

    # Load the entry by absolute path (as the auditor does), apply the
    # unreachable opener, then run the entry's own main() with the rendered
    # arguments.  The entry pins this harness ahead of anything else first.
    entry = shlex.split(auditor_verifier_command())[1]
    driver = textwrap.dedent(
        f"""
        import importlib.util, sys, urllib.error, urllib.request
        sys.dont_write_bytecode = True
        spec = importlib.util.spec_from_file_location("verify_pr_entry", {entry!r})
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        # main() pins the package root itself; pin it here too, up front, so
        # the opener can be installed on THIS harness's module before the run.
        sys.dont_write_bytecode = True
        entry._pin_own_package_root()

        def unreachable(request, timeout=None):
            raise urllib.error.URLError("reach-test driver: no route to GitHub")

        urllib.request.urlopen = unreachable
        from lh_harness import auditor_verify
        # The entry pinned THIS harness, not whatever the interpreter imports.
        assert auditor_verify.__file__ == {str(Path(auditor_verify.__file__).resolve())!r}, auditor_verify.__file__
        sys.exit(entry.main(sys.argv[1:]))
        """
    )
    workspace = _arbitrary_workspace(tmp_path)
    # argv after the interpreter and the entry: ["verify-pr", "--repo", ...].
    verify_args = _concrete_command(auditor_verifier_command())[2:] + ["--json"]

    completed = _run(
        [sys.executable, "-c", driver, *verify_args],
        workspace,
        _auditor_env(token=SENTINEL_TOKEN, tmp_path=tmp_path),
    )

    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0, combined
    assert '"verdict": "unverifiable"' in combined
    assert SENTINEL_TOKEN not in combined
    assert not _TOKEN_SHAPE.search(combined)
    assert "confirmed" not in combined.lower()


def test_the_verifier_output_never_echoes_the_token_when_it_is_absent(tmp_path: Path) -> None:
    """The fail-closed "no token" path names the variable, never a value."""

    workspace = _arbitrary_workspace(tmp_path)
    env = _auditor_env(token="", tmp_path=tmp_path)
    env.pop(AUDITOR_GITHUB_TOKEN_ENV, None)

    completed = _run(_concrete_command(auditor_verifier_command()), workspace, env)

    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert AUDITOR_GITHUB_TOKEN_ENV in combined  # the NAME is the guidance
    assert not _TOKEN_SHAPE.search(combined)


def test_a_shared_credential_refusal_never_echoes_the_token(tmp_path: Path) -> None:
    """Even the refusal of a shared credential prints names, not values.

    A collision the verifier must refuse: the auditor's variable and the
    executor's ``GH_TOKEN`` hold the SAME value.  The refusal is the one
    place a naive implementation would "helpfully" print what it saw.
    """

    workspace = _arbitrary_workspace(tmp_path)
    # Build a clean child env first (the production builder itself refuses a
    # collision, which is the first layer) ...
    env = _auditor_env(token=SENTINEL_TOKEN, tmp_path=tmp_path)
    # ... then force the collision into the child to exercise the VERIFIER's
    # own refusal layer, which is the one that must not leak.
    env["GH_TOKEN"] = SENTINEL_TOKEN

    completed = _run(_concrete_command(auditor_verifier_command()), workspace, env)

    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert SENTINEL_TOKEN not in combined
    assert not _TOKEN_SHAPE.search(combined)
    assert "pr_verifier_unavailable" in combined


def test_the_env_builder_refuses_a_shared_credential_without_echoing_it(tmp_path: Path) -> None:
    """The builder's own refusal names variables only -- and raises."""

    with pytest.raises(AuditorCredentialError) as excinfo:
        build_auditor_subprocess_env(
            {**_hermetic_source(SENTINEL_TOKEN), "GH_TOKEN": SENTINEL_TOKEN},
            config_dir=tmp_path / "gh-config",
            episode_id="reach-test",
        )

    assert SENTINEL_TOKEN not in str(excinfo.value)
    assert AUDITOR_GITHUB_TOKEN_ENV in str(excinfo.value)
