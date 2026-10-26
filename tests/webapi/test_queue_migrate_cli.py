"""The operator CLI for moving a queue entry (LHH-QUEUE-MIGRATION-TOMBSTONE, slice E).

``lh-harness queue migrate <queue_id> --to <node>`` is the operator entry
point.  It goes through the ENDPOINT rather than writing the queue itself, so
the tombstone is created by exactly the same server-side transaction a direct
API caller gets: one transaction that tombstones the source BEFORE the
destination entry is created.

The tests here drive the real CLI entry point against a REAL web API served
on 127.0.0.1 (loopback only).  The destination is the same local stub the
slice-(B) tests use, so no test contacts a non-local host.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from lh_harness.queue import QueueStore

_NAME_SEQ = {"n": 0}


def _body(**overrides: Any) -> dict[str, Any]:
    _NAME_SEQ["n"] += 1
    body: dict[str, Any] = {
        "name": f"cli migrate task {_NAME_SEQ['n']}",
        "task": "do the thing",
        "workspace": "./workspace",
        "max_rounds": 5,
        "trio": "kimi",
        "priority": 0,
        "requested_by": "ci",
    }
    body.update(overrides)
    return body


class _FakeDestination:
    """A LOCAL stand-in for the destination node's queue API."""

    def __init__(self, root: Path) -> None:
        self.store = QueueStore(root)
        self.creates: list[dict[str, Any]] = []

    def create(self, dest_base_url: str, body: dict[str, Any], idempotency_key: str | None):
        self.creates.append(dict(body))
        entry = self.store.create(dict(body))
        return entry.queue_id, entry.to_dict()


@pytest.fixture()
def live_api(tmp_path: Path, monkeypatch: Any):
    """A real web API on 127.0.0.1, plus the destination stub it talks to."""

    import uvicorn
    from fastapi import FastAPI

    from lh_harness.webapi.server import create_app

    runs_root = tmp_path / "runs"
    runs_root.mkdir(parents=True)
    dest_root = tmp_path / "dest-runs"
    dest_root.mkdir(parents=True)
    destination = _FakeDestination(dest_root)

    monkeypatch.setattr(
        "lh_harness.queue_migrate.default_dest_create_entry", destination.create
    )

    app = create_app(runs_root=runs_root, auth_token="secret", bind_host="127.0.0.1")
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Wait for the server to pick its port and start serving.
    deadline = 100
    while not server.started and deadline > 0:
        deadline -= 1
        threading.Event().wait(0.05)
    if not server.started:
        server.should_exit = True
        pytest.skip("the loopback web API did not start in time")
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", runs_root, destination
    server.should_exit = True
    thread.join(timeout=5)


def _enqueue(api_url: str, body: dict[str, Any]) -> str:
    import urllib.request

    request = urllib.request.Request(
        f"{api_url}/api/queue",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer secret",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))["queue_id"]


def _run_cli(argv: list[str]) -> int:
    from lh_harness.cli import main

    return main(argv)


def test_the_cli_moves_a_pending_entry(live_api: Any, capsys: Any) -> None:
    """The CLI migrates the entry and prints the endpoint's answer."""

    api_url, runs_root, destination = live_api
    queue_id = _enqueue(api_url, _body())

    code = _run_cli(
        [
            "queue",
            "migrate",
            queue_id,
            "--to",
            "node-b",
            "--dest-url",
            "http://127.0.0.1:9",
            "--api-url",
            api_url,
            "--auth-token",
            "secret",
        ]
    )
    assert code == 0

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["queue_id"] == queue_id
    assert payload["status"] == "migrated"
    assert payload["dest_node"] == "node-b"

    # The source is a tombstone.
    store = QueueStore(runs_root)
    tomb = store.get(queue_id)
    assert tomb is not None
    assert tomb.status == "migrated"
    assert tomb.migrated_to is not None
    assert tomb.migrated_to["node"] == "node-b"

    # And the destination holds exactly one entry with the fixed dedup key.
    dest_entries = destination.store.list()
    assert len(dest_entries) == 1
    assert dest_entries[0].dedup_key == f"{queue_id}:node-b"


def test_the_cli_is_idempotent(live_api: Any, capsys: Any) -> None:
    """Re-running the same command converges on the same destination entry."""

    api_url, runs_root, destination = live_api
    queue_id = _enqueue(api_url, _body())

    argv = [
        "queue",
        "migrate",
        queue_id,
        "--to",
        "node-b",
        "--dest-url",
        "http://127.0.0.1:9",
        "--api-url",
        api_url,
        "--auth-token",
        "secret",
    ]
    assert _run_cli(argv) == 0
    first = json.loads(capsys.readouterr().out)

    assert _run_cli(argv) == 0
    second = json.loads(capsys.readouterr().out)

    assert first["dest_queue_id"] == second["dest_queue_id"]
    assert len(destination.store.list()) == 1


def test_the_cli_refuses_a_running_entry(live_api: Any, capsys: Any) -> None:
    """A LAUNCHED entry is refused by the endpoint; the CLI reports the failure."""

    api_url, runs_root, destination = live_api
    queue_id = _enqueue(api_url, _body())
    store = QueueStore(runs_root)
    store.mark_launched(queue_id, run_id="run-1")

    code = _run_cli(
        [
            "queue",
            "migrate",
            queue_id,
            "--to",
            "node-b",
            "--dest-url",
            "http://127.0.0.1:9",
            "--api-url",
            api_url,
            "--auth-token",
            "secret",
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "409" in err
    assert destination.creates == []


def test_the_cli_requires_a_destination_url(capsys: Any) -> None:
    """Without --dest-url the command refuses before it touches the network."""

    code = _run_cli(["queue", "migrate", "q-1", "--to", "node-b"])
    assert code == 2
    assert "--dest-url" in capsys.readouterr().err


def test_the_queue_command_without_a_subcommand_prints_help(capsys: Any) -> None:
    """`lh-harness queue` alone lists the queue subcommands."""

    code = _run_cli(["queue"])
    assert code == 2
    assert "migrate" in capsys.readouterr().out
