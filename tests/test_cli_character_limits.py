"""Character-limit CLI behaviour.

The context-injection ceilings resolve in this order:

    CLI flag  >  LH_HARNESS_* env  >  .lh-harness/config.toml  >  dataclass default

These tests assert that ORDER by constructing configs, not by reading source
text. An earlier version of this file opened
``/home/harness/work/LongHorizon-Harness/src/lh_harness/cli.py`` by absolute
path and asserted that literal strings such as ``default=None`` appeared in it.
That test could only pass on one machine in one directory, it proved nothing
about behaviour, and — because it pinned the implementation — it failed as soon
as the config.toml source was added. A test that blocks a correct change is
worse than no test.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import unittest
from contextlib import contextmanager

from lh_harness.cli import _positive_int
from lh_harness.types import HarnessConfig


@contextmanager
def env_set(**values: str | None):
    """Set env vars for the block and restore exactly what was there before."""
    previous = {name: os.environ.get(name) for name in values}
    try:
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, old in previous.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


class TestPositiveInt(unittest.TestCase):
    def test_accepts_positive(self):
        self.assertEqual(_positive_int("1"), 1)
        self.assertEqual(_positive_int("24000"), 24_000)

    def test_rejects_zero_negative_and_non_numeric(self):
        for bad in ("0", "-1", "not_a_number", ""):
            with self.assertRaises(argparse.ArgumentTypeError):
                _positive_int(bad)


class TestCapResolutionOrder(unittest.TestCase):
    """Each source must beat the one below it."""

    CAPS = ("auditor_output_chars", "role_verified_context_chars", "role_history_chars")

    def test_defaults_when_nothing_is_set(self):
        with env_set(
            LH_HARNESS_AUDITOR_OUTPUT_CHARS=None,
            LH_HARNESS_ROLE_VERIFIED_CONTEXT_CHARS=None,
            LH_HARNESS_ROLE_HISTORY_CHARS=None,
        ):
            config = HarnessConfig()
        self.assertEqual(config.auditor_output_chars, 24_000)
        self.assertEqual(config.role_verified_context_chars, 60_000)
        self.assertEqual(config.role_history_chars, 100_000)

    def test_config_file_value_beats_the_default(self):
        """A config.toml value arrives as a constructor argument."""
        with env_set(
            LH_HARNESS_AUDITOR_OUTPUT_CHARS=None,
            LH_HARNESS_ROLE_VERIFIED_CONTEXT_CHARS=None,
            LH_HARNESS_ROLE_HISTORY_CHARS=None,
        ):
            config = HarnessConfig(auditor_output_chars=1_234)
        self.assertEqual(config.auditor_output_chars, 1_234)
        # untouched caps keep their defaults
        self.assertEqual(config.role_history_chars, 100_000)

    def test_env_beats_the_config_file(self):
        """__post_init__ applies the env override ON TOP of constructor args."""
        with env_set(LH_HARNESS_AUDITOR_OUTPUT_CHARS="4321"):
            config = HarnessConfig(auditor_output_chars=1_234)
        self.assertEqual(config.auditor_output_chars, 4_321)

    def test_invalid_env_keeps_the_lower_source_and_warns(self):
        """A typo must not silently fall back to the dataclass default."""
        with env_set(LH_HARNESS_AUDITOR_OUTPUT_CHARS="not_a_number"):
            with self.assertWarns(RuntimeWarning):
                config = HarnessConfig(auditor_output_chars=1_234)
        self.assertEqual(config.auditor_output_chars, 1_234)

    def test_non_positive_env_is_rejected_and_warns(self):
        with env_set(LH_HARNESS_ROLE_HISTORY_CHARS="0"):
            with self.assertWarns(RuntimeWarning):
                config = HarnessConfig()
        self.assertEqual(config.role_history_chars, 100_000)


class TestCLIFlagsExist(unittest.TestCase):
    """The flags must be reachable through the real CLI surface.

    The parser is built inline inside ``main()`` rather than by a
    ``build_parser()`` helper, so there is nothing to introspect without
    executing a run. ``--help`` is therefore the honest seam: it exercises the
    actual argparse configuration and fails if a flag is renamed or dropped.

    NOT covered here, and deliberately not faked: that a CLI flag beats the
    environment override. That path only runs inside ``_run_command`` after a
    real run is constructed. Asserting it would need ``main()`` refactored to
    expose its parser — worth doing, but it is a change to this CLI's shape and
    does not belong in the same commit as the caps fix.
    """

    def test_flags_appear_in_run_help(self):
        from lh_harness.cli import main

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with self.assertRaises(SystemExit) as caught:
                main(["run", "--help"])
        self.assertEqual(caught.exception.code, 0)

        help_text = buffer.getvalue()
        for flag in (
            "--auditor-output-chars",
            "--role-verified-context-chars",
            "--role-history-chars",
        ):
            self.assertIn(flag, help_text, f"{flag} is missing from `run --help`")


if __name__ == "__main__":
    unittest.main()
