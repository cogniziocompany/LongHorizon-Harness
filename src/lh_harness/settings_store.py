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
- Queue trios and capacity live in the store too (schema v2, tables
  ``queue_trio`` and ``queue_capacity``) so they can change at runtime
  without a restart. They are seeded once from the project config.toml
  (``[queue.trios.*]`` / ``[queue.capacity]``); the file stays as the
  bootstrap and fallback only, and seeding never overwrites rows.

See docs/settings-store.md.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, MutableMapping

try:
    tomllib = importlib.import_module("tomllib")
except ModuleNotFoundError:  # Python < 3.11
    tomllib = importlib.import_module("tomli")

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
SCHEMA_VERSION = 3

# Per-role overrides a queue trio may carry (same keys as config.toml's
# ``[queue.trios.<name>]``): ``<role>_agent`` / ``<role>_model`` for the three
# queue roles. Stored as one JSON object in ``queue_trio.role_models``.
TRIO_ROLE_KEYS = tuple(
    f"{role}_{key}" for role in ("manager", "executor", "auditor") for key in ("agent", "model")
)


def trio_role_overrides(spec: Any) -> dict[str, str]:
    """The non-blank per-role keys of a trio spec/row, as ``{key: value}``."""

    if not isinstance(spec, Mapping):
        return {}
    out: dict[str, str] = {}
    for key in TRIO_ROLE_KEYS:
        value = spec.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    return out


def upsert_queue_trio(db: Any, row: Mapping[str, Any], actor: str, *, roles: Mapping[str, str] | None = None) -> None:
    """Write one queue_trio row WITHOUT clobbering its per-role overrides.

    ``roles=None`` keeps whatever ``role_models`` the row already has (the
    single-field edits: set_trio_model, a PUT row that names no role key).
    A mapping replaces them; an empty mapping clears them.  The earlier
    ``INSERT OR REPLACE`` rewrote the whole row, which would have reset a
    mixed trio (zaiq: z.ai manager/auditor, local qwen executor) to one model.
    """

    db.execute(
        """INSERT INTO queue_trio(name, agent, model, mcp_profile, enabled, updated_by, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET
               agent = excluded.agent, model = excluded.model, mcp_profile = excluded.mcp_profile,
               enabled = excluded.enabled, updated_by = excluded.updated_by, updated_at = excluded.updated_at""",
        (row["name"], row["agent"], row["model"], row["mcp_profile"], row["enabled"], actor, now_iso()),
    )
    if roles is not None:
        # Always a JSON object, even when empty: "{}" means "this row has no
        # overrides" (authoritative), while NULL means "written before the
        # column existed" and makes the launcher keep the file's keys.
        db.execute(
            "UPDATE queue_trio SET role_models = ? WHERE name = ?",
            (json.dumps(trio_role_overrides(roles), sort_keys=True), row["name"]),
        )


def _row_role_models(raw: Any) -> dict[str, str]:
    if not raw:
        return {}
    try:
        return trio_role_overrides(json.loads(raw))
    except (TypeError, ValueError):
        return {}
_KEY_CHECK_PLAINTEXT = b"lh-harness settings key check v1"
SEED_ACTOR = "seed-queue"


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
    # LHH-CT110-DISK: every disk-retention threshold is configurable so an
    # operator can dial them in from the admin screen without redeploying.
    # The loader (lh_harness.disk_retention.config) reads them by name; the
    # descriptions are what the admin screen shows next to each field.
    SettingSpec(
        "LH_HARNESS_DISK_RUN_MAX_AGE_DAYS",
        "Prune harness run folders older than this many days (default 14)",
        False,
        restart=False,
    ),
    SettingSpec(
        "LH_HARNESS_DISK_WORKTREE_MAX_AGE_DAYS",
        "Prune parked work trees idle more than this many days (default 7)",
        False,
        restart=False,
    ),
    SettingSpec(
        "LH_HARNESS_DISK_CACHE_MAX_BYTES",
        "Fallback byte cap for npm and pip caches (default 4 GiB); 0 or negative disables the cap",
        False,
        restart=False,
    ),
    SettingSpec(
        "LH_HARNESS_DISK_NPM_CACHE_MAX_BYTES",
        "npm cache byte cap (default: LH_HARNESS_DISK_CACHE_MAX_BYTES)",
        False,
        restart=False,
    ),
    SettingSpec(
        "LH_HARNESS_DISK_PIP_CACHE_MAX_BYTES",
        "pip cache byte cap (default: LH_HARNESS_DISK_CACHE_MAX_BYTES)",
        False,
        restart=False,
    ),
    SettingSpec(
        "LH_HARNESS_DISK_WARN_PCT",
        "Disk usage percent that triggers a warning (default 80)",
        False,
        restart=False,
    ),
    SettingSpec(
        "LH_HARNESS_DISK_PAUSE_PCT",
        "Disk usage percent that pauses new launches (default 90)",
        False,
        restart=False,
    ),
    SettingSpec(
        "LH_HARNESS_DISK_RESUME_PCT",
        "Disk usage percent under which the launcher auto-resumes (default 85)",
        False,
        restart=False,
    ),
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
            if version < 2:
                # Queue hot-reload (LHH-QUEUE-HOTRELOAD): trios and capacity
                # move out of config.toml-at-startup so they can change at
                # runtime. Idempotent: CREATE IF NOT EXISTS, never drops data.
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS queue_trio (
                        name TEXT PRIMARY KEY,
                        agent TEXT NOT NULL,
                        model TEXT,
                        mcp_profile TEXT,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        updated_by TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS queue_capacity (
                        trio TEXT PRIMARY KEY,
                        max_concurrent INTEGER NOT NULL,
                        max_peak INTEGER
                    );
                    """
                )
                # Schedule-aware capacity (2026-10-11): existing stores get the
                # max_peak companion column in place; fresh ones create it above.
                cols = {row[1] for row in db.execute("PRAGMA table_info(queue_capacity)").fetchall()}
                if "max_peak" not in cols:
                    db.execute("ALTER TABLE queue_capacity ADD COLUMN max_peak INTEGER")
            # v3 (2026-10-12): per-role overrides on trios. Checked by column, not
            # only by version, so a store created by any earlier build gains it.
            trio_cols = {row[1] for row in db.execute("PRAGMA table_info(queue_trio)").fetchall()}
            if trio_cols and "role_models" not in trio_cols:
                db.execute("ALTER TABLE queue_trio ADD COLUMN role_models TEXT")
            if version < SCHEMA_VERSION:
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

    # ------------------------------------------------- queue trios + capacity --
    def queue_trios(self) -> list[dict[str, Any]]:
        """Every queue trio row, ordered by name."""
        with self._db() as db:
            rows = db.execute("SELECT * FROM queue_trio ORDER BY name").fetchall()
        return [
            {
                "name": r["name"],
                "agent": r["agent"],
                "model": r["model"],
                "mcp_profile": r["mcp_profile"],
                "enabled": bool(r["enabled"]),
                "updated_by": r["updated_by"],
                "updated_at": r["updated_at"],
                # Per-role overrides, flattened next to the trio-wide fields;
                # ``role_models_set`` tells a reader whether the row carries
                # its own (possibly empty) set or predates the column.
                **_row_role_models(r["role_models"]),
                "role_models_set": r["role_models"] is not None,
            }
            for r in rows
        ]

    def queue_capacity(self) -> dict[str, int]:
        """Per-trio concurrency caps (trio name -> max_concurrent)."""
        with self._db() as db:
            rows = db.execute("SELECT trio, max_concurrent FROM queue_capacity ORDER BY trio").fetchall()
        return {row["trio"]: int(row["max_concurrent"]) for row in rows}

    def queue_capacity_rows(self) -> dict[str, dict[str, Any]]:
        """Per-trio caps with the optional schedule-aware peak companion.

        ``max`` is the off-peak/all-day cap; ``max_peak`` (nullable) is the
        LOWER cap applied while the peak window is active (Mon-Fri
        06:00-10:00 UTC — z.ai peak billing hours, when dynamic concurrency
        is also tightest). None/absent = no peak override, ``max`` applies
        around the clock.
        """
        with self._db() as db:
            rows = db.execute(
                "SELECT trio, max_concurrent, max_peak FROM queue_capacity ORDER BY trio"
            ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            peak = row["max_peak"]
            out[row["trio"]] = {
                "max": int(row["max_concurrent"]),
                "max_peak": int(peak) if peak is not None else None,
            }
        return out

    def seed_queue_defaults(
        self,
        trios: Mapping[str, Any] | None = None,
        capacity: Mapping[str, Any] | None = None,
        *,
        actor: str = SEED_ACTOR,
        source: str = "config.toml",
    ) -> dict[str, Any]:
        """Populate the queue tables from config values, once only.

        Each table is seeded only while it is empty: a restart after an admin
        edited rows (or a config.toml change afterwards) leaves the live rows
        untouched, so the database is authoritative from the first seed on.
        ``capacity`` keys are the config-style ``<trio>_max`` integers; other
        capacity keys (poll_seconds, max_retries, ...) are not per-trio
        concurrency caps and stay out of the store. Every seeded row is
        recorded in the audit log; a skip seeds and audits nothing.

        Returns a report: per table, the seeded names, or ``None`` when the
        table already held rows and was left as is.
        """
        report: dict[str, Any] = {"trios": None, "capacity": None}
        with self._db() as db:
            if db.execute("SELECT 1 FROM queue_trio LIMIT 1").fetchone() is None:
                seeded: list[str] = []
                for name, spec in (trios or {}).items():
                    if not isinstance(spec, dict) or not isinstance(name, str) or not name.strip():
                        continue
                    agent = str(spec.get("agent") or "").strip()
                    if not agent:
                        continue
                    model = str(spec.get("model") or "").strip() or None
                    profile = str(spec.get("mcp_profile") or "").strip() or None
                    roles = trio_role_overrides(spec)
                    db.execute(
                        """INSERT INTO queue_trio(name, agent, model, mcp_profile, enabled, updated_by, updated_at, role_models)
                           VALUES (?, ?, ?, ?, 1, ?, ?, ?)""",
                        (name.strip(), agent, model, profile, actor, now_iso(), json.dumps(roles, sort_keys=True)),
                    )
                    self._audit(
                        db, actor, "seed", f"queue_trio:{name.strip()}",
                        agent=agent, model=model, mcp_profile=profile, source=source, **roles,
                    )
                    seeded.append(name.strip())
                report["trios"] = seeded
            if db.execute("SELECT 1 FROM queue_capacity LIMIT 1").fetchone() is None:
                seeded = []
                for key, raw in (capacity or {}).items():
                    if not isinstance(key, str) or not key.endswith("_max"):
                        continue
                    trio = key[: -len("_max")].strip()
                    if not trio or isinstance(raw, bool) or not isinstance(raw, int):
                        continue
                    db.execute(
                        "INSERT INTO queue_capacity(trio, max_concurrent) VALUES (?, ?)",
                        (trio, max(0, raw)),
                    )
                    self._audit(db, actor, "seed", f"queue_capacity:{trio}", max_concurrent=max(0, raw), source=source)
                    seeded.append(trio)
                report["capacity"] = seeded
        return report

    def seed_queue_from_config(self, path: str | Path, *, actor: str = SEED_ACTOR) -> dict[str, Any] | None:
        """Seed the queue tables from a project config.toml, once only.

        Reads ``[queue.trios.*]`` and ``[queue.capacity]`` exactly as the
        launcher reads them at start; the file is the bootstrap and stays
        untouched (read-only). A missing or unreadable file seeds nothing and
        returns ``None`` (the launcher then falls back to the file as before);
        it never takes the service down.
        """
        source = Path(path)
        try:
            with source.open("rb") as fh:
                payload = tomllib.load(fh)
        except Exception:  # noqa: BLE001 - a missing/broken bootstrap file must be a no-op
            logger.info("queue seed skipped: cannot read %s", source)
            return None
        if not isinstance(payload, dict):
            logger.info("queue seed skipped: %s has no TOML table", source)
            return None
        queue = payload.get("queue")
        queue = queue if isinstance(queue, dict) else {}
        trios = queue.get("trios")
        capacity = queue.get("capacity")
        return self.seed_queue_defaults(
            trios if isinstance(trios, dict) else {},
            capacity if isinstance(capacity, dict) else {},
            actor=actor,
            source=str(source),
        )

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
    # LHH-QUEUE-PROTECT-DOCS (F3): queue trios that refuse model/agent edits
    # unless an SSO admin supplies ``force: true``.  Resolved from the project
    # config's ``[queue].protected_trios``; defaults to
    # ``config.DEFAULT_PROTECTED_TRIOS`` when never set.  An empty tuple
    # disables the rule.
    protected_trios: tuple[str, ...] = ()

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
    # LHH-QUEUE-PROTECT-DOCS (F3): read [queue].protected_trios from the
    # same project config the queue runtime uses.  Defer the import so the
    # settings store stays importable without pulling the full config
    # loader (the auth helpers in ``caller_auth`` import this module).
    # The same opt-in convention as ``_resolve_protected_trios`` in the
    # REST route applies: a missing or unreadable project config leaves
    # the rule OFF; an explicit empty list also disables it; the default
    # ``("kimi", "glm", "qwen")`` only applies when the loader sees a
    # ``[queue].protected_trios`` key that omits a list.  Web deployments
    # that want the rule should make the key explicit so the default is
    # never picked up implicitly.
    try:
        from .config import load_run_defaults as _load_run_defaults_for_protected
        try:
            _project = _load_run_defaults_for_protected()
        except Exception:  # noqa: BLE001 - a broken file must not silently disable the rule
            _project = None
        _configured: tuple[str, ...] | None = None
        if isinstance(_project, dict):
            _queue = _project.get("queue")
            if isinstance(_queue, dict) and "protected_trios" in _queue:
                _raw = _queue.get("protected_trios")
                if isinstance(_raw, list):
                    _configured = tuple(
                        dict.fromkeys(
                            item.strip() for item in _raw if isinstance(item, str) and item.strip()
                        )
                    )
                else:
                    _configured = ()
        runtime.protected_trios = _configured if _configured is not None else ()
    except Exception:  # noqa: BLE001 - imports/loader failures must leave the rule OFF
        runtime.protected_trios = ()
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
    "TRIO_ROLE_KEYS",
    "trio_role_overrides",
    "upsert_queue_trio",
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
