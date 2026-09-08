"""FastAPI REST/WebSocket server for the Web workbench."""

from __future__ import annotations

import asyncio
import base64
import binascii
import gzip
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from ..dashboard.state import DashboardState
from ..launcher import Launcher
from ..mcp_profiles import _default_profile_for_role, gateway_configured, list_available_profiles
from ..mcp_tools import dispatch as _dispatch_mcp_tool, normalize_request_token, tools_manifest
from ..model_catalog import discover_model_catalog
from ..supervisor.service import IdempotencyConflict, RunSupervisor
from ..supervisor.lifecycle import TERMINAL_STATUSES, canonical_lifecycle_status, resume_epoch
from ..supervisor.control_bus import CommandConflict, RevisionConflict
from ..fleet import get_reporter
from ..queue import QueueStore, default_queue_config, queue_config_from_config
from ..types import DEFAULT_CODEX_MODEL, DEFAULT_MAX_ROUNDS, MAX_ROUNDS
from ..utils.agent_cli import resolve_codex_binary, resolve_dsh_binary, resolve_opencode_binary
from ..utils.run_boundary import safe_run_control, safe_run_dir, safe_run_logs, safe_run_role, safe_run_rounds
from .events import EventTailer

# The standalone workbench has no project ``config.toml`` of its own (one
# server can host runs across many workspaces), so the New Task form's
# starting agent/model has always been a hardcoded "codex" literal here,
# ignoring whatever the deployment actually wants to route to. Let an
# operator set the workbench-wide default via environment instead, same way
# --base-url/--api-key already reach the spawned agent CLI through the
# process environment rather than a per-workspace file.
_WEB_DEFAULT_AGENT = os.environ.get("LH_HARNESS_WEB_DEFAULT_AGENT") or "codex"
_WEB_DEFAULT_MODEL = os.environ.get("LH_HARNESS_WEB_DEFAULT_MODEL") or DEFAULT_CODEX_MODEL
_WEB_DEFAULT_MANAGER_MODEL = (
    os.environ.get("LH_HARNESS_WEB_DEFAULT_MANAGER_MODEL") or _WEB_DEFAULT_MODEL
)
_WEB_DEFAULT_AUDITOR_MODEL = (
    os.environ.get("LH_HARNESS_WEB_DEFAULT_AUDITOR_MODEL") or _WEB_DEFAULT_MODEL
)
from .protocol import build_meta
from .snapshot import _provenance, build_run_summary, build_snapshot

# Vite builds directly into this directory, so a source checkout and an
# installed wheel resolve the same path.  It is absent until the frontend is
# built; the API then serves JSON only.
_STATIC_DIR = Path(__file__).resolve().parents[1] / "_frontend" / "web" / "dist"
_DASHBOARD_MIME_TYPES = {
    ".js": "application/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
}

_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_MAX_CONTROL_BODY_BYTES = 1 * 1024 * 1024

# Agent-produced artifacts are untrusted.  Only a small, explicit raster
# allow-list is rendered in the dashboard origin; everything else is a
# download (including SVG, PDF and browser document formats).  Do not derive
# this decision from ``mimetypes.guess_type`` alone: several platforms report
# SVG/XHTML as an executable document type.
_INLINE_RASTER_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
}
_TEXT_ARTIFACT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".json", ".jsonl", ".log", ".csv", ".tsv",
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".css", ".scss",
    ".html", ".htm", ".xhtml", ".xht", ".xml", ".svg", ".svgz", ".yaml", ".yml",
    ".toml", ".ini", ".sh", ".zsh", ".bash", ".diff", ".patch", ".rst",
}


def _artifact_content_disposition(disposition: str, name: str) -> str:
    """Build a header-safe, Unicode-preserving Content-Disposition value.

    ``name`` comes from an agent-controlled directory entry.  A quoted raw
    filename is not sufficient: quotes/newlines can corrupt the header and
    non-Latin names cannot be encoded by the Latin-1 ASGI header transport.
    Keep a conservative ASCII fallback for old clients and add RFC 5987's
    ``filename*`` for clients that understand UTF-8.
    """

    original = str(name or "artifact").replace("\r", "_").replace("\n", "_")
    suffix = Path(original).suffix
    safe_suffix = suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,16}", suffix) else ""
    stem_source = original[: -len(suffix)] if suffix else original
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem_source).strip("._-") or "artifact"
    fallback = f"{safe_stem[: max(1, 120 - len(safe_suffix))]}{safe_suffix}"
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{quote(original, safe='')}"

# Native browser WebSocket clients cannot set an ``Authorization`` header.
# Passing the long-lived bearer in ``?token=`` leaks it into access logs,
# reverse-proxy traces, browser history, and copied URLs.  Built-in clients use
# two URL-safe subprotocols instead: a public marker plus a base64url-encoded
# token.  The server echoes only the marker during the handshake.
_WS_AUTH_MARKER = "lh-harness-auth.v1"
_WS_AUTH_TOKEN_PREFIX = "lh-harness-token."


def _is_loopback_host(host: str) -> bool:
    value = str(host or "").strip().lower().strip("[]")
    if value in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _request_hostname(host_header: str) -> str:
    """Return the hostname from a Host header, ignoring an optional port."""

    value = str(host_header or "").strip()
    if not value:
        return ""
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return ""
        return value[1:end]
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0]
    return value


def _host_header_allowed(host_header: str, bind_host: str) -> bool:
    hostname = _request_hostname(host_header)
    if not hostname:
        return False
    if _is_loopback_host(hostname):
        return True
    configured = str(bind_host or "").strip().lower().strip("[]")
    return bool(configured) and hostname.lower() == configured


def _is_json_content_type(value: str | None) -> bool:
    media = (value or "").split(";", 1)[0].strip().lower()
    return media == "application/json"


async def _cache_bounded_request_body(request: Request, limit: int) -> bool:
    """Buffer at most ``limit`` bytes and replay them to the route handler.

    ``Request.body()`` consumes the complete ASGI stream before its caller can
    inspect the size.  Reading the documented streaming interface lets the
    control plane reject an undeclared/chunked oversized body as soon as it
    crosses the limit.  Starlette's middleware request wrapper replays the
    cached body to ``call_next``.
    """

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            return False
        body.extend(chunk)
    request._body = bytes(body)
    return True


def _configured_token(explicit: str | None) -> str | None:
    value = explicit if explicit is not None else os.environ.get("LH_HARNESS_WEB_TOKEN")
    value = str(value or "").strip()
    return value or None


def _bearer_matches(value: str | None, token: str | None) -> bool:
    if token is None:
        return True
    if not value or not value.startswith("Bearer "):
        return False
    supplied = value.removeprefix("Bearer ").strip()
    return bool(supplied) and hmac.compare_digest(supplied, token)


def _decode_ws_token_protocol(value: str) -> str | None:
    """Decode one bounded, URL-safe WebSocket auth protocol value."""

    if not value.startswith(_WS_AUTH_TOKEN_PREFIX):
        return None
    encoded = value[len(_WS_AUTH_TOKEN_PREFIX) :]
    if not encoded or len(encoded) > 2048:
        return None
    # Base64url without padding is the only representation emitted by the
    # frontends.  Restrict the alphabet before decoding so malformed protocol
    # headers cannot be interpreted ambiguously.
    if any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for char in encoded):
        return None
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
    return decoded or None


def _websocket_auth(websocket: WebSocket, token: str | None) -> tuple[bool, str | None]:
    """Authenticate a WebSocket and return the subprotocol to echo."""

    if token is None:
        return True, None
    if _bearer_matches(websocket.headers.get("authorization"), token):
        return True, None
    protocols = {
        item.strip()
        for item in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if item.strip()
    }
    if _WS_AUTH_MARKER not in protocols:
        # Deliberately do not accept query-string credentials.  A query token
        # is a bearer credential with a much wider leak surface than a header.
        return False, None
    for protocol in protocols:
        supplied = _decode_ws_token_protocol(protocol)
        if supplied is not None and hmac.compare_digest(supplied, token):
            return True, _WS_AUTH_MARKER
    return False, None


def _origin_allowed(origin: str | None, host: str, allowed_origins: set[str]) -> bool:
    # Non-browser clients generally omit Origin.  An explicit Origin must be
    # checked because browsers allow cross-site WebSocket upgrades without
    # applying CORS rules.
    if not origin:
        return True
    if origin in allowed_origins:
        return True
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"}:
        return False
    # Compare the complete authority, not only the hostname: accepting an
    # arbitrary port on the same machine still permits a cross-site browser
    # to read the stream.
    return parsed.netloc.lower().rstrip("/") == host.lower().rstrip("/")


class StateRegistry:
    """Resolve run ids without letting API paths escape the configured root."""

    def __init__(
        self,
        *,
        state: DashboardState,
        runs_root: str | Path | None = None,
        run_id: str | None = None,
        supervisor: RunSupervisor | None = None,
    ) -> None:
        self.base_state = state
        self.runs_root = Path(runs_root).expanduser().resolve() if runs_root else None
        # A multi-run registry must not invent a ``local`` run merely because
        # its base state has not been selected yet.  The old sentinel caused a
        # read of /api/runs/local/snapshot to create durable control metadata
        # for a run that never existed.
        derived_run_id = state.current_run_id if run_id is None else ""
        self.base_run_id = run_id or derived_run_id or ("local" if self.runs_root is None else "")
        self.supervisor = supervisor
        self._states: dict[str, DashboardState] = {self.base_run_id: state} if self.base_run_id else {}

    def _refresh_control_capability(self, run_id: str, state: DashboardState) -> DashboardState:
        """Keep a cached DashboardState aligned with the live supervisor.

        A state can be cached while a newly-created worker is still in its
        reservation window.  Persisting that initial ``False`` forever makes
        later instructions and approvals fail even after the worker is live.
        The supervisor remains the authority, so refresh the mutable adapter
        flag every time the state is attached to an API request.
        """

        if self.supervisor is not None:
            try:
                state.control_enabled = bool(self.supervisor.can_control(run_id))
            except (OSError, ValueError):
                state.control_enabled = False
        return state

    def _run_paths(self, run_id: str) -> tuple[Path, Path] | None:
        """Validate one run boundary before a state can read from it.

        A control-only reservation is valid before its worker creates
        ``lh_harness``. Existing result/control children, however, must be
        real directories belonging to this exact run; a link to a sibling run
        is rejected even though its target remains below ``runs_root``.
        """

        root = self.runs_root
        if root is None:
            return None
        run_dir = safe_run_dir(root, run_id)
        if run_dir is None or not run_dir.is_dir():
            return None
        log_dir = safe_run_logs(root, run_dir, allow_missing=True)
        control_dir = safe_run_control(root, run_dir, allow_missing=True)
        rounds_dir = safe_run_rounds(root, run_dir, allow_missing=True)
        role_dir = safe_run_role(root, run_dir, allow_missing=True)
        if log_dir is None or control_dir is None or rounds_dir is None or role_dir is None:
            return None
        if not role_dir.is_dir() and not control_dir.is_dir():
            return None
        return run_dir, log_dir

    def state_for(self, run_id: str) -> DashboardState | None:
        if not _safe_run_id(run_id):
            return None
        # An embedded dashboard is intentionally single-run.  Reject foreign
        # ids before touching the filesystem so direct API paths cannot inspect
        # or control another worker through the attached supervisor.
        if self.supervisor is not None and bool(getattr(self.supervisor, "attached_only", False)):
            attached_run_id = getattr(self.supervisor, "attached_run_id", None)
            if not attached_run_id or run_id != attached_run_id:
                return None
        if run_id in self._states:
            cached = self._states[run_id]
            if self.runs_root is not None:
                # The base DashboardState may have auto-selected a run during
                # construction. Revalidate the cached path on every request;
                # otherwise a symlinked ``runs/<id>`` could bypass the normal
                # state_for boundary check simply because it was already in
                # the registry cache.
                paths = self._run_paths(run_id)
                if paths is None:
                    return None
                _, log_dir = paths
                try:
                    if cached.log_dir.resolve(strict=False) != log_dir:
                        return None
                except (OSError, RuntimeError, ValueError):
                    return None
            return self._refresh_control_capability(run_id, cached)
        if self.runs_root is None or not _safe_run_id(run_id):
            return None
        paths = self._run_paths(run_id)
        if paths is None:
            return None
        _, log_dir = paths
        state = DashboardState(
            log_dir,
            runs_root=self.runs_root,
            control_enabled=False,
        )
        self._states[run_id] = state
        return self._refresh_control_capability(run_id, state)

    def default_state(self) -> tuple[str, DashboardState]:
        if self.base_state.current_run_id:
            return self.base_state.current_run_id, self.base_state
        if self.base_run_id:
            return self.base_run_id, self.base_state
        if self.runs_root is not None:
            items = self.supervisor.list_run_items() if self.supervisor is not None else self.base_state.list_runs()
            if items:
                selected = str(items[0].get("id") or "")
                selected_state = self.state_for(selected)
                if selected_state is not None:
                    return selected, selected_state
        return self.base_run_id, self.base_state

    def run_items(self) -> list[dict[str, Any]]:
        if self.supervisor is not None:
            return self.supervisor.list_run_items()
        items = self.base_state.list_runs()
        if items:
            return items
        if self.runs_root is not None:
            return []
        run_id, state = self.default_state()
        return [
            {
                "id": run_id,
                "log_dir": str(state.log_dir),
                "mtime": 0.0,
                "status": "",
            }
        ]


def _safe_run_id(run_id: str) -> bool:
    return (
        isinstance(run_id, str)
        and bool(run_id)
        and len(run_id) <= 128
        and run_id not in {".", ".."}
        and "/" not in run_id
        and "\\" not in run_id
        and not any(ord(char) < 0x20 or ord(char) == 0x7F for char in run_id)
    )


def _runs_root_config_path(runs_root: str | Path | None) -> Path | None:
    """Return the project config path inside ``runs_root/.lh-harness`` if it exists."""

    if not runs_root:
        return None
    path = Path(runs_root) / ".lh-harness" / "config.toml"
    return path if path.is_file() else None


def _bounded_command_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if len(normalized) > 256 or "\x00" in normalized:
        raise HTTPException(status_code=422, detail="Idempotency-Key must be at most 256 characters")
    return normalized or None


def _bounded_cursor(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) > 256 or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise HTTPException(status_code=422, detail="after cursor is invalid or too long")
    return value


def _body_positive_int(value: Any, *, field: str, default: int | None = None) -> int:
    if value is None and default is not None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HTTPException(status_code=422, detail=f"{field} must be an integer of at least 1")
    if field == "max_rounds" and value > MAX_ROUNDS:
        raise HTTPException(status_code=422, detail=f"{field} must be at most {MAX_ROUNDS}")
    return value


def _strict_optional_revision(value: Any) -> int | None:
    """Parse a JSON revision without lossy ``int()`` coercion.

    In particular, ``1.5`` must not silently become revision ``1`` and bools
    must not pass as Python's integer subtype.  Strings are accepted only in
    the canonical optional-sign decimal form for compatibility with clients
    that serialize form values.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        raise HTTPException(status_code=422, detail="expected_revision must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise HTTPException(status_code=422, detail="expected_revision must be an integer")
    if isinstance(value, str):
        text = value.strip()
        if not text or len(text) > 32 or not re.fullmatch(r"[+-]?\d+", text):
            raise HTTPException(status_code=422, detail="expected_revision must be an integer")
        try:
            return int(text, 10)
        except ValueError:
            raise HTTPException(status_code=422, detail="expected_revision must be an integer") from None
    raise HTTPException(status_code=422, detail="expected_revision must be an integer")


def _optional_extra_rounds(value: Any) -> int | None:
    """Parse an optional extra-round grant from a request body.

    Rejecting an out-of-range value at the boundary (rather than clamping it)
    keeps an obvious operator mistake visible instead of silently granting a
    different budget than the one that was typed.
    """

    if value is None or value == "":
        return None
    detail = f"extra_rounds must be an integer from 1 to {MAX_ROUNDS}"
    if isinstance(value, bool) or isinstance(value, float):
        raise HTTPException(status_code=422, detail=detail)
    if isinstance(value, str):
        text = value.strip()
        if not text or len(text) > 16 or not re.fullmatch(r"\d+", text):
            raise HTTPException(status_code=422, detail=detail)
        value = int(text, 10)
    if not isinstance(value, int) or not 1 <= value <= MAX_ROUNDS:
        raise HTTPException(status_code=422, detail=detail)
    return value


def _body_text(value: Any, *, field: str, required: bool = False, max_chars: int = 100_000) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise HTTPException(status_code=422, detail=f"{field} must be a string")
    if required and not text:
        raise HTTPException(status_code=422, detail=f"{field} is required")
    if len(text) > max_chars:
        raise HTTPException(status_code=413, detail=f"{field} is too large")
    if "\x00" in text:
        raise HTTPException(status_code=422, detail=f"{field} contains a NUL byte")
    return text


def _state_or_404(registry: StateRegistry, run_id: str) -> DashboardState:
    state = registry.state_for(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="run not found")
    return state


def _public_owner(owner: dict[str, Any]) -> dict[str, Any]:
    """Expose lifecycle metadata without echoing prompts/argv/credentials."""

    allowed = {
        "run_id",
        "pid",
        "pgid",
        "started_at",
        "agent",
        "model",
        "reasoning_effort",
        "role_configs",
        "max_rounds",
        "prompt_language",
        "workspace",
        "resumed_from",
        "resume_kind",
        "resume_epoch",
        "mcp_profile",
    }
    result = {key: owner[key] for key in allowed if key in owner}
    if isinstance(owner.get("task"), str):
        result["task_summary"] = str(owner["task"])[:500]
    return result


def _event_tailer(state: DashboardState, run_id: str) -> EventTailer:
    return EventTailer(state.role_dir / "events.jsonl", run_id=run_id)


def _snapshot_for(registry: StateRegistry, state: DashboardState, run_id: str) -> dict[str, Any]:
    """Overlay durable Supervisor lifecycle state on the log projection.

    A stopped Worker can have a perfectly valid ``events.jsonl`` tail but no
    final report. The log-only projection would call that run ``running``;
    standalone Web clients must trust the persisted process owner/status
    when one exists.
    """
    result = build_snapshot(state, run_id=run_id)
    supervisor = registry.supervisor
    if supervisor is None:
        return result
    # A caller may provide a single-run DashboardState without ``runs_root``;
    # in that mode build_snapshot cannot safely discover control/owner.json on
    # its own.  The supervisor remains the durable provenance authority.
    try:
        owner = supervisor.owner(run_id)
    except (OSError, RuntimeError, ValueError):
        owner = {}
    if isinstance(owner, dict):
        result["run"].update(_provenance(owner))
        # Single-run DashboardState instances may not have ``runs_root`` and
        # therefore cannot read owner.json inside build_snapshot().  The
        # Supervisor is authoritative here, including for the task shown while
        # the first Manager round is running.
        if not str(result.get("mission", {}).get("task") or "").strip():
            owner_task = owner.get("task")
            if isinstance(owner_task, str) and owner_task.strip():
                result["mission"]["task"] = owner_task
    managed = supervisor.status(run_id)
    if managed.get("managed") is False:
        # Keep unmanaged runs' log/report projection visible, but do not let
        # stale lifecycle metadata overwrite it or re-enable operator controls.
        result["run"]["managed"] = False
        result["controls"] = {
            **result.get("controls", {}),
            "can_inject": False,
            "can_abort": False,
            "can_resume": False,
        }
        return result
    status = canonical_lifecycle_status(managed.get("status") or result["run"].get("status") or "idle")
    result["run"]["status"] = status
    # Keep the auditor/report vocabulary available as evidence without making
    # it the process-lifecycle authority.
    if managed.get("report_status"):
        result["run"]["report_status"] = managed["report_status"]
    for field in ("completion_satisfied", "completion_authority", "exit_code", "failure_reason", "finished_at", "started_at"):
        if field in managed and managed.get(field) is not None:
            result["run"][field] = managed[field]
    # Lets a client tell "stopping normally" from "SIGTERM was ignored" and only
    # then offer the force-kill escalation.
    for field in ("requested_action", "stop_requested_at"):
        if managed.get(field) is not None:
            result["run"][field] = managed[field]
    # Clients keep lifecycle monotonic to survive REST/WS races, so a resumed
    # run needs an explicit generation counter: without it a reopened run looks
    # like a stale "running" frame arriving after a terminal one and is dropped.
    epoch = resume_epoch(managed) or resume_epoch(owner)
    if epoch:
        result["run"]["resume_epoch"] = epoch
    if status in TERMINAL_STATUSES:
        # Supervisor lifecycle is authoritative even when the worker died
        # before its final round directory was marked closed. Do not expose a
        # stale in-progress round/role after stop, abort, or terminal exit.
        result["active_round"] = None
        result["active_role"] = None
    alive = bool(managed.get("alive"))
    result["controls"] = {
        **result.get("controls", {}),
        "can_inject": alive,
        "can_abort": alive and status in {"running", "waiting_approval", "starting"},
        "can_resume": (
            not bool(getattr(supervisor, "attached_only", False))
            and not alive
            and status in TERMINAL_STATUSES
        ),
    }
    return result


def _maybe_start_fleet_reporter(registry: StateRegistry, supervisor: RunSupervisor | None) -> None:
    """Start the fleet reporter when LH_HARNESS_FLEET_URL is configured.

    The reporter is a fail-open side-car: if the env var is unset, this function
    is a no-op and the web server behaves exactly as before.  When enabled, it
    registers a heartbeat callback that describes this node and its supervised
    runs every 30 s.
    """

    if not os.environ.get("LH_HARNESS_FLEET_URL"):
        return

    version = "unknown"
    try:
        from ... import __version__ as _lh_version

        version = _lh_version
    except Exception:
        pass

    # Capacity is best-effort: count the workers this supervisor already owns.
    active_cap = 0 if supervisor is None else max(1, len(supervisor._processes))

    def _heartbeat() -> tuple[list[dict[str, Any]], int, int, int]:
        runs: list[dict[str, Any]] = []
        active = 0
        try:
            for item in registry.run_items():
                run_id = str(item.get("id") or "")
                if not run_id:
                    continue
                state = registry.state_for(run_id)
                runs.append(build_run_summary(item, state=state))
            if supervisor is not None:
                active = sum(
                    1 for p in supervisor._processes.values() if p.poll() is None
                )
        except Exception:
            logger.exception("fleet heartbeat callback failed")
        # The queue length is not tracked by the supervisor; report 0.
        return runs, active, active_cap, 0

    reporter = get_reporter(version=version, capacity=active_cap)
    if reporter is not None and reporter.enabled:
        reporter.register_heartbeat(_heartbeat)


def _stream_projection_signature(snapshot: dict[str, Any]) -> tuple[Any, ...]:
    """Track snapshot-only state that must wake connected Web clients."""

    operator_messages = tuple(
        (item.get("id"), item.get("status"))
        for item in snapshot.get("operator_messages", [])
        if isinstance(item, dict)
    )
    approvals = tuple(
        (
            item.get("approval_id"),
            item.get("status"),
            item.get("action"),
            item.get("user_input"),
            item.get("resolved_at"),
        )
        for item in snapshot.get("approvals", [])
        if isinstance(item, dict)
    )
    return (
        snapshot.get("run", {}).get("status"),
        snapshot.get("run", {}).get("finished_at"),
        snapshot.get("run", {}).get("exit_code"),
        snapshot.get("run", {}).get("resume_epoch"),
        snapshot.get("active_round"),
        snapshot.get("active_role"),
        snapshot.get("controls", {}).get("can_inject"),
        snapshot.get("controls", {}).get("can_abort"),
        snapshot.get("controls", {}).get("can_resume"),
        operator_messages,
        approvals,
    )


# Fields that make a snapshot expensive for the UI switch path.  The summary
# snapshot omits them; the dashboard later fetches rounds/transcripts on demand.
_HEAVY_SNAPSHOT_FIELDS = frozenset({"rounds", "events", "legacy"})


def _summary_from_full(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return a lightweight snapshot for run list switching.

    Keeps run, mission, active_round/role, approvals, operator_messages and
    controls.  Drops rounds, events and legacy.  The full payload remains
    available through the unparameterized snapshot route and the per-round
    transcript/artifact endpoints.
    """

    result = dict(snapshot)
    for field in _HEAVY_SNAPSHOT_FIELDS:
        result.pop(field, None)
    # Preserve the schema contract and diagnostics shape without the heavy
    # arrays; the UI uses event_count as a freshness hint.
    result["diagnostics"] = {**snapshot.get("diagnostics", {})}
    result["diagnostics"].pop("warnings", None)
    result["diagnostics"]["event_count"] = snapshot.get("diagnostics", {}).get("event_count", 0)
    return result


def _snapshot_etag(snapshot: dict[str, Any]) -> str:
    """Stable ETag for a snapshot payload; changes when content changes."""

    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    )
    return f'"{digest.hexdigest()}"'


class _SnapshotCache:
    """In-memory cache for per-run snapshots keyed by filesystem mtime+size."""

    def __init__(self, ttl_seconds: float = 2.0) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, tuple[tuple[float, int, float], dict[str, Any]]] = {}
        self._ttl = ttl_seconds

    def _signature(self, state: DashboardState, run_id: str) -> tuple[float, int, float]:
        events_path = state.role_dir / "events.jsonl"
        rounds_root = state._safe_rounds_root()
        try:
            events_stat = events_path.stat()
            events_mtime = events_stat.st_mtime
            events_size = events_stat.st_size
        except (OSError, ValueError):
            events_mtime = 0.0
            events_size = 0
        rounds_mtime = 0.0
        if rounds_root is not None:
            try:
                rounds_stat = rounds_root.stat()
                rounds_mtime = rounds_stat.st_mtime
            except (OSError, ValueError):
                pass
        return (events_mtime, events_size, rounds_mtime)

    def _control_signature(self, state: DashboardState) -> str:
        """Include control-bus state that affects operator_messages/approvals.

        The control bus writes append files and updates receipts that are not
        captured by the events.jsonl mtime/size or the rounds directory mtime.
        """
        control_dir = getattr(state, "control_bus", None)
        if control_dir is None:
            return ""
        control_root = Path(control_dir.run_dir) / "control"
        sig: list[tuple[float, int]] = []
        # The control bus persists commands in ``commands.jsonl`` and receipts
        # in ``command_receipts.jsonl``.  Include both so applied/resolved
        # operator interactions invalidate the cached snapshot.
        for name in ("commands.jsonl", "command_receipts.jsonl"):
            try:
                st = (control_root / name).stat()
                sig.append((st.st_mtime, st.st_size))
            except (OSError, ValueError):
                sig.append((0.0, 0))
        return json.dumps(sig, separators=(",", ":"))

    def get(self, state: DashboardState, run_id: str) -> tuple[tuple[float, int, float, str], dict[str, Any]] | None:
        signature = self._signature(state, run_id)
        control_signature = self._control_signature(state)
        with self._lock:
            cached = self._data.get(run_id)
            if cached is None:
                return None
            cached_signature, snapshot = cached
            # Treat the signature as a freshness hint: if the on-disk state has
            # changed, always rebuild.  Otherwise reuse the in-memory copy for
            # ``ttl_seconds`` so rapid switch polling avoids repeated rebuilds.
            now = time.monotonic()
            if cached_signature != signature + (control_signature,):
                return None
            if now - getattr(self, "_last_write", {}).get(run_id, 0.0) > self._ttl:
                return None
            return signature + (control_signature,), snapshot

    def put(self, run_id: str, signature: tuple[float, int, float, str], snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._data[run_id] = (signature, snapshot)
            last_write = getattr(self, "_last_write", {})
            last_write[run_id] = time.monotonic()
            self._last_write = last_write

    def invalidate(self, run_id: str) -> None:
        with self._lock:
            self._data.pop(run_id, None)
            last_write = getattr(self, "_last_write", {})
            last_write.pop(run_id, None)
            self._last_write = last_write


def create_app(
    *,
    state: DashboardState | None = None,
    log_dir: str | Path | None = None,
    runs_root: str | Path | None = None,
    run_id: str | None = None,
    control_enabled: bool = False,
    workspace_root: str | Path | None = None,
    supervisor: RunSupervisor | None = None,
    auth_token: str | None = None,
    allowed_origins: set[str] | list[str] | tuple[str, ...] | None = None,
    bind_host: str = "127.0.0.1",
) -> FastAPI:
    """Create an API app over a live shared state or a historical runs root."""

    dashboard_state = state or DashboardState(
        log_dir,
        runs_root=runs_root,
        control_enabled=control_enabled,
    )
    registry = StateRegistry(
        state=dashboard_state,
        runs_root=runs_root,
        run_id=run_id,
        supervisor=supervisor,
    )
    token = _configured_token(auth_token)
    origins = {str(item).rstrip("/") for item in (allowed_origins or ()) if str(item).strip()}
    queue_store: QueueStore | None = None
    if runs_root is not None:
        queue_store = QueueStore(runs_root)
    queue_config = default_queue_config()
    if queue_store is not None:
        try:
            from ..config import PROJECT_CONFIG_PATH, load_run_defaults

            # Prefer a project config next to the runs root; fall back to the
            # current working directory so existing deployments keep working.
            config_path = _runs_root_config_path(runs_root) or PROJECT_CONFIG_PATH
            project = load_run_defaults(config_path)
            if isinstance(project.get("queue"), dict):
                queue_config = queue_config_from_config(project)
        except Exception:
            pass
    launcher: Launcher | None = None
    if supervisor is not None and queue_store is not None:
        launcher = Launcher(supervisor, queue_store, queue_config=queue_config)

    snapshot_cache = _SnapshotCache(ttl_seconds=2.0)

    def _cached_snapshot_for(state: DashboardState, run_id: str) -> dict[str, Any]:
        cached = snapshot_cache.get(state, run_id)
        if cached is not None:
            return cached[1]
        snapshot = _snapshot_for(registry, state, run_id)
        signature = snapshot_cache._signature(state, run_id)
        control_signature = snapshot_cache._control_signature(state)
        snapshot_cache.put(run_id, signature + (control_signature,), snapshot)
        return snapshot

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> Any:
        if launcher is not None:
            await launcher.start()
        yield
        if launcher is not None:
            await launcher.stop()

    app = FastAPI(title="LongHorizon-Harness Web API", version="1", lifespan=_lifespan)
    app.state.registry = registry
    app.state.queue_store = queue_store
    app.state.auth_token = token
    app.state.allowed_origins = origins
    app.state.bind_host = bind_host
    _maybe_start_fleet_reporter(registry, supervisor)
    app.state.launcher = launcher
    if supervisor is not None:
        async def _shutdown_owned_workers() -> None:
            await asyncio.to_thread(supervisor.shutdown)

        app.router.add_event_handler("shutdown", _shutdown_owned_workers)

    @app.middleware("http")
    async def _security_headers_and_auth(request: Request, call_next):
        # Static assets remain public so a browser can render the login-free
        # shell, but every API route (including legacy compatibility routes and
        # artifact reads) shares one authentication boundary when a token is
        # configured.
        if _is_loopback_host(bind_host) and not _host_header_allowed(
            request.headers.get("host", ""), bind_host
        ):
            return JSONResponse({"detail": "host is not allowed"}, status_code=403)
        authenticated = _bearer_matches(request.headers.get("authorization"), token)
        if token and request.url.path.startswith("/api/") and not authenticated:
            return JSONResponse(
                {"detail": "invalid or missing bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        if request.method in {"POST", "PUT", "PATCH"} and request.url.path.startswith("/api/"):
            content_type = request.headers.get("content-type")
            if not _is_json_content_type(content_type):
                return JSONResponse(
                    {"detail": "request must be application/json"},
                    status_code=415,
                )
            content_length = request.headers.get("content-length")
            if content_length is not None:
                try:
                    declared = int(content_length)
                except ValueError:
                    return JSONResponse({"detail": "invalid content-length"}, status_code=400)
                if declared < 0:
                    return JSONResponse({"detail": "invalid content-length"}, status_code=400)
                if declared > _MAX_CONTROL_BODY_BYTES:
                    return JSONResponse({"detail": "request body is too large"}, status_code=413)
            if not await _cache_bounded_request_body(request, _MAX_CONTROL_BODY_BYTES):
                return JSONResponse({"detail": "request body is too large"}, status_code=413)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        # API snapshot responses set their own cache headers via _snapshot_response.
        if not request.url.path.startswith("/api/runs/") or not request.url.path.endswith("/snapshot"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/api/meta")
    def meta(request: Request) -> dict[str, Any]:
        return _meta_response(request, force_models=False)

    def _meta_response(request: Request, *, force_models: bool) -> dict[str, Any]:
        endpoint = str(request.base_url).rstrip("/")
        live_control = any(item.control_enabled for item in registry._states.values()) or supervisor is not None
        catalogue = discover_model_catalog(
            force=force_models,
            codex_binary=resolve_codex_binary(),
            dsh_binary=resolve_dsh_binary(),
            opencode_binary=resolve_opencode_binary(),
        )
        return build_meta(
            endpoint=endpoint,
            capabilities={
                "approvals": live_control,
                "injections": live_control,
                "run_control": supervisor is not None,
                "create_run": supervisor is not None and not bool(getattr(supervisor, "attached_only", False)),
                "resume": supervisor is not None and not bool(getattr(supervisor, "attached_only", False)),
                "stop": supervisor is not None,
                "abort": supervisor is not None,
                "fleet_mcp_tools": queue_store is not None,
            },
            mcp_gateway_alias="lhharness",
            agents=catalogue["agents"],
            models=catalogue["models"],
            defaults={
                "agent": _WEB_DEFAULT_AGENT,
                "model": _WEB_DEFAULT_MODEL,
                "roles": {
                    "manager": {
                        "agent": _WEB_DEFAULT_AGENT,
                        "model": _WEB_DEFAULT_MANAGER_MODEL,
                        "mcp_profile": _default_profile_for_role("manager"),
                    },
                    "executor": {
                        "agent": _WEB_DEFAULT_AGENT,
                        "model": _WEB_DEFAULT_MODEL,
                        "mcp_profile": _default_profile_for_role("executor"),
                    },
                    "auditor": {
                        "agent": _WEB_DEFAULT_AGENT,
                        "model": _WEB_DEFAULT_AUDITOR_MODEL,
                        "mcp_profile": _default_profile_for_role("auditor"),
                    },
                },
            },
            mcp_profiles=list_available_profiles(),
            mcp_gateway_configured=gateway_configured(),
            model_discovery=catalogue["model_discovery"],
        )

    @app.post("/api/models/refresh")
    def refresh_models(request: Request) -> dict[str, Any]:
        """Explicitly refresh login-aware model discovery.

        Ordinary metadata polling stays cheap.  The workbench invokes this
        endpoint only when the user asks to re-detect local provider models.
        """

        return _meta_response(request, force_models=True)

    @app.post("/api/queue")
    def create_queue_entry(request: Request, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        if queue_store is None:
            raise HTTPException(status_code=501, detail="queue requires a configured runs root")
        try:
            entry = queue_store.create(body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, "queue_id": entry.queue_id}

    @app.get("/api/queue")
    def list_queue(
        status: str | None = None,
    ) -> dict[str, Any]:
        if queue_store is None:
            raise HTTPException(status_code=501, detail="queue requires a configured runs root")
        entries = queue_store.list()
        valid_statuses = {"pending", "launched", "done", "failed"}
        filtered = entries
        if status is not None:
            if status not in valid_statuses:
                raise HTTPException(status_code=422, detail=f"status must be one of: {', '.join(sorted(valid_statuses))}")
            filtered = [item for item in entries if item.status == status]
        groups: dict[str, list[dict[str, Any]]] = {
            "pending": [],
            "launched": [],
            "done": [],
            "failed": [],
        }
        for item in entries:
            groups[item.status].append(item.to_dict())
        return {
            "entries": [item.to_dict() for item in filtered],
            "groups": groups,
            "counts": queue_store.counts(),
        }

    @app.delete("/api/queue/{queue_id}")
    def delete_queue_entry(queue_id: str) -> dict[str, Any]:
        if queue_store is None:
            raise HTTPException(status_code=501, detail="queue requires a configured runs root")
        entry = queue_store.get(queue_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="queue entry not found")
        if entry.status != "pending":
            raise HTTPException(status_code=409, detail=f"cannot delete entry with status {entry.status}")
        removed = queue_store.delete(queue_id)
        if removed is None:
            raise HTTPException(status_code=404, detail="queue entry not found")
        return {"ok": True, "queue_id": queue_id, "status": "deleted"}

    @app.get("/api/mcp/fleet/tools")
    def mcp_fleet_tools() -> dict[str, Any]:
        return {"ok": True, "gateway_alias": "lhharness", "tools": tools_manifest()}

    @app.post("/api/mcp/fleet/{tool_name}")
    def mcp_fleet_invoke(
        tool_name: str,
        request: Request,
        body: dict[str, Any] = Body(default_factory=dict),
    ) -> dict[str, Any]:
        result = _dispatch_mcp_tool(
            tool_name,
            body.get("arguments", {}),
            queue_store=queue_store,
            registry=registry,
            supervisor=supervisor,
            auth_token=token,
            request_token=normalize_request_token(request.headers.get("authorization")),
        )
        status = result.get("code", 200)
        if status == 401:
            return JSONResponse(result, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        if status == 404:
            return JSONResponse(result, status_code=404)
        if status >= 400:
            return JSONResponse(result, status_code=status)
        return result

    @app.post("/api/queue/{queue_id}/priority")
    def update_queue_priority(queue_id: str, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        if queue_store is None:
            raise HTTPException(status_code=501, detail="queue requires a configured runs root")
        priority = body.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise HTTPException(status_code=422, detail="priority must be an integer")
        entry = queue_store.get(queue_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="queue entry not found")
        if entry.status != "pending":
            raise HTTPException(status_code=409, detail=f"cannot update priority for status {entry.status}")
        updated = queue_store.set_priority(queue_id, priority)
        if updated is None:
            raise HTTPException(status_code=404, detail="queue entry not found")
        return {"ok": True, "queue_id": queue_id, "priority": updated.priority}

    @app.get("/api/queue/config")
    def queue_config_endpoint() -> dict[str, Any]:
        if queue_store is None:
            return default_queue_config()
        effective = default_queue_config()
        try:
            from ..config import load_run_defaults, PROJECT_CONFIG_PATH
            project = load_run_defaults(PROJECT_CONFIG_PATH)
            project_queue = project.get("queue")
            if isinstance(project_queue, dict):
                effective = queue_config_from_config(project)
        except Exception:
            pass
        return effective

    @app.get("/api/runs")
    def runs() -> dict[str, Any]:
        result: list[dict[str, Any]] = []
        for item in registry.run_items():
            item_run_id = str(item.get("id") or "")
            result.append(build_run_summary(item, state=registry.state_for(item_run_id)))
        return {"runs": result}

    @app.post("/api/runs")
    def create_run(request: Request, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        if supervisor is None or bool(getattr(supervisor, "attached_only", False)):
            raise HTTPException(status_code=501, detail="run creation requires the standalone Web supervisor")
        try:
            task = _body_text(body.get("task", body.get("instructions", "")), field="task", required=True)
            agent = _body_text(body.get("agent", "codex"), field="agent", required=True, max_chars=64)
            model = _body_text(body.get("model"), field="model", max_chars=256) or None
            reasoning_effort = _body_text(
                body.get("reasoning_effort"), field="reasoning_effort", max_chars=64
            ) or None
            role_configs = body.get("roles")
            workspace = _body_text(body.get("workspace"), field="workspace", max_chars=4096) or None
            run_id_value = _body_text(body.get("run_id"), field="run_id", max_chars=128) or None
            max_rounds = _body_positive_int(
                body.get("max_rounds"),
                field="max_rounds",
                default=DEFAULT_MAX_ROUNDS,
            )
            prompt_language = _body_text(
                body.get("prompt_language", "en"),
                field="prompt_language",
                required=True,
                max_chars=2,
            )
            if prompt_language not in {"en", "zh"}:
                raise ValueError("prompt_language must be en or zh")
            mcp_profile = _body_text(body.get("mcp_profile"), field="mcp_profile", max_chars=64) or None
            youtrack_issue_id = _body_text(body.get("youtrack_issue_id"), field="youtrack_issue_id", max_chars=64) or None
            created = supervisor.create_run(
                task=task,
                agent=agent,
                model=model,
                role_configs=role_configs,
                workspace=workspace,
                max_rounds=max_rounds,
                prompt_language=prompt_language,
                run_id=run_id_value,
                reasoning_effort=reasoning_effort,
                mcp_profile=mcp_profile,
                youtrack_issue_id=youtrack_issue_id,
                idempotency_key=_bounded_command_id(request.headers.get("Idempotency-Key")),
            )
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (TypeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if isinstance(created.get("owner"), dict):
            created = {**created, "owner": _public_owner(created["owner"])}
        return {"ok": True, "run": created}

    def _snapshot_response(
        request: Request,
        state: DashboardState,
        run_id: str,
        *,
        summary_only: bool,
    ) -> Response:
        snapshot = _cached_snapshot_for(state, run_id)
        etag = _snapshot_etag(snapshot)
        if_none_match = request.headers.get("if-none-match")
        # Fast equality without hashing twice on the 304 path.
        if if_none_match and if_none_match == etag:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, max-age=2"})
        if summary_only:
            body = _summary_from_full(snapshot)
        else:
            body = snapshot
        content = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        # gzip the payload ourselves when the client accepts it and the
        # response is large enough to benefit.  Starlette's GZipMiddleware
        # would otherwise handle it, but explicit control lets us keep the
        # 304 path cheap and only compress full snapshots.
        accepts_gzip = "gzip" in (request.headers.get("accept-encoding") or "").lower()
        if accepts_gzip and len(content) > 256:
            compressed = gzip.compress(content, compresslevel=5)
            return Response(
                content=compressed,
                media_type="application/json",
                headers={
                    "Content-Encoding": "gzip",
                    "ETag": etag,
                    "Cache-Control": "private, max-age=2",
                    "Vary": "Accept-Encoding",
                },
            )
        return Response(
            content=content,
            media_type="application/json",
            headers={
                "ETag": etag,
                "Cache-Control": "private, max-age=2",
                "Vary": "Accept-Encoding",
            },
        )

    @app.get("/api/runs/{run_id}/snapshot")
    def snapshot(
        run_id: str,
        request: Request,
        fields: str | None = Query(None),
    ) -> Response:
        state_for_run = _state_or_404(registry, run_id)
        summary_only = fields == "summary"
        return _snapshot_response(request, state_for_run, run_id, summary_only=summary_only)

    @app.get("/api/runs/{run_id}/events")
    def events(
        run_id: str,
        after: str | None = None,
        limit: int = Query(500, ge=1, le=5000),
    ) -> dict[str, Any]:
        after = _bounded_cursor(after)
        state_for_run = _state_or_404(registry, run_id)
        tailer = _event_tailer(state_for_run, run_id)
        items = tailer.read(limit=limit, after=after)
        return {
            "run_id": run_id,
            "events": [item.to_dict() for item in items],
            "last_event_id": items[-1].event_id if items else after,
            "cursor_gap": tailer.last_cursor_gap,
            "resync_required": tailer.last_resync_required,
            "diagnostics": {"warnings": tailer.last_warnings},
        }

    @app.websocket("/api/runs/{run_id}/stream")
    async def stream(
        websocket: WebSocket,
        run_id: str,
        replay: int = Query(100, ge=0, le=5000),
        after: str | None = None,
    ) -> None:
        after = _bounded_cursor(after)
        state_for_run = registry.state_for(run_id)
        if state_for_run is None:
            await websocket.close(code=4404, reason="run not found")
            return
        origin = websocket.headers.get("origin")
        host = websocket.headers.get("host", "")
        if _is_loopback_host(bind_host) and not _host_header_allowed(host, bind_host):
            await websocket.close(code=4403, reason="host is not allowed")
            return
        authenticated, selected_subprotocol = _websocket_auth(websocket, token)
        if not authenticated:
            await websocket.close(code=4401, reason="invalid or missing bearer token")
            return
        if not _origin_allowed(origin, host, origins):
            await websocket.close(code=4403, reason="origin is not allowed")
            return
        await websocket.accept(subprotocol=selected_subprotocol)
        initial_snapshot = _cached_snapshot_for(state_for_run, run_id)
        await websocket.send_json({"kind": "snapshot", "data": initial_snapshot})
        last_projection_signature = _stream_projection_signature(initial_snapshot)
        tailer = _event_tailer(state_for_run, run_id)
        cursor = after
        initial = tailer.read(limit=max(1, replay), after=after) if replay else []
        if tailer.last_resync_required:
            await websocket.send_json({
                "kind": "resync_required",
                "cursor": after,
                "diagnostics": {"warnings": tailer.last_warnings},
            })
            # A gap invalidates deltas.  Start the live cursor at the current
            # tail so subsequent frames are contiguous after the snapshot.
            current_tail = tailer.read(limit=1)
            cursor = current_tail[-1].event_id if current_tail else None
            initial = []
        if not replay and after is None:
            existing = tailer.read(limit=5000)
            cursor = existing[-1].event_id if existing else None
        for item in initial:
            await websocket.send_json({"kind": "event", "data": item.to_dict()})
            cursor = item.event_id
        idle_ticks = 0
        try:
            while True:
                fresh = tailer.read(limit=5000, after=cursor)
                if tailer.last_resync_required:
                    await websocket.send_json({
                        "kind": "resync_required",
                        "cursor": cursor,
                        "diagnostics": {"warnings": tailer.last_warnings},
                    })
                    # The requested cursor is absent, so last_last_event_id is
                    # that same missing value.  Move to the retained tail after
                    # sending a fresh snapshot; otherwise every poll repeats
                    # the same gap forever and no future event can be emitted.
                    current_tail = tailer.read(limit=1)
                    cursor = current_tail[-1].event_id if current_tail else None
                    await websocket.send_json({
                        "kind": "snapshot",
                        "data": _cached_snapshot_for(state_for_run, run_id),
                    })
                    fresh = []
                if fresh:
                    idle_ticks = 0
                    for item in fresh:
                        await websocket.send_json({"kind": "event", "data": item.to_dict()})
                        cursor = item.event_id
                    # Round files, approvals, and active-role state change beside
                    # the event log. Refresh the projection after each batch so
                    # clients do not need to independently poll every file.
                    updated_snapshot = _cached_snapshot_for(state_for_run, run_id)
                    await websocket.send_json({"kind": "snapshot", "data": updated_snapshot})
                    last_projection_signature = _stream_projection_signature(updated_snapshot)
                else:
                    idle_ticks += 1
                    # Lifecycle and operator commands live outside the role
                    # event log. Poll their projection once per second in both
                    # supervised and attached (`lh-harness run`) modes.
                    if idle_ticks % 4 == 0:
                        updated_snapshot = _cached_snapshot_for(state_for_run, run_id)
                        projection_signature = _stream_projection_signature(updated_snapshot)
                        if projection_signature != last_projection_signature:
                            await websocket.send_json({"kind": "snapshot", "data": updated_snapshot})
                            last_projection_signature = projection_signature
                    if idle_ticks >= 40:
                        await websocket.send_json({"kind": "heartbeat"})
                        idle_ticks = 0
                # Observe client/server close frames instead of only finding
                # out on the next heartbeat send. Otherwise idle WebSocket
                # loops can hold Uvicorn's graceful Ctrl-C shutdown open for
                # up to ten seconds (or indefinitely with reconnecting tabs).
                try:
                    message = await asyncio.wait_for(websocket.receive(), timeout=0.25)
                    if message.get("type") == "websocket.disconnect":
                        return
                except asyncio.TimeoutError:
                    pass
        except (WebSocketDisconnect, asyncio.CancelledError):
            return

    @app.get("/api/runs/{run_id}/rounds/{round_index}/artifacts")
    def artifacts(run_id: str, round_index: int) -> dict[str, Any]:
        state_for_run = _state_or_404(registry, run_id)
        return {
            "run_id": run_id,
            "round_index": round_index,
            "artifacts": state_for_run.list_round_artifacts(round_index),
        }

    @app.get("/api/runs/{run_id}/rounds/{round_index}/artifacts/{name}", response_class=PlainTextResponse)
    def artifact(run_id: str, round_index: int, name: str) -> PlainTextResponse:
        state_for_run = _state_or_404(registry, run_id)
        target = state_for_run.resolve_round_artifact(round_index, name)
        if target is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        size = state_for_run.round_artifact_size(round_index, name)
        if size is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        if size > _MAX_ARTIFACT_BYTES:
            raise HTTPException(status_code=413, detail="artifact is too large")
        content = state_for_run.read_round_artifact(round_index, name)
        if content is None:
            raise HTTPException(status_code=413 if size >= _MAX_ARTIFACT_BYTES else 404, detail="artifact changed during read")
        return PlainTextResponse(
            content,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    @app.get("/api/runs/{run_id}/rounds/{round_index}/artifacts/{name}/raw")
    def raw_artifact(run_id: str, round_index: int, name: str) -> Response:
        artifact_state = _state_or_404(registry, run_id)
        target = artifact_state.resolve_round_artifact(round_index, name)
        if target is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        size = artifact_state.round_artifact_size(round_index, name)
        if size is None:
            raise HTTPException(status_code=404, detail="artifact not found") from None
        if size > _MAX_ARTIFACT_BYTES:
            raise HTTPException(status_code=413, detail="artifact is too large")
        content = artifact_state.read_round_artifact_bytes(round_index, name)
        if content is None:
            # Never hand a path to FileResponse after a separate stat: the
            # worker can replace/grow the file during that window.
            raise HTTPException(status_code=413 if size >= _MAX_ARTIFACT_BYTES else 404, detail="artifact changed during read")
        suffix = target.suffix.lower()
        # Never let an agent-produced document execute in the dashboard
        # origin.  Only explicitly allow-listed raster formats are rendered
        # inline for screenshot/evidence previews; PDFs, SVG/SVGZ, XHTML,
        # scripts and all unknown binary formats are downloads.
        media_type = _INLINE_RASTER_TYPES.get(suffix)
        if media_type is not None:
            disposition = "inline"
            content_security_policy = "default-src 'none'"
        else:
            disposition = "attachment"
            media_type = "text/plain" if suffix in _TEXT_ARTIFACT_SUFFIXES else "application/octet-stream"
            content_security_policy = "default-src 'none'; sandbox"
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": content_security_policy,
                "Content-Disposition": _artifact_content_disposition(disposition, name),
            },
        )

    @app.get("/api/runs/{run_id}/rounds/{round_index}/trajectory/{role}")
    def trajectory(run_id: str, round_index: int, role: str) -> dict[str, Any]:
        trajectory_state = _state_or_404(registry, run_id)
        result = trajectory_state.read_trajectory(round_index, role)
        if result is None:
            raise HTTPException(status_code=404, detail="trajectory not found")
        # Normalized trajectories use OSWorld-style screenshot_file references
        # rather than multi-megabyte data URLs. Resolve only artifacts that pass
        # the same no-follow boundary checks as the raw artifact endpoint.
        for step in result.get("steps", []):
            if not isinstance(step, dict):
                continue
            names = step.get("screenshot_files")
            if not isinstance(names, list):
                single = step.get("screenshot_file")
                names = [single] if isinstance(single, str) else []
            safe_names = [
                name
                for name in names
                if isinstance(name, str)
                and trajectory_state.resolve_round_artifact(round_index, name) is not None
            ]
            if safe_names:
                step["images"] = [
                    f"/api/runs/{quote(run_id, safe='')}/rounds/{round_index}/artifacts/{quote(name, safe='')}/raw"
                    for name in safe_names
                ]
                step["has_image"] = True
        return result

    @app.post("/api/runs/{run_id}/instructions")
    def instructions(
        run_id: str,
        request: Request,
        body: dict[str, Any] = Body(default_factory=dict),
    ) -> dict[str, Any]:
        state_for_run = _state_or_404(registry, run_id)
        if supervisor is not None and not supervisor.can_control(run_id):
            raise HTTPException(status_code=409, detail="run is not accepting instructions")
        text = _body_text(body.get("instructions", body.get("text", "")), field="instructions", required=True, max_chars=50_000)
        expected_revision_int = _strict_optional_revision(body.get("expected_revision"))
        try:
            accepted = state_for_run.add_injection(
                text,
                command_id=_bounded_command_id(request.headers.get("Idempotency-Key")),
                expected_revision=expected_revision_int,
            )
        except (RevisionConflict, CommandConflict) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not accepted:
            raise HTTPException(status_code=409, detail="run is not accepting instructions")
        # Operator interactions change operator_messages and possibly approvals,
        # so the next REST/WS snapshot must not reuse a stale cache.
        snapshot_cache.invalidate(run_id)
        return {
            "ok": True,
            "status": "accepted",
            "idempotency_key": _bounded_command_id(request.headers.get("Idempotency-Key")),
            "revision": state_for_run.control_bus.revision(),
        }

    @app.post("/api/runs/{run_id}/approvals/{approval_id}/resolve")
    def resolve_approval(
        run_id: str,
        approval_id: str,
        request: Request,
        body: dict[str, Any] = Body(default_factory=dict),
    ) -> dict[str, Any]:
        state_for_run = _state_or_404(registry, run_id)
        if supervisor is not None and not supervisor.can_control(run_id):
            raise HTTPException(status_code=409, detail="run is not accepting approvals")
        expected_revision_int = _strict_optional_revision(body.get("expected_revision"))
        user_input = _body_text(body.get("user_input", body.get("instructions", "")), field="user_input", max_chars=50_000)
        # Fleet/chat gateways cannot safely carry non-ASCII bytes in tool args,
        # so the resolve gate enforces ASCII-only operator input.
        if user_input:
            try:
                user_input.encode("ascii")
            except UnicodeEncodeError:
                raise HTTPException(status_code=422, detail="user_input must be ASCII-only") from None
        try:
            ok = state_for_run.resolve_approval(
                approval_id,
                action=_body_text(body.get("action", body.get("decision", "continue")), field="action", required=True, max_chars=128),
                reason=_body_text(body.get("reason", body.get("note", "")), field="reason", max_chars=10_000),
                user_input=user_input,
                extra_rounds=_optional_extra_rounds(body.get("extra_rounds")),
                command_id=_bounded_command_id(request.headers.get("Idempotency-Key")),
                expected_revision=expected_revision_int,
            )
        except (RevisionConflict, CommandConflict) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not ok:
            raise HTTPException(status_code=409, detail="approval is missing, resolved, or read-only")
        snapshot_cache.invalidate(run_id)
        return {
            "ok": True,
            "approval_id": approval_id,
            "status": "accepted",
            "idempotency_key": _bounded_command_id(request.headers.get("Idempotency-Key")),
            "revision": state_for_run.control_bus.revision(),
        }

    @app.post("/api/runs/{run_id}/abort")
    def abort(run_id: str) -> dict[str, Any]:
        _state_or_404(registry, run_id)
        if supervisor is None:
            raise HTTPException(status_code=501, detail="abort requires the standalone Web supervisor")
        try:
            return {"ok": True, **supervisor.abort(run_id)}
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/stop")
    def stop(run_id: str) -> dict[str, Any]:
        _state_or_404(registry, run_id)
        if supervisor is None:
            raise HTTPException(status_code=501, detail="stop requires the standalone Web supervisor")
        try:
            return {"ok": True, **supervisor.stop(run_id)}
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/resume")
    def resume(
        run_id: str,
        request: Request,
        body: dict[str, Any] = Body(default_factory=dict),
    ) -> dict[str, Any]:
        _state_or_404(registry, run_id)
        if supervisor is None or bool(getattr(supervisor, "attached_only", False)):
            raise HTTPException(status_code=501, detail="resume requires the standalone Web supervisor")
        mode = _body_text(body.get("mode", "continue"), field="mode", max_chars=16) or "continue"
        if mode not in {"continue", "retry"}:
            raise HTTPException(status_code=422, detail="mode must be continue or retry")
        try:
            created = supervisor.resume(
                run_id,
                mode=mode,
                extra_rounds=_optional_extra_rounds(body.get("extra_rounds")),
                idempotency_key=_bounded_command_id(request.headers.get("Idempotency-Key")),
            )
            if isinstance(created.get("owner"), dict):
                created = {**created, "owner": _public_owner(created["owner"])}
            return {"ok": True, "run": created}
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (TypeError, ValueError, OSError, RevisionConflict) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/status")
    def run_status(run_id: str) -> dict[str, Any]:
        _state_or_404(registry, run_id)
        if supervisor is None:
            return {"run_id": run_id, "status": "attached", "managed": False}
        managed_status = supervisor.status(run_id)
        return {
            "run_id": run_id,
            "managed": managed_status.get("managed") is not False,
            **managed_status,
            "owner": _public_owner(supervisor.owner(run_id)),
        }

    @app.get("/api/runs/{run_id}/commands/{command_id}")
    def command_receipt(run_id: str, command_id: str) -> dict[str, Any]:
        _state_or_404(registry, run_id)
        if supervisor is None:
            raise HTTPException(status_code=404, detail="command receipts are available for supervised runs")
        receipt = supervisor.command_receipt(run_id, command_id)
        if receipt is None:
            raise HTTPException(status_code=404, detail="command receipt not found")
        return receipt

    # Compress JSON API responses when the client accepts gzip.  The snapshot
    # route handles compression explicitly for large payloads; this covers the
    # rest of the API (events, runs list, queue).
    app.add_middleware(GZipMiddleware, minimum_size=512, compresslevel=5)

    if _STATIC_DIR.is_dir():
        # Starlette's FileResponse delegates MIME detection to Python.  A
        # Windows registry entry can incorrectly map JavaScript to text/plain,
        # which browsers reject for module scripts when nosniff is enabled.
        for suffix, media_type in _DASHBOARD_MIME_TYPES.items():
            mimetypes.add_type(media_type, suffix)
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="dashboard-static")
    return app


def run_web_server(
    *,
    runs_root: str | Path | None = None,
    log_dir: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8799,
    workspace_root: str | Path | None = None,
    auth_token: str | None = None,
    allowed_origins: set[str] | list[str] | tuple[str, ...] | None = None,
) -> int:
    """Run the optional control API in the foreground."""

    import uvicorn

    token = _configured_token(auth_token)
    if not _is_loopback_host(host) and not token:
        raise ValueError(
            "refusing to expose the Web control API beyond localhost without "
            "LH_HARNESS_WEB_TOKEN (or --auth-token)"
        )

    run_id = Path(log_dir).expanduser().resolve().parent.name if log_dir else None
    effective_root = None if log_dir else runs_root
    supervisor = RunSupervisor(effective_root, workspace_root=workspace_root) if effective_root else None
    app = create_app(
        log_dir=log_dir,
        runs_root=effective_root,
        run_id=run_id,
        workspace_root=workspace_root,
        supervisor=supervisor,
        auth_token=token,
        allowed_origins=allowed_origins,
        bind_host=host,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


class WebServerHandle:
    """Handle for an in-process API used by ``lh-harness run --dashboard``."""

    def __init__(self, server: Any, thread: threading.Thread, state: DashboardState, *, host: str) -> None:
        self._server = server
        self._thread = thread
        self.state = state
        self.host = host

    @property
    def port(self) -> int:
        configured = int(self._server.config.port)
        if configured:
            return configured
        for server in getattr(self._server, "servers", []) or []:
            sockets = getattr(server, "sockets", None) or []
            if sockets:
                return int(sockets[0].getsockname()[1])
        return configured

    @property
    def url(self) -> str:
        display_host = "127.0.0.1" if self.host in {"0.0.0.0", "::"} else self.host
        if ":" in display_host and not display_host.startswith("["):
            display_host = f"[{display_host}]"
        return f"http://{display_host}:{self.port}/"

    def shutdown(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            self._server.force_exit = True
            self._thread.join(timeout=1)

    def serve_forever_blocking(self) -> None:
        try:
            self._thread.join()
        except KeyboardInterrupt:
            self.shutdown()


def start_web_server(
    *,
    state: DashboardState | None = None,
    log_dir: str | Path | None = None,
    runs_root: str | Path | None = None,
    run_id: str | None = None,
    task: str = "",
    control_enabled: bool = False,
    workspace_root: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8799,
    supervisor: RunSupervisor | None = None,
    auth_token: str | None = None,
    allowed_origins: set[str] | list[str] | tuple[str, ...] | None = None,
) -> WebServerHandle:
    """Start the FastAPI server in a daemon thread and return its shared state."""

    import uvicorn

    token = _configured_token(auth_token)
    if not _is_loopback_host(host) and not token:
        raise ValueError(
            "refusing to expose the Web control API beyond localhost without "
            "LH_HARNESS_WEB_TOKEN (or --auth-token)"
        )

    dashboard_state = state or DashboardState(
        log_dir,
        task=task,
        runs_root=runs_root,
        control_enabled=control_enabled,
    )
    app = create_app(
        state=dashboard_state,
        runs_root=runs_root,
        run_id=run_id,
        workspace_root=workspace_root,
        supervisor=supervisor,
        auth_token=token,
        allowed_origins=allowed_origins,
        bind_host=host,
    )
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="lh-harness-web", daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=1)
        raise RuntimeError("Web API server did not start")
    return WebServerHandle(server, thread, dashboard_state, host=host)
