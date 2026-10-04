"""Provider-quota retry backoff in the launcher.

Before: ``max_retries=2`` gave three attempts, the retry entry was created the
moment the previous attempt ended, and a 429 / provider_quota /
provider_rate_limit failure retried at once, so every attempt burned inside
one quota window.  Now a quota-class retry carries ``not_before`` (30 min,
then 90 min by default, or the provider's own reset time when later); the
launcher skips it with ``waiting: <reason> until <ts>`` and the wait never
counts as an attempt.  Every other failure kind keeps the immediate retry.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import lh_harness.launcher as launcher_module
from lh_harness.config import ProjectConfigError, _flatten_queue_table
from lh_harness.launcher import Launcher, _classify_failure_cause, _parse_provider_reset
from lh_harness.queue import QueueEntry, QueueStore, queue_config_from_config

NOW = 1_790_000_000.0  # fixed clock: 2026-09-21T14:13:20Z


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Clock:
    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class _Supervisor:
    """Supervisor stand-in: ``create_run`` fails with ``fail_with`` or launches."""

    def __init__(self, runs_root: Path) -> None:
        self.runs_root = runs_root
        self.fail_with: Exception | None = None
        self.calls = 0
        self._statuses: dict[str, dict[str, Any]] = {}
        self._owners: dict[str, dict[str, Any]] = {}

    def list_run_items(self) -> list[dict[str, Any]]:
        return [
            {"id": run_id, "status": status.get("status"), "workspace": self._owners[run_id]["workspace"]}
            for run_id, status in self._statuses.items()
        ]

    def owner(self, run_id: str) -> dict[str, Any]:
        return self._owners.get(run_id, {})

    def status(self, run_id: str) -> dict[str, Any]:
        return self._statuses.get(run_id, {"status": "idle", "run_id": run_id})

    def _run_logs_dir(self, run_id: str) -> Path:
        return self.runs_root / run_id / "lh_harness"

    def create_run(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        run_id = f"run-{uuid.uuid4().hex[:8]}"
        self._owners[run_id] = {"run_id": run_id, "workspace": str(kwargs.get("workspace", "."))}
        self._statuses[run_id] = {"status": "running", "run_id": run_id}
        return {"id": run_id, "status": "creating", "owner": self._owners[run_id]}

    def finish(self, run_id: str, status: str, report: dict[str, Any]) -> None:
        self._statuses[run_id] = {"status": status, "run_id": run_id}
        report_dir = self.runs_root / run_id / "lh_harness"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")


def _config(**capacity: Any) -> dict[str, Any]:
    return {
        "trios": {"kimi": {"agent": "codex", "model": None, "mcp_profile": None}},
        "capacity": {"kimi_max": 1, "qwen_max": 1, "poll_seconds": 1, "max_retries": 2, **capacity},
    }


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    clock = _Clock(NOW)
    monkeypatch.setattr(launcher_module, "_now", clock)
    store = QueueStore(root)
    supervisor = _Supervisor(root)

    def make(config: dict[str, Any] | None = None) -> Launcher:
        return Launcher(supervisor, store, queue_config=config or _config(), probe_open_pr=None)

    return root, store, supervisor, clock, make


def _enqueue(store: QueueStore, name: str = "quota task") -> QueueEntry:
    return store.create(
        {
            "name": name,
            "task": "do the thing",
            "workspace": "./workspace",
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ci",
        }
    )


def _pending(store: QueueStore) -> list[QueueEntry]:
    return [entry for entry in store.list() if entry.status == "pending"]


def _requeued_events(root: Path) -> list[dict[str, Any]]:
    path = root / "queue" / "service_events.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)["payload"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if '"queue.requeued"' in line
    ]


# --- backoff applied ---------------------------------------------------------


def test_rate_limited_launch_failure_retry_waits_30_minutes(env) -> None:
    root, store, supervisor, clock, make = env
    supervisor.fail_with = ConnectionError("provider 429 Too Many Requests")
    entry = _enqueue(store)

    asyncio.run(make().tick())

    assert store.get(entry.queue_id).status == "failed"
    [successor] = _pending(store)
    assert successor.retry_of == entry.queue_id
    assert successor.attempt == 2
    assert successor.not_before == pytest.approx(NOW + 30 * 60)
    assert successor.wait_reason == "provider_rate_limit"
    payload = _requeued_events(root)[-1]
    assert payload["not_before"] == pytest.approx(NOW + 30 * 60)
    assert payload["not_before_utc"] == _iso(NOW + 30 * 60)
    assert payload["wait_reason"] == "provider_rate_limit"


def test_provider_quota_report_retry_waits_and_second_retry_waits_90_minutes(env) -> None:
    root, store, supervisor, clock, make = env
    launcher = make()
    entry = _enqueue(store)
    asyncio.run(launcher.tick())
    first = store.get(entry.queue_id)
    assert first.status == "launched"

    supervisor.finish(
        first.run_id,
        "failed",
        {"abort_reason": "provider_quota", "failure_reason": "Provider quota exceeded for this key"},
    )
    asyncio.run(launcher.tick())

    [retry1] = _pending(store)
    assert retry1.attempt == 2
    assert retry1.wait_reason == "provider_quota"
    assert retry1.not_before == pytest.approx(NOW + 30 * 60)

    # Wait out the first window, launch, fail on quota again: 90 minutes.
    clock.now = retry1.not_before + 1
    asyncio.run(launcher.tick())
    launched = store.get(retry1.queue_id)
    assert launched.status == "launched"
    supervisor.finish(launched.run_id, "failed", {"abort_reason": "provider_quota"})
    asyncio.run(launcher.tick())

    [retry2] = _pending(store)
    assert retry2.attempt == 3
    assert retry2.retry_of == retry1.queue_id
    assert retry2.not_before == pytest.approx(clock.now + 90 * 60)


def test_backoff_schedule_is_configurable(env) -> None:
    root, store, supervisor, clock, make = env
    supervisor.fail_with = ConnectionError("rate limit reached")
    _enqueue(store)

    asyncio.run(make(_config(quota_backoff_minutes=[5])).tick())

    [successor] = _pending(store)
    assert successor.not_before == pytest.approx(NOW + 5 * 60)


# --- skip reason -------------------------------------------------------------


def test_waiting_entry_is_skipped_with_reason_until_not_before(env) -> None:
    root, store, supervisor, clock, make = env
    launcher = make()
    supervisor.fail_with = ConnectionError("provider 429")
    _enqueue(store)
    asyncio.run(launcher.tick())
    [successor] = _pending(store)
    calls_after_failure = supervisor.calls

    supervisor.fail_with = None
    clock.now = NOW + 10 * 60
    asyncio.run(launcher.tick())

    waiting = store.get(successor.queue_id)
    assert waiting.status == "pending"
    assert waiting.skip_reasons[-1] == (
        f"waiting: provider_rate_limit until {_iso(NOW + 30 * 60)}"
    )
    assert supervisor.calls == calls_after_failure  # nothing was launched

    clock.now = NOW + 30 * 60 + 1
    asyncio.run(launcher.tick())
    assert store.get(successor.queue_id).status == "launched"


def test_waiting_entry_does_not_count_as_eligible_for_the_stall_detector(env) -> None:
    root, store, supervisor, clock, make = env
    launcher = make()
    supervisor.fail_with = ConnectionError("provider 429")
    _enqueue(store)
    asyncio.run(launcher.tick())
    stall_before = launcher.stall_cycles

    for minute in (1, 2, 3):
        clock.now = NOW + minute * 60
        asyncio.run(launcher.tick())

    assert launcher.stall_cycles == stall_before


# --- attempt counting ----------------------------------------------------------


def test_waiting_never_consumes_attempts_and_the_cap_still_holds(env) -> None:
    root, store, supervisor, clock, make = env
    launcher = make()
    supervisor.fail_with = ConnectionError("provider 429")
    original = _enqueue(store)

    asyncio.run(launcher.tick())  # attempt 1 fails
    for minute in range(1, 30, 5):  # many passes inside the window
        clock.now = NOW + minute * 60
        asyncio.run(launcher.tick())
    [retry1] = _pending(store)
    assert retry1.attempt == 2
    assert len(store.list()) == 2  # no extra entries created while waiting
    assert supervisor.calls == 1

    clock.now = retry1.not_before + 1
    asyncio.run(launcher.tick())  # attempt 2 fails
    [retry2] = _pending(store)
    assert retry2.attempt == 3
    assert supervisor.calls == 2

    clock.now = retry2.not_before + 1
    asyncio.run(launcher.tick())  # attempt 3 fails: cap reached, no successor
    assert supervisor.calls == 3
    assert _pending(store) == []
    assert [entry.attempt for entry in sorted(store.list(), key=lambda e: e.attempt)] == [1, 2, 3]
    assert store.get(original.queue_id).status == "failed"


# --- reset-time parsing --------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("HTTP 429 Retry-After: 7200", NOW + 7200),
        ("429; retry after 3 hours", NOW + 3 * 3600),
        ("retry-after-ms: 9000000", NOW + 9000),
        ("Rate limit reached. Please try again in 2h30m.", NOW + 2.5 * 3600),
        ("Rate limit reached. Please try again in 1m30s.", NOW + 90),
        (f"quota exceeded, resets at {_iso(NOW + 4 * 3600)}", NOW + 4 * 3600),
        (f'{{"error": "insufficient_quota", "reset_at": {int(NOW + 5000)}}}', float(int(NOW + 5000))),
        (f"Claude AI usage limit reached|{int(NOW + 6000)}", float(int(NOW + 6000))),
        ("x-ratelimit-reset: 2026-09-21T16:13:20", NOW + 2 * 3600),  # naive ISO is UTC
    ],
)
def test_parse_provider_reset_shapes(text: str, expected: float) -> None:
    assert _parse_provider_reset(text, NOW) == pytest.approx(expected)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "provider_rate_limit",
        f"resets at {_iso(NOW - 60)}",  # in the past
        f"usage limit reached|{int(NOW + 30 * 24 * 3600)}",  # beyond the horizon
        "run 20260921T141320Z_ab12cd34 failed",
    ],
)
def test_parse_provider_reset_ignores_unusable_text(text: str) -> None:
    assert _parse_provider_reset(text, NOW) is None


def test_parse_provider_reset_takes_the_latest_hint() -> None:
    text = f"Retry-After: 60 | usage limit reached|{int(NOW + 7200)}"
    assert _parse_provider_reset(text, NOW) == pytest.approx(float(int(NOW + 7200)))


def test_provider_reset_later_than_backoff_wins(env) -> None:
    root, store, supervisor, clock, make = env
    supervisor.fail_with = ConnectionError(
        f"429 Too Many Requests: usage limit resets at {_iso(NOW + 3 * 3600)}"
    )
    _enqueue(store)
    asyncio.run(make().tick())
    [successor] = _pending(store)
    assert successor.not_before == pytest.approx(NOW + 3 * 3600)


def test_provider_reset_earlier_than_backoff_keeps_backoff(env) -> None:
    root, store, supervisor, clock, make = env
    supervisor.fail_with = ConnectionError("429 Too Many Requests; Retry-After: 20")
    _enqueue(store)
    asyncio.run(make().tick())
    [successor] = _pending(store)
    assert successor.not_before == pytest.approx(NOW + 30 * 60)


# --- non-quota failures unchanged -------------------------------------------------


@pytest.mark.parametrize(
    ("report", "label_fragment"),
    [
        ({"abort_reason": "provider_stall", "failure_reason": "Episode stalled: no output for 900s."}, "stall"),
        ({"abort_reason": "provider_timeout", "failure_reason": "executor episode timed out"}, "timeout"),
    ],
)
def test_non_quota_retry_is_immediate(env, report: dict[str, Any], label_fragment: str) -> None:
    root, store, supervisor, clock, make = env
    launcher = make()
    entry = _enqueue(store)
    asyncio.run(launcher.tick())
    run_id = store.get(entry.queue_id).run_id
    supervisor.finish(run_id, "failed", report)
    asyncio.run(launcher.tick())

    successor = next(e for e in store.list() if e.retry_of == entry.queue_id)
    assert successor.not_before is None
    assert successor.wait_reason is None
    assert label_fragment in (successor.failure_cause or "")
    assert "not_before" not in _requeued_events(root)[-1]
    # Immediately launchable on the next pass, same clock.
    asyncio.run(launcher.tick())
    assert store.get(successor.queue_id).status == "launched"


def test_non_retryable_failure_still_has_no_successor(env) -> None:
    root, store, supervisor, clock, make = env
    supervisor.fail_with = ValueError("workspace must be inside configured workspace root")
    _enqueue(store)
    asyncio.run(make().tick())
    assert _pending(store) == []
    assert len(store.list()) == 1


def test_classification_labels() -> None:
    assert _classify_failure_cause("provider_quota | run failed") == "provider_quota"
    assert _classify_failure_cause("429 insufficient_quota") == "provider_quota"
    assert _classify_failure_cause("provider_rate_limit") == "provider_rate_limit"
    assert _classify_failure_cause("provider_stall") == "provider_stall"
    assert _classify_failure_cause("executor timed out") == "episode_timeout"
    # Human stops stay non-retryable even when the text mentions a quota.
    assert _classify_failure_cause("user_cancelled after usage limit") is None


# --- queue API backward compatibility ------------------------------------------


def test_old_entry_json_without_backoff_fields_loads(tmp_path: Path) -> None:
    legacy = {
        "queue_id": "q-legacy",
        "name": "old",
        "task": "t",
        "workspace": "./w",
        "max_rounds": 3,
        "trio": "kimi",
        "priority": 0,
        "requested_by": "ci",
        "retry_of": "q-older",
        "attempt": 2,
    }
    entry = QueueEntry.from_dict(legacy)
    assert entry.not_before is None
    assert entry.wait_reason is None
    data = entry.to_dict()
    assert data["not_before"] is None and data["wait_reason"] is None


def test_backoff_fields_survive_disk_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    store = QueueStore(root)
    original = _enqueue(store)
    store.mark_failed(original.queue_id, "provider_quota")
    successor = store.requeue(
        original.queue_id, "provider_quota", not_before=NOW + 60, wait_reason="provider_quota"
    )
    reloaded = QueueStore(root).get(successor.queue_id)
    assert reloaded.not_before == pytest.approx(NOW + 60)
    assert reloaded.wait_reason == "provider_quota"
    # The two-argument call keeps working and sets nothing.
    other = _enqueue(store, "other")
    store.mark_failed(other.queue_id, "provider_stall")
    plain = store.requeue(other.queue_id, "provider_stall")
    assert plain.not_before is None and plain.wait_reason is None


def test_config_quota_backoff_minutes_normalization() -> None:
    assert queue_config_from_config({})["capacity"]["quota_backoff_minutes"] == [30.0, 90.0]
    configured = queue_config_from_config(
        {"queue": {"capacity": {"quota_backoff_minutes": [10, 20, 40]}}}
    )
    assert configured["capacity"]["quota_backoff_minutes"] == [10.0, 20.0, 40.0]
    # Unusable values fall back to the default instead of crashing the store.
    junk = queue_config_from_config({"queue": {"capacity": {"quota_backoff_minutes": "soon"}}})
    assert junk["capacity"]["quota_backoff_minutes"] == [30.0, 90.0]


def test_project_config_validates_quota_backoff_minutes() -> None:
    table = _flatten_queue_table({"capacity": {"quota_backoff_minutes": [15, 45]}})
    assert table["capacity"]["quota_backoff_minutes"] == [15.0, 45.0]
    assert _flatten_queue_table({})["capacity"]["quota_backoff_minutes"] == [30.0, 90.0]
    for bad in ([], [-1], ["30"], 30, [True]):
        with pytest.raises(ProjectConfigError, match="quota_backoff_minutes"):
            _flatten_queue_table({"capacity": {"quota_backoff_minutes": bad}})
