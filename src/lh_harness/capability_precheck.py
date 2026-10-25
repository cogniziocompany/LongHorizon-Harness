"""Capability precheck before launcher launch (LHH-QUEUE-RECONCILE-CAPABILITY-PRECHECK).

The audit found runs that launched specs the executor could not honour -- for
example an SSH route to ``ptait07`` or ``CT204`` that the executor's MCP
profile does not include. The launch then burned rounds trying to discover
the missing capability at runtime. This module is the gate the launcher
needs *before* it burns those rounds.

A spec may declare capabilities two ways:

*   Explicit: a ``REQUIRES:`` line in the spec body listing the capability
    names. Format is deliberately tiny -- one ``REQUIRES:`` line per
    capability, e.g. ``REQUIRES: ssh:ct204`` -- because that matches what an
    operator reads when hand-authoring a task and survives any later
    reformatting.
*   Heuristic: keywords the gate knows about even when the spec forgot the
    line. ``ssh-ssh_exec`` and bare host names (``ptait07``, ``ptait09``,
    ``CT204``, ``CT100``) all map to the SSH capability; ``gh`` and ``gh-cli``
    map to the github MCP; ``npm`` / ``pnpm`` / ``yarn`` map to npm package
    management.

The gate compares the resolved set against the trio's MCP profile servers.
A missing capability yields ``"missing capability: X"`` -- the exact phrase
the launcher's skip reason machinery already records, so the existing
``queue.skipped`` event carries the human-meaningful reason.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Keep imports local so importing this module on a host without the rest of
# the package still works (the CLI and tests both go through this file).
PACKAGE_ROOT = Path(__file__).resolve().parent
_SRC_ROOT = PACKAGE_ROOT.parent
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

try:
    from lh_harness.mcp_profiles import _BUILTINS, list_available_profiles  # type: ignore
except Exception:  # pragma: no cover - import guard for thin harnesses
    _BUILTINS = {}  # type: ignore[assignment]
    list_available_profiles = None  # type: ignore[assignment]


# Capability names -- a closed set, listed in the order an operator scanning
# the spec would expect. Anything outside this set is treated as a literal
# capability and only rejected when the profile is obviously the wrong shape
# (server matching falls back to a permissive lookup).
KNOWN_CAPABILITIES: tuple[str, ...] = (
    "ssh",
    "ssh:ct204",
    "ssh:ptait07",
    "ssh:ptait09",
    "ssh:ptait01",
    "github",
    "gh",
    "npm",
    "youtrack",
    "hydra",
    "proxmox",
)


# Heuristic substrings that imply an SSH requirement even when the spec
# forgot the REQUIRES line. Each entry is (compiled-regex, capability-name)
# and the test suite asserts this map directly, so adding a new heuristic
# is a deliberate update.
_SSH_HOST_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bssh[_-]?ssh[_-]?exec\b"), "ssh"),
    (re.compile(r"(?i)\bproxmoxct(?:204|100|110)\b"), "ssh:ct204"),
    (re.compile(r"(?i)\bptait07\b"), "ssh:ptait07"),
    (re.compile(r"(?i)\bptait09\b"), "ssh:ptait09"),
    (re.compile(r"(?i)\bptait01\b"), "ssh:ptait01"),
    (re.compile(r"(?i)\bct204\b"), "ssh:ct204"),
    (re.compile(r"(?i)\bct100\b"), "ssh:ct204"),
)

_GH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bgh pr\b"),
    re.compile(r"(?i)\bgh issue\b"),
    re.compile(r"(?i)\bgh api\b"),
    re.compile(r"(?i)\bgh-cli\b"),
)

_NPM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bnpm install\b"),
    re.compile(r"(?i)\bnpm ci\b"),
    re.compile(r"(?i)\bnpm run\b"),
    re.compile(r"(?i)\bpnpm install\b"),
    re.compile(r"(?i)\byarn install\b"),
)


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------


_REQUIRES_LINE_RE = re.compile(r"^\s*REQUIRES\s*:\s*(.+?)\s*$", re.MULTILINE)
# Some specs break across lines; accept a continuation where the next
# non-empty line is indented (>=2 spaces) and starts with another token.
_REQUIRES_CONT_RE = re.compile(r"^\s{2,}\S", re.MULTILINE)


def parse_requires(text: str) -> list[str]:
    """Extract every ``REQUIRES:`` declaration from a spec body.

    The header on the matched line is the first token; quoted forms like
    ``REQUIRES: "ssh:ct204"`` are unquoted before they land in the result.
    Empty tokens are dropped -- they would defeat the membership check below.
    """

    out: list[str] = []
    if not text:
        return out
    for match in _REQUIRES_LINE_RE.finditer(text):
        head = match.group(1).strip()
        if not head:
            continue
        # Strip a single layer of surrounding quotes / backticks.
        if (head[0] == head[-1]) and head[0] in {"\"", "'", "`"} and len(head) >= 2:
            head = head[1:-1].strip()
        out.append(head)
    return out


# ---------------------------------------------------------------------------
# Heuristic detection
# ---------------------------------------------------------------------------


def detect_heuristic_capabilities(text: str) -> list[str]:
    """Return capability names implied by the spec body but not declared explicitly.

    The set is sorted and deduplicated so callers can do a stable diff. The
    heuristics intentionally DO NOT flag common verbs (``run``, ``check``,
    ``verify``) -- the goal is to catch spec-shaped requests for tools the
    executor cannot route to, not to second-guess every imperative sentence.
    """

    if not text:
        return []
    found: set[str] = set()
    for pattern, capability in _SSH_HOST_PATTERNS:
        if pattern.search(text):
            found.add(capability)
    for pattern in _GH_PATTERNS:
        if pattern.search(text):
            found.add("github")
    for pattern in _NPM_PATTERNS:
        if pattern.search(text):
            found.add("npm")
    return sorted(found)


def total_required_capabilities(text: str) -> list[str]:
    """Union of explicit REQUIRES declarations and heuristic detections.

    Order is "explicit then heuristic", duplicates dropped, so the result is
    deterministic: tests assert this list by content, not by index.
    """

    explicit = parse_requires(text)
    explicit = [item for item in explicit if item]
    explicit_set = set(explicit)
    heuristic = [item for item in detect_heuristic_capabilities(text) if item not in explicit_set]
    return list(explicit) + heuristic


# ---------------------------------------------------------------------------
# Profile capability model
# ---------------------------------------------------------------------------


def _profile_servers(profile_name: str) -> tuple[str, ...]:
    """Resolve a profile name to its server tuple via the mcp_profiles registry.

    Unknown profiles are treated as "all servers available" (the gateway's
    ``full`` profile semantics). The "none" profile is the empty tuple -- a
    trio on a profile with no servers fails every capability check that
    requires one. The lookup goes through ``_BUILTINS`` first because that's
    the source of truth; user/project profiles are not consulted here so a
    misconfigured environment does not accidentally widen the gate.
    """

    if profile_name in _BUILTINS:
        spec = _BUILTINS[profile_name]
        servers = spec.get("servers")
        if servers is None:
            return ("*",)  # sentinel meaning "no allow list"
        return tuple(servers)
    return ("*",)


# Map each capability name to the MCP server names that satisfy it.
# ``"*"`` in the profile server tuple means "the gateway has no allow list" --
# every capability that maps to a real server is considered present.
_CAPABILITY_SERVERS: dict[str, tuple[str, ...]] = {
    "ssh": ("ssh",),
    "ssh:ct204": ("proxmoxptait07", "proxmoxct204", "ssh"),
    "ssh:ptait07": ("proxmoxptait07", "ssh"),
    "ssh:ptait09": ("proxmoxptait09", "ssh"),
    "ssh:ptait01": ("proxmoxptait01", "ssh"),
    "github": ("github",),
    "gh": ("github",),
    "npm": ("npm",),
    "youtrack": ("youtrack",),
    "hydra": ("hydra", "hydrafleet"),
    "proxmox": (
        "proxmoxptait01",
        "proxmoxptait07",
        "proxmoxptait09",
        "proxmoxcorsairai300",
        "proxmoxct204",
    ),
}


def profile_has_capability(profile_name: str, capability: str) -> bool:
    """True iff the profile covers ``capability``.

    A profile server tuple containing ``"*"`` covers every mapped capability.
    Unknown capability names fall through to a permissive True (the gate is
    for the closed ``KNOWN_CAPABILITIES`` set; a brand new token should not
    fail closed by accident, but the operator should add it to
    ``_CAPABILITY_SERVERS`` so the next run is gated correctly).
    """

    servers = _profile_servers(profile_name)
    if servers and servers[0] == "*":
        return True
    required = _CAPABILITY_SERVERS.get(capability)
    if required is None:
        return True
    return any(server in servers for server in required)


# ---------------------------------------------------------------------------
# Precheck result
# ---------------------------------------------------------------------------


@dataclass
class PrecheckResult:
    """The gate's answer for one (spec, trio) pair."""

    launch: bool
    missing: list[str] = field(default_factory=list)
    required: list[str] = field(default_factory=list)
    profile: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "launch": self.launch,
            "missing": list(self.missing),
            "required": list(self.required),
            "profile": self.profile,
            "reason": self.reason,
        }


def check_capabilities(
    spec_text: str,
    *,
    profile_name: str,
) -> PrecheckResult:
    """Return the precheck answer for ``spec_text`` against ``profile_name``.

    The ``profile_name`` is the trio's MCP profile (e.g. ``ops``, ``default``,
    ``audit``). When the spec carries no REQUIRES line AND no heuristic
    trigger fires, the gate returns ``launch=True`` (which is the current
    launch behaviour -- no gate applied). This is the contract the test
    suite checks under ``test_no_requires_and_no_heuristic_defaults_to_launch``.
    """

    required = total_required_capabilities(spec_text)
    if not required:
        return PrecheckResult(launch=True, missing=[], required=[], profile=profile_name)
    missing = [
        capability
        for capability in required
        if not profile_has_capability(profile_name, capability)
    ]
    if missing:
        reason = "missing capability: " + missing[0]
        if len(missing) > 1:
            reason += " (+%d more)" % (len(missing) - 1)
        return PrecheckResult(
            launch=False,
            missing=missing,
            required=required,
            profile=profile_name,
            reason=reason,
        )
    return PrecheckResult(launch=True, missing=[], required=required, profile=profile_name)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Lightweight CLI: read a spec file (or stdin), print the gate answer."""

    parser = argparse.ArgumentParser(
        prog="lh-harness capability-precheck",
        description=(
            "Inspect a task spec for REQUIRES lines and obvious capability "
            "heuristics; report whether the named trio profile covers them. "
            "Default exit code is 0 on launch, 2 on missing capability "
            "(chosen so it is indistinguishable from a launcher skip)."
        ),
    )
    parser.add_argument(
        "--spec",
        default=None,
        help="Path to a task spec file. Defaults to stdin when omitted.",
    )
    parser.add_argument(
        "--profile",
        default="default",
        help="MCP profile the trio runs under (default: default).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the precheck result as JSON.",
    )
    args = parser.parse_args(argv)

    if args.spec:
        with open(args.spec, "r", encoding="utf-8") as handle:
            text = handle.read()
    else:
        text = sys.stdin.read()

    result = check_capabilities(text, profile_name=args.profile)
    if args.json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    else:
        print(
            "profile={profile} required={required} missing={missing} launch={launch} reason={reason}".format(
                profile=result.profile,
                required=result.required,
                missing=result.missing,
                launch=result.launch,
                reason=result.reason,
            )
        )
    return 0 if result.launch else 2


if __name__ == "__main__":  # pragma: no cover - module CLI
    raise SystemExit(main())
