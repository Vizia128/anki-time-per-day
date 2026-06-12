"""
scheduler.py
============

Core logic for an Anki add-on that sets a *daily study-time budget* instead of a
fixed new-cards/day count, and dynamically chooses how many new cards to
introduce each day so that:

  (a) predicted daily study time stays at/under the budget, and
  (b) the deck's new-card backlog is introduced as fast as that budget allows.

This module is intentionally free of any Anki imports so it can be unit-tested
in isolation. The thin layer that reads a live collection lives in
`adapter.py`; it produces the same plain inputs this module consumes.

Three pieces:
  1. FsrsKernel        - FSRS-6 memory dynamics (mirrors open-spaced-repetition/py-fsrs)
  2. Forecaster        - deterministic expectation simulation of future workload
  3. plan_schedule     - convolution-based controller that picks new cards/day

Notation (mirrors py-fsrs / the FSRS literature)
------------------------------------------------
  s = stability (days), d = difficulty (1..10), r = retrievability (0..1),
  w = the FSRS parameter vector. These short names are used only inside the
  kernel/forecaster math; application code uses full words.

Key modelling idea
------------------
Cards are scheduled independently in FSRS, so *expected* daily review-seconds are
additive across cards. That means the daily load is a **convolution** of the
new-card schedule with a single fresh card's expected "review tail", on top of a
baseline produced by the cards already in the collection:

    load[d] = base[d] + sum_{t <= d} new[t] * tail[d - t]

The controller exploits this: on each day it introduces the largest number of
new cards whose tail never pushes any current-or-future day over budget.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

# FSRS-6 default parameters (21), as shipped by py-fsrs / Anki. The live add-on
# should overwrite these with the user's optimized per-preset parameters.
FSRS6_DEFAULT_PARAMS = [
    0.212,
    1.2931,
    2.3065,
    8.2956,
    6.4133,
    0.8334,
    3.0194,
    0.001,
    1.8722,
    0.1666,
    0.796,
    1.4835,
    0.0614,
    0.2629,
    1.6483,
    0.6014,
    1.8729,
    0.5425,
    0.0912,
    0.0658,
    0.1542,
]

STABILITY_MIN = 0.001
MIN_DIFFICULTY = 1.0
MAX_DIFFICULTY = 10.0

# Sentinel for "no daily new-card cap". Large enough that the budget, not the
# cap, is always the binding constraint.
NO_DAILY_CAP = 9999

# Ratings (match FSRS / Anki button order)
AGAIN, HARD, GOOD, EASY = 1, 2, 3, 4


# ---------------------------------------------------------------------------
# 1. FSRS-6 kernel
# ---------------------------------------------------------------------------
class FsrsKernel:
    """Pure FSRS-6 memory dynamics. Equations mirror py-fsrs exactly."""

    def __init__(self, params=None, desired_retention: float = 0.9):
        self.w = list(params) if params is not None else list(FSRS6_DEFAULT_PARAMS)
        self.desired_retention = desired_retention
        self.decay = -self.w[20]  # signed decay (negative)
        self.factor = 0.9 ** (1.0 / self.decay) - 1.0

    # retrievability after `t` days since last review
    def retrievability(self, t: float, s: float) -> float:
        return (1.0 + self.factor * t / s) ** self.decay

    # interval that lands retrievability on the desired retention
    def next_interval(self, s: float) -> int:
        interval = (s / self.factor) * (
            self.desired_retention ** (1.0 / self.decay) - 1.0
        )
        return max(1, int(round(interval)))

    def _clamp_d(self, d: float) -> float:
        return min(max(d, MIN_DIFFICULTY), MAX_DIFFICULTY)

    def _clamp_s(self, s: float) -> float:
        return max(s, STABILITY_MIN)

    def initial_stability(self, rating: int) -> float:
        return self._clamp_s(self.w[rating - 1])

    def initial_difficulty(self, rating: int, clamp: bool = True) -> float:
        d = self.w[4] - math.e ** (self.w[5] * (rating - 1)) + 1.0
        return self._clamp_d(d) if clamp else d

    def next_difficulty(self, d: float, rating: int) -> float:
        delta = -(self.w[6] * (rating - 3))
        damped = (10.0 - d) * delta / 9.0
        arg2 = d + damped
        arg1 = self.initial_difficulty(EASY, clamp=False)
        nd = self.w[7] * arg1 + (1.0 - self.w[7]) * arg2
        return self._clamp_d(nd)

    def stability_after_recall(
        self, d: float, s: float, r: float, rating: int
    ) -> float:
        hard = self.w[15] if rating == HARD else 1.0
        easy = self.w[16] if rating == EASY else 1.0
        inc = (
            math.e ** self.w[8]
            * (11.0 - d)
            * (s ** -self.w[9])
            * (math.e ** ((1.0 - r) * self.w[10]) - 1.0)
            * hard
            * easy
        )
        return self._clamp_s(s * (1.0 + inc))

    def stability_after_forget(self, d: float, s: float, r: float) -> float:
        long_term = (
            self.w[11]
            * (d ** -self.w[12])
            * (((s + 1.0) ** self.w[13]) - 1.0)
            * (math.e ** ((1.0 - r) * self.w[14]))
        )
        short_term = s / (math.e ** (self.w[17] * self.w[18]))
        return self._clamp_s(min(long_term, short_term))

    def short_term_stability(self, s: float, rating: int) -> float:
        inc = (math.e ** (self.w[17] * (rating - 3 + self.w[18]))) * (s ** -self.w[19])
        if rating in (GOOD, EASY):
            inc = max(inc, 1.0)
        return self._clamp_s(s * inc)

    def graduated_state(self, first_rating: int = GOOD) -> tuple[float, float]:
        """(stability, difficulty) of a new card after a single same-day learning
        step (Anki default 1m/10m steps, rated Good twice => one short-term bump)."""
        s0 = self.initial_stability(first_rating)
        s = self.short_term_stability(s0, first_rating)
        d = self.initial_difficulty(first_rating)
        return s, d


# ---------------------------------------------------------------------------
# 2. Expectation forecaster
# ---------------------------------------------------------------------------
@dataclass
class CostModel:
    """Per-event study seconds. In the live add-on these come from revlog medians
    (the `time` column, in ms). Can later be made state-dependent."""

    sec_new: float = 18.0  # introducing+learning a new card (sum of same-day steps)
    sec_pass: float = 6.0  # a successful review
    sec_lapse: float = 12.0  # a failed review (longer: you re-study it)


@dataclass
class Seed:
    """A (fractional) population of cards sharing a memory state and due day."""

    mass: float
    s: float
    d: float
    due: int  # whole days from "today" (<=0 means due now)


class Forecaster:
    """Deterministic, expectation-based forward simulation.

    Reviews are assumed on-time, so retrievability at review == desired
    retention (the standard planning assumption for FSRS). Each reviewed
    cohort splits into a recall branch (mass * DR) and a forget branch
    (mass * (1-DR)); cohorts are merged by (due day, log-stability bucket,
    difficulty bucket) to keep the population bounded.
    """

    def __init__(self, kernel: FsrsKernel, cost: CostModel):
        self.kernel = kernel
        self.cost = cost

    @staticmethod
    def _key(s: float, d: float):
        return (int(round(math.log(max(s, 1e-3)) * 6)), int(round(d * 2)))

    def simulate(self, seeds: list[Seed], horizon: int):
        """Return (seconds_per_day, reviews_per_day), each length horizon+1."""
        # buckets[due_day][(sb, db)] = [mass, s*mass, d*mass]
        buckets: dict[int, dict[tuple, list]] = defaultdict(
            lambda: defaultdict(lambda: [0.0, 0.0, 0.0])
        )

        def add(due, mass, s, d):
            if mass <= 1e-12:
                return
            due = max(0, int(round(due)))
            b = buckets[due][self._key(s, d)]
            b[0] += mass
            b[1] += s * mass
            b[2] += d * mass

        for seed in seeds:
            add(seed.due, seed.mass, seed.s, seed.d)

        retention = self.kernel.desired_retention
        per_review = (
            retention * self.cost.sec_pass + (1.0 - retention) * self.cost.sec_lapse
        )
        seconds_per_day = [0.0] * (horizon + 1)
        reviews_per_day = [0.0] * (horizon + 1)

        for day in range(0, horizon + 1):
            day_bucket = buckets.get(day)
            if not day_bucket:
                continue
            for mass, s_weighted, d_weighted in list(day_bucket.values()):
                if mass <= 1e-12:
                    continue
                s = s_weighted / mass
                d = d_weighted / mass
                r = retention  # on-time review
                seconds_per_day[day] += mass * per_review
                reviews_per_day[day] += mass

                # recall branch
                recall_mass = mass * retention
                recall_s = self.kernel.stability_after_recall(d, s, r, GOOD)
                recall_d = self.kernel.next_difficulty(d, GOOD)
                add(
                    day + self.kernel.next_interval(recall_s),
                    recall_mass,
                    recall_s,
                    recall_d,
                )

                # forget branch -> relearn, due next day
                forget_mass = mass * (1.0 - retention)
                forget_s = self.kernel.stability_after_forget(d, s, r)
                forget_d = self.kernel.next_difficulty(d, AGAIN)
                add(day + 1, forget_mass, forget_s, forget_d)

        return seconds_per_day, reviews_per_day

    def base_load(self, existing: list[Seed], horizon: int):
        """Predicted review seconds/day from cards already in the collection."""
        return self.simulate(existing, horizon)[0]

    def new_card_tail(self, horizon: int, first_rating: int = GOOD):
        """Expected review seconds on each future day from ONE new card introduced
        today. tail[0] includes the learning-day cost; tail[k>0] is review load."""
        s, d = self.kernel.graduated_state(first_rating)
        first_interval = self.kernel.next_interval(s)
        seconds, _ = self.simulate(
            [Seed(mass=1.0, s=s, d=d, due=first_interval)], horizon
        )
        seconds[0] += self.cost.sec_new
        return seconds


# ---------------------------------------------------------------------------
# 3. Controller (convolution greedy MPC)
# ---------------------------------------------------------------------------
@dataclass
class Plan:
    schedule: list[int]  # new cards to introduce on each day
    predicted_seconds: list[float]  # forecast study seconds per day under the plan
    base_seconds: list[float]  # baseline from existing cards (for reference)
    completion_day: int  # last day new cards are introduced (-1 if none)
    cards_unscheduled: int  # new cards that didn't fit within the horizon
    budget_seconds: float
    feasible: bool = field(init=False)

    def __post_init__(self):
        self.feasible = self.cards_unscheduled == 0

    def today(self) -> int:
        return self.schedule[0] if self.schedule else 0

    def peak_minutes(self) -> float:
        return max(self.predicted_seconds) / 60.0 if self.predicted_seconds else 0.0


def plan_schedule(
    base: list[float],
    tail: list[float],
    budget_seconds: float,
    total_new_cards: int,
    horizon: int,
    daily_new_cap: int = NO_DAILY_CAP,
) -> Plan:
    """Greedy receding-horizon plan.

    On each day, introduce the largest number of new cards such that, given the
    tail they generate, NO current-or-future day in the horizon exceeds budget.
    Because tail is front-loaded and decaying, the binding constraint is either
    today or a near-future peak; we solve it in closed form per day.
    """
    committed = [0.0] * (horizon + 1)  # load from already-scheduled new cards
    schedule = [0] * (horizon + 1)
    remaining = total_new_cards
    tail_length = len(tail)

    for day in range(horizon + 1):
        if remaining <= 0:
            break
        # Largest n such that, for every affected future day f:
        #   base[f] + committed[f] + n * tail[f - day] <= budget
        max_new = float("inf")
        last_affected_day = min(horizon, day + tail_length - 1)
        for future_day in range(day, last_affected_day + 1):
            tail_load = tail[future_day - day]
            if tail_load <= 0:
                continue
            headroom = budget_seconds - base[future_day] - committed[future_day]
            max_new = min(max_new, headroom / tail_load)
        new_today = 0 if max_new == float("inf") else int(math.floor(max_new + 1e-09))
        new_today = max(0, min(new_today, remaining, daily_new_cap))
        if new_today > 0:
            schedule[day] = new_today
            for offset in range(tail_length):
                if day + offset <= horizon:
                    committed[day + offset] += new_today * tail[offset]
            remaining -= new_today

    predicted = [base[i] + committed[i] for i in range(horizon + 1)]
    completion = max((i for i, v in enumerate(schedule) if v > 0), default=-1)
    return Plan(
        schedule=schedule,
        predicted_seconds=predicted,
        base_seconds=base,
        completion_day=completion,
        cards_unscheduled=remaining,
        budget_seconds=budget_seconds,
    )


def make_plan(
    existing: list[Seed],
    total_new_cards: int,
    budget_minutes: float,
    kernel: FsrsKernel,
    cost: CostModel,
    horizon: int = 365,
    daily_new_cap: int = NO_DAILY_CAP,
    first_rating: int = GOOD,
) -> Plan:
    """Convenience wrapper: build base + tail, then plan. The live add-on calls
    this once per day and applies `plan.today()` as the new-cards/day limit."""
    forecaster = Forecaster(kernel, cost)
    base = forecaster.base_load(existing, horizon)
    tail = forecaster.new_card_tail(horizon, first_rating=first_rating)
    return plan_schedule(
        base=base,
        tail=tail,
        budget_seconds=budget_minutes * 60.0,
        total_new_cards=total_new_cards,
        horizon=horizon,
        daily_new_cap=daily_new_cap,
    )


def adaptive_horizon(
    existing: list[Seed],
    total_new_cards: int,
    budget_minutes: float,
    kernel: FsrsKernel,
    cost: CostModel,
) -> int:
    """Estimate a planning horizon that comfortably fits the full deck.

    Rough estimate: (new cards / estimated daily throughput) * 3 + 60,
    clamped to [30, 3650]. The 3x factor absorbs review-tail overhead and
    base-load underestimation; the +60 adds a minimum buffer.
    """
    if total_new_cards == 0:
        return 30
    retention = kernel.desired_retention
    per_review = retention * cost.sec_pass + (1.0 - retention) * cost.sec_lapse
    base_estimate = sum(seed.mass for seed in existing if seed.due == 0) * per_review
    available_seconds = max(1.0, budget_minutes * 60.0 - base_estimate)
    daily_new_cards = available_seconds / max(1.0, cost.sec_new)
    rough_days = total_new_cards / max(0.01, daily_new_cards)
    return max(30, min(3650, int(rough_days * 3) + 60))


def find_budget_for_target(
    existing: list[Seed],
    total_new_cards: int,
    target_days: int,
    kernel: FsrsKernel,
    cost: CostModel,
    daily_new_cap: int = NO_DAILY_CAP,
) -> float:
    """Binary search: minimum daily budget (minutes) whose plan finishes all
    new cards within target_days."""
    low, high = 0.0, 24.0 * 60.0
    for _ in range(35):
        mid = (low + high) / 2.0
        plan = make_plan(
            existing=existing,
            total_new_cards=total_new_cards,
            budget_minutes=mid,
            kernel=kernel,
            cost=cost,
            horizon=target_days,
            daily_new_cap=daily_new_cap,
        )
        if plan.feasible:
            high = mid
        else:
            low = mid
    return high
