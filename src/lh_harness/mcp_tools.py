"""Fleet MCP tool surface for the LongHorizon-Harness queue and run control.

The LongHorizon-Harness Web API exposes four MCP tools under the ``lhharness``
LiteLLM MCP gateway alias so that fleet chat, Hydra, and curl clients can
enqueue tasks, list the queue, check a run, and resolve operator gates.  This
module is the server-side tool dispatcher: it validates JSON-RPC shaped requests,
calls the same ``QueueStore`` and ``RunSupervisor`` objects used by the REST
routes, and returns plain JSON results that an HTTP MCP wrapper can forward.

No native MCP server SDK is required.  The tools are advertised in
``GET /api/mcp/fleet/tools`` and invoked through ``POST
/api/mcp/fleet/{tool_name}``.  The WebAPI also serves them as a real MCP
server over streamable HTTP at ``POST /mcp`` (``webapi/mcp_jsonrpc.py``), so
LiteLLM can register the gateway alias ``lhharness`` directly; both transports
share this module's manifest and dispatcher.

LHH-QUEUE-HOTRELOAD (R5): four queue admin tools -- ``list_trios``,
``set_trio_model``, ``set_trio_capacity``, ``refresh_models`` -- expose the
settings-DB queue trios/capacity the launcher re-reads every scheduling pass,
so Overseer1 can change the trio model or capacity at runtime with no restart
and no redeploy.  They reuse the R4 admin-route machinery
(``webapi/settings_routes._clean_trio_row``, the store's ``_db``/``_audit``,
and the same injectable model-list fetch seam) so validation, error
semantics, and the per-change before/after audit rows match the REST admin
API exactly.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

from .caller_auth import (
    APPROVED_BY_MAX_CHARS,
    APPROVAL_CHANNELS,
    GATEWAY_KEY_IDENTITY_SOURCE_PREFIX,
    PERSONA_CHANNELS,
    SERVICE_APPROVED_BY_PREFIX,
    validate_resolve_identity,
    ANON_CALLER,
    RESOLVE_TOOL,
    ask_caller_specs,
    ask_scope_allowed,
    emit_caller_audit,
    strip_ask_signature,
    verify_ask_request,
    caller_configs_or_defaults,
    emit_refusal_audit,
    enqueue_rate_violation,
    resolve_mcp_caller,
    rounds_clamp_violation,
    strip_caller_arguments,
    tool_allowed,
)
from .config import _MCP_DISPATCH_TOOL_NAMES as _KNOWN_TOOLS
from .config import ASK_APPLY_TARGETS, ASK_TOOL_SCOPES
from .config import _MCP_TOOL_NAMES as _SCOPING_ELIGIBLE_TOOLS
from .contention import detect_contention, groups_to_json
from .workspace_identity import resolve_many

# LHH-QUEUE-HOTRELOAD R5: the queue admin tools.  They are known to THIS
# dispatcher but deliberately not part of config.py's static tool sets: those
# sets also validate the task-174 ``[callers.*.tools]`` allowlists, so a name
# added there becomes grantable per caller -- a config-surface change this
# round must not make.  Like the task-A3d ask tools (which live outside
# ``_MCP_TOOL_NAMES`` for the same reason), the queue admin tools verify the
# caller identity themselves with the same HMAC machinery before any write,
# and the verified caller becomes the settings-store audit actor.
_QUEUE_ADMIN_TOOL_NAMES = frozenset(
    {
        "list_trios",
        "set_trio_model",
        "set_trio_capacity",
        "refresh_models",
    }
)
# Local dispatcher view: config's known set plus the tools above.  Only the
# unknown-tool 404 check below reads this name; config.py stays untouched.
_KNOWN_TOOLS = _KNOWN_TOOLS | _QUEUE_ADMIN_TOOL_NAMES

# Descriptions exposed to clients and the gateway.  They carry the operating
# rules: scoped task text, trio usage, and lane policy.
_ENQUEUE_DESCRIPTION = """Enqueue a long-horizon run in the harness service.

Use this tool to ask the service to start a task. The service stores the task in
the durable queue, evaluates capacity from [queue.capacity], and launches it
through the same code path as POST /api/runs.

Accepted fields (all persisted on the queue entry):
- name (string, required): short human-readable task name.
- task (string, required): scoped task text. Provide repo, branch,
  deliverables, and hard rules.
- workspace (string, required): workspace directory path for the run.
- trio (string, optional): resource trio, 'kimi' for development work,
  'qwen' for QA only, or 'glm' / 'zai' / 'zaiq' on nodes whose config
  defines them. No default trio is applied at enqueue time. When
  omitted, the entry persists with trio unset and never launches until a
  trio is set: remaining capacity is derived only from the configured
  trios (launcher.py _remaining_capacity), so each launcher pass records
  an ' at capacity' skip for the entry (launcher.py _check_eligibility)
  and it stays pending, unlaunched, consuming no capacity; _launch and
  _shadow_launch_decision guard the same condition with an 'unknown trio'
  skip. Supply 'kimi', 'qwen', 'glm', 'zai', 'zaiq' or 'minimax' whenever the task should actually
  launch.
- max_rounds (integer, optional): maximum harness rounds (default 25).
- priority (integer, optional): higher number = earlier launch within the
  same trio (default 0).
- continue_branch (boolean, optional): continuation opt-in. When true the
  launcher uses whatever branch the workspace currently has checked out
  as-is (no relocation, no stash, no open-PR refusal). Mutually exclusive
  with branch.
- branch (string, optional): continuation opt-in naming a specific branch
  to use as-is. Mutually exclusive with continue_branch.
- dedup_key (string, optional): idempotent-enqueue key. Two enqueues with
  the same non-terminal key resolve to one queue entry, so the work is
  launched at most once; the key frees up once the entry reaches a
  terminal state (done/failed).
- requested_by (string, required): fleet client identity, e.g. 'openwebui'
  or 'hydra'. With per-caller scoping (task 174) the server stamps it from
  the verified caller identity; a client-supplied value is overridden.
- base_check (string, optional): base commit/branch check guard.
- model (string, optional): per-task model override. Must be on the
  backend's live model list (the list list_trios/refresh_models show);
  unknown models are refused and nothing is enqueued, and an unreachable
  provider refuses the enqueue rather than writing it unvalidated. Editable
  while the entry is pending; resolved and locked onto the run at launch.
- roles (string or object, optional): a STRING is the legacy trio alias
  (identical to trio). An OBJECT carries per-role configuration with the
  create-run API's semantics: {"manager"|"executor"|"auditor": {agent?,
  model?, reasoning_effort?, mcp_profile?}} -- only the fields you pin
  override the trio defaults; unknown roles, fields, agents, or malformed
  values are refused at enqueue. Editable while pending, locked at launch.

- requester (object, required): structured identity block (task 300). Send
  kind, name, and host at minimum. When kind is 'ai' you must also send agent
  (claude, codex, cursor, openwebui, hydra, chat-overseer, or any
  [A-Za-z0-9._-]{1,64}), session_id (full session id, <=128), and cwd (your
  working directory, <=4096, no NUL). session_ref and notify are optional.
  The block is validated and normalized by the store; legacy and observed_addr
  are stamped server-side and must not be set.

Unknown fields are rejected with an error naming them; they are never
dropped silently.

Rules:
- task text must be scoped: repository, branch, deliverables, and hard rules.
- trio "kimi" is for development work (claude_code backend). It requires at
  least min_healthy_keys healthy Ollama Cloud keys and counts against kimi_max.
- trio "qwen" is for QA only (codex backend) and is limited to one concurrent
  run (qwen_max=1).
- trio "minimax" runs dev work on the MiniMax M Plan subscription (minimax_max,
  default 2). An additional lane; launches only on a node whose config defines
  [queue.trios.minimax].
- trio "zaiq" is the MIXED lane: manager and auditor on z.ai, executor on the
  local qwen lane (zaiq_max, default 1; shares the local lanes with qwen).
  Launches only on a node whose config defines [queue.trios.zaiq].
- trio "zai" runs dev work on the direct z.ai coding plan (zai_max, default 5).
  It is an additional option and never replaces another trio. It launches only
  on a node whose config defines [queue.trios.zai]; elsewhere the entry stays
  pending.
- trio "glm" runs dev work on Ollama Cloud glm-5.3 (glm_max, default 1). It
  launches only on a node whose config defines [queue.trios.glm]; elsewhere
  the entry stays pending.
- production deploys always go through deployment lanes, never through this
  queue tool.
- second checkouts are permitted and never blocked, but raise a warning naming
  the peer workspaces; the caller should confirm the sibling tree is not mid-PR
  on the same branch.
"""

_LIST_QUEUE_DESCRIPTION = """List harness queue entries and their statuses.

Returns a SUMMARY page by default (newest first, 50 entries): every field except the
task text, which is replaced by task_preview (first 200 chars) and task_bytes, plus
counts. Filter with status (comma list), name (substring) and trio; page with limit
(max 500) and after (the next_cursor of the previous page). Pass queue_id to get one
full entry including its task text, or view=full for the legacy full listing.
"""

_RUN_STATUS_DESCRIPTION = """Return the current status of a harness run.

Use the run_id returned by harness_enqueue_task / harness_list_queue.
"""

_RESOLVE_GATE_DESCRIPTION = """Resolve an operator gate for a running harness run.

Every resolve records who approved and through which channel: approved_by is
exactly one of upn:<user@domain>, slack:<Uxxxx>, terminal:<os user>@<host>
or the unattributed service marker service:<caller alias> (with channel
"service"), and channel is one of fleet-chat, slack-dm, terminal, web-sso,
service. Model-supplied text can never produce a service: value -- the
transport sets it from the verified caller identity. A resolve claiming
fleet-chat or web-sso without a verified signed-in identity is refused as a
relayed approval.

user_input must be plain ASCII. Any non-ASCII character is rejected because
MCP/chat clients cannot safely transmit formatting bytes through the gateway.
"""

_LIST_CONTENTIONS_DESCRIPTION = """List current workspace contentions reported by the launcher.

Returns grouped overlaps so AI clients can warn operators before they start
work in a sibling tree. Second checkouts are permitted and never blocked, but
raise a warning naming the peers; confirm the sibling tree is not mid-PR on the
same branch.
"""

# Task 235: read-only overseer-state tools over the migrated apparatus archive
# (README-OVERSEER-APPARATUS.md). They answer "what is the overseer working on,
# what did it decide, what is waiting on Paxton" without any fleet-host
# filesystem read.

_GET_QUEUE_ENTRY_DESCRIPTION = """Return the full task text and notes for one overseer queue record.

Reads the migrated overseer apparatus (task 104b): the queue entry snapshot
filed at launch (queue/done or queue/blocked) plus the task brief it points
at (tasks/<name>-task.txt in this repository). Pass the record_name, the
entry name, or its run_id. The note field carries the overseer's triage and
block/skip reasons verbatim. Read-only; never touches a fleet host.
"""

_LIST_QUEUE_DESCRIPTION_OVERSEER = """List the overseer's migrated queue archive with skip reasons.

Returns done and blocked queue records (the pre-cutover overseer queue)
newest launch first, each carrying its verbatim note - the note is where
skip and block reasons live. Filter with status (done/blocked/all), a query
substring, and a limit. Read-only over the in-repo apparatus archive.
"""

_GET_TASK_HISTORY_DESCRIPTION = """Return every archived run for a task number or name.

Searches the migrated overseer queue archive (queue/done + queue/blocked) by
task number, name fragment, or run_id, newest first. Each run carries its
name, workspace, trio, launch time, and a note excerpt; use get_queue_entry
for the full task text and note. This is how a chat client finds out whether
task N ever ran and what happened, without reading a fleet filesystem.
"""

_READ_LEDGER_DESCRIPTION = """Read recent rows from the overseer deployment ledger.

Returns sections from docs/LEDGER.md (one section per overseer tick, newest
last in the file; this tool returns the newest N by default). Filter with a
query substring. The ledger is the written record of what the scheduled
overseer observed and decided, tick by tick, up to the cutover.
"""

_LIST_OPEN_ASKS_DESCRIPTION = """List what waits on Paxton: the open-asks rows.

Live rows first (one per pending gate, one per blocked queue entry), then the
CT110 ask store (asks raised with raise_open_ask and every row someone
responded to), then the archived queue/OPEN-ASKS.md rows. Each row carries
its response fields and the saved response; secret fields show metadata only
(set_at, set_by, length), never a value. By default only open rows are
returned; pass include_closed=true to see the answered/closed rows too.
"""

# Task A3d: the writable CT110 ask store. Every one of these tools needs a
# verified caller (HMAC caller/caller_ts/caller_sig) holding the tool's scope
# in [asks.grants]; nothing is granted by default.
_RAISE_OPEN_ASK_DESCRIPTION = """Raise a new open ask in the CT110 ask store (scope: overseer).

Use this for anything that waits on Paxton and is not a gate or a blocked
queue entry (those are derived live). Give the seven columns (id, ask, kind,
evidence, recommended, default_if_silent; state starts OPEN) and optionally
fields. Without fields the row gets one body field 'response', plus one secret
field 'secret' when kind is CREDENTIAL.
"""

_DECLARE_ASK_FIELDS_DESCRIPTION = """Set or replace the response fields of an ask (scope: overseer).

fields is a list of {name, type: body|secret, label, required}. Works on store
asks, live GATE/BLOCKED rows and archive rows (copied into the store on first
write). Removing a secret field deletes its sealed value.
"""

_RESPOND_OPEN_ASK_DESCRIPTION = """Submit a response to an ask (scope: overseer:write, fleet-admin only).

{id, actor, fields: {name: value}}. Body values are stored, secret values are
sealed on CT110 and only ever reported as metadata. The row closes at once as
'CLOSED <ts> by <actor> (web)'; a submit to a closed row reopens it first.
A response to a GATE row never resolves the gate.
"""

_CLEAR_ASK_SECRET_DESCRIPTION = """Delete one sealed secret of an ask (scope: overseer:write)."""

_APPLY_ASK_SECRET_DESCRIPTION = """Apply a sealed secret to an allow-listed target (scope: overseer:apply).

{id, field, target}. target must be on the static allow-list (today only
ct202-mcp-tools-env:GITHUB_MCP_TOKEN). Returns {applied, target, at} and
never the value. A failure reopens the ask with the reason in evidence.
"""

_GET_HANDOFF_DESCRIPTION = """Return an operator handoff written during the overseer era.

Reads docs/handoffs/HANDOFF-*.md from the migrated apparatus archive. With no
argument the most recent handoff is returned; pass a filename (or unique
substring) to pick one. Each handoff is a point-in-time statement of fact,
not a live status - newer handoffs supersede older ones on the same topic.
"""

# LHH-QUEUE-HOTRELOAD R5: queue trio/capacity hot-edits over MCP.  These
# tools read and write the SAME settings-DB tables the R4 admin routes
# (GET/PUT /api/queue/trios, /api/queue/capacity, POST /api/models/refresh)
# manage, through the same store and validation helpers, so a change made
# here is exactly the change the web admin page makes -- the launcher picks
# it up on its next scheduling pass with no restart, and every change lands
# in the settings audit log keyed to the verified MCP caller.
_LIST_TRIOS_DESCRIPTION = """List the queue trios and per-trio capacity from the settings DB.

Returns the same state as GET /api/queue/trios plus GET /api/queue/capacity:
every trio row (name, agent, model, mcp_profile, enabled, updated_by/at) and
the per-trio max_concurrent map.  This is the live state the launcher
re-reads on every scheduling pass, not the config.toml bootstrap.
"""

_SET_TRIO_MODEL_DESCRIPTION = """Change one queue trio's model, live.

The trio keeps its agent, mcp_profile, and enabled flag; only the model
changes.  The model must be on the backend's LIVE model list for the trio's
agent (the same list the workbench dropdowns show): an unknown model is
refused and nothing changes, and if the live list cannot be fetched the
write is refused rather than made unvalidated.  Each accepted change writes
one settings-audit row with the before/after row, keyed to the verified MCP
caller, identical to PUT /api/queue/trios.  The launcher applies the new
model to the next launch; running runs keep the model they started with.

Protected trios (Paxton's standing rule, default ``["kimi", "glm", "qwen"]``):
a write that targets a protected trio returns ``{ok: false, code: 409,
error: "trio '<name>' is protected: ..."}`` unless ``force: true`` is
supplied AND the verified MCP caller is on the SSO admin list.  Force alone
is not enough; a non-admin caller with force still gets 409/403.  Capacity
edits on protected trios are NOT refused (use ``set_trio_capacity``).
"""

_SET_TRIO_CAPACITY_DESCRIPTION = """Change one trio's max_concurrent, live.

The trio must exist in the settings DB and the value must be a non-negative
integer (0 pauses new launches for the trio).  Unknown trios and bad values
are refused and nothing changes; each accepted change writes one
settings-audit row with the before/after value, keyed to the verified MCP
caller, identical to PUT /api/queue/capacity.  The launcher's next
scheduling pass respects the new cap immediately.
"""

_REFRESH_MODELS_DESCRIPTION = """Re-fetch the live provider model list now.

Forces a fresh model discovery (the same refresh POST /api/models/refresh
performs for the workbench) instead of the briefly cached list, and returns
the per-agent model list it sees.  Read-only: nothing in the settings store
changes, so no audit row is written.  Fails when the provider is
unreachable.
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
            # Reject unknown properties instead of dropping them silently
            # (task 233): a chat client misspelling a field must get an error
            # naming it, not a task filed without that field.
            "additionalProperties": False,
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


def _boolean_param(description: str, default: bool) -> dict[str, Any]:
    return {"type": "boolean", "description": description, "default": default}


def _requester_param() -> dict[str, Any]:
    """MCP schema for the task-300 requester identity block.

    A nested object (``type: object``) with ``additionalProperties: false`` so
    a chat client cannot smuggle a sub-field the store does not recognize.  The
    block is required on every enqueue (strict mode); its sub-fields are
    described here for the client, while the store performs the real validation
    (required kind/name/host, kind=ai implies agent/session_id/cwd, the hex
    session_ref derivation, and the store/API-set-only rejection of legacy and
    observed_addr).
    """
    return {
        "type": "object",
        "description": (
            "Structured identity for who requested this task. Required on every "
            "enqueue. Send kind, name, and host at minimum. When kind is 'ai' you "
            "must also send agent (client token: claude, codex, cursor, openwebui, "
            "hydra, chat-overseer, or any [A-Za-z0-9._-]{1,64}) and session_id (full "
            "session id, <=128 chars), and cwd (your working directory, <=4096, no "
            "NUL). omit notify (defaults to 'none'). Do not set legacy or observed_addr: "
            "those are stamped server-side."
        ),
        "additionalProperties": False,
        "properties": {
            "kind": {"type": "string", "description": "One of: user, ai, service. 'unknown' is store-only.", "enum": ["user", "ai", "service"]},
            "name": {"type": "string", "description": "Username, person or agent/client name. <=128 chars, no NUL.", "maxLength": 128},
            "host": {"type": "string", "description": "Device/hostname (PTAIT09, ct110, htpc01). <=128 chars.", "maxLength": 128},
            "address": {"type": "string", "description": "Optional IP or similar the caller claims. <=128 chars.", "maxLength": 128},
            "cwd": {"type": "string", "description": "Required when kind is 'ai'. Requester working directory. <=4096 chars, no NUL.", "maxLength": 4096},
            "agent": {"type": "string", "description": "Required when kind is 'ai'. Client token: claude, codex, cursor, openwebui, hydra, chat-overseer, or any [A-Za-z0-9._-]{1,64}.", "pattern": "^[A-Za-z0-9._-]{1,64}$", "maxLength": 64},
            "session_id": {"type": "string", "description": "Required when kind is 'ai'. Full session id (Claude Code UUID, Codex/Cursor id). <=128 chars.", "maxLength": 128},
            "session_ref": {"type": "string", "description": "Optional; when absent and session_id looks like a UUID/hex, the first 6 hex chars are derived."},
            "notify": {"type": "string", "description": "Optional free-form string, where updates should go (e.g. 'handoff:/tmp/x.md', 'sendmessage:overseer1', 'open-asks', 'none'). Default 'none'. <=512 chars.", "maxLength": 512},
        },
    }


def _fields_param() -> dict[str, Any]:
    """Schema for an ask's declared response fields (task A3d)."""
    return {
        "type": "array",
        "description": "Response fields: [{name, type: body|secret, label, required}].",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,31}$"},
                "type": {"type": "string", "enum": ["body", "secret"]},
                "label": {"type": "string", "maxLength": 120},
                "required": {"type": "boolean"},
                "apply_target": {
                    "type": "string",
                    "description": "Secret fields only: the allow-listed target apply_ask_secret may write this value to.",
                    "enum": sorted(ASK_APPLY_TARGETS),
                },
            },
            "required": ["name", "type"],
        },
        "maxItems": 8,
    }


def _optional_integer_param(description: str) -> dict[str, Any]:
    """Integer parameter the caller may omit entirely (no filled-in default)."""
    return {"type": "integer", "description": description}


def tools_manifest(*, caller_scoped: bool = True) -> list[dict[str, Any]]:
    """Return the list of fleet MCP tools exposed by this service.

    ``caller_scoped`` (task 174) only shapes the ``requested_by`` hint: with
    per-caller scoping the server stamps it from the verified caller, so the
    schema advertises it as deprecated/ignored.  Without scoping the task-233
    store contract applies and the client value is required.
    """
    requested_by_hint = (
        _string_param(
            "Deprecated: stamped server-side from your verified caller identity.",
            required=False,
        )
        if caller_scoped
        else _string_param("Fleet client identity, e.g. 'openwebui' or 'hydra'.", required=True)
    )
    return [
        _tool_spec(
            "harness_enqueue_task",
            _ENQUEUE_DESCRIPTION,
            {
                "name": _string_param("Short human-readable task name.", required=True),
                "task": _string_param("Scoped task text. Provide repo, branch, deliverables, and hard rules.", required=True),
                "workspace": _string_param("Workspace directory path for the run.", required=True),
                "trio": _string_param(
                    "Resource trio: 'kimi' for dev, 'qwen' for QA only, 'glm' for "
                    "Ollama Cloud glm-5.3 dev work where the node defines it. "
                    "Optional: omit to store the entry with trio unset; the "
                    "launcher then skips it as 'unknown trio' until a trio "
                    "is set. No default trio is applied at enqueue time.",
                ),
                "max_rounds": _optional_integer_param("Maximum harness rounds."),
                "priority": _optional_integer_param("Higher number = earlier launch within the same trio."),
                "continue_branch": _boolean_param(
                    "Continuation opt-in: use whatever branch the workspace has checked out as-is. Mutually exclusive with branch.",
                    default=False,
                ),
                "branch": _string_param(
                    "Continuation opt-in naming the branch to use as-is. Mutually exclusive with continue_branch.",
                ),
                "dedup_key": _string_param(
                    "Idempotent-enqueue key: same non-terminal key resolves to one queue entry, launched at most once.",
                ),
                # LHH-QUEUE-HOTRELOAD (R6): per-task launch overrides.  Both
                # are forwarded only when actually supplied (an explicit null
                # stays unforwarded, matching the pre-R6 body); both are
                # validated by the store like every other field.
                "model": _string_param(
                    "Per-task model override: one model id from the backend's live model list "
                    "(unknown ids and an unreachable provider are both refused). Editable while "
                    "pending, locked onto the run at launch.",
                    required=False,
                ),
                "roles": {
                    "description": (
                        "A STRING is the legacy trio alias (same as trio). An OBJECT carries per-role "
                        "configuration (create-run API semantics): {'manager'|'executor'|'auditor': "
                        "{'agent'?, 'model'?, 'reasoning_effort'?, 'mcp_profile'?}}; only pinned fields "
                        "override the trio defaults. Editable while pending, locked at launch."
                    ),
                    "anyOf": [{"type": "string"}, {"type": "object"}],
                },
                "base_check": _string_param("Optional base commit/branch check guard.", required=False),
                "time_limit_minutes": _optional_integer_param(
                    "Optional wall-clock limit in minutes (1-1440) from launch; the launcher stops the run "
                    "when it is exceeded and does not requeue it (task fc-H2)."
                ),
                # With scoping ON the stamp wins (task 174); the field stays in
                # the schema because the store's own contract (task 233)
                # requires it -- the value a client passes is overridden, never
                # trusted.
                "requested_by": requested_by_hint,
                # Task 300: the structured requester identity block.  Required
                # on every enqueue (strict mode).  It is a nested object with
                # additionalProperties=false; the store validates and normalizes
                # it, stamping observed_addr/verified_caller server-side.
                "requester": _requester_param(),
            },
        ),
        _tool_spec(
            "harness_list_queue",
            _LIST_QUEUE_DESCRIPTION,
            {
                "status": _string_param("Filter by status (comma list): pending, launched, done, failed, blocked.", required=False),
                "name": _string_param("Only entries whose name contains this text (case-insensitive).", required=False),
                "trio": _string_param("Only entries for this trio (e.g. qwen, kimi, glm, zai, zaiq, minimax).", required=False),
                "limit": _string_param("Page size, 1-500 (default 50).", required=False),
                "after": _string_param("Cursor from the previous page's next_cursor.", required=False),
                "queue_id": _string_param("Return this one entry in full (task text included).", required=False),
                "view": _string_param("summary (default) or full (legacy: every entry with task text).", required=False),
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
                "approved_by": _string_param(
                    "Who approved: upn:<user@domain>, slack:<Uxxxx>, terminal:<os user>@<host> "
                    "or service:<caller alias> (channel service only). Required; max 254 chars.",
                    required=True,
                ),
                "channel": _string_param(
                    "How the approval arrived: fleet-chat, slack-dm, terminal, web-sso or service. Required.",
                    required=True,
                ),
                "user_input": _string_param("ASCII-only operator message or instruction. Non-ASCII is rejected.", required=False),
                "reason": _string_param("Operator reason for the resolution.", required=False),
            },
        ),
        _tool_spec(
            "harness_list_contentions",
            _LIST_CONTENTIONS_DESCRIPTION,
            {},
        ),
        _tool_spec(
            "get_queue_entry",
            _GET_QUEUE_ENTRY_DESCRIPTION,
            {
                "name": _string_param(
                    "Record name: the record_name file stem, the entry name, or the run_id.",
                    required=True,
                ),
            },
        ),
        _tool_spec(
            "list_queue",
            _LIST_QUEUE_DESCRIPTION_OVERSEER,
            {
                "status": _string_param(
                    "Filter by archived status: done, blocked, or all (default all).",
                    required=False,
                ),
                "query": _string_param(
                    "Optional substring match on record name, entry name, run_id, or task_file.",
                    required=False,
                ),
                "limit": _integer_param("Maximum entries to return (1-500).", 100),
            },
        ),
        _tool_spec(
            "get_task_history",
            _GET_TASK_HISTORY_DESCRIPTION,
            {
                "task": _string_param(
                    "Task number, name fragment, or run_id to search for.", required=True
                ),
                "limit": _integer_param("Maximum runs to return (1-500).", 100),
            },
        ),
        _tool_spec(
            "read_ledger",
            _READ_LEDGER_DESCRIPTION,
            {
                "limit": _integer_param(
                    "Maximum ledger rows to return (1-500). Default 20.", 20
                ),
                "query": _string_param(
                    "Optional substring filter on heading or body.", required=False
                ),
            },
        ),
        _tool_spec(
            "list_open_asks",
            _LIST_OPEN_ASKS_DESCRIPTION,
            {
                "include_closed": _boolean_param(
                    "Include closed/answered rows too.", default=False
                ),
                "limit": _integer_param("Maximum rows to return (1-500).", 100),
            },
        ),
        _tool_spec(
            "raise_open_ask",
            _RAISE_OPEN_ASK_DESCRIPTION,
            {
                "id": _string_param("New ask id, [A-Za-z0-9][A-Za-z0-9._-]{0,127}; not gate-/blocked-.", required=True),
                "ask": _string_param("What is being asked (markdown).", required=True),
                "kind": _string_param("Upper-case kind, e.g. CREDENTIAL or DECISION.", required=True),
                "evidence": _string_param("Links and facts behind the ask."),
                "recommended": _string_param("The overseer's recommendation."),
                "default_if_silent": _string_param("What happens if nobody answers."),
                "fields": _fields_param(),
            },
        ),
        _tool_spec(
            "declare_ask_fields",
            _DECLARE_ASK_FIELDS_DESCRIPTION,
            {
                "id": _string_param("Ask id.", required=True),
                "fields": _fields_param(),
            },
        ),
        _tool_spec(
            "respond_open_ask",
            _RESPOND_OPEN_ASK_DESCRIPTION,
            {
                "id": _string_param("Ask id.", required=True),
                "actor": _string_param("SSO e-mail of the person submitting.", required=True),
                "fields": {
                    "type": "object",
                    "description": "Map of field name to value (strings).",
                    "additionalProperties": {"type": "string"},
                },
                "secret_targets": {
                    "type": "object",
                    "description": "Every secret field submitted, mapped to the apply target shown to the person ('' when none). Recorded in the vault with the value.",
                    "additionalProperties": {"type": "string"},
                },
            },
        ),
        _tool_spec(
            "clear_ask_secret",
            _CLEAR_ASK_SECRET_DESCRIPTION,
            {
                "id": _string_param("Ask id.", required=True),
                "field": _string_param("Secret field name.", required=True),
                "actor": _string_param("SSO e-mail of the person clearing it.", required=True),
            },
        ),
        _tool_spec(
            "apply_ask_secret",
            _APPLY_ASK_SECRET_DESCRIPTION,
            {
                "id": _string_param("Ask id.", required=True),
                "field": _string_param("Secret field name.", required=True),
                "target": {
                    "type": "string",
                    "description": "Allow-listed target name.",
                    "enum": sorted(ASK_APPLY_TARGETS),
                },
            },
        ),
        _tool_spec(
            "get_handoff",
            _GET_HANDOFF_DESCRIPTION,
            {
                "name": _string_param(
                    "Handoff filename or unique substring. Omit for the most recent.",
                    required=False,
                ),
            },
        ),
        # LHH-QUEUE-HOTRELOAD R5: queue admin tools (settings-DB trios and
        # capacity; the launcher's per-pass reads make changes live).
        _tool_spec(
            "list_trios",
            _LIST_TRIOS_DESCRIPTION,
            {},
        ),
        _tool_spec(
            "set_trio_model",
            _SET_TRIO_MODEL_DESCRIPTION,
            {
                "trio": _string_param(
                    "Queue trio name as listed by list_trios (e.g. 'glm').",
                    required=True,
                ),
                "model": _string_param(
                    "Model id from the backend's live model list for the trio's agent.",
                    required=True,
                ),
                # LHH-QUEUE-PROTECT-DOCS (F3): the explicit override flag,
                # honoured only when the verified caller is on the SSO
                # admin list (force alone is not enough).  Defaults to
                # false; a stray truthy value still needs an admin caller.
                "force": _boolean_param(
                    "Override the protected-trio refusal (SSO admins only).",
                    default=False,
                ),
            },
        ),
        _tool_spec(
            "set_trio_capacity",
            _SET_TRIO_CAPACITY_DESCRIPTION,
            {
                "trio": _string_param(
                    "Queue trio name as listed by list_trios (e.g. 'glm').",
                    required=True,
                ),
                # No default: the value is required; the handler validates
                # presence and range (non-negative integer).
                "max_concurrent": {
                    "type": "integer",
                    "description": "Required. New max_concurrent for the trio (non-negative integer; 0 pauses new launches).",
                },
            },
        ),
        _tool_spec(
            "refresh_models",
            _REFRESH_MODELS_DESCRIPTION,
            {},
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
    caller_configs: dict[str, dict[str, Any]] | None = None,
    overseer_root: str | None = None,
    ask_runtime: Any = None,
    settings_runtime: Any = None,
    model_fetch: Any = None,
) -> dict[str, Any]:
    """Run one MCP tool call and return a JSON-RPC style result.

    ``arguments`` is the tool's input object.  The dispatcher reuses the same
    validation and business logic as the REST routes, so clients get identical
    behavior whether they call via MCP, HTTP, or curl.

    Beyond the bearer-parity check, every call is attributed to a caller: the
    arguments carry ``caller``/``caller_ts``/``caller_sig`` and the HMAC over
    ``(caller, ts)`` is verified against the per-caller secret named in
    ``caller_configs`` (task 174).  Missing or bad identity resolves to the
    reserved ``"anon"`` caller -- 401 on every tool; a verified caller outside
    its allowlist gets 403, and a refused gate resolution also emits an audit
    event with ``{caller, tool, run_id, decision}``.

    ``overseer_root`` (task 235) is the resolved apparatus archive root; the
    WebAPI resolves it once at app creation and passes it here so the
    overseer-state tools share the deployment's archive path.

    ``settings_runtime``/``model_fetch`` (LHH-QUEUE-HOTRELOAD R5) are the
    injectable seams of the queue admin tools; R6 reuses the same pair for
    ``harness_enqueue_task`` (live-list validation of a per-task ``model``
    override and the settings-audit row behind override-carrying enqueues).
    The WebAPI passes neither: the tools and the enqueue path then default to
    ``settings_store.active_runtime()`` (the same store the R4 admin routes
    default to) and ``model_catalog.discover_model_catalog`` (the same
    live-list fetch the R4 routes validate against), so tests can run fully
    offline.
    """
    # Auth parity with the REST boundary.
    if auth_token is not None and auth_token != (request_token or ""):
        return {"ok": False, "error": "invalid or missing bearer token", "code": 401}

    # A name that is not a tool at all stays 404 regardless of identity: the
    # allowlist decides refusal for tools that exist, and an unknown name has
    # nothing to authorize.  The known-tool set is the full dispatch surface
    # (task 174 + the task-235 overseer-state tools).
    if tool_name not in _KNOWN_TOOLS:
        return {"ok": False, "error": f"unknown tool {tool_name}", "code": 404}

    # Task A3d: ask-store tools. Always identity-checked (request-bound
    # signature) and scope-gated, independent of whether [callers] scoping is
    # ON for the other tools.
    if tool_name in ASK_TOOL_SCOPES:
        return _dispatch_ask_tool(
            tool_name,
            arguments,
            caller_configs=caller_configs,
            runtime=ask_runtime,
            audit_root=_runs_root(registry, supervisor) or getattr(queue_store, "runs_root", None),
            seed_lookup=lambda ask_id: _ask_seed(
                ask_id,
                overseer_root=overseer_root,
                registry=registry,
                supervisor=supervisor,
                queue_store=queue_store,
            ),
        )

    # LHH-QUEUE-HOTRELOAD R5: queue trio/capacity admin tools.  Dispatched
    # like the ask tools -- before the scoping-eligible/fallback split --
    # because identity is verified by the tool itself (the config-validated
    # task-174 allowlists can never name these tools; see the
    # _QUEUE_ADMIN_TOOL_NAMES note above).
    if tool_name in _QUEUE_ADMIN_TOOL_NAMES:
        return _dispatch_queue_admin_tool(
            tool_name,
            arguments,
            caller_configs=caller_configs,
            settings_runtime=settings_runtime,
            model_fetch=model_fetch,
        )

    # Task 235: the read-only overseer-state tools are called from the
    # authenticated overseer session, which carries no caller identity block.
    # They are not scoping-eligible (config keeps them out of ``tools``
    # validation) so they dispatch before the identity check.  Everything in
    # the scoping-eligible task-174 set still requires a verified caller.
    if tool_name not in _SCOPING_ELIGIBLE_TOOLS:
        overseer_tools = {
            "get_queue_entry": _overseer_get_queue_entry,
            "list_queue": _overseer_list_queue,
            "get_task_history": _overseer_get_task_history,
            "read_ledger": _overseer_read_ledger,
            "list_open_asks": _overseer_list_open_asks,
            "get_handoff": _overseer_get_handoff,
        }
        handler = overseer_tools.get(tool_name)
        if handler is not None:
            if tool_name == "list_open_asks":
                # Task A3: open asks are derived live (gates, blocked queue
                # entries) on top of the archived queue/OPEN-ASKS.md rows.
                return _overseer_list_open_asks(
                    arguments,
                    overseer_root=overseer_root,
                    registry=registry,
                    supervisor=supervisor,
                    queue_store=queue_store,
                    ask_runtime=ask_runtime,
                )
            return handler(arguments, overseer_root=overseer_root)
        # Unreachable while _KNOWN_TOOLS == _SCOPING_ELIGIBLE_TOOLS | the
        # overseer set above (both live in config.py, validated together).
        return {"ok": False, "error": f"unknown tool {tool_name}", "code": 404}

    # Scoping OFF (caller_configs is None -- no [callers] table anywhere):
    # keep the pre-174 dispatch contract (bearer-only, client-supplied
    # requested_by).  An injected empty table still enforces, matching the
    # REST routes' injected-table semantics.
    if caller_configs is None:
        if tool_name == "harness_enqueue_task":
            return _enqueue(
                arguments,
                queue_store=queue_store,
                caller=None,
                model_fetch=model_fetch,
                settings_runtime=settings_runtime,
            )
        if tool_name == "harness_list_queue":
            return _list_queue(arguments, queue_store=queue_store)
        if tool_name == "harness_run_status":
            return _run_status(arguments, registry=registry, supervisor=supervisor)
        if tool_name == "harness_resolve_gate":
            # DR-05a: scoping-OFF installs have no signed caller, so the
            # transport's identity source is "none" -- persona channels and
            # the service channel are refused as relayed.
            return _resolve_gate(arguments, registry=registry, supervisor=supervisor, identity_source="none")
        if tool_name == "harness_list_contentions":
            return _list_contentions(runs_root=_runs_root(registry, supervisor))
        return {"ok": False, "error": f"unknown tool {tool_name}", "code": 404}

    specs = caller_configs_or_defaults(caller_configs)
    caller = resolve_mcp_caller(arguments, specs)
    arguments = strip_caller_arguments(arguments)
    if caller == ANON_CALLER:
        return {"ok": False, "error": "invalid or missing caller identity", "code": 401}
    if not tool_allowed(caller, tool_name, specs):
        if tool_name == RESOLVE_TOOL:
            emit_refusal_audit(
                _runs_root(registry, supervisor),
                caller=caller,
                tool=tool_name,
                run_id=str(arguments.get("run_id") or "") or None,
            )
        return {
            "ok": False,
            "error": f"caller {caller!r} is not allowed to invoke {tool_name}",
            "code": 403,
        }

    if tool_name == "harness_enqueue_task":
        # Scoping OFF (no [callers] table configured) reaches here with
        # caller=None: no identity enforcement, no ceilings -- the pre-174
        # dispatch contract.
        return _enqueue(
            arguments,
            queue_store=queue_store,
            caller=caller,
            spec=specs.get(caller) if caller is not None else None,
            model_fetch=model_fetch,
            settings_runtime=settings_runtime,
        )
    if tool_name == "harness_list_queue":
        return _list_queue(arguments, queue_store=queue_store)
    if tool_name == "harness_run_status":
        return _run_status(arguments, registry=registry, supervisor=supervisor)
    if tool_name == "harness_resolve_gate":
        # DR-05a: the caller reached this branch only after the A3d HMAC
        # verified, so the transport's identity source is "caller-secret".
        return _resolve_gate(arguments, registry=registry, supervisor=supervisor, identity_source="caller-secret")
    if tool_name == "harness_list_contentions":
        return _list_contentions(runs_root=_runs_root(registry, supervisor))
    # Unreachable: every scoping-eligible tool is dispatched above (the config
    # loader validates ``tools`` against this same surface).
    return {"ok": False, "error": f"unknown tool {tool_name}", "code": 404}


def _overseer_get_queue_entry(arguments: dict[str, Any], *, overseer_root: str | None) -> dict[str, Any]:
    from .overseer_state import get_queue_entry

    return get_queue_entry(arguments, overseer_root=overseer_root)


def _overseer_list_queue(arguments: dict[str, Any], *, overseer_root: str | None) -> dict[str, Any]:
    from .overseer_state import list_queue as handler

    return handler(arguments, overseer_root=overseer_root)


def _overseer_get_task_history(arguments: dict[str, Any], *, overseer_root: str | None) -> dict[str, Any]:
    from .overseer_state import get_task_history

    return get_task_history(arguments, overseer_root=overseer_root)


def _overseer_read_ledger(arguments: dict[str, Any], *, overseer_root: str | None) -> dict[str, Any]:
    from .overseer_state import read_ledger

    return read_ledger(arguments, overseer_root=overseer_root)


def _overseer_list_open_asks(
    arguments: dict[str, Any],
    *,
    overseer_root: str | None,
    registry: Any = None,
    supervisor: Any = None,
    queue_store: Any = None,
    ask_runtime: Any = None,
) -> dict[str, Any]:
    from .overseer_state import list_open_asks, live_open_ask_rows

    live_rows = None
    if supervisor is not None or queue_store is not None:
        live_rows = live_open_ask_rows(
            _gated_approvals(registry, supervisor),
            _blocked_queue_entries(queue_store),
        )
    store_rows = None
    if ask_runtime is not None and getattr(ask_runtime, "store_root", None):
        from .ask_store import AskStore

        try:
            store_rows = AskStore(ask_runtime.store_root).public_rows()
        except OSError:
            store_rows = None
    return list_open_asks(
        arguments, overseer_root=overseer_root, live_rows=live_rows, store_rows=store_rows
    )


# Allowed argument keys per ask tool (after the caller fields are stripped).
_ASK_TOOL_KEYS: dict[str, set[str]] = {
    "raise_open_ask": {"id", "ask", "kind", "evidence", "recommended", "default_if_silent", "fields"},
    "declare_ask_fields": {"id", "fields"},
    "respond_open_ask": {"id", "actor", "fields", "secret_targets"},
    "clear_ask_secret": {"id", "field", "actor"},
    "apply_ask_secret": {"id", "field", "target"},
}


def _ask_seed(
    ask_id: Any,
    *,
    overseer_root: str | None,
    registry: Any,
    supervisor: Any,
    queue_store: Any,
) -> tuple[dict[str, Any], str] | None:
    """Find a live or archive row to copy into the store on its first write."""
    from .overseer_state import _list_open_asks_file, live_open_ask_rows

    if not isinstance(ask_id, str):
        return None
    if ask_id.startswith(("gate-", "blocked-")):
        rows = live_open_ask_rows(
            _gated_approvals(registry, supervisor), _blocked_queue_entries(queue_store)
        )
        for row in rows:
            if row["id"] == ask_id:
                return dict(row), "live"
        return None
    archive = _list_open_asks_file({"include_closed": True, "limit": 500}, overseer_root=overseer_root)
    for row in archive.get("rows") or [] if archive.get("ok") else []:
        if row.get("id") == ask_id:
            return dict(row), "archive"
    return None


def _dispatch_ask_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    caller_configs: dict[str, dict[str, Any]] | None,
    runtime: Any,
    audit_root: Any,
    seed_lookup: Any,
) -> dict[str, Any]:
    from .ask_store import AskStoreError, validate_ask_id, validate_columns, validate_fields

    audit_root = str(audit_root) if audit_root else None
    scope = ASK_TOOL_SCOPES[tool_name]

    def audit(**event: Any) -> None:
        emit_caller_audit(audit_root, {"tool": tool_name, "scope": scope, **event})

    if not isinstance(arguments, dict):
        return {"ok": False, "error": "arguments must be an object", "code": 400}
    if runtime is None:
        return {"ok": False, "error": "ask tools are not configured on this node", "code": 503}
    if runtime.disabled_reason:
        return {"ok": False, "error": runtime.disabled_reason, "code": 503}
    grants = runtime.grants
    specs = ask_caller_specs(caller_configs, grants)
    caller, reason = verify_ask_request(tool_name, arguments, specs, runtime.nonces)
    claimed = arguments.get("caller")
    # The helper vault re-verifies this exact signed request itself.
    request = {"tool": tool_name, "arguments": dict(arguments)}
    arguments = strip_ask_signature(arguments)
    if caller == ANON_CALLER:
        audit(caller=str(claimed)[:32] if isinstance(claimed, str) else None, decision="unauthenticated", reason=reason)
        return {"ok": False, "error": f"invalid or missing caller identity ({reason})", "code": 401}
    if not ask_scope_allowed(caller, scope, grants):
        audit(caller=caller, decision="denied")
        return {
            "ok": False,
            "error": f"caller {caller!r} lacks scope {scope!r} for {tool_name}",
            "code": 403,
        }
    unknown = sorted(set(arguments) - _ASK_TOOL_KEYS[tool_name])
    if unknown:
        return {"ok": False, "error": "unknown field(s): " + ", ".join(unknown), "code": 400}
    try:
        store = runtime.store()
        if tool_name == "raise_open_ask":
            columns = validate_columns(arguments, require_ask=True)
            fields = validate_fields(arguments["fields"]) if arguments.get("fields") else None
            ask_id = validate_ask_id(arguments.get("id"))
            if seed_lookup(ask_id) is not None:
                raise AskStoreError(f"ask {ask_id!r} already exists as a live or archive row", 409)
            return {"ok": True, "row": store.raise_ask(ask_id, columns, fields, actor=caller)}
        if tool_name == "declare_ask_fields":
            ask_id = validate_ask_id(arguments.get("id"))
            fields = validate_fields(arguments.get("fields"))
            return {"ok": True, "row": store.declare_fields(ask_id, fields, actor=caller, seed=seed_lookup(ask_id))}
        if tool_name == "respond_open_ask":
            ask_id = validate_ask_id(arguments.get("id"))
            row = store.respond(
                ask_id,
                arguments.get("actor"),
                arguments.get("fields") if "fields" in arguments else {},
                seed=None if store.get(ask_id) is not None else seed_lookup(ask_id),
                secret_targets=arguments.get("secret_targets"),
                request=request,
            )
            return {"ok": True, "row": row}
        if tool_name == "clear_ask_secret":
            row = store.clear_secret(
                validate_ask_id(arguments.get("id")), arguments.get("field"), arguments.get("actor"), request=request
            )
            return {"ok": True, "row": row}
        # apply_ask_secret
        target = arguments.get("target")
        if not isinstance(target, str) or target not in ASK_APPLY_TARGETS:
            audit(caller=caller, target=str(target)[:128], decision="denied")
            return {"ok": False, "error": f"target {str(target)[:128]!r} is not on the apply allow-list", "code": 403}
        try:
            result = store.apply_secret(
                validate_ask_id(arguments.get("id")), arguments.get("field"), target, actor=caller, request=request
            )
        except AskStoreError as exc:
            audit(caller=caller, target=target, decision="failed", code=exc.code)
            raise
        audit(caller=caller, target=target, decision="applied")
        return {"ok": True, **result}
    except AskStoreError as exc:
        return {"ok": False, "error": str(exc), "code": exc.code}


# A gate sweep never needs more runs than this per call.
_MAX_GATED_RUNS = 50


def _gated_approvals(registry: Any, supervisor: Any) -> list[dict[str, Any]]:
    """Pending approvals of runs in ``waiting_approval`` (task A3).

    Cheap path only: the supervisor's summary enumeration plus each gated
    run's approvals.  A failure reading one run skips that run, so the
    open-asks surface never goes down with one bad run directory.
    """
    if registry is None or supervisor is None or not hasattr(supervisor, "list_run_summaries"):
        return []
    try:
        summaries = supervisor.list_run_summaries(statuses={"waiting_approval"})
    except Exception:
        return []
    items: list[dict[str, Any]] = []
    for summary in summaries[:_MAX_GATED_RUNS]:
        run_id = str(summary.get("id") or "")
        if not run_id:
            continue
        try:
            state = registry.state_for(run_id)
            approvals = state.list_approvals() if state is not None else []
        except Exception:
            continue
        for approval in approvals:
            if isinstance(approval, dict) and approval.get("status") == "pending":
                items.append(
                    {
                        "run_id": run_id,
                        "approval_id": approval.get("approval_id"),
                        "title": approval.get("title"),
                        "message": approval.get("message"),
                    }
                )
    return items


def _blocked_queue_entries(queue_store: Any) -> list[dict[str, Any]]:
    """Queue entries parked in status ``blocked`` (task A3)."""
    if queue_store is None:
        return []
    try:
        entries = queue_store.list()
    except Exception:
        return []
    return [
        {"queue_id": entry.queue_id, "name": entry.name, "reason": entry.reason}
        for entry in entries
        if getattr(entry, "status", None) == "blocked"
    ]


def _overseer_get_handoff(arguments: dict[str, Any], *, overseer_root: str | None) -> dict[str, Any]:
    from .overseer_state import get_handoff

    return get_handoff(arguments, overseer_root=overseer_root)


def _runs_root(registry: Any, supervisor: Any) -> str | None:
    if supervisor is not None:
        root = getattr(supervisor, "runs_root", None)
        if root:
            return str(root)
    if registry is not None:
        root = getattr(registry, "runs_root", None)
        if root:
            return str(root)
    return None


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


def _enqueue(
    arguments: dict[str, Any],
    *,
    queue_store: Any,
    caller: str | None = None,
    spec: dict[str, Any] | None = None,
    model_fetch: Any = None,
    settings_runtime: Any = None,
) -> dict[str, Any]:
    if queue_store is None:
        return {"ok": False, "error": "queue requires a configured runs root", "code": 501}
    # Task 233: reject unknown argument keys by name instead of dropping them.
    # The schema already advertises additionalProperties=false; the dispatch
    # layer enforces it too so non-schema-conformant transports behave the
    # same way.
    from .queue import KNOWN_REQUEST_KEYS

    unknown = sorted(set(arguments) - KNOWN_REQUEST_KEYS)
    if unknown:
        return {
            "ok": False,
            "error": "unknown field(s): " + ", ".join(unknown),
            "code": 400,
        }
    # Budget ceilings (task 174, section 3.5) run after the unknown-key check
    # but before any store validation of the payload: a caller at its hourly
    # cap gets 429 with a retry hint, and a max_rounds over the caller's clamp
    # gets 422 with the refusal spelled out -- never a silently truncated
    # entry.  ``caller`` is None only when scoping is OFF (no [callers]
    # table); the ceilings are then unconfigured and skipped.
    if caller is not None:
        rate = enqueue_rate_violation(spec, queue_store, caller)
        if rate is not None:
            return {"ok": False, "error": rate["error"], "code": 429, "retry_after": rate["retry_after"]}
        clamp_reason = rounds_clamp_violation(spec, arguments.get("max_rounds"))
        if clamp_reason is not None:
            return {"ok": False, "error": clamp_reason, "code": 422}
    body = {
        "name": _bounded(arguments.get("name"), field="name", max_chars=256, required=True),
        "task": _bounded(arguments.get("task"), field="task", max_chars=100_000, required=True),
        "workspace": _bounded(arguments.get("workspace"), field="workspace", max_chars=4096, required=True),
        # trio is optional (task 233): omitting it stores the entry with trio
        # unset; no default is invented at enqueue time.
        "trio": _bounded(arguments.get("trio"), field="trio", max_chars=64),
        # max_rounds/priority stay unset when omitted so the store's own
        # validation defaults apply (no duplicated defaults here).
        "max_rounds": arguments.get("max_rounds"),
        "priority": arguments.get("priority"),
        "branch": _bounded(arguments.get("branch"), field="branch", max_chars=256),
        "continue_branch": arguments.get("continue_branch"),
        "base_check": _bounded(arguments.get("base_check"), field="base_check", max_chars=4096),
        # requested_by comes from the VERIFIED caller identity, not from the
        # client: the stamp is the ownership proof for the DELETE rule and the
        # hourly ceiling, so a client cannot forge it by passing the field
        # (task 174 scope item 3). Main (task 233) additionally requires a
        # requested_by field and a dedup_key on the body; the stamp wins for
        # requested_by and dedup_key passes through to the store's own
        # validation.  When scoping is OFF (no [callers] table) the caller
        # argument is None and the client value is required instead.
        "requested_by": caller
        if caller is not None
        else _bounded(
            arguments.get("requested_by"),
            field="requested_by",
            max_chars=256,
            required=True,
        ),
        "dedup_key": _bounded(arguments.get("dedup_key"), field="dedup_key", max_chars=256),
        # fc-H2: validated by the store (int 1..1440 or omitted).
        "time_limit_minutes": arguments.get("time_limit_minutes"),
    }
    # LHH-QUEUE-HOTRELOAD (R6): model/roles are forwarded ONLY when actually
    # supplied.  An explicit null stays unforwarded (matching the pre-R6
    # body), so the legacy "roles" string keeps its exact alias semantics in
    # the store and calls without either override produce a byte-identical
    # body.  A string roles is bounded like ``trio``; a dict passes through
    # to the store's create-run-shaped validation.
    raw_roles = arguments.get("roles")
    if raw_roles is not None:
        body["roles"] = (
            _bounded(raw_roles, field="roles", max_chars=64)
            if isinstance(raw_roles, str)
            else raw_roles
        )
    raw_model = arguments.get("model")
    if raw_model is not None:
        body["model"] = _bounded(raw_model, field="model", max_chars=256)
    # Task 300: the requester block is passed through verbatim (validated by the
    # store) and stamped with the verified caller.  ``arguments["requester"]``
    # is the dict the client supplied; ``caller`` (the verified identity) is the
    # stamp.  When scoping is OFF (caller is None) the client-supplied block is
    # still required in strict mode, so it must be present for the create to
    # succeed.  AdditionalProperties=false in the schema keeps a non-dict
    # requester from reaching here unguarded.
    body["requester"] = arguments.get("requester")
    from fastapi import HTTPException  # lazy: R6 503 carrier from the store
    try:
        entry = queue_store.create(
            body,
            verified_caller=caller,
            model_fetch=model_fetch,
            settings_runtime=settings_runtime,
        )
    except HTTPException as exc:
        # R6: provider-down / settings-store-down refusals carry the R4/R5
        # 503 taxonomy through unchanged; the web route gets the same 503 by
        # letting the exception propagate to FastAPI's own handler.
        return {"ok": False, "error": exc.detail, "code": exc.status_code}
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "code": 422}
    result: dict[str, Any] = {"ok": True, "queue_id": entry.queue_id, "status": entry.status}
    warnings = _contention_warnings(
        entry.workspace,
        queue_store,
        include_pending=True,
    )
    if warnings:
        result["warnings"] = warnings
    return result


def _list_queue(arguments: dict[str, Any], *, queue_store: Any) -> dict[str, Any]:
    if queue_store is None:
        return {"ok": False, "error": "queue requires a configured runs root", "code": 501}
    from .queue import _VALID_STATUS
    from .webapi.server import _queue_entry_summary

    queue_id = _bounded(arguments.get("queue_id"), field="queue_id", max_chars=80) or None
    if queue_id:
        entry = queue_store.get(queue_id)
        if entry is None:
            return {"ok": False, "error": "queue entry not found", "code": 404}
        return {"ok": True, "entry": entry.to_dict()}
    view = (_bounded(arguments.get("view"), field="view", max_chars=16) or "summary").lower()
    status_raw = _bounded(arguments.get("status"), field="status", max_chars=80) or None
    name = (_bounded(arguments.get("name"), field="name", max_chars=200) or "").lower()
    trio = _bounded(arguments.get("trio"), field="trio", max_chars=64) or None
    after = _bounded(arguments.get("after"), field="after", max_chars=200) or None
    try:
        limit = max(1, min(500, int(_bounded(arguments.get("limit"), field="limit", max_chars=6) or 50)))
    except ValueError:
        return {"ok": False, "error": "limit must be an integer 1-500", "code": 422}
    valid_statuses = set(_VALID_STATUS)
    entries = queue_store.list()
    if view == "full" and not any((name, trio, after, arguments.get("limit"))):
        # Legacy contract: every matching entry with its task text.
        filtered = entries
        if status_raw is not None and status_raw in valid_statuses:
            filtered = [item for item in entries if item.status == status_raw]
        result: dict[str, Any] = {"ok": True, "entries": [item.to_dict() for item in filtered], "counts": queue_store.counts()}
    else:
        wanted = {s.strip() for s in status_raw.split(",")} & valid_statuses if status_raw else None
        filtered = [
            item for item in entries
            if (wanted is None or item.status in wanted)
            and (not name or name in (item.name or "").lower())
            and (trio is None or (item.trio or "") == trio)
        ]
        filtered.sort(key=lambda item: (float(item.created_at or 0), item.queue_id), reverse=True)
        if after:
            try:
                ts_raw, cursor_id = after.split(",", 1)
                key = (float(ts_raw), cursor_id)
            except ValueError:
                return {"ok": False, "error": "after must be '<created_at>,<queue_id>'", "code": 422}
            filtered = [item for item in filtered if (float(item.created_at or 0), item.queue_id) < key]
        page = filtered[:limit]
        next_cursor = None
        if len(filtered) > limit and page:
            next_cursor = f"{float(page[-1].created_at or 0)!r},{page[-1].queue_id}"
        summary = view != "full"
        result = {
            "ok": True,
            "entries": [_queue_entry_summary(item) if summary else item.to_dict() for item in page],
            "counts": queue_store.counts(),
            "total": len(filtered),
            "next_cursor": next_cursor,
            "view": "summary" if summary else "full",
        }
    contentions = _read_contentions(queue_store.runs_root)
    if contentions:
        result["contentions"] = contentions
    return result


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
    result: dict[str, Any] = {
        "ok": True,
        "run_id": run_id,
        "managed": managed_status.get("managed") is not False,
        **managed_status,
        "owner": public_owner,
    }
    if supervisor is not None:
        workspace = public_owner.get("workspace") if isinstance(public_owner, dict) else None
        if workspace:
            warnings = _contention_warnings_for_run(run_id, workspace, supervisor)
            if warnings:
                result["warnings"] = warnings
    return result


def _list_contentions(*, runs_root: str | None) -> dict[str, Any]:
    if runs_root is None:
        return {"ok": False, "error": "contentions require a configured runs root", "code": 501}
    contentions = _read_contentions(runs_root)
    return {"ok": True, "contentions": contentions}


def _read_contentions(runs_root_value: Any) -> list[dict[str, Any]]:
    if runs_root_value is None:
        return []
    path = __import__("pathlib").Path(runs_root_value) / "queue" / "contention.json"
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("contentions"), list):
            return data["contentions"]
    except Exception:
        pass
    return []


def _contention_warnings(
    workspace: str,
    queue_store: Any,
    *,
    include_pending: bool,
) -> list[dict[str, Any]]:
    """Build warning payloads for a workspace against active runs and queued entries."""

    try:
        runs_root = queue_store.runs_root
    except AttributeError:
        return []
    return _warnings_for_workspace(workspace, runs_root, include_pending=include_pending)


def _contention_warnings_for_run(
    run_id: str,
    workspace: str,
    supervisor: Any,
) -> list[dict[str, Any]]:
    try:
        runs_root = supervisor.runs_root
    except AttributeError:
        return []
    return _warnings_for_workspace(
        workspace,
        runs_root,
        include_pending=False,
        exclude_run_id=run_id,
    )


def _active_run_workspaces(runs_root: Any, include_pending: bool) -> dict[str, str]:
    """Collect workspace paths for active runs and optionally pending queue entries."""

    workspaces: dict[str, str] = {}
    if include_pending:
        try:
            from .queue import QueueStore

            store = QueueStore(runs_root)
            for entry in store.list():
                if entry.status not in {"pending", "launched"}:
                    continue
                if not entry.workspace:
                    continue
                workspaces[entry.queue_id] = str(entry.workspace)
        except Exception:
            pass
    return workspaces


def _warnings_for_workspace(
    workspace: str,
    runs_root: Any,
    *,
    include_pending: bool,
    exclude_run_id: str | None = None,
) -> list[dict[str, Any]]:
    workspaces = _active_run_workspaces(runs_root, include_pending=include_pending)
    if exclude_run_id:
        workspaces.pop(exclude_run_id, None)

    target = os.path.abspath(workspace)
    all_paths = sorted(set([*workspaces.values(), target]))
    identities = resolve_many(all_paths, budget_seconds=2.0)
    target_identity = identities.get(target) or identities.get(workspace)
    if target_identity is None:
        return []

    active_identities: dict[str, Any] = {}
    for run_id, ws in workspaces.items():
        if not ws:
            continue
        identity = identities.get(ws) or identities.get(os.path.abspath(ws))
        if identity is not None:
            active_identities[run_id] = identity

    groups = detect_contention(active_identities, min_emit_severity="same_repo")
    warnings: list[dict[str, Any]] = []
    for group in groups:
        peer_names = [peer.run_id for peer in group.members if peer.run_id != exclude_run_id]
        if not peer_names:
            continue
        warnings.append(_warning_payload(group.severity, peer_names))
    return warnings


def _warning_payload(severity: str, peer_ids: list[str]) -> dict[str, Any]:
    en = f"Workspace overlaps with {', '.join(peer_ids)} (severity: {severity}). Second checkouts are allowed, but verify the sibling tree is not mid-PR on the same branch."
    zh = f"工作区与 {', '.join(peer_ids)} 重叠（等级：{severity}）。允许第二个 checkout，但请确认同级树不在同一分支的 PR 中间。"
    return {
        "code": "workspace_contention",
        "severity": severity,
        "message": en,
        "message_zh": zh,
        "detail": {"peer_ids": peer_ids},
    }


def _resolve_gate(
    arguments: dict[str, Any],
    *,
    registry: Any,
    supervisor: Any,
    identity_source: str = "none",
) -> dict[str, Any]:
    run_id = _bounded(arguments.get("run_id"), field="run_id", max_chars=128, required=True)
    approval_id = _bounded(arguments.get("approval_id"), field="approval_id", max_chars=128, required=True)
    state = registry.state_for(run_id)
    if state is None:
        return {"ok": False, "error": "run not found", "code": 404}
    if supervisor is not None and not supervisor.can_control(run_id):
        return {"ok": False, "error": "run is not accepting approvals", "code": 409}

    # DR-05a: approved_by/channel are required wire-contract inputs.
    # ``identity_source`` comes from the dispatch layer (verified caller or
    # explicit "none"), never from the caller payload.
    approved_by = arguments.get("approved_by")
    channel = arguments.get("channel")
    refusal = validate_resolve_identity(approved_by, channel, identity_source)
    if refusal is not None:
        # Every refusal is logged at WARNING with the identity source the
        # transport presented, so a relayed or malformed resolve is auditable.
        logger.warning(
            "gate resolve refused (%s): identity_source=%s channel=%s run_id=%s approval_id=%s",
            refusal,
            str(identity_source),
            str(channel),
            run_id,
            approval_id,
        )
        if refusal == "relayed approval refused":
            logger.warning(
                "relayed approval refused: identity_source=%s channel=%s approved_by=%s run_id=%s approval_id=%s",
                str(identity_source),
                str(channel),
                str(approved_by),
                run_id,
                approval_id,
            )
        return {
            "ok": False,
            "error": refusal,
            "code": 403 if refusal == "relayed approval refused" else 422,
        }
    approved_by_text = str(approved_by).strip()
    channel_text = str(channel).strip()

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
            approved_by=approved_by_text,
            channel=channel_text,
            unattributed=(channel_text == "service"),
            identity_source=identity_source,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc), "code": 409}
    if not ok:
        return {"ok": False, "error": "approval is missing, resolved, or read-only", "code": 409}
    return {"ok": True, "approval_id": approval_id, "status": "accepted"}


# --------------------------------------------------------------------- R5 --
# LHH-QUEUE-HOTRELOAD: queue trio/capacity admin tools.
#
# These handlers are thin wrappers: row validation reuses the R4 admin
# routes' ``_clean_trio_row`` verbatim (imported lazily -- it lives behind
# the webapi module that imports fastapi), the reads reuse the store's
# ``queue_trios()``/``queue_capacity()``, and the writes go through the
# store's own ``_db`` transaction and ``_audit`` machinery with the R4
# routes' exact write pattern (INSERT OR REPLACE + before/after audit, only
# audited when the row actually changed).  The live model list comes through
# the R4 injectable fetch seam: ``model_fetch`` when given, else
# ``model_catalog.discover_model_catalog``.

# Error wording shared with the R4 routes so both surfaces refuse the same
# way (the routes answer 503 with this text when the provider is down).
_QUEUE_ADMIN_LIST_UNAVAILABLE = "the live model list is unavailable; refusing to write unvalidated trios"


def _queue_admin_live_models(model_fetch: Any) -> dict[str, Any] | None:
    """The backend's live model list per agent, or ``None`` when unknown.

    Same shape as the R4 routes' ``live_models()``: the fetch is injectable
    so tests never touch the network, and a broken provider yields ``None``
    (503 below), never an exception.
    """

    fetch = model_fetch
    if fetch is None:
        from .model_catalog import discover_model_catalog

        fetch = discover_model_catalog
    try:
        catalogue = fetch()
    except Exception:  # noqa: BLE001 - a broken provider must refuse writes, not crash
        return None
    if not isinstance(catalogue, dict):
        return None
    models = catalogue.get("models")
    return models if isinstance(models, dict) else None


# LHH-QUEUE-PROTECT-DOCS (F3): the MCP tools mirror the R4 ``set_trio_model``
# protection.  These two helpers are the rule's runtime seams:
#
# - ``_queue_admin_resolve_protected`` returns the project config's
#   ``[queue].protected_trios`` tuple (``("kimi", "glm", "qwen")`` by default
#   -- ``config.DEFAULT_PROTECTED_TRIOS``).  An empty tuple disables the
#   rule entirely (the same way the REST route does).
# - ``_queue_admin_caller_is_admin`` answers True only when the runtime has
#   a non-empty admin list AND the caller identity is on it.  ``actor=None``
#   (bearer-only dispatch, caller_configs is None) always returns False --
#   force has no meaning without an SSO admin list to guard it.
def _queue_admin_resolve_protected(settings_runtime: Any) -> tuple[str, ...]:
    from .settings_store import active_runtime

    rt = settings_runtime if settings_runtime is not None else active_runtime()
    protected = getattr(rt, "protected_trios", None)
    if isinstance(protected, tuple):
        return protected
    if isinstance(protected, list):
        return tuple(dict.fromkeys(item.strip() for item in protected if isinstance(item, str) and item.strip()))
    # No runtime list -> the rule is off (matches the REST route's
    # ``_resolve_protected_trios``).  The MCP path defaults via the
    # ``SettingsRuntime`` that ``dispatch`` constructs through
    # ``active_runtime()``, whose ``protected_trios`` is populated from the
    # project config when present and stays empty otherwise.
    return ()


def _queue_admin_caller_is_admin(actor: str | None, settings_runtime: Any) -> bool:
    from .settings_store import active_runtime

    rt = settings_runtime if settings_runtime is not None else active_runtime()
    if not isinstance(actor, str) or not actor.strip():
        return False
    is_admin = getattr(rt, "is_admin", None)
    if not callable(is_admin):
        return False
    try:
        return bool(is_admin(actor))
    except Exception:  # noqa: BLE001 - a broken runtime must fail closed
        return False


def _dispatch_queue_admin_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    caller_configs: dict[str, dict[str, Any]] | None,
    settings_runtime: Any = None,
    model_fetch: Any = None,
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        return {"ok": False, "error": "arguments must be an object", "code": 400}
    from .settings_store import active_runtime

    rt = settings_runtime if settings_runtime is not None else active_runtime()
    store = getattr(rt, "store", None)
    if store is None:
        return {
            "ok": False,
            "error": getattr(rt, "error", None) or "the settings store is not configured",
            "code": 503,
        }
    # Identity.  There is no per-caller grant surface for these tools (their
    # names can never enter a task-174 ``tools`` allowlist -- the config
    # loader validates that list against config.py's static tool set), so
    # when scoping is ON a verified non-anon caller is required but no
    # allowlist check applies; when scoping is OFF the bearer-parity check at
    # the top of ``dispatch`` is the gate, matching the pre-174 contract of
    # every other fleet tool, and the audit actor marks the channel.
    if caller_configs is None:
        caller = None
    else:
        caller = resolve_mcp_caller(arguments, caller_configs_or_defaults(caller_configs))
        if caller == ANON_CALLER:
            return {"ok": False, "error": "invalid or missing caller identity", "code": 401}
    arguments = strip_caller_arguments(arguments)
    actor = caller if caller is not None else "mcp"
    handlers = {
        "list_trios": _queue_admin_list_trios,
        "set_trio_model": _queue_admin_set_trio_model,
        "set_trio_capacity": _queue_admin_set_trio_capacity,
        "refresh_models": _queue_admin_refresh_models,
    }
    return handlers[tool_name](
        arguments,
        store=store,
        actor=actor,
        model_fetch=model_fetch,
        settings_runtime=settings_runtime,
    )


def _queue_admin_unknown_fields(arguments: dict[str, Any], allowed: set[str]) -> dict[str, Any] | None:
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        return {"ok": False, "error": "unknown field(s): " + ", ".join(unknown), "code": 400}
    return None


def _queue_admin_list_trios(
    arguments: dict[str, Any],
    *,
    store: Any,
    actor: str,
    model_fetch: Any,
    settings_runtime: Any = None,
) -> dict[str, Any]:
    refusal = _queue_admin_unknown_fields(arguments, set())
    if refusal is not None:
        return refusal
    return {"ok": True, "trios": store.queue_trios(), "capacity": store.queue_capacity()}


def _queue_admin_set_trio_model(
    arguments: dict[str, Any],
    *,
    store: Any,
    actor: str,
    model_fetch: Any,
    settings_runtime: Any = None,
) -> dict[str, Any]:
    refusal = _queue_admin_unknown_fields(arguments, {"trio", "model", "force"})
    if refusal is not None:
        return refusal
    from .settings_store import upsert_queue_trio
    from .webapi.settings_routes import _clean_trio_row

    trio = arguments.get("trio")
    if not isinstance(trio, str) or not trio.strip():
        return {"ok": False, "error": "trio must be a non-empty string", "code": 422}
    name = trio.strip()
    model = arguments.get("model")
    if not isinstance(model, str) or not model.strip():
        return {"ok": False, "error": "model must be a non-empty string model id", "code": 422}
    # LHH-QUEUE-PROTECT-DOCS (F3): honour the explicit override flag ONLY
    # when the caller is on the SSO admin list (``runtime.is_admin``). A
    # non-admin caller that supplies ``force: true`` is refused with 409
    # (the force flag is not a bypass on its own). The check runs BEFORE
    # the live-list validation so a refused request never reaches the
    # provider, never writes an audit row, and never mutates the trio.
    force_raw = arguments.get("force", False)
    if isinstance(force_raw, bool):
        force = force_raw
    elif isinstance(force_raw, int):
        force = bool(force_raw)
    else:
        force = False
    protected = _queue_admin_resolve_protected(settings_runtime)
    if name in protected and not force:
        return {
            "ok": False,
            "error": (
                f"trio {name!r} is protected: Paxton's standing rule "
                f"prohibits model or agent changes to {name}"
            ),
            "code": 409,
        }
    if name in protected and force and not _queue_admin_caller_is_admin(actor, settings_runtime):
        # Force without admin status: refuse with the same 409 the REST
        # route answers for a non-admin caller asking to bypass the rule.
        return {
            "ok": False,
            "error": (
                f"force=true on protected trio {name!r} requires the verified "
                "caller to be on the SSO admin list"
            ),
            "code": 409,
        }
    existing = next((row for row in store.queue_trios() if row["name"] == name), None)
    if existing is None:
        return {"ok": False, "error": f"unknown trio {name!r}", "code": 404}
    models = _queue_admin_live_models(model_fetch)
    if models is None:
        return {"ok": False, "error": _QUEUE_ADMIN_LIST_UNAVAILABLE, "code": 503}
    # Only the model changes; every other field keeps its stored value.  The
    # full row goes through the same validator the R4 PUT applies to each
    # row, so a refusal here carries the identical rule set and wording.
    row, error = _clean_trio_row(
        {
            "name": name,
            "agent": existing["agent"],
            "model": model.strip(),
            "mcp_profile": existing["mcp_profile"],
            "enabled": existing["enabled"],
        },
        models,
    )
    if error is not None:
        return {"ok": False, "error": error, "code": 422}
    assert row is not None
    with store._db() as db:  # the R4 PUT body, verbatim, for the single row
        old = db.execute("SELECT * FROM queue_trio WHERE name = ?", (row["name"],)).fetchone()
        # Only the trio-wide model changes; per-role overrides are kept.
        upsert_queue_trio(db, row, actor, roles=None)
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
        if before != after:
            audit_detail: dict[str, Any] = {"before": before, "after": after}
            # LHH-QUEUE-PROTECT-DOCS (F3): mark the audit row when the
            # protected-trio override was used, so the SSO admin identity
            # (``actor``) is searchable in ``GET /api/admin/audit`` with
            # the structured flag.
            if row["name"] in protected and force:
                audit_detail["protected_override"] = True
            store._audit(db, actor, "set", f"queue_trio:{row['name']}", **audit_detail)
    result = {"ok": True, "trios": store.queue_trios()}
    if name in protected and force:
        result["protected_override"] = [name]
    return result


def _queue_admin_set_trio_capacity(
    arguments: dict[str, Any],
    *,
    store: Any,
    actor: str,
    model_fetch: Any,
    settings_runtime: Any = None,
) -> dict[str, Any]:
    refusal = _queue_admin_unknown_fields(arguments, {"trio", "max_concurrent"})
    if refusal is not None:
        return refusal
    trio = arguments.get("trio")
    if not isinstance(trio, str) or not trio.strip():
        return {"ok": False, "error": "trio must be a non-empty string", "code": 422}
    name = trio.strip()
    known_trios = {row["name"] for row in store.queue_trios()}
    if name not in known_trios:
        return {"ok": False, "error": f"capacity for unknown trio {name!r}", "code": 422}
    raw = arguments.get("max_concurrent")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return {"ok": False, "error": f"capacity for {name!r} must be a non-negative integer", "code": 422}
    with store._db() as db:  # the R4 PUT body, verbatim, for the single trio
        old = db.execute("SELECT max_concurrent FROM queue_capacity WHERE trio = ?", (name,)).fetchone()
        db.execute("INSERT OR REPLACE INTO queue_capacity(trio, max_concurrent) VALUES (?, ?)", (name, raw))
        before = None if old is None else int(old["max_concurrent"])
        if before != raw:
            store._audit(db, actor, "set", f"queue_capacity:{name}", before=before, after=raw)
    return {"ok": True, "capacity": store.queue_capacity()}


def _queue_admin_refresh_models(
    arguments: dict[str, Any],
    *,
    store: Any,
    actor: str,
    model_fetch: Any,
    settings_runtime: Any = None,
) -> dict[str, Any]:
    refusal = _queue_admin_unknown_fields(arguments, set())
    if refusal is not None:
        return refusal
    fetch = model_fetch
    if fetch is None:
        from .model_catalog import discover_model_catalog

        fetch = discover_model_catalog
    try:
        # force=True is the refresh semantic -- the POST /api/models/refresh
        # route calls the same discovery with the same flag; otherwise the
        # list is briefly cached.
        catalogue = fetch(force=True)
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "the live model list is unavailable", "code": 503}
    if not isinstance(catalogue, dict):
        return {"ok": False, "error": "the live model list is unavailable", "code": 503}
    models = catalogue.get("models")
    models = models if isinstance(models, dict) else {}
    return {
        "ok": True,
        "refreshed": True,
        "models": models,
        "counts": {agent: len(entries) for agent, entries in models.items() if isinstance(entries, list)},
    }


def normalize_request_token(authorization: str | None) -> str | None:
    """Extract the bare bearer token from an Authorization header value."""
    if not authorization:
        return None
    parts = str(authorization).split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None
