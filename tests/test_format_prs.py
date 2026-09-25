"""TASK 245: golden tests for scripts/overseer/format_prs.py.

The golden output is the reference example from the task, reproduced verbatim
in ``docs/LOOP-PROMPT.md`` ("PRESENTING PRs TO PAXTON").  If this test fails,
the helper has drifted from the doctrine — fix the helper, not the doctrine.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "overseer" / "format_prs.py"

# Load the script as a module (it lives in scripts/, not in a package).
spec = importlib.util.spec_from_file_location("format_prs", SCRIPT)
assert spec is not None and spec.loader is not None
fp = importlib.util.module_from_spec(spec)
sys.modules["format_prs"] = fp
spec.loader.exec_module(fp)

GOLDEN = """LongHorizon-Harness (LHH)

1. [cogniziocompany/LongHorizon-Harness#50](https://github.com/cogniziocompany/LongHorizon-Harness/pull/50): queue-stall fix (task 230). Merge this first, since it's what stops the queue getting stuck.
2. [cogniziocompany/LongHorizon-Harness#52](https://github.com/cogniziocompany/LongHorizon-Harness/pull/52): enqueue fields fix (233).
3. [cogniziocompany/LongHorizon-Harness#53](https://github.com/cogniziocompany/LongHorizon-Harness/pull/53): overseer files moved into the repo (104b). It's big (598 files) but mostly records.
4. [cogniziocompany/LongHorizon-Harness#49](https://github.com/cogniziocompany/LongHorizon-Harness/pull/49): CI deploy workflow (224). It needs setup after merge, which I'll do.
5. [cogniziocompany/LongHorizon-Harness#51](https://github.com/cogniziocompany/LongHorizon-Harness/pull/51): queue drain switch (242). Only after #49, because it's built on #49's branch.

cognizioware-hydra

* [cogniziocompany/cognizioware-hydra#32](https://github.com/cogniziocompany/cognizioware-hydra/pull/32): hydra sees CT110 runs (232).
* [cogniziocompany/cognizioware-hydra#33](https://github.com/cogniziocompany/cognizioware-hydra/pull/33): removes the dead `rc_*` tools (237).
* [cogniziocompany/cognizioware-hydra#34](https://github.com/cogniziocompany/cognizioware-hydra/pull/34): tool error handling and stale devices (238).

cognizioware-mcp-tools

* [cogniziocompany/cognizioware-mcp-tools#185](https://github.com/cogniziocompany/cognizioware-mcp-tools/pull/185): fleet.easybutt0n.ai accuracy (231). This fixes the 12 dead runs showing as live.
* [cogniziocompany/cognizioware-mcp-tools#186](https://github.com/cogniziocompany/cognizioware-mcp-tools/pull/186): Penpot (241).
* [cogniziocompany/cognizioware-mcp-tools#188](https://github.com/cogniziocompany/cognizioware-mcp-tools/pull/188): keeps the chat's `ssh` tool off the Windows PCs (239). One review point: chat gets the Proxmox hosts only, not CTs."""


def _lhh_group() -> "fp.RepoGroup":
    """The example's LHH group: PR #51 is stacked on PR #49's branch.

    Rows are given in the merge order the overseer presents (#50 first — the
    queue stall), which the formatter must preserve while guaranteeing the
    stacked #51 renders after #49.
    """
    return fp.RepoGroup(
        repo="LongHorizon-Harness",
        prs=[
            fp.PRRow(
                repo="LongHorizon-Harness",
                number=50,
                head_ref="fix/task-230-queue-stall",
                base_ref="main",
                description="queue-stall fix",
                task_label="task 230",
                note="Merge this first, since it's what stops the queue getting stuck.",
            ),
            fp.PRRow(
                repo="LongHorizon-Harness",
                number=52,
                head_ref="fix/task-233-enqueue-fields",
                base_ref="main",
                description="enqueue fields fix",
                task_label="233",
            ),
            fp.PRRow(
                repo="LongHorizon-Harness",
                number=53,
                head_ref="wip/task-104-apparatus-migration",
                base_ref="main",
                description="overseer files moved into the repo",
                task_label="104b",
                note="It's big (598 files) but mostly records.",
            ),
            fp.PRRow(
                repo="LongHorizon-Harness",
                number=49,
                head_ref="feat/task-224-ci-deploy-ct110",
                base_ref="main",
                description="CI deploy workflow",
                task_label="224",
                note="It needs setup after merge, which I'll do.",
            ),
            fp.PRRow(
                repo="LongHorizon-Harness",
                number=51,
                head_ref="feat/task-242-queue-drain",
                base_ref="feat/task-224-ci-deploy-ct110",
                description="queue drain switch",
                task_label="242",
                note="Only after #49, because it's built on #49's branch.",
            ),
        ],
    )


def _hydra_group() -> "fp.RepoGroup":
    return fp.RepoGroup(
        repo="cognizioware-hydra",
        prs=[
            fp.PRRow(
                repo="cognizioware-hydra",
                number=32,
                head_ref="feat/task-232-ct110-runs",
                base_ref="main",
                description="hydra sees CT110 runs",
                task_label="232",
            ),
            fp.PRRow(
                repo="cognizioware-hydra",
                number=33,
                head_ref="chore/task-237-remove-rc-tools",
                base_ref="main",
                description="removes the dead `rc_*` tools",
                task_label="237",
            ),
            fp.PRRow(
                repo="cognizioware-hydra",
                number=34,
                head_ref="fix/task-238-tool-errors",
                base_ref="main",
                description="tool error handling and stale devices",
                task_label="238",
            ),
        ],
    )


def _mcp_tools_group() -> "fp.RepoGroup":
    return fp.RepoGroup(
        repo="cognizioware-mcp-tools",
        prs=[
            fp.PRRow(
                repo="cognizioware-mcp-tools",
                number=185,
                head_ref="fix/task-231-wallboard-accuracy",
                base_ref="main",
                description="fleet.easybutt0n.ai accuracy",
                task_label="231",
                note="This fixes the 12 dead runs showing as live.",
            ),
            fp.PRRow(
                repo="cognizioware-mcp-tools",
                number=186,
                head_ref="feat/task-241-penpot",
                base_ref="main",
                description="Penpot",
                task_label="241",
            ),
            fp.PRRow(
                repo="cognizioware-mcp-tools",
                number=188,
                head_ref="fix/task-239-chat-ssh-scope",
                base_ref="main",
                description="keeps the chat's `ssh` tool off the Windows PCs",
                task_label="239",
                note="One review point: chat gets the Proxmox hosts only, not CTs.",
            ),
        ],
    )


def test_golden_reference_example_is_reproduced_exactly() -> None:
    """The doctrine's reference example is the golden output, byte for byte."""
    assert fp.render([_lhh_group(), _hydra_group(), _mcp_tools_group()]) == GOLDEN


def test_merge_order_puts_a_stacked_pr_after_its_base() -> None:
    """#51's base is #49's branch, so #51 must render AFTER #49, numbered."""
    rendered = fp.render([_lhh_group()])
    lines = rendered.splitlines()
    pr_49 = next(i for i, l in enumerate(lines) if "LongHorizon-Harness#49" in l)
    pr_51 = next(i for i, l in enumerate(lines) if "LongHorizon-Harness#51" in l)
    assert pr_51 > pr_49
    # The group carries a stack, so it is a NUMBERED list in merge order.
    # lines[0] is the heading, lines[1] the blank separator, items follow.
    assert lines[2].startswith("1. [cogniziocompany/LongHorizon-Harness#50]")
    assert lines[3].startswith("2. [cogniziocompany/LongHorizon-Harness#52]")
    assert lines[6].startswith("5. [cogniziocompany/LongHorizon-Harness#51]")


def test_stack_reorders_a_stacked_row_that_was_reported_first() -> None:
    """A stacked PR listed before its base still renders after it."""
    group = fp.RepoGroup(
        repo="testrepo",
        prs=[
            fp.PRRow(repo="testrepo", number=2, head_ref="feat-b", base_ref="feat-a"),
            fp.PRRow(repo="testrepo", number=1, head_ref="feat-a", base_ref="main"),
        ],
    )
    lines = fp.render([group]).splitlines()
    assert lines[2].startswith("1. [cogniziocompany/testrepo#1]")
    assert lines[3].startswith("2. [cogniziocompany/testrepo#2]")


def test_no_stacked_pr_renders_a_bulleted_list() -> None:
    """Rule 4: without a stack, the group is a bulleted list, not numbered."""
    rendered = fp.render([_hydra_group()])
    for line in rendered.splitlines()[2:]:
        assert line.startswith("* [cogniziocompany/cognizioware-hydra#"), line


def test_link_text_is_full_owner_repo_ref_and_target_is_full_url() -> None:
    """Rule 2: never a bare #N, never a bare URL."""
    rendered = fp.render([_mcp_tools_group()])
    assert (
        "[cogniziocompany/cognizioware-mcp-tools#185]"
        "(https://github.com/cogniziocompany/cognizioware-mcp-tools/pull/185): "
        "fleet.easybutt0n.ai accuracy (231)"
    ) in rendered


def test_group_heading_uses_the_display_name_map() -> None:
    """Rule 1: the LHH group heading carries the (LHH) suffix; others don't."""
    rendered = fp.render([_lhh_group(), _hydra_group()])
    assert rendered.startswith("LongHorizon-Harness (LHH)\n")
    assert "\n\ncognizioware-hydra\n" in rendered


def test_format_pr_rows_groups_plain_gh_json_rows_by_repo() -> None:
    """The gh-driven path renders the same shape from raw ``gh pr list`` JSON."""
    rows = [
        {
            "repository": {"name": "cognizioware-hydra"},
            "number": 32,
            "headRefName": "feat/task-232-ct110-runs",
            "baseRefName": "main",
        },
        {
            "repository": {"name": "cognizioware-hydra"},
            "number": 33,
            "headRefName": "chore/task-237-remove-rc-tools",
            "baseRefName": "main",
        },
    ]
    rendered = fp.format_pr_rows(rows)
    expected = (
        "cognizioware-hydra\n"
        "\n"
        "* [cogniziocompany/cognizioware-hydra#32]"
        "(https://github.com/cogniziocompany/cognizioware-hydra/pull/32):\n"
        "* [cogniziocompany/cognizioware-hydra#33]"
        "(https://github.com/cogniziocompany/cognizioware-hydra/pull/33):"
    )
    assert rendered == expected


def test_transitive_stack_resolves_chain_order() -> None:
    """A -> B -> main renders A after B, both numbered after independents."""
    group = fp.RepoGroup(
        repo="testrepo",
        prs=[
            fp.PRRow(repo="testrepo", number=2, head_ref="feat-b", base_ref="feat-a"),
            fp.PRRow(repo="testrepo", number=1, head_ref="feat-a", base_ref="main"),
            fp.PRRow(repo="testrepo", number=3, head_ref="feat-c", base_ref="main"),
        ],
    )
    lines = fp.render([group]).splitlines()
    assert lines[0] == "testrepo"
    order = [lines[i].split("testrepo#")[1].split("]")[0] for i in (2, 3, 4)]
    # #2 stacks on #1's branch, so #2 must render after #1; #3 is independent.
    assert order.index("2") > order.index("1")