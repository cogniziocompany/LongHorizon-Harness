"""The auditor's child environment must let the read-only verifier run.

Regression for 2026-11-11: ``build_auditor_subprocess_env`` handed the
auditor's own token to the child only as ``GH_TOKEN``, while the verifier
(``ReadOnlyGitHubClient.from_environment``) reads ONLY
``LH_HARNESS_AUDITOR_GITHUB_TOKEN`` and refuses ``GH_TOKEN``.  Every
in-auditor PR verification therefore ended ``pr_verifier_unavailable``
even on nodes that had the auditor token installed (CT110 canary
20261112T003329Z_82a27ca9).
"""

from __future__ import annotations

from lh_harness.auditor_env import build_auditor_subprocess_env
from lh_harness.auditor_verify import (
    AUDITOR_GITHUB_TOKEN_ENV,
    ReadOnlyGitHubClient,
    resolve_auditor_credential,
)

AUDITOR_VALUE = "github_pat_auditor_only_test_value"
EXECUTOR_VALUE = "ghp_executor_shared_test_value"


def _parent_env() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/harness",
        AUDITOR_GITHUB_TOKEN_ENV: AUDITOR_VALUE,
        "GH_TOKEN": EXECUTOR_VALUE,
        "GITHUB_TOKEN": EXECUTOR_VALUE,
    }


def test_child_carries_auditor_token_under_the_verifier_name(tmp_path) -> None:
    child = build_auditor_subprocess_env(_parent_env(), config_dir=tmp_path)
    assert child.get(AUDITOR_GITHUB_TOKEN_ENV) == AUDITOR_VALUE
    assert "GH_TOKEN" not in child
    assert "GITHUB_TOKEN" not in child
    assert EXECUTOR_VALUE not in child.values()


def test_verifier_resolves_its_credential_inside_the_child(tmp_path) -> None:
    child = build_auditor_subprocess_env(_parent_env(), config_dir=tmp_path)
    credential = resolve_auditor_credential(child)
    assert credential.present
    client = ReadOnlyGitHubClient.from_environment(child)
    assert client is not None


def test_child_has_no_token_when_parent_has_none(tmp_path) -> None:
    parent = _parent_env()
    del parent[AUDITOR_GITHUB_TOKEN_ENV]
    child = build_auditor_subprocess_env(parent, config_dir=tmp_path)
    assert AUDITOR_GITHUB_TOKEN_ENV not in child
    assert "GH_TOKEN" not in child
    assert not resolve_auditor_credential(child).present
