"""Writable open-asks store on CT110 (task A3d).

Spec: ``docs/handoffs/open-asks-web-responses-2026-09-30.md`` (PR #99).
``queue/OPEN-ASKS.md`` is retired; asks the overseer raises, and the web
responses Paxton gives to any open-asks row, live here instead.

Two parts, kept apart on purpose:

- The ask records (``<store>/asks/<id>.json``: seven columns, fields,
  response metadata, history). No secret value is ever in them.
- The sealed secrets, behind a *vault*. In production the vault is
  ``scripts/ask-apply/lh-ask-vault`` running as a separate uid through one
  fixed sudo command (:class:`HelperVault`): the harness process can seal,
  clear and apply a secret but can never read one back, and the ssh key and
  the grants live where harness workers (same uid as the service) cannot
  read them. :class:`LocalVault` keeps secrets in a local directory; it exists
  for tests, and :func:`load_ask_runtime` refuses to enable the secret tools
  with it whenever the directory is readable by the uid workers run as.

Security rules (pinned by tests): a secret value never appears in a record, a
tool result, an error, a log line or ``history``; record writes are atomic
under a per-ask lock; ``apply_ask_secret`` writes only to a target on the
static allow-list (``config.ASK_APPLY_TARGETS``) that the field itself
declared, and returns ``{applied, target, at}`` only.

State rules (Paxton D3): a submit closes a stored ask with attribution; a
submit to a closed ask reopens it first (history keeps both steps) and closes
it again; a failed apply reopens it with the reason in ``evidence``. A
response to a live GATE/BLOCKED row is context only and never hides it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import pwd
import re
import shlex
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

try:  # pragma: no cover - Linux is the deployment target; tests run there too
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from .config import ASK_APPLY_TARGETS

logger = logging.getLogger(__name__)

ASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
FIELD_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
KIND_RE = re.compile(r"[A-Z][A-Z0-9_-]{0,31}")
# Ids the live derivation owns (gate-<run>-<approval>, blocked-<queue_id>).
LIVE_ID_PREFIXES = ("gate-", "blocked-")
CLOSED_PREFIXES = ("closed", "answered", "done", "resolved", "superseded")
LIVE_RESPONDED_NOTE = "responded (context only), gate pending"

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
HELPER_TIMEOUT_SECONDS = 90
SECRETS_DISABLED_PREFIX = "disabled: "

# Token shapes that must go in a secret field, never a body field.
_TOKEN_SHAPES = re.compile(
    r"(github_pat_[A-Za-z0-9_]{20,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bsk-[A-Za-z0-9_-]{20,}"
    r"|\bAKIA[0-9A-Z]{16}\b|\bxox[abpr]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)

# Environment names that must never reach a harness worker (review H1).
WORKER_ENV_DENY_PREFIXES = ("LH_HARNESS_CALLER_", "LH_HARNESS_ASK_")
WORKER_ENV_DENY_NAMES = ("LH_HARNESS_WEB_TOKEN",)


def scrub_worker_env(env: dict[str, str]) -> dict[str, str]:
    """Copy of ``env`` without control-plane credentials or ask-store settings."""
    return {
        k: v
        for k, v in env.items()
        if k not in WORKER_ENV_DENY_NAMES and not k.startswith(WORKER_ENV_DENY_PREFIXES)
    }


class AskStoreError(ValueError):
    """A refused store operation; ``code`` is the HTTP-ish status."""

    def __init__(self, message: str, code: int = 400) -> None:
        super().__init__(message)
        self.code = code


class AskApplyError(RuntimeError):
    """The vault could not seal/clear/apply (the reason never has a value)."""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_closed_state(state: Any) -> bool:
    return str(state or "").strip().lower().lstrip("*").startswith(CLOSED_PREFIXES)


def is_live_id(ask_id: Any) -> bool:
    return isinstance(ask_id, str) and ask_id.startswith(LIVE_ID_PREFIXES)


def default_fields(kind: Any) -> list[dict[str, Any]]:
    """D2 defaults: one body field, plus one secret field for CREDENTIAL asks.

    The default secret field declares no apply target, so it can be stored
    but not applied until the overseer declares the field with a target.
    """
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


def looks_like_token(text: str) -> bool:
    return bool(_TOKEN_SHAPES.search(text or ""))


def validate_ask_id(value: Any) -> str:
    if not isinstance(value, str) or not ASK_ID_RE.fullmatch(value.strip()):
        raise AskStoreError("id must match " + ASK_ID_RE.pattern, 400)
    return value.strip()


def validate_field_name(value: Any) -> str:
    if not isinstance(value, str) or not FIELD_NAME_RE.fullmatch(value):
        raise AskStoreError("field must match " + FIELD_NAME_RE.pattern, 400)
    return value


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
        unknown = set(item) - {"name", "type", "label", "required", "apply_target"}
        if unknown:
            raise AskStoreError("unknown field key(s): " + ", ".join(sorted(unknown)), 400)
        name = item.get("name")
        if not isinstance(name, str) or not FIELD_NAME_RE.fullmatch(name):
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
        spec: dict[str, Any] = {"name": name, "type": ftype, "label": label.strip() or name, "required": required}
        target = item.get("apply_target")
        if target not in (None, ""):
            if ftype != "secret":
                raise AskStoreError(f"field {name!r}: only a secret field can declare apply_target", 400)
            if not isinstance(target, str) or target not in ASK_APPLY_TARGETS:
                raise AskStoreError(f"field {name!r}: apply_target is not on the apply allow-list", 400)
            spec["apply_target"] = target
        out.append(spec)
    return out


def validate_columns(arguments: dict[str, Any], *, require_ask: bool) -> dict[str, str]:
    cols: dict[str, str] = {}
    kind = arguments.get("kind")
    if not isinstance(kind, str) or not KIND_RE.fullmatch(kind.strip()):
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
    if looks_like_token(value):
        raise AskStoreError(
            f"field {name!r} looks like it contains a secret token; put it in a secret field instead", 422
        )
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


def _redact(text: str, value: str | None) -> str:
    text = (text or "").strip()
    if value:
        text = text.replace(value, "[redacted]")
    text = " ".join(text.split())
    return text[:300]


# ---------------------------------------------------------------------- vaults --
class LocalVault:
    """Secrets in a local directory (dirs 0700, files 0600).

    For tests and development. Production uses :class:`HelperVault`;
    :func:`load_ask_runtime` keeps the secret tools disabled with a local
    vault whose directory the worker uid can read.
    """

    def __init__(self, root: str | Path, writer: Callable[[str, dict[str, str], str], None] | None = None) -> None:
        self.root = Path(root)
        self.writer = writer

    def _path(self, ask_id: str, field: str) -> Path:
        return self.root / validate_ask_id(ask_id) / validate_field_name(field)

    def seal(self, ask_id: str, field: str, value: str) -> None:
        path = self._path(ask_id, field)
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        path.parent.mkdir(exist_ok=True)
        os.chmod(path.parent, 0o700)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(value)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def has(self, ask_id: str, field: str) -> bool:
        return self._path(ask_id, field).is_file()

    def clear(self, ask_id: str, field: str) -> bool:
        try:
            self._path(ask_id, field).unlink()
        except FileNotFoundError:
            return False
        return True

    def apply(self, ask_id: str, field: str, target: str) -> None:
        spec = ASK_APPLY_TARGETS[target]
        path = self._path(ask_id, field)
        if not path.is_file():
            raise AskApplyError("no sealed secret for this field")
        value = path.read_text(encoding="utf-8")
        if not re.fullmatch(spec["value_pattern"], value):
            raise AskApplyError("the value does not match the target's format")
        if self.writer is None:
            raise AskApplyError("this vault cannot apply (use the separate-uid helper)")
        try:
            self.writer(target, spec, value)
        except AskApplyError as exc:
            raise AskApplyError(_redact(str(exc), value)) from None
        except Exception as exc:  # noqa: BLE001
            raise AskApplyError(_redact(f"{type(exc).__name__}: {exc}", value)) from None
        path.unlink()


class HelperVault:
    """The separate-uid helper (``scripts/ask-apply/lh-ask-vault``) via one fixed command.

    ``command`` is e.g. ``["/usr/bin/sudo", "-n", "-u", "lhasks",
    "/usr/local/sbin/lh-ask-vault"]``. Values travel on stdin only; the helper
    never prints a value, so this process never holds one after sealing.
    """

    def __init__(self, command: list[str], run: Callable[..., Any] = subprocess.run) -> None:
        self.command = list(command)
        self._run = run

    def call(self, *args: str, stdin: str | None = None) -> tuple[int, str, str]:
        try:
            proc = self._run(
                [*self.command, *args],
                input=stdin if stdin is not None else "",
                capture_output=True,
                text=True,
                timeout=HELPER_TIMEOUT_SECONDS,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
            )
        except subprocess.TimeoutExpired:
            raise AskApplyError(f"vault helper timed out after {HELPER_TIMEOUT_SECONDS}s") from None
        except OSError as exc:
            raise AskApplyError(f"vault helper could not start: {exc.strerror or exc}") from None
        return proc.returncode, (proc.stdout or ""), (proc.stderr or "")

    def _ok(self, rc: int, err: str, value: str | None = None) -> None:
        if rc != 0:
            raise AskApplyError(f"vault helper exit {rc}: {_redact(err, value)}")

    def seal(self, ask_id: str, field: str, value: str) -> None:
        rc, _out, err = self.call("seal", validate_ask_id(ask_id), validate_field_name(field), stdin=value + "\n")
        self._ok(rc, err, value)

    def has(self, ask_id: str, field: str) -> bool:
        rc, _out, err = self.call("has", validate_ask_id(ask_id), validate_field_name(field))
        if rc == 3:
            return False
        self._ok(rc, err)
        return True

    def clear(self, ask_id: str, field: str) -> bool:
        rc, _out, err = self.call("clear", validate_ask_id(ask_id), validate_field_name(field))
        if rc == 3:
            return False
        self._ok(rc, err)
        return True

    def apply(self, ask_id: str, field: str, target: str) -> None:
        rc, _out, err = self.call("apply", validate_ask_id(ask_id), validate_field_name(field), target)
        self._ok(rc, err)

    def grants(self) -> dict[str, list[str]]:
        rc, out, err = self.call("grants")
        self._ok(rc, err)
        data = json.loads(out)
        if not isinstance(data, dict):
            raise AskApplyError("vault helper returned malformed grants")
        return {str(k): [str(s) for s in v] for k, v in data.items() if isinstance(v, list)}

    def check(self, worker_uid: int) -> str | None:
        """None when the helper's paths are sealed from ``worker_uid``, else a reason."""
        rc, out, err = self.call("check", str(int(worker_uid)))
        if rc == 0:
            return None
        return _redact(err or out, None) or f"vault helper check exit {rc}"


# ------------------------------------------------------------- the self-check --
def _uid_groups(uid: int) -> set[int]:
    try:
        entry = pwd.getpwuid(uid)
    except KeyError:
        return set()
    try:
        return set(os.getgrouplist(entry.pw_name, entry.pw_gid))
    except OSError:
        return {entry.pw_gid}


def readable_by(path: str | Path, uid: int) -> bool:
    """True when ``uid`` could read ``path`` (and search every parent).

    A path this process cannot stat counts as readable (cannot prove
    otherwise), so the check fails closed.
    """
    if uid == 0:
        return True
    groups = _uid_groups(uid)
    target = Path(path).absolute()

    def allows(st: os.stat_result, bits: tuple[int, int, int]) -> bool:
        if st.st_uid == uid:
            return bool(st.st_mode & bits[0])
        if st.st_gid in groups:
            return bool(st.st_mode & bits[1])
        return bool(st.st_mode & bits[2])

    try:
        for parent in list(target.parents)[::-1]:
            if not allows(os.stat(parent), (stat.S_IXUSR, stat.S_IXGRP, stat.S_IXOTH)):
                return False
        st = os.stat(target)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return allows(st, (stat.S_IRUSR, stat.S_IRGRP, stat.S_IROTH))


def _make_non_dumpable() -> bool:
    """PR_SET_DUMPABLE=0: same-uid workers can no longer read this process's
    /proc/<pid>/environ (caller secrets) or ptrace it."""
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(4, 0, 0, 0, 0) == 0  # PR_SET_DUMPABLE = 4
    except Exception:  # pragma: no cover - non-Linux
        return False


@dataclass
class AskRuntime:
    """Everything the ask tools need, resolved once at app start."""

    store_root: str | None
    grants: dict[str, list[str]] = dc_field(default_factory=dict)
    vault: Any = None
    disabled_reason: str | None = None  # all ask tools off (bad config)
    secrets_disabled_reason: str | None = None  # secret tools off (self-check)
    nonces: Any = None

    def __post_init__(self) -> None:
        if self.nonces is None:
            from .caller_auth import NonceCache

            self.nonces = NonceCache()

    def store(self) -> "AskStore":
        if not self.store_root:
            raise AskStoreError("the ask store requires a configured store directory", 501)
        return AskStore(self.store_root, vault=self.vault, secrets_disabled_reason=self.secrets_disabled_reason)


def load_ask_runtime(runs_root: str | Path | None, env: dict[str, str] | None = None) -> AskRuntime:
    """Resolve the ask runtime from the environment (names only, see docs/ask-store.md).

    - ``LH_HARNESS_ASK_STORE_DIR``: ask records (no secrets); default the runs root.
    - ``LH_HARNESS_ASK_VAULT_CMD``: the fixed helper command (production).
      Grants then come from the helper; its ``check`` must pass.
    - Otherwise a local vault (``LH_HARNESS_ASK_VAULT_DIR``, default
      ``<runs_root>/asks-secrets``) and grants from ``LH_HARNESS_ASK_GRANTS_FILE``
      or ``[asks]`` in the project config. The secret tools stay disabled while
      any of those paths is readable by the worker uid.
    - ``LH_HARNESS_WORKER_UID``: the uid workers run as (default: ours).

    Never raises: problems disable the ask tools and are logged.
    """
    from .config import PROJECT_CONFIG_PATH, ProjectConfigError, load_ask_grants

    env = dict(os.environ if env is None else env)
    store_root = env.get("LH_HARNESS_ASK_STORE_DIR") or (str(runs_root) if runs_root else None)
    try:
        worker_uid = int(env.get("LH_HARNESS_WORKER_UID") or os.getuid())
    except ValueError:
        return AskRuntime(store_root, disabled_reason="disabled: LH_HARNESS_WORKER_UID is not a number")

    helper_cmd = (env.get("LH_HARNESS_ASK_VAULT_CMD") or "").strip()
    if helper_cmd:
        command = shlex.split(helper_cmd)
        if not command or not command[0].startswith("/"):
            return AskRuntime(store_root, disabled_reason="disabled: LH_HARNESS_ASK_VAULT_CMD must start with an absolute path")
        vault = HelperVault(command)
        try:
            grants = vault.grants()
            reason = vault.check(worker_uid)
        except (AskApplyError, ValueError) as exc:
            return AskRuntime(store_root, vault=vault, disabled_reason=f"disabled: vault helper unusable: {exc}")
        if reason is None and not _make_non_dumpable():
            reason = "could not make the service non-dumpable; workers could read its environment"
        return AskRuntime(
            store_root,
            grants=grants,
            vault=vault,
            secrets_disabled_reason=(SECRETS_DISABLED_PREFIX + reason) if reason else None,
        )

    grants_file = env.get("LH_HARNESS_ASK_GRANTS_FILE")
    if grants_file:
        grants_path = Path(grants_file)
    else:
        candidate = Path(runs_root) / ".lh-harness" / "config.toml" if runs_root else None
        grants_path = candidate if candidate is not None and candidate.is_file() else PROJECT_CONFIG_PATH
    try:
        grants = load_ask_grants(grants_path)
    except ProjectConfigError as exc:
        reason = f"disabled: bad [asks] in {grants_path}: {exc}"
        logger.error("ask tools %s", reason)
        return AskRuntime(store_root, disabled_reason=reason)
    vault_dir = env.get("LH_HARNESS_ASK_VAULT_DIR") or (str(Path(runs_root) / "asks-secrets") if runs_root else None)
    vault = LocalVault(vault_dir) if vault_dir else None
    secrets_reason = None
    checked = [p for p in (vault_dir, str(grants_path) if grants_file else None) if p]
    for path in checked:
        if readable_by(path, worker_uid) or (not Path(path).exists() and readable_by(Path(path).parent, worker_uid)):
            secrets_reason = f"disabled: store readable by worker uid {worker_uid} ({path})"
            break
    if vault is None:
        secrets_reason = "disabled: no vault configured"
    if secrets_reason:
        logger.warning("ask secret tools %s", secrets_reason)
    return AskRuntime(store_root, grants=grants, vault=vault, secrets_disabled_reason=secrets_reason)


# ----------------------------------------------------------------------- store --
class AskStore:
    """JSON-file ask records; secrets go through ``vault`` (see module docstring)."""

    def __init__(self, root: str | Path, vault: Any = None, secrets_disabled_reason: str | None = None) -> None:
        self.root = Path(root)
        self.asks_dir = self.root / "asks"
        self.locks_dir = self.asks_dir / ".locks"
        self.vault = vault if vault is not None else LocalVault(self.root / "asks-secrets")
        self.secrets_disabled_reason = secrets_disabled_reason

    # -------------------------------------------------------------- plumbing --
    def _require_secrets(self) -> None:
        if self.secrets_disabled_reason:
            raise AskStoreError(self.secrets_disabled_reason, 503)

    def _ensure_dirs(self) -> None:
        for path in (self.asks_dir, self.locks_dir):
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)

    @contextlib.contextmanager
    def _locked(self, ask_id: str) -> Iterator[None]:
        self._ensure_dirs()
        fd = os.open(self.locks_dir / f"{ask_id}.lock", os.O_RDWR | os.O_CREAT, 0o600)
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
        return {
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

    def _secret_field(self, record: dict[str, Any], field: str, ask_id: str) -> dict[str, Any]:
        spec = {f["name"]: f for f in self.effective_fields(record)}.get(field)
        if spec is None or spec["type"] != "secret":
            raise AskStoreError(f"{field!r} is not a secret field of {ask_id!r}", 400)
        return spec

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
        if is_live_id(ask_id):
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
            new_types = {f["name"]: f["type"] for f in fields}
            response = record.setdefault("response", {})
            for old in self.effective_fields(record):
                # A secret field that disappears (or turns into a body) takes
                # its sealed value with it: no orphaned secrets.
                if old["type"] == "secret" and new_types.get(old["name"]) != "secret":
                    if response.get(old["name"]) is not None:
                        self._require_secrets()
                        try:
                            self.vault.clear(ask_id, old["name"])
                        except AskApplyError as exc:
                            raise AskStoreError(f"could not clear the sealed secret: {exc}", 502) from None
                    response.pop(old["name"], None)
                elif old["name"] not in new_types:
                    response.pop(old["name"], None)
            record["fields"] = fields
            self._history(record, actor, "declare_fields", sorted(new_types))
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
            if secrets_in:
                self._require_secrets()
            response = record.setdefault("response", {})
            projected = {**response, **bodies}
            for name in secrets_in:
                projected[name] = {"set": True}

            def filled(name: str, f: dict[str, Any]) -> bool:
                v = projected.get(name)
                return bool(v) if f["type"] == "secret" else bool(str(v or "").strip())

            if not any(filled(n, f) for n, f in fields.items()):
                raise AskStoreError("nothing to submit: every field is empty", 422)
            missing = [n for n, f in fields.items() if f.get("required") and not filled(n, f)]
            if missing:
                raise AskStoreError("required field(s) empty: " + ", ".join(missing), 422)
            # Seal first: a vault failure leaves the record untouched.
            for name, value in secrets_in.items():
                try:
                    self.vault.seal(ask_id, name, value)
                except AskApplyError as exc:
                    raise AskStoreError(f"could not seal {name!r}: {_redact(str(exc), value)}", 502) from None
            changed = sorted(set(bodies) | set(secrets_in))
            live = is_live_id(ask_id)
            if is_closed_state(record.get("state")) and not live:
                # D3: an edit to a closed ask reopens it, then the submit
                # closes it again with the new attribution.
                record["state"] = "OPEN"
                self._history(record, actor, "reopen", changed)
            for name, text in bodies.items():
                response[name] = text
            for name, value in secrets_in.items():
                response[name] = {"set_at": now_iso(), "set_by": actor, "length": len(value), "sealed": True}
            stamp = f"CLOSED {now_iso()} by {actor} (web)"
            # A live row is context only: its stored state records who
            # answered, but the live gate/blocked row itself stays open.
            record["state"] = stamp
            self._history(record, actor, "respond", changed)
            self._save(record)
            return self.public_row(record)

    def clear_secret(self, ask_id: str, field: str, actor: str) -> dict[str, Any]:
        ask_id = validate_ask_id(ask_id)
        field = validate_field_name(field)
        actor = validate_actor(actor)
        self._require_secrets()
        with self._locked(ask_id):
            record = self._load(ask_id)
            if record is None:
                raise AskStoreError(f"ask {ask_id!r} not found", 404)
            self._secret_field(record, field, ask_id)
            try:
                removed = self.vault.clear(ask_id, field)
            except AskApplyError as exc:
                raise AskStoreError(f"could not clear the sealed secret: {exc}", 502) from None
            had_meta = (record.get("response") or {}).pop(field, None) is not None
            if not removed and not had_meta:
                raise AskStoreError(f"secret {field!r} is not set", 404)
            self._history(record, actor, "clear_secret", [field])
            self._save(record)
            return self.public_row(record)

    # ----------------------------------------------------------------- apply --
    def apply_secret(self, ask_id: str, field: str, target: Any, actor: str) -> dict[str, Any]:
        """Apply one sealed secret to its declared, allow-listed target (D1 + D3).

        Refusals that must NOT change the row raise AskStoreError: target not
        on the allow-list (403), unknown ask/field (404/400), the field
        declaring no or another target (409), secret tools disabled (503).
        Any vault failure after that reopens the row with the reason in
        ``evidence``; the value is never in the message.
        """
        ask_id = validate_ask_id(ask_id)
        field = validate_field_name(field)
        if not isinstance(target, str) or target not in ASK_APPLY_TARGETS:
            raise AskStoreError(f"target {str(target)[:128]!r} is not on the apply allow-list", 403)
        self._require_secrets()
        with self._locked(ask_id):
            record = self._load(ask_id)
            if record is None:
                raise AskStoreError(f"ask {ask_id!r} not found", 404)
            spec = self._secret_field(record, field, ask_id)
            declared = spec.get("apply_target")
            if declared != target:
                raise AskStoreError(
                    f"field {field!r} declares apply_target {declared!r}; refusing {target!r}", 409
                )
            try:
                self.vault.apply(ask_id, field, target)
            except Exception as exc:  # noqa: BLE001 - every failure reopens
                reason = _redact(str(exc) if isinstance(exc, AskApplyError) else f"{type(exc).__name__}: {exc}", None)
                note = f"apply failed {now_iso()}: {target} {reason}"
                evidence = (record.get("evidence") or "").strip()
                evidence = (evidence + " | " + note) if evidence else note
                record["evidence"] = evidence[-_COLUMN_LIMITS["evidence"]:]
                record["state"] = "OPEN"
                self._history(record, actor, "apply_failed", [field], target=target, reason=reason)
                self._save(record)
                raise AskStoreError(f"apply failed: {target} {reason}", 502) from None
            at = now_iso()
            # Applied: the vault deleted its sealed copy; the plaintext lives
            # only at the target from here on.
            meta = record.setdefault("response", {}).get(field) or {}
            meta.update({"applied_at": at, "applied_target": target, "sealed": False})
            record["response"][field] = meta
            self._history(record, actor, "apply", [field], target=target)
            self._save(record)
            return {"applied": True, "target": target, "at": at}


__all__ = [
    "AskApplyError",
    "AskRuntime",
    "AskStore",
    "AskStoreError",
    "HelperVault",
    "LocalVault",
    "default_fields",
    "is_closed_state",
    "load_ask_runtime",
    "looks_like_token",
    "readable_by",
    "scrub_worker_env",
    "validate_actor",
    "validate_ask_id",
    "validate_columns",
    "validate_fields",
]
