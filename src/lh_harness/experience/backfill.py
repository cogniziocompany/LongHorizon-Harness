"""Reflection-weighted value backfill (MSCE Eq. 2).

After an episode closes and a terminal reward R is available, each L1 step is
backfilled with Eq. 2 of ``From Memory to Skills`` (arXiv 2607.16621):

    V(f(i;t)) = alpha(i;t) * R + (1 - alpha(i;t)) * gamma * V(f(i;t+1))

with the terminal step inheriting the terminal feedback directly:
``V(f(i;H)) = R``. High-alpha steps (faithful, concrete, causally informative
reflections) blend the terminal reward with what they observed locally; low-
alpha steps mostly inherit value from the steps that follow them.

The harness applies this at round granularity: one TraceUnit per managed
round, alphas from :mod:`lh_harness.experience.reflection`, R from
:mod:`lh_harness.experience.reward`, gamma fixed at the paper's 0.9 default.
"""

from __future__ import annotations

from typing import Any, Sequence

from .reflection import ALPHA_LOW
from .trace import TraceUnit

# Paper Appendix C: gamma = 0.9 (Table 5 worked example: alpha 0.7, R 0.8
# reproduces V = 0.776 for the key discovery step).
DEFAULT_GAMMA = 0.9


def backfill(units: Sequence[TraceUnit] | Sequence[Any], R: float, gamma: float = DEFAULT_GAMMA) -> list[float]:
    """Backfill step values and return them in input order.

    ``units`` is the episode's ordered trace units (one per managed round).
    The last unit is the terminal anchor and takes ``R`` exactly; every
    earlier unit applies Eq. 2 with its reflection weight. Units whose alpha
    is unknown behave like unverified reflections and take ``ALPHA_LOW``.

    The function is pure: it returns a new list of floats and leaves the
    callers' units untouched (callers assign ``value`` on their own objects).
    """
    if not 0.0 <= gamma < 1.0:
        raise ValueError("gamma must be in [0, 1)")
    if R is None:
        raise ValueError("terminal reward R is required for backfill")
    reward = float(R)
    if not -1.0 <= reward <= 1.0:
        raise ValueError("terminal reward R must lie in [-1, 1]")
    material = list(units or [])
    if not material:
        return []
    values: list[float] = [0.0] * len(material)
    # Terminal anchor: the last step inherits the terminal feedback directly.
    values[-1] = reward
    next_value = reward
    for index in range(len(material) - 2, -1, -1):
        weight = _alpha_of(material[index])
        values[index] = weight * reward + (1.0 - weight) * gamma * next_value
        next_value = values[index]
    return values


def backfill_values(units: Sequence[Any], R: float, gamma: float = DEFAULT_GAMMA) -> list[float]:
    """Alias kept for call sites that want the values without touching units."""
    return backfill(units, R, gamma)


def apply_backfill(units: Sequence[TraceUnit], R: float, gamma: float = DEFAULT_GAMMA) -> list[float]:
    """Compute Eq. 2 values and write them onto ``TraceUnit.value`` in place.

    Slice 3's capture uses this once the reward and reflection weights are
    known, immediately before the trace records are serialized.
    """
    values = backfill(units, R, gamma)
    for unit, value in zip(units, values, strict=True):
        if isinstance(unit, TraceUnit):
            unit.value = value
    return values


def _alpha_of(unit: Any) -> float:
    alpha = getattr(unit, "alpha", None)
    if alpha is None and isinstance(unit, dict):
        alpha = unit.get("alpha")
    if alpha is None:
        return ALPHA_LOW
    try:
        weight = float(alpha)
    except (TypeError, ValueError):
        return ALPHA_LOW
    if not 0.0 <= weight <= 1.0:
        return ALPHA_LOW
    return weight
