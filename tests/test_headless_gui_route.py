from __future__ import annotations

from lh_harness.manager import _effective_next_step
from lh_harness.role_prompts import MANAGER_NEXT_CLI, MANAGER_NEXT_DONE, MANAGER_NEXT_GUI


def test_gui_kept_when_flag_unset(monkeypatch):
    monkeypatch.delenv("LH_HARNESS_DISABLE_GUI_ROUTE", raising=False)
    assert _effective_next_step(MANAGER_NEXT_GUI) == MANAGER_NEXT_GUI


def test_gui_becomes_cli_on_headless_node(monkeypatch):
    monkeypatch.setenv("LH_HARNESS_DISABLE_GUI_ROUTE", "1")
    assert _effective_next_step(MANAGER_NEXT_GUI) == MANAGER_NEXT_CLI


def test_other_routes_untouched(monkeypatch):
    monkeypatch.setenv("LH_HARNESS_DISABLE_GUI_ROUTE", "1")
    assert _effective_next_step(MANAGER_NEXT_CLI) == MANAGER_NEXT_CLI
    assert _effective_next_step(MANAGER_NEXT_DONE) == MANAGER_NEXT_DONE
