"""JSON-RPC 2.0 plumbing for the MCP streamable-HTTP endpoint (``POST /mcp``).

LiteLLM registers an MCP server over streamable HTTP, so the fleet tools that
were previously reachable only through the REST bridge (``GET
/api/mcp/fleet/tools`` and ``POST /api/mcp/fleet/{tool_name}``) also need to
speak JSON-RPC 2.0 directly.  This module carries the protocol layer only:

- ``initialize`` / ``notifications/initialized`` / ``tools/list`` /
  ``tools/call`` dispatch, with ``-32601`` for anything else.
- The tool list and the tool execution are supplied by the caller (the WebAPI
  route passes the same ``tools_manifest()`` and ``mcp_tools.dispatch`` used by
  the REST bridge), so there is exactly one source of truth and one dispatch
  path.

Responses are plain ``application/json``; the streamable-HTTP SSE flavor is not
required.  The endpoint is stateless and never issues or requires an
``Mcp-Session-Id``.
"""

from __future__ import annotations

import json
from typing import Any, Callable

# Newest protocol revision this server speaks.  A client asking for an older
# supported revision gets exactly what it asked for; anything unknown falls
# back to this default so legacy gateways still interoperate.
MCP_PROTOCOL_VERSION = "2025-03-26"
_SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")

SERVER_NAME = "lhharness"

# JSON-RPC error codes used below.
ERROR_PARSE = -32700
ERROR_INVALID_REQUEST = -32600
ERROR_METHOD_NOT_FOUND = -32601


def negotiate_protocol_version(requested: Any) -> str:
    """Echo the client's protocolVersion when supported, else the default."""
    if isinstance(requested, str) and requested in _SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return MCP_PROTOCOL_VERSION


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def parse_message(raw: bytes | str) -> dict[str, Any]:
    """Parse one JSON-RPC message, returning an error envelope on failure."""
    try:
        message = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return mcp_error(None, ERROR_PARSE, "Parse error")
    if not isinstance(message, dict) or not isinstance(message.get("method"), str):
        return mcp_error(None, ERROR_INVALID_REQUEST, "Invalid Request")
    return message


def mcp_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return _error(request_id, code, message)


def is_error_envelope(message: dict[str, Any]) -> bool:
    """True when parse_message produced a protocol-level error envelope."""
    error = message.get("error")
    return isinstance(error, dict) and isinstance(error.get("code"), int)


def handle_message(
    message: dict[str, Any],
    *,
    list_tools: Callable[[], list[dict[str, Any]]],
    call_tool: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> tuple[int, dict[str, Any] | None]:
    """Handle one JSON-RPC message.

    Returns an ``(http_status, payload)`` pair; ``payload is None`` means an
    empty 202 response (a notification).  ``list_tools`` and ``call_tool`` are
    injected so this module stays protocol-only: the WebAPI passes the same
    manifest/dispatch functions the REST bridge uses.
    """
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params")
    if not isinstance(params, dict):
        params = {}

    # Notifications carry no id and expect no response body.
    if request_id is None or method.startswith("notifications/"):
        return 202, None

    if method == "initialize":
        return 200, _result(
            request_id,
            {
                "protocolVersion": negotiate_protocol_version(params.get("protocolVersion")),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": MCP_PROTOCOL_VERSION},
            },
        )

    if method == "tools/list":
        return 200, _result(request_id, {"tools": list_tools()})

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        if not isinstance(tool_name, str) or not tool_name:
            return 200, _error(request_id, ERROR_INVALID_REQUEST, "Invalid Request")
        result = call_tool(tool_name, arguments)
        ok = result.get("ok") is True and not (isinstance(result.get("code"), int) and result["code"] >= 400)
        return 200, _result(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(result)}],
                "isError": not ok,
            },
        )

    return 200, _error(request_id, ERROR_METHOD_NOT_FOUND, "Method not found")