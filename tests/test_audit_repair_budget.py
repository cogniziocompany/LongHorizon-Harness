from __future__ import annotations

from lh_harness.manager import _format_repair_budget
from lh_harness.types import EpisodeBudget


def test_repair_budget_caps_at_300_seconds():
    assert _format_repair_budget(EpisodeBudget(max_duration_seconds=900)).max_duration_seconds == 300


def test_repair_budget_keeps_smaller_budgets():
    assert _format_repair_budget(EpisodeBudget(max_duration_seconds=60)).max_duration_seconds == 60


def test_repair_budget_floor_is_30_seconds():
    assert _format_repair_budget(EpisodeBudget(max_duration_seconds=10)).max_duration_seconds == 30
