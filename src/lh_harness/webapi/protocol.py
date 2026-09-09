"""Versioned protocol metadata for the Web client."""

from __future__ import annotations

import time
from typing import Any

API_VERSION = "1"
PROTOCOL_VERSION = "1"
SERVICE_NAME = "lh-harness"

DEFAULT_CAPABILITIES: dict[str, bool] = {
    "websocket": True,
    "replay": True,
    "approvals": True,
    "injections": True,
    "run_control": False,
    "multi_run": True,
}


def build_meta(
    *,
    endpoint: str,
    capabilities: dict[str, bool] | None = None,
    server_time: float | None = None,
    agents: list[dict[str, Any]] | None = None,
    models: dict[str, list[dict[str, Any]]] | None = None,
    defaults: dict[str, Any] | None = None,
    mcp_profiles: list[dict[str, Any]] | None = None,
    mcp_gateway_configured: bool = False,
    mcp_gateway_alias: str | None = None,
    model_discovery: dict[str, dict[str, Any]] | None = None,
    fleet_configured: bool = False,
    fleet_ever_succeeded: bool = False,
    fleet_last_ok: bool | None = None,
    fleet_last_error: str | None = None,
) -> dict[str, Any]:
    """Return the stable handshake payload used by both clients."""

    merged = dict(DEFAULT_CAPABILITIES)
    if capabilities:
        merged.update({str(key): bool(value) for key, value in capabilities.items()})
    result: dict[str, Any] = {
        "service": SERVICE_NAME,
        "api_version": API_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "capabilities": merged,
        "server_time": time.time() if server_time is None else server_time,
        "endpoint": endpoint,
    }
    if agents is not None:
        result["agents"] = agents
    if models is not None:
        result["models"] = models
    if defaults is not None:
        result["defaults"] = defaults
    if mcp_profiles is not None:
        result["mcp_profiles"] = mcp_profiles
    result["mcp_gateway_configured"] = bool(mcp_gateway_configured)
    # Fleet registration state, in the same plain-flag style as
    # mcp_gateway_configured.  ``fleet_configured`` False means this node
    # cannot register (missing env vars) and must never be read as idle.
    # ``fleet_last_ok``/``fleet_last_error`` are None until the first
    # registration POST completes (POSTs are batched on a 2 s worker flush).
    result["fleet_configured"] = bool(fleet_configured)
    result["fleet_ever_succeeded"] = bool(fleet_ever_succeeded)
    result["fleet_last_ok"] = fleet_last_ok
    result["fleet_last_error"] = fleet_last_error
    if mcp_gateway_alias is not None:
        result["mcp_gateway_alias"] = mcp_gateway_alias
    if model_discovery is not None:
        result["model_discovery"] = model_discovery
    return result
