"""Supervisor lifecycle tests run with host-independent worker isolation.

See ``host_independent_worker_isolation`` in ``tests/conftest.py``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _host_independent_worker_isolation(host_independent_worker_isolation):
    yield
