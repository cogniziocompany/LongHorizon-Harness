#!/usr/bin/env python3
"""TASK 245: format open PRs for presentation to Paxton.

Every overseer actor (the CT overseer tick, the chat Fleet Operator, or any
session reporting for the overseer) presents PRs in exactly one format — see
``docs/LOOP-PROMPT.md``, section "PRESENTING PRs TO PAXTON".  This helper emits
it so the format is produced, not retyped.

Rules implemented here:
  - PRs are grouped by repository, group heading is the plain repo name
    (``LongHorizon-Harness (LHH)`` style).
  - One line per PR: the link TEXT is the full ``owner/repo#N`` reference and
    the link TARGET is the full PR URL.  Never a bare ``#N``, never a bare URL.
  - After the link: a colon, the short description, the task number in
    parentheses, then any action sentence.
  - A NUMBERED list in merge order when merge order matters within a repo;
    otherwise a bulleted list.  Merge order matters when a PR's base branch is
    another open PR's branch (a stacked PR) — the stacked PR goes AFTER the
    one it stacks on.

Input is the JSON rows of ``gh pr list --state open --json ...`` (fields:
``number``, ``headRefName``, ``baseRefName``, ``title``, optionally
``repository``/``headRepositoryOwner``).  A live pull (``gh pr list`` per
repo) is the caller's job; the formatter only renders verified rows.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional


OWNER = "cogniziocompany"

# Plain group headings for repos that carry one (rule 1).  Anything else
# renders under its bare repo name.
DISPLAY_NAMES = {
    "LongHorizon-Harness": "LongHorizon-Harness (LHH)",
}


@dataclass
class PRRow:
    """One open PR as the formatter consumes it.

    ``repo`` is the bare repo name (e.g. ``LongHorizon-Harness``).
    """

    repo: str
    number: int
    head_ref: str
    base_ref: str
    description: str = ""
    # The task reference exactly as the doctrine's example renders it —
    # "(task 230)", "(233)", "(104b)" — a raw label, not re-derived.
    task_label: str = ""
    note: str = ""

    @property
    def url(self) -> str:
        return f"https://github.com/{OWNER}/{self.repo}/pull/{self.number}"

    @property
    def ref(self) -> str:
        return f"{OWNER}/{self.repo}#{self.number}"


@dataclass
class RepoGroup:
    repo: str
    prs: List[PRRow] = field(default_factory=list)

    @property
    def heading(self) -> str:
        return DISPLAY_NAMES.get(self.repo, self.repo)


def _merge_order(group: RepoGroup) -> List[PRRow]:
    """Return the group's PRs in merge order, or [] if no ordering applies.

    Merge order matters only when some PR's base branch is another open PR's
    head branch within the same repo (a stacked PR); such a PR sorts AFTER the
    one it stacks on.  Chains resolve transitively.  Everything else keeps
    input order.
    """
    by_head: Dict[str, PRRow] = {pr.head_ref: pr for pr in group.prs}
    stacked_bases = {pr.base_ref for pr in group.prs if pr.base_ref in by_head}
    if not stacked_bases:
        return []
    ordered: List[PRRow] = []
    placed: List[int] = []

    def emit(pr: PRRow) -> None:
        if id(pr) in placed:
            return
        base_pr = by_head.get(pr.base_ref)
        if base_pr is not None:
            emit(base_pr)  # the PR a stack sits on merges first
        placed.append(id(pr))
        ordered.append(pr)

    for pr in group.prs:
        emit(pr)
    return ordered


def _line(pr: PRRow) -> str:
    parts = [f"[{pr.ref}]({pr.url})"]
    label = f" ({pr.task_label})" if pr.task_label else ""
    if pr.description:
        parts.append(f": {pr.description}{label}")
    elif pr.task_label:
        parts.append(f":{label}")
    else:
        parts.append(":")
    if pr.note:
        parts.append(f". {pr.note}")
    elif pr.description or pr.task_label:
        parts.append(".")
    return "".join(parts)


def render(groups: Iterable[RepoGroup]) -> str:
    """Render repo groups in the Paxton presentation format."""
    out: List[str] = []
    for index, group in enumerate(groups):
        if index:
            out.append("")
        out.append(group.heading)
        out.append("")
        ordered = _merge_order(group)
        if ordered:
            for i, pr in enumerate(ordered, 1):
                out.append(f"{i}. {_line(pr)}")
        else:
            for pr in group.prs:
                out.append(f"* {_line(pr)}")
    return "\n".join(out)


def format_pr_rows(rows: Iterable[dict]) -> str:
    """Convenience: render plain ``gh pr list`` JSON rows (no descriptions).

    Each row needs ``repository.name`` (or ``baseRefName``'s repo when the
    list is already per-repo), ``number``, ``headRefName``, ``baseRefName``.
    """
    by_repo: "OrderedDict[str, List[PRRow]]" = OrderedDict()
    for row in rows:
        repo = (row.get("repository") or {}).get("name") or row.get("repo") or ""
        pr = PRRow(
            repo=repo,
            number=int(row["number"]),
            head_ref=row.get("headRefName", ""),
            base_ref=row.get("baseRefName", ""),
        )
        by_repo.setdefault(repo, []).append(pr)
    return render(RepoGroup(repo=repo, prs=prs) for repo, prs in by_repo.items())


def pull_open_prs(repo: str) -> List[dict]:
    """Fetch one repo's open PR rows live via gh (rule 5: verified at report time).

    Always pass the repo explicitly: from this checkout a bare ``gh pr list``
    resolves to the wrong remote (see docs/LOOP-PROMPT.md, STEP 3).
    """
    cmd = [
        "gh",
        "pr",
        "list",
        "--repo",
        f"{OWNER}/{repo}",
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        "number,headRefName,baseRefName,title",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(proc.stdout or "[]")


def main(argv: List[str]) -> int:
    """Render rows read from stdin (``gh pr list --json ...`` output)."""
    rows = json.loads(sys.stdin.read() or "[]")
    sys.stdout.write(format_pr_rows(rows) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))