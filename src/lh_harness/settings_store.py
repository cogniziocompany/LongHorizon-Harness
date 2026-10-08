"""Embedded settings store for lh-harness configuration and secrets.

lh-harness keeps its own configuration and credentials in a SQLite database
it creates and migrates itself, instead of an environment file. Admins change
values from the web UI (``/admin``); the service reads them at start.

- Secret values are encrypted at rest (AES-256-GCM, the setting name bound
  as associated data) and are never returned by any API or UI: they are
  write-only and show only who set them, when, and their length.
- Non-secret values are stored and shown in plain text.
- Every change is recorded in an audit log, without values.
- Precedence at start: a value in the store wins over the same name in the
  environment. The environment remains only as the bootstrap: where the
  database is (``LH_HARNESS_SETTINGS_DB``) and its key
  (``LH_HARNESS_SETTINGS_KEY``), plus the admin bootstrap
  (``LH_HARNESS_SETTINGS_ADMINS``, ``LH_HARNESS_SETTINGS_PROXY_AUTH``).

See docs/settings-store.md.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, MutableMapping

logger = logging.getLogger(__name__)

DB_ENV = "LH_HARNESS_SETTINGS_DB"
KEY_ENV = "LH_HARNESS_SETTINGS_KEY"
ADMINS_ENV = "LH_HARNESS_SETTINGS_ADMINS"
PROXY_AUTH_ENV = "LH_HARNESS_SETTINGS_PROXY_AUTH"
BOOTSTRAP_NAMES = frozenset({DB_ENV, KEY_ENV, ADMINS_ENV, PROXY_AUTH_ENV})
DEFAULT_DB_PATH = Path.home() / ".lh-harness" / "settings.db"

NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
_SECRET_NAME_RE = re.compile(r"(TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL)")
MAX_VALUE_CHARS = 8192
MAX_NOTE_CHARS = 500
MIN_KEY_CHARS = 32
SCHEMA_VERSION = 1
_KEY_CHECK_PLAINTEXT = b"lh-harness settings key check v1"


class SettingsError(ValueError):
    """A refused settings operation; ``code`` is the HTTP-ish status."""

    def __init__(self, message: str, code: int = 400) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SettingSpec:
    """A setting lh-harness knows about (shown even while unset)."""

    name: str
    description: str
    secret: bool
    restart: bool = True
    kind: str = "config"  # "credential" = a key/caller in the key restriction view
    restriction: str = ""


def _caller_spec(caller: str) -> SettingSpec:
    env = f"LH_HARNESS_CALLER_{caller.upper().replace('-', '_')}_SECRET"
    return SettingSpec(env, f"HMAC secret of the harness caller '{caller}'", True, kind="credential",
                       restriction=f"caller:{caller}")


CATALOG: tuple[SettingSpec, ...] = (
    SettingSpec("LH_HARNESS_WEB_TOKEN", "Bearer token for this node's web API and MCP endpoint", True,
                kind="credential", restriction="bearer"),
    _caller_spec("overseer"),
    _caller_spec("fleet-admin"),
    _caller_spec("hydra"),
    _caller_spec("operator"),
    _caller_spec("chat-agent"),
    SettingSpec("LH_HARNESS_MCP_GATEWAY_KEY", "LiteLLM gateway key that runs use for MCP tools", True,
                kind="credential", restriction="gateway"),
    SettingSpec("LH_HARNESS_MCP_GATEWAY_URL", "MCP gateway URL (empty = public gateway, 'lan' = LAN alias)", False),
    SettingSpec("GH_TOKEN", "GitHub token runs use (scope it to this node's repos)", True,
                kind="credential", restriction="github"),
    SettingSpec("LH_HARNESS_FLEET_KEY", "This node's fleet-admin device key", True,
                kind="credential", restriction="fleet"),
    SettingSpec("LH_HARNESS_FLEET_URL", "fleet-admin ingest origin (reporting is off while empty)", False),
    SettingSpec("LH_HARNESS_FLEET_NODE", "Host name this node is enrolled under in fleet-admin", False),
    SettingSpec("LH_HARNESS_FLEET_LABELS", "Comma-separated key=value labels for fleet reporting", False),
    SettingSpec("LH_HARNESS_MAX_ARTIFACT_BYTES",
                "Inline artifact size cap in bytes (default 8388608); larger run files upload as gzip chunks", False),
    SettingSpec("LH_HARNESS_MAX_TRANSCRIPT_BYTES",
                "Hard cap in bytes for chunked transcript uploads (default 67108864); larger files keep the truncated marker", False),
    SettingSpec("LH_HARNESS_DB_PASSWORD", "Password of the Postgres queue store", True,
                kind="credential", restriction="postgres"),
    SettingSpec("ANTHROPIC_AUTH_TOKEN", "Model-provider token runs use", True, kind="credential",
                restriction="provider"),
    SettingSpec("LH_HARNESS_ASK_VAULT_CMD", "Fixed command of the open-asks vault helper", False),
    SettingSpec("LH_HARNESS_ASK_STORE_DIR", "Directory for open-asks records (default: the runs root)", False),
    SettingSpec("LH_HARNESS_WEB_DEFAULT_MODEL", "Default model for runs created from the web workbench", False),
    SettingSpec("LH_HARNESS_WORKER_MEMORY_MAX", "Per-run worker memory cap (e.g. 2G)", False),
    SettingSpec("LH_HARNESS_LOG_LEVEL", "Log level of the service", False),
    SettingSpec("SEQ_URL", "Seq log ingest URL", False),
    SettingSpec("SEQ_API_KEY", "Seq API key", True, kind="credential", restriction="seq"),
)
CATALOG_BY_NAME = {spec.name: spec for spec in CATALOG}


def is_secret_name(name: str) -> bool:
    spec = CATALOG_BY_NAME.get(name)
    if spec is not None:
        return spec.secret
    return bool(_SECRET_NAME_RE.search(name))


def spec_for(name: str) -> SettingSpec:
    return CATALOG_BY_NAME.get(name) or SettingSpec(
        name, "Custom setting", is_secret_name(name),
        kind="credential" if is_secret_name(name) else "config",
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_name(name: Any) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise SettingsError("name must match " + NAME_RE.pattern, 400)
    if name in BOOTSTRAP_NAMES:
        raise SettingsError(f"{name} is a bootstrap variable; it stays in the environment", 400)
    return name


def validate_value(value: Any) -> str:
    if not isinstance(value, str):
        raise SettingsError("value must be a string", 400)
    if not value or len(value) > MAX_VALUE_CHARS:
        raise SettingsError(f"value must be 1..{MAX_VALUE_CHARS} characters", 400)
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise SettingsError("value must not contain control characters or newlines", 400)
    return value


def _derive_key(raw: str) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    if not isinstance(raw, str) or len(raw) < MIN_KEY_CHARS:
        raise SettingsError(f"{KEY_ENV} must be at least {MIN_KEY_CHARS} characters", 500)
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"lh-harness settings v1").derive(
        raw.encode("utf-8")
    )


class SettingsStore:
    """SQLite settings store (file 0600, directory 0700)."""

    def __init__(self, path: str | Path, key: str) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        self.path = Path(path)
        self._aead = AESGCM(_derive_key(key))
        self._init()

    # ---------------------------------------------------------------- plumbing --
    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.path.parent, 0o700)
        if not self.path.exists():
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        os.chmod(self.path, 0o600)
        with self._db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version < 1:
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value BLOB NOT NULL);
                    CREATE TABLE IF NOT EXISTS settings (
                        name TEXT PRIMARY KEY,
                        secret INTEGER NOT NULL,
                        value_plain TEXT,
                        value_enc BLOB,
                        length INTEGER NOT NULL,
                        note TEXT NOT NULL DEFAULT '',
                        expires_at TEXT,
                        updated_at TEXT NOT NULL,
                        updated_by TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        at TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        action TEXT NOT NULL,
                        name TEXT NOT NULL,
                        detail TEXT NOT NULL DEFAULT '{}'
                    );
                    """
                )
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            row = db.execute("SELECT value FROM meta WHERE name = 'key_check'").fetchone()
            if row is None:
                db.execute("INSERT INTO meta(name, value) VALUES ('key_check', ?)", (self._seal("key_check", _KEY_CHECK_PLAINTEXT),))
            else:
                try:
                    plain = self._open("key_check", row["value"])
                except Exception:
                    plain = None
                if plain != _KEY_CHECK_PLAINTEXT:
                    raise SettingsError(f"{KEY_ENV} does not match the settings store at {self.path}", 500)

    def _seal(self, name: str, data: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._aead.encrypt(nonce, data, name.encode("utf-8"))

    def _open(self, name: str, blob: bytes) -> bytes:
        return self._aead.decrypt(blob[:12], blob[12:], name.encode("utf-8"))

    def _audit(self, db: sqlite3.Connection, actor: str, action: str, name: str, **detail: Any) -> None:
        db.execute(
            "INSERT INTO audit(at, actor, action, name, detail) VALUES (?, ?, ?, ?, ?)",
            (now_iso(), actor, action, name, json.dumps(detail, sort_keys=True)),
        )

    # ------------------------------------------------------------------ writes --
    def set(
        self,
        name: str,
        value: str,
        actor: str,
        *,
        secret: bool | None = None,
        note: str | None = None,
        expires_at: str | None = None,
        source: str = "admin",
    ) -> dict[str, Any]:
        name = validate_name(name)
        value = validate_value(value)
        actor = _validate_actor(actor)
        secret_flag = is_secret_name(name) if secret is None else bool(secret)
        if is_secret_name(name):
            secret_flag = True  # a credential-shaped or catalogued secret name is never plain
        if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE_CHARS or "\n" in note):
            raise SettingsError(f"note must be at most {MAX_NOTE_CHARS} characters on one line", 400)
        if expires_at not in (None, ""):
            try:
                datetime.strptime(str(expires_at), "%Y-%m-%d")
            except ValueError:
                raise SettingsError("expires_at must be YYYY-MM-DD", 400) from None
        with self._db() as db:
            existing = db.execute("SELECT name, note, expires_at FROM settings WHERE name = ?", (name,)).fetchone()
            enc = self._seal(name, value.encode("utf-8")) if secret_flag else None
            db.execute(
                """INSERT INTO settings(name, secret, value_plain, value_enc, length, note, expires_at, updated_at, updated_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET secret=excluded.secret, value_plain=excluded.value_plain,
                     value_enc=excluded.value_enc, length=excluded.length, note=excluded.note,
                     expires_at=excluded.expires_at, updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
                (
                    name, int(secret_flag), None if secret_flag else value, enc, len(value),
                    (note if note is not None else (existing["note"] if existing else "")) or "",
                    (expires_at or None) if expires_at is not None else (existing["expires_at"] if existing else None),
                    now_iso(), actor,
                ),
            )
            self._audit(db, actor, "rotate" if existing else "set", name, secret=secret_flag, source=source)
        return self.metadata(name)

    def update_meta(self, name: str, actor: str, *, note: str | None = None, expires_at: str | None = None) -> dict[str, Any]:
        name = validate_name(name)
        actor = _validate_actor(actor)
        if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE_CHARS or "\n" in note):
            raise SettingsError(f"note must be at most {MAX_NOTE_CHARS} characters on one line", 400)
        if expires_at not in (None, ""):
            try:
                datetime.strptime(str(expires_at), "%Y-%m-%d")
            except ValueError:
                raise SettingsError("expires_at must be YYYY-MM-DD", 400) from None
        with self._db() as db:
            if db.execute("SELECT 1 FROM settings WHERE name = ?", (name,)).fetchone() is None:
                raise SettingsError(f"{name} is not set", 404)
            if note is not None:
                db.execute("UPDATE settings SET note = ? WHERE name = ?", (note, name))
            if expires_at is not None:
                db.execute("UPDATE settings SET expires_at = ? WHERE name = ?", (expires_at or None, name))
            self._audit(db, actor, "annotate", name)
        return self.metadata(name)

    def unset(self, name: str, actor: str) -> None:
        name = validate_name(name)
        actor = _validate_actor(actor)
        with self._db() as db:
            cur = db.execute("DELETE FROM settings WHERE name = ?", (name,))
            if cur.rowcount == 0:
                raise SettingsError(f"{name} is not set", 404)
            self._audit(db, actor, "unset", name)

    # ------------------------------------------------------------------- reads --
    def _value_of(self, row: sqlite3.Row) -> str:
        if row["secret"]:
            return self._open(row["name"], row["value_enc"]).decode("utf-8")
        return row["value_plain"]

    def values(self) -> dict[str, str]:
        """Every stored value, decrypted. Internal: for the service start only."""
        with self._db() as db:
            rows = db.execute("SELECT * FROM settings ORDER BY name").fetchall()
        return {row["name"]: self._value_of(row) for row in rows}

    def metadata(self, name: str) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT * FROM settings WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise SettingsError(f"{name} is not set", 404)
        return self._public(row)

    def _public(self, row: sqlite3.Row) -> dict[str, Any]:
        out = {
            "name": row["name"],
            "secret": bool(row["secret"]),
            "set": True,
            "length": row["length"],
            "note": row["note"],
            "expires_at": row["expires_at"],
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
        }
        if not row["secret"]:
            out["value"] = row["value_plain"]
        return out

    def list_metadata(self) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM settings ORDER BY name").fetchall()
        return [self._public(row) for row in rows]

    def audit(self, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._db() as db:
            rows = db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [
            {"id": r["id"], "at": r["at"], "actor": r["actor"], "action": r["action"], "name": r["name"],
             "detail": json.loads(r["detail"] or "{}")}
            for r in rows
        ]

    # ------------------------------------------------------------------ import --
    def import_env_file(self, path: str | Path, actor: str) -> list[dict[str, str]]:
        """One-shot import of NAME=value lines. Returns names and outcomes, never values."""
        report: list[dict[str, str]] = []
        text = Path(path).read_text(encoding="utf-8")
        for lineno, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            name, sep, value = line.partition("=")
            name = name.strip()
            if not sep or not NAME_RE.fullmatch(name):
                report.append({"name": f"(line {lineno})", "outcome": "skipped: not NAME=value"})
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if name in BOOTSTRAP_NAMES:
                report.append({"name": name, "outcome": "skipped: bootstrap variable stays in the environment"})
                continue
            if not value:
                report.append({"name": name, "outcome": "skipped: empty"})
                continue
            try:
                meta = self.set(name, value, actor, source="import")
            except SettingsError as exc:
                report.append({"name": name, "outcome": f"skipped: {exc}"})
                continue
            report.append({"name": name, "outcome": "imported (secret)" if meta["secret"] else "imported"})
        return report


def _validate_actor(actor: Any) -> str:
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 254 or any(ord(c) < 32 for c in actor):
        raise SettingsError("actor is required", 400)
    return actor.strip()


# ------------------------------------------------------------------- runtime --
@dataclass
class SettingsRuntime:
    """What the web service knows about the store, resolved at start."""

    store: SettingsStore | None = None
    error: str | None = None
    admins: tuple[str, ...] = ()
    proxy_auth: str | None = None
    started_at: str = field(default_factory=now_iso)
    sources: dict[str, str] = field(default_factory=dict)  # name -> "db" | "env"

    def is_admin(self, identity: str | None) -> bool:
        return bool(identity) and identity.strip().lower() in self.admins


_ACTIVE: SettingsRuntime | None = None


def active_runtime() -> SettingsRuntime:
    return _ACTIVE if _ACTIVE is not None else SettingsRuntime(error="the settings store is not configured")


def open_from_environment(environ: MutableMapping[str, str] | None = None) -> SettingsStore | None:
    environ = os.environ if environ is None else environ
    key = environ.get(KEY_ENV)
    if not key:
        return None
    path = environ.get(DB_ENV) or str(DEFAULT_DB_PATH)
    return SettingsStore(path, key)


def apply_startup_settings(environ: MutableMapping[str, str] | None = None) -> SettingsRuntime:
    """Load the store into the process environment at start (DB over env).

    Called by ``lh-harness web`` before anything reads its configuration.
    The settings key and the admin proxy secret leave the environment (they
    stay in memory only), so no child process inherits them. Never raises:
    a missing or unreadable store leaves the environment as it was and the
    admin screen reports why.
    """
    global _ACTIVE
    environ = os.environ if environ is None else environ
    admins = tuple(
        a.strip().lower() for a in (environ.get(ADMINS_ENV) or "").split(",") if a.strip()
    )
    proxy_auth = environ.pop(PROXY_AUTH_ENV, None) or None
    runtime = SettingsRuntime(admins=admins, proxy_auth=proxy_auth)
    try:
        store = open_from_environment(environ)
    except Exception as exc:  # noqa: BLE001 - a broken store must not take the service down
        runtime.error = f"settings store unusable: {exc}"
        logger.error("%s; using the environment only", runtime.error)
        store = None
    environ.pop(KEY_ENV, None)
    if store is None:
        runtime.error = runtime.error or f"the settings store is not configured ({KEY_ENV} unset)"
        for name in environ:
            if NAME_RE.fullmatch(name):
                runtime.sources[name] = "env"
        _ACTIVE = runtime
        return runtime
    runtime.store = store
    for name in list(environ):
        if NAME_RE.fullmatch(name):
            runtime.sources[name] = "env"
    try:
        values = store.values()
    except Exception as exc:  # noqa: BLE001
        runtime.error = f"settings store unreadable: {exc}"
        logger.error("%s; using the environment only", runtime.error)
        values = {}
    for name, value in values.items():
        if name in BOOTSTRAP_NAMES:
            continue
        environ[name] = value  # DB over env
        runtime.sources[name] = "db"
    logger.info("settings store %s: %d value(s) applied over the environment", store.path, len(values))
    _ACTIVE = runtime
    return runtime


__all__ = [
    "ADMINS_ENV",
    "BOOTSTRAP_NAMES",
    "CATALOG",
    "DB_ENV",
    "KEY_ENV",
    "PROXY_AUTH_ENV",
    "SettingSpec",
    "SettingsError",
    "SettingsRuntime",
    "SettingsStore",
    "active_runtime",
    "apply_startup_settings",
    "is_secret_name",
    "open_from_environment",
    "spec_for",
]
