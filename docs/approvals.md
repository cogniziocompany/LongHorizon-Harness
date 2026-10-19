# Approvals identity (DR-05a)

Every gate/approval resolve records **WHO** approved and **through WHAT
channel**, and the transport that carried the resolve certifies the
identity it observed. This document is the source of truth for those
field names, their enum values, and the wire contract that
`/api/runs/{run_id}/gates/{approval_id}/resolve` (REST) and
`harness_resolve_approval` (MCP) enforce.

The allow-list of who may approve lives in the existing caller-config
layer (`LH_HARNESS_CALLER_*` and `[callers]`) and is **not changed by
DR-05a** -- a resolve that names a verified identity still has to pass
the existing per-caller tool allow-list. DR-05a adds the recorded
identity; it does not relax any allow-list.

## Fields on a resolve

A resolve body MUST carry both:

| Field | Type | Required | Notes |
|---|---|---|---|
| `approved_by` | string | yes | The identity of the approver, prefixed with the channel family (see "Approved-by form" below). |
| `channel` | string | yes | One of the five `APPROVAL_CHANNELS` enum values (see below). |

A resolve MUST also surface an `identity_source`, but the transport sets
that field itself -- it is **never** taken from the caller payload. The
caller cannot spoof the identity source.

| `channel` | Required `approved_by` prefix | Notes |
|---|---|---|
| `fleet-chat` | `upn:` | SSO/web identity, the same shape as `web-sso`. |
| `web-sso` | `upn:` | SSO/web identity served by the Caddy/oauth2-proxy edge. |
| `slack-dm` | `slack:` | Real Slack user id (`U...`), forwarded by the DR-05b relay. |
| `terminal` | `terminal:` | `<os user>@<host>` of the operator who ran the command. |
| `service` | `service:` | Unattributed service resolve; only accepted from an authorised service caller. |

The prefix and the channel must agree. A resolve claiming
`channel: slack-dm` cannot carry an `upn:` identity, and a resolve
claiming `channel: web-sso` cannot carry a `slack:` identity. The
validator is `valid_approved_by` in `src/lh_harness/caller_auth.py`.

## Enum: `channel`

```
APPROVAL_CHANNELS = ("fleet-chat", "slack-dm", "terminal", "web-sso", "service")
```

Any other value is rejected as missing/invalid (see "Wire contract"
below). The persona channels -- those that must present a verified
signed-in identity -- are a strict subset:

```
PERSONA_CHANNELS = ("fleet-chat", "web-sso")
```

A resolve that names one of these channels with `identity_source: none`
is a relayed approval and is refused (HTTP 403).

## Enum: `identity_source`

Set by the transport (web routes / MCP dispatch) only. The full set:

| Value | Set by | Meaning |
|---|---|---|
| `signed-header` | web routes | The SSO signed-in header (the same chain that resolves an admin in `/admin`) verified the identity. |
| `caller-secret` | web routes + MCP dispatch | An A3d signed caller (its verified name is passed through). |
| `gateway-key:<alias>` | gateway relay | The gateway allowed a key under the named alias; the alias is the suffix. |
| `none` | transport default | No verified identity reached the transport (relayed / model-supplied). |

```
APPROVAL_IDENTITY_SOURCES = ("signed-header", "caller-secret", "none")
# "gateway-key:<alias>" is prefix-matched and is not in the tuple.
GATEWAY_KEY_IDENTITY_SOURCE_PREFIX = "gateway-key:"
```

`channel: service` accepts only `caller-secret` or `gateway-key:<alias>`;
all other persona channels accept any non-`none` source. `none` on a
persona channel is always a 403.

## Approved-by form

```
APPROVED_BY_MAX_CHARS = 254
```

The wire contract caps the value at 254 characters and fixes only the
prefix. The body shape is one of:

| Channel family | Shape | Example |
|---|---|---|
| `upn:` | `upn:<user@domain>` | `upn:dev@corp.example` |
| `slack:` | `slack:U<alnum...>` | `slack:U123ABCDEF` |
| `terminal:` | `terminal:<os user>@<host>` | `terminal:deploy@ct110` |
| `service:` | `service:<caller alias>` | `service:overseer1` |

The full regexes live in `src/lh_harness/caller_auth.py`:
`_UPN_APPROVED_BY_RE`, `_SLACK_APPROVED_BY_RE`, `_TERMINAL_APPROVED_BY_RE`,
`_SERVICE_APPROVED_BY_RE`. They police shape only; the only length cap
anywhere is the 254-character value cap from the contract.

## Wire contract

The REST and MCP resolves are the same contract. A resolve that omits
or mis-shapes `approved_by` or `channel` is a 422:

```http
HTTP/1.1 422 Unprocessable Entity
Content-Type: application/json

{"error":"approved_by and channel are required"}
```

A resolve that names a persona channel (`fleet-chat`, `web-sso`) with
`identity_source: none`, or a `service` channel from an unauthorised
identity source, is a 403:

```http
HTTP/1.1 403 Forbidden
Content-Type: application/json

{"error":"relayed approval refused"}
```

The validator is `validate_resolve_identity` in
`src/lh_harness/caller_auth.py`. A refusal is logged at WARNING with the
identity source, the channel, the proposed `approved_by`, the run id and
the approval id; an audit line is appended to
`<runs_root>/queue/caller_audit.jsonl`. The state record keeps
`approved_by`, `channel`, and `identity_source` on the approval; an
unattributed resolve is flagged automatically.

## Constants reference

The single source of truth for the enums, the regexes, the validator
and the prefix table is `src/lh_harness/caller_auth.py`:

- `APPROVED_BY_MAX_CHARS`
- `APPROVAL_CHANNELS`, `PERSONA_CHANNELS`
- `APPROVAL_IDENTITY_SOURCES`, `GATEWAY_KEY_IDENTITY_SOURCE_PREFIX`
- `valid_approved_by`, `valid_approval_identity_source`,
  `service_identity_source_ok`, `validate_resolve_identity`

Any change to a value, an enum or a regex here is a wire-contract
change and must be reflected in this document.
