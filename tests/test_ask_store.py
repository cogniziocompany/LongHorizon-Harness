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
    row = store.respond(ASK, "paxton@example.com", {"response": "here it is", "secret": value}, secret_targets={"secret": TARGET})
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
        store.respond(ASK, "p@example.com", {"secret": bad}, secret_targets={"secret": TARGET})
    assert exc.value.code == 400 and bad not in str(exc.value)
    with pytest.raises(AskStoreError):
        store.respond(ASK, "p@example.com", {"secret": "x" * 4097}, secret_targets={"secret": TARGET})
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
        store.respond(ASK, "p@example.com", {"secret": _pat()}, secret_targets={"secret": TARGET})
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
    store.respond(ASK, "a@example.com", {"secret": _pat()}, secret_targets={"secret": TARGET})
    row = store.clear_secret(ASK, "secret", "a@example.com")
    assert "secret" not in row["response"]
    assert not (tmp_path / "vault" / ASK / "secret").exists()
    assert store.get(ASK)["history"][-1]["action"] == "clear_secret"
    with pytest.raises(AskStoreError) as exc:
        store.clear_secret(ASK, "secret", "a@example.com")
    assert exc.value.code == 404


def test_declare_fields_refuses_to_drop_or_retarget_a_sealed_field(tmp_path: Path) -> None:
    """Review M-B: a sealed value keeps its field type and target until cleared."""
    store = _store(tmp_path)
    store.respond(ASK, "a@example.com", {"secret": _pat()}, secret_targets={"secret": TARGET})
    for fields in (
        [{"name": "note", "type": "body"}],
        [{"name": "secret", "type": "body"}],
        [{"name": "secret", "type": "secret"}],  # target removed
    ):
        with pytest.raises(AskStoreError) as exc:
            store.declare_fields(ASK, validate_fields(fields), "overseer")
        assert exc.value.code == 409
    assert (tmp_path / "vault" / ASK / "secret").is_file()
    store.declare_fields(ASK, validate_fields([{"name": "note", "type": "body"}, *CRED_FIELDS[1:]]), "overseer")
    store.clear_secret(ASK, "secret", "a@example.com")
    store.declare_fields(ASK, validate_fields([{"name": "note", "type": "body"}]), "overseer")


def test_respond_secret_targets_must_match_declared_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(AskStoreError) as exc:
        store.respond(ASK, "a@example.com", {"secret": _pat()})
    assert exc.value.code == 400
    with pytest.raises(AskStoreError) as exc:
        store.respond(ASK, "a@example.com", {"secret": _pat()}, secret_targets={"secret": ""})
    assert exc.value.code == 409
    with pytest.raises(AskStoreError) as exc:
        store.respond(ASK, "a@example.com", {"response": "x"}, secret_targets={"response": TARGET})
    assert exc.value.code == 409
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
    store.respond(ASK, "a@example.com", {"secret": value}, secret_targets={"secret": TARGET})
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
    store.respond(ASK, "a@example.com", {"secret": value}, secret_targets={"secret": TARGET})
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
    store.respond(ASK, "a@example.com", {"secret": "not-a-github-token"}, secret_targets={"secret": TARGET})
    with pytest.raises(AskStoreError) as exc:
        store.apply_secret(ASK, "secret", TARGET, "overseer")
    assert "format" in str(exc.value) and called == []


def test_apply_refuses_off_list_and_undeclared_targets_without_touching_row(tmp_path: Path) -> None:
    store = _store(tmp_path, writer=lambda *a: pytest.fail("wrote"))
    store.respond(ASK, "a@example.com", {"secret": _pat()}, secret_targets={"secret": TARGET})
    assert LocalVault(tmp_path / "vault").status(ASK)["secret"]["target"] == TARGET
    before = store.get(ASK)
    for bad in ("ct202-mcp-tools-env:LITELLM_MASTER_KEY", "/etc/passwd", "", None):
        with pytest.raises(AskStoreError) as exc:
            store.apply_secret(ASK, "secret", bad, "overseer")
        assert exc.value.code == 403
    assert store.get(ASK) == before
    # A secret field without a declared target (the D2 default) cannot be applied.
    other = AskStore(tmp_path / "s2", vault=LocalVault(tmp_path / "v2", writer=lambda *a: pytest.fail("wrote")))
    other.raise_ask("cred-2", {"kind": "CREDENTIAL", "ask": "x", "evidence": "", "recommended": "", "default_if_silent": ""}, None, "overseer")
    other.respond("cred-2", "a@example.com", {"secret": _pat()}, secret_targets={"secret": ""})
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
    hidden = tmp_path / "x"
    hidden.mkdir()
    os.chmod(hidden, 0o755)
    os.chmod(tmp_path, 0o700)
    assert readable_by(hidden, other) is False


@pytest.mark.skipif(shutil.which("setfacl") is None, reason="needs setfacl")
def test_readable_by_counts_a_posix_acl_as_readable(open_base: Path) -> None:
    vault = open_base / "vault"
    vault.mkdir()
    os.chmod(vault, 0o700)
    other = os.getuid() + 54321
    assert readable_by(vault, other) is False
    proc = subprocess.run(["setfacl", "-m", f"u:{other}:rx", str(vault)], capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip("filesystem has no ACL support")
    os.chmod(vault, 0o700)  # mode bits alone would say "not readable"
    assert readable_by(vault, other) is True


def test_local_vault_in_runs_root_is_disabled_for_same_uid_workers(tmp_path: Path) -> None:
    runtime = load_ask_runtime(tmp_path, env={"LH_HARNESS_WORKER_UID": "999999"})
    assert runtime.disabled_reason is None
    # The env var is not trusted: workers run as this process's uid.
    assert runtime.secrets_disabled_reason.startswith(f"disabled: store readable by worker uid {os.getuid()}")


def test_local_vault_enabled_only_when_sealed_from_worker_uid(open_base: Path) -> None:
    vault = open_base / "vault"
    vault.mkdir()
    os.chmod(vault, 0o700)
    grants = open_base / "grants.toml"
    grants.write_text('[asks.grants]\noverseer = ["overseer:apply"]\n', encoding="utf-8")
    os.chmod(grants, 0o600)
    env = {"LH_HARNESS_ASK_VAULT_DIR": str(vault), "LH_HARNESS_ASK_GRANTS_FILE": str(grants)}
    other = os.getuid() + 54321
    runtime = load_ask_runtime(open_base, env=env, worker_uid=other)
    assert runtime.secrets_disabled_reason is None and runtime.grants == {"overseer": ["overseer:apply"]}
    if readable_by(open_base, other):
        os.chmod(vault, 0o755)
        assert "readable by worker uid" in load_ask_runtime(open_base, env=env, worker_uid=other).secrets_disabled_reason


SUDO_OK = """Matching Defaults entries for harness on ct110:
    env_reset, mail_badpass

User harness may run the following commands on ct110:
    (lhasks) NOPASSWD: /usr/local/sbin/lh-ask-vault
"""
CMD = ["/usr/bin/sudo", "-n", "-u", "lhasks", "/usr/local/sbin/lh-ask-vault"]


@pytest.mark.parametrize(
    "out,ok",
    [
        (SUDO_OK, True),
        (SUDO_OK.replace("(lhasks)", "(lhasks : lhasks)"), True),
        (SUDO_OK + "    (root) NOPASSWD: ALL\n", False),
        (SUDO_OK.replace("lh-ask-vault", "lh-ask-vault *"), False),
        (SUDO_OK.replace("(lhasks)", "(root)"), False),
        (SUDO_OK.replace("NOPASSWD: ", ""), False),
        ("", False),
    ],
)
def test_sudo_rule_must_be_exactly_the_helper(out: str, ok: bool) -> None:
    from lh_harness.ask_store import sudo_rule_reason

    reason = sudo_rule_reason(CMD, run=lambda *a, **k: SimpleNamespace(returncode=0, stdout=out, stderr=""))
    assert (reason is None) is ok


def _fake_helper(check_rc: int = 0, sudo_out: str = SUDO_OK):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        if argv[:3] == ["/usr/bin/sudo", "-n", "-l"]:
            return SimpleNamespace(returncode=0, stdout=sudo_out, stderr="")
        op = argv[len(CMD)]
        if op == "grants":
            return SimpleNamespace(returncode=0, stdout='{"overseer": ["overseer:apply"]}', stderr="")
        if op == "check":
            return SimpleNamespace(returncode=check_rc, stdout="", stderr="store readable by worker uid: vault_dir x" if check_rc else "ok")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    return run, calls


def test_helper_runtime_enabled_only_when_check_sudo_and_dumpable_pass(tmp_path: Path, monkeypatch) -> None:
    import lh_harness.safe_subprocess as ss

    monkeypatch.setattr(ss, "make_non_dumpable", lambda: True)
    monkeypatch.setattr(ss, "is_non_dumpable", lambda: True)
    env = {"LH_HARNESS_ASK_VAULT_CMD": " ".join(CMD)}
    run, calls = _fake_helper()
    runtime = load_ask_runtime(tmp_path, env=env, run=run)
    assert runtime.secrets_disabled_reason is None and runtime.grants == {"overseer": ["overseer:apply"]}
    assert all(kw["env"].keys() <= {"PATH", "LANG"} for _a, kw in calls)
    run, _ = _fake_helper(check_rc=4)
    assert load_ask_runtime(tmp_path, env=env, run=run).secrets_disabled_reason.startswith("disabled: store readable by worker uid")
    run, _ = _fake_helper(sudo_out=SUDO_OK + "    (root) ALL\n")
    assert "sudo" in load_ask_runtime(tmp_path, env=env, run=run).secrets_disabled_reason
    monkeypatch.setattr(ss, "is_non_dumpable", lambda: False)
    run, _ = _fake_helper()
    assert "non-dumpable" in load_ask_runtime(tmp_path, env=env, run=run).secrets_disabled_reason


def test_helper_vault_forwards_the_signed_request_only_on_stdin() -> None:
    value = _pat()
    calls = []
    vault = HelperVault(CMD, run=lambda argv, **kw: calls.append((argv, kw)) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    request = {"tool": "respond_open_ask", "arguments": {"id": ASK, "fields": {"secret": value}, "secret_targets": {"secret": TARGET}}}
    vault.seal(ASK, {"secret": (value, TARGET)}, "a@example.com", request)
    argv, kw = calls[0]
    assert argv == [*CMD, "request"] and value not in " ".join(argv)
    assert json.loads(kw["input"]) == request
    with pytest.raises(AskApplyError):
        vault.seal(ASK, {"secret": (value, TARGET)}, "a@example.com", None)  # no signed request, no call
    failing = HelperVault(CMD, run=lambda argv, **kw: SimpleNamespace(returncode=4, stdout="", stderr=f"oops {value}"))
    with pytest.raises(AskApplyError) as exc:
        failing.seal(ASK, {"secret": (value, TARGET)}, "a@example.com", request)
    assert value not in str(exc.value)


# ---------------------------------------------------------------- the helper --
SECRETS = {"fleet-admin": "fa-" + "1" * 32, "overseer": "ov-" + "2" * 32, "hydra": "hy-" + "3" * 32}


def _helper_cfg(tmp_path: Path) -> dict:
    vault = tmp_path / "hv"
    vault.mkdir(mode=0o700)
    key = tmp_path / "id_ed25519"
    key.write_text("k", encoding="utf-8")
    os.chmod(key, 0o600)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{}", encoding="utf-8")
    os.chmod(cfg_path, 0o600)
    return {
        "_path": str(cfg_path),
        "vault_dir": str(vault),
        "state_dir": str(tmp_path / "state"),
        "ssh_key": str(key),
        "ssh_dest": {TARGET: "lhapply@192.0.2.10"},
        "grants": {"fleet-admin": ["overseer:write"], "overseer": ["overseer", "overseer:apply"]},
        "callers": {"fleet-admin": SECRETS["fleet-admin"], "overseer": SECRETS["overseer"]},
    }


def _signed(tool: str, args: dict, caller: str, secret: str | None = None, ts: int | None = None) -> str:
    from lh_harness.caller_auth import ask_signature

    t = str(int(__import__("time").time()) if ts is None else ts)
    nonce = secrets.token_hex(16)
    sig = ask_signature(tool, caller, t, nonce, args, secret or SECRETS[caller])
    return json.dumps({"tool": tool, "arguments": {**args, "caller": caller, "caller_ts": t, "caller_nonce": nonce, "caller_sig": sig}})


def _req(h, cfg, payload: str, run=None) -> int:
    kw = {"run": run} if run else {}
    return h.main(["v", "request"], cfg=cfg, stdin=io.StringIO(payload), **kw)


def _respond(value: str, target: str = TARGET, ask: str = ASK) -> dict:
    return {"id": ask, "actor": "paxton@example.com", "fields": {"response": "note", "secret": value}, "secret_targets": {"secret": target}}


def test_helper_seals_only_with_a_valid_fleet_admin_request(tmp_path: Path) -> None:
    h = _helper()
    cfg = _helper_cfg(tmp_path)
    value = _pat()
    sealed = Path(cfg["vault_dir"]) / ASK / "secret"
    # H-A: argv-only verbs are gone; an unsigned or forged request is refused.
    assert h.main(["v", "seal", ASK, "secret"], cfg=cfg, stdin=io.StringIO(value + "\n")) == 2
    assert _req(h, cfg, json.dumps({"tool": "respond_open_ask", "arguments": _respond(value)})) == 5
    assert _req(h, cfg, _signed("respond_open_ask", _respond(value), "fleet-admin", secret="guessed")) == 5
    assert _req(h, cfg, _signed("respond_open_ask", _respond(value), "overseer")) == 5  # no overseer:write
    assert _req(h, cfg, _signed("respond_open_ask", _respond(value), "fleet-admin", ts=int(__import__("time").time()) + 60)) == 5
    assert not sealed.exists()
    payload = _signed("respond_open_ask", _respond(value), "fleet-admin")
    assert _req(h, cfg, payload) == 0
    assert sealed.read_text(encoding="utf-8") == value and stat.S_IMODE(sealed.stat().st_mode) == 0o600
    assert json.loads(h.op_status(cfg, ASK))["secret"]["target"] == TARGET
    # Replay of the same signed request (e.g. to re-seal) is refused, even after a restart (persisted).
    assert _req(h, cfg, payload) == 5
    assert "response" not in json.loads(h.op_status(cfg, ASK))  # body fields are never sealed


def test_helper_apply_requires_overseer_and_the_recorded_target(tmp_path: Path, capsys) -> None:
    h = _helper()
    cfg = _helper_cfg(tmp_path)
    value = _pat()
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(returncode=0, stderr="", stdout="ok")

    # Sealed with NO target (what the person saw): apply is refused.
    assert _req(h, cfg, _signed("respond_open_ask", _respond(value, target=""), "fleet-admin")) == 0
    apply_args = {"id": ASK, "field": "secret", "target": TARGET}
    assert _req(h, cfg, _signed("apply_ask_secret", apply_args, "overseer"), run=run) == 4
    assert calls == []
    # Sealed bound to the target: fleet-admin may not apply, the overseer may.
    assert _req(h, cfg, _signed("respond_open_ask", _respond(value), "fleet-admin")) == 0
    assert _req(h, cfg, _signed("apply_ask_secret", apply_args, "fleet-admin"), run=run) == 5
    assert _req(h, cfg, json.dumps({"tool": "apply_ask_secret", "arguments": apply_args}), run=run) == 5
    assert _req(h, cfg, _signed("apply_ask_secret", apply_args, "overseer"), run=run) == 0
    argv, kw = calls[0]
    assert argv[:3] == ["/usr/bin/ssh", "-F", "/dev/null"]
    assert "StrictHostKeyChecking=yes" in argv and value not in " ".join(argv)
    assert kw["input"] == value + "\n"
    assert argv[-3:] == ["lh-apply-env-var", "/opt/cognizioware-mcp-tools/mcp-tools.env", "GITHUB_MCP_TOKEN"]
    assert not (Path(cfg["vault_dir"]) / ASK / "secret").exists()
    out = capsys.readouterr()
    assert value not in out.out + out.err


def test_helper_clear_needs_overseer_write(tmp_path: Path) -> None:
    h = _helper()
    cfg = _helper_cfg(tmp_path)
    assert _req(h, cfg, _signed("respond_open_ask", _respond(_pat()), "fleet-admin")) == 0
    clear = {"id": ASK, "field": "secret", "actor": "a@example.com"}
    assert _req(h, cfg, _signed("clear_ask_secret", clear, "overseer")) == 5
    assert _req(h, cfg, _signed("clear_ask_secret", clear, "fleet-admin")) == 0
    assert _req(h, cfg, _signed("clear_ask_secret", clear, "fleet-admin")) == 3


def test_helper_check_and_grants(tmp_path: Path) -> None:
    h = _helper()
    cfg = _helper_cfg(tmp_path)
    with pytest.raises(h.Refused) as exc:
        h.op_check(cfg, os.getuid(), str(HELPER_SCRIPT))
    assert str(exc.value).startswith("store readable by worker uid")
    assert json.loads(h.op_grants(cfg)) == {"fleet-admin": ["overseer:write"], "overseer": ["overseer", "overseer:apply"]}
    assert HELPER_SCRIPT.read_text(encoding="utf-8").splitlines()[0] == "#!/usr/bin/python3 -I"


def test_helper_signature_matches_the_service(tmp_path: Path) -> None:
    from lh_harness.caller_auth import ask_signature

    h = _helper()
    args = {"id": ASK, "fields": {"secret": "naïve – ✓"}, "secret_targets": {"secret": TARGET}}
    assert h.signature("respond_open_ask", "fleet-admin", "1700000000", "n" * 32, args, "k") == ask_signature(
        "respond_open_ask", "fleet-admin", "1700000000", "n" * 32, args, "k"
    )


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
