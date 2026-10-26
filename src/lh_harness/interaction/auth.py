from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


class InteractionAssertionError(ValueError):
    pass


def canonical_body_digest(body: dict[str, Any]) -> str:
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _b64encode(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not value or len(value) > 16_384:
        raise InteractionAssertionError("interaction assertion is invalid")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeError) as exc:
        raise InteractionAssertionError("interaction assertion is invalid") from exc


def issue_interaction_assertion(
    secret: str,
    claims: dict[str, Any],
    *,
    now: float | None = None,
) -> str:
    issued = int(time.time() if now is None else now)
    payload = {"iat": issued, "exp": issued + 120, **claims}
    encoded = _b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_interaction_assertion(
    assertion: str | None,
    secret: str | None,
    *,
    run_id: str,
    action: str,
    body: dict[str, Any],
    role: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    if not secret:
        raise InteractionAssertionError("interaction assertions are not configured")
    if not assertion or assertion.count(".") != 1:
        raise InteractionAssertionError("a signed interaction assertion is required")
    encoded, supplied = assertion.split(".", 1)
    expected = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        raise InteractionAssertionError("interaction assertion signature is invalid")
    try:
        claims = json.loads(_b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise InteractionAssertionError("interaction assertion is invalid") from exc
    if not isinstance(claims, dict):
        raise InteractionAssertionError("interaction assertion is invalid")
    current = time.time() if now is None else now
    iat, exp = claims.get("iat"), claims.get("exp")
    if isinstance(iat, bool) or isinstance(exp, bool) or not isinstance(iat, int) or not isinstance(exp, int):
        raise InteractionAssertionError("interaction assertion times are invalid")
    if exp < current or iat > current + 30 or exp - iat > 300:
        raise InteractionAssertionError("interaction assertion is expired or invalid")
    exact = {
        "run_id": run_id,
        "action": action,
        "body_sha256": canonical_body_digest(body),
    }
    if role is not None:
        exact["role"] = role
    for key, value in exact.items():
        if not hmac.compare_digest(str(claims.get(key) or ""), str(value)):
            raise InteractionAssertionError(f"interaction assertion is not bound to {key}")
    for key in ("sub", "tenant_id", "authorization_epoch", "credential_id", "policy"):
        if not isinstance(claims.get(key), (str, int)) or isinstance(claims.get(key), bool) or not str(claims.get(key)):
            raise InteractionAssertionError(f"interaction assertion is missing {key}")
    return claims
