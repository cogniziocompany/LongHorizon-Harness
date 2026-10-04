"""Task A3d: the CT110 ask store, its sealed secrets and the apply writer.

Every secret used here is a throwaway generated per test; the assertions grep
for it everywhere it must NOT be (record JSON, tool results, history, error
text, argv).
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from lh_harness.ask_store import (
    AskApplyError,
    AskStore,
    AskStoreError,
    default_fields,
    is_closed_state,
    ssh_env_file_writer,
    validate_fields,
)
from lh_harness.config import (
    ASK_APPLY_TARGETS,
    ProjectConfigError,
    config_defines_callers,
    load_ask_grants,
)

TARGET = "ct202-mcp-tools-env:GITHUB_MCP_TOKEN"
REPO = Path(__file__).resolve().parents[1]
REMOTE_SCRIPT = REPO / "scripts" / "ask-apply" / "lh-apply-env-var"


def _pat() -> str:
    return "github_pat_" + secrets.token_hex(30)


def _seed_credential(store: AskStore, ask_id: str = "279-gateway-github-token") -> None:
    store.raise_ask(
        ask_id,
        {"kind": "CREDENTIAL", "ask": "PAT for github_mcp", "evidence": "", "recommended": "", "default_if_silent": ""},
        None,
        actor="overseer",
    )


def _all_text(root: Path) -> str:
    out = []
    for path in (root / "asks").rglob("*"):
        if path.is_file():
            out.append(path.read_text(encoding="utf-8"))
    return "\n".join(out)


# ------------------------------------------------------------------ defaults --
def test_credential_defaults_one_body_and_one_secret() -> None:
    assert [(f["name"], f["type"]) for f in default_fields("CREDENTIAL")] == [("response", "body"), ("secret", "secret")]
    assert [(f["name"], f["type"]) for f in default_fields("DECISION")] == [("response", "body")]
    assert [(f["name"], f["type"]) for f in default_fields("GATE")] == [("response", "body")]


def test_allow_list_has_exactly_the_spec_target() -> None:
    assert set(ASK_APPLY_TARGETS) == {TARGET}
    spec = ASK_APPLY_TARGETS[TARGET]
    assert spec["file"] == "/opt/cognizioware-mcp-tools/mcp-tools.env"
    assert spec["variable"] == "GITHUB_MCP_TOKEN"


# ------------------------------------------------------------- secret sealing --
def test_secret_sealed_0600_and_never_in_json_or_result(tmp_path: Path) -> None:
    store = AskStore(tmp_path)
    _seed_credential(store)
    value = _pat()
    row = store.respond("279-gateway-github-token", "paxton@example.com", {"response": "here it is", "secret": value})
    assert value not in json.dumps(row)
    assert row["response"]["secret"]["length"] == len(value)
    assert row["response"]["secret"]["set_by"] == "paxton@example.com"
    assert row["response"]["response"] == "here it is"
    assert value not in _all_text(tmp_path)
    assert value not in json.dumps(store.public_rows())
    sealed = tmp_path / "asks-secrets" / "279-gateway-github-token" / "secret"
    assert sealed.read_text(encoding="utf-8") == value
    assert stat.S_IMODE(sealed.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "asks-secrets").stat().st_mode) == 0o700
    assert stat.S_IMODE(sealed.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "asks").stat().st_mode) == 0o700


def test_secret_limits_refused_without_echo(tmp_path: Path) -> None:
    store = AskStore(tmp_path)
    _seed_credential(store)
    bad = "line1\nline2-" + secrets.token_hex(8)
    with pytest.raises(AskStoreError) as exc:
        store.respond("279-gateway-github-token", "p@example.com", {"secret": bad})
    assert exc.value.code == 400 and bad not in str(exc.value)
    with pytest.raises(AskStoreError):
        store.respond("279-gateway-github-token", "p@example.com", {"secret": "x" * 4097})
    with pytest.raises(AskStoreError):
        store.respond("279-gateway-github-token", "p@example.com", {"response": "y" * 20_001})
    # Nothing was written by the refused submits.
    assert not (tmp_path / "asks-secrets" / "279-gateway-github-token" / "secret").exists()
    assert not is_closed_state(store.get("279-gateway-github-token")["state"])


def test_unknown_field_and_empty_submit_refused(tmp_path: Path) -> None:
    store = AskStore(tmp_path)
    _seed_credential(store)
    with pytest.raises(AskStoreError) as exc:
        store.respond("279-gateway-github-token", "p@example.com", {"nope": "x"})
    assert exc.value.code == 400
    with pytest.raises(AskStoreError) as exc:
        store.respond("279-gateway-github-token", "p@example.com", {"response": "  "})
    assert exc.value.code == 422


# ------------------------------------------------------------- D3 transitions --
def test_submit_closes_and_edit_reopens_then_closes(tmp_path: Path) -> None:
    store = AskStore(tmp_path)
    _seed_credential(store)
    first = store.respond("279-gateway-github-token", "a@example.com", {"response": "one"})
    assert first["state"].startswith("CLOSED ") and first["state"].endswith(" by a@example.com (web)")
    second = store.respond("279-gateway-github-token", "b@example.com", {"response": "two"})
    assert second["state"].endswith(" by b@example.com (web)")
    actions = [(h["action"], h["actor"]) for h in store.get("279-gateway-github-token")["history"]]
    assert actions == [
        ("raise", "overseer"),
        ("respond", "a@example.com"),
        ("reopen", "b@example.com"),
        ("respond", "b@example.com"),
    ]


def test_clear_secret_deletes_file_and_records_history(tmp_path: Path) -> None:
    store = AskStore(tmp_path)
    _seed_credential(store)
    store.respond("279-gateway-github-token", "a@example.com", {"secret": _pat()})
    row = store.clear_secret("279-gateway-github-token", "secret", "a@example.com")
    assert "secret" not in row["response"]
    assert not (tmp_path / "asks-secrets" / "279-gateway-github-token" / "secret").exists()
    assert store.get("279-gateway-github-token")["history"][-1]["action"] == "clear_secret"
    with pytest.raises(AskStoreError) as exc:
        store.clear_secret("279-gateway-github-token", "secret", "a@example.com")
    assert exc.value.code == 404


def test_declare_fields_removing_secret_deletes_sealed_value(tmp_path: Path) -> None:
    store = AskStore(tmp_path)
    _seed_credential(store)
    store.respond("279-gateway-github-token", "a@example.com", {"secret": _pat()})
    store.declare_fields("279-gateway-github-token", validate_fields([{"name": "note", "type": "body"}]), "overseer")
    assert not (tmp_path / "asks-secrets" / "279-gateway-github-token" / "secret").exists()
    assert [f["name"] for f in store.public_rows()[0]["fields"]] == ["note"]


def test_validate_fields_rules() -> None:
    with pytest.raises(AskStoreError):
        validate_fields([])
    with pytest.raises(AskStoreError):
        validate_fields([{"name": "a", "type": "body"}, {"name": "a", "type": "secret"}])
    with pytest.raises(AskStoreError):
        validate_fields([{"name": "../x", "type": "secret"}])
    with pytest.raises(AskStoreError):
        validate_fields([{"name": f"f{i}", "type": "body"} for i in range(9)])


def test_ids_cannot_escape_the_store(tmp_path: Path) -> None:
    store = AskStore(tmp_path)
    for bad in ("../etc", ".hidden", "a/b", ""):
        with pytest.raises(AskStoreError):
            store.respond(bad, "a@example.com", {"response": "x"})


# ---------------------------------------------------------------------- apply --
def test_apply_success_returns_only_handle_and_purges_sealed_copy(tmp_path: Path) -> None:
    store = AskStore(tmp_path)
    _seed_credential(store)
    value = _pat()
    store.respond("279-gateway-github-token", "a@example.com", {"secret": value})
    seen = []
    result = store.apply_secret(
        "279-gateway-github-token", "secret", TARGET, "overseer", writer=lambda t, s, v: seen.append((t, v))
    )
    assert set(result) == {"applied", "target", "at"} and result["applied"] is True
    assert seen == [(TARGET, value)]
    assert not (tmp_path / "asks-secrets" / "279-gateway-github-token" / "secret").exists()
    row = store.public_rows()[0]
    assert row["response"]["secret"]["applied_target"] == TARGET
    assert is_closed_state(row["state"])


def test_apply_failure_reopens_with_reason_and_redacts_value(tmp_path: Path) -> None:
    store = AskStore(tmp_path)
    _seed_credential(store)
    value = _pat()
    store.respond("279-gateway-github-token", "a@example.com", {"secret": value})

    def boom(_t, _s, v):
        raise AskApplyError(f"remote said no to {v}")

    with pytest.raises(AskStoreError) as exc:
        store.apply_secret("279-gateway-github-token", "secret", TARGET, "overseer", writer=boom)
    assert exc.value.code == 502 and value not in str(exc.value)
    record = store.get("279-gateway-github-token")
    assert record["state"] == "OPEN"
    assert "apply failed" in record["evidence"] and TARGET in record["evidence"]
    assert value not in json.dumps(record)
    assert record["history"][-1]["action"] == "apply_failed"
    # The sealed copy stays for a retry.
    assert (tmp_path / "asks-secrets" / "279-gateway-github-token" / "secret").is_file()


def test_apply_without_secret_or_bad_format_reopens(tmp_path: Path) -> None:
    store = AskStore(tmp_path)
    _seed_credential(store)
    store.respond("279-gateway-github-token", "a@example.com", {"response": "no token today"})
    with pytest.raises(AskStoreError):
        store.apply_secret("279-gateway-github-token", "secret", TARGET, "overseer", writer=lambda *a: None)
    assert store.get("279-gateway-github-token")["state"] == "OPEN"
    store.respond("279-gateway-github-token", "a@example.com", {"secret": "not-a-github-token"})
    called = []
    with pytest.raises(AskStoreError) as exc:
        store.apply_secret("279-gateway-github-token", "secret", TARGET, "overseer", writer=lambda *a: called.append(1))
    assert "format" in str(exc.value) and called == []
    assert store.get("279-gateway-github-token")["state"] == "OPEN"


def test_apply_refuses_target_off_allow_list_without_touching_row(tmp_path: Path) -> None:
    store = AskStore(tmp_path)
    _seed_credential(store)
    store.respond("279-gateway-github-token", "a@example.com", {"secret": _pat()})
    before = store.get("279-gateway-github-token")
    for bad in ("ct202-mcp-tools-env:LITELLM_MASTER_KEY", "/etc/passwd", "", None):
        with pytest.raises(AskStoreError) as exc:
            store.apply_secret("279-gateway-github-token", "secret", bad, "overseer", writer=lambda *a: pytest.fail("wrote"))
        assert exc.value.code == 403
    assert store.get("279-gateway-github-token") == before


# ----------------------------------------------------------------- ssh writer --
def test_ssh_writer_sends_value_on_stdin_only() -> None:
    value = _pat()
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(returncode=0, stderr="", stdout="ok GITHUB_MCP_TOKEN")

    env = {"LH_HARNESS_ASK_APPLY_CT202_SSH": "root@ct202.lan", "LH_HARNESS_ASK_APPLY_SSH_KEY": "/k/id"}
    ssh_env_file_writer(TARGET, ASK_APPLY_TARGETS[TARGET], value, run=fake_run, env=env)
    argv, kw = calls[0]
    assert value not in " ".join(argv)
    assert kw["input"] == value + "\n"
    assert "StrictHostKeyChecking=yes" in argv and "BatchMode=yes" in argv
    assert argv[-3:] == ["lh-apply-env-var", "/opt/cognizioware-mcp-tools/mcp-tools.env", "GITHUB_MCP_TOKEN"]


def test_ssh_writer_failures_are_reasons_without_value() -> None:
    value = _pat()
    with pytest.raises(AskApplyError, match="LH_HARNESS_ASK_APPLY_CT202_SSH is not set"):
        ssh_env_file_writer(TARGET, ASK_APPLY_TARGETS[TARGET], value, env={})
    env = {"LH_HARNESS_ASK_APPLY_CT202_SSH": "-oProxyCommand=x", "LH_HARNESS_ASK_APPLY_SSH_KEY": "/k"}
    with pytest.raises(AskApplyError, match="plain user@host"):
        ssh_env_file_writer(TARGET, ASK_APPLY_TARGETS[TARGET], value, env=env)
    env["LH_HARNESS_ASK_APPLY_CT202_SSH"] = "root@ct202"
    with pytest.raises(AskApplyError) as exc:
        ssh_env_file_writer(
            TARGET,
            ASK_APPLY_TARGETS[TARGET],
            value,
            env=env,
            run=lambda *a, **k: SimpleNamespace(returncode=255, stderr=f"echo {value} denied"),
        )
    assert value not in str(exc.value) and "255" in str(exc.value)


# -------------------------------------------------------------- remote script --
@pytest.mark.skipif(shutil.which("sh") is None or shutil.which("awk") is None, reason="needs sh + awk")
def test_remote_script_replaces_one_variable_atomically(tmp_path: Path) -> None:
    env_file = tmp_path / "mcp-tools.env"
    env_file.write_text("A=1\nGITHUB_MCP_TOKEN=placeholder\nB=2\nGITHUB_MCP_TOKEN=dup\n", encoding="utf-8")
    os.chmod(env_file, 0o640)
    value = _pat()
    proc = subprocess.run(
        ["sh", str(REMOTE_SCRIPT), str(env_file), "GITHUB_MCP_TOKEN"],
        input=value + "\n", capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert value not in proc.stdout + proc.stderr
    assert env_file.read_text(encoding="utf-8") == f"A=1\nGITHUB_MCP_TOKEN={value}\nB=2\n"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o640
    assert [p.name for p in tmp_path.iterdir()] == ["mcp-tools.env"]


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")
@pytest.mark.parametrize("value", ["has space", "quote'x", "dollar$x", ""])
def test_remote_script_refuses_unsafe_values(tmp_path: Path, value: str) -> None:
    env_file = tmp_path / "x.env"
    env_file.write_text("GITHUB_MCP_TOKEN=keep\n", encoding="utf-8")
    proc = subprocess.run(
        ["sh", str(REMOTE_SCRIPT), str(env_file), "GITHUB_MCP_TOKEN"],
        input=value + "\n", capture_output=True, text=True, check=False,
    )
    assert proc.returncode != 0
    assert env_file.read_text(encoding="utf-8") == "GITHUB_MCP_TOKEN=keep\n"


# --------------------------------------------------------------------- config --
def test_asks_grants_parse_and_do_not_enable_callers_scoping(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[asks.grants]\n"fleet-admin" = ["overseer:write"]\noverseer = ["overseer", "overseer:apply"]\n',
        encoding="utf-8",
    )
    assert load_ask_grants(cfg) == {"fleet-admin": ["overseer:write"], "overseer": ["overseer", "overseer:apply"]}
    assert config_defines_callers(cfg) is False
    assert load_ask_grants(tmp_path / "missing.toml") == {}


@pytest.mark.parametrize(
    "body",
    [
        '[asks.grants]\nx = ["admin"]\n',
        '[asks.grants]\nanon = ["overseer"]\n',
        '[asks]\nother = 1\n',
        '[asks.grants]\nx = "overseer"\n',
    ],
)
def test_asks_grants_rejects_bad_tables(tmp_path: Path, body: str) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(body, encoding="utf-8")
    with pytest.raises(ProjectConfigError):
        load_ask_grants(cfg)
