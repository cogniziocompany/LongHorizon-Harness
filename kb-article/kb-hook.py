#!/usr/bin/env python3
"""
KB Article Hook for Claude Code — auto-submits knowledge articles on notable events.
====================================================================================

EVENTS CAPTURED
  Bash PostToolUse:
    git commit   → procedure  / @git-commit
    git branch   → procedure  / @git-branch
    git push     → procedure  / @git-push
    npm build    → procedure  / @build
    npm test     → debugging  / @test-run
    deploy       → procedure  / @deployment @uat
    docker build → procedure  / @docker-build
    e2e / playwright → debugging / @e2e-test
  Edit / Write PostToolUse:
    Significant file saves (*.ts, *.py, Dockerfile, *.yml, *.sql, .claude/*.json)
  Stop:
    Session-end summary (session_id + repo + branch)

WEBHOOK ENDPOINT (central receiver)
  Default: http://192.168.21.161:3114/api/kb/articles
           (kb-mcp-uat REST server on CT202, port 3114 — same process as the MCP server)
  Override: set KB_WEBHOOK_URL env var
           OR create .claude/kb-hook.env  →  KB_WEBHOOK_URL=http://...

SETUP IN ANY REPO (handoff instructions)
-----------------------------------------
1. Copy this file to:  <repo-root>/kb-article/kb-hook.py

2. Create (or merge into) .claude/settings.json:

   {
     "hooks": {
       "PostToolUse": [
         {
           "matcher": "Bash",
           "hooks": [{"type": "command", "command": "python3 kb-article/kb-hook.py"}]
         },
         {
           "matcher": "Edit",
           "hooks": [{"type": "command", "command": "python3 kb-article/kb-hook.py"}]
         },
         {
           "matcher": "Write",
           "hooks": [{"type": "command", "command": "python3 kb-article/kb-hook.py"}]
         }
       ],
       "Stop": [
         {"hooks": [{"type": "command", "command": "python3 kb-article/kb-hook.py"}]}
       ]
     }
   }

3. (Optional) Override the webhook URL in .claude/kb-hook.env:
   KB_WEBHOOK_URL=http://192.168.21.161:3114/api/kb/articles

4. The hook auto-detects:  repo name (git remote), current branch, git sha.
   Articles are tagged with @<repo-slug> automatically.

NOTES
  - Errors are logged to kb-article/kb-hook.log — never propagated to Claude
  - Fires are idempotent: re-running the same commit twice produces duplicate articles
    (acceptable — billingservice deduplications can be added later via git_sha index)
  - The webhook endpoint lives at http://192.168.21.161:3114/api/kb/articles
    (kb-mcp-uat REST server on CT202). That service connects directly to billingservice
    Postgres on CT100 (192.168.21.153:5432). CT202 must be reachable for articles to persist.
  - Fallback: if the HTTP webhook fails, the article is skipped silently and logged.
    To use SSH+psql fallback instead, set KB_USE_SSH_FALLBACK=1 in kb-hook.env.

SSH FALLBACK (if app is unavailable)
  Requires SSH access to Proxmox host (cognizioware-ptait01):
    ssh root@cognizioware-ptait01 pct exec 100 -- docker exec postgres-server psql ...
  Set KB_USE_SSH_FALLBACK=1 in .claude/kb-hook.env to enable.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
LOG_FILE = SCRIPT_DIR / "kb-hook.log"
ENV_FILE = PROJECT_ROOT / ".claude" / "kb-hook.env"

DEFAULT_WEBHOOK = "http://192.168.21.161:3114/api/kb/articles"
PROXMOX_HOST = "root@cognizioware-ptait01"


def _load_env_file() -> dict[str, str]:
    result: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip()
    return result


_extra_env = _load_env_file()

WEBHOOK_URL = _extra_env.get("KB_WEBHOOK_URL") or os.environ.get("KB_WEBHOOK_URL") or DEFAULT_WEBHOOK
USE_SSH_FALLBACK = _extra_env.get("KB_USE_SSH_FALLBACK", "0") == "1"
AUTHOR = _extra_env.get("KB_AUTHOR") or os.environ.get("KB_AUTHOR") or "claude-code-hook"


# ── Logging ──────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


# ── Git helpers ───────────────────────────────────────────────────────────────

def _git(*args: str) -> str:
    try:
        r = subprocess.run(
            ["git", *args], capture_output=True, text=True,
            cwd=PROJECT_ROOT, timeout=5
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _repo_name() -> str:
    remote = _git("remote", "get-url", "origin")
    if remote:
        name = re.sub(r"\.git$", "", remote.split("/")[-1].split(":")[-1])
        return name or PROJECT_ROOT.name
    return PROJECT_ROOT.name


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", s.lower()).strip("-")


def _base_tags(extra: list[str]) -> list[str]:
    repo = _slug(_repo_name())
    tags = ["@knowledge-article", f"@{repo}"] + extra
    return list(dict.fromkeys(tags))  # deduplicate, preserve order


# ── Submission ────────────────────────────────────────────────────────────────

def submit(
    title: str,
    category: str,
    tags: list[str],
    content: str,
    source_file: str | None = None,
    git_sha: str | None = None,
    metadata: dict | None = None,
) -> str | None:
    """POST to the KB webhook endpoint. Returns article_id or None."""
    payload = {
        "title": title[:255],
        "category": category,
        "tags": tags,
        "content": content,
        "source_file": source_file,
        "author": AUTHOR,
        "git_sha": git_sha,
        "metadata": metadata or {},
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(WEBHOOK_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            article_id = body.get("article_id")
            log(f"SUBMITTED id={article_id} title={title!r}")
            return str(article_id)
    except urllib.error.HTTPError as e:
        log(f"HTTP {e.code} submitting {title!r}: {e.read().decode()[:200]}")
    except Exception as exc:
        log(f"ERROR submitting {title!r}: {exc}")

    if USE_SSH_FALLBACK:
        return _ssh_fallback(title, category, tags, content, source_file, git_sha, metadata)
    return None


def _ssh_fallback(
    title: str, category: str, tags: list[str], content: str,
    source_file: str | None, git_sha: str | None, metadata: dict | None,
) -> str | None:
    """Fallback: submit via SSH + docker exec psql on CT100."""
    def q(s: str) -> str:
        return s.replace("'", "''")

    tags_literal = "{" + ",".join(f'"{t}"' for t in tags) + "}"
    meta_json = json.dumps(metadata or {}).replace("'", "''")
    sql = (
        f"SELECT create_knowledge_article("
        f"'{q(title)}','{category}',ARRAY{tags_literal}::TEXT[],"
        f"'{q(content)}',"
        f"{'NULL' if not source_file else repr(source_file)},"
        f"'{AUTHOR}','published','{meta_json}'::JSONB,"
        f"{'NULL' if not git_sha else repr(git_sha)});"
    )
    cmd = [
        "ssh", PROXMOX_HOST,
        f"pct exec 100 -- docker exec postgres-server psql -U ai_easybutt0n "
        f"-d billingservice --no-align --tuples-only -c \"{sql}\""
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            article_id = r.stdout.strip()
            log(f"SSH-FALLBACK id={article_id} title={title!r}")
            return article_id
        log(f"SSH-FALLBACK FAILED: {r.stderr[:200]}")
    except Exception as exc:
        log(f"SSH-FALLBACK ERROR: {exc}")
    return None


# ── Event extractors: Bash ────────────────────────────────────────────────────

def _handle_git_commit(cmd: str, output: str, session_id: str) -> None:
    sha = _git("rev-parse", "--short", "HEAD") or ""
    branch = _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"

    # Extract commit message: from -m "..." or from output "[branch sha] message"
    msg_match = re.search(r'-m\s+["\'](.+?)["\']', cmd, re.DOTALL)
    if msg_match:
        msg = msg_match.group(1)
    else:
        out_match = re.search(r'\[[\w/.-]+\s+\w+\]\s+(.+)', output)
        msg = out_match.group(1) if out_match else "commit"

    # Stats from output: "3 files changed, 10 insertions(+), 2 deletions(-)"
    stats_match = re.search(r'(\d+ files? changed.*)', output)
    stats = stats_match.group(1) if stats_match else ""

    title = f"[git-commit] {msg[:120]}"
    content = (
        f"## Git Commit — {branch}\n\n"
        f"**Message:** {msg}\n\n"
        f"**SHA:** {sha}  **Branch:** {branch}\n\n"
        f"**Stats:** {stats}\n\n"
        f"**Command:** `{cmd[:300]}`\n\n"
        f"**Output:**\n```\n{output[:800]}\n```"
    )
    submit(
        title=title,
        category="procedure",
        tags=_base_tags(["@git-commit", f"@branch-{_slug(branch)}"]),
        content=content,
        source_file=f"git:{branch}:{sha}",
        git_sha=sha,
        metadata={"session_id": session_id, "branch": branch, "stats": stats},
    )


def _handle_git_branch(cmd: str, output: str, session_id: str) -> None:
    branch_match = re.search(r'(?:-b|-c)\s+([\w./-]+)', cmd)
    branch = branch_match.group(1) if branch_match else "unknown"
    base_branch = _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"

    title = f"[git-branch] New branch: {branch}"
    content = (
        f"## New Branch Created\n\n"
        f"**Branch:** `{branch}`  **From:** `{base_branch}`\n\n"
        f"**Command:** `{cmd[:300]}`"
    )
    submit(
        title=title,
        category="procedure",
        tags=_base_tags(["@git-branch", f"@branch-{_slug(branch)}"]),
        content=content,
        metadata={"session_id": session_id, "branch": branch, "from_branch": base_branch},
    )


def _handle_git_push(cmd: str, output: str, session_id: str) -> None:
    branch = _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    sha = _git("rev-parse", "--short", "HEAD") or ""

    title = f"[git-push] Pushed {branch}"
    content = (
        f"## Git Push\n\n"
        f"**Branch:** `{branch}`  **SHA:** {sha}\n\n"
        f"**Command:** `{cmd[:300]}`\n\n"
        f"**Output:**\n```\n{output[:600]}\n```"
    )
    submit(
        title=title,
        category="procedure",
        tags=_base_tags(["@git-push", f"@branch-{_slug(branch)}"]),
        content=content,
        git_sha=sha,
        metadata={"session_id": session_id, "branch": branch},
    )


def _handle_build(cmd: str, output: str, session_id: str) -> None:
    success = "error" not in output.lower() and "failed" not in output.lower()
    status = "SUCCESS" if success else "FAILED"
    branch = _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    sha = _git("rev-parse", "--short", "HEAD") or ""

    # Vite build stats
    size_match = re.search(r'([\d.]+ kB.*gzip.*)', output)
    build_stats = size_match.group(1) if size_match else ""

    title = f"[build] {status} — {_repo_name()}"
    content = (
        f"## Build {status}\n\n"
        f"**Repo:** {_repo_name()}  **Branch:** {branch}  **SHA:** {sha}\n\n"
        f"**Command:** `{cmd[:300]}`\n\n"
        + (f"**Bundle:** {build_stats}\n\n" if build_stats else "")
        + f"**Output (tail):**\n```\n{output[-600:]}\n```"
    )
    submit(
        title=title,
        category="procedure",
        tags=_base_tags(["@build", f"@build-{status.lower()}"]),
        content=content,
        git_sha=sha,
        metadata={"session_id": session_id, "success": success, "branch": branch},
    )


def _handle_test(cmd: str, output: str, session_id: str) -> None:
    # Extract pass/fail counts
    pass_match = re.search(r'(\d+)\s+pass(?:ing|ed)', output, re.IGNORECASE)
    fail_match = re.search(r'(\d+)\s+fail(?:ing|ed)', output, re.IGNORECASE)
    passed = int(pass_match.group(1)) if pass_match else 0
    failed = int(fail_match.group(1)) if fail_match else 0
    status = "PASS" if failed == 0 and passed > 0 else ("FAIL" if failed > 0 else "RUN")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"

    title = f"[test] {status} — {passed}p/{failed}f — {_repo_name()}"
    content = (
        f"## Test Run — {status}\n\n"
        f"**Passed:** {passed}  **Failed:** {failed}  **Branch:** {branch}\n\n"
        f"**Command:** `{cmd[:300]}`\n\n"
        f"**Output (tail):**\n```\n{output[-800:]}\n```"
    )
    submit(
        title=title,
        category="debugging",
        tags=_base_tags(["@test-run", f"@test-{status.lower()}"]),
        content=content,
        metadata={"session_id": session_id, "passed": passed, "failed": failed, "branch": branch},
    )


def _handle_e2e(cmd: str, output: str, session_id: str) -> None:
    success = "passed" in output.lower() or "✓" in output
    status = "PASS" if success else "FAIL"
    branch = _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"

    title = f"[e2e] {status} — {_repo_name()}"
    content = (
        f"## E2E / Playwright Test — {status}\n\n"
        f"**Branch:** {branch}\n\n"
        f"**Command:** `{cmd[:300]}`\n\n"
        f"**Output:**\n```\n{output[-1000:]}\n```"
    )
    submit(
        title=title,
        category="debugging",
        tags=_base_tags(["@e2e-test", f"@e2e-{status.lower()}"]),
        content=content,
        metadata={"session_id": session_id, "success": success, "branch": branch},
    )


def _handle_deploy(cmd: str, output: str, session_id: str) -> None:
    host_match = re.search(r'--host\s+([\d.]+)|@([\d.]+)', cmd)
    host = (host_match.group(1) or host_match.group(2)) if host_match else "unknown"
    success = "SUCCESS" in output or "healthy" in output.lower()
    status = "SUCCESS" if success else ("FAILED" if "error" in output.lower() or "failed" in output.lower() else "RAN")
    sha = _git("rev-parse", "--short", "HEAD") or ""
    branch = _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"

    title = f"[deploy] {status} → {host} — {_repo_name()}"
    content = (
        f"## Deployment {status}\n\n"
        f"**Target:** `{host}`  **Branch:** {branch}  **SHA:** {sha}\n\n"
        f"**Command:** `{cmd[:400]}`\n\n"
        f"**Output (tail):**\n```\n{output[-800:]}\n```"
    )
    submit(
        title=title,
        category="procedure",
        tags=_base_tags(["@deployment", "@uat", f"@deploy-{status.lower()}"]),
        content=content,
        git_sha=sha,
        source_file=f"deployments/uat:{host}",
        metadata={"session_id": session_id, "host": host, "success": success, "branch": branch},
    )


def _handle_docker_build(cmd: str, output: str, session_id: str) -> None:
    image_match = re.search(r'-t\s+([\w./:_-]+)', cmd)
    image = image_match.group(1) if image_match else "unknown"
    cached = output.count("CACHED")
    success = "error" not in output.lower() and "failed" not in output.lower()
    status = "SUCCESS" if success else "FAILED"
    sha = _git("rev-parse", "--short", "HEAD") or ""

    title = f"[docker-build] {status} — {image}"
    content = (
        f"## Docker Build {status}\n\n"
        f"**Image:** `{image}`  **Cached layers:** {cached}\n\n"
        f"**Command:** `{cmd[:300]}`\n\n"
        f"**Output (tail):**\n```\n{output[-600:]}\n```"
    )
    submit(
        title=title,
        category="procedure",
        tags=_base_tags(["@docker-build", f"@docker-{status.lower()}"]),
        content=content,
        git_sha=sha,
        metadata={"session_id": session_id, "image": image, "cached_layers": cached, "success": success},
    )


# Bash pattern registry
_BASH_RULES: list[tuple[str, object]] = [
    (r"\bgit\s+commit\b", _handle_git_commit),
    (r"\bgit\s+checkout\s+-[bBc]\b|\bgit\s+switch\s+-c\b", _handle_git_branch),
    (r"\bgit\s+push\b", _handle_git_push),
    (r"\bnpm\s+run\s+build\b|\bnpm\s+run\s+build:", _handle_build),
    (r"\bnpm\s+(run\s+)?test\b|\bvitest\b|\bjest\b|\bpytest\b", _handle_test),
    (r"\bplaywright\b|\bnpx\s+playwright\b", _handle_e2e),
    (r"register-stack\.py\b|deploy\.sh\b", _handle_deploy),
    (r"\bdocker\s+build\b", _handle_docker_build),
]


# ── Event extractors: Edit / Write ────────────────────────────────────────────

_SIGNIFICANT = re.compile(
    r"\.(ts|tsx|js|jsx|py|sh|yml|yaml|json|sql|tf|env|mdc)$"
    r"|Dockerfile|docker-compose|\.claude/settings"
)
_IGNORE = re.compile(
    r"package-lock\.json|\.tsbuildinfo|/dist/|node_modules|\.log$|kb-hook\.log"
)


def _handle_file_change(tool_name: str, file_path: str, session_id: str) -> None:
    if not _SIGNIFICANT.search(file_path):
        return
    if _IGNORE.search(file_path):
        return

    sha = _git("rev-parse", "--short", "HEAD") or ""
    branch = _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    rel = file_path.replace(str(PROJECT_ROOT), "").lstrip("/\\")
    ext = Path(file_path).suffix.lstrip(".")

    title = f"[{tool_name.lower()}] {rel}"
    content = (
        f"## File {tool_name} — `{rel}`\n\n"
        f"**Branch:** {branch}  **SHA:** {sha}\n\n"
        f"**Full path:** `{file_path}`"
    )
    submit(
        title=title,
        category="configuration" if ext in ("json", "yml", "yaml", "env", "mdc") else "procedure",
        tags=_base_tags([f"@file-{tool_name.lower()}", f"@ext-{ext or 'unknown'}"]),
        content=content,
        source_file=rel,
        git_sha=sha,
        metadata={"session_id": session_id, "branch": branch, "tool": tool_name},
    )


# ── Stop handler ──────────────────────────────────────────────────────────────

def _handle_stop(session_id: str) -> None:
    branch = _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    sha = _git("rev-parse", "--short", "HEAD") or ""
    repo = _repo_name()

    title = f"[session-end] {repo} — {branch}"
    content = (
        f"## Claude Code Session Ended\n\n"
        f"**Repo:** {repo}  **Branch:** {branch}  **SHA:** {sha}\n\n"
        f"**Session:** {session_id}"
    )
    submit(
        title=title,
        category="general",
        tags=_base_tags(["@session-end"]),
        content=content,
        git_sha=sha,
        metadata={"session_id": session_id, "branch": branch},
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        payload = json.loads(raw)
    except Exception as exc:
        log(f"PARSE ERROR: {exc}")
        return

    event = payload.get("hook_event_name", "")
    session_id = payload.get("session_id", "unknown")

    try:
        if event == "PostToolUse":
            tool = payload.get("tool_name", "")
            tool_input = payload.get("tool_input") or {}
            tool_response = payload.get("tool_response") or {}
            is_error = tool_response.get("isError", False)

            if tool == "Bash":
                cmd = tool_input.get("command", "")
                output = tool_response.get("output", "") or ""
                if is_error:
                    # Capture failed deploys / builds for debugging
                    if re.search(r"register-stack\.py|npm run build|docker build|npm test", cmd):
                        output = f"[ERROR]\n{output}"
                for pattern, handler in _BASH_RULES:
                    if re.search(pattern, cmd, re.IGNORECASE):
                        handler(cmd, output, session_id)  # type: ignore[operator]
                        break  # one article per command

            elif tool in ("Edit", "Write"):
                file_path = tool_input.get("file_path", "")
                if file_path:
                    _handle_file_change(tool, file_path, session_id)

        elif event == "Stop":
            _handle_stop(session_id)

    except Exception as exc:
        log(f"HANDLER ERROR [{event}]: {exc}")


if __name__ == "__main__":
    main()
