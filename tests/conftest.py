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
