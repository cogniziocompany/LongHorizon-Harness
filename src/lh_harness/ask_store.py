"""Writable open-asks store on CT110 (task A3d).

Spec: ``docs/handoffs/open-asks-web-responses-2026-09-30.md`` (PR #99).
``queue/OPEN-ASKS.md`` is retired; asks the overseer raises, and the web
responses Paxton gives to any open-asks row, live here instead.

Layout under the harness state root (the runs root, next to ``queue/``)::

    asks/<id>.json                 seven columns + fields + response + history
    asks/.locks/<id>.lock          per-ask flock
    asks-secrets/<id>/<field>      one sealed secret per file (dir 0700, file 0600)

Security rules this module enforces (and the tests pin):

- A secret VALUE is written only to ``asks-secrets/``. It never appears in
  ``asks/*.json``, a tool result, an error message, a log line or ``history``;
  everything else sees metadata only (``set_at``, ``set_by``, ``length``).
- Every write is atomic (temp file + ``os.replace``) under the ask's lock.
- ``apply_ask_secret`` (:func:`apply_secret`) writes only to a target on the
  static allow-list in ``config.ASK_APPLY_TARGETS``, after the value matched
  that target's format, and returns ``{applied, target, at}`` only.

State rules (Paxton D3): a submit closes the row immediately with
attribution; a submit to a closed row reopens it first (history keeps both
steps) and closes it again; a failed apply reopens the row with the reason in
``evidence``. That is the only automatic reopen.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

try:  # pragma: no cover - Linux is the deployment target; tests run there too
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from .config import ASK_APPLY_TARGETS

ASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
KIND_RE = re.compile(r"^[A-Z][A-Z0-9_-]{0,31}$")
# Ids the live derivation owns (gate-<run>-<approval>, blocked-<queue_id>).
LIVE_ID_PREFIXES = ("gate-", "blocked-")
CLOSED_PREFIXES = ("closed", "answered", "done", "resolved", "superseded")

MAX_FIELDS = 8
MAX_LABEL_CHARS = 120
MAX_BODY_CHARS = 20_000
MAX_SECRET_CHARS = 4096
MAX_ACTOR_CHARS = 254
MAX_HISTORY = 200
_COLUMN_LIMITS = {
    "ask": 4000,
    "evidence": 4000,
    "recommended": 2000,
    "default_if_silent": 1000,
}
APPLY_TIMEOUT_SECONDS = 60
SSH_KEY_ENV = "LH_HARNESS_ASK_APPLY_SSH_KEY"
SSH_KNOWN_HOSTS_ENV = "LH_HARNESS_ASK_APPLY_KNOWN_HOSTS"
_SSH_DEST_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9.-]*$|^[A-Za-z0-9][A-Za-z0-9.-]*$")
# The remote end is a forced command (scripts/ask-apply/lh-apply-env-var); the
# requested command is only a label for it and never carries the value.
REMOTE_COMMAND = "lh-apply-env-var"


class AskStoreError(ValueError):
    """A refused store operation; ``code`` is the HTTP-ish status."""

    def __init__(self, message: str, code: int = 400) -> None:
        super().__init__(message)
        self.code = code


class AskApplyError(RuntimeError):
    """The deterministic writer could not apply a secret (reason has no value)."""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_closed_state(state: Any) -> bool:
    return str(state or "").strip().lower().lstrip("*").startswith(CLOSED_PREFIXES)


def default_fields(kind: Any) -> list[dict[str, Any]]:
    """D2 defaults: one body field, plus one secret field for CREDENTIAL asks."""
    fields: list[dict[str, Any]] = [
        {"name": "response", "type": "body", "label": "Response", "required": False}
    ]
    if str(kind or "").strip().upper() == "CREDENTIAL":
        fields.append({"name": "secret", "type": "secret", "label": "Secret", "required": False})
    return fields


def _no_controls(text: str, *, allow_newlines: bool) -> bool:
    for ch in text:
        code = ord(ch)
        if ch in "\n\r\t" and allow_newlines:
            continue
        if code < 32 or code == 127:
            return False
    return True


def validate_ask_id(value: Any) -> str:
    if not isinstance(value, str) or not ASK_ID_RE.match(value.strip()):
        raise AskStoreError("id must match " + ASK_ID_RE.pattern, 400)
    return value.strip()


def validate_actor(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AskStoreError("actor is required", 400)
    text = value.strip()
    if len(text) > MAX_ACTOR_CHARS or not _no_controls(text, allow_newlines=False):
        raise AskStoreError(f"actor must be at most {MAX_ACTOR_CHARS} printable characters", 400)
    return text


def validate_fields(value: Any) -> list[dict[str, Any]]:
    """Validate a ``declare_ask_fields`` list (names unique, 1..MAX_FIELDS)."""
    if not isinstance(value, list) or not value:
        raise AskStoreError("fields must be a non-empty list", 400)
    if len(value) > MAX_FIELDS:
        raise AskStoreError(f"at most {MAX_FIELDS} fields per ask", 400)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise AskStoreError("each field must be an object", 400)
        unknown = set(item) - {"name", "type", "label", "required"}
        if unknown:
            raise AskStoreError("unknown field key(s): " + ", ".join(sorted(unknown)), 400)
        name = item.get("name")
        if not isinstance(name, str) or not FIELD_NAME_RE.match(name):
            raise AskStoreError("field name must match " + FIELD_NAME_RE.pattern, 400)
        if name in seen:
            raise AskStoreError(f"duplicate field name {name!r}", 400)
        seen.add(name)
        ftype = item.get("type")
        if ftype not in ("body", "secret"):
            raise AskStoreError(f"field {name!r}: type must be 'body' or 'secret'", 400)
        label = item.get("label", name)
        if not isinstance(label, str) or len(label) > MAX_LABEL_CHARS or not _no_controls(label, allow_newlines=False):
            raise AskStoreError(f"field {name!r}: label must be at most {MAX_LABEL_CHARS} printable characters", 400)
        required = item.get("required", False)
        if not isinstance(required, bool):
            raise AskStoreError(f"field {name!r}: required must be true or false", 400)
        out.append({"name": name, "type": ftype, "label": label.strip() or name, "required": required})
    return out


def validate_columns(arguments: dict[str, Any], *, require_ask: bool) -> dict[str, str]:
    cols: dict[str, str] = {}
    kind = arguments.get("kind")
    if not isinstance(kind, str) or not KIND_RE.match(kind.strip()):
        raise AskStoreError("kind must match " + KIND_RE.pattern + " (e.g. CREDENTIAL, DECISION)", 400)
    cols["kind"] = kind.strip()
    for name, limit in _COLUMN_LIMITS.items():
        value = arguments.get(name, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise AskStoreError(f"{name} must be a string", 400)
        value = value.strip()
        if len(value) > limit or not _no_controls(value, allow_newlines=True):
            raise AskStoreError(f"{name} must be at most {limit} characters without control bytes", 400)
        cols[name] = value
    if require_ask and not cols["ask"]:
        raise AskStoreError("ask is required", 400)
    return cols


def _validate_body(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise AskStoreError(f"field {name!r} must be a string", 400)
    if len(value) > MAX_BODY_CHARS or "\x00" in value:
        raise AskStoreError(f"field {name!r} must be at most {MAX_BODY_CHARS} characters without NUL", 400)
    return value


def _validate_secret(name: str, value: Any) -> str:
    # The error text never echoes the value.
    if not isinstance(value, str):
        raise AskStoreError(f"secret field {name!r} must be a string", 400)
    if len(value) > MAX_SECRET_CHARS or not _no_controls(value, allow_newlines=False):
        raise AskStoreError(
            f"secret field {name!r} must be at most {MAX_SECRET_CHARS} characters without control bytes or newlines",
            400,
        )
    return value


class AskStore:
    """JSON-file ask store with sealed secrets (see module docstring)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.asks_dir = self.root / "asks"
        self.secrets_dir = self.root / "asks-secrets"
        self.locks_dir = self.asks_dir / ".locks"

    # -------------------------------------------------------------- plumbing --
    def _ensure_dirs(self) -> None:
        for path in (self.asks_dir, self.locks_dir, self.secrets_dir):
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)

    @contextlib.contextmanager
    def _locked(self, ask_id: str) -> Iterator[None]:
        self._ensure_dirs()
        lock_path = self.locks_dir / f"{ask_id}.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _record_path(self, ask_id: str) -> Path:
        return self.asks_dir / f"{ask_id}.json"

    def _secret_path(self, ask_id: str, field: str) -> Path:
        return self.secrets_dir / ask_id / field

    @staticmethod
    def _atomic_write(path: Path, data: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", text=True)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def _load(self, ask_id: str) -> dict[str, Any] | None:
        path = self._record_path(ask_id)
        if not path.is_file():
            return None
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _save(self, record: dict[str, Any]) -> None:
        record["updated_at"] = now_iso()
        record["history"] = record.get("history", [])[-MAX_HISTORY:]
        self._atomic_write(self._record_path(record["id"]), json.dumps(record, indent=2, sort_keys=True))

    def _seal(self, ask_id: str, field: str, value: str) -> None:
        ask_dir = self.secrets_dir / ask_id
        ask_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(ask_dir, 0o700)
        path = self._secret_path(ask_id, field)
        self._atomic_write(path, value)
        os.chmod(path, 0o600)

    def _unseal_delete(self, ask_id: str, field: str) -> bool:
        path = self._secret_path(ask_id, field)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    @staticmethod
    def _history(record: dict[str, Any], actor: str, action: str, field_names: list[str], **extra: Any) -> None:
        entry: dict[str, Any] = {"at": now_iso(), "actor": actor, "action": action, "field_names": field_names}
        entry.update(extra)
        record.setdefault("history", []).append(entry)

    @staticmethod
    def effective_fields(record: dict[str, Any]) -> list[dict[str, Any]]:
        return record.get("fields") or default_fields(record.get("kind"))

    @staticmethod
    def _new_record(seed: dict[str, Any], origin: str) -> dict[str, Any]:
        record = {
            "id": seed["id"],
            "ask": str(seed.get("ask") or ""),
            "kind": str(seed.get("kind") or ""),
            "evidence": str(seed.get("evidence") or ""),
            "recommended": str(seed.get("recommended") or ""),
            "default_if_silent": str(seed.get("default_if_silent") or ""),
            "state": str(seed.get("state") or "OPEN"),
            "origin": origin,
            "fields": None,
            "response": {},
            "history": [],
            "created_at": now_iso(),
        }
        return record

    # ------------------------------------------------------------- read side --
    def get(self, ask_id: str) -> dict[str, Any] | None:
        return self._load(validate_ask_id(ask_id))

    def records(self) -> list[dict[str, Any]]:
        if not self.asks_dir.is_dir():
            return []
        out = []
        for path in sorted(self.asks_dir.glob("*.json")):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except (OSError, ValueError):
                continue
        return out

    def public_row(self, record: dict[str, Any]) -> dict[str, Any]:
        """The row any tool returns: seven columns, fields, response metadata."""
        fields = self.effective_fields(record)
        response: dict[str, Any] = {}
        stored = record.get("response") or {}
        for field in fields:
            value = stored.get(field["name"])
            if value is None:
                continue
            if field["type"] == "secret":
                # Metadata only, copied key by key so nothing else can leak.
                response[field["name"]] = {
                    k: value[k]
                    for k in ("set_at", "set_by", "length", "applied_at", "applied_target", "sealed")
                    if isinstance(value, dict) and k in value
                }
            else:
                response[field["name"]] = value
        return {
            "id": record["id"],
            "ask": record.get("ask", ""),
            "kind": record.get("kind", ""),
            "evidence": record.get("evidence", ""),
            "recommended": record.get("recommended", ""),
            "default_if_silent": record.get("default_if_silent", ""),
            "state": record.get("state", "OPEN"),
            "origin": record.get("origin", "raised"),
            "fields": fields,
            "response": response,
            "updated_at": record.get("updated_at"),
        }

    def public_rows(self) -> list[dict[str, Any]]:
        return [self.public_row(r) for r in self.records()]

    # ------------------------------------------------------------ write side --
    def raise_ask(self, ask_id: str, columns: dict[str, str], fields: list[dict[str, Any]] | None, actor: str) -> dict[str, Any]:
        ask_id = validate_ask_id(ask_id)
        if ask_id.startswith(LIVE_ID_PREFIXES):
            raise AskStoreError("ids starting gate- or blocked- belong to live rows", 400)
        with self._locked(ask_id):
            if self._load(ask_id) is not None:
                raise AskStoreError(f"ask {ask_id!r} already exists", 409)
            record = self._new_record({"id": ask_id, **columns, "state": "OPEN"}, "raised")
            record["fields"] = fields
            self._history(record, actor, "raise", [f["name"] for f in (fields or [])])
            self._save(record)
            return self.public_row(record)

    def _get_or_seed(self, ask_id: str, seed: tuple[dict[str, Any], str] | None) -> dict[str, Any]:
        record = self._load(ask_id)
        if record is not None:
            return record
        if seed is None:
            raise AskStoreError(f"ask {ask_id!r} not found", 404)
        row, origin = seed
        # Copied into the store on first write; from then on the store wins.
        return self._new_record({**row, "id": ask_id}, origin)

    def declare_fields(self, ask_id: str, fields: list[dict[str, Any]], actor: str, seed: tuple[dict[str, Any], str] | None = None) -> dict[str, Any]:
        ask_id = validate_ask_id(ask_id)
        with self._locked(ask_id):
            record = self._get_or_seed(ask_id, seed)
            new_names = {f["name"]: f["type"] for f in fields}
            response = record.setdefault("response", {})
            for old in self.effective_fields(record):
                # A secret field that disappears (or turns into a body) takes
                # its sealed value with it: no orphaned secret files.
                if old["type"] == "secret" and new_names.get(old["name"]) != "secret":
                    self._unseal_delete(ask_id, old["name"])
                    response.pop(old["name"], None)
                elif old["name"] not in new_names:
                    response.pop(old["name"], None)
            record["fields"] = fields
            self._history(record, actor, "declare_fields", sorted(new_names))
            self._save(record)
            return self.public_row(record)

    def respond(self, ask_id: str, actor: str, values: Any, seed: tuple[dict[str, Any], str] | None = None) -> dict[str, Any]:
        ask_id = validate_ask_id(ask_id)
        actor = validate_actor(actor)
        if not isinstance(values, dict):
            raise AskStoreError("fields must be an object of {name: value}", 400)
        with self._locked(ask_id):
            record = self._get_or_seed(ask_id, seed)
            fields = {f["name"]: f for f in self.effective_fields(record)}
            unknown = sorted(set(values) - set(fields))
            if unknown:
                raise AskStoreError("unknown field(s): " + ", ".join(unknown), 400)
            # Validate every value before anything is written.
            bodies: dict[str, str] = {}
            secrets_in: dict[str, str] = {}
            for name, value in values.items():
                if fields[name]["type"] == "body":
                    bodies[name] = _validate_body(name, "" if value is None else value)
                elif value not in (None, ""):
                    secrets_in[name] = _validate_secret(name, value)
            response = record.setdefault("response", {})
            projected = {**{k: v for k, v in response.items()}, **bodies}
            for name in secrets_in:
                projected[name] = {"set": True}
            has_value = any(
                (bool(projected.get(n)) if f["type"] == "secret" else bool(str(projected.get(n) or "").strip()))
                for n, f in fields.items()
            )
            if not has_value:
                raise AskStoreError("nothing to submit: every field is empty", 422)
            missing = [
                n for n, f in fields.items()
                if f.get("required") and not (projected.get(n) if f["type"] == "secret" else str(projected.get(n) or "").strip())
            ]
            if missing:
                raise AskStoreError("required field(s) empty: " + ", ".join(missing), 422)
            changed = sorted(set(bodies) | set(secrets_in))
            if is_closed_state(record.get("state")):
                # D3: an edit to a closed row reopens it, then the submit
                # closes it again with the new attribution.
                record["state"] = "OPEN"
                self._history(record, actor, "reopen", changed)
            for name, text in bodies.items():
                response[name] = text
            for name, value in secrets_in.items():
                self._seal(ask_id, name, value)
                response[name] = {"set_at": now_iso(), "set_by": actor, "length": len(value), "sealed": True}
            record["state"] = f"CLOSED {now_iso()} by {actor} (web)"
            self._history(record, actor, "respond", changed)
            self._save(record)
            return self.public_row(record)

    def clear_secret(self, ask_id: str, field: str, actor: str) -> dict[str, Any]:
        ask_id = validate_ask_id(ask_id)
        actor = validate_actor(actor)
        with self._locked(ask_id):
            record = self._load(ask_id)
            if record is None:
                raise AskStoreError(f"ask {ask_id!r} not found", 404)
            spec = {f["name"]: f for f in self.effective_fields(record)}.get(field)
            if spec is None or spec["type"] != "secret":
                raise AskStoreError(f"{field!r} is not a secret field of {ask_id!r}", 400)
            removed = self._unseal_delete(ask_id, field)
            had_meta = (record.get("response") or {}).pop(field, None) is not None
            if not removed and not had_meta:
                raise AskStoreError(f"secret {field!r} is not set", 404)
            self._history(record, actor, "clear_secret", [field])
            self._save(record)
            return self.public_row(record)

    # ----------------------------------------------------------------- apply --
    def apply_secret(
        self,
        ask_id: str,
        field: str,
        target: str,
        actor: str,
        writer: Callable[[str, dict[str, str], str], None] | None = None,
    ) -> dict[str, Any]:
        """Apply one sealed secret to an allow-listed target (D1 + D3).

        Raises AskStoreError for refusals that must NOT change the row
        (unknown target, unknown ask or field). Any failure after that point
        reopens the row with the reason in ``evidence`` and raises
        AskStoreError with that reason; the value is never in the message.
        """
        ask_id = validate_ask_id(ask_id)
        spec = ASK_APPLY_TARGETS.get(target) if isinstance(target, str) else None
        if spec is None:
            raise AskStoreError(f"target {target!r} is not on the apply allow-list", 403)
        writer = writer or ssh_env_file_writer
        with self._locked(ask_id):
            record = self._load(ask_id)
            if record is None:
                raise AskStoreError(f"ask {ask_id!r} not found", 404)
            fspec = {f["name"]: f for f in self.effective_fields(record)}.get(field)
            if fspec is None or fspec["type"] != "secret":
                raise AskStoreError(f"{field!r} is not a secret field of {ask_id!r}", 400)
            value: str | None = None
            try:
                path = self._secret_path(ask_id, field)
                if not path.is_file():
                    raise AskApplyError("no sealed secret for this field")
                value = path.read_text(encoding="utf-8")
                if not re.fullmatch(spec["value_pattern"], value):
                    raise AskApplyError("the value does not match the target's format")
                writer(target, spec, value)
            except Exception as exc:  # noqa: BLE001 - every failure reopens
                reason = _redact(str(exc) if isinstance(exc, AskApplyError) else type(exc).__name__ + ": " + str(exc), value)
                at = now_iso()
                note = f"apply failed {at}: {target} {reason}"
                evidence = (record.get("evidence") or "").strip()
                evidence = (evidence + " | " + note) if evidence else note
                record["evidence"] = evidence[-_COLUMN_LIMITS["evidence"]:]
                record["state"] = "OPEN"
                self._history(record, actor, "apply_failed", [field], target=target, reason=reason)
                self._save(record)
                raise AskStoreError(f"apply failed: {target} {reason}", 502) from None
            at = now_iso()
            # Applied: the sealed copy has done its job and is deleted, so the
            # plaintext lives only at the target from here on.
            self._unseal_delete(ask_id, field)
            meta = (record.setdefault("response", {}).get(field) or {})
            meta.update({"applied_at": at, "applied_target": target, "sealed": False})
            record["response"][field] = meta
            self._history(record, actor, "apply", [field], target=target)
            self._save(record)
            return {"applied": True, "target": target, "at": at}


def _redact(text: str, value: str | None) -> str:
    text = (text or "").strip()
    if value:
        text = text.replace(value, "[redacted]")
    text = " ".join(text.split())
    return text[:300]


def ssh_env_file_writer(
    target: str,
    spec: dict[str, str],
    value: str,
    *,
    run: Callable[..., Any] = subprocess.run,
    env: dict[str, str] | None = None,
) -> None:
    """Write ``value`` to an env-file variable on a remote host over ssh.

    The value travels on stdin only, never in argv or the environment of a
    local process. The remote side is a forced command
    (``scripts/ask-apply/lh-apply-env-var``) pinned to one file and one
    variable in ``authorized_keys``, so this key can do nothing else.
    """
    env = dict(os.environ if env is None else env)
    dest = (env.get(spec["host_env"]) or "").strip()
    if not dest:
        raise AskApplyError(f"{spec['host_env']} is not set")
    if not _SSH_DEST_RE.match(dest):
        raise AskApplyError(f"{spec['host_env']} is not a plain user@host")
    key = (env.get(SSH_KEY_ENV) or "").strip()
    if not key:
        raise AskApplyError(f"{SSH_KEY_ENV} is not set")
    argv = [
        "ssh",
        "-i", key,
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=10",
    ]
    known = (env.get(SSH_KNOWN_HOSTS_ENV) or "").strip()
    if known:
        argv += ["-o", f"UserKnownHostsFile={known}"]
    argv += ["-T", dest, "--", REMOTE_COMMAND, spec["file"], spec["variable"]]
    try:
        proc = run(argv, input=value + "\n", capture_output=True, text=True, timeout=APPLY_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise AskApplyError(f"ssh timed out after {APPLY_TIMEOUT_SECONDS}s") from None
    except OSError as exc:
        raise AskApplyError(f"ssh could not start: {exc.strerror or exc}") from None
    if proc.returncode != 0:
        raise AskApplyError(f"ssh exit {proc.returncode}: {_redact(proc.stderr or '', value)}")


__all__ = [
    "AskApplyError",
    "AskStore",
    "AskStoreError",
    "default_fields",
    "is_closed_state",
    "ssh_env_file_writer",
    "validate_actor",
    "validate_ask_id",
    "validate_columns",
    "validate_fields",
]
