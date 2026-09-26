"""Rule-based redaction (MSCE B.11): every secret class, plus the spec's
headline case — a synthetic key inside an auditor report must never reach
the persisted trace."""

from __future__ import annotations

import json

from lh_harness.experience.redact import (
    REDACTED,
    SECRET_ENV_VAR_NAMES,
    redact_text,
    redact_trace,
    redact_value,
    redact_texts,
)
from lh_harness.experience.trace import RoleTrace, TraceUnit

FAKE_ANTHROPIC_KEY = "sk-ant-api03-" + "A1b2C3d4E5f6" * 2
FAKE_OPENAI_KEY = "sk-proj-" + "Ze9Yx8Wv7U" * 3
FAKE_GHP_TOKEN = "ghp_" + "ABCDEFGH01" * 3 + "ij"
FAKE_HEX_TOKEN = "deadbeefcafe0123456789abcdef0123456789"  # 40 hex chars


def _trace(reflection: str, **overrides) -> TraceUnit:
    kwargs = dict(
        run_id="run_abc123",
        task_context_id="run_abc123",
        round_index=3,
        next_step="cli",
        state_summary="state",
        action="executor did things",
        observation="feedback",
    )
    kwargs.update(overrides)
    return TraceUnit(reflection=reflection, **kwargs)


def test_bearer_token_scheme():
    out = redact_text(f"curl -H 'Authorization: Bearer {FAKE_HEX_TOKEN[:-4]}xyz' https://x")
    assert "Bearer" in out
    assert f"Bearer {REDACTED}" in out
    assert "xyz" not in out.split("https://x")[0]


def test_bearer_min_length_keeps_prose():
    # "bearer tokens rotate daily" holds no credential; the 8-char bar leaves
    # real English alone.
    text = "the bearer tokens rotate daily, and the flag is off by default"
    assert redact_text(text) == text


def test_sk_style_keys():
    for key in (FAKE_ANTHROPIC_KEY, FAKE_OPENAI_KEY):
        out = redact_text(f"export key {key} done")
        assert key not in out
        assert REDACTED in out


def test_github_token_families():
    for key in (
        FAKE_GHP_TOKEN,
        "gho_" + "Aa1" * 8,
        "ghu_" + "Bb2" * 8,
        "ghs_" + "Cc3" * 8,
        "ghr_" + "Dd4" * 8,
        "github_pat_" + "Ee5Ff6Gg7Hh8Ii9Jj0Kk",
    ):
        out = redact_text(f"tok: {key} end")
        assert key not in out, key
        assert REDACTED in out


def test_private_key_block_keeps_markers_but_masks_body():
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" * 2
    text = (
        "-----BEGIN PRIVATE KEY-----\n"
        f"{body}\n"
        "-----END PRIVATE KEY-----\n"
    )
    out = redact_text(text)
    assert body not in out
    assert "-----BEGIN PRIVATE KEY-----" in out
    assert "-----END PRIVATE KEY-----" in out
    assert REDACTED in out


def test_rsa_private_key_variant():
    body = "Proc-Type: 4,ENCRYPTED\nDEK-Info: AES-256-CBC"
    text = f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----"
    out = redact_text(text)
    assert body not in out
    assert "BEGIN RSA PRIVATE KEY" in out


def test_password_assignments():
    for text in ("password=hunter2x", "password: hunter2x", 'db_password="hunter2 x"'):
        out = redact_text(text)
        assert "hunter2" not in out
        assert REDACTED in out


def test_long_hex_strings():
    out = redact_text(f"token {FAKE_HEX_TOKEN} tail")
    assert FAKE_HEX_TOKEN not in out
    assert REDACTED in out
    # 31 hex chars is below the bar.
    short = "0" * 31
    assert short in redact_text(f"hash {short} ok")


def test_known_env_var_names_masked_by_assignment():
    assert SECRET_ENV_VAR_NAMES
    for name in ("ANTHROPIC_API_KEY", "LH_HARNESS_WEB_TOKEN", "MOONSHOT_API_KEY"):
        out = redact_text(f"{name}=s3cr3t-value-abcdef ok")
        assert "s3cr3t-value-abcdef" not in out
        assert f"{name}={REDACTED}" in out


def test_env_var_name_prefixes_do_not_partially_match():
    # A name that merely *contains* a known name must not be masked: the
    # assignment is bounded by word characters.
    text = "MY_ANTHROPIC_API_KEY_LOOKUP=visible123"
    assert "visible123" in redact_text(text)


def test_redaction_is_idempotent():
    once = redact_text(f"key {FAKE_ANTHROPIC_KEY} and password=hunter2x")
    assert redact_text(once) == once


def test_nonmatching_text_passes_through():
    text = "run 13ccf895 on branch main, models kimi/claude, hex ok"
    assert redact_text(text) == text


def test_empty_and_whitespace():
    assert redact_text("") == ""
    assert redact_texts(["", "x"]) == ("", "x")


def _auditor_report_with_key() -> str:
    return (
        "Status: incomplete\n"
        "Integrity: clean\n"
        "Contract audit: aligned\n\n"
        "Audit facts and evidence: the config leaked a key:\n"
        f"  ANTHROPIC_API_KEY={FAKE_ANTHROPIC_KEY}\n"
        f"and the PAT {FAKE_GHP_TOKEN} appeared in the log with password=hunter2x.\n"
    )


def test_synthetic_key_in_auditor_report_never_reaches_output():
    """Spec headline: a key typed into the auditor report cannot reach the trace."""
    unit = _trace(_auditor_report_with_key())
    cleaned = redact_trace(unit)
    blob = json.dumps(cleaned.to_dict(), ensure_ascii=False)
    assert FAKE_ANTHROPIC_KEY not in blob
    assert FAKE_GHP_TOKEN not in blob
    assert "hunter2x" not in blob
    assert REDACTED in blob
    # The input trace is untouched: redact_trace returns a new unit.
    assert FAKE_ANTHROPIC_KEY in unit.reflection


def test_redact_trace_covers_every_text_field():
    unit = _trace(
        "clean",
        action=f"export OPENAI_API_KEY={FAKE_OPENAI_KEY}",
        observation="ok",
        error_signature=f"blocker: ANTHROPIC_API_KEY={FAKE_ANTHROPIC_KEY}",
        workspace="/home/harness/work/repo",
        repo="repo",
        branch="main",
        domain_tags=("lh_harness",),
        tool_names=("Bash", "Read"),
        roles={
            "manager": RoleTrace(
                role="manager",
                agent="claude_code",
                model="claude-sonnet-5",
                route_tier="ideal",
                route_rationale=f"rationale quotes ANTHROPIC_API_KEY={FAKE_ANTHROPIC_KEY}",
            )
        },
        alpha=0.7,
        value=0.42,
    )
    cleaned = redact_trace(unit).to_dict()
    blob = json.dumps(cleaned, ensure_ascii=False)
    assert FAKE_OPENAI_KEY not in blob
    assert FAKE_ANTHROPIC_KEY not in blob
    # Non-text fields survive the round trip exactly.
    assert cleaned["round_index"] == 3
    assert cleaned["alpha"] == 0.7
    assert cleaned["value"] == 0.42
    # Identities that were never recorded stay absent (not empty strings).
    assert "device_id" not in cleaned and "terminal_id" not in cleaned
    assert "hydra_node" not in cleaned and "occurred_at" not in cleaned
    # Roles are preserved as structured data.
    assert cleaned["roles"]["manager"]["route_rationale"] == REDACTED or REDACTED in cleaned[
        "roles"
    ]["manager"].get("route_rationale", "")


def test_redact_trace_preserves_device_fields_without_inventing_values():
    unit = _trace("clean")
    unit.device_id = "desk03-tty7"
    unit.hydra_node = "corsairai300"
    cleaned = redact_trace(unit).to_dict()
    assert cleaned["device_id"] == "desk03-tty7"
    assert cleaned["hydra_node"] == "corsairai300"
    assert "terminal_id" not in cleaned


def test_redact_value_nested_and_typed():
    payload = {
        "list": [f"Bearer {FAKE_HEX_TOKEN}", {"deep": "password=hunter2x"}],
        "tuple": ("plain", f"sk-ant-api03-{'q' * 24}"),
        "number": 7,
        "flag": None,
    }
    out = redact_value(payload)
    assert out["number"] == 7 and out["flag"] is None
    assert isinstance(out["tuple"], tuple)
    blob = json.dumps(out)
    assert "hunter2x" not in blob and FAKE_HEX_TOKEN not in blob and "q" * 24 not in blob


def test_mapping_keys_are_redacted_too():
    out = redact_value({f"sk-ant-api03-{'z' * 20}": "value"})
    assert next(iter(out)) == REDACTED
