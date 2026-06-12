"""
test_controller.py
==================
Pure-Python unit tests for plan_schedule / make_plan.
No anki or aqt import required.
"""

from __future__ import annotations

import pytest
from time_budget.scheduler import (
    CostModel,
    Forecaster,
    FsrsKernel,
    Seed,
    make_plan,
    plan_schedule,
)


def _kernel() -> FsrsKernel:
    return FsrsKernel(desired_retention=0.9)


def _cost() -> CostModel:
    return CostModel(sec_new=20.0, sec_pass=7.0, sec_lapse=14.0)


# ---------------------------------------------------------------------------
# Empty deck
# ---------------------------------------------------------------------------
def test_empty_deck():
    plan = make_plan(
        existing=[],
        total_new_cards=0,
        budget_minutes=30.0,
        kernel=_kernel(),
        cost=_cost(),
        horizon=30,
    )
    assert plan.today() == 0
    assert plan.completion_day == -1
    assert plan.feasible is True
    assert plan.cards_unscheduled == 0
    assert all(v == 0 for v in plan.schedule)


# ---------------------------------------------------------------------------
# Infeasible budget
# ---------------------------------------------------------------------------
def test_infeasible_budget():
    """5 min / 2000 new cards with default costs cannot fit in 365 days."""
    plan = make_plan(
        existing=[],
        total_new_cards=2000,
        budget_minutes=5.0,
        kernel=_kernel(),
        cost=_cost(),
        horizon=365,
    )
    assert plan.feasible is False
    assert plan.cards_unscheduled > 0
    # Budget must never be exceeded even for an infeasible plan
    assert plan.peak_minutes() <= 5.0 + 1e-6, f"peak={plan.peak_minutes():.4f}"


# ---------------------------------------------------------------------------
# daily_new_cap respected
# ---------------------------------------------------------------------------
def test_daily_new_cap():
    cap = 5
    plan = make_plan(
        existing=[],
        total_new_cards=100,
        budget_minutes=60.0,
        kernel=_kernel(),
        cost=_cost(),
        horizon=365,
        daily_new_cap=cap,
    )
    assert all(v <= cap for v in plan.schedule), (
        f"cap={cap} violated: max daily={max(plan.schedule)}"
    )


# ---------------------------------------------------------------------------
# Budget never exceeded — feasible plans only
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("budget", [10.0, 30.0, 60.0])
def test_budget_never_exceeded(budget):
    plan = make_plan(
        existing=[],
        total_new_cards=200,
        budget_minutes=budget,
        kernel=_kernel(),
        cost=_cost(),
        horizon=365,
    )
    assert plan.peak_minutes() <= budget + 1e-6, (
        f"budget={budget}, peak={plan.peak_minutes():.4f}"
    )


# ---------------------------------------------------------------------------
# Base load: existing cards generate positive future load
# ---------------------------------------------------------------------------
def test_base_load_positive():
    k = _kernel()
    cost = _cost()
    seeds = [Seed(mass=1.0, s=10.0, d=5.0, due=5) for _ in range(50)]
    fc = Forecaster(k, cost)
    base = fc.base_load(seeds, horizon=60)
    assert any(v > 0 for v in base), "Expected non-zero base load from existing cards"
    # Cards due on day 5 → days 0–4 should be near zero
    assert sum(base[:5]) < 1.0, f"Unexpected early load: {sum(base[:5]):.4f}"


# ---------------------------------------------------------------------------
# plan_schedule: zero tail → today()=0 (can't schedule without cost info)
# ---------------------------------------------------------------------------
def test_zero_tail():
    base = [0.0] * 31
    tail = [0.0] * 31
    plan = plan_schedule(
        base=base,
        tail=tail,
        budget_seconds=1800.0,
        total_new_cards=10,
        horizon=30,
    )
    assert plan.today() == 0


# ---------------------------------------------------------------------------
# Feasibility flag: small deck always fits large budget
# ---------------------------------------------------------------------------
def test_feasible_small_deck():
    plan = make_plan(
        existing=[],
        total_new_cards=10,
        budget_minutes=60.0,
        kernel=_kernel(),
        cost=_cost(),
        horizon=365,
    )
    assert plan.feasible is True
    assert plan.cards_unscheduled == 0
    assert plan.today() > 0
