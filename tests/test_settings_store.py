"""Embedded settings store: encryption at rest, audit, precedence, import."""

from __future__ import annotations

import os
import secrets
import sqlite3
import stat
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from lh_harness import settings_store as ss
from lh_harness.settings_store import SettingsError, SettingsStore, apply_startup_settings

KEY = "k" * 40


def _token() -> str:
    return "tok-" + secrets.token_hex(24)


def _store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(tmp_path / "db" / "settings.db", KEY)


def test_secret_encrypted_at_rest_and_file_modes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    value = _token()
    meta = store.set("LH_HARNESS_WEB_TOKEN", value, "admin@example.com")
    assert meta["secret"] is True and "value" not in meta and meta["length"] == len(value)
    raw = (tmp_path / "db" / "settings.db").read_bytes()
    assert value.encode() not in raw
    assert stat.S_IMODE((tmp_path / "db" / "settings.db").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "db").stat().st_mode) == 0o700
    assert store.values()["LH_HARNESS_WEB_TOKEN"] == value


def test_metadata_list_and_audit_never_carry_secret_values(tmp_path: Path) -> None:
    store = _store(tmp_path)
    value = _token()
    store.set("GH_TOKEN", value, "a@example.com")
    store.set("GH_TOKEN", _token(), "b@example.com")  # rotate
    store.set("LH_HARNESS_FLEET_URL", "https://fleet.example", "a@example.com")
    store.unset("LH_HARNESS_FLEET_URL", "a@example.com")
    blob = repr(store.list_metadata()) + repr(store.audit())
    assert value not in blob
    assert [(e["action"], e["name"], e["actor"]) for e in store.audit()] == [
        ("unset", "LH_HARNESS_FLEET_URL", "a@example.com"),
        ("set", "LH_HARNESS_FLEET_URL", "a@example.com"),
        ("rotate", "GH_TOKEN", "b@example.com"),
        ("set", "GH_TOKEN", "a@example.com"),
    ]


def test_plain_settings_are_shown_and_secret_names_cannot_be_plain(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.set("LH_HARNESS_FLEET_URL", "https://f.example", "a@example.com")["value"] == "https://f.example"
    meta = store.set("MY_CUSTOM_API_KEY", _token(), "a@example.com", secret=False)
    assert meta["secret"] is True and "value" not in meta


def test_ciphertext_is_bound_to_its_name(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set("GH_TOKEN", _token(), "a@example.com")
    store.set("SEQ_API_KEY", _token(), "a@example.com")
    db = sqlite3.connect(tmp_path / "db" / "settings.db")
    enc = db.execute("SELECT value_enc FROM settings WHERE name='GH_TOKEN'").fetchone()[0]
    db.execute("UPDATE settings SET value_enc=? WHERE name='SEQ_API_KEY'", (enc,))
    db.commit()
    db.close()
    with pytest.raises(Exception):
        store.values()


def test_wrong_key_is_refused(tmp_path: Path) -> None:
    _store(tmp_path).set("GH_TOKEN", _token(), "a@example.com")
    with pytest.raises(SettingsError) as exc:
        SettingsStore(tmp_path / "db" / "settings.db", "x" * 40)
    assert "does not match" in str(exc.value)
    with pytest.raises(SettingsError):
        SettingsStore(tmp_path / "other.db", "short")


@pytest.mark.parametrize("name", ["lowercase", "LH_HARNESS_SETTINGS_KEY", "LH_HARNESS_SETTINGS_DB", "A", "BAD-NAME"])
def test_names_validated_and_bootstrap_names_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(SettingsError):
        _store(tmp_path).set(name, "v", "a@example.com")


@pytest.mark.parametrize("value", ["", "two\nlines", "x" * 8193])
def test_values_validated(tmp_path: Path, value: str) -> None:
    with pytest.raises(SettingsError):
        _store(tmp_path).set("LH_HARNESS_FLEET_URL", value, "a@example.com")


# -------------------------------------------------------------- precedence --
def test_startup_db_wins_over_env_and_bootstrap_secrets_leave_env(tmp_path: Path) -> None:
    db_path = tmp_path / "settings.db"
    store = SettingsStore(db_path, KEY)
    db_token = _token()
    store.set("LH_HARNESS_WEB_TOKEN", db_token, "a@example.com")
    store.set("LH_HARNESS_FLEET_URL", "https://from-db.example", "a@example.com")
    environ = {
        "LH_HARNESS_SETTINGS_DB": str(db_path),
        "LH_HARNESS_SETTINGS_KEY": KEY,
        "LH_HARNESS_SETTINGS_PROXY_AUTH": "p" * 32,
        "LH_HARNESS_SETTINGS_ADMINS": "Paxton@Example.com, b@example.com",
        "LH_HARNESS_WEB_TOKEN": "env-token-loses",
        "GH_TOKEN": "env-only-stays",
    }
    rt = apply_startup_settings(environ)
    assert environ["LH_HARNESS_WEB_TOKEN"] == db_token
    assert environ["LH_HARNESS_FLEET_URL"] == "https://from-db.example"
    assert environ["GH_TOKEN"] == "env-only-stays"
    assert "LH_HARNESS_SETTINGS_KEY" not in environ and "LH_HARNESS_SETTINGS_PROXY_AUTH" not in environ
    assert rt.sources["LH_HARNESS_WEB_TOKEN"] == "db" and rt.sources["GH_TOKEN"] == "env"
    assert rt.admins == ("paxton@example.com", "b@example.com") and rt.proxy_auth == "p" * 32
    assert rt.is_admin("PAXTON@example.com") and not rt.is_admin("c@example.com")


def test_startup_without_key_or_with_wrong_key_keeps_env(tmp_path: Path) -> None:
    environ = {"LH_HARNESS_WEB_TOKEN": "env"}
    rt = apply_startup_settings(environ)
    assert rt.store is None and "not configured" in rt.error and environ["LH_HARNESS_WEB_TOKEN"] == "env"
    SettingsStore(tmp_path / "s.db", KEY).set("LH_HARNESS_WEB_TOKEN", _token(), "a@example.com")
    environ = {"LH_HARNESS_SETTINGS_DB": str(tmp_path / "s.db"), "LH_HARNESS_SETTINGS_KEY": "w" * 40, "LH_HARNESS_WEB_TOKEN": "env"}
    rt = apply_startup_settings(environ)
    assert rt.store is None and "does not match" in rt.error and environ["LH_HARNESS_WEB_TOKEN"] == "env"
    assert "LH_HARNESS_SETTINGS_KEY" not in environ


def test_cli_applies_settings_before_web_reads_its_token() -> None:
    src = (Path(__file__).resolve().parents[1] / "src" / "lh_harness" / "cli.py").read_text(encoding="utf-8")
    assert src.index("apply_startup_settings()") < src.index('default=os.environ.get("LH_HARNESS_WEB_TOKEN")')


def test_workers_never_inherit_settings_bootstrap() -> None:
    from lh_harness.safe_subprocess import scrub_env

    env = {"LH_HARNESS_SETTINGS_DB": "/x", "LH_HARNESS_SETTINGS_ADMINS": "a", "LH_HARNESS_MCP_GATEWAY_KEY": "kept", "GH_TOKEN": "kept"}
    assert scrub_env(env) == {"LH_HARNESS_MCP_GATEWAY_KEY": "kept", "GH_TOKEN": "kept"}  # MCP access unchanged


# ------------------------------------------------------------------ import --
def test_import_env_file_reports_names_only(tmp_path: Path, capsys) -> None:
    from lh_harness.cli import main

    web = _token()
    env_file = tmp_path / "secrets.env"
    env_file.write_text(
        "# comment\n"
        f"LH_HARNESS_WEB_TOKEN={web}\n"
        "LH_HARNESS_FLEET_URL='https://fleet.example'\n"
        "export GH_TOKEN=ghp_" + "a" * 36 + "\n"
        "LH_HARNESS_MCP_GATEWAY_KEY=\n"
        "LH_HARNESS_SETTINGS_KEY=should-not-import-" + "z" * 20 + "\n"
        "not a line\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "settings.db"
    os.environ["LH_HARNESS_SETTINGS_DB"] = str(db_path)
    os.environ["LH_HARNESS_SETTINGS_KEY"] = KEY
    try:
        assert main(["settings", "import", "--env-file", str(env_file), "--actor", "operator"]) == 0
        out = capsys.readouterr().out
        assert main(["settings", "list"]) == 0
        listed = capsys.readouterr().out
    finally:
        os.environ.pop("LH_HARNESS_SETTINGS_DB", None)
        os.environ.pop("LH_HARNESS_SETTINGS_KEY", None)
    assert web not in out + listed and "ghp_" not in out + listed and "fleet.example" not in out
    assert "LH_HARNESS_WEB_TOKEN: imported (secret)" in out
    assert "LH_HARNESS_FLEET_URL: imported" in out
    assert "GH_TOKEN: imported (secret)" in out
    assert "LH_HARNESS_MCP_GATEWAY_KEY: skipped: empty" in out
    assert "LH_HARNESS_SETTINGS_KEY: skipped: bootstrap" in out
    assert "(line 7): skipped" in out
    values = SettingsStore(db_path, KEY).values()
    assert values["LH_HARNESS_WEB_TOKEN"] == web and values["LH_HARNESS_FLEET_URL"] == "https://fleet.example"
    audit = SettingsStore(db_path, KEY).audit()
    assert all(e["actor"] == "operator" and e["detail"]["source"] == "import" for e in audit)


# ------------------------------------------- queue trios + capacity (v2) --
QUEUE_CONFIG_TOML = """\
[queue]
observe = false

[queue.trios.kimi]
agent = "claude_code"
model = "glm-5.2:synthetic-anthropic"
mcp_profile = "default"

[queue.trios.qwen]
agent = "claude_code"
model = "qwen3.8"
mcp_profile = "default"

[queue.trios.glm]
agent = "claude_code"
model = "glm-5.3:zai"
mcp_profile = "default"

[queue.capacity]
glm_max = 2
kimi_max = 1
qwen_max = 2
"""


def _config_file(tmp_path: Path, text: str = QUEUE_CONFIG_TOML) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(text, encoding="utf-8")
    return cfg


def test_schema_v2_fresh_db_has_empty_queue_tables(tmp_path: Path) -> None:
    path = tmp_path / "db" / "settings.db"
    store = SettingsStore(path, KEY)
    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        raw.close()
    assert {"meta", "settings", "audit", "queue_trio", "queue_capacity"} <= tables
    assert store.queue_trios() == [] and store.queue_capacity() == {}


def test_seed_from_config_toml_populates_trios_and_capacity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cfg = _config_file(tmp_path)
    report = store.seed_queue_from_config(cfg)
    assert report == {"trios": ["kimi", "qwen", "glm"], "capacity": ["glm", "kimi", "qwen"]}
    trios = {t["name"]: t for t in store.queue_trios()}
    assert set(trios) == {"kimi", "qwen", "glm"}
    assert trios["glm"] == {
        "name": "glm", "agent": "claude_code", "model": "glm-5.3:zai",
        "mcp_profile": "default", "enabled": True,
        "updated_by": ss.SEED_ACTOR, "updated_at": trios["glm"]["updated_at"],
    }
    assert trios["kimi"]["model"] == "glm-5.2:synthetic-anthropic" and trios["qwen"]["model"] == "qwen3.8"
    assert all(t["updated_at"] for t in trios.values())
    assert store.queue_capacity() == {"glm": 2, "kimi": 1, "qwen": 2}
    seeds = [e for e in store.audit() if e["action"] == "seed"]
    assert {e["name"] for e in seeds} == {
        "queue_trio:kimi", "queue_trio:qwen", "queue_trio:glm",
        "queue_capacity:glm", "queue_capacity:kimi", "queue_capacity:qwen",
    }
    assert all(e["actor"] == ss.SEED_ACTOR and e["detail"]["source"] == str(cfg) for e in seeds)
    glm_seed = next(e for e in seeds if e["name"] == "queue_trio:glm")
    assert glm_seed["detail"]["model"] == "glm-5.3:zai" and glm_seed["detail"]["mcp_profile"] == "default"
    cap_seed = next(e for e in seeds if e["name"] == "queue_capacity:glm")
    assert cap_seed["detail"]["max_concurrent"] == 2
    # the bootstrap file is read-only for the seed: no writes to config.toml
    assert cfg.read_text(encoding="utf-8") == QUEUE_CONFIG_TOML


def test_reseed_leaves_existing_rows_untouched(tmp_path: Path) -> None:
    path = tmp_path / "db" / "settings.db"
    store = SettingsStore(path, KEY)
    store.seed_queue_from_config(_config_file(tmp_path))
    raw = sqlite3.connect(path)
    raw.execute("UPDATE queue_trio SET model='glm-5.2:synthetic-anthropic', updated_by='admin:op' WHERE name='glm'")
    raw.execute("UPDATE queue_capacity SET max_concurrent=9 WHERE trio='glm'")
    raw.commit()
    raw.close()
    # config.toml changed afterwards (the whole point of hot reload: the DB wins)
    cfg = _config_file(tmp_path, QUEUE_CONFIG_TOML.replace("glm_max = 2", "glm_max = 3"))
    before = len(store.audit())
    report = SettingsStore(path, KEY).seed_queue_from_config(cfg)
    assert report == {"trios": None, "capacity": None}
    kept = {t["name"]: t for t in store.queue_trios()}
    assert kept["glm"]["model"] == "glm-5.2:synthetic-anthropic" and kept["glm"]["updated_by"] == "admin:op"
    assert store.queue_capacity()["glm"] == 9
    assert len(store.audit()) == before  # a skip seeds and audits nothing


def test_queue_tables_seed_independently(tmp_path: Path) -> None:
    path = tmp_path / "db" / "settings.db"
    store = SettingsStore(path, KEY)
    cfg = _config_file(tmp_path)
    store.seed_queue_from_config(cfg)
    raw = sqlite3.connect(path)
    raw.execute("DELETE FROM queue_capacity")
    raw.execute("UPDATE queue_trio SET model='hand-edited' WHERE name='glm'")
    raw.commit()
    raw.close()
    report = SettingsStore(path, KEY).seed_queue_from_config(cfg)
    assert report == {"trios": None, "capacity": ["glm", "kimi", "qwen"]}
    assert store.queue_capacity() == {"glm": 2, "kimi": 1, "qwen": 2}
    assert {t["name"]: t["model"] for t in store.queue_trios()}["glm"] == "hand-edited"


def test_seed_without_config_or_with_empty_queue_section_is_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.seed_queue_from_config(tmp_path / "missing.toml") is None
    cfg = _config_file(tmp_path, "[run]\nmodel = 'x'\n")
    assert store.seed_queue_from_config(cfg) == {"trios": [], "capacity": []}
    assert store.queue_trios() == [] and store.queue_capacity() == {}
    assert [e for e in store.audit() if e["action"] == "seed"] == []


def test_trio_rows_needing_an_agent_and_caps_needing_an_int_are_skipped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cfg = _config_file(
        tmp_path,
        "[queue.trios.broken]\nmodel='no-agent'\n"
        "[queue.trios.ok]\nagent='codex'\n"
        "[queue.capacity]\nok_max = 4\nweird_max = 'not-an-int'\naverage = 9\n",
    )
    assert store.seed_queue_from_config(cfg) == {"trios": ["ok"], "capacity": ["ok"]}
    assert [t["name"] for t in store.queue_trios()] == ["ok"]
    assert store.queue_capacity() == {"ok": 4}


def test_migration_from_v1_preserves_settings_meta_and_audit(tmp_path: Path) -> None:
    path = tmp_path / "db" / "settings.db"
    store = SettingsStore(path, KEY)
    token = _token()
    store.set("LH_HARNESS_WEB_TOKEN", token, "<EMAIL_ADDRESS>")
    store.set("LH_HARNESS_FLEET_URL", "https://fleet.example", "<EMAIL_ADDRESS>")
    # Downgrade to exactly what a v1 build left behind: no queue tables, version 1.
    raw = sqlite3.connect(path)
    raw.execute("DROP TABLE queue_trio")
    raw.execute("DROP TABLE queue_capacity")
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()
    migrated = SettingsStore(path, KEY)  # raises if the key check (meta) broke
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 2
    assert raw.execute("SELECT COUNT(*) FROM queue_trio").fetchone()[0] == 0
    raw.close()
    assert migrated.values()["LH_HARNESS_WEB_TOKEN"] == token
    assert migrated.values()["LH_HARNESS_FLEET_URL"] == "https://fleet.example"
    assert [(e["action"], e["name"]) for e in migrated.audit()] == [
        ("set", "LH_HARNESS_FLEET_URL"), ("set", "LH_HARNESS_WEB_TOKEN"),
    ]
    # a migrated v1 database still seeds like a fresh one
    assert migrated.seed_queue_from_config(_config_file(tmp_path))["trios"] == ["kimi", "qwen", "glm"]
    assert migrated.queue_capacity()["glm"] == 2
