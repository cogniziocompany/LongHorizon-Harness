"""Fleet MCP tool surface for the LongHorizon-Harness queue and run control.

The LongHorizon-Harness Web API exposes four MCP tools under the ``lhharness``
LiteLLM MCP gateway alias so that fleet chat, Hydra, and curl clients can
enqueue tasks, list the queue, check a run, and resolve operator gates.  This
module is the server-side tool dispatcher: it validates JSON-RPC shaped requests,
calls the same ``QueueStore`` and ``RunSupervisor`` objects used by the REST
routes, and returns plain JSON results that an HTTP MCP wrapper can forward.

No native MCP server SDK is required.  The tools are advertised in
``GET /api/mcp/fleet/tools`` and invoked through ``POST
/api/mcp/fleet/{tool_name}``; a separate LiteLLM-compatible MCP bridge (not in
this repo) maps the gateway alias ``lhharness`` to those endpoints.
"""

from __future__ import annotations

import re
from typing import Any

# Descriptions exposed to clients and the gateway.  They carry the operating
# rules: scoped task text, trio usage, and lane policy.
_ENQUEUE_DESCRIPTION = """Enqueue a long-horizon run in the harness service.

Use this tool to ask the service to start a task. The service stores the task in
the durable queue, evaluates capacity from [queue.capacity], and launches it
through the same code path as POST /api/runs.

Rules:
- task text must be scoped: repository, branch, deliverables, and hard rules.
- trio "kimi" is for development work (claude_code backend). It requires at
  least min_healthy_keys healthy Ollama Cloud keys and counts against kimi_max.
- trio "qwen" is for QA only (codex backend) and is limited to one concurrent
  run (qwen_max=1).
- production deploys always go through deployment lanes, never through this
  queue tool.
"""

_LIST_QUEUE_DESCRIPTION = """List harness queue entries and their statuses.

Returns pending, launched, done, and failed entries with counts and skip
reasons. Hydra and chat clients can use this to show the current backlog.
"""

_RUN_STATUS_DESCRIPTION = """Return the current status of a harness run.

Use the run_id returned by harness_enqueue_task / harness_list_queue.
"""

_RESOLVE_GATE_DESCRIPTION = """Resolve an operator gate for a running harness run.

user_input must be plain ASCII. Any non-ASCII character is rejected because
MCP/chat clients cannot safely transmit formatting bytes through the gateway.
"""


def _is_ascii_only(value: str) -> bool:
    """Return True if every character in value is ASCII."""
    try:
        value.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _tool_spec(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description.strip(),
        "input_schema": {
            "type": "object",
            "properties": parameters,
        },
    }


# Shared string parameter shape.
def _string_param(description: str, required: bool = False) -> dict[str, Any]:
    spec: dict[str, Any] = {"type": "string", "description": description}
    if not required:
        spec["default"] = ""
    return spec


def _integer_param(description: str, default: int) -> dict[str, Any]:
    return {"type": "integer", "description": description, "default": default}


def tools_manifest() -> list[dict[str, Any]]:
    """Return the list of fleet MCP tools exposed by this service."""
    return [
        _tool_spec(
            "harness_enqueue_task",
            _ENQUEUE_DESCRIPTION,
            {
                "name": _string_param("Short human-readable task name.", required=True),
                "task": _string_param("Scoped task text. Provide repo, branch, deliverables, and hard rules.", required=True),
                "workspace": _string_param("Workspace directory path for the run.", required=True),
                "trio": _string_param("Resource trio: 'kimi' for dev, 'qwen' for QA only.", required=True),
                "max_rounds": _integer_param("Maximum harness rounds.", default=25),
                "priority": _integer_param("Higher number = earlier launch within the same trio.", default=0),
                "base_check": _string_param("Optional base commit/branch check guard.", required=False),
                "requested_by": _string_param("Fleet client identity, e.g. 'openwebui' or 'hydra'.", required=True),
            },
        ),
        _tool_spec(
            "harness_list_queue",
            _LIST_QUEUE_DESCRIPTION,
            {
                "status": _string_param("Filter by status: pending, launched, done, failed.", required=False),
            },
        ),
        _tool_spec(
            "harness_run_status",
            _RUN_STATUS_DESCRIPTION,
            {
                "run_id": _string_param("Run identifier returned by enqueue or list.", required=True),
            },
        ),
        _tool_spec(
            "harness_resolve_gate",
            _RESOLVE_GATE_DESCRIPTION,
            {
                "run_id": _string_param("Run whose gate should be resolved.", required=True),
                "approval_id": _string_param("Gate/approval identifier from the run snapshot.", required=True),
                "action": _string_param("Action chosen by the operator (continue/stop/etc).", required=True),
                "user_input": _string_param("ASCII-only operator message or instruction. Non-ASCII is rejected.", required=False),
                "reason": _string_param("Operator reason for the resolution.", required=False),
            },
        ),
    ]


def dispatch(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    queue_store: Any,
    registry: Any,
    supervisor: Any,
    auth_token: str | None,
    request_token: str | None,
) -> dict[str, Any]:
    """Run one MCP tool call and return a JSON-RPC style result.

    ``arguments`` is the tool's input object.  The dispatcher reuses the same
    validation and business logic as the REST routes, so clients get identical
    behavior whether they call via MCP, HTTP, or curl.
    """
    # Auth parity with the REST boundary.
    if auth_token is not None and auth_token != (request_token or ""):
        return {"ok": False, "error": "invalid or missing bearer token", "code": 401}

    if tool_name == "harness_enqueue_task":
        return _enqueue(arguments, queue_store=queue_store)
    if tool_name == "harness_list_queue":
        return _list_queue(arguments, queue_store=queue_store)
    if tool_name == "harness_run_status":
        return _run_status(arguments, registry=registry, supervisor=supervisor)
    if tool_name == "harness_resolve_gate":
        return _resolve_gate(arguments, registry=registry, supervisor=supervisor)
    return {"ok": False, "error": f"unknown tool {tool_name}", "code": 404}


def _bounded(value: Any, *, field: str, max_chars: int = 4096, required: bool = False) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ValueError(f"{field} must be a string")
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > max_chars:
        raise ValueError(f"{field} is too long")
    if "\x00" in text:
        raise ValueError(f"{field} contains a NUL byte")
    return text


def _enqueue(arguments: dict[str, Any], *, queue_store: Any) -> dict[str, Any]:
    if queue_store is None:
        return {"ok": False, "error": "queue requires a configured runs root", "code": 501}
    body = {
        "name": _bounded(arguments.get("name"), field="name", max_chars=256, required=True),
        "task": _bounded(arguments.get("task"), field="task", max_chars=100_000, required=True),
        "workspace": _bounded(arguments.get("workspace"), field="workspace", max_chars=4096, required=True),
        "trio": _bounded(arguments.get("trio"), field="trio", max_chars=64, required=True),
        "max_rounds": arguments.get("max_rounds", 25),
        "priority": arguments.get("priority", 0),
        "base_check": _bounded(arguments.get("base_check"), field="base_check", max_chars=4096),
        "requested_by": _bounded(arguments.get("requested_by"), field="requested_by", max_chars=256, required=True),
    }
    try:
        entry = queue_store.create(body)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "code": 422}
    return {"ok": True, "queue_id": entry.queue_id, "status": entry.status}


def _list_queue(arguments: dict[str, Any], *, queue_store: Any) -> dict[str, Any]:
    if queue_store is None:
        return {"ok": False, "error": "queue requires a configured runs root", "code": 501}
    status = _bounded(arguments.get("status"), field="status", max_chars=32) or None
    entries = queue_store.list()
    valid_statuses = {"pending", "launched", "done", "failed"}
    filtered = entries
    if status is not None and status in valid_statuses:
        filtered = [item for item in entries if item.status == status]
    return {
        "ok": True,
        "entries": [item.to_dict() for item in filtered],
        "counts": queue_store.counts(),
    }


def _run_status(arguments: dict[str, Any], *, registry: Any, supervisor: Any) -> dict[str, Any]:
    run_id = _bounded(arguments.get("run_id"), field="run_id", max_chars=128, required=True)
    state = registry.state_for(run_id)
    if state is None:
        return {"ok": False, "error": "run not found", "code": 404}
    if supervisor is None:
        return {"ok": True, "run_id": run_id, "status": "attached", "managed": False}
    managed_status = supervisor.status(run_id)
    owner = supervisor.owner(run_id)
    public_owner: dict[str, Any] = {}
    if isinstance(owner, dict):
        public_owner = {
            key: value for key, value in owner.items() if key not in {"token", "api_key", "auth"}
        }
    return {
        "ok": True,
        "run_id": run_id,
        "managed": managed_status.get("managed") is not False,
        **managed_status,
        "owner": public_owner,
    }


def _resolve_gate(arguments: dict[str, Any], *, registry: Any, supervisor: Any) -> dict[str, Any]:
    run_id = _bounded(arguments.get("run_id"), field="run_id", max_chars=128, required=True)
    approval_id = _bounded(arguments.get("approval_id"), field="approval_id", max_chars=128, required=True)
    state = registry.state_for(run_id)
    if state is None:
        return {"ok": False, "error": "run not found", "code": 404}
    if supervisor is not None and not supervisor.can_control(run_id):
        return {"ok": False, "error": "run is not accepting approvals", "code": 409}

    action = _bounded(arguments.get("action"), field="action", max_chars=128, required=True)
    reason = _bounded(arguments.get("reason"), field="reason", max_chars=10_000)
    user_input = _bounded(arguments.get("user_input"), field="user_input", max_chars=50_000)

    # ASCII-only enforcement is required for gateway/chat safety.
    if user_input and not _is_ascii_only(user_input):
        return {
            "ok": False,
            "error": "user_input must be ASCII-only; non-ASCII characters are rejected",
            "code": 422,
        }

    try:
        ok = state.resolve_approval(
            approval_id,
            action=action,
            reason=reason,
            user_input=user_input,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc), "code": 409}
    if not ok:
        return {"ok": False, "error": "approval is missing, resolved, or read-only", "code": 409}
    return {"ok": True, "approval_id": approval_id, "status": "accepted"}


def normalize_request_token(authorization: str | None) -> str | None:
    """Extract the bare bearer token from an Authorization header value."""
    if not authorization:
        return None
    parts = str(authorization).split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None
