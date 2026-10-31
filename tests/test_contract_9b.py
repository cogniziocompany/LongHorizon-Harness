"""Tests for the compact 9B task-contract parser/renderer."""

from __future__ import annotations

import pytest

from lh_harness.contract_9b import (
    Contract9bError,
    parse_contract_9b,
    render_contract_9b,
    REQUIRED_SECTIONS,
)


def _full_contract() -> str:
    return (
        "GOAL: refactor trim helper to module scope\n"
        "FILES: src/lh_harness/tool_output_trimming.py\n"
        "       tests/test_tool_output_trimming.py\n"
        "EXACT EDIT: move the leading_whitespace() helper out of _local() "
        "and into module scope so pytest can import it.\n"
        "TEST: pytest -q tests/test_tool_output_trimming.py\n"
        "DONE WHEN: pytest exits 0 and the helper is importable as "
        "lh_harness.tool_output_trimming.leading_whitespace.\n"
    )


_FULL_DICT = {
    "goal": "refactor trim helper to module scope",
    "files": [
        "src/lh_harness/tool_output_trimming.py",
        "tests/test_tool_output_trimming.py",
    ],
    "exact_edit": (
        "move the leading_whitespace() helper out of _local() "
        "and into module scope so pytest can import it."
    ),
    "test": "pytest -q tests/test_tool_output_trimming.py",
    "done_when": (
        "pytest exits 0 and the helper is importable as "
        "lh_harness.tool_output_trimming.leading_whitespace."
    ),
}


def test_round_trip_basic() -> None:
    text = render_contract_9b(_FULL_DICT)
    parsed = parse_contract_9b(text)
    assert parsed == _FULL_DICT


def test_round_trip_from_text() -> None:
    parsed = parse_contract_9b(_full_contract())
    assert parsed == _FULL_DICT
    re_text = render_contract_9b(parsed)
    re_parsed = parse_contract_9b(re_text)
    assert re_parsed == _FULL_DICT


def test_extra_whitespace_tolerated() -> None:
    messy = (
        "\n"
        "   GOAL:    refactor trim helper to module scope   \n"
        "\n"
        "FILES:    src/lh_harness/tool_output_trimming.py\n"
        "\n"
        "          tests/test_tool_output_trimming.py\n"
        "\n"
        "\n"
        "EXACT EDIT:    move the leading_whitespace() helper out of _local() "
        "and into module scope so pytest can import it.\n"
        "\n"
        "TEST:    pytest -q tests/test_tool_output_trimming.py\n"
        "\n"
        "DONE WHEN:    pytest exits 0 and the helper is importable as "
        "lh_harness.tool_output_trimming.leading_whitespace.\n"
        "\n"
    )
    parsed = parse_contract_9b(messy)
    assert parsed == _FULL_DICT


@pytest.mark.parametrize("missing_key,missing_header", [
    ("goal", "GOAL"),
    ("files", "FILES"),
    ("exact_edit", "EXACT EDIT"),
    ("test", "TEST"),
    ("done_when", "DONE WHEN"),
])
def test_missing_section_raises(missing_key: str, missing_header: str) -> None:
    section_lines = {
        "goal": "GOAL: refactor trim helper",
        "files": "FILES: src/lh_harness/tool_output_trimming.py",
        "exact_edit": "EXACT EDIT: move helper",
        "test": "TEST: pytest -q",
        "done_when": "DONE WHEN: passes",
    }
    text = "\n".join(
        line for k, line in section_lines.items() if k != missing_key
    ) + "\n"
    with pytest.raises(Contract9bError) as excinfo:
        parse_contract_9b(text)
    msg = str(excinfo.value)
    assert missing_header in msg, (
        f"missing-section message should name '{missing_header}', got: {msg!r}"
    )


def test_render_then_parse_equivalent() -> None:
    d = {
        "goal": "ship it",
        "files": ["a.py", "b.py"],
        "exact_edit": "replace foo with bar",
        "test": "pytest -q",
        "done_when": "tests pass",
    }
    text = render_contract_9b(d)
    assert parse_contract_9b(text) == d


def test_render_rejects_missing_key() -> None:
    d = {
        "goal": "ship it",
        "files": ["a.py"],
        "exact_edit": "replace foo",
        "test": "pytest -q",
    }
    with pytest.raises(Contract9bError):
        render_contract_9b(d)


def test_render_rejects_non_str_files_entry() -> None:
    d = {
        "goal": "ship it",
        "files": ["a.py", 42],  # type: ignore[list-item]
        "exact_edit": "replace foo",
        "test": "pytest -q",
        "done_when": "tests pass",
    }
    with pytest.raises(Contract9bError):
        render_contract_9b(d)


def test_render_rejects_wrong_files_type() -> None:
    d = {
        "goal": "ship it",
        "files": "a.py",  # type: ignore[dict-item]
        "exact_edit": "replace foo",
        "test": "pytest -q",
        "done_when": "tests pass",
    }
    with pytest.raises(Contract9bError):
        render_contract_9b(d)


def test_parse_empty_string_raises() -> None:
    with pytest.raises(Contract9bError):
        parse_contract_9b("")


def test_parse_non_string_raises() -> None:
    with pytest.raises(Contract9bError):
        parse_contract_9b(12345)  # type: ignore[arg-type]


def test_blank_lines_around_sections_only_trimmed_at_edges() -> None:
    """Internal blank lines in a multi-line body are preserved."""
    d = {
        "goal": "ship it",
        "files": ["a.py"],
        "exact_edit": "line one\n\nline two after blank",
        "test": "pytest -q",
        "done_when": "tests pass",
    }
    text = render_contract_9b(d)
    parsed = parse_contract_9b(text)
    assert parsed["exact_edit"] == "line one\n\nline two after blank"


def test_files_with_multiple_paths() -> None:
    text = (
        "GOAL: bump\n"
        "FILES: src/lh_harness/a.py\n"
        "       src/lh_harness/b.py\n"
        "       tests/test_a.py\n"
        "EXACT EDIT: bump version\n"
        "TEST: pytest -q\n"
        "DONE WHEN: passes\n"
    )
    parsed = parse_contract_9b(text)
    assert parsed["files"] == [
        "src/lh_harness/a.py",
        "src/lh_harness/b.py",
        "tests/test_a.py",
    ]


def test_section_header_aliases_tolerated() -> None:
    """9B models sometimes emit EXACT_EDIT / DONE_WHEN underscore variants."""
    text = (
        "GOAL: ship\n"
        "FILES: a.py\n"
        "EXACT_EDIT: bump\n"
        "TEST: pytest -q\n"
        "DONE_WHEN: passes\n"
    )
    parsed = parse_contract_9b(text)
    assert parsed["exact_edit"] == "bump"
    assert parsed["done_when"] == "passes"
