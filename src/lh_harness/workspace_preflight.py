"""Run-start workspace preflight (LHH-WORKSPACE-TEST-PREFLIGHT).

A run used to discover its test environment the hard way: the first round
burned its whole budget discovering that the interpreter it inherited had no
``pytest``, that ``httpx`` was missing, or that the installed ``fastapi`` did
not match the interpreter it came from.  The failure was discovered *inside*
round 1, so the run spent everything and produced nothing.

This module is the gate that runs **before** round 1 is dispatched.  It
answers three questions about the run's workspace and records the answers:

1. *What does the repo declare?*  The manifest and lock files are read
   (``pyproject.toml``, ``poetry.lock``, ``uv.lock``, ``requirements*.txt``,
   ``package.json`` + lock, ``go.mod``), and the test extras the repo
   declares are extracted from them.
2. *What does the interpreter actually have?*  The resolved interpreter
   (``sys.executable`` for Python, ``node --version`` / ``go version``
   otherwise), its version, and the installed package set are recorded.
3. *Can the declared tests even be collected?*  A collection smoke
   (``pytest --collect-only -q`` or the repo's equivalent) counts the
   collected tests.  **Zero collected is a failure** unless the repo
   explicitly declares that it has no tests -- a skipped or empty collection
   must never be reported as a healthy environment.

The gate fails fast with a *typed owned prerequisite* when a missing
dependency cannot be satisfied from the repo's own manifest.  The owner is
named in the message (currently ``infra onboarding``) so the run stops at a
gate instead of consuming a round.

Environment preparation is workspace-isolated by construction: when the
declared test extras are missing, a ``.venv-task`` virtual environment is
created **inside the workspace** from the manifest and used for both the
baseline and the candidate.  The shared harness virtualenv is never used and
never written to -- a harness venv shared by every run on the host is exactly
the wrong place for a per-run test environment.

Isolation needs one more thing: *sanitization*.  The harness shell exports
``PYTHONPATH=/home/harness/venv/lib/python3.12/site-packages``, so a child
launched with the workspace interpreter still resolves packages from the
shared harness venv.  Every step here therefore runs with ``PYTHONPATH``,
``VIRTUAL_ENV``, and ``PYTHONHOME`` cleared, and the receipt carries the
environment's *provenance* -- ``sys.executable``, the whole ``sys.path``, and
the resolved paths of the repo package and of ``pytest`` -- so a leak is
visible instead of silently producing a green count.

Every observation is recorded as a receipt so a later reader (or an auditor)
can see exactly what the run saw before it started: see
:class:`PreflightRecord`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .safe_subprocess import child_env, git_argv

# The owner of a prerequisite this module cannot satisfy itself.  A missing
# test dependency that cannot be installed from the repo's own manifest is an
# onboarding problem, not something the executor can route around -- so the
# message names the owner explicitly instead of letting a round discover it.
PREREQUISITE_OWNER = "infra onboarding"

# The name of the workspace-local virtual environment.  It lives *inside* the
# workspace so a run never mutates a shared interpreter.
TASK_VENV_DIRNAME = ".venv-task"

# Tooling the task text may require (for example the graphify profile tool).
# Availability is recorded truthfully -- never assumed -- and every unavailable
# tool carries the alternate route an executor should use instead.
PROFILE_TOOLS: tuple[str, ...] = ("graphify",)

# The alternate route when the graphify tool is not installed.  The graph is
# then built by reading the source directly, which is slower but always
# available.
GRAPHIFY_ALTERNATE_ROUTE = (
    "graphify unavailable: grep the source tree for the affected symbols "
    "(e.g. 'grep -rn \"<symbol>\" src/lh_harness/') and read the callers "
    "directly instead of querying the graph"
)

# How long a single subprocess probe may take.  Every subprocess call in this
# module is bounded so a hostile or hung workspace cannot wedge the gate.
_PROBE_TIMEOUT = 30.0

# The smoke collection timeout is larger: a cold collection of a large suite
# legitimately takes a while, but it must still terminate.
_COLLECT_TIMEOUT = 180.0

# The environment variables that make a child's import resolution depend on
# something other than the interpreter it was launched with.
#
# ``PYTHONPATH`` is the dangerous one: the harness shell exports
# ``PYTHONPATH=/home/harness/venv/lib/python3.12/site-packages``, so a child
# launched with the *workspace* interpreter still resolves packages from the
# *shared harness* venv.  The preflight then reports a healthy environment
# that a real test run cannot reproduce -- the fake-green hazard.
#
# ``VIRTUAL_ENV`` names an environment the child is not running in, and
# ``PYTHONHOME`` moves the interpreter's prefix away from the one it was
# built for.  All three are cleared before every install and every
# collection, for the baseline and the candidate alike.
SANITIZED_ENV_NAMES: tuple[str, ...] = ("PYTHONPATH", "VIRTUAL_ENV", "PYTHONHOME")


def sanitized_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a child environment with the import-leaking names removed.

    The result is the harness's own scrubbed child environment (no
    control-plane secrets, no interactive prompts) with
    :data:`SANITIZED_ENV_NAMES` removed.  Every preflight subprocess runs
    under this environment, so the interpreter that the preflight inspects
    is the interpreter a real test run would use.
    """

    env = child_env(base)
    for name in SANITIZED_ENV_NAMES:
        env.pop(name, None)
    return env


# ---------------------------------------------------------------------------
# Manifest reading
# ---------------------------------------------------------------------------

# The manifest files this module knows how to read, in priority order.  The
# first one that exists wins for the "declared" answer; all of them are
# reported so an auditor can see exactly what the workspace declared.
PYTHON_MANIFESTS: tuple[str, ...] = (
    "pyproject.toml",
    "poetry.lock",
    "uv.lock",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
)
NODE_MANIFESTS: tuple[str, ...] = (
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
)
GO_MANIFESTS: tuple[str, ...] = ("go.mod",)


def _read_text(path: Path, *, max_bytes: int = 1_000_000) -> str | None:
    """Read a bounded text file; ``None`` when it cannot be read."""

    try:
        raw = path.read_bytes()[:max_bytes]
    except OSError:
        return None
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - decode with errors= never raises
        return None


def _pyproject_test_extras(text: str) -> list[str]:
    """Extract the requirement names from ``[project.optional-dependencies]``.

    The table is parsed with a small, deliberately narrow scanner rather than
    a TOML parser: the harness must run on a Python that may predate
    ``tomllib`` (3.11+) and the on-disk format here is simple.  Only the
    ``test`` extra is read -- that is the extra the task contract names.
    """

    try:
        import tomllib  # type: ignore[import-not-found]
    except Exception:
        tomllib = None  # type: ignore[assignment]

    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except Exception:
            data = None
        if isinstance(data, dict):
            project = data.get("project")
            if isinstance(project, dict):
                extras = project.get("optional-dependencies")
                if isinstance(extras, dict):
                    values = extras.get("test")
                    if isinstance(values, list):
                        return _requirement_names(str(item) for item in values)
            return []

    # Fallback scanner for a Python without ``tomllib``.
    match = re.search(
        r"^\s*\[project\.optional-dependencies\]\s*$", text, re.MULTILINE
    )
    if not match:
        return []
    tail = text[match.end():]
    section = re.split(r"^\s*\[", tail, maxsplit=1, flags=re.MULTILINE)[0]
    extra_match = re.search(
        r"^\s*test\s*=\s*\[(.*?)\]", section, re.MULTILINE | re.DOTALL
    )
    if not extra_match:
        return []
    body = extra_match.group(1)
    names = re.findall(r"[\"']([^\"']+)[\"']", body)
    return _requirement_names(names)


def _requirement_names(raw: Iterable[str]) -> list[str]:
    """Reduce requirement specifiers to their importable package names.

    ``pytest>=7`` -> ``pytest``; ``httpx2`` -> ``httpx2``; a VCS or URL
    requirement has no importable name and is reported verbatim so the
    operator can see what was actually declared.
    """

    names: list[str] = []
    for item in raw:
        text = str(item).strip()
        if not text or text.startswith("#"):
            continue
        if "://" in text or text.startswith("git+"):
            names.append(text)
            continue
        match = re.match(r"^([A-Za-z0-9_.\-]+)", text)
        if not match:
            names.append(text)
            continue
        names.append(match.group(1))
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return ordered


def _requirements_txt_names(text: str) -> list[str]:
    """Extract requirement names from a ``requirements*.txt`` body."""

    names: list[str] = []
    for line in text.splitlines():
        text_line = line.strip()
        if not text_line or text_line.startswith("#") or text_line.startswith("-"):
            continue
        names.append(text_line)
    return _requirement_names(names)


def _package_json_test_scripts(text: str) -> list[str]:
    """Return the npm ``test`` script names a ``package.json`` declares."""

    try:
        data = json.loads(text)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return []
    names = [name for name in scripts if "test" in str(name).lower()]
    return sorted(names)


@dataclass
class ManifestInfo:
    """What the workspace's manifest and lock files declare."""

    ecosystem: str = ""
    manifest_path: str | None = None
    lock_paths: list[str] = field(default_factory=list)
    declared_test_requirements: list[str] = field(default_factory=list)
    declared_no_tests: bool = False
    node_test_scripts: list[str] = field(default_factory=list)
    requires_python: str | None = None
    # True when the repo has a test suite the preflight can look for: a
    # declared test extra, a declared test script, or a test directory.
    has_tests: bool = False
    # -- amendment-3: the manifest's own health ------------------------------
    #
    # "No manifest" and "the repo declares no tests" are *different* facts and
    # must never be conflated.  A workspace whose manifest is missing,
    # unreadable, or simply not one this module knows how to read is a
    # workspace whose declared test suite is **unknown** -- not empty.  The
    # preflight reports that as a readiness gap rather than as a healthy
    # "no tests" green.
    #
    # ``manifest_found``   -- at least one known manifest file exists.
    # ``manifest_error``   -- a manifest exists but could not be read.
    # ``suite_kind``       -- which test runner the declared suite implies:
    #                         "python", "node", "go", or "unknown".  A repo may
    #                         be polyglot; the *test* ecosystem is what
    #                         matters here, and it is not assumed to be
    #                         Python.
    manifest_found: bool = False
    manifest_error: str | None = None
    suite_kind: str = "unknown"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ecosystem": self.ecosystem,
            "manifest_path": self.manifest_path,
            "lock_paths": list(self.lock_paths),
            "declared_test_requirements": list(self.declared_test_requirements),
            "declared_no_tests": self.declared_no_tests,
            "node_test_scripts": list(self.node_test_scripts),
            "requires_python": self.requires_python,
            "has_tests": self.has_tests,
            "manifest_found": self.manifest_found,
            "manifest_error": self.manifest_error,
            "suite_kind": self.suite_kind,
        }


def _pyproject_declares_no_tests(text: str) -> bool:
    """True when the repo explicitly says it ships no tests.

    A repo may legitimately have no test suite; the contract only forbids
    treating an *empty collection* as healthy, and that exception has to be
    declared by the repo rather than inferred from a failed collection.
    """

    return bool(
        re.search(
            r"(?im)^\s*(?:no[_-]?tests|tests?\s*=\s*false|skip[_-]?tests|"
            r"disable[_-]?tests|testpaths\s*=\s*\[\s*\])",
            text,
        )
    )


def read_manifest(workspace_path: str | Path) -> ManifestInfo:
    """Read the workspace's manifest and lock files.

    Every probe degrades to ``None``/``[]`` instead of raising: a workspace
    without a manifest is a legitimate (if unusual) workspace, and the gate
    must report that truthfully rather than crash.
    """

    root = Path(workspace_path)
    info = ManifestInfo()

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        info.manifest_found = True
        text = _read_text(pyproject)
        if text is None:
            # The manifest exists but cannot be read (permissions, a binary
            # file, an I/O error).  That is *not* "the repo declares no
            # tests" -- it is an unreadable declaration.
            info.manifest_error = f"could not read {pyproject}"
        else:
            info.ecosystem = "python"
            info.manifest_path = str(pyproject)
            info.declared_test_requirements = _pyproject_test_extras(text)
            info.declared_no_tests = _pyproject_declares_no_tests(text)
            match = re.search(r'(?m)^\s*requires-python\s*=\s*"([^"]+)"', text)
            if match:
                info.requires_python = match.group(1)
            for name in PYTHON_MANIFESTS[1:]:
                if (root / name).is_file():
                    info.lock_paths.append(str(root / name))
            # A requirements*.txt may carry the test extras when pyproject
            # does not declare them.
            if not info.declared_test_requirements:
                for name in PYTHON_MANIFESTS[3:]:
                    candidate = root / name
                    if not candidate.is_file():
                        continue
                    body = _read_text(candidate)
                    if body:
                        names = _requirements_txt_names(body)
                        if names:
                            info.declared_test_requirements = names
                            break
    else:
        requirements = [
            root / name for name in PYTHON_MANIFESTS[3:] if (root / name).is_file()
        ]
        if requirements:
            info.manifest_found = True
            info.ecosystem = "python"
            info.manifest_path = str(requirements[0])
            body = _read_text(requirements[0])
            if body is None:
                info.manifest_error = f"could not read {requirements[0]}"
            else:
                info.declared_test_requirements = _requirements_txt_names(body)
            info.lock_paths = [str(item) for item in requirements[1:]]

    package_json = root / "package.json"
    if package_json.is_file():
        info.manifest_found = True
        text = _read_text(package_json)
        if text is None:
            info.manifest_error = info.manifest_error or f"could not read {package_json}"
        else:
            # A Python manifest wins the ecosystem label: this repo may ship a
            # frontend, but the test extras the contract names are the Python
            # ones.
            if not info.ecosystem:
                info.ecosystem = "node"
                info.manifest_path = str(package_json)
            elif str(package_json) not in info.lock_paths:
                info.lock_paths.append(str(package_json))
            info.node_test_scripts = _package_json_test_scripts(text)
            for name in NODE_MANIFESTS[1:]:
                candidate = root / name
                if candidate.is_file() and str(candidate) not in info.lock_paths:
                    info.lock_paths.append(str(candidate))

    go_mod = root / "go.mod"
    if go_mod.is_file():
        info.manifest_found = True
        if not info.ecosystem:
            info.ecosystem = "go"
            info.manifest_path = str(go_mod)
        elif str(go_mod) not in info.lock_paths:
            info.lock_paths.append(str(go_mod))

    # A repo has tests when it declares them (a test extra, a test script) or
    # when it ships a test directory.  A repo with no manifest and no test
    # directory simply has no test suite, and the preflight must not treat
    # "no tests to run" as a broken test environment.
    #
    # -- amendment-3: a test *directory* alone does not establish a declared
    # suite.  A directory called ``tests`` proves the repo has tests; it does
    # not prove which runner collects them, and it says nothing about whether
    # the manifest that would declare them was read at all.
    info.has_tests = bool(
        info.declared_test_requirements
        or info.node_test_scripts
        or (root / "tests").is_dir()
        or (root / "test").is_dir()
        or (root / "spec").is_dir()
        or (root / "__tests__").is_dir()
    )

    # The declared suite's runner.  This is derived from what the manifest
    # *declares*, never assumed to be Python: a polyglot repo that declares a
    # Node test script has a Node suite even when it also ships a pyproject.
    if info.declared_test_requirements or info.ecosystem == "python":
        info.suite_kind = "python"
    elif info.node_test_scripts or info.ecosystem == "node":
        info.suite_kind = "node"
    elif info.ecosystem == "go":
        info.suite_kind = "go"
    else:
        info.suite_kind = "unknown"

    return info


# ---------------------------------------------------------------------------
# Interpreter resolution
# ---------------------------------------------------------------------------


def _run_probe(
    argv: list[str],
    *,
    timeout: float = _PROBE_TIMEOUT,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    sanitize: bool = True,
) -> tuple[int, str, str]:
    """Run one bounded probe; never raises.

    By default the probe runs under the **sanitized** environment: the
    import-leaking names are removed, so no probe can accidentally resolve a
    package from outside the interpreter it names.  ``env`` is merged **on
    top of** that sanitized environment rather than replacing it.

    ``sanitize=False`` is used by exactly one caller -- the provenance
    probe.  That probe has to *measure* the environment the preflight
    itself inherited, because the whole point of the provenance receipt is
    to show whether the run's own environment leaked a shared venv into the
    interpreter's import path.  Measuring a sanitized environment would
    always come back clean and hide the very thing the receipt exists to
    reveal.
    """

    merged = sanitized_env() if sanitize else child_env()
    if env is not None:
        merged.update(dict(env))
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=str(cwd) if cwd is not None else None,
            env=merged,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"
    return completed.returncode, completed.stdout, completed.stderr


def _venv_interpreter(venv_dir: Path) -> Path | None:
    """Return the interpreter inside a virtual environment, if it has one."""

    for relative in ("bin/python", "bin/python3", "Scripts/python.exe"):
        candidate = venv_dir / relative
        if candidate.is_file():
            return candidate
    return None


def _is_shared_harness_venv(interpreter: Path) -> bool:
    """True when the interpreter lives in the shared harness virtualenv.

    The shared harness venv must never be used as a *task* environment: it is
    shared by every run on the host, so a per-run install into it corrupts
    other runs and hides a broken workspace behind another run's packages.
    """

    # The path is inspected **as invoked**: resolving a venv's ``bin/python``
    # symlink would land on the base interpreter and hide the very directory
    # the shared venv lives in.
    try:
        absolute = interpreter.absolute()
    except OSError:
        absolute = interpreter
    parts = {part.lower() for part in absolute.parts}
    if "venv" in parts and "harness" in parts:
        return True
    text = str(absolute)
    return "/home/harness/venv" in text


def resolve_interpreter(
    workspace_path: str | Path,
    *,
    preferred: str | Path | None = None,
) -> dict[str, Any]:
    """Resolve the interpreter the workspace's tests should run under.

    The workspace-local ``.venv-task`` wins when it exists and carries an
    interpreter: it is the workspace-isolated environment this module creates
    and reuses.  Otherwise the *declared* interpreter is used -- the one
    running this process -- but only when it is not the shared harness venv.

    The returned mapping always carries the fields the receipt needs:
    ``executable``, ``version``, ``source``, and ``shared_harness_venv``.
    """

    root = Path(workspace_path)
    result: dict[str, Any] = {
        "executable": None,
        "version": None,
        "source": "",
        "shared_harness_venv": False,
        "in_virtualenv": False,
    }

    candidates: list[tuple[Path, str]] = []
    if preferred:
        candidates.append((Path(preferred), "preferred"))
    task_venv = root / TASK_VENV_DIRNAME
    found = _venv_interpreter(task_venv)
    if found is not None:
        candidates.append((found, "workspace_venv"))
    candidates.append((Path(sys.executable), "current_process"))

    for candidate, source in candidates:
        if not candidate.is_file():
            continue
        if _is_shared_harness_venv(candidate):
            result["shared_harness_venv"] = True
            continue
        version = _probe_interpreter_version(candidate)
        if version is None:
            continue
        # The path is recorded **as invoked**, not resolved: a venv's
        # ``bin/python`` is normally a symlink to the base interpreter, so
        # resolving it would name the system Python and lose the very fact
        # that this interpreter carries the venv's own site-packages.
        result["executable"] = str(candidate.absolute())
        result["version"] = version
        result["source"] = source
        result["shared_harness_venv"] = False
        result["in_virtualenv"] = _probe_venv_membership(candidate)
        return result

    # Nothing usable.  Report the shared-venv case explicitly so the receipt
    # never implies a usable interpreter that does not exist.
    if result["shared_harness_venv"]:
        result["source"] = "shared_harness_venv_rejected"
    return result


def _probe_interpreter_version(interpreter: Path) -> str | None:
    """Return the interpreter's version string, or ``None`` when unusable."""

    code, stdout, stderr = _run_probe([str(interpreter), "-c", "import sys; print(sys.version.split()[0])"])
    if code == 0:
        text = stdout.strip()
        if text:
            return text
    # An interpreter that cannot even print its version is not usable for a
    # test run, but the error is worth surfacing to the operator.
    detail = (stderr or stdout).strip().splitlines()
    return None if not detail else f"unusable: {detail[0][:200]}"


def _probe_venv_membership(interpreter: Path) -> bool:
    """True when the interpreter runs inside a virtual environment.

    A venv's ``bin/python`` is normally a symlink to the *base* interpreter,
    so the path on disk does not say whether the interpreter is inside a
    venv.  ``sys.prefix != sys.base_prefix`` does, and it is what decides
    whether the interpreter carries the venv's own site-packages.
    """

    code, stdout, _stderr = _run_probe(
        [
            str(interpreter),
            "-c",
            "import sys; print('1' if sys.prefix != sys.base_prefix else '0')",
        ]
    )
    return code == 0 and stdout.strip() == "1"


# The probe that reports where the interpreter, its import path, the repo
# package, and the test runner actually come from.  It is printed as JSON on
# one line so the caller can parse it without guessing at the layout.
_PROVENANCE_SCRIPT = r"""
import json, os, sys
out = {
    "sys_executable": sys.executable,
    "sys_prefix": sys.prefix,
    "sys_base_prefix": sys.base_prefix,
    "sys_path": list(sys.path),
    "pythonpath": os.environ.get("PYTHONPATH"),
    "virtual_env": os.environ.get("VIRTUAL_ENV"),
    "pythonhome": os.environ.get("PYTHONHOME"),
    "repo_package_path": None,
    "pytest_path": None,
}
try:
    import lh_harness
    out["repo_package_path"] = getattr(lh_harness, "__file__", None)
except Exception:
    out["repo_package_path"] = None
try:
    import pytest
    out["pytest_path"] = getattr(pytest, "__file__", None)
except Exception:
    out["pytest_path"] = None
print(json.dumps(out))
"""


def collect_provenance(interpreter: str | Path) -> dict[str, Any]:
    """Record *where* the interpreter's packages actually come from.

    The preflight's job is not just to say "pytest imports" but to say
    *which* pytest: a run that believes it has a workspace-isolated
    environment, while its imports are silently resolved from the shared
    harness venv, is the exact failure this receipt exists to make visible.

    The fields are:

    ``sys_executable``
        The interpreter that actually ran.
    ``sys_path``
        The whole import path, in order.
    ``repo_package_path``
        The resolved file path of the repo's own package.
    ``pytest_path``
        The resolved file path of the test runner.

    Every one of them is checked against the shared harness venv by
    :func:`provenance_violations`.

    The probe deliberately runs with the **inherited** environment
    (``sanitize=False``).  Sanitizing it would hide the very leak the
    receipt exists to reveal: the harness shell exports
    ``PYTHONPATH=/home/harness/venv/lib/python3.12/site-packages``, and a
    sanitized probe would report a clean import path every time.  The
    preflight *measures* the inherited environment and then acts on it --
    the install and the collection both run sanitized, so the environment
    it builds is the sanitized one.
    """

    code, stdout, _stderr = _run_probe(
        [str(interpreter), "-c", _PROVENANCE_SCRIPT], timeout=60.0, sanitize=False
    )
    if code != 0:
        return {
            "sys_executable": str(interpreter),
            "sys_path": [],
            "repo_package_path": None,
            "pytest_path": None,
            "pythonpath": None,
            "virtual_env": None,
            "pythonhome": None,
            "error": "provenance probe failed",
        }
    try:
        parsed = json.loads(stdout)
    except Exception:
        return {
            "sys_executable": str(interpreter),
            "sys_path": [],
            "repo_package_path": None,
            "pytest_path": None,
            "pythonpath": None,
            "virtual_env": None,
            "pythonhome": None,
            "error": "provenance probe returned unreadable output",
        }
    if not isinstance(parsed, dict):
        parsed = {}
    return {
        "sys_executable": parsed.get("sys_executable"),
        "sys_prefix": parsed.get("sys_prefix"),
        "sys_base_prefix": parsed.get("sys_base_prefix"),
        "sys_path": [str(item) for item in (parsed.get("sys_path") or [])],
        "repo_package_path": parsed.get("repo_package_path"),
        "pytest_path": parsed.get("pytest_path"),
        "pythonpath": parsed.get("pythonpath"),
        "virtual_env": parsed.get("virtual_env"),
        "pythonhome": parsed.get("pythonhome"),
    }


def provenance_violations(provenance: Mapping[str, Any]) -> list[str]:
    """Return the provenance fields that land under the shared harness venv.

    A violation is any resolved path -- the interpreter, an entry of
    ``sys.path``, the repo package, or pytest -- that lives under the
    shared harness virtualenv.  The preflight refuses such an environment:
    the shared venv belongs to every run on the host, so a task environment
    that resolves packages from it is neither isolated nor reproducible.
    """

    violations: list[str] = []

    def _check(label: str, value: object) -> None:
        if isinstance(value, str) and value and _under_shared_harness_venv(value):
            violations.append(f"{label}={value}")

    _check("sys_executable", provenance.get("sys_executable"))
    _check("repo_package_path", provenance.get("repo_package_path"))
    _check("pytest_path", provenance.get("pytest_path"))
    sys_path = provenance.get("sys_path")
    if isinstance(sys_path, list):
        for entry in sys_path:
            if isinstance(entry, str) and _under_shared_harness_venv(entry):
                violations.append(f"sys_path={entry}")
    return violations


def _under_shared_harness_venv(path: str) -> bool:
    """True when ``path`` lives under the shared harness virtualenv.

    The check is a *path-string* comparison: nothing is executed from, or
    written to, the shared venv.  The comparison is deliberately generous
    about the surrounding path shape so that a venv nested under the
    harness home (``/home/harness/venv``, ``/home/harness/work/venv``, ...)
    is caught too.
    """

    text = str(path).replace("\\", "/")
    if "/home/harness/venv" in text:
        return True
    parts = {part.lower() for part in text.split("/") if part}
    return "venv" in parts and "harness" in parts


def installed_packages(interpreter: str | Path) -> list[str]:
    """Return the interpreter's installed distribution names, sorted.

    ``pip list`` is not used: it is slow and depends on pip being importable.
    ``importlib.metadata`` is part of the standard library since 3.8 and gives
    the same answer for the question the preflight asks.
    """

    code, stdout, _stderr = _run_probe(
        [
            str(interpreter),
            "-c",
            "import importlib.metadata as m; "
            "print('\\n'.join(sorted({d.metadata['Name'] for d in m.distributions() "
            "if d.metadata.get('Name')}, key=str.lower)))",
        ]
    )
    if code != 0:
        return []
    return [line.strip() for line in stdout.splitlines() if line.strip()]


def import_check(interpreter: str | Path, requirements: Iterable[str]) -> dict[str, bool]:
    """Check that each declared requirement is importable under the interpreter.

    The check maps a declared distribution name to its importable module the
    way pip names it: ``pytest`` -> ``pytest``, ``httpx2`` -> ``httpx2``,
    ``pytest-asyncio`` -> ``pytest_asyncio``.

    The module is **actually imported**, not merely located with
    ``find_spec``: a package that is present on disk but raises on import is
    not usable, and reporting it as available would send the run straight
    into the same failure the preflight exists to catch.
    """

    names = [name for name in requirements if name]
    if not names:
        return {}
    script = (
        "import importlib, json, sys\n"
        "out = {}\n"
        "for name in json.loads(sys.argv[1]):\n"
        "    ok = False\n"
        "    for mod in name.replace('-', '_').split('.'):\n"
        "        if not mod:\n"
        "            continue\n"
        "        try:\n"
        "            importlib.import_module(mod)\n"
        "        except Exception:\n"
        "            ok = False\n"
        "            break\n"
        "        ok = True\n"
        "    out[name] = ok\n"
        "print(json.dumps(out))\n"
    )
    code, stdout, _stderr = _run_probe(
        [str(interpreter), "-c", script, json.dumps(names)]
    )
    if code != 0:
        return {name: False for name in names}
    try:
        parsed = json.loads(stdout)
    except Exception:
        return {name: False for name in names}
    if not isinstance(parsed, dict):
        return {name: False for name in names}
    return {name: bool(parsed.get(name, False)) for name in names}


# ---------------------------------------------------------------------------
# Collection smoke
# ---------------------------------------------------------------------------


@dataclass
class CollectionResult:
    """The outcome of the test-collection smoke."""

    command: list[str] = field(default_factory=list)
    collected: int = 0
    ok: bool = False
    detail: str = ""
    returncode: int = -1

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "collected": self.collected,
            "ok": self.ok,
            "detail": self.detail,
            "returncode": self.returncode,
        }


_COLLECT_COUNT_RE = re.compile(r"(\d+)\s+tests?\s+collected", re.IGNORECASE)
_COLLECT_COUNT_ALT_RE = re.compile(r"collected\s+(\d+)\s+items?", re.IGNORECASE)


def _parse_collected(text: str) -> int:
    """Extract the collected-test count from pytest's summary line."""

    for pattern in (_COLLECT_COUNT_RE, _COLLECT_COUNT_ALT_RE):
        matches = pattern.findall(text)
        if matches:
            try:
                return int(matches[-1])
            except (TypeError, ValueError):
                continue
    return 0


def collect_tests(
    interpreter: str | Path,
    workspace_path: str | Path,
    *,
    timeout: float = _COLLECT_TIMEOUT,
) -> CollectionResult:
    """Run the collection smoke and count the collected tests.

    ``pytest --collect-only -q`` is the repo's equivalent runner when the
    workspace declares pytest.  A zero count is **not** success: the caller
    decides, using the manifest's ``declared_no_tests`` flag, whether an empty
    collection is legitimate.
    """

    root = Path(workspace_path)
    command = [str(interpreter), "-m", "pytest", "--collect-only", "-q"]
    # The collection must run *inside* the workspace: pytest discovers tests
    # relative to its rootdir, and running it from the harness's own cwd
    # would collect the harness's suite instead of the workspace's -- a
    # green count for the wrong repository.
    code, stdout, stderr = _run_probe(command, timeout=timeout, cwd=root)
    combined = (stdout or "") + ("\n" + stderr if stderr else "")
    collected = _parse_collected(combined)
    detail = ""
    if code != 0:
        tail = [line for line in combined.strip().splitlines() if line.strip()]
        detail = tail[-1][:500] if tail else "collection failed"
    return CollectionResult(
        command=command,
        collected=collected,
        ok=(code == 0 and collected > 0),
        detail=detail,
        returncode=code,
    )


# ---------------------------------------------------------------------------
# Git baseline
# ---------------------------------------------------------------------------


def git_baseline(workspace_path: str | Path) -> dict[str, Any]:
    """Record the workspace's git baseline: head, dirty files, and stashes.

    The git calls go through the service's hardened helpers: ``git_argv``
    neutralises ``core.fsmonitor`` and ``core.hooksPath`` (a run controls its
    own repository and could plant either), and ``child_env`` scrubs the
    control-plane secrets so no child inherits them.
    """

    root = Path(workspace_path)

    def _git(args: list[str]) -> str | None:
        code, stdout, _stderr = _run_probe(
            git_argv("-C", str(root), *args),
            env=sanitized_env(),
        )
        if code != 0:
            return None
        return stdout.strip()

    head = _git(["rev-parse", "HEAD"])
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    status = _git(["status", "--porcelain"])
    dirty: list[str] = []
    if status:
        dirty = [line[3:] for line in status.splitlines() if len(line) > 3]
    stash_text = _git(["stash", "list"])
    stashes: list[str] = []
    if stash_text:
        stashes = [line for line in stash_text.splitlines() if line.strip()]
    return {
        "head_sha": head,
        "branch": branch,
        "dirty_files": dirty,
        "stashes": stashes,
        "is_repo": head is not None,
    }


# ---------------------------------------------------------------------------
# Profile tool availability
# ---------------------------------------------------------------------------


def profile_tool_availability() -> dict[str, dict[str, Any]]:
    """Record, truthfully, whether each profile tool is actually available.

    Availability is probed, never assumed.  A tool that is not installed is
    recorded as unavailable **with the alternate route** an executor must use
    instead -- the preflight never silently drops a required tool.
    """

    result: dict[str, dict[str, Any]] = {}
    for tool in PROFILE_TOOLS:
        path = shutil.which(tool)
        available = path is not None
        entry: dict[str, Any] = {
            "available": available,
            "path": path,
            "alternate_route": "" if available else GRAPHIFY_ALTERNATE_ROUTE,
        }
        if available:
            # A tool on PATH can still be broken; a version probe keeps the
            # record truthful rather than reporting "available" on a stub.
            code, stdout, stderr = _run_probe([path, "--version"])
            entry["version"] = (stdout or stderr).strip().splitlines()[0][:120] if (stdout or stderr).strip() else ""
            entry["usable"] = code == 0
            if code != 0:
                entry["alternate_route"] = GRAPHIFY_ALTERNATE_ROUTE
        else:
            entry["version"] = ""
            entry["usable"] = False
        result[tool] = entry
    return result


# ---------------------------------------------------------------------------
# Workspace-isolated environment preparation
# ---------------------------------------------------------------------------


def prepare_workspace_env(
    workspace_path: str | Path,
    *,
    interpreter: str | Path | None = None,
    allow_install: bool = True,
) -> dict[str, Any]:
    """Create (or reuse) the workspace-local ``.venv-task`` environment.

    The environment is created **inside the workspace** from the repo's own
    manifest (``pip install -e '.[test]'`` for a Python repo that declares a
    ``test`` extra).  The shared harness virtualenv is never touched.

    The function reports what it did; it does not raise.  A failed install is
    a prerequisite failure that the caller turns into a typed owned
    prerequisite -- it is never a reason to fall back to some other
    interpreter.
    """

    root = Path(workspace_path)
    venv_dir = root / TASK_VENV_DIRNAME
    result: dict[str, Any] = {
        "venv_path": str(venv_dir),
        "created": False,
        "reused": False,
        "installed": False,
        "interpreter": None,
        "detail": "",
    }

    existing = _venv_interpreter(venv_dir)
    if existing is not None:
        result["reused"] = True
        result["interpreter"] = str(existing)
        return result

    if not allow_install:
        result["detail"] = "environment creation not permitted"
        return result

    if shutil.which("python3") is None and shutil.which("python") is None:
        result["detail"] = "no python interpreter available to create the environment"
        return result

    base = shutil.which("python3") or shutil.which("python")
    code, stdout, stderr = _run_probe(
        [str(base), "-m", "venv", str(venv_dir)], timeout=120.0, cwd=root
    )
    if code != 0:
        detail = (stderr or stdout).strip().splitlines()
        result["detail"] = (
            detail[-1][:500]
            if detail
            else "could not create the workspace environment"
        )
        return result

    created_interpreter = _venv_interpreter(venv_dir)
    if created_interpreter is None:
        result["detail"] = "the created environment has no interpreter"
        return result
    result["created"] = True
    result["interpreter"] = str(created_interpreter)

    manifest = read_manifest(root)
    if manifest.declared_test_requirements:
        # The install runs under the sanitized environment: an inherited
        # ``PYTHONPATH`` pointing at the shared harness venv would make pip
        # resolve the declared dependencies *from that venv* instead of
        # installing them into the new one -- the workspace would look
        # provisioned while carrying none of its own packages.
        code, stdout, stderr = _run_probe(
            [
                str(created_interpreter),
                "-m",
                "pip",
                "install",
                "-e",
                ".[test]",
            ],
            timeout=600.0,
            cwd=root,
        )
        if code != 0:
            detail = (stderr or stdout).strip().splitlines()
            result["detail"] = (
                detail[-1][:500]
                if detail
                else "could not install the declared test extras"
            )
            return result
        result["installed"] = True
    return result


# ---------------------------------------------------------------------------
# The typed owned prerequisite
# ---------------------------------------------------------------------------


@dataclass
class PreflightPrerequisite:
    """A prerequisite the preflight cannot satisfy, with its owner.

    This is the typed counterpart of a launch failure: it says *what* is
    missing, *why* it cannot be fixed from the repo's own manifest, and
    *who* owns fixing it.  The owner is a named team, not a vague "operator".
    """

    code: str
    message: str
    owner: str = PREREQUISITE_OWNER
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "owner": self.owner,
            "detail": dict(self.detail),
        }

    def actionable(self) -> str:
        """The operator-facing message naming the owner and the action."""

        return f"{self.message} (owner: {self.owner})"


# ---------------------------------------------------------------------------
# The typed readiness gap
# ---------------------------------------------------------------------------


@dataclass
class PreflightReadinessGap:
    """A gap in what the preflight could determine about the workspace.

    A *prerequisite* (above) is a hard stop: a dependency the preflight
    cannot satisfy from the repo's own manifest, owned by a named team.  A
    *readiness gap* is different in kind: the preflight could not determine
    the workspace's declared test suite at all, so it cannot say the
    environment is healthy.

    The three canonical gaps are:

    ``manifest_missing``
        No known manifest file exists in the workspace.  This is **not** the
        repo declaring it has no tests -- it is the preflight being unable
        to read the declaration.  A wrong working directory and a partial
        checkout both land here, and both would otherwise produce a green
        "the repo declares no tests".

    ``manifest_unreadable``
        A manifest exists but could not be read.

    ``unsupported_suite``
        The repo declares a test suite whose runner this preflight does not
        know how to drive (a Node or Go suite, or an unknown one).  The
        preflight reports the gap truthfully instead of forcing a Python
        runner onto a non-Python repo.

    A readiness gap never reports a healthy collection.  It is recorded, it
    is projected into both receipts, and it is distinguishable from a
    prerequisite by its ``kind`` field -- so an overseer can tell "the
    environment is broken" from "we could not tell".
    """

    code: str
    message: str
    kind: str = "readiness_gap"
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "kind": self.kind,
            "detail": dict(self.detail),
        }


# The readiness-gap codes.  They are named constants so a test can assert on
# the exact outcome instead of on a message string.
READINESS_GAP_MANIFEST_MISSING = "manifest_missing"
READINESS_GAP_MANIFEST_UNREADABLE = "manifest_unreadable"
READINESS_GAP_UNSUPPORTED_SUITE = "unsupported_suite"


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


@dataclass
class PreflightRecord:
    """Everything the preflight observed about the run's workspace.

    The same fields appear in the run-dir receipt file and in the run
    snapshot, so the overseer sees one shape in both places.
    """

    candidate_sha: str | None = None
    workspace_path: str = ""
    env_manifest: dict[str, Any] = field(default_factory=dict)
    interpreter: dict[str, Any] = field(default_factory=dict)
    installed_packages: list[str] = field(default_factory=list)
    dependency_imports: dict[str, bool] = field(default_factory=dict)
    collection: dict[str, Any] = field(default_factory=dict)
    git_baseline: dict[str, Any] = field(default_factory=dict)
    tool_availability: dict[str, Any] = field(default_factory=dict)
    env_preparation: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    prerequisite: dict[str, Any] | None = None
    # -- amendment-3: a typed readiness gap, distinct from the prerequisite.
    # At most one gap is recorded (the first one the preflight hits).
    readiness_gap: dict[str, Any] | None = None
    ok: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_sha": self.candidate_sha,
            "workspace_path": self.workspace_path,
            "env_manifest": dict(self.env_manifest),
            "interpreter": dict(self.interpreter),
            "installed_packages": list(self.installed_packages),
            "dependency_imports": dict(self.dependency_imports),
            "collection": dict(self.collection),
            "git_baseline": dict(self.git_baseline),
            "tool_availability": dict(self.tool_availability),
            "env_preparation": dict(self.env_preparation),
            "provenance": dict(self.provenance),
            "prerequisite": dict(self.prerequisite) if self.prerequisite else None,
            "readiness_gap": dict(self.readiness_gap) if self.readiness_gap else None,
            "ok": self.ok,
        }


# The receipt's field names, asserted by the test suite so a rename cannot
# silently drop a field from one of the two carriers.
RECEIPT_FIELDS: tuple[str, ...] = (
    "candidate_sha",
    "env_manifest",
    "interpreter",
    "collected",
    "tool_availability",
)

# The provenance fields the receipt must carry, asserted by the test suite so
# a rename cannot silently drop one of the two carriers.
PROVENANCE_FIELDS: tuple[str, ...] = (
    "sys_executable",
    "sys_path",
    "repo_package_path",
    "pytest_path",
)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def _readiness_gap_for(
    manifest: ManifestInfo,
    root: Path,
    *,
    no_tests_applicable: bool = False,
) -> PreflightReadinessGap | None:
    """Return the readiness gap for this workspace, or ``None``.

    This is the amendment-3 guard.  It answers one question: *could the
    preflight determine the workspace's declared test suite?*  When the
    answer is no, the preflight must say so with a typed gap instead of
    reporting a healthy environment.

    The four situations that produce a gap:

    * **the manifest is missing** -- no known manifest file exists.  A
      wrong working directory, a partial checkout, and a repo whose
      manifest this module does not know all land here.  None of them
      means "the repo has no tests".
    * **the manifest is unreadable** -- it exists but could not be read.
    * **the declared suite is unsupported** -- the repo declares a Node or
      Go suite and this preflight cannot drive it.  The gap names the
      suite; it never forces a Python runner onto a non-Python repo.
    * **the suite is unknown** -- a manifest was found but it declares
      nothing that identifies a test runner at all.

    ``no_tests_applicable`` (the explicit task/profile signal) and the
    repo's own ``declared_no_tests`` declaration both suppress the gap:
    those are the two ways an absent suite is *truthfully* established.

    A wrong working directory is worth calling out on its own: a
    directory that is not a git checkout and holds no manifest is exactly
    what "the preflight was pointed at the wrong path" looks like, so the
    gap message says so.
    """

    if no_tests_applicable:
        # The task/profile established that there is no test suite.  That is
        # an explicit applicability, so the absence is not a gap.
        return None

    if manifest.manifest_error:
        return PreflightReadinessGap(
            code=READINESS_GAP_MANIFEST_UNREADABLE,
            message=(
                "the workspace's manifest could not be read, so its declared "
                f"test suite could not be determined: {manifest.manifest_error}"
            ),
            detail={
                "manifest_path": manifest.manifest_path,
                "manifest_error": manifest.manifest_error,
                "workspace_path": str(root),
            },
        )

    if not manifest.manifest_found:
        # A workspace that is not a git checkout at all is a *legitimate*
        # non-repo workspace -- the launcher's own guard calls it
        # ``not-a-repo`` and hands it over as found (a docs-only tree, a
        # scratch directory).  Such a workspace has no declared test suite,
        # and the caller establishes that explicitly.
        #
        # A workspace that *is* a checkout but carries no manifest is a
        # different fact: a partial checkout, or the preflight pointed at
        # the wrong directory.  That is a readiness gap.
        is_checkout = (root / ".git").exists()
        if not is_checkout:
            message = (
                "the workspace path is not a checkout and holds no known "
                "manifest (pyproject.toml, requirements*.txt, package.json, "
                "go.mod), so the declared test suite could not be determined; "
                "this is what a wrong working directory looks like, and it "
                "is not a declaration that the repo has no tests"
            )
        else:
            message = (
                "the workspace is a checkout but carries no known manifest "
                "(pyproject.toml, requirements*.txt, package.json, go.mod), "
                "so its declared test suite could not be determined; a "
                "missing manifest is not a declaration that the repo has no "
                "tests (a partial checkout looks exactly like this)"
            )
        return PreflightReadinessGap(
            code=READINESS_GAP_MANIFEST_MISSING,
            message=message,
            detail={
                "workspace_path": str(root),
                "looks_like_checkout": is_checkout,
                "known_manifests": list(PYTHON_MANIFESTS)
                + list(NODE_MANIFESTS)
                + list(GO_MANIFESTS),
            },
        )

    if manifest.declared_no_tests:
        # The repo itself declares that it ships no tests.  That is a
        # truthful declaration, so there is no gap.
        return None

    if manifest.suite_kind in ("node", "go"):
        runner = "npm test" if manifest.suite_kind == "node" else "go test"
        return PreflightReadinessGap(
            code=READINESS_GAP_UNSUPPORTED_SUITE,
            message=(
                f"the workspace declares a {manifest.suite_kind} test suite "
                f"(manifest: {manifest.manifest_path}), which this preflight "
                f"cannot drive; the declared runner is {runner!r}. The "
                "environment was not verified and no collection was attempted"
            ),
            detail={
                "suite_kind": manifest.suite_kind,
                "manifest_path": manifest.manifest_path,
                "node_test_scripts": list(manifest.node_test_scripts),
                "declared_runner": runner,
            },
        )

    if manifest.suite_kind == "unknown":
        return PreflightReadinessGap(
            code=READINESS_GAP_UNSUPPORTED_SUITE,
            message=(
                "the workspace's manifest does not declare a test suite this "
                "preflight can recognise, so the declared test suite could "
                "not be determined; an unknown suite is not a declaration "
                "that the repo has no tests"
            ),
            detail={
                "suite_kind": manifest.suite_kind,
                "manifest_path": manifest.manifest_path,
            },
        )

    return None


def run_preflight(
    workspace_path: str | Path,
    *,
    candidate_sha: str | None = None,
    allow_env_prep: bool = True,
    allow_install: bool = True,
    require_tool: str | None = None,
    interpreter: str | Path | None = None,
    no_tests_applicable: bool = False,
) -> PreflightRecord:
    """Run the whole preflight for one workspace.

    The record is always returned, even when the gate fails -- a failed
    preflight is a receipt, too, and it is the evidence for the typed
    prerequisite the caller raises.

    ``interpreter`` pins the interpreter the preflight inspects.  It exists
    for callers that already know which interpreter the run will use (and
    for the test suite, whose scenarios must not depend on what the host
    happens to have installed).  When omitted, the interpreter is resolved
    from the workspace.

    ``no_tests_applicable`` is the **explicit** applicability signal: it says
    the task or the profile has already established that this run has no test
    suite to collect.  It is the only way, besides a declaration in the
    repo's own manifest, for an empty collection to be legitimate.  A
    missing manifest is never such a signal.
    """

    root = Path(workspace_path)
    record = PreflightRecord(
        candidate_sha=candidate_sha,
        workspace_path=str(root),
    )

    # 1. The manifest and lock files.
    manifest = read_manifest(root)
    record.env_manifest = manifest.as_dict()

    # 1b. -- amendment-3: can the preflight determine the declared suite at
    #     all?  A missing, unreadable, or unknown manifest is a *readiness
    #     gap*, never "the repo declares no tests".  This is checked before
    #     the interpreter is resolved so a wrong working directory is
    #     reported as what it is instead of being papered over by a healthy
    #     interpreter and an empty collection.
    gap = _readiness_gap_for(manifest, root, no_tests_applicable=no_tests_applicable)
    if gap is not None:
        record.readiness_gap = gap.as_dict()
        # The git baseline is still recorded: the gap is *about* the
        # workspace, and the baseline tells the operator whether the
        # workspace was even a checkout.
        record.git_baseline = git_baseline(root)
        if record.candidate_sha is None:
            record.candidate_sha = record.git_baseline.get("head_sha")
        record.tool_availability = profile_tool_availability()
        # A readiness gap is not a hard prerequisite: the run is not stopped
        # by an owned dependency problem.  But the preflight did not verify
        # anything, so it does not report success either -- ``ok`` stays
        # False and the gap says why.
        record.ok = False
        return record

    # 2. The interpreter the repo declares, and the package set it has.
    if interpreter is not None:
        pinned = Path(interpreter)
        version = _probe_interpreter_version(pinned)
        resolved_pinned = {
            "executable": str(pinned.absolute()) if version else None,
            "version": version,
            "source": "pinned",
            "shared_harness_venv": _is_shared_harness_venv(pinned),
            "in_virtualenv": _probe_venv_membership(pinned) if version else False,
        }
    else:
        resolved_pinned = resolve_interpreter(root)
    record.interpreter = resolved_pinned

    # 3. The git baseline.
    record.git_baseline = git_baseline(root)
    if record.candidate_sha is None:
        record.candidate_sha = record.git_baseline.get("head_sha")

    # 4. The profile tools, truthfully.
    record.tool_availability = profile_tool_availability()

    declared = manifest.declared_test_requirements

    if not resolved_pinned.get("executable"):
        record.prerequisite = PreflightPrerequisite(
            code="no_usable_interpreter",
            message=(
                "no usable interpreter for the workspace's declared test "
                f"environment (ecosystem={manifest.ecosystem or 'unknown'}); "
                "the shared harness virtualenv is never used as a task "
                "environment"
            ),
            detail={"interpreter": resolved_pinned},
        ).as_dict()
        record.ok = False
        return record

    interpreter_path = str(resolved_pinned["executable"])

    # 5. The provenance of that interpreter: where its packages come from.
    #    The check runs before the import check because a resolution that
    #    lands under the shared harness venv is a provenance violation even
    #    when every import happens to succeed.
    provenance = collect_provenance(interpreter_path)
    record.provenance = provenance
    violations = provenance_violations(provenance)
    if violations:
        record.prerequisite = PreflightPrerequisite(
            code="environment_provenance_violation",
            message=(
                "the test environment resolves packages from the shared "
                "harness virtualenv, so it is not workspace-isolated: "
                + "; ".join(violations[:8])
            ),
            detail={
                "violations": violations[:32],
                "provenance": provenance,
                "interpreter": record.interpreter,
            },
        ).as_dict()
        record.ok = False
        return record

    # 6. The dependency-import check for the declared test extras.
    imports = import_check(interpreter_path, declared)
    record.dependency_imports = imports
    missing = [name for name, ok in imports.items() if not ok]

    if missing and allow_env_prep:
        # 7. Workspace-isolated env prep: build ``.venv-task`` from the
        #    manifest and reuse it for both the baseline and the candidate.
        preparation = prepare_workspace_env(
            root, interpreter=interpreter_path, allow_install=allow_install
        )
        record.env_preparation = preparation
        prepared = preparation.get("interpreter")
        if prepared and preparation.get("created"):
            resolved_pinned = resolve_interpreter(root, preferred=prepared)
            record.interpreter = resolved_pinned
            interpreter_path = str(resolved_pinned.get("executable") or prepared)
            # The prepared environment gets its own provenance check: a
            # freshly created venv is exactly where a leaked ``PYTHONPATH``
            # would show up, and the candidate must be as isolated as the
            # baseline.
            provenance = collect_provenance(interpreter_path)
            record.provenance = provenance
            violations = provenance_violations(provenance)
            if violations:
                record.prerequisite = PreflightPrerequisite(
                    code="environment_provenance_violation",
                    message=(
                        "the prepared test environment resolves packages from "
                        "the shared harness virtualenv, so it is not "
                        "workspace-isolated: " + "; ".join(violations[:8])
                    ),
                    detail={
                        "violations": violations[:32],
                        "provenance": provenance,
                        "interpreter": record.interpreter,
                        "env_preparation": record.env_preparation,
                    },
                ).as_dict()
                record.ok = False
                return record
            imports = import_check(interpreter_path, declared)
            record.dependency_imports = imports
            missing = [name for name, ok in imports.items() if not ok]

    if missing:
        record.prerequisite = PreflightPrerequisite(
            code="missing_declared_test_dependencies",
            message=(
                "declared test dependencies are not importable and could not "
                f"be installed from the repo's own manifest: {', '.join(missing)}"
            ),
            detail={
                "missing": missing,
                "declared_test_requirements": declared,
                "env_preparation": record.env_preparation,
                "interpreter": record.interpreter,
            },
        ).as_dict()
        record.ok = False
        return record

    record.installed_packages = installed_packages(interpreter_path)

    # 8. The collection smoke.  Zero collected is a failure unless the repo
    #    explicitly declares that it has no tests.  The collection runs under
    #    the sanitized environment, so the tests it finds are the tests a
    #    real run would find.
    collection = collect_tests(interpreter_path, root)
    record.collection = collection.as_dict()
    if collection.collected <= 0 and manifest.has_tests and not manifest.declared_no_tests:
        record.prerequisite = PreflightPrerequisite(
            code="no_tests_collected",
            message=(
                "the declared test suite collected 0 tests, so the workspace's "
                "test environment cannot be verified; a skipped collection is "
                "never a healthy environment"
            ),
            detail={"collection": collection.as_dict()},
        ).as_dict()
        record.ok = False
        return record
    if collection.collected <= 0 and manifest.declared_no_tests:
        # The repo itself declares that it ships no tests.  That is an
        # explicit declaration, so an empty collection is legitimate here.
        record.collection["ok"] = True
        record.collection["detail"] = "the repo declares no tests"
    elif collection.collected <= 0 and no_tests_applicable:
        # The task/profile explicitly established that this run has no test
        # suite.  Same treatment as an explicit repo declaration: the empty
        # collection is legitimate, and it is recorded as such rather than
        # silently reported as a healthy collection.
        record.collection["ok"] = True
        record.collection["detail"] = "the task declares no tests"
    elif collection.collected <= 0:
        # -- amendment-3: neither an explicit declaration nor an explicit
        # applicability, yet nothing was collected.  This is *not* "the repo
        # declares no tests": the preflight did not establish that, so it
        # must not report a healthy environment.  It is a readiness gap.
        #
        # Note that this branch is reached only when a manifest *was* found
        # and *was* read (the missing/unreadable manifest cases returned
        # earlier), so the gap here is about the declared suite specifically.
        record.readiness_gap = PreflightReadinessGap(
            code=READINESS_GAP_UNSUPPORTED_SUITE,
            message=(
                "the workspace's manifest declares no test suite that could "
                "be collected, so the test environment could not be verified; "
                "an empty collection with no explicit no-tests declaration is "
                "a readiness gap, not a healthy environment"
            ),
            detail={
                "collection": collection.as_dict(),
                "manifest_path": manifest.manifest_path,
                "suite_kind": manifest.suite_kind,
            },
        ).as_dict()
        record.ok = False
        return record

    # 8. A required profile tool that is not available.
    if require_tool:
        entry = record.tool_availability.get(require_tool) or {}
        if not entry.get("available"):
            record.prerequisite = PreflightPrerequisite(
                code="profile_tool_unavailable",
                message=(
                    f"the required profile tool {require_tool!r} is not "
                    f"available in this workspace; alternate route: "
                    f"{entry.get('alternate_route') or GRAPHIFY_ALTERNATE_ROUTE}"
                ),
                detail={"tool": require_tool, "availability": entry},
            ).as_dict()
            record.ok = False
            return record

    return record


def receipt_from_record(record: PreflightRecord) -> dict[str, Any]:
    """Project a record into the flat receipt shape the overseer reads.

    The receipt carries the five fields the task contract names -- candidate
    SHA, env manifest, interpreter, collected count, and tool availability --
    in one flat mapping, so both the run-dir file and the snapshot field
    carry the same shape.
    """

    payload = record.as_dict()
    payload["collected"] = int(record.collection.get("collected", 0) or 0)
    payload["candidate_sha"] = record.candidate_sha
    # -- amendment-3: the readiness gap is projected with the same fields as
    # the prerequisite so a reader can tell the two apart by their ``kind``.
    gap = record.readiness_gap
    payload["readiness_gap"] = dict(gap) if gap else None
    return payload


def write_receipt(path: str | Path, payload: dict[str, Any]) -> None:
    """Write the run-dir preflight receipt as JSON."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
