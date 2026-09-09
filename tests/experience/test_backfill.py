"""Table 5 reproduction and Eq. 2 properties for the L1 value backfill.

The worked example the spec pins (Appendix C, Table 5): alpha 0.7, R 0.8,
gamma 0.9 reproduces V = 0.776 for the key discovery step, and the terminal
step carries R directly.
"""

from __future__ import annotations

import pytest

from lh_harness.experience import ALPHA_LOW, DEFAULT_GAMMA, TraceUnit, backfill
from lh_harness.experience.backfill import apply_backfill


def _unit(index: int, alpha: float) -> TraceUnit:
    return TraceUnit(run_id="run-a", task_context_id="run-a", round_index=index, alpha=alpha)


def test_table5_task_a_reproduces_0776_exactly() -> None:
    # Task A (R_A = 0.8): tau_1,1 alpha 0.7 -> V 0.776; tau_1,2 alpha 0.5 -> V 0.80.
    units = [_unit(1, 0.7), _unit(2, 0.5)]
    values = backfill(units, R=0.8, gamma=0.9)
    assert values == pytest.approx([0.776, 0.8], abs=1e-12)
    assert round(values[0], 12) == 0.776


def test_table5_task_b_reproduces_paper_values() -> None:
    # Task B (R_B = 0.9): tau_2,1 alpha 0.6 -> V 0.864; tau_2,2 alpha 0.4 -> V 0.90.
    units = [_unit(1, 0.6), _unit(2, 0.4)]
    values = backfill(units, R=0.9, gamma=0.9)
    assert values == pytest.approx([0.864, 0.9], abs=1e-12)


def test_eq2_formula_matches_paper_expression() -> None:
    units = [_unit(1, 0.7), _unit(2, 0.5)]
    values = backfill(units, R=0.8, gamma=0.9)
    assert values[0] == pytest.approx(0.7 * 0.8 + (1 - 0.7) * 0.9 * 0.8, abs=1e-15)
    assert values[1] == pytest.approx(0.8, abs=1e-15)


def test_gamma_defaults_to_paper_value() -> None:
    assert DEFAULT_GAMMA == 0.9
    units = [_unit(1, 0.7), _unit(2, 0.5)]
    assert backfill(units, R=0.8) == backfill(units, R=0.8, gamma=0.9)


def test_terminal_step_is_the_R_anchor() -> None:
    # A single-step episode takes R directly, with no discount applied.
    assert backfill([_unit(1, 0.9)], R=0.4) == [0.4]
    assert backfill([], R=0.4) == []


def test_high_alpha_blends_more_of_R_low_alpha_inherits_more() -> None:
    units = [_unit(1, 1.0), _unit(2, 0.0)]
    values = backfill(units, R=0.8, gamma=0.9)
    # alpha 1.0 -> V = R; alpha 0.0 -> V = gamma * V_next.
    assert values == pytest.approx([0.8, 0.8], abs=1e-15)
    assert values[0] == pytest.approx(1.0 * 0.8 + 0.0 * 0.9 * 0.8, abs=1e-15)


def test_negative_terminal_reward_propagates_downward() -> None:
    units = [_unit(1, 0.7), _unit(2, 0.5)]
    values = backfill(units, R=-0.9, gamma=0.9)
    assert values[1] == pytest.approx(-0.9, abs=1e-12)
    assert values[0] == pytest.approx(0.7 * -0.9 + 0.3 * 0.9 * -0.9, abs=1e-12)
    assert values[0] < 0


def test_missing_alpha_behaves_like_an_unverified_reflection() -> None:
    units = [TraceUnit(run_id="run-a", task_context_id="run-a", round_index=1), _unit(2, 0.5)]
    values = backfill(units, R=0.8, gamma=0.9)
    assert values[0] == pytest.approx(ALPHA_LOW * 0.8 + 0.7 * 0.9 * 0.8, abs=1e-12)


def test_out_of_range_R_and_gamma_are_rejected() -> None:
    units = [_unit(1, 0.5)]
    with pytest.raises(ValueError):
        backfill(units, R=1.5)
    with pytest.raises(ValueError):
        backfill(units, R=-1.5)
    with pytest.raises(ValueError):
        backfill(units, R=0.5, gamma=1.0)
    with pytest.raises(ValueError):
        backfill(units, R=0.5, gamma=-0.1)


def test_apply_backfill_writes_values_onto_units() -> None:
    units = [_unit(1, 0.7), _unit(2, 0.5)]
    values = apply_backfill(units, R=0.8)
    assert units[0].value == pytest.approx(values[0], abs=1e-12)
    assert units[1].value == pytest.approx(0.8, abs=1e-12)


def test_backfill_is_pure_over_dictionaries() -> None:
    # Round records may arrive as plain dicts (the final report's "rounds").
    units = [{"round_index": 1, "alpha": 0.7}, {"round_index": 2, "alpha": 0.5}]
    values = backfill(units, R=0.8)
    assert values == pytest.approx([0.776, 0.8], abs=1e-12)
    assert "value" not in units[0]
