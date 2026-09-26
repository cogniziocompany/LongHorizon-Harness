"""Single-role PR review runs (``run_kind="review"``).

A review run is deliberately NOT the four-role management loop. It has one
role — the reviewer, drawn from the ``auditor_agent`` prompt family — one
episode, and one machine-readable output: ``review.json`` next to the run's
``report.json``. The reviewer receives a PR's diff vs its base, the changed
files, the pr-gate results (task 185) fetched as JSON, and the PR body, then
produces a verdict of ``pass``, ``fail``, or ``cannot_review``.

Hard boundaries enforced here:

- The review workspace is a read-only checkout of the PR head, fetched by sha
  via ``refs/pull/N/head`` into ``<runs_root>/review/<repo>/<pr>/``. Review
  runs never touch the shared task workspaces and never write to any repo.
- Review runs never push, never comment on GitHub, and never use the
  subscription route. The only outbound GitHub traffic in this module is the
  read-only fetch of the PR head ref, the gate-results read, and the PR-body
  read.
- ``cannot_review`` is the only verdict for timeouts, provider errors, and
  unreadable diffs. ``pass`` is never a default for anything.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .types import EpisodeBudget, EpisodeResult

# [run.timeouts] reviewer budget. The config template carries the same value;
# callers cannot loosen it through the run body.
REVIEWER_TIMEOUT_SECONDS = 900
# Review runs are structurally one round; enforced regardless of caller input.
REVIEW_MAX_ROUNDS = 1

REVIEW_RUN_KIND = "review"
VERDICTS = ("pass", "fail", "cannot_review")

# The reviewer seat's model trio, in fallback order. The launcher's trio
# resolve picks the first seat; when it reads synthetic=DOWN the pool entry
# applies. ``[run.roles.reviewer]`` agent/model overrides replace the two
# synthetic entries.
REVIEWER_SEAT_TRIO: tuple[dict[str, str | None], ...] = (
    {"agent": "claude_code", "model": "kimi-k3:synthetic-anthropic"},
    {"agent": "claude_code", "model": "nemotron-3-super:synthetic-anthropic"},
    {"agent": "claude_code", "model": "ornith-1.5:pool"},
)
SYNTHETIC_DOWN_MODEL = "ornith-1.5:pool"
DEFAULT_REVIEWER_AGENT = "claude_code"

# Bounded reads so a hostile/large PR body or gate payload cannot become a
# memory problem inside the reviewer prompt.
_MAX_PR_BODY_CHARS = 20_000
_MAX_DIFF_CHARS = 200_000
_MAX_GATE_JSON_CHARS = 100_000
_MAX_CHANGED_FILES = 500
_MAX_EVIDENCE_CHARS = 2_000
_MAX_SPEC_JSON_BYTES = 65_536

_GATE_TIMEOUT_SECONDS = 30
_GIT_TIMEOUT_SECONDS = 300
_PR_BODY_TIMEOUT_SECONDS = 20


class ReviewSpecError(ValueError):
    """Raised when a review request fails validation."""


class ReviewWorkspaceError(RuntimeError):
    """The PR head could not be fetched or checked out."""


@dataclass(frozen=True)
class ReviewSpec:
    """Validated inputs for one review run.

    Built only through :func:`parse_review_spec`; all string fields are
    normalized and bounded there so downstream code can trust the shape.
    """

    repo: str
    pr_number: int
    head_sha: str
    base_ref: str
    gate_results_url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "pr_number": self.pr_number,
            "head_sha": self.head_sha,
            "base_ref": self.base_ref,
            "gate_results_url": self.gate_results_url,
        }


_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)?$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{6,64}$")


def parse_review_spec(value: object) -> ReviewSpec:
    """Validate the ``kind: "review"`` request body into a :class:`ReviewSpec`.

    Every field is required and tightly bounded: ``repo`` is an ``owner/name``
    (or plain) repo name used verbatim as a path component, ``pr_number`` a
    positive integer, ``head_sha`` a hex sha, ``base_ref`` a git ref name, and
    ``gate_results_url`` an http(s) URL. Unknown or caller-controlled knobs
    (``max_rounds``, ``roles``, ``timeout``) are rejected here so a review run
    can never be reshaped through its request body.
    """

    if not isinstance(value, dict):
        raise ReviewSpecError("review spec must be an object")
    unknown = set(value) - {"repo", "pr_number", "head_sha", "base_ref", "gate_results_url"}
    if unknown:
        raise ReviewSpecError(f"unknown review field: {sorted(unknown)[0]}")
    repo = str(value.get("repo") or "").strip()
    if not repo or not _REPO_RE.match(repo) or ".." in repo:
        raise ReviewSpecError("review.repo must be a non-empty repo name")
    pr_number = value.get("pr_number")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
        raise ReviewSpecError("review.pr_number must be an integer of at least 1")
    head_sha = str(value.get("head_sha") or "").strip().lower()
    if not _SHA_RE.match(head_sha):
        raise ReviewSpecError("review.head_sha must be a hex commit sha")
    base_ref = str(value.get("base_ref") or "").strip()
    if (
        not base_ref
        or any(part in base_ref for part in (" ", "\x00", "..", "\n", "~", "^", ":"))
        or base_ref.startswith("-")
    ):
        raise ReviewSpecError("review.base_ref must be a git ref name")
    gate_url = str(value.get("gate_results_url") or "").strip()
    if not gate_url.lower().startswith(("http://", "https://")) or "\x00" in gate_url or any(
        ord(char) < 0x20 for char in gate_url
    ):
        raise ReviewSpecError("review.gate_results_url must be an http(s) URL")
    return ReviewSpec(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        base_ref=base_ref,
        gate_results_url=gate_url,
    )


def review_workspace_path(runs_root: str | Path, spec: ReviewSpec) -> Path:
    """Return the review checkout path for one repo/PR.

    Always ``<runs_root>/review/<repo>/<pr>/`` — never a shared task
    workspace. ``repo`` and ``pr`` are pre-validated path components.
    """

    return Path(runs_root).expanduser().resolve() / "review" / spec.repo / str(spec.pr_number)


@dataclass
class ReviewCheckout:
    """A checked-out PR head plus the commit the diff is built against."""

    workspace: Path
    head_commit: str
    base_commit: str


def prepare_review_workspace(
    runs_root: str | Path,
    spec: ReviewSpec,
    *,
    remote_url: str,
) -> ReviewCheckout:
    """Check out the PR head sha read-only under the runs root.

    The head is fetched by sha via ``refs/pull/N/head``; the fetched commit is
    verified against the requested ``head_sha`` and the base ref is fetched so
    a two-dot diff is possible. Any failure raises
    :class:`ReviewWorkspaceError`, which the caller maps to ``cannot_review``.
    No push, no comment, and no write to the repo's own origin ever happens.
    """

    workspace = review_workspace_path(runs_root, spec)
    if workspace.exists():
        # A previous attempt may have left a partial checkout; rebuild it
        # rather than reviewing a stale tree.
        shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)
    env = _git_env()
    _run_git(["init", "-q", str(workspace)], cwd=workspace.parent, env=env)
    _run_git(["remote", "add", "origin", remote_url], cwd=workspace, env=env)
    fetched_head = _fetch_and_verify(
        ["fetch", "-q", "--depth=50", "origin", f"refs/pull/{spec.pr_number}/head"],
        workspace,
        env,
        spec.head_sha,
        f"refs/pull/{spec.pr_number}/head",
    )
    _run_git(["checkout", "-q", "--detach", fetched_head], cwd=workspace, env=env)
    try:
        _run_git(["fetch", "-q", "--depth=50", "origin", spec.base_ref], cwd=workspace, env=env)
        base_commit = _run_git(
            ["rev-parse", "FETCH_HEAD"], cwd=workspace, env=env
        ).stdout.strip().lower()
    except ReviewWorkspaceError:
        # The base ref may not exist on the remote anymore; the diff then falls
        # back to the requested base name if it is resolvable locally at all.
        try:
            base_commit = _run_git(
                ["rev-parse", "--verify", "--quiet", f"{spec.base_ref}^{{commit}}"],
                cwd=workspace,
                env=env,
            ).stdout.strip().lower()
        except ReviewWorkspaceError:
            base_commit = ""
    if base_commit:
        try:
            _run_git(
                ["merge-base", "--is-ancestor", base_commit, fetched_head],
                cwd=workspace,
                env=env,
            )
        except ReviewWorkspaceError:
            # Unrelated histories are still reviewable with a two-dot diff
            # against the requested base name.
            base_commit = ""
    return ReviewCheckout(workspace=workspace, head_commit=fetched_head, base_commit=base_commit)


def _fetch_and_verify(
    args: list[str],
    workspace: Path,
    env: dict[str, str],
    expected_sha: str,
    ref_description: str,
) -> str:
    """Fetch one ref and verify the result against the requested head sha."""

    _run_git(args, cwd=workspace, env=env)
    fetched = _run_git(
        ["rev-parse", "FETCH_HEAD"], cwd=workspace, env=env
    ).stdout.strip().lower()
    if fetched == expected_sha:
        return fetched
    # The head ref moved past the requested sha; try to fetch the sha
    # explicitly, which GitHub serves directly for reachable commits.
    try:
        _run_git(["fetch", "-q", "origin", expected_sha], cwd=workspace, env=env)
    except ReviewWorkspaceError as exc:
        raise ReviewWorkspaceError(
            f"requested head_sha {expected_sha} is not reachable via {ref_description} "
            f"(resolved {fetched[:12]}): {exc}"
        ) from None
    fetched = _run_git(["rev-parse", "FETCH_HEAD"], cwd=workspace, env=env).stdout.strip().lower()
    if fetched != expected_sha:
        raise ReviewWorkspaceError(
            f"could not check out head_sha {expected_sha} from {ref_description}; "
            f"FETCH_HEAD resolved to {fetched[:12]}"
        )
    return fetched


def _git_env() -> dict[str, str]:
    """Git env for the read-only fetch: no prompts, no credential helpers."""

    env = os.environ.copy()
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "SSH_AUTH_SOCK": "",
            # Isolate the fetch from the machine's own git config: review
            # checkouts must not inherit user insteadOf rewrites or credential
            # helpers (the user config rewrites git@github.com: forms, which
            # would silently change the fetch URL).
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
    )
    # Review runs hold GH_TOKEN for read access to private PR heads. The
    # header is injected through the environment so the token never lands in
    # argv, in the checkout's config, or in any durable artifact.
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        header = "Authorization: Basic " + base64.b64encode(
            f"x-access-token:{token}".encode("utf-8")
        ).decode("ascii")
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = header
    return env


def _run_git(
    args: list[str], *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise ReviewWorkspaceError(
            f"git {' '.join(args[:2])} failed: {(result.stderr or result.stdout).strip()[-400:]}"
        )
    return result


@dataclass
class ReviewInputs:
    """Everything the reviewer episode receives, bounded before prompt build."""

    workspace: str
    diff: str
    changed_files: list[str]
    gate_results: dict[str, Any]
    gate_results_error: str | None
    pr_body: str
    head_sha: str
    base_ref: str
    diff_error: str | None = None


def collect_review_inputs(
    spec: ReviewSpec,
    checkout: ReviewCheckout,
    *,
    pr_body: str = "",
    gate_results_url: str | None = None,
    diff_runner: Callable[[list[str]], str] | None = None,
) -> ReviewInputs:
    """Assemble the reviewer's inputs from the local checkout plus fetches.

    ``diff_runner`` is a test seam: it receives the git diff argv tail and
    returns stdout, letting tests exercise unreadable-diff handling without a
    real git history.

    The gate fetch is best-effort: a fetch failure is recorded as
    ``gate_results_error`` and told to the reviewer (an unreachable gate alone
    does not become a ``fail``). An unreadable diff, by contrast, is surfaced
    through ``diff_error`` so the verdict path can record ``cannot_review``.
    """

    env = _git_env()
    base_commit = checkout.base_commit
    head_commit = checkout.head_commit
    range_spec = (
        f"{base_commit}..{head_commit}" if base_commit else f"{spec.base_ref}...{head_commit}"
    )
    diff = ""
    name_only = ""
    diff_error: str | None = None
    try:
        if diff_runner is not None:
            diff = diff_runner(["diff", "--no-color", range_spec])
            name_only = diff_runner(["diff", "--name-only", range_spec])
        else:
            diff = _run_git(
                ["diff", "--no-color", range_spec], cwd=checkout.workspace, env=env
            ).stdout
            name_only = _run_git(
                ["diff", "--name-only", range_spec], cwd=checkout.workspace, env=env
            ).stdout
    except Exception as exc:  # noqa: BLE001 - surfaced as cannot_review evidence
        diff_error = str(exc)[:400]
    changed_files = [line.strip() for line in name_only.splitlines() if line.strip()][
        :_MAX_CHANGED_FILES
    ]
    gate_results, gate_results_error = _fetch_gate_results(
        gate_results_url or spec.gate_results_url
    )
    return ReviewInputs(
        workspace=str(checkout.workspace),
        diff=diff[:_MAX_DIFF_CHARS],
        changed_files=changed_files,
        gate_results=gate_results,
        gate_results_error=gate_results_error,
        pr_body=pr_body[:_MAX_PR_BODY_CHARS],
        head_sha=head_commit,
        base_ref=spec.base_ref,
        diff_error=diff_error,
    )


def _fetch_gate_results(url: str) -> tuple[dict[str, Any], str | None]:
    """Fetch the pr-gate results JSON (task 185) from its URL.

    The shape is deliberately tolerant: whatever the gate endpoint returns is
    passed through as JSON for the reviewer to read. A fetch or parse failure
    is reported back instead of being invented. (Residual risk: task 185's
    concrete schema is not merged yet, so this stays shape-agnostic.)
    """

    if not url:
        return {}, None
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=_GATE_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        return (data if isinstance(data, dict) else {"results": data}), None
    except Exception as exc:  # noqa: BLE001 - a fetch failure is reviewer-visible, never fatal
        return {}, f"gate results could not be fetched from {url}: {exc}"


def fetch_pr_body(repo: str, pr_number: int) -> str:
    """Read the PR body through the GitHub REST API (read-only GET).

    This is the only GitHub API call in the review flow besides the gate
    fetch, and it is a GET: review runs never comment, label, or push. A
    failure returns an empty body; the reviewer still gets the diff and the
    gate results.
    """

    if "/" not in repo:
        return ""
    host = (
        os.environ.get("LH_HARNESS_REVIEW_API_HOST") or "https://api.github.com"
    ).rstrip("/")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{host}/repos/{repo}/pulls/{pr_number}"
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=_PR_BODY_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        return str(data.get("body") or "") if isinstance(data, dict) else ""
    except Exception:  # noqa: BLE001 - the body is an optional input, never a hard requirement
        return ""


def build_reviewer_prompt(inputs: ReviewInputs) -> str:
    """Build the reviewer prompt: diff, changed files, gate JSON, PR body.

    The output contract is the review.json schema verbatim so the harness can
    parse the verdict block even when the model adds surrounding prose.
    """

    if inputs.gate_results_error:
        gate_text = f"(pr-gate results unavailable: {inputs.gate_results_error})"
    elif inputs.gate_results:
        gate_text = json.dumps(inputs.gate_results, ensure_ascii=False, indent=2)[
            :_MAX_GATE_JSON_CHARS
        ]
    else:
        gate_text = "(no pr-gate results supplied)"
    changed = "\n".join(f"- {item}" for item in inputs.changed_files) or "(no changed files)"
    if inputs.diff_error:
        diff_header = f"DIFF UNREADABLE: {inputs.diff_error}"
    else:
        diff_header = "Unified diff vs base"
    return f"""\
You are the read-only LongHorizon-Harness PR reviewer (auditor_agent prompt family).

You review one pull request and produce a machine-readable verdict. You have
read-only access to a detached checkout of the PR head at {inputs.workspace}.
You must NOT push, comment on GitHub, open or merge PRs, or write to the
repository. Your tools are for reading and for local read-only checks (tests,
static inspection) inside that checkout only.

Inputs:
- head sha: {inputs.head_sha}
- base ref: {inputs.base_ref}

PR body:
{inputs.pr_body.strip() or "(empty PR body)"}

Changed files:
{changed}

pr-gate results (JSON):
{gate_text}

{diff_header}:
```diff
{inputs.diff.strip() or "(empty diff)"}
```

Review the change like a code reviewer: correctness, regressions, test
coverage, and whether the pr-gate results corroborate the diff. You may run
read-only commands (git log/show/diff, pytest with no writes outside /tmp)
inside the checkout.

End your reply with EXACTLY one fenced block in this JSON schema and nothing
else inside it:

```review
{{"verdict": "pass" | "fail" | "cannot_review",
 "blocking": [{{"file": "...", "line": 1, "why": "..."}}],
 "findings": [{{"file": "...", "line": 1, "severity": "low|medium|high", "note": "..."}}],
 "evidence": ["..."]}}
```

Verdict rules:
- "pass": the change is safe to merge.
- "fail": at least one blocking problem exists; list it in "blocking" and name
  any failing test from the gate results in its "why" field.
- "cannot_review": only for timeouts, provider errors, unreadable diffs, or
  gate data so broken the change cannot be judged. Never use it to dodge real
  review work. Never default to "pass" when evidence is missing — use "fail"
  with a blocking entry or "cannot_review" with evidence explaining why.
"""


_REVIEW_BLOCK_RE = re.compile(r"```review\s*\n(.*?)```", re.S)


def _json_object_candidates(text: str) -> list[Any]:
    """Return every balanced JSON object in ``text``, decoded with offsets.

    A non-greedy ``\\{.*?\\}`` regex truncates nested objects, so a verdict
    like ``{"verdict": "fail", "blocking": [{"file": "a"}]}`` would never
    parse. ``raw_decode`` handles the nesting instead.
    """

    decoder = json.JSONDecoder()
    candidates: list[Any] = []
    index = text.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index = text.find("{", index + 1)
            continue
        candidates.append(value)
        index = text.find("{", max(index + 1, end))
    return candidates


def parse_review_output(raw: str, *, served_model: str | None = None) -> dict[str, Any]:
    """Parse the reviewer's fenced JSON block into the review.json shape.

    Malformed or missing verdict output degrades to ``cannot_review`` — never
    ``pass``. This is the harness-level guarantee that a reviewer which died,
    timed out, or emitted garbage cannot certify a change.
    """

    text = str(raw or "")
    verdict: dict[str, Any] | None = None
    match = _REVIEW_BLOCK_RE.search(text)
    if match:
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, dict):
                verdict = parsed
        except json.JSONDecodeError:
            verdict = None
    if verdict is None:
        # Fall back to any balanced JSON object carrying a real verdict so a
        # model that forgot the fence but emitted clean JSON still parses.
        for candidate in _json_object_candidates(text):
            if isinstance(candidate, dict) and candidate.get("verdict") in VERDICTS:
                verdict = candidate
                break
    if verdict is None:
        return _cannot_review(
            "reviewer output contained no parseable review verdict block",
            served_model=served_model,
        )
    result = _normalise_review_dict(verdict, served_model=served_model)
    if result["verdict"] not in VERDICTS:
        return _cannot_review(
            f"reviewer emitted unknown verdict {result['verdict']!r}",
            evidence=result.get("evidence", []),
            served_model=served_model,
        )
    if result["verdict"] == "pass" and result["blocking"]:
        # A pass with blocking entries is internally inconsistent; the
        # blocking evidence wins and the verdict is downgraded.
        result["verdict"] = "fail"
        result["evidence"] = [
            *result.get("evidence", []),
            "verdict downgraded from pass to fail because blocking entries were present",
        ]
    if result["verdict"] == "fail" and not result["blocking"]:
        result["verdict"] = "cannot_review"
        result["evidence"] = [
            *result.get("evidence", []),
            "verdict downgraded from fail to cannot_review because no blocking entry named a problem",
        ]
    return result


def _normalise_review_dict(value: dict[str, Any], *, served_model: str | None) -> dict[str, Any]:
    blocking = [
        {
            "file": str(item.get("file") or ""),
            "line": _coerce_int(item.get("line")),
            "why": str(item.get("why") or "")[:_MAX_EVIDENCE_CHARS],
        }
        for item in value.get("blocking", [])
        if isinstance(item, dict)
    ]
    findings = [
        {
            "file": str(item.get("file") or ""),
            "line": _coerce_int(item.get("line")),
            "severity": str(item.get("severity") or "low"),
            "note": str(item.get("note") or "")[:_MAX_EVIDENCE_CHARS],
        }
        for item in value.get("findings", [])
        if isinstance(item, dict)
    ]
    evidence = [
        str(item)[:_MAX_EVIDENCE_CHARS]
        for item in value.get("evidence", [])
        if str(item).strip()
    ]
    return {
        "verdict": str(value.get("verdict") or ""),
        "blocking": blocking,
        "findings": findings,
        "evidence": evidence,
        "model": str(served_model or value.get("model") or ""),
        "duration_s": _coerce_number(value.get("duration_s")),
    }


def _coerce_int(value: Any) -> int | None:
    number = _coerce_number(value)
    return int(number) if number is not None else None


def _coerce_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None


def _cannot_review(
    reason: str, *, evidence: list[str] | None = None, served_model: str | None = None
) -> dict[str, Any]:
    return {
        "verdict": "cannot_review",
        "blocking": [],
        "findings": [],
        "evidence": [reason[:_MAX_EVIDENCE_CHARS], *(evidence or [])],
        "model": str(served_model or ""),
        "duration_s": None,
    }


def _extract_review_output(result: EpisodeResult, override: str | None) -> str:
    """Prefer adapter-provided assistant text over the diagnostic trajectory."""

    if override is not None:
        return override
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    from .auditor_agent import VISIBLE_OUTPUT_KEYS

    for key in VISIBLE_OUTPUT_KEYS:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if metadata.get("actions_log_diagnostics_only"):
        return ""
    raw = result.actions_log or ""
    from .agent_logs import visible_output as decode_agent_visible_output

    decoded = decode_agent_visible_output(raw)
    return decoded if decoded else raw


def review_from_episode_result(
    result: EpisodeResult,
    *,
    served_model: str | None = None,
    started_at: float | None = None,
    output_override: str | None = None,
) -> dict[str, Any]:
    """Map one reviewer episode onto the review.json verdict.

    ``timeout``, ``error``, and ``cancelled`` episode statuses are the ONLY
    harness-level sources of ``cannot_review``. A ``done`` episode still goes
    through :func:`parse_review_output`, which degrades unparsable output to
    ``cannot_review``. Nothing on this path can produce ``pass`` by default.
    """

    duration_s = (
        round(result.duration_ms / 1000, 1)
        if result.duration_ms
        else (
            round(max(0.0, time.time() - started_at), 1)
            if started_at is not None
            else None
        )
    )
    if result.status != "done":
        reason = {
            "timeout": f"reviewer episode timed out after {result.duration_ms / 1000:.1f}s",
            "error": f"reviewer episode failed: {(result.error or 'provider error')[:400]}",
            "cancelled": "reviewer episode was cancelled/killed before producing a verdict",
        }.get(result.status, f"reviewer episode ended with status {result.status}")
        report = _cannot_review(reason, served_model=served_model)
        # The episode's own wall clock survives a killed run; only the verdict
        # is lost, and only ever to cannot_review.
        report["duration_s"] = duration_s
        return report
    review = parse_review_output(
        _extract_review_output(result, output_override), served_model=served_model
    )
    if review.get("duration_s") is None:
        review["duration_s"] = duration_s
    if not review.get("model"):
        review["model"] = str(served_model or "")
    return review


@dataclass
class ReviewSeat:
    """One resolved reviewer backend binding."""

    agent: str
    model: str | None
    mcp_profile: str | None = None


def resolve_reviewer_candidates(
    config: dict[str, Any] | None = None,
    *,
    synthetic_up: bool | None = None,
) -> list[ReviewSeat]:
    """Resolve the reviewer seat trio into an ordered candidate list.

    The launcher's trio resolve picks the reviewer seat:
    ``kimi-k3:synthetic-anthropic`` first, ``nemotron-3-super:synthetic-anthropic``
    as the fallback, and ``ornith-1.5:pool`` when the launcher reads
    ``synthetic=DOWN``. ``[run.roles.reviewer]`` overrides via flattened run
    defaults: ``reviewer_agent``, ``reviewer_model``, ``reviewer_mcp_profile``.
    """

    seat_config = config if isinstance(config, dict) else {}
    agent = str(seat_config.get("reviewer_agent") or DEFAULT_REVIEWER_AGENT)
    raw_profile = seat_config.get("reviewer_mcp_profile")
    profile = str(raw_profile).strip() if isinstance(raw_profile, str) and raw_profile.strip() else None
    models: list[str] = []
    raw_models = seat_config.get("reviewer_model")
    if isinstance(raw_models, list):
        models = [str(item).strip() for item in raw_models if str(item).strip()]
    elif isinstance(raw_models, str) and raw_models.strip():
        models = [raw_models.strip()]
    if not models:
        models = [
            str(entry["model"])
            for entry in REVIEWER_SEAT_TRIO[:2]
            if entry.get("model")
        ]
    pool_model = str(seat_config.get("reviewer_pool_model") or SYNTHETIC_DOWN_MODEL)
    if synthetic_up is False:
        return [ReviewSeat(agent=agent, model=pool_model, mcp_profile=profile)]
    return [ReviewSeat(agent=agent, model=model, mcp_profile=profile) for model in models]


def probe_synthetic_status(url: str | None, *, timeout: float = 5.0) -> bool | None:
    """Read the synthetic provider status the launcher consults.

    Returns ``True`` (UP), ``False`` (DOWN), or ``None`` when no URL is
    configured or the probe fails. Only an explicit DOWN reading moves the
    seat to the pool model; an unreadable probe keeps the normal trio.
    """

    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")[:4096]
    except Exception:
        return None
    status: str | None = None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for key in ("synthetic", "status", "state"):
                value = data.get(key)
                if isinstance(value, str):
                    status = value.strip().lower()
                    break
    except json.JSONDecodeError:
        status = text.strip().lower()[:32]
    if status in {"down", "down.", "failed", "outage"}:
        return False
    if status in {"up", "up.", "healthy", "ok"}:
        return True
    return None


def resolve_reviewer_timeout(config: dict[str, Any] | None = None) -> int:
    """Return the ``[run.timeouts] reviewer`` budget, bounded to the default.

    A caller cannot push the budget above :data:`REVIEWER_TIMEOUT_SECONDS`
    through a request body; config may only shorten it.
    """

    raw = (config or {}).get("reviewer_timeout")
    value = _coerce_int(raw)
    if value is None or value < 1:
        return REVIEWER_TIMEOUT_SECONDS
    return min(value, REVIEWER_TIMEOUT_SECONDS)


def repo_remote_url(repo: str, *, host: str | None = None) -> str:
    """Return the read-only fetch URL for one repo name.

    Local filesystem paths are deliberately rejected: a review checkout must
    come from a real remote via ``refs/pull/N/head``, never from a shared
    local tree.
    """

    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", repo) or re.match(r"^[\w.+-]+@[\w.-]+:", repo):
        if repo.lower().startswith("file:"):
            raise ReviewSpecError("review.repo must be a remote repository, not a local path")
        return repo
    base = (host or os.environ.get("LH_HARNESS_REVIEW_GIT_HOST") or "https://github.com").rstrip("/")
    # A URL-shaped host (file://, ssh://, https://host/path) is joined as-is;
    # a bare host is treated as a forge base and the repo is appended.
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", base):
        return f"{base}/{repo}.git"
    return f"{base}/{repo}.git"


def review_report(review: dict[str, Any], *, served_model: str) -> dict[str, Any]:
    """Build the review.json payload: the exact contract schema, nothing more."""

    return {
        "verdict": str(review.get("verdict") or "cannot_review"),
        "blocking": list(review.get("blocking") or []),
        "findings": list(review.get("findings") or []),
        "evidence": list(review.get("evidence") or []),
        "model": str(served_model or review.get("model") or ""),
        "duration_s": review.get("duration_s"),
    }


def read_review_report(runs_root: str | Path, run_id: str) -> dict[str, Any] | None:
    """Read one run's durable ``lh_harness/review.json``.

    Callers use this for ``GET /api/runs/<id>/review`` and for heartbeat
    verdict counts; a missing or corrupt file reads as ``None`` and the caller
    decides whether that is a 404 or an absent verdict.
    """

    path = Path(runs_root).expanduser() / run_id / "lh_harness" / "review.json"
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with open(tmp_path, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def _default_agent_factory(seat: ReviewSeat, *, workspace: str, prompt_dir: str):
    """Build the reviewer agent through the same factory the CLI uses."""

    from .cli import _build_agent

    return _build_agent(
        seat.agent,
        role="auditor",  # auditor prompt family + read-only permission policy
        model=seat.model,
        api_key=os.environ.get("LH_HARNESS_REVIEW_API_KEY"),
        base_url=os.environ.get("LH_HARNESS_REVIEW_BASE_URL"),
        workspace_path=workspace,
        prompt_dir=prompt_dir,
        mcp_profile=seat.mcp_profile,
    )


async def run_review_session(
    *,
    spec: ReviewSpec,
    run_dir: str | Path,
    runs_root: str | Path,
    agent_factory: Any = None,
    config: dict[str, Any] | None = None,
    episode_runner: Any = None,
) -> dict[str, Any]:
    """Run one review session and leave the durable review/report artifacts.

    ``agent_factory`` and ``episode_runner`` are test seams: tests can supply
    a scripted reviewer without a real provider. Every failure path —
    workspace, provider, timeout, cancellation — is persisted as a
    ``cannot_review`` review.json plus a completed report.json so a killed
    review run is still a durable, machine-readable outcome.
    """

    run_dir = Path(run_dir).expanduser().resolve()
    log_dir = run_dir / "lh_harness"
    started = time.time()
    timeout_s = resolve_reviewer_timeout(config)
    candidates = resolve_reviewer_candidates(config)

    async def _execute(seat: ReviewSeat, prompt: str) -> EpisodeResult:
        if episode_runner is not None:
            return await episode_runner(prompt, timeout_s)
        agent = (agent_factory or _default_agent_factory)(
            seat,
            workspace=str(review_workspace_path(runs_root, spec)),
            prompt_dir=str(run_dir / "tmp" / "prompts"),
        )
        from .environment.local import LocalEnvironment

        env = LocalEnvironment(tmp_dir=str(run_dir / "tmp"))
        return await agent.run_episode(
            prompt,
            env,
            EpisodeBudget(max_duration_seconds=timeout_s),
        )

    review = await _run_review(spec, runs_root, candidates, _execute, timeout_s, started)
    report = review_report(review, served_model=str(review.get("model") or ""))
    _persist_review_artifacts(
        run_dir=run_dir,
        log_dir=log_dir,
        spec=spec,
        report=report,
        timeout_s=timeout_s,
    )
    return report


async def _run_review(
    spec: ReviewSpec,
    runs_root: str | Path,
    candidates: list[ReviewSeat],
    execute: Any,
    timeout_s: int,
    started: float,
) -> dict[str, Any]:
    """Prepare the workspace, collect inputs, and run the reviewer episode.

    Candidate seats are tried in trio order: a provider error moves to the
    next seat; a timeout, cancellation, or workspace failure does not. The
    last attempted seat's model is recorded as the served model either way.
    """

    last_seat = candidates[0] if candidates else ReviewSeat(
        agent=DEFAULT_REVIEWER_AGENT, model=SYNTHETIC_DOWN_MODEL
    )
    try:
        checkout = prepare_review_workspace(
            runs_root, spec, remote_url=repo_remote_url(spec.repo)
        )
    except Exception as exc:  # noqa: BLE001 - workspace failure is cannot_review evidence
        return _cannot_review(
            f"review workspace could not be prepared: {str(exc)[:400]}",
            served_model=str(last_seat.model or ""),
        )
    try:
        pr_body = fetch_pr_body(spec.repo, spec.pr_number)
    except Exception:  # noqa: BLE001 - defensive; fetch_pr_body already swallows
        pr_body = ""
    inputs = collect_review_inputs(
        spec, checkout, pr_body=pr_body, gate_results_url=spec.gate_results_url
    )
    prompt = build_reviewer_prompt(inputs)
    result: EpisodeResult | None = None
    for index, seat in enumerate(candidates):
        last_seat = seat
        result = await execute(seat, prompt)
        if result.status != "error" or index == len(candidates) - 1:
            break
    assert result is not None
    served_model = str(last_seat.model or "")
    review = review_from_episode_result(
        result, served_model=served_model, started_at=started
    )
    if inputs.diff_error and review.get("verdict") == "pass":
        # An unreadable diff can never certify a change: the harness refuses a
        # pass produced on top of a failed diff read.
        review["verdict"] = "cannot_review"
        review["evidence"] = [
            *review.get("evidence", []),
            f"diff could not be read: {inputs.diff_error}",
        ]
    review["model"] = served_model
    review.setdefault("duration_s", round(time.time() - started, 1))
    return review


def _persist_review_artifacts(
    *,
    run_dir: Path,
    log_dir: Path,
    spec: ReviewSpec,
    report: dict[str, Any],
    timeout_s: int,
) -> None:
    """Write review.json, report.json, and the review role event durably."""

    _atomic_write_json(log_dir / "review.json", report)
    final_report = {
        "schema_version": 2,
        "run_kind": REVIEW_RUN_KIND,
        "status": "completed",
        "task": f"Review {spec.repo}#{spec.pr_number} ({spec.base_ref} -> {spec.head_sha})",
        "completion_satisfied": True,
        "completion_authority": "single_role_reviewer",
        "review_verdict": report.get("verdict"),
        "reviewer_timeout_s": timeout_s,
        "rounds_run": 1,
        "max_rounds": REVIEW_MAX_ROUNDS,
        "abort_reason": None,
        "elapsed_seconds": report.get("duration_s"),
    }
    _atomic_write_json(log_dir / "report.json", final_report)
    event = {
        "schema_version": 1,
        "event": "review_done",
        "status": "completed",
        "run_kind": REVIEW_RUN_KIND,
        "ts": time.time(),
        "verdict": report.get("verdict"),
        "model": report.get("model"),
        "duration_s": report.get("duration_s"),
    }
    _append_event(log_dir / "role_orchestration" / "events.jsonl", event)


def _append_event(path: Path, record: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    """Worker entry point: ``python -m lh_harness.review --spec <file> ...``.

    The supervisor writes the validated review spec to a run-local file and
    launches this worker; the worker executes the single reviewer episode and
    leaves review.json + report.json behind. Invalid specs still leave a
    ``cannot_review`` artifact so no review run can end without a verdict.
    """

    parser = argparse.ArgumentParser(
        prog="lh-harness-review",
        description="Execute one single-role PR review run (kind=\"review\").",
    )
    parser.add_argument("--spec", required=True, help="Path to the review spec JSON file")
    parser.add_argument("--run-id", required=True, help="Run id of the reserved run directory")
    parser.add_argument("--runs-root", required=True, help="Root that holds the run directory")
    parser.add_argument(
        "--reviewer-timeout",
        type=int,
        default=None,
        help="Per-episode reviewer budget override (bounded to the configured default)",
    )
    args = parser.parse_args(argv)

    from .config import load_run_defaults

    runs_root = Path(args.runs_root).expanduser().resolve()
    run_dir = runs_root / args.run_id
    log_dir = run_dir / "lh_harness"
    try:
        spec_payload = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        spec = parse_review_spec(spec_payload)
    except Exception as exc:  # noqa: BLE001 - an invalid spec must still end in cannot_review
        report = _cannot_review(f"review spec was invalid: {exc}", served_model="")
        try:
            _atomic_write_json(log_dir / "review.json", review_report(report, served_model=""))
        except OSError:
            pass
        print(f"Cannot run review: {exc}", file=sys.stderr)
        return 2

    config: dict[str, Any] = {}
    try:
        from .config import PROJECT_CONFIG_PATH

        config = load_run_defaults(PROJECT_CONFIG_PATH)
    except Exception:
        config = {}
    if args.reviewer_timeout is not None and args.reviewer_timeout > 0:
        config["reviewer_timeout"] = min(int(args.reviewer_timeout), REVIEWER_TIMEOUT_SECONDS)

    try:
        report = asyncio_main(
            spec=spec,
            run_dir=run_dir,
            runs_root=runs_root,
            config=config,
        )
    except BaseException as exc:  # noqa: BLE001 - worker boundary: persist even crashes
        report = _cannot_review(f"review worker crashed: {exc}", served_model="")
        try:
            _atomic_write_json(log_dir / "review.json", review_report(report, served_model=""))
        except OSError:
            pass
        return 1
    return 0


def asyncio_main(
    *,
    spec: ReviewSpec,
    run_dir: str | Path,
    runs_root: str | Path,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Synchronous bridge into :func:`run_review_session` for the worker CLI."""

    import asyncio

    return asyncio.run(
        run_review_session(spec=spec, run_dir=run_dir, runs_root=runs_root, config=config)
    )


if __name__ == "__main__":
    raise SystemExit(main())