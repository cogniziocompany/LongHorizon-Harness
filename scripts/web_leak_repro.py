#!/usr/bin/env python3
"""Reproduce the CT110 web-app RSS growth + ``/api/queue`` wedge locally.

LHH-WEB-LEAK-FIX deliverable (A): the pre-fix measurement.

Overseer1 observed on CT110 (build add619c9, 0.1.7, 2026-10-25/26) that
``python -m lh_harness web`` RSS grows to multi-GB (3.2 GB @ 39 min, 4.6 GB @
2 h, 5.3 GB @ 2 h 40 min) with 62-66 threads, while ``/api/queue`` and the
dashboard hang (curl 000 after 40-140 s) even though an unauthenticated probe
answers in 5-10 ms.  Those are the operator's numbers; this script measures
the LOCAL equivalent against synthetic fixtures so the growth mechanism is
observable without touching any production node.

What it does, against the REAL app (``lh_harness.webapi.server.create_app``
over a real ``RunSupervisor`` and the file-backed ``QueueStore``, served by a
real uvicorn in a CHILD process so the measured RSS is the web app's own):

1. Synthesizes N fixture run dirs (default 600) laid out exactly as the
   run-boundary helpers expect (``<run>/control/{status,owner}.json`` and
   ``<run>/lh_harness/role_orchestration/{report.json,events.jsonl,
   rounds/round_NNN/...}``) plus M queue entries (default 150) written in the
   store's own JSON format, all inside a temp runs root.  Nothing under the
   repo, and nothing under any harness-owned path, is read or written.
2. Pre-writes ``runs_root/queue/drain.json`` so the embedded Launcher can
   never launch a real worker (safety: the drain is the FIRST eligibility
   check), and strips every network-facing / secret env var from the child so
   the server cannot dial out or write outside the temp root.
3. Drives the poll mix the UI and the fleet window use: GET /api/queue
   (legacy full shape), GET /api/queue?view=summary, GET /api/queue/stats,
   GET /api/runs, GET /api/runs?fields=summary, per-run snapshot reads
   spread across DISTINCT run ids, trajectory reads, a few DELETE
   /api/queue/{id} calls, and GET /api/meta as the control endpoint that must
   stay flat.
4. Samples the web process RSS (VmRSS), thread count and per-endpoint
   latency after each batch and prints a growth table.

A pre-fix run is expected to show GROWING RSS and GROWING latency; the
post-fix run of the same script must stay bounded.

Usage:
    python scripts/web_leak_repro.py --runs 600 --queue 150 --batches 8 \
        --polls-per-batch 25

Run from the repo root with the interpreter that has this checkout's
dependencies.  The child server inherits this script's interpreter and puts
this checkout's ``src`` first, so the measured app is this tree.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

# The child server process needs the same interpreter this script runs under
# so the measured app and the fixtures share one environment.
_PYTHON = sys.executable

# Env keys stripped from the child server environment.  Fleet reporting must
# stay off (no dial-out), the settings store must not be opened (no writes
# outside the temp root; without a key it stays unconfigured), and the web
# token must be unset so the local server needs no bearer.
_CHILD_STRIP_ENV = (
    "LH_HARNESS_FLEET_URL",
    "LH_HARNESS_FLEET_KEY",
    "LH_HARNESS_FLEET_NODE",
    "LH_HARNESS_FLEET_LABELS",
    "LH_HARNESS_SETTINGS_DB",
    "LH_HARNESS_SETTINGS_KEY",
    "LH_HARNESS_WEB_TOKEN",
    "LH_HARNESS_QA_ROLE",
    "LH_HARNESS_QA_WORKSPACE",
)

# Env keys that are inherited but pinned to safe values.
_CHILD_PINNED_ENV = {
    # The launcher's disk pause reads these; keep the defaults so the drain
    # reason (not a disk pause) is what pending entries accumulate.
    "LH_HARNESS_DISK_PAUSE_PERCENT": "95",
    "LH_HARNESS_DISK_RESUME_PERCENT": "90",
    "LH_HARNESS_DISK_WARN_PERCENT": "85",
}

# Roles used by the fixture runs, mirroring a three-role harness run.
_ROLES = ("manager", "executor", "auditor")

# Bytes per per-role normalized trajectory in a fixture run.  The real
# operator's Codex lead wrote multi-MB trajectories per role; the read path
# parses whole lines under a 16 MiB bound, so stay inside that bound.
#
# The default is deliberately modest (32 KiB, not 256 KiB): with 500+ run
# dirs the fixture tree is what dominates this script's wall clock, and the
# leak being reproduced is driven by the NUMBER of run dirs and the per-run
# metadata read, not by the trajectory payload size.  ``--trajectory-bytes``
# raises it when a payload-size-focused run is wanted.
_TRAJECTORY_BYTES = 32 * 1024

# Bytes per per-role *text* artifact (executor_output.txt, auditor_report.txt,
# ...).  The operator's dashboard lead (run 9437a8e1) reported ~4.4 MB for a
# single executor role output.  The dashboard's per-round text reads are
# bounded by ``_MAX_ROUND_TEXT_BYTES`` (512 KiB) in
# ``lh_harness/dashboard/state.py``, so a real 4.4 MB role output is already
# truncated by the reader; the default here reproduces that truncated size.
_ROLE_TEXT_BYTES = 64 * 1024

# Rounds per fixture run.  Each round contributes one set of per-role text
# artifacts to the snapshot, so this multiplies the per-request payload.
_ROUNDS_PER_RUN = 4


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def _proc_rss_kb(pid: int) -> int:
    """Return the process RSS in KiB, read from ``/proc/<pid>/status``."""
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _proc_threads(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("Threads:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _make_run(
    root: Path,
    run_id: str,
    *,
    status: str,
    task: str,
    index: int,
    rounds: int = _ROUNDS_PER_RUN,
    role_text_bytes: int = _ROLE_TEXT_BYTES,
    trajectory_bytes: int = _TRAJECTORY_BYTES,
) -> None:
    """One run directory in the canonical layout the boundary helpers accept.

    ``control/status.json`` + ``control/owner.json`` feed the supervisor's
    lifecycle projection; ``lh_harness/role_orchestration`` holds the report,
    the events ledger, and per-round artifacts including per-role normalized
    trajectories, so a snapshot read does realistic bounded work.
    """

    run_dir = root / run_id
    _write_json(
        run_dir / "control" / "status.json",
        {"status": status, "mtime": 1234.0, "managed": True},
    )
    _write_json(
        run_dir / "control" / "owner.json",
        {
            "task": task,
            "workspace": f"/tmp/lh-repro-ws/{run_id}",
            "agent": "claude_code",
            "model": "glm-5.3",
            "max_rounds": 12,
        },
    )
    logs = run_dir / "lh_harness" / "role_orchestration"
    rounds_root = logs / "rounds"
    rounds_root.mkdir(parents=True)
    _write_json(
        logs / "report.json",
        {
            "task": task,
            "status": "running",
            "final_response": "ok",
            "started_at": time.time() - 600,
        },
    )
    events: list[dict[str, Any]] = []
    for round_index in range(1, rounds + 1):
        events.append(
            {
                "schema_version": 1,
                "ts": 100 * round_index + 0.1,
                "event": "manager_round_start",
                "round_index": round_index,
                "run_id": run_id,
                "role": "manager",
            }
        )
        events.append(
            {
                "schema_version": 1,
                "ts": 100 * round_index + 0.9,
                "event": "role_harness_done",
                "round_index": round_index,
                "run_id": run_id,
                "role": "manager",
            }
        )
    with (logs / "events.jsonl").open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")

    # Per-round artifacts.  The per-role payloads the snapshot builder reads
    # (executor_output.txt and friends) are sized here so a snapshot read does
    # a realistic amount of bounded I/O.
    for round_index in range(1, rounds + 1):
        round_dir = rounds_root / f"round_{round_index:03d}"
        round_dir.mkdir(parents=True)
        (round_dir / "manager_plan.txt").write_text(
            f"plan for {run_id} round {round_index}\n" + ("m" * 2048), encoding="utf-8"
        )
        (round_dir / "executor_output.txt").write_text(
            f"executor output for {run_id} round {round_index}\n" + ("e" * role_text_bytes),
            encoding="utf-8",
        )
        (round_dir / "auditor_report.txt").write_text(
            f"auditor report for {run_id} round {round_index}\n" + ("a" * role_text_bytes),
            encoding="utf-8",
        )
        for role in _ROLES:
            trajectory = round_dir / f"{role}_trajectory.jsonl"
            with trajectory.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps({"role": role, "round": round_index, "kind": "start"}) + "\n")
                written = 0
                while written < trajectory_bytes:
                    chunk = "x" * 1024
                    handle.write(
                        json.dumps({"role": role, "round": round_index, "data": chunk}) + "\n"
                    )
                    written += len(chunk)
                handle.write(json.dumps({"role": role, "round": round_index, "kind": "end"}) + "\n")


def _make_queue_entry(root: Path, queue_id: str, *, index: int) -> None:
    """One durable queue entry in the file store's own JSON shape."""

    now = time.time()
    payload = {
        "queue_id": queue_id,
        "name": f"repro-task-{index:04d}",
        "task": f"synthetic repro task {index} " + ("t" * 4096),
        "workspace": f"/tmp/lh-repro-ws/ws-{index:04d}",
        "max_rounds": 12,
        "trio": "glm",
        "priority": index % 10,
        "requested_by": "repro",
        "status": "pending",
        "run_id": None,
        "reason": None,
        "skip_reasons": [],
        "created_at": now - index,
        "updated_at": now - index,
        "launched_at": None,
        "last_checked_at": None,
        "dedup_key": None,
        "requester": None,
        "retry_of": None,
        "attempt": 1,
        "failure_cause": None,
        "provider_quota": None,
        "provider_rate_limit": None,
        "not_before": None,
        "wait_reason": None,
        "gtm_manager": None,
    }
    _write_json(root / "queue" / f"{queue_id}.json", payload)


def _drain_queue(root: Path) -> None:
    """Write the drain marker so the embedded Launcher never starts a worker."""

    _write_json(
        root / "queue" / "drain.json",
        {"enabled": True, "reason": "web_leak_repro", "since": time.time()},
    )


# ---------------------------------------------------------------------------
# child server
# ---------------------------------------------------------------------------
# LHH-WEB-LEAK-FIX c2 -- MEASUREMENT-ONLY instrumentation.
#
# ``--tracemalloc`` starts tracemalloc inside the CHILD web-server process and
# dumps the top allocation sites (file:line) to the child's stderr when the
# process is asked to stop.  It is diagnostic only: no application behaviour
# changes, no request path is touched, and the app under test is exactly the
# same object as in an uninstrumented run.
_CHILD_BOOTSTRAP = """
import json, os, sys
sys.path.insert(0, {src!r})
from lh_harness.webapi.server import create_app
from lh_harness.supervisor.service import RunSupervisor

runs_root = {runs_root!r}
supervisor = RunSupervisor(runs_root)
app = create_app(
    runs_root=runs_root,
    supervisor=supervisor,
    control_enabled=False,
    bind_host="127.0.0.1",
    bind_port={port},
    probe_open_pr=None,
    caller_configs=None,
)
import uvicorn
uvicorn.run(app, host="127.0.0.1", port={port}, log_level="warning")
"""

# The tracemalloc variant of the bootstrap.  It is a separate string so the
# uninstrumented path stays byte-identical to the pre-c2 bootstrap above.
#
# NOTE: this template is filled with ``%``-style substitution, NOT
# ``str.format``: the dump code below is full of ``{}`` braces, which
# ``str.format`` would try to interpret as replacement fields.
_CHILD_BOOTSTRAP_TRACEMALLOC = """
import json, os, sys
sys.path.insert(0, %(src)r)
from lh_harness.webapi.server import create_app
from lh_harness.supervisor.service import RunSupervisor

# LHH-WEB-LEAK-FIX c2 -- MEASUREMENT-ONLY.  tracemalloc is started BEFORE the
# app is created so the top allocation sites include the app's own import and
# start-up objects.  Nothing else about the app changes.
#
# ``tracemalloc.start(nframe)`` sets the number of STACK FRAMES stored per
# allocation, NOT the number of top sites printed.  The first version of this
# instrumentation passed the top-site count (15) here, which made every
# allocation carry a 15-frame traceback: ``GET /api/runs`` over 300 run dirs
# went from 18.5 s to >300 s (it timed out) and the request path could not be
# polled at all.  One frame is enough to name the allocating line and keeps
# the overhead to roughly 9x.
import tracemalloc
tracemalloc.start(1)

runs_root = %(runs_root)r
supervisor = RunSupervisor(runs_root)
app = create_app(
    runs_root=runs_root,
    supervisor=supervisor,
    control_enabled=False,
    bind_host="127.0.0.1",
    bind_port=%(port)d,
    probe_open_pr=None,
    caller_configs=None,
)
import uvicorn

# The BASELINE marker is taken AFTER the app exists, so the "diff vs start"
# table shows only what the REQUEST PATH allocated.  With the marker taken
# before ``create_app`` the diff was dominated by import-time objects
# (importlib, uvloop, fastapi routing), which say nothing about what grows
# while polling.
_tracemalloc_marker = tracemalloc.take_snapshot()

# LHH-WEB-LEAK-FIX c2 (MEASUREMENT-ONLY): a SECOND marker, taken at the
# midpoint of the poll sequence (SIGUSR2), so the dump can report what the
# SECOND HALF of the polls allocated on top of the first half.  A single
# end-of-run snapshot cannot distinguish "this site is big" from "this site
# GROWS with every poll" -- only a mid-run marker can.
_tracemalloc_marker_mid = None
_tracemalloc_marker_mid_poll = None

def _write_sites(out, stats, limit, size_key, count_key):
    for index, stat in enumerate(stats[:limit], start=1):
        frame = stat.traceback[0]
        out.write("%%3d. %%10d B  %%8d B  %%s:%%s\\n" %% (
            index, getattr(stat, size_key), getattr(stat, count_key),
            frame.filename, frame.lineno))
        for line in stat.traceback.format():
            out.write("       " + line + "\\n")

def _dump_tracemalloc(*_args, **_kwargs):
    global _tracemalloc_marker_mid, _tracemalloc_marker_mid_poll
    snap = tracemalloc.take_snapshot()
    limit = %(trace_limit)d
    out = sys.stderr
    out.write("=== TRACEMALLOC TOP %%d SITES (cumulative, current) ===\\n" %% limit)
    _write_sites(out, snap.statistics("lineno"), limit, "size", "count")
    out.write("=== TRACEMALLOC TOP %%d SITES (cumulative, diff vs start) ===\\n" %% limit)
    diff = snap.compare_to(_tracemalloc_marker, "lineno")
    _write_sites(out, diff, limit, "size_diff", "count_diff")
    # The growth table: what the polls AFTER the midpoint allocated on top of
    # what the polls BEFORE the midpoint had already allocated.  This is the
    # table that names the site that GROWS.
    if _tracemalloc_marker_mid is not None:
        out.write("=== TRACEMALLOC TOP %%d SITES (GROWTH, diff vs poll %%s) ===\\n" %% (
            limit, _tracemalloc_marker_mid_poll))
        growth = snap.compare_to(_tracemalloc_marker_mid, "lineno")
        _write_sites(out, growth, limit, "size_diff", "count_diff")
    out.write("=== END TRACEMALLOC ===\\n")
    out.flush()

import atexit
import signal
atexit.register(_dump_tracemalloc)
def _on_sigusr1(*_args):
    # SIGUSR1 is not used by uvicorn, so this handler always runs and the
    # dump is written to stderr while the server is still serving.
    _dump_tracemalloc()
def _on_sigusr2(*_args):
    # SIGUSR2 marks the midpoint of the poll sequence: it takes the second
    # marker and then dumps, so the dump carries the GROWTH table.
    global _tracemalloc_marker_mid, _tracemalloc_marker_mid_poll
    _tracemalloc_marker_mid = tracemalloc.take_snapshot()
    _tracemalloc_marker_mid_poll = %(midpoint_poll)r
    _dump_tracemalloc()
try:
    signal.signal(signal.SIGUSR1, _on_sigusr1)
    signal.signal(signal.SIGUSR2, _on_sigusr2)
except Exception:
    pass
uvicorn.run(app, host="127.0.0.1", port=%(port)d, log_level="warning")
"""


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _CHILD_STRIP_ENV:
        env.pop(key, None)
    env.update(_CHILD_PINNED_ENV)
    env["PYTHONPATH"] = str(_REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _start_server(
    runs_root: Path,
    port: int,
    *,
    tracemalloc: bool = False,
    trace_limit: int = 15,
    stderr_path: Path | None = None,
    midpoint_poll: int = 0,
) -> subprocess.Popen[bytes]:
    if tracemalloc:
        code = _CHILD_BOOTSTRAP_TRACEMALLOC % {
            "src": str(_REPO / "src"),
            "runs_root": str(runs_root),
            "port": port,
            "trace_limit": trace_limit,
            "midpoint_poll": midpoint_poll,
        }
    else:
        code = _CHILD_BOOTSTRAP.format(
            src=str(_REPO / "src"), runs_root=str(runs_root), port=port
        )
    stderr_target: Any = subprocess.DEVNULL
    stderr_handle = None
    if stderr_path is not None:
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_handle = stderr_path.open("wb")
        stderr_target = stderr_handle
    proc = subprocess.Popen(
        [_PYTHON, "-c", code],
        env=_child_env(),
        stdout=subprocess.DEVNULL,
        stderr=stderr_target,
    )
    # Keep the handle alive for the process lifetime; the caller closes it.
    proc._repro_stderr_handle = stderr_handle  # type: ignore[attr-defined]
    return proc


def _wait_for_port(port: int, proc: subprocess.Popen[bytes], timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


# ---------------------------------------------------------------------------
# request helpers
# ---------------------------------------------------------------------------
def _get(port: int, path: str, timeout: float = 30.0) -> tuple[int, int, float]:
    """Return ``(status, body_bytes, elapsed_seconds)`` for one GET.

    A timeout is NOT an error here: the wedge this script reproduces is
    exactly a request that stops answering, so the timeout IS the
    measurement.  It is reported as status 0 with the elapsed time.
    """

    url = f"http://127.0.0.1:{port}{path}"
    request = urllib.request.Request(url, method="GET")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status = int(exc.code)
    except Exception:  # noqa: BLE001 - a hang/timeout is a measurement
        status, body = 0, b""
    return status, len(body), time.monotonic() - started


def _delete(port: int, path: str, timeout: float = 30.0) -> int:
    url = f"http://127.0.0.1:{port}{path}"
    request = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
            return int(response.status)
    except urllib.error.HTTPError as exc:
        exc.read()
        return int(exc.code)


# ---------------------------------------------------------------------------
# the measurement
# ---------------------------------------------------------------------------
def build_fixtures(
    runs_root: Path,
    run_count: int,
    queue_count: int,
    *,
    rounds: int = _ROUNDS_PER_RUN,
    role_text_bytes: int = _ROLE_TEXT_BYTES,
    trajectory_bytes: int = _TRAJECTORY_BYTES,
) -> list[str]:
    """Create the synthetic run dirs and queue entries; return the run ids."""

    runs_root.mkdir(parents=True, exist_ok=True)
    (runs_root / "queue").mkdir(parents=True, exist_ok=True)
    run_ids: list[str] = []
    for index in range(run_count):
        run_id = f"repro{index:06x}run"
        status = "running" if index % 3 else "completed"
        _make_run(
            runs_root,
            run_id,
            status=status,
            task=f"synthetic repro task {index}",
            index=index,
            rounds=rounds,
            role_text_bytes=role_text_bytes,
            trajectory_bytes=trajectory_bytes,
        )
        run_ids.append(run_id)
    for index in range(queue_count):
        _make_queue_entry(runs_root, f"q{index:06d}", index=index)
    _drain_queue(runs_root)
    return run_ids


def drive_traffic(
    port: int,
    run_ids: list[str],
    *,
    batches: int,
    polls_per_batch: int,
    queue_ids: list[str],
    timeout: float = 30.0,
    include_full_runs: bool = True,
    poll_mix: str = "full",
    midpoint_poll: int = 0,
    server_pid: int = 0,
    rss_sampler: Any = None,
    poll_rows: list[dict[str, Any]] | None = None,
    on_poll: Any = None,
) -> list[dict[str, Any]]:
    """Drive the poll mix the dashboard/fleet use; return per-batch rows.

    LHH-WEB-LEAK-FIX c2 (MEASUREMENT-ONLY): ``rss_sampler`` is called after
    every completed poll round and ``poll_rows`` collects one row per poll
    round, so the RSS-vs-poll-count curve has one sample per poll rather than
    one per batch.  Neither argument changes what is requested.

    ``poll_mix`` selects WHICH endpoints a poll round hits:

    ``"full"``
        the original dashboard/fleet mix (eight endpoints per round).
    ``"c2"``
        exactly the four endpoints the c2 request names -- ``GET /api/runs``,
        ``GET /api/runs?fields=summary``, ``GET /api/runs/<id>/snapshot`` and
        ``GET /api/queue``.  The other four (queue summary/stats, events,
        trajectory, meta) are dropped because under tracemalloc they dominate
        the wall clock and are not what the c2 request measures.  It is the
        same four request paths, polled the same number of times.
    """

    rows: list[dict[str, Any]] = []
    poll_index = 0
    for batch in range(batches):
        latencies: dict[str, list[float]] = {}
        statuses: dict[str, int] = {}

        def record(label: str, status: int, elapsed: float, size: int) -> None:
            latencies.setdefault(label, []).append(elapsed)
            statuses[f"{label}:{status}"] = statuses.get(f"{label}:{status}", 0) + 1

        # The poll mix: queue list in its legacy full shape, the summary
        # projection, the fleet stats window, the run list (full + cheap), and
        # a per-run snapshot read on a DISTINCT run id every time so the
        # snapshot cache cannot serve the same entry twice in a row.
        for _ in range(polls_per_batch):
            # LHH-WEB-LEAK-FIX c2 (MEASUREMENT-ONLY): the "c2" mix polls
            # exactly the four endpoints the c2 request names.  The other
            # four are dropped for this mix only.
            if poll_mix == "c2":
                queue_paths = (("queue_full", "/api/queue"),)
            else:
                queue_paths = (
                    ("queue_full", "/api/queue"),
                    ("queue_summary", "/api/queue?view=summary&limit=50"),
                    ("queue_stats", "/api/queue/stats"),
                )
            for label, path in queue_paths:
                status, size, elapsed = _get(port, path, timeout=timeout)
                record(label, status, elapsed, size)

            status, size, elapsed = _get(port, "/api/runs?fields=summary", timeout=timeout)
            record("runs_summary", status, elapsed, size)

            # The full run-list projection is the O(runs^2) path.  It is the
            # wedge; it is polled once per batch so the growth trend is
            # visible without a single request hanging the whole phase.
            if include_full_runs:
                status, size, elapsed = _get(port, "/api/runs", timeout=timeout)
                record("runs_full", status, elapsed, size)

            # LHH-WEB-LEAK-FIX c2 (MEASUREMENT-ONLY): rotate the run id on
            # EVERY poll so the snapshot cache cannot serve the same entry
            # twice in a row.  The previous expression used ``len(latencies)``,
            # which is CONSTANT within a batch (one entry per distinct
            # endpoint label), so it picked the same run id for every poll in
            # the batch and the per-run snapshot path was barely exercised.
            run_id = run_ids[
                (batch * polls_per_batch + poll_index) % len(run_ids)
            ]
            status, size, elapsed = _get(
                port, f"/api/runs/{run_id}/snapshot", timeout=timeout
            )
            record("snapshot", status, elapsed, size)

            # LHH-WEB-LEAK-FIX c2 (MEASUREMENT-ONLY): the events, trajectory
            # and control endpoints are part of the "full" mix only.
            if poll_mix != "c2":
                status, size, elapsed = _get(
                    port, f"/api/runs/{run_id}/events?limit=50", timeout=timeout
                )
                record("events", status, elapsed, size)

                role = _ROLES[batch % len(_ROLES)]
                status, size, elapsed = _get(
                    port, f"/api/runs/{run_id}/trajectory/{role}", timeout=timeout
                )
                record("trajectory", status, elapsed, size)

                # The control endpoint: it must stay flat.  On CT110 the
                # unauthenticated probe answered in 5-10 ms while /api/queue hung.
                status, size, elapsed = _get(port, "/api/meta", timeout=timeout)
                record("control_meta", status, elapsed, size)

            # LHH-WEB-LEAK-FIX c2 (MEASUREMENT-ONLY): one RSS sample per
            # completed poll round.
            poll_index += 1
            # LHH-WEB-LEAK-FIX c2 (MEASUREMENT-ONLY): at the MIDPOINT of the
            # poll sequence, ask the child (SIGUSR2) to take its second
            # tracemalloc marker.  The final dump then reports what the polls
            # after the midpoint grew, which is what names the leaking site.
            if midpoint_poll and poll_index == midpoint_poll and server_pid:
                try:
                    os.kill(server_pid, signal.SIGUSR2)
                except OSError:
                    pass
            if rss_sampler is not None:
                rss_kb, threads = rss_sampler()
                row = {
                    "poll": poll_index,
                    "batch": batch,
                    "rss_kb": rss_kb,
                    "threads": threads,
                    "snapshot_ms": latencies.get("snapshot", [0.0])[-1] * 1000,
                    "runs_full_ms": latencies.get("runs_full", [0.0])[-1] * 1000,
                    "runs_summary_ms": latencies.get("runs_summary", [0.0])[-1] * 1000,
                    "queue_full_ms": latencies.get("queue_full", [0.0])[-1] * 1000,
                }
                if on_poll is not None:
                    on_poll(row)
                elif poll_rows is not None:
                    poll_rows.append(row)

        # A few queue mutations, like the dashboard's "cancel" button.
        for queue_id in queue_ids[batch : batch + 2]:
            status = _delete(port, f"/api/queue/{queue_id}")
            statuses[f"delete:{status}"] = statuses.get(f"delete:{status}", 0) + 1

        rows.append(
            {
                "batch": batch,
                "latencies": {k: sum(v) / len(v) for k, v in latencies.items()},
                "max_latency": {k: max(v) for k, v in latencies.items()},
                "statuses": statuses,
            }
        )
    return rows


def drive_concurrent(
    port: int,
    run_ids: list[str],
    *,
    workers: int,
    rounds: int,
    timeout: float,
) -> dict[str, Any]:
    """Drive the slow paths from many threads at once.

    CT110's wedge was a *concurrency* failure: the dashboard, the fleet
    gateway and the overseer all poll the same slow endpoints, so dozens of
    full run-dir scans were in flight together.  Each in-flight request holds
    a fully materialized snapshot (every per-role payload) in memory, so the
    concurrent case is the one that grows RSS, and it is the one that
    exhausts the server's thread pool so a fresh ``/api/queue`` request
    queues behind the scans already running.
    """

    results: list[tuple[str, int, int, float, str]] = []
    lock = threading.Lock()
    counter = {"n": 0}
    counter_lock = threading.Lock()

    def worker(index: int) -> None:
        for round_index in range(rounds):
            with counter_lock:
                counter["n"] += 1
                seq = counter["n"]
            run_id = run_ids[(index * 977 + round_index) % len(run_ids)]
            # Alternate the two slow paths: the full run list (O(runs) scan
            # per request) and the per-run snapshot (reads every per-role
            # payload for the run).
            if seq % 2 == 0:
                path = "/api/runs"
            else:
                path = f"/api/runs/{run_id}/snapshot"
            started = time.monotonic()
            try:
                status, size, elapsed = _get(port, path, timeout=timeout)
                outcome = ""
            except Exception as exc:  # noqa: BLE001 - a hang IS the measurement
                status, size, elapsed = 0, 0, time.monotonic() - started
                outcome = f"{type(exc).__name__}: {exc}"[:160]
            with lock:
                results.append((path.split("?")[0], status, size, elapsed, outcome))

    threads = [
        threading.Thread(target=worker, args=(index,), daemon=True)
        for index in range(workers)
    ]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.monotonic() - started

    failures = [row for row in results if row[1] == 0 or row[4]]
    latencies = sorted(row[3] for row in results)
    return {
        "requests": len(results),
        "failures": failures,
        "wall_seconds": elapsed,
        "latencies": latencies,
        "max_latency": latencies[-1] if latencies else 0.0,
        "median_latency": latencies[len(latencies) // 2] if latencies else 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=520, help="Fixture run dirs to create.")
    parser.add_argument("--queue", type=int, default=120, help="Fixture queue entries to create.")
    parser.add_argument("--batches", type=int, default=4, help="Measurement batches.")
    parser.add_argument(
        "--polls-per-batch",
        type=int,
        default=4,
        help=(
            "Poll rounds per batch.  Kept small by default: at 500+ run dirs "
            "a single full run-list request takes minutes pre-fix, so a big "
            "poll count would not finish inside the reproduction's budget."
        ),
    )
    parser.add_argument(
        "--concurrent-workers",
        type=int,
        default=0,
        help="Threads driving the slow paths at once (0 = skip the concurrent phase).",
    )
    parser.add_argument(
        "--concurrent-rounds",
        type=int,
        default=4,
        help="Slow-path requests per concurrent worker.",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=30.0,
        help=(
            "Per-request timeout in seconds.  Kept short on purpose: the "
            "pre-fix wedge makes a single full run-list request take minutes "
            "at 500+ run dirs, and a timeout IS the measurement."
        ),
    )
    parser.add_argument(
        "--wedge-timeout",
        type=float,
        default=180.0,
        help="Timeout for the dedicated wedge measurement of the full run list.",
    )
    parser.add_argument(
        "--keep", action="store_true", help="Keep the temp fixtures (debugging)."
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=_ROUNDS_PER_RUN,
        help="Rounds per fixture run (each round adds one set of per-role payloads).",
    )
    parser.add_argument(
        "--role-text-bytes",
        type=int,
        default=_ROLE_TEXT_BYTES,
        help="Bytes per per-role text artifact (executor_output.txt etc.).",
    )
    parser.add_argument(
        "--trajectory-bytes",
        type=int,
        default=_TRAJECTORY_BYTES,
        help="Bytes per per-role normalized trajectory.",
    )
    # LHH-WEB-LEAK-FIX c2 -- MEASUREMENT-ONLY flags.
    parser.add_argument(
        "--poll-mix",
        choices=("full", "c2"),
        default="full",
        help=(
            "MEASUREMENT-ONLY: which endpoints one poll round hits. "
            "'c2' polls exactly the four endpoints the c2 request names "
            "(/api/runs, /api/runs?fields=summary, a per-run snapshot and "
            "/api/queue); 'full' is the original eight-endpoint mix."
        ),
    )
    parser.add_argument(
        "--midpoint-poll",
        type=int,
        default=0,
        help=(
            "MEASUREMENT-ONLY: the poll index at which the child takes its "
            "second tracemalloc marker (SIGUSR2).  The final dump then "
            "reports what the polls AFTER this point grew."
        ),
    )
    parser.add_argument(
        "--tracemalloc",
        action="store_true",
        help=(
            "MEASUREMENT-ONLY: run the child web server under tracemalloc and "
            "dump the top allocation sites (file:line) when it exits."
        ),
    )
    parser.add_argument(
        "--tracemalloc-top",
        type=int,
        default=15,
        help="How many allocation sites the tracemalloc dump prints.",
    )
    parser.add_argument(
        "--tracemalloc-out",
        type=Path,
        default=None,
        help="Write the child's tracemalloc dump to this file.",
    )
    parser.add_argument(
        "--rss-out",
        type=Path,
        default=None,
        help="Write the RSS-vs-poll table (CSV) to this file.",
    )
    # NOTE: RSS is sampled after EVERY poll round unconditionally (the
    # ``rss_sampler`` hook in ``drive_traffic``), so the RSS-vs-poll-count
    # curve always has one row per poll.  No flag is needed for that.
    parser.add_argument(
        "--include-full-runs",
        action="store_true",
        help=(
            "Poll the FULL ``GET /api/runs`` projection on every poll round "
            "(not just once per batch).  This is the c2 request path under "
            "test."
        ),
    )
    args = parser.parse_args(argv)

    temp_root = Path(tempfile.mkdtemp(prefix="lh-web-leak-repro-"))
    runs_root = temp_root / "runs"
    port = _free_port()
    proc: subprocess.Popen[bytes] | None = None
    try:
        print(f"[repro] fixtures: {args.runs} run dirs, {args.queue} queue entries")
        print(f"[repro] runs root: {runs_root}")
        started = time.monotonic()
        run_ids = build_fixtures(
            runs_root,
            args.runs,
            args.queue,
            rounds=args.rounds,
            role_text_bytes=args.role_text_bytes,
            trajectory_bytes=args.trajectory_bytes,
        )
        print(f"[repro] fixtures built in {time.monotonic() - started:.1f}s")

        proc = _start_server(
            runs_root,
            port,
            tracemalloc=args.tracemalloc,
            trace_limit=args.tracemalloc_top,
            midpoint_poll=args.midpoint_poll,
            stderr_path=args.tracemalloc_out if args.tracemalloc else None,
        )
        if not _wait_for_port(port, proc):
            print("[repro] server did not come up", file=sys.stderr)
            return 2
        # Let the server settle before the baseline sample.
        time.sleep(1.0)

        baseline_rss = _proc_rss_kb(proc.pid)
        baseline_threads = _proc_threads(proc.pid)
        print(
            f"[repro] baseline: RSS {baseline_rss / 1024:.1f} MiB, "
            f"threads {baseline_threads}"
        )

        queue_ids = [f"q{i:06d}" for i in range(args.queue)]
        # LHH-WEB-LEAK-FIX c2 (MEASUREMENT-ONLY): per-poll RSS sampling.
        poll_rows: list[dict[str, Any]] = []
        # The CSV is written INCREMENTALLY (one row per poll, flushed each
        # time) so a run that is killed by a wall-clock timeout still leaves
        # usable evidence behind.
        rss_handle = None
        if args.rss_out is not None:
            args.rss_out.parent.mkdir(parents=True, exist_ok=True)
            rss_handle = args.rss_out.open("w", encoding="utf-8")
            rss_handle.write(
                "poll,batch,rss_kb,rss_mib,threads,"
                "runs_full_ms,runs_summary_ms,queue_full_ms,snapshot_ms\n"
            )
            rss_handle.flush()

        def _sample() -> tuple[int, int]:
            return _proc_rss_kb(proc.pid), _proc_threads(proc.pid)  # type: ignore[union-attr]

        def _record_poll(row: dict[str, Any]) -> None:
            poll_rows.append(row)
            if rss_handle is not None:
                rss_handle.write(
                    "{poll},{batch},{rss_kb},{rss_mib:.3f},{threads},"
                    "{runs_full_ms:.3f},{runs_summary_ms:.3f},"
                    "{queue_full_ms:.3f},{snapshot_ms:.3f}\n".format(
                        rss_mib=row["rss_kb"] / 1024.0, **row
                    )
                )
                rss_handle.flush()

        rows = drive_traffic(
            port,
            run_ids,
            batches=args.batches,
            polls_per_batch=args.polls_per_batch,
            queue_ids=queue_ids,
            timeout=args.request_timeout,
            include_full_runs=args.include_full_runs,
            poll_mix=args.poll_mix,
            midpoint_poll=args.midpoint_poll,
            server_pid=proc.pid if proc is not None else 0,
            rss_sampler=_sample,
            poll_rows=poll_rows,
            on_poll=_record_poll,
        )
        if rss_handle is not None:
            rss_handle.close()

        if poll_rows:
            print()
            print("[repro] RSS vs poll count (MEASUREMENT-ONLY)")
            print(
                f"{'poll':>5} {'RSS MiB':>9} {'dRSS MiB':>9} {'thr':>4} "
                f"{'runs_full ms':>14} {'runs_sum ms':>12} {'queue ms':>10} {'snap ms':>9}"
            )
            prev = baseline_rss
            for row in poll_rows:
                rss = row["rss_kb"]
                print(
                    f"{row['poll']:>5} {rss / 1024:>9.1f} {(rss - prev) / 1024:>9.1f} "
                    f"{row['threads']:>4} {row['runs_full_ms']:>14.1f} "
                    f"{row['runs_summary_ms']:>12.1f} {row['queue_full_ms']:>10.1f} "
                    f"{row['snapshot_ms']:>9.1f}"
                )
                prev = rss
            if args.rss_out is not None:
                print(f"[repro] RSS table written to {args.rss_out}")

        print()
        print("[repro] growth table")
        header = (
            f"{'batch':>5} {'RSS MiB':>9} {'dRSS MiB':>9} {'thr':>4} "
            + " ".join(f"{k:>14}" for k in sorted(rows[0]["latencies"]))
        )
        print(header)
        prev_rss = baseline_rss
        for row in rows:
            rss = _proc_rss_kb(proc.pid)
            threads = _proc_threads(proc.pid)
            lat = " ".join(
                f"{row['latencies'][k] * 1000:>13.1f}m" for k in sorted(row["latencies"])
            )
            print(
                f"{row['batch']:>5} {rss / 1024:>9.1f} {(rss - prev_rss) / 1024:>9.1f} "
                f"{threads:>4} {lat}"
            )
            prev_rss = rss

        final_rss = _proc_rss_kb(proc.pid)
        first = rows[0]["latencies"]
        last = rows[-1]["latencies"]
        print()
        print("[repro] summary (mean latency per endpoint, ms)")
        print(f"{'endpoint':>16} {'first':>10} {'last':>10} {'x':>8}")
        for key in sorted(first):
            ratio = last[key] / first[key] if first[key] else float("nan")
            print(f"{key:>16} {first[key] * 1000:>10.1f} {last[key] * 1000:>10.1f} {ratio:>8.2f}")
        print()
        print(
            f"[repro] RSS: baseline {baseline_rss / 1024:.1f} MiB -> "
            f"final {final_rss / 1024:.1f} MiB "
            f"(+{(final_rss - baseline_rss) / 1024:.1f} MiB)"
        )
        print(
            f"[repro] threads: baseline {baseline_threads} -> "
            f"final {_proc_threads(proc.pid)}"
        )
        print(
            f"[repro] worst endpoint growth: "
            + ", ".join(
                f"{k} {first[k] and last[k] / first[k]:.2f}x"
                for k in sorted(first)
                if first[k]
            )
        )

        # The wedge measurement: the full run-list projection is the path
        # that stops answering.  Measure it on its own, with a generous
        # timeout, so a timeout is reported as a number rather than a crash.
        print()
        print("[repro] wedge measurement: the full run-list projection")
        for path in ("/api/runs", "/api/runs?fields=summary", "/api/queue", "/api/meta"):
            status, size, elapsed = _get(port, path, timeout=args.wedge_timeout)
            verdict = "TIMEOUT (wedge)" if status == 0 else f"HTTP {status}"
            print(
                f"[repro]   {path:28s} {verdict:16s} {elapsed * 1000:10.1f} ms  "
                f"bytes={size}"
            )

        if args.concurrent_workers > 0:
            # Concurrent phase: this is the CT110 wedge.  Sample RSS while
            # the slow paths are in flight so the PEAK is visible, not just
            # the value after the traffic drains.
            print()
            print(
                f"[repro] concurrent phase: {args.concurrent_workers} workers x "
                f"{args.concurrent_rounds} rounds against the slow paths"
            )
            samples: list[tuple[float, int, int]] = []
            stop = threading.Event()

            def sampler() -> None:
                while not stop.is_set():
                    samples.append(
                        (
                            time.monotonic(),
                            _proc_rss_kb(proc.pid),  # type: ignore[union-attr]
                            _proc_threads(proc.pid),  # type: ignore[union-attr]
                        )
                    )
                    time.sleep(0.05)

            sampler_thread = threading.Thread(target=sampler, daemon=True)
            sampler_thread.start()
            concurrent = drive_concurrent(
                port,
                run_ids,
                workers=args.concurrent_workers,
                rounds=args.concurrent_rounds,
                timeout=args.request_timeout,
            )
            stop.set()
            sampler_thread.join(timeout=5)

            peak_rss = max((row[1] for row in samples), default=0)
            peak_threads = max((row[2] for row in samples), default=0)
            rss_after = _proc_rss_kb(proc.pid)  # type: ignore[union-attr]
            lat = concurrent["latencies"]
            print(
                f"[repro] concurrent: {concurrent['requests']} requests in "
                f"{concurrent['wall_seconds']:.1f}s"
            )
            print(
                f"[repro] concurrent latency: median "
                f"{concurrent['median_latency'] * 1000:.1f} ms, max "
                f"{concurrent['max_latency'] * 1000:.1f} ms"
            )
            if lat:
                p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
                print(f"[repro] concurrent latency: p95 {p95 * 1000:.1f} ms")
            print(
                f"[repro] concurrent failures: {len(concurrent['failures'])}"
                + (
                    " (first: " + concurrent["failures"][0][4] + ")"
                    if concurrent["failures"]
                    else ""
                )
            )
            print(
                f"[repro] RSS during concurrent load: peak "
                f"{peak_rss / 1024:.1f} MiB, after {rss_after / 1024:.1f} MiB"
            )
            print(f"[repro] peak threads: {peak_threads}")
        return 0
    finally:
        if proc is not None and proc.poll() is None:
            # LHH-WEB-LEAK-FIX c2 (MEASUREMENT-ONLY): ask the child for its
            # tracemalloc dump BEFORE terminating it.  SIGUSR1 is not handled
            # by uvicorn, so the dump is always written while the process is
            # still alive.
            if args.tracemalloc:
                try:
                    os.kill(proc.pid, signal.SIGUSR1)
                    proc.wait(timeout=30)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
        elif proc is not None:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
        # Close the child's stderr handle so the tracemalloc dump is flushed.
        stderr_handle = getattr(proc, "_repro_stderr_handle", None) if proc is not None else None
        if stderr_handle is not None:
            try:
                stderr_handle.close()
            except OSError:
                pass
        if args.keep:
            print(f"[repro] fixtures kept at {temp_root}")
        else:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
