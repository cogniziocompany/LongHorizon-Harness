"""Record-store plan task 2: fixture tests for scripts/sdlc_export.py.

All fixtures live under tmp_path and carry OBVIOUSLY FAKE secrets only:
  * FAKE_HARNESS_TOKEN — a 48-char hex run of 'a' repeated (the harness token
    pattern per docs/SECRETS.md is a 48-char hex string);
  * FAKE_SK_LITERAL    — an sk- literal that says FAKE repeatedly;
  * FAKE_PRIVATE_KEY   — a PEM-shaped block whose header and body say FAKE;
  * FAKE_TOK_BODY      — body of a fake .tok* secret-by-name file;
  * FAKE_PASSWORD      — a quoted assignment value that says FAKE.
None of these is, resembles, or derives from a real credential.

Covered: classification of every audited category, redaction value
replacement with surrounding content kept, secret-by-name files recorded
without bodies, blob dedup collapsing ledger copies, dry-run being the
default that writes nothing, and the independent second scan failing the
export with a non-zero exit on any hit.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "sdlc_export.py"

# Load the script as a module (it lives in scripts/, not in a package),
# exactly as tests/test_compare_shadow.py does for scripts/compare_shadow.py.
spec = importlib.util.spec_from_file_location("sdlc_export", SCRIPT)
assert spec is not None and spec.loader is not None
sx = importlib.util.module_from_spec(spec)
sys.modules["sdlc_export"] = sx
spec.loader.exec_module(sx)

# --- obviously-fake fixture secrets (never real or real-looking) ------------

FAKE_HARNESS_TOKEN = "a" * 48  # 48-hex shape, all 'a' — synthetic filler
FAKE_SK_LITERAL = "sk-" + "FAKE" * 6 + "1234"  # 28 alnum, says FAKE 6 times
FAKE_PRIVATE_KEY = (
    "-----BEGIN FAKE PRIVATE KEY-----\n"
    + "RkFLRS1LRVktRkFLRS1LRVktRkFLRS1LRVkgZm9yIHRlc3RzIG9ubHk=\n"  # base64("FAKE-KEY-... for tests only")
    + "-----END FAKE PRIVATE KEY-----\n"
)
FAKE_TOK_BODY = "OBVIOUSLY-FAKE-TOK-FILE-BODY-for-tests-only"
FAKE_TNAS_BODY = "OBVIOUSLY-FAKE-TNAS-KEYS-BODY-for-tests-only"
FAKE_PASSWORD = "FAKE-password-123"
FAKE_REDIS_PW = "FAKE-redis-password-for-tests"
FAKE_INGEST_KEY = "FAKE-ops-ingest-key-for-tests"


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def apparatus(tmp_path: Path) -> Path:
    """A miniature of the audited legacy apparatus, one entry per class."""
    src = tmp_path / "apparatus"
    _write(src, "288-task.txt", "spec body for a fake task\n")
    _write(src, "queue/9999aa-1-fake-live.json", '{"name": "9999aa-1-fake-live"}\n')
    _write(src, "queue/blocked/005-2-fake-blocked.json", '{"name": "005-2-fake-blocked"}\n')
    _write(src, "queue/done/995-3-fake-done.json", '{"name": "995-3-fake-done"}\n')
    _write(src, "LEDGER.md", "# ledger\nrow one\n")
    _write(src, "LEDGER.md.1", "# ledger\nrow one\n")
    _write(src, "tick-0042.md", "# fake tick row\n")
    _write(src, "OPEN-ASKS.md", "# open asks (fake)\n")
    _write(src, "HANDOFF-fake-2026-09-29.md", "# fake handoff\n")
    _write(src, "run-dump-fake/rounds.jsonl", '{"round": 0}\n')
    _write(src, "scratch/launch_queue.py", f'TOKEN = "{FAKE_HARNESS_TOKEN}"\n')
    _write(src, "notes.md", "nothing special here\n")
    _write(src, ".tokfakefixture", FAKE_TOK_BODY)
    return src


# ---------------------------------------------------------------------------
# 1. Classification
# ---------------------------------------------------------------------------


def test_classification_covers_audited_categories():
    named = sx.classify
    assert named("288-task.txt") == sx.CLASS_SPEC
    assert named("fraud-review-pipeline-task.txt") == sx.CLASS_SPEC
    assert named("queue/9999aa-1-fake-live.json") == sx.CLASS_QUEUE_LIVE
    assert named("queue/blocked/005-2-fake-blocked.json") == sx.CLASS_QUEUE_BLOCKED
    assert named("queue/done/995-3-fake-done.json") == sx.CLASS_QUEUE_DONE
    assert named("queue/imported/900-4-fake-imported.json") == sx.CLASS_QUEUE_DONE
    assert named("LEDGER.md") == sx.CLASS_LEDGER
    assert named("LEDGER.md.1") == sx.CLASS_LEDGER_COPY
    assert named("LEDGER.md.bak") == sx.CLASS_LEDGER_COPY
    assert named("LEDGER - Copy.md") == sx.CLASS_LEDGER_COPY
    assert named("LEDGER (2).md") == sx.CLASS_LEDGER_COPY
    assert named("tick-0042.md") == sx.CLASS_TICK_ROW
    assert named("OPEN-ASKS.md") == sx.CLASS_OPEN_ASK
    assert named("queue/OPEN-ASKS.md") == sx.CLASS_OPEN_ASK
    assert named("HANDOFF-fake-2026-09-29.md") == sx.CLASS_HANDOFF
    assert named("run-dump-fake/rounds.jsonl") == sx.CLASS_RUN_DUMP
    assert named("scratch/launch_queue.py") == sx.CLASS_SCRATCH
    assert named("launch_queue.py.3") == sx.CLASS_SCRATCH
    assert named("quota_resume_v2.py") == sx.CLASS_SCRATCH
    assert named("notes.md") == sx.CLASS_OTHER


def test_classification_secret_files_win_by_name():
    assert sx.classify(".redis_pw") == sx.CLASS_SECRET_FILE
    assert sx.classify(".ops_ingest_key") == sx.CLASS_SECRET_FILE
    assert sx.classify(".tokfakefixture") == sx.CLASS_SECRET_FILE
    assert sx.classify("deep/inside/.tok9") == sx.CLASS_SECRET_FILE
    assert sx.classify("backup_tnas_keys.txt") == sx.CLASS_SECRET_FILE


# ---------------------------------------------------------------------------
# 2. Redaction
# ---------------------------------------------------------------------------


def test_redaction_replaces_values_and_keeps_the_rest():
    original = (
        f'TOKEN = "{FAKE_HARNESS_TOKEN}"\n'
        f"api key is {FAKE_SK_LITERAL} ok\n"
        + FAKE_PRIVATE_KEY
        + f'db_password: "{FAKE_PASSWORD}"\n'
        + "ordinary prose around the secrets stays put\n"
    )
    redacted, counts = sx.redact_body(original)
    assert FAKE_HARNESS_TOKEN not in redacted
    assert FAKE_SK_LITERAL not in redacted
    assert "FAKE-KEY" not in redacted
    assert FAKE_PASSWORD not in redacted
    assert "[REDACTED:harness_token]" in redacted
    assert "[REDACTED:sk_key]" in redacted
    assert "[REDACTED:private_key]" in redacted
    assert "[REDACTED:credential]" in redacted
    assert "ordinary prose around the secrets stays put" in redacted
    assert counts == {
        "harness_token": 1,
        "sk_key": 1,
        "private_key": 1,
        "credential": 1,
    }


def test_redaction_leaves_documented_lookalikes_alone():
    benign = (
        "git sha " + "b" * 40 + " and sha256 " + "c" * 64 + "\n"
        "see `sk-ant-api0...` (truncated doc mention)\n"
        "branch feat/task-sk-fleet-tools\n"
        'token = os.environ.get("LH_HARNESS_WEB_TOKEN")\n'
        'API_KEY = "changeme"\n'
        'PASSWORD = "<placeholder>"\n'
    )
    redacted, counts = sx.redact_body(benign)
    assert redacted == benign
    assert counts == {}


# ---------------------------------------------------------------------------
# 3. Secret-by-name files: name/size/hash only, body never stored
# ---------------------------------------------------------------------------


def test_secret_files_recorded_without_body(tmp_path: Path):
    src = tmp_path / "src"
    _write(src, ".redis_pw", FAKE_REDIS_PW)
    _write(src, ".ops_ingest_key", FAKE_INGEST_KEY)
    _write(src, ".tok-fixture", FAKE_TOK_BODY)
    _write(src, "nested/backup_tnas_keys.txt", FAKE_TNAS_BODY)
    _write(src, "LEDGER.md", "# fake ledger\n")
    out = tmp_path / "bundle"

    assert sx.main([str(src), "--write", "--out", str(out)]) == 0

    secrets = [json.loads(line) for line in (out / "secrets-by-name.jsonl").read_text().splitlines()]
    assert {row["path"] for row in secrets} == {
        ".redis_pw",
        ".ops_ingest_key",
        ".tok-fixture",
        "nested/backup_tnas_keys.txt",
    }
    for row in secrets:
        assert set(row) == {"path", "class", "size", "sha256"}
        assert len(row["sha256"]) == 64

    manifest_paths = {json.loads(line)["original_path"] for line in (out / "manifest.jsonl").read_text().splitlines()}
    assert manifest_paths == {"LEDGER.md"}

    # The fake secret bodies must appear NOWHERE in the bundle.
    bundle_bytes = b"".join(p.read_bytes() for p in out.rglob("*") if p.is_file())
    for marker in (FAKE_REDIS_PW, FAKE_INGEST_KEY, FAKE_TOK_BODY, FAKE_TNAS_BODY):
        assert marker.encode() not in bundle_bytes


# ---------------------------------------------------------------------------
# 4. Dedup: ledger copies collapse to far fewer blobs
# ---------------------------------------------------------------------------


def test_dedup_collapses_identical_copies(tmp_path: Path):
    src = tmp_path / "src"
    body = "# fake ledger shared body\nrow\n"
    _write(src, "LEDGER.md", body)
    for i in range(4):
        _write(src, f"archive/LEDGER.md.{i + 1}", body)
    _write(src, "archive/LEDGER.md.trimmed", body + "extra passage\n")
    _write(src, "tick-0001.md", "# a tick row\n")
    out = tmp_path / "bundle"

    assert sx.main([str(src), "--write", "--out", str(out)]) == 0

    blobs = [p for p in (out / "blobs").rglob("*") if p.is_file()]
    manifest = [json.loads(line) for line in (out / "manifest.jsonl").read_text().splitlines()]
    assert len(manifest) == 7  # 1 ledger + 4 identical copies + 1 trimmed + 1 tick row
    ledger_family = [m for m in manifest if m["class"] in (sx.CLASS_LEDGER, sx.CLASS_LEDGER_COPY)]
    assert len(ledger_family) == 6
    assert {m["sha256"] for m in ledger_family} == {
        ledger_family[0]["sha256"],
        ledger_family[-1]["sha256"],
    }  # exactly two distinct bodies among six copies
    assert len(blobs) == 3  # shared body + trimmed variant + tick row
    # manifest carries the contract fields per source file
    for row in manifest:
        for field in ("original_path", "class", "byte_size", "sha256", "redactions"):
            assert field in row


# ---------------------------------------------------------------------------
# 5. Dry-run is the default: classify + report only, write nothing
# ---------------------------------------------------------------------------


def test_dry_run_is_default_and_writes_nothing(tmp_path: Path, capsys, apparatus: Path):
    out = tmp_path / "should-not-exist"
    report = tmp_path / "report.jsonl"

    rc = sx.main([str(apparatus), "--out", str(out), "--report", str(report)])

    assert rc == 0
    captured = capsys.readouterr().out
    assert "dry run" in captured.lower()
    assert "harness_token" in captured  # counted, never valued
    assert FAKE_HARNESS_TOKEN not in captured
    assert not out.exists()
    assert not (tmp_path / "apparatus.bundle").exists()
    # The only artifact a dry run may write is the report itself.
    rows = [json.loads(line) for line in report.read_text().splitlines()]
    assert rows == [{"count": 1, "file": "scratch/launch_queue.py", "kind": "harness_token"}]
    assert FAKE_TOK_BODY not in captured  # secret-file bodies are never even printed


def test_explicit_dry_run_flag_matches_default(tmp_path: Path, capsys, apparatus: Path):
    assert sx.main([str(apparatus), "--dry-run"]) == 0
    assert not (tmp_path / "apparatus.bundle").exists()


# ---------------------------------------------------------------------------
# 6. Second scan: any hit fails the export non-zero
# ---------------------------------------------------------------------------


def test_second_scan_clean_export_passes(tmp_path: Path, apparatus: Path):
    out = tmp_path / "bundle"
    assert sx.main([str(apparatus), "--write", "--out", str(out)]) == 0
    assert sx.main(["--rescan", str(out)]) == 0
    assert sx.second_scan(out) == []


def test_second_scan_failure_exits_nonzero(tmp_path: Path, capsys, apparatus: Path):
    out = tmp_path / "bundle"
    assert sx.main([str(apparatus), "--write", "--out", str(out)]) == 0
    blob = next(p for p in (out / "blobs").rglob("*") if p.is_file())
    # Simulate a body that evaded the first pass landing in the bundle.
    with open(blob, "a", encoding="utf-8") as fh:
        fh.write(FAKE_HARNESS_TOKEN + "\n")

    rc = sx.main(["--rescan", str(out)])

    assert rc == sx.EXIT_SCAN_FAILED != 0
    err = capsys.readouterr().err
    assert "SECOND SCAN FAILED" in err
    assert FAKE_HARNESS_TOKEN not in err  # findings print path/kind/count only
