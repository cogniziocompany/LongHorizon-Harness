"""Compact task-contract format for 9B local models.

The 9B contract is a short, fixed-section markdown-ish block that small local
models can reliably follow. It is intentionally simple — no nested keys, no
implicit defaults, no recovery heuristics.

Five sections, in order:

    GOAL: <text>
    FILES: <path1>
           <path2>
    EXACT EDIT: <text>
    TEST: <text>
    DONE WHEN: <text>

Each section header is a literal uppercase keyword followed by a colon. The
section body runs from the header colon to the next header line (or EOF). All
leading/trailing whitespace on individual lines is stripped, and empty lines
around a section body are ignored. Extra inter-line whitespace between the
header and the first body line is tolerated.

Parser contract:
    parse_contract_9b(text) -> dict with keys
        "goal"        : str
        "files"       : list[str]
        "exact_edit"  : str
        "test"        : str
        "done_when"   : str
    Raises ValueError naming the missing section if any of those are absent.

Renderer contract:
    render_contract_9b(d) -> text. The dict must contain all five keys.
    The output round-trips back through parse_contract_9b to an equivalent dict.
"""

from __future__ import annotations

import re
from typing import Mapping

__all__ = [
    "REQUIRED_SECTIONS",
    "SECTION_HEADERS",
    "parse_contract_9b",
    "render_contract_9b",
    "Contract9bError",
]

REQUIRED_SECTIONS = ("goal", "files", "exact_edit", "test", "done_when")

# Canonical spelling used in render output and accepted on parse.
SECTION_HEADERS = {
    "goal": "GOAL",
    "files": "FILES",
    "exact_edit": "EXACT EDIT",
    "test": "TEST",
    "done_when": "DONE WHEN",
}

# Lines that begin a section: an uppercase (possibly multi-word) header,
# a colon, then either end-of-line or body content. We capture the header
# text and the rest of the line separately so a body line like
#     "GOAL: refactor trim helper..."
# is detected as a GOAL header with body "refactor trim helper...".
# Headers may use spaces ("EXACT EDIT") or underscores ("EXACT_EDIT").
_HEADER_RE = re.compile(
    r"^(?P<key>(?:[A-Z]+(?:[ _][A-Z]+)*))\s*:\s*(?P<body>.*)$"
)


class Contract9bError(ValueError):
    """Raised when a contract is malformed or missing a required section."""


def _split_lines(text: str) -> list[str]:
    # Normalize newlines; do not strip trailing whitespace from individual
    # content lines — body content is preserved verbatim aside from explicit
    # leading/trailing blank-line trims at section granularity.
    return text.splitlines()


def _parse_body_lines(raw_lines: list[str]) -> list[str]:
    """Trim surrounding blank lines from a section body, preserving internal blanks."""
    start = 0
    end = len(raw_lines)
    while start < end and raw_lines[start].strip() == "":
        start += 1
    while end > start and raw_lines[end - 1].strip() == "":
        end -= 1
    return raw_lines[start:end]


def parse_contract_9b(text: str) -> dict:
    """Parse a 9B task contract.

    Args:
        text: Multi-line contract text with the five required sections.

    Returns:
        Dict with keys ``goal``, ``files``, ``exact_edit``, ``test``,
        ``done_when``. ``files`` is always a list of str; the other four
        values are str.

    Raises:
        Contract9bError: If any required section is missing or the input
            cannot be parsed. The exception message names the missing key.
    """
    if not isinstance(text, str):
        raise Contract9bError("contract text must be a str")

    lines = _split_lines(text)

    # Map uppercase header (as written) -> body lines collected after it.
    section_aliases = {header: key for key, header in SECTION_HEADERS.items()}
    # Allow some tolerant alternates that 9B models occasionally emit.
    section_aliases["EXACT_EDIT"] = "exact_edit"
    section_aliases["DONE_WHEN"] = "done_when"

    bodies: dict[str, list[str]] = {}
    current_key: str | None = None
    current_buf: list[str] = []

    def _flush() -> None:
        if current_key is None:
            return
        trimmed = _parse_body_lines(current_buf)
        # First occurrence wins; later duplicate headers are ignored.
        bodies.setdefault(current_key, trimmed)

    for raw in lines:
        stripped = raw.strip()
        m = _HEADER_RE.match(stripped) if stripped else None
        if m:
            header_text = m.group("key").strip().upper()
            inline_body = m.group("body")
            key = section_aliases.get(header_text)
            if key is not None:
                _flush()
                current_key = key
                current_buf = []
                if inline_body:
                    current_buf.append(inline_body)
                continue
        if current_key is not None:
            current_buf.append(raw)
    _flush()

    # Assemble the canonical output. For "files", split on newlines and blank
    # separators, keep non-empty entries as-written.
    result: dict = {}
    missing: list[str] = []

    for key in REQUIRED_SECTIONS:
        if key not in bodies:
            missing.append(key)
            continue
        if key == "files":
            items = [
                ln.strip()
                for ln in bodies[key]
                if ln.strip() != ""
            ]
            result[key] = items
        else:
            # Re-join the body to its original string form, but trim only the
            # surrounding blank lines (not interior ones).
            result[key] = "\n".join(bodies[key]).strip("\n").rstrip()

    if missing:
        pretty = ", ".join(SECTION_HEADERS[k] for k in missing)
        raise Contract9bError(f"missing required section(s): {pretty}")

    return result


def render_contract_9b(d: Mapping[str, object]) -> str:
    """Render a 9B contract dict back to its canonical text form.

    Args:
        d: Mapping with all five keys ``goal``, ``files``, ``exact_edit``,
            ``test``, ``done_when``.

    Returns:
        Multi-line text that round-trips through :func:`parse_contract_9b`
        to an equivalent dict.

    Raises:
        Contract9bError: If any required key is missing or has the wrong type.
    """
    out: list[str] = []
    for key in REQUIRED_SECTIONS:
        if key not in d:
            raise Contract9bError(
                f"missing required section(s): {SECTION_HEADERS[key]}"
            )
        out.append(f"{SECTION_HEADERS[key]}:")
        value = d[key]
        if key == "files":
            if not isinstance(value, (list, tuple)):
                raise Contract9bError("'files' must be a list of paths")
            for item in value:
                if not isinstance(item, str):
                    raise Contract9bError("'files' entries must be strings")
                out.append(item)
            out.append("")  # blank separator between sections
        else:
            if not isinstance(value, str):
                raise Contract9bError(
                    f"'{key}' must be a str"
                )
            out.append(value)
            out.append("")

    # Drop trailing blank line(s) but keep exactly one trailing newline.
    while len(out) >= 2 and out[-1] == "" and out[-2] == "":
        out.pop()
    if not out or out[-1] != "":
        out.append("")
    return "\n".join(out)
