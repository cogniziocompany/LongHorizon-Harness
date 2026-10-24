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

for _var in ("LH_HARNESS_FLEET_URL", "LH_HARNESS_FLEET_KEY", "LH_HARNESS_FLEET_NODE", "LH_HARNESS_FLEET_LABELS"):
    _os.environ.pop(_var, None)

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

    The session-scope ``_relocate_pytest_into_a_leaf_cgroup`` autouse fixture
    also lives here: when that fixture ran first (pytest session started
    inside a host with a delegated subtree) it set ``_RELOCATED = True`` and
    pointed ``_WORKER_PARENT_CGROUP`` at a real cgroup, which would otherwise
    mask the "no delegated subtree" branch of the fallback routing these
    tests exercise.  Reset both flags so the test sees the documented
    host-independent baseline, and force ``cgroup_plan`` itself to raise so
    the post-scope retry always lands on the log-only plan (some of these
    tests specifically exercise the fallback-after-scope-failure path).
    """

    from lh_harness import worker_isolation

    monkeypatch.setattr(worker_isolation, "probe_systemd_run", lambda: None)
    monkeypatch.setattr(worker_isolation, "cgroup_subtree_writable", lambda *a, **k: False)
    monkeypatch.setattr(worker_isolation, "_RELOCATED", False, raising=False)
    monkeypatch.setattr(worker_isolation, "_WORKER_PARENT_CGROUP", None, raising=False)

    def _raise_cgroup_plan(*args, **kwargs):
        raise OSError("host_independent_worker_isolation: cgroup subtree not delegated")

    monkeypatch.setattr(worker_isolation, "cgroup_plan", _raise_cgroup_plan)


@_pytest_task300.fixture(scope="session", autouse=True)
def _relocate_pytest_into_a_leaf_cgroup():
    """Move the pytest process into a fresh leaf cgroup on session start.

    The RSS-bound tests (``test_worker_rss_bound``) call
    ``worker_isolation._prepare_child_cgroup`` which writes
    ``+memory +pids`` to ``cgroup.subtree_control`` of whatever cgroup the
    pytest process happens to be in - the precondition for ``+memory +pids``
    is that the target cgroup has no resident processes.  When pytest is
    launched inside the lh-harness service unit it lands in a leaf already
    full of worker processes (or shares one with siblings), the write
    returns EBUSY, and the cgroup plan fails.  This fixture creates a
    dedicated ``lh-pytest`` leaf under the service cgroup, moves the pytest
    process into it, and tells ``worker_isolation`` to use the service
    cgroup itself (now empty of pytest) as the parent of any
    ``worker-*`` child cgroups.  The move is best-effort: hosts without a
    delegated cgroup subtree simply skip it (the subtree-availability check
    in ``test_worker_rss_bound`` already does its own honest skip), and we
    never raise here - a misconfigured cgroupfs must not mask the real
    test failures with an autouse-fixture crash.
    """

    import os as _os

    from lh_harness import worker_isolation as _wi

    try:
        current = _wi.own_cgroup_path()
        if not current or not current.startswith("/"):
            return
        root = _wi.CGROUP_ROOT
        current_path = root / current.strip("/")
        if not (current_path / "cgroup.procs").exists():
            return
        leaf_name = f"lh-pytest-{_os.getpid()}"
        service_cgroup = current_path
        while service_cgroup != root:
            ctrl = service_cgroup / "cgroup.subtree_control"
            try:
                if ctrl.exists() and "memory" in ctrl.read_text().split():
                    break
            except Exception:
                pass
            service_cgroup = service_cgroup.parent
        if service_cgroup == root:
            return
        leaf_path = service_cgroup / leaf_name
        try:
            leaf_path.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        try:
            with open(leaf_path / "cgroup.procs", "w", encoding="utf-8") as fh:
                fh.write(f"{_os.getpid()}\n")
        except OSError:
            return
        parent_cgroup = "/" + str(service_cgroup.relative_to(root)).strip("/")
        _wi._RELOCATED = True
        _wi._WORKER_PARENT_CGROUP = parent_cgroup
    except Exception:
        return
