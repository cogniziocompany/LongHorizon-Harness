"""Admin settings screen and key restriction view (``/api/admin/*``), plus the
queue trio/capacity admin API (``/api/queue/trios``, ``/api/queue/capacity``)
so queue defaults change at runtime from the same settings store, and the
pending-task override edit (``PUT /api/queue/{queue_id}/model``) so one queued
task's model/roles/trio can change while it waits.

Who is an admin (all must hold, otherwise the request is refused):

1. the web bearer is configured and the request carries it (the existing web
   auth; on lh-harness.easybutt0n.ai Caddy adds it after SSO);
2. ``X-LH-Proxy-Auth`` equals ``LH_HARNESS_SETTINGS_PROXY_AUTH``, a secret
   only the SSO edge (Caddy) sends, so a LAN caller that holds the bearer
   cannot claim an identity;
3. the SSO identity (``X-Auth-Request-Email``, set by oauth2-proxy) is on
   ``LH_HARNESS_SETTINGS_ADMINS``.

Unset proxy secret or admin list = nobody is an admin (fail closed). Writes
also need ``X-Requested-With: lh-harness`` (no cross-site form can send it).
No route ever returns a secret value.
"""

from __future__ import annotations

import json
import hashlib
import hmac
import os
import socket
from typing import Any, Callable

from fastapi import Body, FastAPI, HTTPException, Query, Request

from ..settings_store import (
    CATALOG,
    SettingsError,
    SettingsRuntime,
    active_runtime,
    TRIO_ROLE_KEYS,
    is_secret_name,
    now_iso,
    spec_for,
    trio_role_overrides,
    upsert_queue_trio,
)
# LHH-QUEUE-PROTECT-DOCS (F3): the kimi (synthetic) and glm (cloud) trios are
# never modified at runtime.  Both REST ``PUT /api/queue/trios`` and the MCP
# ``set_trio_model`` tool refuse writes to a row whose ``name`` is on the
# project config's ``[queue].protected_trios`` list with HTTP 409, unless the
# request carries ``force: true`` AND the caller is verified as an SSO admin.
# Capacity edits on protected trios stay allowed (caps are operational).  The
# list is read live from the same project config the other queue keys come
# from.  See docs/queue.md "Protected trios".
from .. import queue as _queue_runtime  # the helper that exposes the resolved set + WIP

ADMIN_PROXY_HEADER = "x-lh-proxy-auth"
_KEEP_PEAK = object()  # PUT capacity: preserve the stored max_peak (legacy bare-int form)
IDENTITY_HEADER = "x-auth-request-email"
PREFERRED_USERNAME_HEADER = "x-forwarded-preferred-username"
USER_HEADER = "x-forwarded-user"


_HEADER_CHAIN = (IDENTITY_HEADER, PREFERRED_USERNAME_HEADER, USER_HEADER)


def _admin_identity(headers: Any) -> str | None:
    """Resolve the signed-in identity from the SSO header chain.

    oauth2-proxy sets ``X-Auth-Request-Email``.  Entra does not always
    publish a ``mail`` attribute for service/ai-dev accounts, so that
    header can be empty; in that case we fall back to
    ``X-Forwarded-Preferred-Username`` and then ``X-Forwarded-User``
    (the UPN / oid).  Comparison against ``LH_HARNESS_SETTINGS_ADMINS``
    is case-insensitive and happens in ``SettingsRuntime.is_admin``.
    """
    for name in _HEADER_CHAIN:
        value = (headers.get(name) or "").strip()
        if value:
            return value
    return None


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _is_set(name: str) -> bool:
    from ..caller_auth import captured_secret

    return bool(os.environ.get(name) or captured_secret(name))


def register_settings_routes(
    app: FastAPI,
    *,
    token: str | None,
    bearer_matches: Callable[[str | None, str | None], bool],
    settings_runtime: SettingsRuntime | None = None,
    caller_specs: dict[str, dict[str, Any]] | None = None,
    ask_runtime: Any = None,
    project_config_path: Any = None,
    model_fetch: Callable[[], dict[str, Any]] | None = None,
    protected_trios: tuple[str, ...] | None = None,
) -> None:
    def runtime() -> SettingsRuntime:
        return settings_runtime if settings_runtime is not None else active_runtime()

    def identity_check(request: Request) -> tuple[str | None, str | None]:
        """(identity, refusal reason). identity is None when not an admin."""
        rt = runtime()
        if not token:
            return None, "admin needs the web bearer to be configured on this node"
        if not bearer_matches(request.headers.get("authorization"), token):
            return None, "invalid or missing bearer token"
        if not rt.proxy_auth:
            return None, "admin is off: LH_HARNESS_SETTINGS_PROXY_AUTH is not set"
        supplied = request.headers.get(ADMIN_PROXY_HEADER) or ""
        if not supplied or not hmac.compare_digest(_digest(supplied), _digest(rt.proxy_auth)):
            return None, "not signed in through the SSO edge"
        identity = _admin_identity(request.headers)
        if not identity:
            return None, "no signed-in identity on the request"
        if not rt.admins:
            return identity, "admin is off: LH_HARNESS_SETTINGS_ADMINS is empty"
        if not rt.is_admin(identity):
            return identity, "this account is not on the admin list"
        return identity, None

    def require_admin(request: Request, *, write: bool = False) -> str:
        identity, reason = identity_check(request)
        if reason is not None:
            raise HTTPException(status_code=403, detail=reason)
        if write and request.headers.get("x-requested-with") != "lh-harness":
            raise HTTPException(status_code=403, detail="missing X-Requested-With: lh-harness")
        rt = runtime()
        if rt.store is None:
            raise HTTPException(status_code=503, detail=rt.error or "the settings store is not configured")
        assert identity is not None
        return identity

    # LHH-QUEUE-PROTECT-DOCS (F3): resolve the project config's
    # ``[queue].protected_trios`` set the way the launcher's
    # ``_refresh_queue_config`` does.  An explicit injection wins so tests
    # can pin a deterministic set without touching the on-disk config;
    # the file fallback keeps the rule live across edits.  The default
    # (kimi / glm / qwen) only applies when the project config EXISTS --
    # not when it is missing or empty.  A missing file means a deployment
    # that has not opted into the rule (the same convention [queue].observe
    # uses: no table = no behaviour change).
    def _resolve_protected_trios() -> tuple[str, ...]:
        if protected_trios is not None:
            return tuple(protected_trios)
        try:
            from ..config import load_run_defaults
            from pathlib import Path as _P

            payload = None
            if project_config_path is not None and _P(str(project_config_path)).is_file():
                payload = load_run_defaults(project_config_path)
            queue = payload.get("queue") if isinstance(payload, dict) else None
            if isinstance(queue, dict) and "protected_trios" in queue:
                configured = queue.get("protected_trios")
                if isinstance(configured, (list, tuple)):
                    return tuple(
                        dict.fromkeys(
                            item.strip() for item in configured if isinstance(item, str) and item.strip()
                        )
                    )
                return ()
        except Exception:  # noqa: BLE001 - a broken file must not silently disable the rule
            pass
        return ()

    def _refuse_if_protected(name: str, *, force: bool, actor: str) -> None:
        """Reject a write that targets a protected trio, unless the caller
        asked for ``force`` AND holds SSO-admin status (a verified identity
        on ``[settings_admins]``).  Capacity edits bypass this path: caps
        are operational, not modelling choices.  The check runs BEFORE any
        live-model validation so a refused request never reaches the
        provider, never writes the audit log, and never mutates the row.
        """

        protected = _resolve_protected_trios()
        if name not in protected:
            return
        if not force:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"trio {name!r} is protected: Paxton's standing rule "
                    f"prohibits model or agent changes to {name}"
                ),
            )
        # ``force=true`` is honoured only when the caller is an SSO admin
        # (the ``actor`` returned from ``require_admin`` IS the verified
        # admin identity -- non-admin callers were already 403'd there).
        # Belt-and-braces: require_admin raises 403 before this fires, so
        # reaching the override branch means the caller is an admin.
        # Documented in docs/queue.md "Protected trios".
        return

    def setting_row(name: str, stored: dict[str, dict[str, Any]], rt: SettingsRuntime) -> dict[str, Any]:
        spec = spec_for(name)
        meta = stored.get(name)
        secret = bool(meta["secret"]) if meta else spec.secret
        row: dict[str, Any] = {
            "name": name,
            "description": spec.description,
            "secret": secret,
            "restart_required": spec.restart,
            "catalog": name in {s.name for s in CATALOG},
        }
        if meta is not None:
            row.update({k: meta[k] for k in ("length", "note", "expires_at", "updated_at", "updated_by")})
            row["set"] = True
            row["source"] = "db"
            row["changed_since_start"] = meta["updated_at"] >= rt.started_at
            if not secret:
                row["value"] = meta.get("value")
        else:
            present = _is_set(name)
            row["set"] = present
            row["source"] = "env" if present else "unset"
            row["changed_since_start"] = False
            if present and not secret:
                row["value"] = os.environ.get(name, "")
        return row

    @app.get("/api/admin/whoami")
    def admin_whoami(request: Request) -> dict[str, Any]:
        identity, reason = identity_check(request)
        rt = runtime()
        return {
            "identity": identity,
            "is_admin": reason is None and rt.store is not None,
            "reason": reason or (None if rt.store is not None else rt.error),
            "store": {"configured": rt.store is not None, "error": rt.error, "path": str(rt.store.path) if rt.store else None},
            "started_at": rt.started_at,
        }

    @app.get("/api/admin/settings")
    def admin_list_settings(request: Request) -> dict[str, Any]:
        require_admin(request)
        rt = runtime()
        assert rt.store is not None
        stored = {m["name"]: m for m in rt.store.list_metadata()}
        names = sorted({s.name for s in CATALOG} | set(stored))
        return {"settings": [setting_row(n, stored, rt) for n in names], "started_at": rt.started_at}

    @app.put("/api/admin/settings/{name}")
    def admin_put_setting(name: str, request: Request, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        actor = require_admin(request, write=True)
        rt = runtime()
        assert rt.store is not None
        unknown = set(body) - {"value", "secret", "note", "expires_at"}
        if unknown:
            raise HTTPException(status_code=400, detail="unknown field(s): " + ", ".join(sorted(unknown)))
        try:
            if "value" in body:
                secret = body.get("secret")
                if secret is not None and not isinstance(secret, bool):
                    raise SettingsError("secret must be true or false", 400)
                rt.store.set(name, body["value"], actor, secret=secret, note=body.get("note"), expires_at=body.get("expires_at"))
            else:
                rt.store.update_meta(name, actor, note=body.get("note"), expires_at=body.get("expires_at"))
            stored = {m["name"]: m for m in rt.store.list_metadata()}
        except SettingsError as exc:
            raise HTTPException(status_code=exc.code, detail=str(exc)) from None
        row = setting_row(name, stored, rt)
        return {"ok": True, "setting": row, "restart_required": row["restart_required"]}

    @app.delete("/api/admin/settings/{name}")
    def admin_delete_setting(name: str, request: Request) -> dict[str, Any]:
        actor = require_admin(request, write=True)
        rt = runtime()
        assert rt.store is not None
        try:
            rt.store.unset(name, actor)
        except SettingsError as exc:
            raise HTTPException(status_code=exc.code, detail=str(exc)) from None
        return {"ok": True, "name": name, "restart_required": spec_for(name).restart}

    @app.get("/api/admin/audit")
    def admin_audit(request: Request, limit: int = Query(default=200, ge=1, le=1000)) -> dict[str, Any]:
        require_admin(request)
        rt = runtime()
        assert rt.store is not None
        return {"entries": rt.store.audit(limit)}

    @app.get("/api/admin/keys")
    def admin_keys(request: Request) -> dict[str, Any]:
        require_admin(request)
        rt = runtime()
        assert rt.store is not None
        return {"node": _node_name(), "keys": key_restrictions(rt, caller_specs, ask_runtime, project_config_path)}

    # ------------------------------------------- queue trios + capacity (R4) --
    # LHH-QUEUE-HOTRELOAD: trio defaults and per-trio capacity live in the
    # settings DB (queue_trio/queue_capacity) and the launcher re-reads them on
    # every scheduling pass, so a PUT below takes effect on the next launch
    # with no restart and no redeploy.  The store remains the sole accessor --
    # the writes below go through its ``_db``/``_audit`` machinery, never a
    # second connection or parallel store.
    def live_models() -> dict[str, Any] | None:
        """The backend's live model list per agent, or ``None`` when unknown.

        ``model_fetch`` is injectable so tests never touch the network; the
        default re-reads model discovery (briefly cached; POST
        /api/models/refresh forces a re-fetch) on every validation.
        """

        fetch = model_fetch
        if fetch is None:
            from ..model_catalog import discover_model_catalog
            fetch = discover_model_catalog
        try:
            catalogue = fetch()
        except Exception:  # noqa: BLE001 - a broken provider must refuse writes, not 500
            return None
        if not isinstance(catalogue, dict):
            return None
        models = catalogue.get("models")
        return models if isinstance(models, dict) else None

    @app.get("/api/queue/trios")
    def get_queue_trios(request: Request) -> dict[str, Any]:
        require_admin(request)
        rt = runtime()
        assert rt.store is not None
        return {"trios": rt.store.queue_trios()}

    @app.put("/api/queue/trios")
    def put_queue_trios(request: Request, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        actor = require_admin(request, write=True)
        rt = runtime()
        assert rt.store is not None
        store = rt.store
        # LHH-QUEUE-PROTECT-DOCS (F3): the protected check runs BEFORE
        # ``live_models()`` and the row validation -- a refused request
        # never reaches the provider, never writes the audit log, and
        # never mutates a row.  The ``force`` flag (boolean) opts in to the
        # override branch and is honoured ONLY when ``actor`` came from
        # ``require_admin`` (which requires the SSO admin list).
        unknown = set(body) - {"trios", "force"}
        if unknown:
            raise HTTPException(status_code=400, detail="unknown field(s): " + ", ".join(sorted(unknown)))
        force = bool(body.get("force")) if isinstance(body.get("force"), (bool, int)) else False
        rows = body.get("trios")
        if not isinstance(rows, list):
            raise HTTPException(status_code=422, detail="trios must be a list of trio objects")
        # The protected check is per row: each row's name is checked, and a
        # refusal on any row is all-or-nothing (mirrors the rest of the
        # validation in this route).  Capacity edits go through the
        # separate ``put_queue_capacity`` route, which does NOT apply the
        # protected rule.
        protected_set = _resolve_protected_trios()
        if protected_set:
            refused_protected: list[str] = []
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                name = raw.get("name")
                if isinstance(name, str) and name.strip() in protected_set:
                    refused_protected.append(name.strip())
            if refused_protected and not force:
                names = ", ".join(repr(n) for n in refused_protected)
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"trio(s) {names} are protected: Paxton's standing rule "
                        "prohibits model or agent changes to protected trios"
                    ),
                )
            # When force is set we still surface which rows are protected so
            # the audit row (and the response) make the override explicit.
        models = live_models()
        if models is None:
            raise HTTPException(status_code=503, detail="the live model list is unavailable; refusing to write unvalidated trios")
        # Every row is validated against the live list BEFORE any write: a
        # rejected request leaves the DB and the audit log untouched.
        cleaned: list[dict[str, Any]] = []
        errors: list[str] = []
        seen: set[str] = set()
        for raw in rows:
            row, error = _clean_trio_row(raw, models)
            if error is not None:
                errors.append(error)
                continue
            assert row is not None
            if row["name"] in seen:
                errors.append(f"duplicate trio name {row['name']!r}")
                continue
            seen.add(row["name"])
            cleaned.append(row)
        if errors:
            raise HTTPException(status_code=422, detail="; ".join(errors))
        with store._db() as db:
            for row in cleaned:
                old = db.execute("SELECT * FROM queue_trio WHERE name = ?", (row["name"],)).fetchone()
                upsert_queue_trio(db, row, actor, roles=row.get("roles"))
                before = (
                    None
                    if old is None
                    else {
                        "agent": old["agent"],
                        "model": old["model"],
                        "mcp_profile": old["mcp_profile"],
                        "enabled": bool(old["enabled"]),
                    }
                )
                after = {
                    "agent": row["agent"],
                    "model": row["model"],
                    "mcp_profile": row["mcp_profile"],
                    "enabled": bool(row["enabled"]),
                }
                if row.get("roles") is not None:
                    before = None if before is None else {**before, "roles": _old_roles(old)}
                    after["roles"] = dict(row["roles"])
                if before != after:
                    audit_detail: dict[str, Any] = {"before": before, "after": after}
                    # Mark the audit row when the override was used -- the
                    # SSO admin identity in ``actor`` is the actual proof,
                    # but a structured flag makes the override searchable
                    # in ``GET /api/admin/audit``.
                    if row["name"] in protected_set and force:
                        audit_detail["protected_override"] = True
                    store._audit(db, actor, "set", f"queue_trio:{row['name']}", **audit_detail)
        result: dict[str, Any] = {"ok": True, "trios": store.queue_trios()}
        if protected_set and force:
            overridden = sorted({row["name"] for row in cleaned if row["name"] in protected_set})
            if overridden:
                result["protected_override"] = overridden
        return result

    @app.get("/api/queue/capacity")
    def get_queue_capacity(request: Request) -> dict[str, Any]:
        require_admin(request)
        rt = runtime()
        assert rt.store is not None
        rows = rt.store.queue_capacity_rows()
        return {
            "capacity": {name: row["max"] for name, row in rows.items()},
            "max_peak": {name: row["max_peak"] for name, row in rows.items()},
            "peak_window": "Mon-Fri 06:00-10:00 UTC",
        }

    @app.put("/api/queue/capacity")
    def put_queue_capacity(request: Request, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        actor = require_admin(request, write=True)
        rt = runtime()
        assert rt.store is not None
        store = rt.store
        unknown = set(body) - {"capacity"}
        if unknown:
            raise HTTPException(status_code=400, detail="unknown field(s): " + ", ".join(sorted(unknown)))
        caps = body.get("capacity")
        if not isinstance(caps, dict):
            raise HTTPException(status_code=422, detail="capacity must be an object of trio name -> max_concurrent or {max, max_peak}")
        known_trios = {row["name"] for row in store.queue_trios()}
        keep = _KEEP_PEAK  # sentinel: preserve the stored max_peak
        updates: dict[str, dict[str, int | None]] = {}
        errors = []
        for key, raw in caps.items():
            trio = key.strip() if isinstance(key, str) else ""
            if not trio:
                errors.append("capacity keys must be trio names (non-empty strings)")
                continue
            if trio not in known_trios:
                errors.append(f"capacity for unknown trio {trio!r}")
                continue
            # Legacy form: a bare int = the all-day max, peak untouched.
            # Rich form: {"max": int, "max_peak": int|null} (null clears the
            # peak override). The schedule layer resolves which applies when.
            if isinstance(raw, bool) or isinstance(raw, int):
                if isinstance(raw, bool) or raw < 0:
                    errors.append(f"capacity for {trio!r} must be a non-negative integer")
                    continue
                # Legacy bare int: the all-day max only; an existing peak
                # override is PRESERVED (cleared only via the rich form's
                # explicit max_peak: null).
                parsed = {"max": raw, "max_peak": _KEEP_PEAK}
            elif isinstance(raw, dict) and set(raw) <= {"max", "max_peak"}:
                base_val = raw.get("max")
                peak_val = raw.get("max_peak")
                if isinstance(base_val, bool) or not isinstance(base_val, int) or base_val < 0:
                    errors.append(f"capacity.max for {trio!r} must be a non-negative integer")
                    continue
                if peak_val is not None and (isinstance(peak_val, bool) or not isinstance(peak_val, int) or peak_val < 0):
                    errors.append(f"capacity.max_peak for {trio!r} must be a non-negative integer or null")
                    continue
                parsed = {"max": base_val, "max_peak": peak_val}
            else:
                errors.append(f"capacity for {trio!r} must be a non-negative integer or {{max, max_peak}}")
                continue
            updates[trio] = parsed
        if errors:
            raise HTTPException(status_code=422, detail="; ".join(errors))
        with store._db() as db:
            for trio, value in updates.items():
                old = db.execute(
                    "SELECT max_concurrent, max_peak FROM queue_capacity WHERE trio = ?", (trio,)
                ).fetchone()
                new_peak = value["max_peak"]
                if new_peak is keep:
                    new_peak = None if old is None else old["max_peak"]
                db.execute(
                    "INSERT INTO queue_capacity(trio, max_concurrent, max_peak) VALUES (?, ?, ?) "
                    "ON CONFLICT(trio) DO UPDATE SET max_concurrent=excluded.max_concurrent, max_peak=excluded.max_peak",
                    (trio, value["max"], new_peak),
                )
                before = None
                if old is not None:
                    before = {"max": int(old["max_concurrent"]), "max_peak": None if old["max_peak"] is None else int(old["max_peak"])}
                after = {"max": value["max"], "max_peak": new_peak}
                if before != after:
                    store._audit(db, actor, "set", f"queue_capacity:{trio}", before=before, after=after)
        rows = store.queue_capacity_rows()
        return {
            "ok": True,
            "capacity": {name: row["max"] for name, row in rows.items()},
            "max_peak": {name: row["max_peak"] for name, row in rows.items()},
        }

    # ----------------------------- pending-task model/roles edit (R7) --
    # LHH-QUEUE-HOTRELOAD (Paxton 2026-10-11): a queued task's model (and its
    # trio/roles) stays EDITABLE while the entry is pending.  The edit below
    # accepts the R6 per-task overrides with the exact semantics enqueue
    # applies -- ``model`` is one id validated against the backend's LIVE
    # model list (unknown -> 422, provider down -> 503, nothing written), and
    # ``roles`` is a STRING trio alias (same validation as the legacy enqueue
    # alias, so the trio is editable too) or an OBJECT of per-role
    # configuration validated with create_fleet_run's own normalizer.
    # ``null`` clears: ``model`` null/blank drops the override; ``roles``
    # null drops the per-role configuration (the trio is only ever touched
    # through the explicit string form).  The persisted entry is what the R3
    # launcher re-reads on every scheduling pass, so the edit applies to the
    # next launch with no restart.  Once the entry is no longer pending the
    # resolved model belongs to the run and is LOCKED: the edit is refused
    # with 409 (a state refusal, not a validation error) and a trio-default
    # change can never alter a running run.  Every accepted change writes one
    # settings-audit row (R4/R5 convention: ``_db`` + ``_audit``, before/
    # after keyed to the verified admin identity).  The PgQueueStore
    # override-persistence gap tracked for the gate applies here exactly as
    # it does to R6 enqueue; the file store (CT110's backend) persists fully.
    @app.put("/api/queue/{queue_id}/model")
    def put_queue_entry_model(
        queue_id: str,
        request: Request,
        body: dict[str, Any] = Body(default_factory=dict),
    ) -> dict[str, Any]:
        actor = require_admin(request, write=True)
        rt = runtime()
        assert rt.store is not None
        queue_store = getattr(request.app.state, "queue_store", None)
        if queue_store is None:
            raise HTTPException(status_code=501, detail="queue requires a configured runs root")
        unknown = set(body) - {"model", "roles"}
        if unknown:
            raise HTTPException(status_code=400, detail="unknown field(s): " + ", ".join(sorted(unknown)))
        if "model" not in body and "roles" not in body:
            raise HTTPException(status_code=422, detail="provide model and/or roles to edit")
        try:
            entry = queue_store.get(queue_id)
        except ValueError:  # malformed queue id: the store refuses the path
            entry = None
        if entry is None:
            raise HTTPException(status_code=404, detail="queue entry not found")
        if entry.status != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"cannot edit model/roles for status {entry.status}",
            )
        from .. import queue as _q  # lazy: the R6 override validators live with the entry semantics

        # Every supplied value is validated BEFORE the entry is mutated, so a
        # refused request leaves the entry and the audit log untouched.  The
        # R6 helpers raise ValueError (422 here) and the 503 ``HTTPException``
        # for a provider that is down, which FastAPI answers directly -- the
        # R4/R5/R6 taxonomy, unchanged.
        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        try:
            if "model" in body:
                cleaned_model = _q._validate_model_override(body.get("model"))
                if cleaned_model is not None:
                    _q._validate_model_live(cleaned_model, model_fetch)
                before["model"] = entry.model
                after["model"] = cleaned_model
            if "roles" in body:
                raw_roles = body.get("roles")
                if raw_roles is None:
                    before["roles"] = entry.roles
                    after["roles"] = None
                elif isinstance(raw_roles, str):
                    before["trio"] = entry.trio
                    after["trio"] = _q._validate_trio(raw_roles)
                elif isinstance(raw_roles, dict):
                    before["roles"] = entry.roles
                    after["roles"] = _q._validate_roles_config(raw_roles)
                else:
                    raise HTTPException(
                        status_code=422,
                        detail="roles must be a string trio alias or an object of per-role configuration",
                    )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        changed = before != after
        if changed:
            for key, value in after.items():
                setattr(entry, key, value)
            queue_store.update(entry)
            with rt.store._db() as db:
                rt.store._audit(db, actor, "edit", f"queue_entry:{queue_id}", before=before, after=after)
        return {
            "ok": True,
            "queue_id": queue_id,
            "status": entry.status,
            "changed": changed,
            "model": entry.model,
            "roles": entry.roles,
            "trio": entry.trio,
        }

    # The server registers its ``/api/queue/{queue_id}`` capture before this
    # function runs, and Starlette resolves routes in registration order (the
    # reason server.py places ``/api/queue/config`` before that capture).
    # Registering here only appends, so the exact /api/queue/trios and
    # /api/queue/capacity paths would be shadowed by the parameter route;
    # move them ahead of the first capture of that shape so they are
    # reachable.  Server.py itself stays untouched.
    _rise_exact_queue_routes(app)


_TRIO_ROW_FIELDS = {"name", "agent", "model", "mcp_profile", "enabled"} | set(TRIO_ROLE_KEYS)
_NAME_LIMIT = 64


def _old_roles(old: Any) -> dict[str, str]:
    """The stored per-role overrides of a fetched queue_trio row (for audit)."""

    try:
        raw = old["role_models"]
    except (IndexError, KeyError, TypeError):
        return {}
    try:
        return trio_role_overrides(json.loads(raw)) if raw else {}
    except (TypeError, ValueError):
        return {}


def _clean_trio_row(raw: Any, models_by_agent: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Validate one queue-trio row; ``(row, None)`` when clean, else ``(None, error)``.

    A trio's model must be on the LIVE model list of its agent -- the source
    of truth the workbench dropdowns are drawn from -- so an admin cannot
    schedule work on a model the backend cannot actually route to.  ``model``
    and ``mcp_profile`` may be null (the launcher then falls back per key to
    config.toml), matching what the one-time seed writes.
    """

    if not isinstance(raw, dict):
        return None, "each trio row must be an object"
    unknown = set(raw) - _TRIO_ROW_FIELDS
    if unknown:
        return None, "unknown field(s): " + ", ".join(sorted(unknown))
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > _NAME_LIMIT:
        return None, f"trio name must be a non-empty string of at most {_NAME_LIMIT} characters"
    trio = name.strip()
    agent = raw.get("agent")
    if not isinstance(agent, str) or not agent.strip():
        return None, f"trio {trio!r}: agent must be a non-empty string"
    agent = agent.strip()
    if agent not in models_by_agent:
        return None, f"trio {trio!r}: unknown agent {agent!r}"
    model = raw.get("model")
    if model is not None:
        if not isinstance(model, str):
            return None, f"trio {trio!r}: model must be a string or null"
        model = model.strip() or None
    if model is not None:
        entries = models_by_agent.get(agent) or []
        known = {str(entry.get("id")) for entry in entries if isinstance(entry, dict)}
        if model not in known:
            return None, f"trio {trio!r}: unknown model {model!r} for agent {agent!r} in the live model list"
    profile = raw.get("mcp_profile")
    if profile is not None:
        if not isinstance(profile, str):
            return None, f"trio {trio!r}: mcp_profile must be a string or null"
        profile = profile.strip() or None
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        return None, f"trio {trio!r}: enabled must be true or false"
    row = {
        "name": trio,
        "agent": agent,
        "model": model,
        "mcp_profile": profile,
        "enabled": 1 if enabled else 0,
    }
    # Per-role overrides: validated like the trio-wide pair.  ``roles`` is
    # None when the row names no role key at all (the stored set is kept),
    # else the full replacement set (null/blank values drop that key).
    if any(key in raw for key in TRIO_ROLE_KEYS):
        roles: dict[str, str] = {}
        for role in ("manager", "executor", "auditor"):
            role_agent = raw.get(f"{role}_agent")
            if role_agent is not None and not isinstance(role_agent, str):
                return None, f"trio {trio!r}: {role}_agent must be a string or null"
            role_agent = (role_agent or "").strip() or None
            if role_agent is not None and role_agent not in models_by_agent:
                return None, f"trio {trio!r}: unknown {role}_agent {role_agent!r}"
            role_model = raw.get(f"{role}_model")
            if role_model is not None and not isinstance(role_model, str):
                return None, f"trio {trio!r}: {role}_model must be a string or null"
            role_model = (role_model or "").strip() or None
            if role_model is not None:
                entries = models_by_agent.get(role_agent or agent) or []
                known = {str(entry.get("id")) for entry in entries if isinstance(entry, dict)}
                if role_model not in known:
                    return None, (
                        f"trio {trio!r}: unknown {role}_model {role_model!r} for agent "
                        f"{(role_agent or agent)!r} in the live model list"
                    )
            if role_agent:
                roles[f"{role}_agent"] = role_agent
            if role_model:
                roles[f"{role}_model"] = role_model
        row["roles"] = roles
    return row, None


_QUEUE_EXACT_PATHS = {"/api/queue/trios", "/api/queue/capacity"}


def _rise_exact_queue_routes(app: FastAPI) -> None:
    """Reposition the exact /api/queue/trios and /api/queue/capacity routes.

    Server.py registers ``/api/queue/{queue_id}`` before register_settings_routes
    runs, and Starlette scans routes in registration order, so an exact path
    appended here would never be reached (the parameter route would answer
    ``queue_id="trios"`` with a 404).  Move only these routes ahead of the
    first parameter capture of that shape; every other route keeps its order.
    """

    routes = app.router.routes
    moved = [route for route in routes if getattr(route, "path", None) in _QUEUE_EXACT_PATHS]
    if not moved:
        return
    for route in moved:
        routes.remove(route)
    anchor = next(
        (i for i, route in enumerate(routes) if getattr(route, "path", None) == "/api/queue/{queue_id}"),
        len(routes),
    )
    for offset, route in enumerate(moved):
        routes.insert(anchor + offset, route)


def _node_name() -> str:
    return os.environ.get("LH_HARNESS_FLEET_NODE") or socket.gethostname()


def _servers(servers: Any) -> list[str] | str:
    """A profile's gateway servers; None means every server the key can reach."""
    return "all servers the gateway key allows" if servers is None else list(servers)


def _profiles(project_config_path: Any) -> list[dict[str, Any]]:
    from .. import mcp_profiles as mp

    out = [
        {"name": name, "servers": _servers(spec["servers"]), "read_only": spec["read_only"], "source": "built-in"}
        for name, spec in mp._BUILTINS.items()
    ]
    seen = set(mp._BUILTINS)
    for label, loader in (("user", lambda: mp._load_user_profiles()), ("project", lambda: mp._load_project_profiles(project_config_path))):
        try:
            profiles = loader()
        except Exception:  # noqa: BLE001 - a broken profile file must not break the view
            continue
        for name, profile in profiles.items():
            if name in seen:
                continue
            seen.add(name)
            out.append({"name": name, "servers": _servers(profile.servers), "read_only": profile.read_only, "source": label})
    return out


def key_restrictions(
    rt: SettingsRuntime,
    caller_specs: dict[str, dict[str, Any]] | None,
    ask_runtime: Any,
    project_config_path: Any = None,
) -> list[dict[str, Any]]:
    """Every key/caller this node knows, with its restrictions. Never a value."""
    from ..caller_auth import caller_configs_or_defaults

    stored = {m["name"]: m for m in rt.store.list_metadata()} if rt.store is not None else {}
    scoping_on = caller_specs is not None
    callers = caller_specs if scoping_on else caller_configs_or_defaults(None)
    grants = dict(getattr(ask_runtime, "grants", {}) or {})
    names = [s.name for s in CATALOG if s.kind == "credential"]
    names += sorted(n for n, m in stored.items() if m["secret"] and n not in names)
    out = []
    for name in names:
        spec = spec_for(name)
        meta = stored.get(name)
        entry: dict[str, Any] = {
            "name": name,
            "description": spec.description,
            "set": bool(meta) or _is_set(name),
            "source": "db" if meta else ("env" if _is_set(name) else "unset"),
            "expires_at": meta.get("expires_at") if meta else None,
            "note": meta.get("note") if meta else "",
            "node": _node_name(),
        }
        kind = spec.restriction
        if kind == "bearer":
            entry["restrictions"] = {
                "grants": "every /api route and the /mcp endpoint on this node",
                "per_caller_scoping": "on" if scoping_on else "off (bearer-only for non-ask tools)",
            }
        elif kind.startswith("caller:"):
            caller = kind.split(":", 1)[1]
            cspec = (callers or {}).get(caller, {})
            entry["restrictions"] = {
                "tools": list(cspec.get("tools") or []),
                "tools_enforced": scoping_on,
                "rest_run_control": bool(cspec.get("rest_run_control", False)),
                "max_entries_per_hour": cspec.get("max_entries_per_hour"),
                "max_rounds_clamp": cspec.get("max_rounds_clamp"),
                "ask_scopes": list(grants.get(caller, [])),
            }
        elif kind == "gateway":
            entry["restrictions"] = {
                "mcp_profiles": _profiles(project_config_path),
                "enforced_by": "the LiteLLM gateway (the key's own server list); record it in the note",
            }
        else:
            entry["restrictions"] = {"enforced_by": "the issuer; record the key's scope in the note"}
        out.append(entry)
    return out


__all__ = ["register_settings_routes", "key_restrictions", "_admin_identity"]
