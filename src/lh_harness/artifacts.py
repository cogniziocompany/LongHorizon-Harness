"""Session context artifacts (LHH-SESSION-CONTEXT-ARTIFACTS).

A small, file-backed store for the ``ContextPacket`` blobs a Claude Code
session, an SDK call, or the ``lh-harness artifact push`` CLI posts to
``POST /api/artifacts``.  The store is keyed by a stable ``artifact_id``
(``art-<32-hex>``), returns it on POST, and exposes a read for
``GET /api/artifacts/<id>``.  Idempotency rides on
``producer.session_id + ':' + captured_at`` so a retrying producer never
duplicates.

Git is explicitly optional: ``files_touched`` paths are advisory, and the
producer is not required to be in a repo at all.  The store does not write
or read from any git remote.

The store lives on disk so the LH-Harness app stays hermetic; the
``PgArtifactStore`` migration (007_session_context_artifacts.sql) provides
the same shape for the Postgres-backed deployment.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .supervisor.control_bus import _atomic_bytes_write


_ARTIFACT_DIR = "artifacts"
_MAX_PACKET_BYTES = 1 * 1024 * 1024  # see webapi/server.py _MAX_CONTROL_BODY_BYTES
_MAX_TITLE_CHARS = 512
_MAX_SUMMARY_CHARS = 4096
_MAX_KIND_CHARS = 32
_MAX_PRODUCER_AGENT_CHARS = 64
_MAX_PRODUCER_HOST_CHARS = 256
_MAX_PRODUCER_SESSION_CHARS = 128
_MAX_PRODUCER_REF_CHARS = 64
_MAX_VISIBILITY_CHARS = 16

_VALID_KINDS = frozenset({"session_context", "task_handoff", "plan", "evidence"})
_VALID_AGENTS = frozenset({"claude_code", "codex", "opencode", "sdk", "other"})
_VALID_VISIBILITIES = frozenset({"private", "team", "all"})

_ARTIFACT_ID_RE = re.compile(r"^art-[0-9a-f]{8,64}$")
_SAFE_ARTIFACT_ID_RE = re.compile(r"^art-[0-9a-f]{8,64}$")


@dataclass
class Artifact:
    """One stored session-context artifact (a ContextPacket)."""

    artifact_id: str
    kind: str
    title: str
    summary: str
    producer_agent: str
    producer_host: str
    producer_session: str
    producer_ref: str
    captured_at: float
    visibility: str
    packet: dict[str, Any] = field(default_factory=dict)
    packet_sha256: str = ""
    bytes: int = 0
    redaction_count: int = 0
    created_at: float = 0.0
    idempotency_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "producer": {
                "agent": self.producer_agent,
                "host": self.producer_host,
                "session_id": self.producer_session,
                "session_ref": self.producer_ref,
                "captured_at": self.captured_at,
            },
            "visibility": self.visibility,
            "packet": self.packet,
            "packet_sha256": self.packet_sha256,
            "bytes": self.bytes,
            "redaction_count": self.redaction_count,
            "created_at": self.created_at,
            "idempotency_key": self.idempotency_key,
            "url": f"/artifacts/{self.artifact_id}",
            "api_url": f"/api/artifacts/{self.artifact_id}",
        }


class ArtifactValidationError(ValueError):
    """The packet failed shape validation (HTTP 422 surface)."""


class ArtifactConflictError(ValueError):
    """The packet's idempotency key collides with a different packet (HTTP 409)."""


def _bounded(value: Any, *, field: str, max_chars: int) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise ArtifactValidationError(f"{field} must be a string")
    text = value.strip()
    if len(text) > max_chars:
        raise ArtifactValidationError(f"{field} is too long")
    if "\x00" in text:
        raise ArtifactValidationError(f"{field} contains a NUL byte")
    return text


def _redact(value: Any) -> tuple[Any, int]:
    """Run :func:`redact_value` and count the number of replacements made.

    The count is recorded on the artifact for audit: a non-zero count on a
    packet the producer claimed was already-redacted is an honest signal that
    the producer's pre-redaction pass was incomplete.
    """
    # Lazy import keeps the artifact module usable in tests that do not
    # load the experience layer (the redact module pulls in trace units).
    from .experience.redact import redact_value

    def _walk(node: Any, counter: list[int]) -> Any:
        if isinstance(node, str):
            replaced = redact_value(node)
            if replaced != node:
                counter[0] += 1
            return replaced
        if isinstance(node, dict):
            return {k: _walk(v, counter) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(item, counter) for item in node]
        return node

    counter = [0]
    out = _walk(value, counter)
    return out, counter[0]


def _validate_packet_shape(packet: Any) -> dict[str, Any]:
    """Validate the top-level ContextPacket shape; raise on bad input.

    The shape rules follow docs/design/session-context-artifacts.md §3.
    """
    if isinstance(packet, bool) or not isinstance(packet, dict):
        raise ArtifactValidationError("packet must be an object")
    kind = _bounded(packet.get("kind"), field="kind", max_chars=_MAX_KIND_CHARS)
    if kind not in _VALID_KINDS:
        raise ArtifactValidationError(
            f"kind must be one of: {', '.join(sorted(_VALID_KINDS))}"
        )
    title = _bounded(packet.get("title", ""), field="title", max_chars=_MAX_TITLE_CHARS)
    summary = _bounded(
        packet.get("summary", ""), field="summary", max_chars=_MAX_SUMMARY_CHARS
    )
    producer_raw = packet.get("producer")
    if isinstance(producer_raw, bool) or not isinstance(producer_raw, dict):
        raise ArtifactValidationError("producer must be an object")
    agent = _bounded(
        producer_raw.get("agent", ""),
        field="producer.agent",
        max_chars=_MAX_PRODUCER_AGENT_CHARS,
    )
    if agent not in _VALID_AGENTS:
        raise ArtifactValidationError(
            f"producer.agent must be one of: {', '.join(sorted(_VALID_AGENTS))}"
        )
    host = _bounded(
        producer_raw.get("host", ""),
        field="producer.host",
        max_chars=_MAX_PRODUCER_HOST_CHARS,
    )
    session_id = _bounded(
        producer_raw.get("session_id", ""),
        field="producer.session_id",
        max_chars=_MAX_PRODUCER_SESSION_CHARS,
    )
    if not session_id:
        raise ArtifactValidationError("producer.session_id is required")
    ref = _bounded(
        producer_raw.get("session_ref", ""),
        field="producer.session_ref",
        max_chars=_MAX_PRODUCER_REF_CHARS,
    )
    captured_at_raw = producer_raw.get("captured_at", 0)
    if (
        isinstance(captured_at_raw, bool)
        or not isinstance(captured_at_raw, (int, float))
    ):
        raise ArtifactValidationError("producer.captured_at must be a number")
    captured_at = float(captured_at_raw)
    visibility = _bounded(
        packet.get("visibility", "private"),
        field="visibility",
        max_chars=_MAX_VISIBILITY_CHARS,
    )
    if visibility not in _VALID_VISIBILITIES:
        raise ArtifactValidationError(
            f"visibility must be one of: {', '.join(sorted(_VALID_VISIBILITIES))}"
        )
    return {
        "kind": kind,
        "title": title,
        "summary": summary,
        "producer": {
            "agent": agent,
            "host": host,
            "session_id": session_id,
            "session_ref": ref,
            "captured_at": captured_at,
        },
        "visibility": visibility,
        # Pass-through fields preserved verbatim; the API does not interpret
        # them, the renderer does.
        "transcript_delta": packet.get("transcript_delta") or [],
        "plan": packet.get("plan") or [],
        "decisions": packet.get("decisions") or [],
        "files_touched": packet.get("files_touched") or [],
        "links": packet.get("links") or [],
        "evidence": packet.get("evidence") or [],
    }


def _redact_packet(packet: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Apply the project's secret-scan rules to every string field.

    Rules reused from :mod:`lh_harness.experience.redact` (MSCE B.11):
    bearer tokens, ``sk-*``, GitHub tokens, PEM blocks, password=,
    secret-env-name assignments, long hex strings.
    """
    redacted, count = _redact(packet)
    if not isinstance(redacted, dict):
        raise ArtifactValidationError("packet must redact to an object")
    return redacted, count


def artifact_dir(runs_root: str | Path) -> Path:
    """``runs_root/artifacts`` directory; created on demand."""

    root = Path(runs_root).expanduser().resolve() / _ARTIFACT_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_artifact_id(value: str) -> bool:
    """True when ``value`` is a syntactically valid ``art-<hex>`` token."""

    return bool(_ARTIFACT_ID_RE.fullmatch(value))


class ArtifactStore:
    """Atomic file-backed store keyed by ``art-<hex>``."""

    def __init__(self, runs_root: str | Path) -> None:
        self.runs_root = Path(runs_root).expanduser().resolve()
        # Ensure the directory exists at construction so caller does not have
        # to know the relative path.
        self._root = artifact_dir(self.runs_root)
        self._idempotency_index: dict[str, str] = {}

    def _path(self, artifact_id: str) -> Path:
        if not safe_artifact_id(artifact_id):
            raise ArtifactValidationError("artifact_id is not valid")
        return self._root / f"{artifact_id}.json"

    def _build_idempotency_key(self, producer_session: str, captured_at: float) -> str:
        return f"{producer_session}:{captured_at:.6f}"

    def create(self, packet: Any, *, raw_body: bytes | None = None) -> tuple[Artifact, bool]:
        """Validate, redact, and persist one packet.

        Returns ``(artifact, created)``; ``created`` is False when an
        existing artifact with the same idempotency key was returned
        (a re-POST).  Raises :class:`ArtifactValidationError` on bad shape
        and :class:`ArtifactConflictError` when the idempotency key collides
        with a *different* ``packet_sha256`` (a producer reused a session id
        with a different captured_at or different content).
        """
        validated = _validate_packet_shape(packet)
        # Redact first; the redaction count is recorded.
        redacted, count = _redact_packet(validated)
        # Re-serialize the redacted packet to compute sha256 + bytes; the
        # caller may also pass the raw body for the on-the-wire digest.
        body = json.dumps(redacted, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if len(body) > _MAX_PACKET_BYTES:
            raise ArtifactValidationError(
                f"packet is too large (>{_MAX_PACKET_BYTES} bytes)"
            )
        sha = hashlib.sha256(body).hexdigest()

        idem_key = self._build_idempotency_key(
            redacted["producer"]["session_id"], redacted["producer"]["captured_at"]
        )
        existing = self._find_by_idempotency(idem_key)
        if existing is not None:
            if existing.packet_sha256 != sha:
                raise ArtifactConflictError(
                    "idempotency_key collision with a different packet"
                )
            return existing, False

        artifact_id = f"art-{uuid.uuid4().hex[:32]}"
        artifact = Artifact(
            artifact_id=artifact_id,
            kind=redacted["kind"],
            title=redacted["title"],
            summary=redacted["summary"],
            producer_agent=redacted["producer"]["agent"],
            producer_host=redacted["producer"]["host"],
            producer_session=redacted["producer"]["session_id"],
            producer_ref=redacted["producer"]["session_ref"],
            captured_at=redacted["producer"]["captured_at"],
            visibility=redacted["visibility"],
            packet=redacted,
            packet_sha256=sha,
            bytes=len(body),
            redaction_count=count,
            created_at=time.time(),
            idempotency_key=idem_key,
        )
        self._write(artifact)
        return artifact, True

    def _write(self, artifact: Artifact) -> None:
        payload = json.dumps(artifact.to_dict(), ensure_ascii=False, sort_keys=True)
        _atomic_bytes_write(self._path(artifact.artifact_id), payload.encode("utf-8"))

    def get(self, artifact_id: str) -> Artifact | None:
        if not safe_artifact_id(artifact_id):
            return None
        path = self._path(artifact_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        artifact = Artifact(
            artifact_id=str(data.get("artifact_id") or artifact_id),
            kind=str(data.get("kind", "")),
            title=str(data.get("title", "")),
            summary=str(data.get("summary", "")),
            producer_agent=str(data.get("producer_agent", "")),
            producer_host=str(data.get("producer_host", "")),
            producer_session=str(data.get("producer_session", "")),
            producer_ref=str(data.get("producer_ref", "")),
            captured_at=float(data.get("captured_at", 0) or 0),
            visibility=str(data.get("visibility", "private")),
            packet=data.get("packet") if isinstance(data.get("packet"), dict) else {},
            packet_sha256=str(data.get("packet_sha256", "")),
            bytes=int(data.get("bytes", 0) or 0),
            redaction_count=int(data.get("redaction_count", 0) or 0),
            created_at=float(data.get("created_at", 0) or 0),
            idempotency_key=str(data.get("idempotency_key", "")),
        )
        if artifact.artifact_id != artifact_id:
            return None
        return artifact

    def _find_by_idempotency(self, key: str) -> Artifact | None:
        """Linear scan over the (small) artifact directory.

        The store is not indexed by idempotency key in memory; a list scan
        is acceptable for the first slice.  ``list_runs`` already scans the
        directory, so the cost is dominated by JSON parsing.
        """
        if not key:
            return None
        for path in sorted(self._root.glob("art-*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            if data.get("idempotency_key") == key:
                return self.get(str(data.get("artifact_id") or ""))
        return None


def _select_artifact_store(
    runs_root: str | Path | None, project: dict[str, Any] | None
) -> ArtifactStore | None:
    """Pick the artifact store backend.

    Mirrors ``_select_queue_store``: file-backed by default, and the
    Postgres backend is the same shape through migration 007.  This slice
    implements only the file-backed store.
    """
    if runs_root is None:
        return None
    return ArtifactStore(runs_root)


__all__ = [
    "Artifact",
    "ArtifactStore",
    "ArtifactValidationError",
    "ArtifactConflictError",
    "safe_artifact_id",
    "_select_artifact_store",
]
