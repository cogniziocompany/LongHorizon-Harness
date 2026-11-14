"""Default TestClient Host to loopback so Host-header hardening stays on."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urljoin

# TASK 211: put this checkout's own src tree ahead of any installed lh_harness
# copy BEFORE any test module imports it.  conftest is imported before every
# test module, so once this runs, ``import lh_harness`` resolves here even
# when a host site-packages copy was loaded first (a PYTHONPATH/venv shadow):
# sys.modules is already primed, and a later test-file sys.path.insert could
# not retarget it — which made whole-suite collection fail with ImportError
# whenever the installed copy predated the newest worker_isolation API (seen
# live: ``ensure_service_relocated`` missing from the deployed venv copy).
_SRC = str((Path(__file__).resolve().parent.parent / "src").resolve())
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
# A stale site-packages copy can still win if the ambient PYTHONPATH lists one
# AFTER our insertion point, or if ``lh_harness`` was imported before conftest
# ran (e.g. pytest plugins).  Retarget any already-primed modules to this
# checkout so the suite always tests the tree being run.
if "lh_harness" in sys.modules:
    _primed = getattr(sys.modules["lh_harness"], "__file__", "") or ""
    if Path(_primed).resolve() != Path(_SRC) / "lh_harness" / "__init__.py":
        for _name in [n for n in sys.modules if n == "lh_harness" or n.startswith("lh_harness.")]:
            del sys.modules[_name]
elif [
    p
    for p in sys.path
    if p
    and "site-packages" in p
    and Path(p).resolve() == Path(_SRC).resolve().parent
]:
    pass  # src tree itself, nothing to do
else:
    for _p in [
        p
        for p in sys.path
        if p
        and "site-packages" in p
        and (Path(p) / "lh_harness").exists()
        and Path(p).resolve() != Path(_SRC).resolve().parent
    ]:
        sys.path.remove(_p)

# TASK 211: a test run launched from inside a harness run inherits the
# service's own fleet-reporter environment (LH_HARNESS_FLEET_URL/KEY/...).
# With it set, dashboard state emits ``approval_created`` events that the
# exact-sequence assertions in the webapi suite never saw when the suite was
# written, and the suite fails everywhere the service actually runs.  Tests
# that exercise the fleet reporter set their own environment.
import os as _os
import tempfile as _tempfile

for _var in ("LH_HARNESS_FLEET_URL", "LH_HARNESS_FLEET_KEY", "LH_HARNESS_FLEET_NODE", "LH_HARNESS_FLEET_LABELS"):
    _os.environ.pop(_var, None)

# easybutt0n-17: workspace-neutral test execution.  Auditors validate this
# tree by RUNNING the suite, and a run that writes bytecode caches into
# src/**/__pycache__ invalidates the read-only audit with its own mutation
# (it happened twice in a row).  Two layers:
#   1. ``sys.dont_write_bytecode`` stops THIS interpreter; conftest is
#      imported before any test module, so no harness module import can
#      write a cache afterwards, however pytest was launched.
#   2. ``PYTHONDONTWRITEBYTECODE`` in os.environ reaches every child
#      interpreter whose env is derived from the parent's (helpers that
#      build a "clean" env strip only LH_HARNESS_* variables).
# ``_workspace_neutral_bytecode_scrub`` below is the backstop for the one
# remaining shape: a child python launched with an env built from scratch.
sys.dont_write_bytecode = True
_os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

import fastapi.testclient as fastapi_testclient
import starlette.testclient as starlette_testclient

_Orig = starlette_testclient.TestClient
_Upgrade = starlette_testclient._Upgrade


class LoopbackTestClient(_Orig):
    def __init__(self, app, *args, **kwargs):
        kwargs.setdefault("base_url", "http://127.0.0.1")
        super().__init__(app, *args, **kwargs)

    def websocket_connect(self, url, subprotocols=None, **kwargs):
        # Starlette hardcodes ws://testserver; rewrite so Host stays loopback.
        url = urljoin("ws://127.0.0.1", url)
        headers = kwargs.get("headers", {})
        headers.setdefault("connection", "upgrade")
        headers.setdefault("sec-websocket-key", "testserver==")
        headers.setdefault("sec-websocket-version", "13")
        if subprotocols is not None:
            headers.setdefault("sec-websocket-protocol", ", ".join(subprotocols))
        kwargs["headers"] = headers
        try:
            super().request("GET", url, **kwargs)
        except _Upgrade as exc:
            return exc.session
        raise RuntimeError("Expected WebSocket upgrade")


starlette_testclient.TestClient = LoopbackTestClient
fastapi_testclient.TestClient = LoopbackTestClient


import pytest as _pytest_task300


@_pytest_task300.fixture(autouse=True)
def _queue_requester_legacy_default(monkeypatch):
    """Task 300: run the pre-existing suite in requester ``legacy`` mode.

    Strict mode (the production default) requires a ``requester`` block on
    every enqueue.  The many existing enqueue call sites predate it, so the
    suite defaults to the documented rollout flag; legacy mode synthesizes the
    block and leaves every other behaviour unchanged, which is itself the
    backward-compatibility check.  Tests that exercise strict mode override this
    with ``monkeypatch.setenv("LH_HARNESS_QUEUE_REQUESTER", "strict")``.
    """
    monkeypatch.setenv("LH_HARNESS_QUEUE_REQUESTER", "legacy")


@_pytest_task300.fixture()
def host_independent_worker_isolation(monkeypatch):
    """Pin worker memory isolation to the log-only plan, whatever the host.

    The supervisor unit tests stub ``subprocess.Popen`` with fake processes
    (a pid and ``poll()``), but ``RunSupervisor`` still asks
    ``worker_isolation.prepare_launch`` how to bound the worker, and that
    probes the HOST: on a machine with ``systemd-run`` and a reachable systemd
    manager (GitHub's ubuntu runners, VM 211, most dev boxes) it picks the
    ``systemd-run --scope`` plan.  The fake pid never shows up in the scope's
    cgroup, so the supervisor settles the "launcher" through
    ``worker_isolation.await_scope_exit`` -> ``process.wait(timeout=...)`` and
    the tests die with ``AttributeError: ... has no attribute 'wait'`` (or, for
    a fake with a non-zero return code, try to ``kill()`` it and relaunch).
    On a host without systemd the same tests take the unbounded plan and pass,
    which is why they were green where they were written and red in CI.

    These tests are about run lifecycle, not memory isolation (that has its
    own suites: test_worker_isolation_fallback.py, test_worker_rss_bound.py,
    supervisor/test_memory_kill_reason.py), so make the isolation decision
    deterministic: no systemd scope, no delegated cgroup subtree.
    """

    from lh_harness import worker_isolation

    monkeypatch.setattr(worker_isolation, "probe_systemd_run", lambda: None)
    monkeypatch.setattr(worker_isolation, "cgroup_subtree_writable", lambda *a, **k: False)


@_pytest_task300.fixture(autouse=True)
def _agent_availability_is_host_independent(monkeypatch):
    """Make the pre-spawn agent check independent of what this host installed.

    The supervisor refuses a launch whose role names an agent with no worker
    on this node (easybutt0n-238).  That question is answered by resolving
    the agent's CLI on PATH, so it is a HOST question -- exactly like the
    systemd probe above.  A developer box or CI runner that never installed
    the Codex/DSH/OpenCode CLIs would otherwise refuse every run that names
    one of them, and the tests that stub ``subprocess.Popen`` (they assert the
    worker's COMMAND, never the agent's CLI) would fail for a reason that has
    nothing to do with what they test.

    The default here is "every known agent has a worker": the check is
    exercised on purpose by the tests that care about it, which stub the
    registry's resolution to report a missing binary.
    """

    from lh_harness import agent_registry

    monkeypatch.setattr(agent_registry, "resolve_agent_binary", lambda binary: f"/usr/bin/{binary}")


@_pytest_task300.fixture(autouse=True)
def _auditor_verification_budget_state_isolated(monkeypatch, tmp_path):
    """easybutt0n-17: isolate the auditor verifier's step-budget counter.

    The episode-scoped verification-step budget persists its counter in a
    state directory.  Inside one pytest process there is no auditor episode
    boundary, so without isolation every test would share the ambient state
    directory and the ambient episode id, and the budget would leak across
    tests (and across hosts that have run the verifier before).  Each test
    gets a fresh state directory and no inherited episode id -- exactly the
    one-auditor-episode, three-structured-passes world the harness runs.
    """

    monkeypatch.setenv(
        "LH_HARNESS_AUDITOR_VERIFY_STATE_DIR",
        str(tmp_path / "auditor-verification-budget"),
    )
    monkeypatch.delenv("LH_HARNESS_AUDITOR_EPISODE_ID", raising=False)


@_pytest_task300.fixture(autouse=True)
def _auditor_gh_config_dir_isolated(monkeypatch, tmp_path):
    """easybutt0n-17: verifier gh-config scratch dirs stay in the sandbox.

    ``build_auditor_subprocess_env`` points ``gh`` at a fresh, empty config
    directory (so no stored credential can be found) and creates it in the
    system temp directory -- one per call, and nothing ever removes it.
    Every test that builds an auditor child environment therefore dropped
    an ``lh-auditor-gh-config-*`` dir into the real /tmp (15 per run for
    the child-environment suite alone, and they accumulate forever).
    Redirect that fallback to the test's own tmp directory.  Callers that
    pass ``config_dir`` explicitly, and the subprocess-launched verifier
    (whose fail-closed preflight means no directory is ever created), are
    untouched.
    """

    import lh_harness.auditor_env as _auditor_env

    _original = _auditor_env.auditor_gh_config_dir
    _target = tmp_path / "auditor-gh-config"

    def _isolated(config_dir=None):
        return _original(_target if config_dir is None else config_dir)

    monkeypatch.setattr(_auditor_env, "auditor_gh_config_dir", _isolated)


@_pytest_task300.fixture(autouse=True, scope="session")
def _workspace_neutral_bytecode_scrub():
    """easybutt0n-17: the suite leaves the checkout exactly as it found it.

    Backstop for the bytecode suppression above: a child interpreter
    launched with an environment built from scratch misses the propagated
    ``PYTHONDONTWRITEBYTECODE`` and would still drop ``__pycache__``/``*.pyc``
    into the checkout, mutating a tree the auditor only has read-only
    rights to.  Everything the session created that did not pre-exist is
    removed at teardown; caches that existed before the run are left
    untouched, and harness-owned/VCS directories are never descended.
    """

    import shutil as _shutil

    repo = Path(__file__).resolve().parent.parent
    pruned = {".git", ".lh-harness", ".pytest_cache", "node_modules"}

    def _bytecode_paths() -> tuple[set[Path], set[Path]]:
        caches: set[Path] = set()
        loose: set[Path] = set()
        for root, dirnames, filenames in _os.walk(repo):
            if "__pycache__" in dirnames:
                caches.add(Path(root) / "__pycache__")
            dirnames[:] = [
                name
                for name in dirnames
                if name not in pruned and name != "__pycache__"
            ]
            for name in filenames:
                if name.endswith((".pyc", ".pyo")):
                    loose.add(Path(root) / name)
        return caches, loose

    # The two /tmp leak families the previous audits actually saw: verifier
    # gh-config scratch dirs and the shared verification-budget counter
    # dir.  Only entries the session created are removed at its end --
    # pre-existing ones (including any real episode's counter) stay.
    tmp_root = Path(_os.environ.get("TMPDIR") or _tempfile.gettempdir())
    gh_config_prefix = tmp_root / "lh-auditor-gh-config-"

    pre_caches, pre_loose = _bytecode_paths()
    pre_gh_dirs = set(gh_config_prefix.parent.glob(gh_config_prefix.name + "*"))

    yield

    post_caches, post_loose = _bytecode_paths()
    for path in sorted(post_caches - pre_caches, key=lambda p: len(p.parts), reverse=True):
        _shutil.rmtree(path, ignore_errors=True)
    for path in post_loose - pre_loose:
        path.unlink(missing_ok=True)
    for path in set(gh_config_prefix.parent.glob(gh_config_prefix.name + "*")) - pre_gh_dirs:
        _shutil.rmtree(path, ignore_errors=True)
