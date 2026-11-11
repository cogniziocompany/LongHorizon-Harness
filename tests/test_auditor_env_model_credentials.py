"""easybutt0n-80 follow-up: the auditor keeps its MODEL access, never GitHub access."""

from lh_harness.auditor_env import AUDITOR_CREDENTIAL_ENV_NAMES, build_auditor_subprocess_env


def _source():
    return {
        "PATH": "/usr/bin",
        "HOME": "/home/harness",
        "ANTHROPIC_AUTH_TOKEN": "model-token",
        "ANTHROPIC_API_KEY": "model-key",
        "ANTHROPIC_BASE_URL": "https://gateway.example/",
        "GH_TOKEN": "gh-shared",
        "GITHUB_TOKEN": "gh-shared-2",
        "LH_HARNESS_FLEET_TOKEN": "fleet",
        "SOME_OTHER_SECRET": "x",
    }


def test_auditor_env_carries_model_credentials(tmp_path):
    env = build_auditor_subprocess_env(_source(), config_dir=tmp_path)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "model-token"
    assert env["ANTHROPIC_API_KEY"] == "model-key"
    assert env["ANTHROPIC_BASE_URL"] == "https://gateway.example/"


def test_auditor_env_still_drops_github_and_unknown_credentials(tmp_path):
    env = build_auditor_subprocess_env(_source(), config_dir=tmp_path)
    for name in AUDITOR_CREDENTIAL_ENV_NAMES:
        assert name not in env
    assert "SOME_OTHER_SECRET" not in env
