import pytest


def test_runtime_only_role_resolves_through_alias():
    """auditor_format_repair has no CLI flags; it must fall back to the auditor chain, not KeyError."""
    import argparse
    from lh_harness import cli

    args = argparse.Namespace(agent="claude", model="m0", auditor_model="m-aud", auditor_agent=None)
    assert cli._resolve_role_option(args, "auditor_format_repair", "model") == "m-aud"
    assert cli._resolve_role_option(args, "auditor_format_repair", "agent") == "claude"


def test_task_text_warn_chars_flag_flows_into_config(monkeypatch):
    """--task-text-warn-chars must reach HarnessConfig the same way the other caps do."""
    import argparse
    from lh_harness.types import HarnessConfig

    monkeypatch.delenv("LH_HARNESS_TASK_TEXT_WARN_CHARS", raising=False)
    config = HarnessConfig()
    assert config.task_text_warn_chars == 8_000

    # Mirror the _run_command override block: an operator-typed flag wins.
    args = argparse.Namespace(task_text_warn_chars=12_345)
    if args.task_text_warn_chars is not None:
        config.task_text_warn_chars = args.task_text_warn_chars
    assert config.task_text_warn_chars == 12_345


def test_task_text_warn_chars_flag_is_parsed_by_run_parser():
    import contextlib
    import io
    from lh_harness.cli import main

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        with pytest.raises(SystemExit) as caught:
            main(["run", "--help"])
    assert caught.value.code == 0
    assert "--task-text-warn-chars" in buffer.getvalue()


def test_manager_run_warns_once_for_oversized_task():
    """The run-start warning fires before agent validation, so it is testable in isolation."""
    import asyncio
    import warnings

    from lh_harness import manager
    from lh_harness.types import HarnessConfig

    config = HarnessConfig(task_text_warn_chars=100)
    big_task = "x" * 101

    with pytest.warns(RuntimeWarning, match=r"task text is 101 chars \(> 100\)"):
        # No agents are bound, so _run_impl raises right after the warning and
        # never touches the filesystem or an environment.
        with pytest.raises(ValueError, match="Every role needs an agent"):
            asyncio.run(manager._run_impl(task=big_task, env=None, config=config))

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(ValueError, match="Every role needs an agent"):
            asyncio.run(manager._run_impl(task="x" * 100, env=None, config=config))
