"""Task A3d: the CT110 ask store, its vaults, the self-check and the helpers.

Every secret here is a throwaway generated per test; the assertions grep for
it everywhere it must NOT be (records, tool results, history, errors, argv).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
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
    HelperVault,
    LocalVault,
    default_fields,
    is_closed_state,
    load_ask_runtime,
    readable_by,
    scrub_worker_env,
    validate_fields,
)
from lh_harness.config import ASK_APPLY_TARGETS, ProjectConfigError, config_defines_callers, load_ask_grants, load_run_defaults

TARGET = "ct202-mcp-tools-env:GITHUB_MCP_TOKEN"
REPO = Path(__file__).resolve().parents[1]
REMOTE_SCRIPT = REPO / "scripts" / "ask-apply" / "lh-apply-env-var"
HELPER_SCRIPT = REPO / "scripts" / "ask-apply" / "lh-ask-vault"
ASK = "279-gateway-github-token"
CRED_FIELDS = [
    {"name": "response", "type": "body"},
    {"name": "secret", "type": "secret", "apply_target": TARGET},
]


def _pat() -> str:
    return "github_pat_" + secrets.token_hex(30)


def _helper():
    loader = importlib.machinery.SourceFileLoader("lh_ask_vault", str(HELPER_SCRIPT))
    spec = importlib.util.spec_from_loader("lh_ask_vault", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _store(tmp_path: Path, writer=None, fields=CRED_FIELDS) -> AskStore:
    store = AskStore(tmp_path / "store", vault=LocalVault(tmp_path / "vault", writer=writer))
    store.raise_ask(
        ASK,
        {"kind": "CREDENTIAL", "ask": "PAT for github_mcp", "evidence": "", "recommended": "", "default_if_silent": ""},
        validate_fields(fields) if fields else None,
        actor="overseer",
    )
    return store


def _all_text(root: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in root.rglob("*") if p.is_file())


# ------------------------------------------------------------------ defaults --
def test_credential_defaults_one_body_and_one_secret_without_target() -> None:
    assert [(f["name"], f["type"]) for f in default_fields("CREDENTIAL")] == [("response", "body"), ("secret", "secret")]
    assert all("apply_target" not in f for f in default_fields("CREDENTIAL"))
    assert [(f["name"], f["type"]) for f in default_fields("DECISION")] == [("response", "body")]


def test_allow_list_has_exactly_the_spec_target_and_helper_matches() -> None:
    assert set(ASK_APPLY_TARGETS) == {TARGET}
    assert ASK_APPLY_TARGETS[TARGET]["file"] == "/opt/cognizioware-mcp-tools/mcp-tools.env"
    assert ASK_APPLY_TARGETS[TARGET]["variable"] == "GITHUB_MCP_TOKEN"
    assert _helper().ASK_APPLY_TARGETS == ASK_APPLY_TARGETS


# ------------------------------------------------------------- secret sealing --
def test_secret_sealed_0600_and_never_in_json_or_result(tmp_path: Path) -> None:
    store = _store(tmp_path)
    value = _pat()
    row = store.respond(ASK, "paxton@example.com", {"response": "here it is", "secret": value})
    assert value not in json.dumps(row)
    assert row["response"]["secret"]["length"] == len(value)
    assert value not in _all_text(tmp_path / "store")
    sealed = tmp_path / "vault" / ASK / "secret"
    assert sealed.read_text(encoding="utf-8") == value
    assert stat.S_IMODE(sealed.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "vault").stat().st_mode) == 0o700
    assert stat.S_IMODE(sealed.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "store" / "asks").stat().st_mode) == 0o700


def test_secret_limits_refused_without_echo(tmp_path: Path) -> None:
    store = _store(tmp_path)
    bad = "line1\nline2-" + secrets.token_hex(8)
    with pytest.raises(AskStoreError) as exc:
        store.respond(ASK, "p@example.com", {"secret": bad})
    assert exc.value.code == 400 and bad not in str(exc.value)
    with pytest.raises(AskStoreError):
        store.respond(ASK, "p@example.com", {"secret": "x" * 4097})
    with pytest.raises(AskStoreError):
        store.respond(ASK, "p@example.com", {"response": "y" * 20_001})
    assert not (tmp_path / "vault" / ASK / "secret").exists()
    assert not is_closed_state(store.get(ASK)["state"])


@pytest.mark.parametrize(
    "text",
    ["token: github_pat_" + "a" * 40, "use ghp_" + "b" * 36, "-----BEGIN OPENSSH PRIVATE KEY-----", "AKIA" + "A" * 16],
)
def test_token_shaped_body_refused_with_hint(tmp_path: Path, text: str) -> None:
    store = _store(tmp_path)
    with pytest.raises(AskStoreError) as exc:
        store.respond(ASK, "p@example.com", {"response": text})
    assert exc.value.code == 422 and "secret field" in str(exc.value)


def test_unknown_field_and_empty_submit_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(AskStoreError) as exc:
        store.respond(ASK, "p@example.com", {"nope": "x"})
    assert exc.value.code == 400
    with pytest.raises(AskStoreError) as exc:
        store.respond(ASK, "p@example.com", {"response": "  "})
    assert exc.value.code == 422


def test_secret_tools_disabled_reason_blocks_secrets_not_bodies(tmp_path: Path) -> None:
    _store(tmp_path)
    store = AskStore(tmp_path / "store", vault=LocalVault(tmp_path / "vault"), secrets_disabled_reason="disabled: store readable by worker uid 1000 (x)")
    with pytest.raises(AskStoreError) as exc:
        store.respond(ASK, "p@example.com", {"secret": _pat()})
    assert exc.value.code == 503 and exc.value.args[0].startswith("disabled: store readable by worker uid")
    assert store.respond(ASK, "p@example.com", {"response": "fine"})["response"]["response"] == "fine"
    for call in (lambda: store.clear_secret(ASK, "secret", "p@example.com"), lambda: store.apply_secret(ASK, "secret", TARGET, "overseer")):
        with pytest.raises(AskStoreError) as exc:
            call()
        assert exc.value.code == 503


# ------------------------------------------------------------- D3 transitions --
def test_submit_closes_and_edit_reopens_then_closes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.respond(ASK, "a@example.com", {"response": "one"})
    assert first["state"].startswith("CLOSED ") and first["state"].endswith(" by a@example.com (web)")
    second = store.respond(ASK, "b@example.com", {"response": "two"})
    assert second["state"].endswith(" by b@example.com (web)")
    actions = [(h["action"], h["actor"]) for h in store.get(ASK)["history"]]
    assert actions == [("raise", "overseer"), ("respond", "a@example.com"), ("reopen", "b@example.com"), ("respond", "b@example.com")]


def test_clear_secret_deletes_and_records_history(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.respond(ASK, "a@example.com", {"secret": _pat()})
    row = store.clear_secret(ASK, "secret", "a@example.com")
    assert "secret" not in row["response"]
    assert not (tmp_path / "vault" / ASK / "secret").exists()
    assert store.get(ASK)["history"][-1]["action"] == "clear_secret"
    with pytest.raises(AskStoreError) as exc:
        store.clear_secret(ASK, "secret", "a@example.com")
    assert exc.value.code == 404


def test_declare_fields_removing_secret_deletes_sealed_value(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.respond(ASK, "a@example.com", {"secret": _pat()})
    store.declare_fields(ASK, validate_fields([{"name": "note", "type": "body"}]), "overseer")
    assert not (tmp_path / "vault" / ASK / "secret").exists()


def test_validate_fields_rules() -> None:
    for bad in (
        [],
        [{"name": "a", "type": "body"}, {"name": "a", "type": "secret"}],
        [{"name": "../x", "type": "secret"}],
        [{"name": "x\n", "type": "secret"}],
        [{"name": f"f{i}", "type": "body"} for i in range(9)],
        [{"name": "a", "type": "body", "apply_target": TARGET}],
        [{"name": "a", "type": "secret", "apply_target": "ct202-mcp-tools-env:LITELLM_MASTER_KEY"}],
    ):
        with pytest.raises(AskStoreError):
            validate_fields(bad)


def test_ids_and_fields_use_fullmatch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for bad in ("../etc", ".hidden", "a/b", "", "abc\n"):
        with pytest.raises(AskStoreError):
            store.respond(bad, "a@example.com", {"response": "x"})
    with pytest.raises(AskStoreError):
        store.clear_secret(ASK, "secret\n", "a@example.com")


# ---------------------------------------------------------------------- apply --
def test_apply_success_returns_only_handle_and_purges(tmp_path: Path) -> None:
    seen = []
    store = _store(tmp_path, writer=lambda t, s, v: seen.append((t, v)))
    value = _pat()
    store.respond(ASK, "a@example.com", {"secret": value})
    result = store.apply_secret(ASK, "secret", TARGET, "overseer")
    assert set(result) == {"applied", "target", "at"} and result["applied"] is True
    assert seen == [(TARGET, value)]
    assert not (tmp_path / "vault" / ASK / "secret").exists()
    row = store.public_rows()[0]
    assert row["response"]["secret"]["applied_target"] == TARGET and is_closed_state(row["state"])


def test_apply_failure_reopens_with_reason_and_redacts(tmp_path: Path) -> None:
    def boom(_t, _s, v):
        raise AskApplyError(f"remote said no to {v}")

    store = _store(tmp_path, writer=boom)
    value = _pat()
    store.respond(ASK, "a@example.com", {"secret": value})
    with pytest.raises(AskStoreError) as exc:
        store.apply_secret(ASK, "secret", TARGET, "overseer")
    assert exc.value.code == 502 and value not in str(exc.value)
    record = store.get(ASK)
    assert record["state"] == "OPEN" and "apply failed" in record["evidence"] and TARGET in record["evidence"]
    assert value not in json.dumps(record)
    assert (tmp_path / "vault" / ASK / "secret").is_file()


def test_apply_without_secret_or_bad_format_reopens(tmp_path: Path) -> None:
    called = []
    store = _store(tmp_path, writer=lambda *a: called.append(1))
    store.respond(ASK, "a@example.com", {"response": "no token today"})
    with pytest.raises(AskStoreError):
        store.apply_secret(ASK, "secret", TARGET, "overseer")
    assert store.get(ASK)["state"] == "OPEN"
    store.respond(ASK, "a@example.com", {"secret": "not-a-github-token"})
    with pytest.raises(AskStoreError) as exc:
        store.apply_secret(ASK, "secret", TARGET, "overseer")
    assert "format" in str(exc.value) and called == []


def test_apply_refuses_off_list_and_undeclared_targets_without_touching_row(tmp_path: Path) -> None:
    store = _store(tmp_path, writer=lambda *a: pytest.fail("wrote"))
    store.respond(ASK, "a@example.com", {"secret": _pat()})
    before = store.get(ASK)
    for bad in ("ct202-mcp-tools-env:LITELLM_MASTER_KEY", "/etc/passwd", "", None):
        with pytest.raises(AskStoreError) as exc:
            store.apply_secret(ASK, "secret", bad, "overseer")
        assert exc.value.code == 403
    assert store.get(ASK) == before
    # A secret field without a declared target (the D2 default) cannot be applied.
    other = AskStore(tmp_path / "s2", vault=LocalVault(tmp_path / "v2", writer=lambda *a: pytest.fail("wrote")))
    other.raise_ask("cred-2", {"kind": "CREDENTIAL", "ask": "x", "evidence": "", "recommended": "", "default_if_silent": ""}, None, "overseer")
    other.respond("cred-2", "a@example.com", {"secret": _pat()})
    with pytest.raises(AskStoreError) as exc:
        other.apply_secret("cred-2", "secret", TARGET, "overseer")
    assert exc.value.code == 409


# ------------------------------------------------------------- worker env (H1) --
def test_worker_env_has_no_caller_secrets_or_ask_settings() -> None:
    env = {
        "PATH": "/usr/bin",
        "OLLAMA_API_KEY": "provider-key-kept",
        "LH_HARNESS_WEB_TOKEN": "x",
        "LH_HARNESS_CALLER_OVERSEER_SECRET": "x",
        "LH_HARNESS_CALLER_FLEET_ADMIN_SECRET": "x",
        "LH_HARNESS_ASK_VAULT_CMD": "/usr/bin/sudo",
        "LH_HARNESS_ASK_STORE_DIR": "/srv/asks",
        "LH_HARNESS_ASK_VAULT_DIR": "/srv/vault",
        "LH_HARNESS_ASK_GRANTS_FILE": "/etc/grants.toml",
        "LH_HARNESS_ASK_APPLY_SSH_KEY": "/k",
    }
    out = scrub_worker_env(env)
    assert out == {"PATH": "/usr/bin", "OLLAMA_API_KEY": "provider-key-kept"}


def test_supervisor_builds_worker_env_with_scrub() -> None:
    source = (REPO / "src" / "lh_harness" / "supervisor" / "service.py").read_text(encoding="utf-8")
    assert "worker_env = scrub_worker_env(dict(os.environ))" in source
    assert "worker_env = os.environ.copy()" not in source


# ---------------------------------------------------------- self-check (H1) --
@pytest.fixture
def open_base():
    """A world-searchable base dir, so only the leaf mode decides (pytest's
    own temp base is 0700, which already hides everything from other uids)."""
    import tempfile

    base = Path(tempfile.mkdtemp())
    os.chmod(base, 0o755)
    yield base
    shutil.rmtree(base, ignore_errors=True)


def test_readable_by_respects_mode_owner_and_parents(open_base: Path, tmp_path: Path) -> None:
    secret_dir = open_base / "vault"
    secret_dir.mkdir()
    os.chmod(secret_dir, 0o700)
    me = os.getuid()
    other = me + 54321
    assert readable_by(secret_dir, me) is True
    assert readable_by(secret_dir, other) is False
    os.chmod(secret_dir, 0o755)
    if not readable_by(open_base, other):
        pytest.skip("temp dir parents are not searchable by other uids here")
    assert readable_by(secret_dir, other) is True
    # A non-searchable parent hides an otherwise world-readable leaf.
    hidden = tmp_path / "x"
    hidden.mkdir()
    os.chmod(hidden, 0o755)
    os.chmod(tmp_path, 0o700)
    assert readable_by(hidden, other) is False


def test_local_vault_in_runs_root_is_disabled_for_same_uid_workers(tmp_path: Path) -> None:
    runtime = load_ask_runtime(tmp_path, env={})
    assert runtime.disabled_reason is None
    assert runtime.secrets_disabled_reason.startswith(f"disabled: store readable by worker uid {os.getuid()}")


def test_local_vault_enabled_only_when_sealed_from_worker_uid(open_base: Path) -> None:
    vault = open_base / "vault"
    vault.mkdir()
    os.chmod(vault, 0o700)
    grants = open_base / "grants.toml"
    grants.write_text('[asks.grants]\noverseer = ["overseer:apply"]\n', encoding="utf-8")
    os.chmod(grants, 0o600)
    env = {
        "LH_HARNESS_ASK_VAULT_DIR": str(vault),
        "LH_HARNESS_ASK_GRANTS_FILE": str(grants),
        "LH_HARNESS_WORKER_UID": str(os.getuid() + 54321),
    }
    runtime = load_ask_runtime(open_base, env=env)
    assert runtime.secrets_disabled_reason is None and runtime.grants == {"overseer": ["overseer:apply"]}
    if readable_by(open_base, os.getuid() + 54321):
        os.chmod(vault, 0o755)
        assert "readable by worker uid" in load_ask_runtime(open_base, env=env).secrets_disabled_reason
        os.chmod(vault, 0o700)
        os.chmod(grants, 0o644)
        assert "grants.toml" in load_ask_runtime(open_base, env=env).secrets_disabled_reason


def test_helper_vault_runtime_uses_helper_grants_and_check(tmp_path: Path) -> None:
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        op = argv[len(["/usr/bin/sudo", "-n", "-u", "lhasks", "/usr/local/sbin/lh-ask-vault"])]
        if op == "grants":
            return SimpleNamespace(returncode=0, stdout='{"overseer": ["overseer:apply"]}', stderr="")
        if op == "check":
            return SimpleNamespace(returncode=4, stdout="", stderr="store readable by worker uid: vault_dir x")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    import lh_harness.ask_store as mod

    real = mod.HelperVault.__init__

    def init(self, command, run=None):
        real(self, command, run=fake_run)

    mod.HelperVault.__init__ = init  # type: ignore[method-assign]
    try:
        runtime = load_ask_runtime(tmp_path, env={"LH_HARNESS_ASK_VAULT_CMD": "/usr/bin/sudo -n -u lhasks /usr/local/sbin/lh-ask-vault"})
    finally:
        mod.HelperVault.__init__ = real  # type: ignore[method-assign]
    assert runtime.grants == {"overseer": ["overseer:apply"]}
    assert runtime.secrets_disabled_reason.startswith("disabled: store readable by worker uid")
    assert all(kw["env"].keys() == {"PATH", "LANG"} for _a, kw in calls)


def test_helper_vault_never_puts_value_in_argv() -> None:
    value = _pat()
    calls = []
    vault = HelperVault(["/usr/local/sbin/lh-ask-vault"], run=lambda argv, **kw: calls.append((argv, kw)) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    vault.seal(ASK, "secret", value)
    argv, kw = calls[0]
    assert value not in " ".join(argv) and kw["input"] == value + "\n"
    failing = HelperVault(["/x"], run=lambda argv, **kw: SimpleNamespace(returncode=4, stdout="", stderr=f"oops {value}"))
    with pytest.raises(AskApplyError) as exc:
        failing.seal(ASK, "secret", value)
    assert value not in str(exc.value)


# ---------------------------------------------------------------- the helper --
def _helper_cfg(tmp_path: Path) -> dict:
    vault = tmp_path / "hv"
    vault.mkdir(mode=0o700)
    key = tmp_path / "id_ed25519"
    key.write_text("k", encoding="utf-8")
    os.chmod(key, 0o600)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{}", encoding="utf-8")
    os.chmod(cfg_path, 0o640)
    return {
        "_path": str(cfg_path),
        "vault_dir": str(vault),
        "ssh_key": str(key),
        "ssh_dest": {TARGET: "lhapply@192.0.2.10"},
        "grants": {"fleet-admin": ["overseer:write"]},
    }


def test_helper_seal_has_clear_and_grants(tmp_path: Path) -> None:
    h = _helper()
    cfg = _helper_cfg(tmp_path)
    value = _pat()
    assert h.main(["v", "seal", ASK, "secret"], cfg=cfg, stdin=io.StringIO(value + "\n")) == 0
    sealed = Path(cfg["vault_dir"]) / ASK / "secret"
    assert sealed.read_text(encoding="utf-8") == value and stat.S_IMODE(sealed.stat().st_mode) == 0o600
    assert h.main(["v", "has", ASK, "secret"], cfg=cfg) == 0
    assert h.main(["v", "clear", ASK, "secret"], cfg=cfg) == 0
    assert h.main(["v", "has", ASK, "secret"], cfg=cfg) == 3
    assert h.main(["v", "seal", "../x", "secret"], cfg=cfg, stdin=io.StringIO("v\n")) == 2
    assert json.loads(h.op_grants(cfg)) == {"fleet-admin": ["overseer:write"]}
    assert h.main(["v", "read", ASK, "secret"], cfg=cfg) == 2  # there is no read operation


def test_helper_apply_uses_ssh_F_dev_null_and_stdin_only(tmp_path: Path, capsys) -> None:
    h = _helper()
    cfg = _helper_cfg(tmp_path)
    value = _pat()
    h.main(["v", "seal", ASK, "secret"], cfg=cfg, stdin=io.StringIO(value + "\n"))
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(returncode=0, stderr="", stdout="ok")

    assert h.main(["v", "apply", ASK, "secret", TARGET], cfg=cfg, run=run) == 0
    argv, kw = calls[0]
    assert argv[:3] == ["/usr/bin/ssh", "-F", "/dev/null"]
    assert "StrictHostKeyChecking=yes" in argv and value not in " ".join(argv)
    assert kw["input"] == value + "\n"
    assert argv[-3:] == ["lh-apply-env-var", "/opt/cognizioware-mcp-tools/mcp-tools.env", "GITHUB_MCP_TOKEN"]
    assert not (Path(cfg["vault_dir"]) / ASK / "secret").exists()
    h.main(["v", "seal", ASK, "secret"], cfg=cfg, stdin=io.StringIO(value + "\n"))
    assert h.main(["v", "apply", ASK, "secret", "other:TARGET"], cfg=cfg, run=run) == 4
    assert h.main(["v", "apply", ASK, "secret", TARGET], cfg=cfg, run=lambda *a, **k: SimpleNamespace(returncode=255, stderr=f"no {value}")) == 4
    out = capsys.readouterr()
    assert value not in out.out + out.err


def test_helper_check_refuses_paths_the_worker_can_read(tmp_path: Path) -> None:
    h = _helper()
    cfg = _helper_cfg(tmp_path)
    with pytest.raises(h.Refused) as exc:
        h.op_check(cfg, os.getuid(), str(HELPER_SCRIPT))
    assert str(exc.value).startswith("store readable by worker uid")


# -------------------------------------------------------------- remote script --
def _run_remote(env_file: Path, value: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(REMOTE_SCRIPT), str(env_file), "GITHUB_MCP_TOKEN"],
        input=value + "\n", capture_output=True, text=True, check=False,
    )


@pytest.mark.skipif(shutil.which("sh") is None or shutil.which("awk") is None, reason="needs sh + awk")
def test_remote_script_replaces_one_variable_atomically(tmp_path: Path) -> None:
    env_file = tmp_path / "mcp-tools.env"
    env_file.write_text("A=1\nGITHUB_MCP_TOKEN=placeholder\nB=2\nGITHUB_MCP_TOKEN=dup\n", encoding="utf-8")
    os.chmod(env_file, 0o640)
    value = _pat()
    proc = _run_remote(env_file, value)
    assert proc.returncode == 0, proc.stderr
    assert value not in proc.stdout + proc.stderr
    assert env_file.read_text(encoding="utf-8") == f"A=1\nGITHUB_MCP_TOKEN={value}\nB=2\n"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o640
    assert [p.name for p in tmp_path.iterdir()] == ["mcp-tools.env"]


@pytest.mark.skipif(shutil.which("sh") is None or shutil.which("awk") is None, reason="needs sh + awk")
def test_remote_script_handles_export_lines(tmp_path: Path) -> None:
    env_file = tmp_path / "mcp-tools.env"
    env_file.write_text("export GITHUB_MCP_TOKEN=old\nGITHUB_MCP_TOKENX=keep\n", encoding="utf-8")
    value = _pat()
    assert _run_remote(env_file, value).returncode == 0
    assert env_file.read_text(encoding="utf-8") == f"export GITHUB_MCP_TOKEN={value}\nGITHUB_MCP_TOKENX=keep\n"


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")
@pytest.mark.parametrize("value", ["has space", "quote'x", "dollar$x", ""])
def test_remote_script_refuses_unsafe_values(tmp_path: Path, value: str) -> None:
    env_file = tmp_path / "x.env"
    env_file.write_text("GITHUB_MCP_TOKEN=keep\n", encoding="utf-8")
    assert _run_remote(env_file, value).returncode != 0
    assert env_file.read_text(encoding="utf-8") == "GITHUB_MCP_TOKEN=keep\n"


# --------------------------------------------------------------- config (M1) --
def test_asks_grants_parse_in_own_loader_and_not_in_run_defaults(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('[asks.grants]\n"fleet-admin" = ["overseer:write"]\noverseer = ["overseer", "overseer:apply"]\n', encoding="utf-8")
    assert load_ask_grants(cfg) == {"fleet-admin": ["overseer:write"], "overseer": ["overseer", "overseer:apply"]}
    assert config_defines_callers(cfg) is False
    assert "ask_grants" not in load_run_defaults(cfg)


def test_bad_asks_table_never_breaks_run_defaults_and_disables_ask_tools(tmp_path: Path) -> None:
    lh = tmp_path / ".lh-harness"
    lh.mkdir()
    (lh / "config.toml").write_text('[asks]\ngrnats = 1\n', encoding="utf-8")
    load_run_defaults(lh / "config.toml")  # must not raise
    runtime = load_ask_runtime(tmp_path, env={})
    assert runtime.disabled_reason.startswith("disabled: bad [asks]")


@pytest.mark.parametrize(
    "body",
    ['[asks.grants]\nx = ["admin"]\n', '[asks.grants]\nanon = ["overseer"]\n', '[asks]\nother = 1\n', '[asks.grants]\nx = "overseer"\n'],
)
def test_asks_grants_rejects_bad_tables(tmp_path: Path, body: str) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(body, encoding="utf-8")
    with pytest.raises(ProjectConfigError):
        load_ask_grants(cfg)
