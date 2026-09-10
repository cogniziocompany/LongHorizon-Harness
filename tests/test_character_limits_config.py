from __future__ import annotations

import os
import sys
import pytest

# Add src to path so we import the local version
sys.path.insert(0, '/home/harness/work/LongHorizon-Harness/src')

from lh_harness.types import HarnessConfig


def test_harness_config_default_character_limits():
    """Test that HarnessConfig uses the expected default values."""
    config = HarnessConfig()
    assert config.auditor_output_chars == 24_000
    assert config.role_verified_context_chars == 60_000
    assert config.role_history_chars == 100_000


def test_harness_config_environment_override():
    """Test that environment variables can override character limits."""
    # Save original environment
    orig_auditor = os.environ.get("LH_HARNESS_AUDITOR_OUTPUT_CHARS")
    orig_verified = os.environ.get("LH_HARNESS_ROLE_VERIFIED_CONTEXT_CHARS")
    orig_history = os.environ.get("LH_HARNESS_ROLE_HISTORY_CHARS")

    try:
        # Set test values
        os.environ["LH_HARNESS_AUDITOR_OUTPUT_CHARS"] = "1000"
        os.environ["LH_HARNESS_ROLE_VERIFIED_CONTEXT_CHARS"] = "2000"
        os.environ["LH_HARNESS_ROLE_HISTORY_CHARS"] = "3000"

        config = HarnessConfig()
        assert config.auditor_output_chars == 1000
        assert config.role_verified_context_chars == 2000
        assert config.role_history_chars == 3000
    finally:
        # Restore original environment
        if orig_auditor is None:
            os.environ.pop("LH_HARNESS_AUDITOR_OUTPUT_CHARS", None)
        else:
            os.environ["LH_HARNESS_AUDITOR_OUTPUT_CHARS"] = orig_auditor

        if orig_verified is None:
            os.environ.pop("LH_HARNESS_ROLE_VERIFIED_CONTEXT_CHARS", None)
        else:
            os.environ["LH_HARNESS_ROLE_VERIFIED_CONTEXT_CHARS"] = orig_verified

        if orig_history is None:
            os.environ.pop("LH_HARNESS_ROLE_HISTORY_CHARS", None)
        else:
            os.environ["LH_HARNESS_ROLE_HISTORY_CHARS"] = orig_history


def test_harness_config_invalid_environment_values_ignored():
    """Test that invalid environment variable values are ignored."""
    # Save original environment
    orig_auditor = os.environ.get("LH_HARNESS_AUDITOR_OUTPUT_CHARS")
    orig_verified = os.environ.get("LH_HARNESS_ROLE_VERIFIED_CONTEXT_CHARS")
    orig_history = os.environ.get("LH_HARNESS_ROLE_HISTORY_CHARS")

    try:
        # Set invalid values
        os.environ["LH_HARNESS_AUDITOR_OUTPUT_CHARS"] = "not_a_number"
        os.environ["LH_HARNESS_ROLE_VERIFIED_CONTEXT_CHARS"] = "also_not_a_number"
        os.environ["LH_HARNESS_ROLE_HISTORY_CHARS"] = "still_not_a_number"

        config = HarnessConfig()
        # Should keep defaults since values are invalid
        assert config.auditor_output_chars == 24_000
        assert config.role_verified_context_chars == 60_000
        assert config.role_history_chars == 100_000
    finally:
        # Restore original environment
        if orig_auditor is None:
            os.environ.pop("LH_HARNESS_AUDITOR_OUTPUT_CHARS", None)
        else:
            os.environ["LH_HARNESS_AUDITOR_OUTPUT_CHARS"] = orig_auditor

        if orig_verified is None:
            os.environ.pop("LH_HARNESS_ROLE_VERIFIED_CONTEXT_CHARS", None)
        else:
            os.environ["LH_HARNESS_ROLE_VERIFIED_CONTEXT_CHARS"] = orig_verified

        if orig_history is None:
            os.environ.pop("LH_HARNESS_ROLE_HISTORY_CHARS", None)
        else:
            os.environ["LH_HARNESS_ROLE_HISTORY_CHARS"] = orig_history