"""Wire adapter for the effect-lease gate.

The lease module (``effect_lease``) is storage + CAS only.  The wire layer
below turns a gate outcome into a FastAPI ``HTTPException`` and a JSON-RPC
error, plus the receipt the harness returns to the caller.

The gate is OFF by default: ``is_enabled()`` returns False until an operator
sets ``LH_HARNESS_EFFECT_LEASE_ENABLED=true``.  When OFF, every helper is a
no-op so the pre-lease harness behaviour is preserved bit-for-bit.

The wire helper is keyed by the FastAPI app's ``state.effect_gate``.  When
no gate has been installed, the helper is again a no-op.  This keeps the
new code inert on test apps that don't need leases.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import HTTPException, Request

from . import effect_lease

logger = logging.getLogger(__name__)


# Header and body fields the gate expects on the wire.  Same names on REST
# and MCP so the on-the-wire shape is identical.
FAMILY_HEADER = "x-harness-lease-family"
EPOCH_HEADER = "x-harness-lease-epoch"
ACTION_HEADER = "x-harness-lease-action"

# A few endpoints and tools have no caller-identity header.  The gate may
# still be wired by an operator via the LH_HARNESS_EFFECT_LEASE_BEARER env
# (a static bearer-equivalent lease holder for that family).  When unset,
# bearer-only endpoints stay un-gated regardless of the flag.
DEFAULT_BEARER_HOLDER_ENV = "LH_HARNESS_EFFECT_LEASE_BEARER_HOLDER"
DEFAULT_BEARER_FAMILY_ENV = "LH_HARNESS_EFFECT_LEASE_BEARER_FAMILY"


def bearer_holder() -> tuple[str, str] | None:
    """The static (caller, family) the gate uses for bearer-only endpoints."""
    holder = os.environ.get(DEFAULT_BEARER_HOLDER_ENV, "").strip()
    family = os.environ.get(DEFAULT_BEARER_FAMILY_ENV, "").strip()
    if holder and family:
        return holder, family
    return None


def parse_epoch_header(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = int(text)
        except ValueError:
            return None
        return number
    return None


def parse_family_header(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text in effect_lease.FAMILIES:
        return text
    return None


def _gate_for(request: Request) -> effect_lease.LeaseGate | None:
    """Find the installed gate, if any.  Reads ``request.app.state``."""
    state = getattr(request.app, "state", None)
    return getattr(state, "effect_gate", None) if state is not None else None


def _deny(exc: effect_lease.LeaseError) -> HTTPException:
    """Map a ``LeaseError`` to an HTTP 409 with a structured body."""
    detail: dict[str, Any] = {
        "error": exc.reason,
        "code": exc.code,
        "message": exc.message,
    }
    detail.update(exc.detail)
    return HTTPException(status_code=exc.code, detail=detail)


def check_effect(
    request: Request,
    *,
    caller: str | None,
    tool: str = "rest",
    action_id: str | None = None,
) -> dict[str, str] | None:
    """Gate a mutating call.  Returns a receipt dict on accept, raises
    HTTPException on deny.  Returns ``None`` when the gate is OFF or no
    gate has been installed: the rest of the route keeps behaving exactly
    like the pre-lease harness.

    ``caller`` is the verified caller name from the existing
    ``_scoped_caller`` / ``_run_control_caller`` path (already HMAC
    verified).  When the gate is OFF we don't even look at headers.
    """
    if not effect_lease.is_enabled():
        return None
    gate = _gate_for(request)
    if gate is None:
        return None
    family_header = parse_family_header(request.headers.get(FAMILY_HEADER))
    epoch_header = parse_epoch_header(request.headers.get(EPOCH_HEADER))
    action_header = request.headers.get(ACTION_HEADER) or action_id
    if caller is None:
        # Bearer-only path: a misconfigured lease that requires a caller we
        # don't have.  Refuse with 409 unless the deployment has a default
        # bearer holder mapping.
        default = bearer_holder()
        if default is None:
            return None
        caller, family_header = default[0], default[1] if family_header is None else family_header
        if family_header is None:
            raise _deny(effect_lease.MissingLease(default[1]))
    family = family_header
    if family is None:
        # The route is mutating but did not name a family.  Infer from the
        # caller's declared family; refuse if unknown.
        family = gate.family_for(caller)
    if family is None:
        # Caller is not declared under any family: not lease-gated.
        return None
    try:
        result = gate.check(
            caller=caller,
            family=family,
            observed_epoch=epoch_header,
            action_id=action_header,
        )
    except effect_lease.LeaseError as exc:
        logger.info(
            "effect-lease deny family=%s caller=%s tool=%s reason=%s",
            family,
            caller,
            tool,
            exc.reason,
        )
        raise _deny(exc) from None
    return result.receipt


def install_gate(
    request: Request,
    *,
    store: effect_lease.LeaseStore,
    family_map: dict[str, str] | None = None,
    pair_map: dict[str, str] | None = None,
) -> effect_lease.LeaseGate:
    """Attach a fresh gate to the app; return it so callers can register
    caller→family mappings."""
    gate = effect_lease.LeaseGate(store, family_map=family_map, pair_map=pair_map)
    state = getattr(request.app, "state", None)
    if state is not None:
        state.effect_gate = gate
    return gate


__all__ = [
    "FAMILY_HEADER",
    "EPOCH_HEADER",
    "ACTION_HEADER",
    "bearer_holder",
    "check_effect",
    "install_gate",
    "parse_epoch_header",
    "parse_family_header",
]