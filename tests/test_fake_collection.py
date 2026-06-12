"""
test_fake_collection.py
=======================
Integration tests using a real Anki pylib collection (headless, no GUI).
Requires: pip install anki

All tests are self-contained: they build a throwaway collection, assert, then
close it. Nothing touches real Anki profiles.
"""
from __future__ import annotations

import time_budget.adapter as A
from time_budget.scheduler import FsrsKernel, make_plan

from .fake_collection_builder import (
    build_fake_collection,
    build_fake_collection_empty,
    build_fake_collection_no_fsrs,
    build_fake_collection_with_overdue,
    build_fake_collection_with_subdecks,
    build_fake_collection_with_suspended,
)


# ---------------------------------------------------------------------------
# 1. Original test (port of ideas/fake_collection.py::test_adapter_against_fake_collection)
# ---------------------------------------------------------------------------
def test_original_fake_collection():
    col, did, path = build_fake_collection(n_review=250, n_new=1750)
    try:
        dr, params = A.read_fsrs_params(col, did)
        assert abs(dr - 0.9) < 1e-9
        assert params is not None and len(params) == 21

        cost = A.read_cost_model(col, did)
        assert abs(cost.sec_pass - 7.0) < 0.5, cost
        assert abs(cost.sec_lapse - 14.0) < 0.5, cost
        assert abs(cost.sec_new - 22.0) < 1.0, cost   # two 11 s steps

        seeds = A.read_existing_cards(col, did)
        assert len(seeds) == 250, len(seeds)
        assert all(s.s > 0 and 1 <= s.d <= 10 and s.due >= 0 for s in seeds)

        assert A.count_new_cards(col, did) == 1750

        kernel = FsrsKernel(params=params, desired_retention=dr)
        plan = make_plan(seeds, A.count_new_cards(col, did), 30.0, kernel, cost,
                         horizon=400, daily_new_cap=200)
        assert plan.peak_minutes() <= 30.0 + 1e-6
        assert plan.today() > 0

        A.set_today_new_limit(col, did, plan.today())
        deck = col.decks.get(did)
        assert deck["newLimitToday"]["limit"] == plan.today()
        assert deck["newLimitToday"]["today"] == col.sched.today

        # preset must be untouched
        assert col.decks.config_dict_for_deck_id(did)["desiredRetention"] == 0.9
    finally:
        col.close()


# ---------------------------------------------------------------------------
# 2. Empty deck
# ---------------------------------------------------------------------------
def test_empty_deck():
    col, did, path = build_fake_collection_empty()
    try:
        dr, params = A.read_fsrs_params(col, did)
        assert params is not None

        seeds = A.read_existing_cards(col, did)
        assert len(seeds) == 0

        n_new = A.count_new_cards(col, did)
        assert n_new == 0

        kernel = FsrsKernel(params=params, desired_retention=dr)
        cost = A.read_cost_model(col, did)
        plan = make_plan(seeds, n_new, 30.0, kernel, cost, horizon=30)

        assert plan.today() == 0
        assert plan.completion_day == -1
        assert plan.feasible is True
        assert plan.cards_unscheduled == 0
    finally:
        col.close()


# ---------------------------------------------------------------------------
# 3. FSRS-disabled deck
# ---------------------------------------------------------------------------
def test_fsrs_disabled_deck():
    col, did, path = build_fake_collection_no_fsrs()
    try:
        assert not A.is_fsrs_enabled(col, did)
        _, params = A.read_fsrs_params(col, did)
        assert params is None

        result = A.update_deck(did=did, budget_minutes=30.0, col=col, dry_run=True)
        assert result.fsrs_disabled is True
        assert result.plan is None
    finally:
        col.close()


# ---------------------------------------------------------------------------
# 4. Subdeck structure
# ---------------------------------------------------------------------------
def test_subdeck_structure():
    col, parent_did, child_did, path = build_fake_collection_with_subdecks(
        n_parent_review=5,
        n_child_review=10,
        n_child_new=5,
    )
    try:
        # Reading from parent should include both parent and child cards
        seeds = A.read_existing_cards(col, parent_did)
        assert len(seeds) == 15, f"Expected 15 review seeds (5+10), got {len(seeds)}"

        n_new = A.count_new_cards(col, parent_did)
        assert n_new == 5, f"Expected 5 new cards (child only), got {n_new}"

        # Writing limit on parent
        A.set_today_new_limit(col, parent_did, 3)
        deck = col.decks.get(parent_did)
        assert deck["newLimitToday"]["limit"] == 3
        assert deck["newLimitToday"]["today"] == col.sched.today
    finally:
        col.close()


# ---------------------------------------------------------------------------
# 5. Suspended cards excluded
# ---------------------------------------------------------------------------
def test_suspended_cards_excluded():
    col, did, path, n_suspended = build_fake_collection_with_suspended(
        n_review=50, n_new=50, n_suspended=20
    )
    try:
        # Suspended new cards (queue=-1) must not count as new
        n_new = A.count_new_cards(col, did)
        assert n_new == 50 - 20, (
            f"Expected {50 - 20} new (suspended excluded), got {n_new}"
        )

        # Suspended cards are excluded from existing-card seeds too
        seeds = A.read_existing_cards(col, did)
        assert len(seeds) == 50, (
            f"Expected 50 review seeds (suspensions were on new cards), got {len(seeds)}"
        )
    finally:
        col.close()


# ---------------------------------------------------------------------------
# 6. Overdue cards: due_off clamped to 0
# ---------------------------------------------------------------------------
def test_overdue_cards_clamped():
    col, did, path = build_fake_collection_with_overdue(n_review=30, n_new=50)
    try:
        seeds = A.read_existing_cards(col, did)
        assert len(seeds) == 30
        assert all(s.due >= 0 for s in seeds), (
            f"Negative due found: {[s.due for s in seeds if s.due < 0]}"
        )
        # Some cards were set to today-10, so at least some must have due=0
        assert any(s.due == 0 for s in seeds)
    finally:
        col.close()


# ---------------------------------------------------------------------------
# 7. Infeasible budget end-to-end
# ---------------------------------------------------------------------------
def test_infeasible_budget_end_to_end():
    col, did, path = build_fake_collection(n_review=0, n_new=2000)
    try:
        dr, params = A.read_fsrs_params(col, did)
        kernel = FsrsKernel(params=params, desired_retention=dr)
        cost = A.read_cost_model(col, did)
        seeds = A.read_existing_cards(col, did)
        n_new = A.count_new_cards(col, did)

        plan = make_plan(seeds, n_new, 5.0, kernel, cost, horizon=365)
        assert plan.feasible is False, "Expected infeasible plan"
        assert plan.cards_unscheduled > 0
        assert plan.peak_minutes() <= 5.0 + 1e-6, (
            f"Budget exceeded: peak={plan.peak_minutes():.4f}"
        )
    finally:
        col.close()


# ---------------------------------------------------------------------------
# 8. Preset untouched after limit write
# ---------------------------------------------------------------------------
def test_preset_untouched_after_limit_write():
    col, did, path = build_fake_collection(n_review=50, n_new=100)
    try:
        dr_before = col.decks.config_dict_for_deck_id(did)["desiredRetention"]

        A.set_today_new_limit(col, did, 7)

        dr_after = col.decks.config_dict_for_deck_id(did)["desiredRetention"]
        assert dr_before == dr_after, (
            f"Preset modified: before={dr_before}, after={dr_after}"
        )
        deck = col.decks.get(did)
        assert deck["newLimitToday"]["limit"] == 7
    finally:
        col.close()


# ---------------------------------------------------------------------------
# 9. update_deck dry_run=True writes nothing
# ---------------------------------------------------------------------------
def test_dry_run_writes_nothing():
    col, did, path = build_fake_collection(n_review=50, n_new=200)
    try:
        # Ensure no prior limit is set
        deck = col.decks.get(did)
        limit_before = deck.get("newLimitToday")

        result = A.update_deck(did=did, budget_minutes=30.0, col=col, dry_run=True)

        deck = col.decks.get(did)
        limit_after = deck.get("newLimitToday")

        assert limit_after == limit_before, (
            f"dry_run=True should not write limit; before={limit_before}, after={limit_after}"
        )
        assert result.today_new_limit > 0, "Expected a non-zero planned limit"
        assert result.fsrs_disabled is False
        assert result.error is None
    finally:
        col.close()


# ---------------------------------------------------------------------------
# 10. compute_deck_plan: studied time is detected and subtracted
# ---------------------------------------------------------------------------
def test_compute_deck_plan_subtracts_studied_time():
    col, did, path = build_fake_collection(n_review=250, n_new=1750)
    try:
        studied = A.studied_today_minutes(col, did)
        assert studied > 0, "Fake revlog rows are dated today"

        # Budget already exhausted by today's studying: limit collapses.
        spent = A.compute_deck_plan(col, did, budget_minutes=30.0)
        # Same long-term budget, but today's budget leaves 30 min headroom.
        fresh = A.compute_deck_plan(
            col, did,
            budget_minutes=30.0,
            today_budget_minutes=studied + 30.0,
        )
        assert abs(spent.studied_minutes - studied) < 1e-6
        assert fresh.today_new_limit > spent.today_new_limit
        # Long-term stats come from the full-budget plan, not today's.
        assert fresh.completion_day == spent.completion_day
    finally:
        col.close()


# ---------------------------------------------------------------------------
# 11. compute_deck_plan: write_limit flag controls the write
# ---------------------------------------------------------------------------
def test_compute_deck_plan_write_limit():
    col, did, path = build_fake_collection(n_review=50, n_new=200)
    try:
        before = col.decks.get(did).get("newLimitToday")
        A.compute_deck_plan(col, did, budget_minutes=60.0)
        assert col.decks.get(did).get("newLimitToday") == before, (
            "write_limit=False (default) must not write"
        )

        result = A.compute_deck_plan(col, did, budget_minutes=60.0, write_limit=True)
        deck = col.decks.get(did)
        assert deck["newLimitToday"]["limit"] == result.today_new_limit
        assert deck["newLimitToday"]["today"] == col.sched.today
    finally:
        col.close()


# ---------------------------------------------------------------------------
# 12. compute_deck_plan: FSRS-disabled deck is never written
# ---------------------------------------------------------------------------
def test_compute_deck_plan_fsrs_disabled():
    col, did, path = build_fake_collection_no_fsrs()
    try:
        result = A.compute_deck_plan(col, did, budget_minutes=30.0, write_limit=True)
        assert result.fsrs_disabled is True
        assert result.plan is None
        assert col.decks.get(did).get("newLimitToday") is None
    finally:
        col.close()


# ---------------------------------------------------------------------------
# 13. compute_deck_plan: daily_new_cap None/0 means uncapped
# ---------------------------------------------------------------------------
def test_compute_deck_plan_cap_conventions():
    col, did, path = build_fake_collection(n_review=0, n_new=500)
    try:
        uncapped = A.compute_deck_plan(col, did, budget_minutes=240.0)
        zero_cap = A.compute_deck_plan(col, did, budget_minutes=240.0, daily_new_cap=0)
        capped = A.compute_deck_plan(col, did, budget_minutes=240.0, daily_new_cap=3)
        assert uncapped.today_new_limit == zero_cap.today_new_limit
        assert capped.today_new_limit <= 3
        assert uncapped.today_new_limit > capped.today_new_limit
    finally:
        col.close()
