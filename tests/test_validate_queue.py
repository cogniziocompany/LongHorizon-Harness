"""TASK 218: fixture-based tests for lh_harness.validate_queue.

Every test runs against an in-repo fixture queue built in tmp_path.  No test may
touch the live queue: the module's QUEUE_DIR/HARNESS_SRC are pinned to fixture
paths for the duration of each test, and module-level findings state is reset
between tests because the prototype accumulates findings in a global list.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import lh_harness.validate_queue as vq

SRC_DIR = Path(vq.__file__).resolve().parent


def _write_entry(queue: Path, filename: str, payload: dict) -> Path:
    path = queue / filename
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    q = tmp_path / "queue"
    (q / "blocked").mkdir(parents=True)
    (q / "done").mkdir(parents=True)
    return q


@pytest.fixture
def harness_src(tmp_path: Path) -> Path:
    """A minimal harness-source dir so rule 4 runs instead of skipping."""
    src = tmp_path / "harness_src"
    src.mkdir()
    (src / "code.py").write_text('X = "LH_HARNESS_DB_PASSWORD"\n', encoding="utf-8")
    return src


@pytest.fixture
def run(queue, harness_src, monkeypatch):
    """Run the validator against fixture dirs; returns (exit_code, findings snapshot).

    The gateway-key env var is removed by default so the gateway-alias rule takes its
    skip path in every test that does not set the key itself; tests that DO set it
    must also mock ``_fetch_registry_aliases`` - no test may contact the live gateway.
    """
    monkeypatch.delenv("LH_HARNESS_MCP_GATEWAY_KEY", raising=False)

    def _run() -> tuple[int, list[tuple[str, str, str]], str]:
        monkeypatch.setattr(vq, "QUEUE", str(queue))
        monkeypatch.setattr(vq, "HARNESS_SRC", str(harness_src))
        vq.findings.clear()
        code = vq.main()
        return code, list(vq.findings)

    return _run


def _rules(rules_and_sevs, findings):
    return {(sev, rule) for sev, rule, _ in findings if (sev, rule) in rules_and_sevs}


# ---- rule 1: duplicate-task-number ----
def test_duplicate_task_number_is_error(run, queue):
    _write_entry(queue, "strand-201-alpha.json", {"name": "strand-201-alpha", "workspace": "w", "max_rounds": 1})
    _write_entry(queue, "strand-201-beta.json", {"name": "strand-201-beta", "workspace": "w", "max_rounds": 1})
    code, findings = run()
    assert ("ERROR", "duplicate-task-number") in {(s, r) for s, r, _ in findings}
    assert code == 1


# ---- rule 2: task-file-not-found / missing-task-file-field ----
def test_task_file_not_found_is_error(run, queue):
    _write_entry(
        queue,
        "202-file-check.json",
        {"name": "202-file-check", "workspace": "w", "max_rounds": 1,
         "task_source": "task_file", "task_file": "C:/definitely/not/here.md"},
    )
    code, findings = run()
    assert ("ERROR", "task-file-not-found") in {(s, r) for s, r, _ in findings}
    assert code == 1


def test_task_source_without_task_file_is_error(run, queue):
    _write_entry(
        queue,
        "203-file-check.json",
        {"name": "203-file-check", "workspace": "w", "max_rounds": 1, "task_source": "task_file"},
    )
    code, findings = run()
    assert ("ERROR", "missing-task-file-field") in {(s, r) for s, r, _ in findings}
    assert code == 1


def test_existing_task_file_is_clean(run, queue, tmp_path):
    task_file = tmp_path / "task.md"
    task_file.write_text("do the thing", encoding="utf-8")
    _write_entry(
        queue,
        "204-file-check.json",
        {"name": "204-file-check", "workspace": "w", "max_rounds": 1,
         "task_source": "task_file", "task_file": str(task_file)},
    )
    code, findings = run()
    assert not findings
    assert code == 0


# ---- rule 3: blocker-target-does-not-exist / blocker-may-be-satisfied ----
def test_blocker_target_dangling_is_error(run, queue):
    _write_entry(
        queue,
        "205-blocked.json",
        {"name": "205-blocked", "workspace": "w", "max_rounds": 1,
         "note": "BLOCKED ON TASK 999 which does not exist"},
    )
    code, findings = run()
    assert ("ERROR", "blocker-target-does-not-exist") in {(s, r) for s, r, _ in findings}
    assert code == 1


def test_blocker_in_done_is_warn_only_exit_zero(run, queue):
    (queue / "done" / "fleet-998-done-entry.json").write_text("{}", encoding="utf-8")
    _write_entry(
        queue,
        "206-blocked.json",
        {"name": "206-blocked", "workspace": "w", "max_rounds": 1,
         "note": "BLOCKED ON TASK 998 which has landed"},
    )
    code, findings = run()
    assert ("WARN", "blocker-may-be-satisfied") in {(s, r) for s, r, _ in findings}
    # HARD RULE: exit non-zero only on ERROR - warn-only must exit 0.
    assert code == 0


# ---- rule 4: env-name-not-in-source (+ env-check-skipped when source is missing) ----
def test_env_name_not_in_source_is_warn(run, queue, harness_src, monkeypatch):
    (harness_src / "code.py").write_text('X = "LH_HARNESS_DB_PASSWORD"\n', encoding="utf-8")
    task_file = harness_src.parent / "task.md"
    task_file.write_text("set LH_HARNESS_DATABASE_URL for the run", encoding="utf-8")
    _write_entry(
        queue,
        "207-env-check.json",
        {"name": "207-env-check", "workspace": "w", "max_rounds": 1, "task_file": str(task_file)},
    )
    code, findings = run()
    assert ("WARN", "env-name-not-in-source") in {(s, r) for s, r, _ in findings}
    assert ("ERROR", "env-name-not-in-source") not in {(s, r) for s, r, _ in findings}
    assert code == 0


def test_env_check_skipped_when_no_source(run, queue, harness_src):
    shutil.rmtree(harness_src)  # no harness source -> rule 4 must skip with a WARN, not fail
    _write_entry(queue, "208-env-check.json", {"name": "208-env-check", "workspace": "w", "max_rounds": 1})
    code, findings = run()
    assert ("WARN", "env-check-skipped") in {(s, r) for s, r, _ in findings}
    assert code == 0


# ---- rule 5: schema ----
def test_missing_required_field_is_error(run, queue):
    _write_entry(queue, "209-schema.json", {"name": "209-schema"})  # no workspace/max_rounds
    code, findings = run()
    assert ("ERROR", "missing-required-field") in {(s, r) for s, r, _ in findings}
    assert code == 1


def test_unknown_trio_is_error(run, queue):
    _write_entry(
        queue,
        "210-schema.json",
        {"name": "210-schema", "workspace": "w", "max_rounds": 1, "trio": "gpt"},
    )
    code, findings = run()
    assert ("ERROR", "unknown-trio") in {(s, r) for s, r, _ in findings}
    assert code == 1


def test_name_filename_mismatch_is_warn_only(run, queue):
    _write_entry(
        queue,
        "747-114-fleet-heartbeat-413.json",
        {"name": "fleet-heartbeat-413", "workspace": "w", "max_rounds": 1},
    )
    code, findings = run()
    assert ("WARN", "name-filename-mismatch") in {(s, r) for s, r, _ in findings}
    assert code == 0


def test_continuation_without_branch_is_error(run, queue):
    _write_entry(
        queue,
        "211-continuation.json",
        {"name": "211-continuation", "workspace": "w", "max_rounds": 1, "continue_branch": True},
    )
    code, findings = run()
    assert ("ERROR", "continuation-without-branch") in {(s, r) for s, r, _ in findings}
    assert code == 1


def test_continue_branch_with_branch_is_clean(run, queue):
    _write_entry(
        queue,
        "212-continuation.json",
        {"name": "212-continuation", "workspace": "w", "max_rounds": 1,
         "continue_branch": True, "branch": "feat/some-branch"},
    )
    code, findings = run()
    assert findings == []
    assert code == 0


# ---- unparseable entry ----
def test_unparseable_entry_is_error(run, queue):
    (queue / "213-broken.json").write_text("{not json", encoding="utf-8")
    code, findings = run()
    assert ("ERROR", "unparseable-entry") in {(s, r) for s, r, _ in findings}
    assert code == 1


# ---- HARD RULE: no writes anywhere in the code path ----
def test_validator_never_writes_to_queue(run, queue, monkeypatch):
    _write_entry(queue, "214-clean.json", {"name": "214-clean", "workspace": "w", "max_rounds": 1})
    (queue / "done" / "fleet-997-done.json").write_text("{}", encoding="utf-8")
    (queue / "blocked" / "215-blocked.json").write_text("{}", encoding="utf-8")

    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in queue.rglob("*") if p.is_file()}

    # Fail loudly if anything opens a queue path for writing during the run.
    real_open = open

    def guarded_open(file, *a, **kw):
        mode = a[0] if a else kw.get("mode", "r")
        try:
            resolved = Path(file).resolve()
        except (TypeError, ValueError, OSError):
            return real_open(file, *a, **kw)
        if str(resolved).startswith(str(queue.resolve())) and any(c in str(mode) for c in "wax+"):
            pytest.fail(f"validator opened queue file for writing: {resolved} mode={mode!r}")
        return real_open(file, *a, **kw)

    monkeypatch.setattr("builtins.open", guarded_open)
    code, _ = run()

    after = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in queue.rglob("*") if p.is_file()}
    assert before == after
    # No new files (e.g. seen-findings state) appeared either.
    assert set(before) == set(after)


# ---- HARD RULE: no secret values in output, env-var names only ----
def test_output_contains_no_secret_values(run, queue, capsys, monkeypatch):
    secret_value = "super-secret-value-42"
    monkeypatch.setenv("LH_HARNESS_DB_PASSWORD", secret_value)
    task_file = queue.parent / "task.md"
    task_file.write_text("set LH_HARNESS_DB_PASSWORD to the right value", encoding="utf-8")
    _write_entry(
        queue,
        "216-secrets.json",
        {"name": "216-secrets", "workspace": "w", "max_rounds": 1, "task_file": str(task_file)},
    )
    code, findings = run()
    out = capsys.readouterr().out
    # The env-var NAME may appear; its VALUE must not.
    assert secret_value not in out
    assert "LH_HARNESS_DB_PASSWORD" in out or code == 0
    for _, _, msg in findings:
        assert secret_value not in msg


# ---- console entry point behaves the same as in-process main ----
def test_module_entry_point_exit_semantics(tmp_path: Path):
    queue = tmp_path / "queue"
    queue.mkdir(parents=True)
    (queue / "blocked").mkdir()
    (queue / "done").mkdir()
    _write_entry(queue, "217-clean.json", {"name": "217-clean", "workspace": "w", "max_rounds": 1})
    env = {
        "QUEUE_DIR": str(queue),
        "HARNESS_SRC": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        # Path.home() needs one of these on Windows (Linux falls back to pwd).
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        # Point the child interpreter at this checkout's src tree so it imports
        # the same module the tests do, not a possibly-stale installed copy.
        "PYTHONPATH": str(SRC_DIR.parent),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "lh_harness.validate_queue"],
        capture_output=True, text=True, env=env, cwd=str(SRC_DIR.parent.parent),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 live entries checked" in proc.stdout


# ================= scope-D rules 6 and 7 (TASK 218) =================
# HARD RULE: every test below is offline. The registry fetch is mocked via
# monkeypatch.setattr(vq, "_fetch_registry_aliases", ...); the no-key tests prove
# the fetch is never even reached.


# ---- rule 6: gateway-alias-exists ----
def _entry_naming_alias(queue: Path, tmp_path: Path, alias: str = "lhharness") -> Path:
    task_file = tmp_path / "alias-task.md"
    task_file.write_text(f"route fleet requests through the gateway alias `{alias}`", encoding="utf-8")
    return _write_entry(
        queue,
        "601-alias-check.json",
        {"name": "601-alias-check", "workspace": "w", "max_rounds": 1, "task_file": str(task_file)},
    )


def test_gateway_alias_present_in_registry_is_clean(run, queue, tmp_path, monkeypatch):
    _entry_naming_alias(queue, tmp_path)
    monkeypatch.setenv("LH_HARNESS_MCP_GATEWAY_KEY", "dummy-test-key")
    monkeypatch.setattr(vq, "_fetch_registry_aliases", lambda root, key: {"lhharness", "kb"})
    code, findings = run()
    assert code == 0
    assert not [f for f in findings if f[1].startswith("gateway")]


def test_gateway_alias_absent_from_registry_is_error(run, queue, tmp_path, monkeypatch):
    _entry_naming_alias(queue, tmp_path)
    monkeypatch.setenv("LH_HARNESS_MCP_GATEWAY_KEY", "dummy-test-key")
    monkeypatch.setattr(vq, "_fetch_registry_aliases", lambda root, key: {"kb", "guides"})
    code, findings = run()
    assert ("ERROR", "gateway-alias-exists") in {(s, r) for s, r, _ in findings}
    # The finding names the candidate alias and NEVER the key value.
    msg = next(m for s, r, m in findings if r == "gateway-alias-exists")
    assert "lhharness" in msg
    assert "dummy-test-key" not in msg
    assert code == 1


def test_gateway_alias_from_entry_field_is_checked(run, queue, monkeypatch):
    _write_entry(
        queue,
        "602-alias-field.json",
        {"name": "602-alias-field", "workspace": "w", "max_rounds": 1, "mcp_alias": "audit"},
    )
    monkeypatch.setenv("LH_HARNESS_MCP_GATEWAY_KEY", "dummy-test-key")
    monkeypatch.setattr(vq, "_fetch_registry_aliases", lambda root, key: {"kb"})
    code, findings = run()
    assert ("ERROR", "gateway-alias-exists") in {(s, r) for s, r, _ in findings}
    assert code == 1


def test_gateway_alias_skips_entirely_without_key(run, queue, tmp_path, monkeypatch):
    """HARD RULE: no key -> SKIP (no scan-triggered fetch, no finding, exit unaffected)."""
    _entry_naming_alias(queue, tmp_path)
    monkeypatch.delenv("LH_HARNESS_MCP_GATEWAY_KEY", raising=False)

    def boom(root, key):  # pragma: no cover - must never run
        pytest.fail("registry fetch must not run without a gateway key (and never offline)")

    monkeypatch.setattr(vq, "_fetch_registry_aliases", boom)
    code, findings = run()
    assert not [f for f in findings if f[1] in ("gateway-alias-exists", "gateway-registry-unavailable")]
    assert code == 0


def test_gateway_alias_skips_when_registry_root_underivable(run, queue, tmp_path, monkeypatch):
    _entry_naming_alias(queue, tmp_path)
    monkeypatch.setenv("LH_HARNESS_MCP_GATEWAY_KEY", "dummy-test-key")
    monkeypatch.setenv("LH_HARNESS_MCP_GATEWAY_URL", "not a usable url")

    def boom(root, key):  # pragma: no cover - must never run
        pytest.fail("registry fetch must not run when no root can be derived")

    monkeypatch.setattr(vq, "_fetch_registry_aliases", boom)
    code, findings = run()
    assert not [f for f in findings if f[1].startswith("gateway")]
    assert code == 0


def test_gateway_registry_unavailable_is_warn_only(run, queue, tmp_path, monkeypatch):
    _entry_naming_alias(queue, tmp_path)
    monkeypatch.setenv("LH_HARNESS_MCP_GATEWAY_KEY", "dummy-test-key")

    def down(root, key):
        raise OSError("connection refused stub")

    monkeypatch.setattr(vq, "_fetch_registry_aliases", down)
    code, findings = run()
    assert ("WARN", "gateway-registry-unavailable") in {(s, r) for s, r, _ in findings}
    # The message carries the exception TYPE only - never the key value or error text.
    msg = next(m for s, r, m in findings if r == "gateway-registry-unavailable")
    assert "OSError" in msg and "refused" not in msg
    assert "dummy-test-key" not in msg
    assert code == 0


def test_gateway_registry_root_derives_from_in_repo_source(monkeypatch):
    """Root derivation must come from lh_harness.mcp_profiles' defaults, not from a
    hand-invented constant."""
    monkeypatch.delenv("LH_HARNESS_MCP_GATEWAY_URL", raising=False)
    assert vq._gateway_registry_root() == "https://litellm-gateway-api.cognizioware.com"
    monkeypatch.setenv("LH_HARNESS_MCP_GATEWAY_URL", "http://gateway.example.test:4000/mcp/")
    assert vq._gateway_registry_root() == "http://gateway.example.test:4000"
    monkeypatch.setenv("LH_HARNESS_MCP_GATEWAY_URL", "not a usable url")
    assert vq._gateway_registry_root() is None


def test_fetch_registry_aliases_builds_readonly_get(monkeypatch):
    """The ONE network-capable function, stubbed offline: it must issue exactly one
    GET-shaped request to {root}/v1/mcp/server and parse the registry payload."""
    seen = {}

    class _Resp(io.StringIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        return _Resp(json.dumps([{"server_name": "kb", "url": "http://kb.internal"},
                                 {"server_name": "lhharness"}]))

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    aliases = vq._fetch_registry_aliases("https://gateway.example", "dummy-test-key")
    assert seen == {"url": "https://gateway.example/v1/mcp/server", "method": "GET"}
    assert aliases == {"kb", "lhharness"}


def test_registry_alias_parsing_shapes():
    assert vq._registry_aliases([{"server_name": "Kb"}, {"alias": "lhharness"}]) == {"kb", "lhharness"}
    assert vq._registry_aliases({"data": [{"name": "guides"}]}) == {"guides"}
    assert vq._registry_aliases(["memory", "skills"]) == {"memory", "skills"}
    assert vq._registry_aliases({"unexpected": True}) == set()


def test_gateway_key_value_never_in_output(run, queue, tmp_path, monkeypatch, capsys):
    fake_key = "tok-dummy-test-key-12345"
    _entry_naming_alias(queue, tmp_path)
    monkeypatch.setenv("LH_HARNESS_MCP_GATEWAY_KEY", fake_key)
    monkeypatch.setattr(vq, "_fetch_registry_aliases", lambda root, key: {"lhharness"})
    code, findings = run()
    out = capsys.readouterr().out
    assert fake_key not in out
    assert all(fake_key not in msg for _, _, msg in findings)
    assert code == 0


# ---- rule 7: open-asks-row-integrity ----
def test_open_asks_file_absent_is_fine(run, queue):
    _write_entry(queue, "603-clean.json", {"name": "603-clean", "workspace": "w", "max_rounds": 1})
    code, findings = run()
    assert not [f for f in findings if f[1].startswith("open-asks")]
    assert code == 0


def test_open_asks_rows_with_both_cells_are_clean(run, queue):
    (queue / "OPEN-ASKS.md").write_text(
        "# Open asks\n\n"
        "| id | topic | recommendation | default-if-silent |\n"
        "| -- | -- | -- | -- |\n"
        "| 1 | cache backend | recommend: persist | proceed |\n",
        encoding="utf-8",
    )
    code, findings = run()
    assert not [f for f in findings if f[1].startswith("open-asks")]
    assert code == 0


def test_open_asks_row_missing_recommendation_is_error(run, queue):
    (queue / "OPEN-ASKS.md").write_text(
        "| id | topic | recommendation | default-if-silent |\n"
        "| -- | -- | -- | -- |\n"
        "| 1 | cache backend |  | proceed |\n",
        encoding="utf-8",
    )
    code, findings = run()
    assert ("ERROR", "open-asks-missing-recommendation") in {(s, r) for s, r, _ in findings}
    assert code == 1


def test_open_asks_row_missing_default_if_silent_is_error(run, queue):
    (queue / "OPEN-ASKS.md").write_text(
        "| id | topic | recommendation | default-if-silent |\n"
        "| -- | -- | -- | -- |\n"
        "| 2 | routing | recommend: direct | - |\n",
        encoding="utf-8",
    )
    code, findings = run()
    assert ("ERROR", "open-asks-missing-default-if-silent") in {(s, r) for s, r, _ in findings}
    assert code == 1


def test_open_asks_header_without_default_column_is_error(run, queue):
    (queue / "OPEN-ASKS.md").write_text(
        "| id | topic | recommendation |\n"
        "| -- | -- | -- |\n"
        "| 3 | deploy | recommend: lane |\n",
        encoding="utf-8",
    )
    code, findings = run()
    assert ("ERROR", "open-asks-missing-column") in {(s, r) for s, r, _ in findings}
    assert code == 1


def test_open_asks_inline_row_carrying_both_markers_is_clean(run, queue):
    """No mappable header -> the request's wording applies row-wise."""
    (queue / "OPEN-ASKS.md").write_text(
        "open asks this tick:\n\n"
        "| decide-cache | recommend: persist; default-if-silent: proceed |\n",
        encoding="utf-8",
    )
    code, findings = run()
    assert not [f for f in findings if f[1].startswith("open-asks")]
    assert code == 0


def test_open_asks_inline_row_without_markers_is_error(run, queue):
    (queue / "OPEN-ASKS.md").write_text(
        "open asks this tick:\n\n"
        "| decide-cache | just proceed |\n",
        encoding="utf-8",
    )
    code, findings = run()
    assert ("ERROR", "open-asks-row-missing-recommendation-or-default") in {(s, r) for s, r, _ in findings}
    assert code == 1


# ---- output summary line (tick finding COUNT in one clause) ----
def test_summary_line_reports_clean_counts(run, queue, capsys):
    _write_entry(queue, "604-clean.json", {"name": "604-clean", "workspace": "w", "max_rounds": 1})
    code, findings = run()
    out = capsys.readouterr().out
    assert "validate_queue findings: 0 errors, 0 warns" in out
    assert code == 0


def test_summary_line_reports_warn_counts(run, queue, capsys):
    (queue / "done" / "fleet-998-done-entry.json").write_text("{}", encoding="utf-8")
    _write_entry(
        queue,
        "605-blocked.json",
        {"name": "605-blocked", "workspace": "w", "max_rounds": 1,
         "note": "BLOCKED ON TASK 998 which has landed"},
    )
    code, findings = run()
    out = capsys.readouterr().out
    assert "validate_queue findings: 0 errors, 1 warns" in out
    # HARD RULE unchanged: WARN-only exit is 0.
    assert code == 0


def test_summary_line_reports_error_counts(run, queue, capsys):
    _write_entry(queue, "606-schema.json", {"name": "606-schema"})
    code, findings = run()
    out = capsys.readouterr().out
    errors = sum(1 for s, _, _ in findings if s == "ERROR")
    warns = sum(1 for s, _, _ in findings if s == "WARN")
    assert f"validate_queue findings: {errors} errors, {warns} warns" in out
    assert errors > 0 and code == 1