"""The RLIMIT_AS fallback path of per-run worker memory isolation (TASK 202).

On hosts without a reachable systemd manager the worker must still be
memory-bounded: ``prepare_launch`` resolves to the ``rlimit-as`` mechanism,
wraps nothing, and hands the child a ``preexec_fn`` that caps ``RLIMIT_AS``.
Only this fallback is exercised here; the systemd scope path needs a live
manager and is covered on the host, not in the unit suite.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest  # noqa: F401  (pytest parametrize)

# Exercise the checkout's own src tree, not whatever lh_harness copy a host
# venv may have installed: a non-editable install predates worker_isolation.
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src")
)

from lh_harness import worker_isolation  # noqa: E402
from lh_harness.worker_isolation import (  # noqa: E402
    DEFAULT_WORKER_MEMORY_MAX,
    ENV_WORKER_MEMORY_MAX,
    MECHANISM_RLIMIT,
    prepare_launch,
    rlimit_plan,
    to_bytes,
)


_PROBE_NONE = lambda: None  # noqa: E731


def test_fallback_mechanism_when_no_systemd_run() -> None:
    plan = prepare_launch(
        command=["/bin/true", "--flag", "x"],
        run_id="run-fallback",
        memory_max="3G",
        probe=_PROBE_NONE,
    )

    assert plan.mechanism == MECHANISM_RLIMIT
    # The bare worker argv is launched unchanged (no systemd-run wrapper).
    assert plan.command == ["/bin/true", "--flag", "x"]
    assert plan.preexec is not None
    assert plan.unit == ""
    record = plan.record()
    assert record == {
        "mechanism": MECHANISM_RLIMIT,
        "limit": "3G",
        "unit": "",
        "cgroup": "",
        "oom_kill_base": None,
    }


def test_fallback_plan_is_the_scope_retry_shape() -> None:
    plan = rlimit_plan("512M", ["python", "-m", "lh_harness.worker"])

    assert plan.mechanism == MECHANISM_RLIMIT
    assert plan.limit == "512M"
    assert plan.command == ["python", "-m", "lh_harness.worker"]
    assert plan.preexec is not None


@pytest.mark.parametrize(
    ("explicit", "env", "config", "expected"),
    [
        (None, None, None, DEFAULT_WORKER_MEMORY_MAX),
        (None, "512M", "3G", "512M"),  # env override beats config
        (None, None, "768M", "768M"),  # config beats the default
        ("1G", "512M", "3G", "1G"),  # explicit argument wins overall
    ],
)
def test_fallback_limit_resolution_precedence(explicit, env, config, expected) -> None:
    # The supervisor feeds os.environ.get(ENV_WORKER_MEMORY_MAX) into env.
    assert (
        worker_isolation.resolve_memory_limit(explicit=explicit, env=env, config=config)
        == expected
    )


def test_fallback_applies_rlimit_as_in_the_child() -> None:
    """The preexec really caps RLIMIT_AS, and the child inherits it."""

    limit_bytes = to_bytes("64M")
    probe = textwrap.dedent(
        """
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        print(soft, hard)
        """
    )
    plan = rlimit_plan("64M", [sys.executable, "-c", probe])

    result = subprocess.run(
        plan.command,
        preexec_fn=plan.preexec,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ},
        check=True,
    )
    soft, hard = (int(value) for value in result.stdout.split())
    assert soft == limit_bytes
    assert hard > soft  # the hard limit is not lowered by the fallback