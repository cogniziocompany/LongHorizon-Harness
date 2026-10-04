"""Task A3d security review H-B / M-A: no secret reaches a service child process,
and a run's .git/config cannot make service-side git run its code."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from lh_harness import caller_auth
from lh_harness.safe_subprocess import SAFE_GIT_CONFIG, child_env, git_argv, scrub_env, seal_process_environment

REPO = Path(__file__).resolve().parents[1]


def test_seal_process_environment_moves_secrets_into_memory() -> None:
    environ = {
        "PATH": "/usr/bin",
        "LH_HARNESS_WEB_TOKEN": "w",
        "LH_HARNESS_CALLER_OVERSEER_SECRET": "o",
        "LH_HARNESS_CALLER_FLEET_ADMIN_SECRET": "f",
        "LH_HARNESS_ASK_VAULT_CMD": "/usr/bin/sudo -n -u lhasks /usr/local/sbin/lh-ask-vault",
    }
    try:
        moved = seal_process_environment(environ)
        assert moved == ["LH_HARNESS_CALLER_FLEET_ADMIN_SECRET", "LH_HARNESS_CALLER_OVERSEER_SECRET", "LH_HARNESS_WEB_TOKEN"]
        assert environ == {"PATH": "/usr/bin", "LH_HARNESS_ASK_VAULT_CMD": environ["LH_HARNESS_ASK_VAULT_CMD"]}
        assert caller_auth.captured_secret("LH_HARNESS_CALLER_OVERSEER_SECRET") == "o"
        spec = {"secret_env": "LH_HARNESS_CALLER_OVERSEER_SECRET"}
        assert caller_auth._secret_for(spec) == "o"
    finally:
        caller_auth._CAPTURED_SECRETS.clear()


def test_run_web_server_seals_before_building_the_app() -> None:
    source = (REPO / "src" / "lh_harness" / "webapi" / "server.py").read_text(encoding="utf-8")
    body = source[source.index("def run_web_server("):source.index("def start_web_server(") if "def start_web_server(" in source else None]
    assert body.index("seal_process_environment()") < body.index("create_app(")


def test_child_env_and_scrub_drop_control_plane_names() -> None:
    env = {"PATH": "/bin", "LH_HARNESS_WEB_TOKEN": "x", "LH_HARNESS_CALLER_X_SECRET": "x", "LH_HARNESS_ASK_STORE_DIR": "/s", "HOME": "/h"}
    assert scrub_env(env) == {"PATH": "/bin", "HOME": "/h"}
    assert child_env(env)["GIT_TERMINAL_PROMPT"] == "0"
    assert git_argv("status")[:5] == ["git", *SAFE_GIT_CONFIG]


@pytest.fixture
def poisoned_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A repo whose .git/config runs a script on `git status` (fsmonitor)."""
    if shutil.which("git") is None:
        pytest.skip("needs git")
    repo = tmp_path / "ws"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(tmp_path)}
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    (repo / "f.txt").write_text("x", encoding="utf-8")
    marker = tmp_path / "pwned"
    hook = tmp_path / "fsmon.sh"
    hook.write_text(f"#!/bin/sh\nenv > {marker}\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", str(hook)], check=True, env=env)
    return repo, marker


def test_plain_git_status_would_run_the_planted_fsmonitor(poisoned_repo) -> None:
    """Control: proves the attack works without the hardening."""
    repo, marker = poisoned_repo
    subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, check=False)
    assert marker.exists()


def test_service_git_calls_do_not_run_the_planted_fsmonitor(poisoned_repo, monkeypatch) -> None:
    from lh_harness import launcher, workspace_guard, workspace_identity, workspace_park

    repo, marker = poisoned_repo
    monkeypatch.setenv("LH_HARNESS_CALLER_OVERSEER_SECRET", "must-not-leak")
    launcher._dirty_workspace_occupied(str(repo))
    workspace_guard._git_soft(repo, "status", "--porcelain")
    workspace_identity._run_git(str(repo), "status", "--porcelain")
    workspace_park._git(repo, "status", "--porcelain", check=False)
    assert not marker.exists()


def test_every_service_git_call_is_hardened() -> None:
    """Audit: no bare ["git", ...] argv left in the modules the service runs."""
    offenders = []
    for path in (REPO / "src" / "lh_harness").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r'\[\s*"git"\s*,(?!\s*\*SAFE_GIT_CONFIG)', text):
            if path.name == "safe_subprocess.py":
                continue
            offenders.append(f"{path.relative_to(REPO)}:{text[:m.start()].count(chr(10)) + 1}")
    assert offenders == []


def test_no_service_module_copies_the_full_environment_for_git() -> None:
    for rel in ("launcher.py", "workspace_guard.py", "workspace_identity.py", "workspace_park.py"):
        text = (REPO / "src" / "lh_harness" / rel).read_text(encoding="utf-8")
        assert "os.environ.copy()" not in text, rel
