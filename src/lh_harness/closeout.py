"""The harness close-out contract: how a run's WORK ended.

Why this exists
---------------
A run's ``status`` is the *process lifecycle* — did the worker exit, was it
killed, did it run out of rounds.  It says nothing about whether the run's
*work* was delivered.  The normal way a harness run ends is a "stop" at the
end-of-round gate, which the harness records as ``status=cancelled`` even
when the run delivered everything it was asked to deliver.  A run that
shipped its PR and a run that was killed mid-flight are then
indistinguishable to any consumer that reads ``status``.

That makes ``status`` useless as an optimisation signal: an RSI (recursive
self-improvement) loop that tunes on "how many runs completed?" mostly tunes
on noise (1390 cancelled vs 1 completed across the fleet).

So ``status`` stays exactly as it is — many consumers depend on it — and this
module adds a SEPARATE, authoritative close-out outcome beside it.

The close-out FORM
------------------
Closing a run requires a structured form, not a free-text rationale:

=========================  ==================================================
``outcome``                one of the six values in :data:`CLOSE_OUT_OUTCOMES`
``outcome_reason``         short, ASCII, at most 200 characters
``artifact_refs``          list of ``{kind, repo, ref, url}`` artifacts
``closed_by``              ``agent`` or ``human``
``closed_by_identity``     who closed it (a name, a key id, a gateway id)
``closed_at``              epoch seconds
``delivered``              explicit boolean, true only for a delivery
``derived``                true when the value was inferred, not observed
``derived_from``           e.g. ``rationale_parse`` for legacy runs
=========================  ==================================================

Both closers supply it: the agent (manager) path that proposes done, and the
human path (gate resolution / stop action).  They go through ONE validator
(:func:`validate_close_out`), so both produce identical record shapes.

Guards
------
* A ``delivered``/``delivered_partial`` outcome MUST be backed by at least
  one verifiable artifact.  There is no ``delivered_no_artifact`` variant:
  an artifactless delivery is refused, and the caller must use
  ``blocked``/``incomplete`` with a reason instead.
* The manager must not be able to self-certify completion.  The independent
  auditor verdict is the authority: a ``delivered`` close-out that conflicts
  with it is rejected and the run is left ``incomplete`` with a conflict
  reason.
* A run is closed once.  The close-out is immutable; re-closing requires an
  explicit audited amendment carrying an actor and a reason.
* A free-text ``done:`` rationale is NEVER a valid close-out.  It is parsed
  only as a legacy migration hint, and any value derived that way is marked
  ``derived=True`` / ``derived_from="rationale_parse"``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

# --------------------------------------------------------------------------
# The vocabulary.  This is the SINGLE source of truth for the six-value
# close-out outcome enum.  Reporting-sync and RSI loops import it rather
# than re-deriving the semantics.
# --------------------------------------------------------------------------

#: The canonical close-out outcome enum.  Exactly six values, no synonyms.
#:
#: ``delivered``          the work was completed and an artifact proves it
#: ``delivered_partial``  part of the work landed (artifacts required too)
#: ``blocked``            the run could not proceed and needs a human
#: ``failed``             the run failed (provider, crash, protocol)
#: ``aborted``            the run was deliberately terminated
#: ``incomplete``         the run ended without delivering, for any reason
CLOSE_OUT_OUTCOMES: tuple[str, ...] = (
    "delivered",
    "delivered_partial",
    "blocked",
    "failed",
    "aborted",
    "incomplete",
)

#: Outcomes that count as a delivery.  ``delivered`` is true for these and
#: ONLY these — it is set here, at close-out time, and never inferred
#: downstream.
DELIVERED_OUTCOMES: frozenset[str] = frozenset({"delivered", "delivered_partial"})

#: Who may close a run.
CLOSED_BY_VALUES: frozenset[str] = frozenset({"agent", "human"})

#: The kinds of artifact a close-out may cite.
ARTIFACT_KINDS: frozenset[str] = frozenset({"pr", "commit", "file"})

#: Reasons are forwarded into ASCII-only channels (CT110 gate rationales),
#: so they are bounded and ASCII.
REASON_MAX_CHARS = 200

#: The close-out record's schema version.
CLOSE_OUT_SCHEMA_VERSION = 1

#: The file name of the durable close-out record, written next to
#: ``report.json`` in the run's log directory.
CLOSE_OUT_FILENAME = "closeout.json"

#: A free-text ``done:`` rationale — the legacy gate close-out.  Parsed ONLY
#: as a migration hint.
_LEGACY_DONE_RE = re.compile(r"^done\s*:\s*(?P<body>.+)$", re.IGNORECASE | re.DOTALL)

#: A legacy ``done:`` body naming a PR, e.g.
#: ``cogniziocompany/cognizioware-hydra#96``.
_LEGACY_PR_RE = re.compile(
    r"^(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<number>\d+)"
)


class CloseOutError(Exception):
    """Base class for close-out contract errors."""


class CloseOutValidationError(CloseOutError):
    """The submitted close-out is invalid, missing, or conflicts with truth.

    A run whose close-out fails validation is NOT reported as completed: it
    stays close-out incomplete, with the reason recorded.
    """


# --------------------------------------------------------------------------
# The record types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactRef:
    """One verifiable artifact backing a close-out.

    ``kind`` is ``pr``, ``commit`` or ``file``.  ``repo`` names the
    repository (empty for a local file), ``ref`` the PR number / commit sha
    / path, and ``url`` the browsable location.

    ``derived`` marks an artifact that was TRANSCRIBED (e.g. read out of a
    legacy ``done:`` rationale) rather than verified against the forge.  A
    derived artifact is not a weaker kind of artifact — the close-out is
    already marked derived — it is an honest statement that the URL was
    reconstructed from a human's free text, not fetched from GitHub.
    """

    kind: str
    repo: str
    ref: str
    url: str
    derived: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "repo": self.repo,
            "ref": self.ref,
            "url": self.url,
            "derived": self.derived,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ArtifactRef":
        return cls(
            kind=str(payload.get("kind") or ""),
            repo=str(payload.get("repo") or ""),
            ref=str(payload.get("ref") or ""),
            url=str(payload.get("url") or ""),
            derived=bool(payload.get("derived")),
        )


@dataclass(frozen=True)
class CloseOutAmendment:
    """An explicit, audited amendment to an existing close-out.

    A close-out is immutable.  Re-closing a run requires an amendment that
    names WHO changed it (``actor``) and WHY (``reason``).  ``previous``
    keeps the record that was amended, so the history is not lost.
    """

    actor: str
    reason: str
    at: float
    previous: "CloseOutRecord | None" = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "reason": self.reason,
            "at": self.at,
            "previous": self.previous.to_dict() if self.previous is not None else None,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CloseOutAmendment":
        previous_raw = payload.get("previous")
        previous = (
            CloseOutRecord.from_dict(previous_raw)
            if isinstance(previous_raw, Mapping)
            else None
        )
        return cls(
            actor=str(payload.get("actor") or ""),
            reason=str(payload.get("reason") or ""),
            at=float(payload.get("at") or 0.0),
            previous=previous,
        )


@dataclass(frozen=True)
class CloseOutRecord:
    """The durable close-out record for one run.

    ``delivered`` is an EXPLICIT boolean, true only for
    ``delivered``/``delivered_partial``.  Consumers must read it; they must
    not re-derive it from ``outcome`` on their own, because the whole point
    of the contract is that the value is set once, by the validator, and
    carried unchanged.
    """

    outcome: str
    outcome_reason: str
    artifact_refs: tuple[ArtifactRef, ...]
    closed_by: str
    closed_by_identity: str
    closed_at: float
    delivered: bool
    derived: bool = False
    derived_from: str = ""
    amendment: CloseOutAmendment | None = None
    schema_version: int = CLOSE_OUT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "outcome": self.outcome,
            "outcome_reason": self.outcome_reason,
            "artifact_refs": [ref.to_dict() for ref in self.artifact_refs],
            "closed_by": self.closed_by,
            "closed_by_identity": self.closed_by_identity,
            "closed_at": self.closed_at,
            "delivered": self.delivered,
            "derived": self.derived,
            "derived_from": self.derived_from,
            "amendment": self.amendment.to_dict() if self.amendment is not None else None,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CloseOutRecord":
        refs_raw = payload.get("artifact_refs")
        refs = tuple(
            ArtifactRef.from_dict(item)
            for item in (refs_raw if isinstance(refs_raw, list) else [])
            if isinstance(item, Mapping)
        )
        amendment_raw = payload.get("amendment")
        amendment = (
            CloseOutAmendment.from_dict(amendment_raw)
            if isinstance(amendment_raw, Mapping)
            else None
        )
        return cls(
            outcome=str(payload.get("outcome") or ""),
            outcome_reason=str(payload.get("outcome_reason") or ""),
            artifact_refs=refs,
            closed_by=str(payload.get("closed_by") or ""),
            closed_by_identity=str(payload.get("closed_by_identity") or ""),
            closed_at=float(payload.get("closed_at") or 0.0),
            delivered=bool(payload.get("delivered")),
            derived=bool(payload.get("derived")),
            derived_from=str(payload.get("derived_from") or ""),
            amendment=amendment,
            schema_version=int(payload.get("schema_version") or CLOSE_OUT_SCHEMA_VERSION),
        )


# --------------------------------------------------------------------------
# The ONE validator
# --------------------------------------------------------------------------


def _is_ascii(value: str) -> bool:
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def _require_reason(value: Any) -> str:
    if not isinstance(value, str):
        raise CloseOutValidationError("outcome_reason is required and must be a string")
    text = value.strip()
    if not text:
        raise CloseOutValidationError("outcome_reason is required and must not be empty")
    if len(text) > REASON_MAX_CHARS:
        raise CloseOutValidationError(
            f"outcome_reason must be at most {REASON_MAX_CHARS} characters"
        )
    if not _is_ascii(text):
        raise CloseOutValidationError("outcome_reason must be ASCII")
    return text


def _require_artifacts(value: Any) -> tuple[ArtifactRef, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise CloseOutValidationError("artifact_refs must be a list")
    refs: list[ArtifactRef] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise CloseOutValidationError("each artifact_ref must be an object")
        ref = ArtifactRef.from_dict(item)
        if ref.kind not in ARTIFACT_KINDS:
            raise CloseOutValidationError(
                f"artifact_ref.kind must be one of {sorted(ARTIFACT_KINDS)}"
            )
        if not ref.ref.strip():
            raise CloseOutValidationError("artifact_ref.ref is required")
        if not ref.url.strip():
            raise CloseOutValidationError("artifact_ref.url is required")
        if not _is_ascii(ref.url):
            raise CloseOutValidationError("artifact_ref.url must be ASCII")
        refs.append(ref)
    return tuple(refs)


def _auditor_conflict(
    outcome: str,
    auditor_verdict: Mapping[str, Any] | None,
) -> str | None:
    """Return a conflict reason when a delivery contradicts the auditor.

    The auditor is the independent authority on whether the work actually
    landed.  The manager may PROPOSE a delivery, but it cannot certify one
    on its own: if the auditor's verdict says the round did not complete
    cleanly, a ``delivered`` close-out conflicts with it and is refused.

    Only deliveries are gated this way.  An honest ``blocked`` close-out is
    never refused because the auditor disagreed — the auditor vetoes
    DELIVERY, it does not veto honesty.
    """

    if outcome not in DELIVERED_OUTCOMES:
        return None
    if not isinstance(auditor_verdict, Mapping):
        # No verdict reachable from this call site: the delivery cannot be
        # confirmed here.  The caller decides whether that is acceptable;
        # this function stays conservative and reports the absence.
        return None
    status = str(auditor_verdict.get("status") or "").strip().lower()
    integrity = str(auditor_verdict.get("integrity_status") or "").strip().lower()
    if integrity and integrity != "clean":
        return (
            f"auditor conflict: integrity_status={integrity} contradicts a "
            f"{outcome} close-out"
        )
    if status and status not in {"complete", "completed"}:
        return (
            f"auditor conflict: auditor status={status} contradicts a "
            f"{outcome} close-out"
        )
    return None


def validate_close_out(
    payload: Mapping[str, Any] | None,
    *,
    closed_by: str,
    auditor_verdict: Mapping[str, Any] | None = None,
) -> CloseOutRecord:
    """Validate one close-out form and return the canonical record.

    This is the ONE validator: the agent close path and the human close path
    both go through it, so both produce identical record shapes.

    Rules (all of them refuse, raising :class:`CloseOutValidationError`):

    1. the form must be present;
    2. ``outcome`` must be one of the six canonical values;
    3. ``outcome_reason`` must be a non-empty ASCII string of at most 200
       characters;
    4. ``artifact_refs`` must be a list of well-formed
       ``{kind: pr|commit|file, repo, ref, url}`` objects;
    5. ``delivered``/``delivered_partial`` MUST cite at least one artifact —
       there is no artifactless delivery;
    6. ``closed_by`` must be ``agent`` or ``human`` and must match the
       caller;
    7. ``closed_by_identity`` and ``closed_at`` are required;
    8. a ``delivered`` close-out that conflicts with the auditor verdict is
       rejected.

    A refused close-out must leave the run close-out incomplete with the
    reason recorded — never "completed".
    """

    if payload is None:
        raise CloseOutValidationError("a close-out form is required to close a run")
    if not isinstance(payload, Mapping):
        raise CloseOutValidationError("a close-out form is required to close a run")

    # A DERIVED record (e.g. one reconstructed from a legacy ``done:``
    # rationale) is never a valid standalone close-out.  It exists so a
    # historical run can carry a truthful, clearly-marked outcome; it is
    # not a form anybody submitted, so it cannot close a run.
    if payload.get("derived"):
        raise CloseOutValidationError(
            "a derived close-out (derived_from="
            f"{str(payload.get('derived_from') or 'unknown')!r}) is a migration "
            "hint, not a valid close-out; supply an observed close-out form"
        )

    outcome = payload.get("outcome")
    if not isinstance(outcome, str) or not outcome.strip():
        raise CloseOutValidationError("outcome is required")
    outcome = outcome.strip()
    if outcome not in CLOSE_OUT_OUTCOMES:
        raise CloseOutValidationError(
            f"outcome must be one of {list(CLOSE_OUT_OUTCOMES)}, got {outcome!r}"
        )

    reason = _require_reason(payload.get("outcome_reason"))
    refs = _require_artifacts(payload.get("artifact_refs"))

    if outcome in DELIVERED_OUTCOMES and not refs:
        raise CloseOutValidationError(
            f"a {outcome} close-out must be backed by at least one verifiable "
            "artifact_ref; use blocked/incomplete with a reason instead"
        )

    caller = str(closed_by or "").strip()
    if caller not in CLOSED_BY_VALUES:
        raise CloseOutValidationError(
            f"closed_by must be one of {sorted(CLOSED_BY_VALUES)}"
        )
    payload_closed_by = payload.get("closed_by")
    if payload_closed_by is None:
        raise CloseOutValidationError("closed_by is required")
    if not isinstance(payload_closed_by, str) or not payload_closed_by.strip():
        raise CloseOutValidationError("closed_by is required")
    payload_closed_by = payload_closed_by.strip()
    if payload_closed_by not in CLOSED_BY_VALUES:
        raise CloseOutValidationError(
            f"closed_by must be one of {sorted(CLOSED_BY_VALUES)}"
        )
    if payload_closed_by != caller:
        raise CloseOutValidationError(
            f"closed_by {payload_closed_by!r} does not match the closing path {caller!r}"
        )

    identity = payload.get("closed_by_identity")
    if not isinstance(identity, str) or not identity.strip():
        raise CloseOutValidationError("closed_by_identity is required")
    identity = identity.strip()
    if not _is_ascii(identity):
        raise CloseOutValidationError("closed_by_identity must be ASCII")

    closed_at = payload.get("closed_at")
    if isinstance(closed_at, bool) or not isinstance(closed_at, (int, float)):
        raise CloseOutValidationError("closed_at is required and must be a number")
    closed_at = float(closed_at)
    if closed_at <= 0:
        raise CloseOutValidationError("closed_at must be a positive epoch timestamp")

    conflict = _auditor_conflict(outcome, auditor_verdict)
    if conflict:
        raise CloseOutValidationError(conflict)

    # A close-out that CLAIMS to be derived must say where it came from.
    derived = bool(payload.get("derived"))
    derived_from = str(payload.get("derived_from") or "").strip()
    if derived and not derived_from:
        raise CloseOutValidationError("derived_from is required when derived is true")

    return CloseOutRecord(
        outcome=outcome,
        outcome_reason=reason,
        artifact_refs=refs,
        closed_by=payload_closed_by,
        closed_by_identity=identity,
        closed_at=closed_at,
        delivered=outcome in DELIVERED_OUTCOMES,
        derived=derived,
        derived_from=derived_from,
    )


# --------------------------------------------------------------------------
# The legacy migration hint
# --------------------------------------------------------------------------


def parse_legacy_rationale(rationale: str | None) -> CloseOutRecord | None:
    """Parse a free-text ``done:`` rationale as a LEGACY MIGRATION HINT.

    A ``done:`` rationale is what the gate used to record when an operator
    ended a run that had delivered its work.  It is NOT a close-out form:
    it has no artifact, no identity and no timestamp.  It is parsed here
    only so historical runs can carry a derived close-out, and any record
    produced this way is marked ``derived=True`` with
    ``derived_from="rationale_parse"``.

    The returned record is never a valid standalone close-out: it carries no
    artifact, so validating it is refused (see
    :func:`validate_close_out`).
    """

    if not isinstance(rationale, str):
        return None
    text = rationale.strip()
    if not text:
        return None
    match = _LEGACY_DONE_RE.match(text)
    if match is None:
        return None
    body = match.group("body").strip()
    if not body:
        return None

    # A legacy rationale MAY name the PR it delivered.  When it does, the
    # artifact is recorded as DERIVED: the URL was reconstructed from a
    # human's free text, never fetched from the forge, so it must not read
    # as a verified artifact.
    refs: tuple[ArtifactRef, ...] = ()
    pr_match = _LEGACY_PR_RE.match(body)
    if pr_match is not None:
        repo = pr_match.group("repo")
        number = pr_match.group("number")
        refs = (
            ArtifactRef(
                kind="pr",
                repo=repo,
                ref=number,
                url=f"https://github.com/{repo}/pull/{number}",
                derived=True,
            ),
        )

    return CloseOutRecord(
        outcome="delivered",
        outcome_reason=body[:REASON_MAX_CHARS],
        artifact_refs=refs,
        # The operator who resolved the gate closed the run; the harness
        # cannot know which one, so the identity stays empty for the record
        # and the caller fills it in from the gate's own audit trail.
        closed_by="human",
        closed_by_identity="",
        closed_at=0.0,
        delivered=True,
        derived=True,
        derived_from="rationale_parse",
    )


# --------------------------------------------------------------------------
# The agent's close-out proposal
# --------------------------------------------------------------------------

#: The agent proposes its close-out as a structured block in its plan text:
#:
#:     ```closeout
#:     outcome: delivered
#:     reason: opened hydra#96 and verified the head sha
#:     artifact: pr cogniziocompany/cognizioware-hydra 96
#:     ```
#:
#: The agent may PROPOSE a close-out; it may not CERTIFY one.  The proposal
#: is parsed here and handed to :func:`validate_close_out`, which is the
#: same validator the human path uses.
_CLOSE_OUT_BLOCK_RE = re.compile(
    r"```+\s*close[_\- ]?out\s*\n(?P<body>.*?)```+",
    re.IGNORECASE | re.DOTALL,
)

#: ``artifact: <kind> <repo> <ref>`` — the kind is pr / commit / file.
_ARTIFACT_LINE_RE = re.compile(
    r"^\s*artifact\s*:\s*(?P<kind>pr|commit|file)\s+(?P<repo>\S+)\s+(?P<ref>\S+)\s*$",
    re.IGNORECASE,
)

#: A ``key: value`` line inside the block.
_CLOSE_OUT_FIELD_RE = re.compile(r"^\s*(?P<key>[a-z_]+)\s*:\s*(?P<value>.*?)\s*$")


def parse_agent_close_out_proposal(plan_text: str | None) -> dict[str, Any] | None:
    """Parse the agent's close-out proposal out of its plan text.

    Returns the proposed close-out FORM (the same shape the human path
    builds), or ``None`` when the agent proposed nothing.  A proposal is
    never a valid close-out on its own: it still has to pass
    :func:`validate_close_out`, which refuses an artifactless delivery and
    a delivery that conflicts with the auditor verdict.
    """

    if not isinstance(plan_text, str):
        return None
    match = _CLOSE_OUT_BLOCK_RE.search(plan_text)
    if match is None:
        return None
    outcome = ""
    reason = ""
    artifact_refs: list[dict[str, str]] = []
    for line in match.group("body").splitlines():
        artifact_match = _ARTIFACT_LINE_RE.match(line)
        if artifact_match is not None:
            kind = artifact_match.group("kind").lower()
            repo = artifact_match.group("repo")
            ref = artifact_match.group("ref")
            artifact_refs.append(
                {
                    "kind": kind,
                    "repo": repo,
                    "ref": ref,
                    "url": _artifact_url(kind, repo, ref),
                }
            )
            continue
        field_match = _CLOSE_OUT_FIELD_RE.match(line)
        if field_match is None:
            continue
        key = field_match.group("key").lower()
        value = field_match.group("value")
        if key == "outcome":
            outcome = value
        elif key in {"reason", "outcome_reason"}:
            reason = value
    if not outcome:
        return None
    return {
        "outcome": outcome,
        "outcome_reason": reason,
        "artifact_refs": artifact_refs,
        "closed_by": "agent",
        "closed_by_identity": "",
        "closed_at": 0.0,
    }


def _artifact_url(kind: str, repo: str, ref: str) -> str:
    """The browsable URL for one artifact reference."""

    if kind == "pr":
        return f"https://github.com/{repo}/pull/{ref}"
    if kind == "commit":
        return f"https://github.com/{repo}/commit/{ref}"
    return ref


# --------------------------------------------------------------------------
# Persistence: the durable record next to report.json
# --------------------------------------------------------------------------


def _close_out_path(logs_dir: Path) -> Path:
    return Path(logs_dir) / CLOSE_OUT_FILENAME


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON durably: temp file in the same directory, then rename."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def read_close_out(logs_dir: Path) -> CloseOutRecord | None:
    """Read the durable close-out record, or ``None`` when there is none."""

    path = _close_out_path(logs_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return CloseOutRecord.from_dict(payload)
    except (TypeError, ValueError):
        return None


def load_close_out(logs_dir: Path) -> CloseOutRecord | None:
    """Alias for :func:`read_close_out` (the durable record's loader)."""

    return read_close_out(logs_dir)


def persist_close_out(
    logs_dir: Path,
    record: CloseOutRecord,
    *,
    amendment: CloseOutAmendment | None = None,
) -> Path:
    """Persist the close-out record next to ``report.json``.

    The record is written ONCE per run and is then immutable.  A second
    write is refused unless it carries an explicit audited amendment with an
    actor and a reason; the amended record keeps the previous one in
    ``amendment.previous`` so the history is preserved.
    """

    path = _close_out_path(logs_dir)
    existing = read_close_out(logs_dir)
    if existing is not None:
        if amendment is None:
            raise CloseOutError(
                "this run is already closed; a re-close requires an explicit "
                "audited amendment carrying an actor and a reason"
            )
        if not amendment.actor.strip():
            raise CloseOutError("an amendment requires an actor")
        if not amendment.reason.strip():
            raise CloseOutError("an amendment requires a reason")
        if amendment.previous is None:
            amendment = CloseOutAmendment(
                actor=amendment.actor,
                reason=amendment.reason,
                at=amendment.at,
                previous=existing,
            )
    record = CloseOutRecord(
        outcome=record.outcome,
        outcome_reason=record.outcome_reason,
        artifact_refs=record.artifact_refs,
        closed_by=record.closed_by,
        closed_by_identity=record.closed_by_identity,
        closed_at=record.closed_at,
        delivered=record.delivered,
        derived=record.derived,
        derived_from=record.derived_from,
        amendment=amendment,
        schema_version=record.schema_version,
    )
    _write_json_atomic(path, record.to_dict())
    return path


def apply_close_out_to_report(
    report: dict[str, Any],
    record: CloseOutRecord,
) -> dict[str, Any]:
    """Mirror the close-out record into the run status fields.

    The mirrored fields are exactly the ones consumers read:

    ``outcome``, ``outcome_reason``, ``artifact_refs``, ``closed_by``,
    ``closed_by_identity``, ``closed_at`` and the explicit ``delivered``
    boolean.

    The process lifecycle (``status``, ``completion_satisfied``,
    ``abort_reason``, ``exit_code`` …) is NOT touched: this adds a separate
    signal, it does not rewrite the existing one.
    """

    report["outcome"] = record.outcome
    report["outcome_reason"] = record.outcome_reason
    report["artifact_refs"] = [ref.to_dict() for ref in record.artifact_refs]
    report["closed_by"] = record.closed_by
    report["closed_by_identity"] = record.closed_by_identity
    report["closed_at"] = record.closed_at
    report["delivered"] = record.delivered
    if record.derived:
        report["derived"] = True
        report["derived_from"] = record.derived_from
    return report


__all__ = [
    "ARTIFACT_KINDS",
    "CLOSE_OUT_FILENAME",
    "CLOSE_OUT_OUTCOMES",
    "CLOSE_OUT_SCHEMA_VERSION",
    "CLOSED_BY_VALUES",
    "DELIVERED_OUTCOMES",
    "REASON_MAX_CHARS",
    "ArtifactRef",
    "CloseOutAmendment",
    "CloseOutError",
    "CloseOutRecord",
    "CloseOutValidationError",
    "apply_close_out_to_report",
    "load_close_out",
    "parse_agent_close_out_proposal",
    "parse_legacy_rationale",
    "persist_close_out",
    "read_close_out",
    "validate_close_out",
]
