from __future__ import annotations

import pytest

from lh_harness.auditor_agent import (
    has_valid_auditor_control_header,
    infer_contract_audit_status,
    infer_report_status,
)
from lh_harness.role_prompts import (
    MANAGER_NEXT_CLI,
    MANAGER_NEXT_DONE,
    MANAGER_NEXT_INVALID,
    parse_role_manager_next_step,
)


@pytest.mark.parametrize(
    "text",
    [
        "Plan text.\nNext: cli",
        "Plan text.\n**Next:** cli",
        "Plan text.\n`Next: cli`",
        "Plan text.\nNext: `cli`",
        "Plan text.\n> Next: cli",
        "Plan text.\nNext step: cli",
    ],
)
def test_manager_route_tolerates_markdown(text):
    assert parse_role_manager_next_step(text) == MANAGER_NEXT_CLI


def test_manager_route_still_rejects_prose():
    assert parse_role_manager_next_step("Next: cli later maybe") == MANAGER_NEXT_INVALID
    assert parse_role_manager_next_step("no route here") == MANAGER_NEXT_INVALID


def test_manager_route_done_with_rationale_in_bold():
    assert parse_role_manager_next_step("**Next:** done — all checks passed") == MANAGER_NEXT_DONE


def test_control_header_plain_still_valid():
    text = "Status: complete\nIntegrity: clean\nContract audit: aligned\n\nFindings."
    assert has_valid_auditor_control_header(text)
    assert infer_report_status(text) == "complete"


def test_control_header_markdown_bold_values():
    text = "**Status:** complete\n**Integrity:** clean\n**Contract audit:** aligned\n\nFindings."
    assert has_valid_auditor_control_header(text)
    assert infer_report_status(text) == "complete"
    assert infer_contract_audit_status(text) == "aligned"


def test_control_header_after_short_preamble():
    text = "Here is my audit.\n\nStatus: incomplete\nIntegrity: suspect\nContract audit: needs_revision\n\nFindings."
    assert has_valid_auditor_control_header(text)
    assert infer_report_status(text) == "incomplete"
    assert infer_contract_audit_status(text) == "needs_revision"


def test_control_header_missing_is_still_invalid():
    assert not has_valid_auditor_control_header("I reviewed the work and it looks fine.")
