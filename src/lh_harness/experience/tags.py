"""Domain tags and per-role identity for L1 traces (pure, deterministic).

Everything a trace records about *where* the run happened is derived here
from data the run already produced — no LLM call, no network, and no writes:

* ``repo`` — ``control/owner.json`` carries the workspace path; the repo tag
  is its basename (``/home/harness/work/LongHorizon-Harness`` →
  ``LongHorizon-Harness``).
* ``branch`` — parsed from the task text first (``branch: feat/x``,
  ``on branch feat/x``). The git fallback is a read-only
  ``git rev-parse --abbrev-ref HEAD`` against a copy *inside the run dir*
  (``never`` the live workspace: the candidate must resolve under the run
  dir, and a caller cannot point it anywhere else). If no run-dir copy
  exists — today's harness does not place one — the branch stays absent.
* ``next_step`` — the round's resolved next step normalised to ``gui`` /
  ``cli``; anything else (``done``, ``ask``, …) is not a branch choice.
* ``tool_names`` — the ``tool_use`` names from the round's *normalised*
  trajectories (``{role}_trajectory.jsonl``), first-appearance order,
  malformed lines skipped.
* ``error_signature`` — the first line of the auditor's ``blockers`` list
  (the ``Blocking constraints:`` section of the report text, en/zh), else a
  run-level ``provider_*`` abort reason. The extracted line is redacted: a
  blocker can quote a secret the auditor saw.
* model trio — ``owner.route.bound.roles`` when the run was routed (task 49
  handoff shape: ``{role}.{model, backend, tier, rationale}``), falling back
  to ``owner.role_configs``; a legacy owner that carries neither replies with
  the top-level ``agent``/``model`` for all three roles. Route rationales are
  redacted here and length-capped by ``RoleTrace``; never written raw.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from .redact import redact_text
from .trace import RoleTrace

PUBLIC_ROLE_NAMES: tuple[str, ...] = ("manager", "executor", "auditor")

# Subpaths a read-only git probe may target, always relative to the run dir.
# Anything else (notably the live workspace) is refused by construction.
_RUN_DIR_REPO_CANDIDATES: tuple[str, ...] = ("workspace_copy", "repo_copy")

_GIT_PROBE_TIMEOUT_SECONDS = 5

# Bounded read of one normalised trajectory: generous but finite.
_TRAJECTORY_READ_MAX_BYTES = 8 * 1024 * 1024

ERROR_SIGNATURE_MAX_CHARS = 256

_BRANCH_FROM_TEXT_RE = re.compile(
    r"\b(?:on\s+)?branch\b\s*[:=]?\s*[`'\"]?([A-Za-z0-9][\w./-]{0,127})[`'\"]?",
    re.I,
)

# Mirrors the auditor contract prompt vocabulary (auditor_agent's blocking
# acceptance-constraint section), en + zh.
_BLOCKER_SECTION_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:阻断约束|blocking\s+(?:acceptance\s+)?(?:constraints?|claims?))\s*[:：]\s*(?P<rest>.*)$",
    re.I,
)
_BLOCKER_NO_OP_RE = re.compile(
    r"(?ix)^\s*(?:[-*+]\s*|\d+[.)]\s*)?(?:无|没有|暂无|none|nothing|n/?a|not\s+applicable)(?:\s*[。.,，;；:].*)?\s*$"
)
# A line that names another report section terminates the blockers list. The
# heading vocabulary is the auditor contract's own (mirrors
# auditor_agent._ACCEPTANCE_SECTION_BOUNDARY_RE) plus markdown headings and
# the report's control-line labels, so a blocker sentence that *contains* a
# colon never ends the section by accident — while ``Next step: keep going``
# (colon not at line end) still does.
_BLOCKER_SECTION_END_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s+|(?:[-*]\s*)?(?:契约结论|可能评分风险|过窄或错误解释|建议契约修订|审计事实|证据|缺口|下一步|给任务管理器的状态更新"
    r"|验收约束反查|原题约束清单|契约覆盖检查|逐项反查"
    r"|contract\s+conclusion|possible\s+scoring\s+risks|over-narrow|recommended\s+contract\s+revision"
    r"|audit\s+facts|evidence|gaps?|next\s+step|state\s+update\s+for\s+manager|status|integrity|contract\s+audit"
    r"|acceptance[-\s]*constraint\s+backcheck|original\s+constraint\s+inventory|contract\s+coverage\s+check"
    r"|per[-\s]*constraint\s+backcheck)\s*[:：])"
)

_BULLET_RE = re.compile(r"^[-*+]\s+")

_PROVIDER_PREFIX = "provider_"

_NEXT_STEP_TAGS = {"gui": "gui", "cli": "cli"}


def repo_from_workspace_path(workspace: str | os.PathLike[str]) -> str:
    """Repo tag: the basename of the workspace path (owner.json source).

    Workspaces exist on Windows hosts too, so both separators split — a
    recorded ``C:\\Users\\x\\checkout`` still yields ``checkout``.
    """
    text = str(workspace or "").strip().rstrip("/\\")
    if not text:
        return ""
    parts = [part for part in re.split(r"[/\\]+", text) if part]
    return parts[-1] if parts else ""


def repo_from_owner(owner: Mapping[str, Any] | None) -> str:
    """Repo tag from the supervisor's ``control/owner.json`` workspace."""
    data = owner if isinstance(owner, Mapping) else {}
    return repo_from_workspace_path(str(data.get("workspace") or ""))


def branch_from_task_text(task_text: str) -> str:
    """Branch tag parsed from the task text (``branch: feat/x`` / ``on branch``)."""
    match = _BRANCH_FROM_TEXT_RE.search(str(task_text or ""))
    if not match:
        return ""
    branch = match.group(1).strip().strip(".,;")
    if not branch or branch.lower() in {"and", "or", "to", "the", "a", "in"}:
        return ""
    return branch[:128]


def branch_from_run_dir_copy(
    run_dir: str | os.PathLike[str],
    *,
    relpaths: Sequence[str] = _RUN_DIR_REPO_CANDIDATES,
) -> str:
    """Branch from a read-only ``git rev-parse`` against a copy *inside* the run dir.

    Hard rule (kb-hook workspace-write hazard): the live workspace is never
    touched. The probe only runs when the candidate resolves to a directory
    beneath the run dir itself and looks like a git checkout; every failure
    mode (missing copy, detached/empty branch, git absent, timeout) degrades
    to an absent (empty) branch rather than an exception.
    """
    try:
        root = Path(run_dir).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return ""
    for relpath in relpaths or ():
        candidate = _contained_child(root, relpath)
        if candidate is None or not (candidate / ".git").exists():
            continue
        branch = _rev_parse_head(candidate)
        if branch:
            return branch
    return ""


def detect_branch(
    task_text: str = "",
    run_dir: str | os.PathLike[str] | None = None,
    *,
    relpaths: Sequence[str] = _RUN_DIR_REPO_CANDIDATES,
) -> str:
    """Resolved branch tag: task text first, then a read-only run-dir copy probe."""
    branch = branch_from_task_text(task_text)
    if branch:
        return branch
    if run_dir is not None:
        return branch_from_run_dir_copy(run_dir, relpaths=relpaths)
    return ""


def next_step_tag(next_step: Any) -> str:
    """Normalised next-step tag: ``gui`` or ``cli`` only; anything else is absent."""
    value = str(next_step or "").strip().lower()
    return _NEXT_STEP_TAGS.get(value, "")


def tool_names_from_steps(steps: Sequence[Any]) -> list[str]:
    """``tool_use`` names from normalised trajectory steps, first-appearance order."""
    names: list[str] = []
    seen: set[str] = set()
    for step in steps or ():
        if not isinstance(step, Mapping):
            continue
        if str(step.get("kind") or "") != "tool_use":
            continue
        name = str(step.get("name") or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def tool_names_from_trajectory_jsonl(
    path: str | os.PathLike[str],
    *,
    max_bytes: int = _TRAJECTORY_READ_MAX_BYTES,
) -> list[str]:
    """Names from one numbered ``{role}_trajectory.jsonl`` (tolerates a truncated tail)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if len(text.encode("utf-8", errors="replace")) > max_bytes:
        text = text[:max_bytes]
    steps: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            # A streamed writer can be cut mid-line; skip the bad record.
            continue
        if isinstance(parsed, dict):
            steps.append(parsed)
    return tool_names_from_steps(steps)


def tool_names_from_round_dir(
    round_dir: str | os.PathLike[str],
    *,
    max_bytes: int = _TRAJECTORY_READ_MAX_BYTES,
) -> list[str]:
    """All roles' normalised ``tool_use`` names for one round.

    Raw trajectories (``*_raw_trajectory.jsonl``) are adapter-native streams,
    not the normalised step vocabulary, so only ``{role}_trajectory.jsonl``
    files count. Public roles are read first so the order stays stable.
    """
    try:
        directory = Path(round_dir)
        entries = sorted(directory.glob("*_trajectory.jsonl"))
    except (OSError, ValueError):
        return []
    files = [path for path in entries if not path.name.endswith("_raw_trajectory.jsonl")]
    ordered = sorted(files, key=lambda path: _role_order_key(path.name))
    steps: list[dict[str, Any]] = []
    for path in ordered:
        raw = _read_bounded_jsonl(path, max_bytes)
        steps.extend(raw)
    return tool_names_from_steps(steps)


def first_blocker_line(auditor_text: str) -> str:
    """First real line of the auditor's blocking-constraints (``blockers``) list.

    Inline sections (``Blocking constraints: provider refused…``) and bulleted
    sections are both handled; ``none`` and its zh equivalents mean no
    blockers, and any subsequent report heading terminates the list.
    """
    lines = str(auditor_text or "").splitlines()
    for index, line in enumerate(lines):
        match = _BLOCKER_SECTION_RE.match(line)
        if not match:
            continue
        rest = _clean_item(match.group("rest"))
        if rest:
            return "" if _BLOCKER_NO_OP_RE.match(rest) else rest
        for following in lines[index + 1 :]:
            stripped = following.strip()
            if not stripped:
                continue
            if _BLOCKER_SECTION_RE.match(stripped):
                break
            if _BLOCKER_SECTION_END_RE.match(stripped) and not _BULLET_RE.match(stripped):
                break
            item = _clean_item(stripped)
            if item and not _BLOCKER_NO_OP_RE.match(item):
                return item
        return ""
    return ""


def error_signature(round_record: Any = None, *, abort_reason: str = "") -> str | None:
    """Error signature: first blocker line, else a ``provider_*`` abort reason.

    A round that produced neither blockers nor a provider abort has no
    signature (``None``), and the extracted line is redacted before it is
    labelled — the auditor may have quoted a credential it saw.
    """
    line = first_blocker_line(_round_text(round_record, "auditor_report"))
    if line:
        return redact_text(line)[:ERROR_SIGNATURE_MAX_CHARS]
    reason = str(abort_reason or "").strip()
    if reason.startswith(_PROVIDER_PREFIX):
        return reason[:ERROR_SIGNATURE_MAX_CHARS]
    return None


def roles_from_owner(owner: Mapping[str, Any] | None) -> dict[str, RoleTrace]:
    """Model trio (and extras) per role from the run owner document.

    Priority per role (task 49 handoff, "must not re-key"):

    1. ``owner.route.bound.roles[role]`` supplies ``model``, ``route_tier``
       (``tier``) and a redacted ``route_rationale``; the agent identity comes
       from ``owner.role_configs`` because a route carries no agent.
    2. ``owner.role_configs[role]`` supplies ``agent``/``model`` otherwise.
    3. A legacy owner with neither falls back to the top-level ``agent`` /
       ``model`` for each of the three public roles.
    """
    data = owner if isinstance(owner, Mapping) else {}
    route = data.get("route")
    bound_roles: Mapping[str, Any] = {}
    if isinstance(route, Mapping):
        bound = route.get("bound")
        if isinstance(bound, Mapping) and isinstance(bound.get("roles"), Mapping):
            bound_roles = bound["roles"]
    role_configs: Mapping[str, Any] = (
        data.get("role_configs") if isinstance(data.get("role_configs"), Mapping) else {}
    )
    legacy_agent = str(data.get("agent") or "").strip() or None
    legacy_model = data.get("model")

    ordered: list[str] = [role for role in PUBLIC_ROLE_NAMES if role in bound_roles or role in role_configs]
    ordered += [role for role in bound_roles if role not in ordered]
    ordered += [role for role in role_configs if role not in ordered]
    if not ordered and (legacy_agent or legacy_model):
        ordered = list(PUBLIC_ROLE_NAMES)

    result: dict[str, RoleTrace] = {}
    for role in ordered:
        bound_entry = bound_roles.get(role) if isinstance(bound_roles.get(role), Mapping) else {}
        config_entry = role_configs.get(role) if isinstance(role_configs.get(role), Mapping) else {}
        model = bound_entry.get("model") or config_entry.get("model") or legacy_model
        agent = config_entry.get("agent") or legacy_agent
        result[role] = RoleTrace(
            role=str(role),
            agent=redact_text(str(agent)) if agent is not None else None,
            model=redact_text(str(model)) if model is not None and str(model).strip() else None,
            route_tier=str(bound_entry.get("tier") or "").strip() or None,
            # Rationales carry free text the router produced; redact it the
            # moment it crosses into the experience layer (RoleTrace caps it).
            route_rationale=(
                redact_text(str(bound_entry.get("rationale")))
                if bound_entry.get("rationale") is not None
                else None
            ),
        )
    return result


def _clean_item(text: str) -> str:
    """Drop bullet/list decoration and the backticks auditors favour."""
    cleaned = _BULLET_RE.sub("", str(text).strip()).strip().strip("`*").strip()
    return cleaned


def _contained_child(root: Path, relpath: str) -> Path | None:
    text = str(relpath or "").strip()
    if not text or "\x00" in text:
        return None
    try:
        candidate = (root / text).resolve(strict=False)
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    if candidate.is_symlink() or not candidate.is_dir():
        return None
    return candidate


def _rev_parse_head(directory: Path) -> str:
    git = str(directory / ".git")
    if not (os.path.isdir(git) or os.path.isfile(git)):
        return ""
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={directory}",
                "-C",
                str(directory),
                "rev-parse",
                "--abbrev-ref",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=_GIT_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    branch = str(result.stdout or "").strip()
    if not branch or branch == "HEAD":
        return ""
    return branch[:128]


def _role_order_key(filename: str) -> tuple[int, str]:
    stem = filename.removesuffix("_trajectory.jsonl")
    for index, role in enumerate(PUBLIC_ROLE_NAMES):
        if stem == role:
            return (index, filename)
    return (len(PUBLIC_ROLE_NAMES), filename)


def _read_bounded_jsonl(path: Path, max_bytes: int) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    steps: list[dict[str, Any]] = []
    for line in text[:max_bytes].splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            steps.append(parsed)
    return steps


def _round_text(record: Any, field: str) -> str:
    if record is None:
        return ""
    if isinstance(record, Mapping):
        return str(record.get(field) or "")
    if hasattr(record, "__dataclass_fields__") and field in record.__dataclass_fields__:
        return str(getattr(record, field) or "")
    return ""
