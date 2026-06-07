"""
time_budget_scheduler.py
========================

Core logic for an Anki add-on that sets a *daily study-time budget* instead of a
fixed new-cards/day count, and dynamically chooses how many new cards to
introduce each day so that:

  (a) predicted daily study time stays at/under the budget, and
  (b) the deck's new-card backlog is introduced as fast as that budget allows.

This module is intentionally free of any Anki imports so it can be unit-tested
in isolation. The thin layer that reads a live collection lives in
`anki_adapter.py`; it produces the same plain inputs this module consumes.

Three pieces:
  1. FsrsKernel        - FSRS-6 memory dynamics (mirrors open-spaced-repetition/py-fsrs)
  2. Forecaster        - deterministic expectation simulation of future workload
  3. plan_schedule     - convolution-based controller that picks new cards/day

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
    0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194, 0.001,
    1.8722, 0.1666, 0.796, 1.4835, 0.0614, 0.2629, 1.6483, 0.6014,
    1.8729, 0.5425, 0.0912, 0.0658, 0.1542,
]

STABILITY_MIN = 0.001
MIN_DIFFICULTY = 1.0
MAX_DIFFICULTY = 10.0

# Ratings (match FSRS / Anki button order)
AGAIN, HARD, GOOD, EASY = 1, 2, 3, 4


# ---------------------------------------------------------------------------
# 1. FSRS-6 kernel
# ---------------------------------------------------------------------------
class FsrsKernel:
    """Pure FSRS-6 memory dynamics. Equations mirror py-fsrs exactly."""

    def __init__(self, params=None, desired_retention: float = 0.9):
        self.w = list(params) if params is not None else list(FSRS6_DEFAULT_PARAMS)
        self.dr = desired_retention
        self.decay = -self.w[20]                      # signed decay (negative)
        self.factor = 0.9 ** (1.0 / self.decay) - 1.0

    # retrievability after `t` days since last review
    def retrievability(self, t: float, s: float) -> float:
        return (1.0 + self.factor * t / s) ** self.decay

    # interval that lands retrievability on the desired retention
    def next_interval(self, s: float) -> int:
        ivl = (s / self.factor) * (self.dr ** (1.0 / self.decay) - 1.0)
        return max(1, int(round(ivl)))

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

    def stability_after_recall(self, d: float, s: float, r: float, rating: int) -> float:
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
    sec_new: float = 18.0      # introducing+learning a new card (sum of same-day steps)
    sec_pass: float = 6.0      # a successful review
    sec_lapse: float = 12.0    # a failed review (longer: you re-study it)


@dataclass
class Seed:
    """A (fractional) population of cards sharing a memory state and due day."""
    mass: float
    s: float
    d: float
    due: int                   # whole days from "today" (<=0 means due now)


class Forecaster:
    """Deterministic, expectation-based forward simulation.

    Reviews are assumed on-time, so retrievability at review == desired
    retention (the standard planning assumption for FSRS). Each reviewed
    cohort splits into a recall branch (mass * DR) and a forget branch
    (mass * (1-DR)); cohorts are merged by (due day, log-stability bucket,
    difficulty bucket) to keep the population bounded.
    """

    def __init__(self, kernel: FsrsKernel, cost: CostModel):
        self.k = kernel
        self.c = cost

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

        for z in seeds:
            add(z.due, z.mass, z.s, z.d)

        dr = self.k.dr
        c = self.c
        per_review = dr * c.sec_pass + (1.0 - dr) * c.sec_lapse
        sec = [0.0] * (horizon + 1)
        revs = [0.0] * (horizon + 1)

        for day in range(0, horizon + 1):
            day_bucket = buckets.get(day)
            if not day_bucket:
                continue
            for (mass, sw, dw) in list(day_bucket.values()):
                if mass <= 1e-12:
                    continue
                s = sw / mass
                d = dw / mass
                r = dr  # on-time review
                sec[day] += mass * per_review
                revs[day] += mass

                # recall branch
                mp = mass * dr
                sp = self.k.stability_after_recall(d, s, r, GOOD)
                dp = self.k.next_difficulty(d, GOOD)
                add(day + self.k.next_interval(sp), mp, sp, dp)

                # forget branch -> relearn, due next day
                ml = mass * (1.0 - dr)
                sl = self.k.stability_after_forget(d, s, r)
                dl = self.k.next_difficulty(d, AGAIN)
                add(day + 1, ml, sl, dl)

        return sec, revs

    def base_load(self, existing: list[Seed], horizon: int):
        """Predicted review seconds/day from cards already in the collection."""
        return self.simulate(existing, horizon)[0]

    def new_card_tail(self, horizon: int, first_rating: int = GOOD):
        """Expected review seconds on each future day from ONE new card introduced
        today. tail[0] includes the learning-day cost; tail[k>0] is review load."""
        s, d = self.k.graduated_state(first_rating)
        first_ivl = self.k.next_interval(s)
        sec, _ = self.simulate([Seed(mass=1.0, s=s, d=d, due=first_ivl)], horizon)
        sec[0] += self.c.sec_new
        return sec


# ---------------------------------------------------------------------------
# 3. Controller (convolution greedy MPC)
# ---------------------------------------------------------------------------
@dataclass
class Plan:
    schedule: list[int]            # new cards to introduce on each day
    predicted_seconds: list[float] # forecast study seconds per day under the plan
    base_seconds: list[float]      # baseline from existing cards (for reference)
    completion_day: int            # last day new cards are introduced (-1 if none)
    cards_unscheduled: int         # new cards that didn't fit within the horizon
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
    daily_new_cap: int = 9999,
) -> Plan:
    """Greedy receding-horizon plan.

    On each day, introduce the largest number of new cards such that, given the
    tail they generate, NO current-or-future day in the horizon exceeds budget.
    Because tail is front-loaded and decaying, the binding constraint is either
    today or a near-future peak; we solve it in closed form per day.
    """
    H = horizon
    conv = [0.0] * (H + 1)          # load already committed by scheduled new cards
    schedule = [0] * (H + 1)
    remaining = total_new_cards
    tlen = len(tail)

    for d in range(H + 1):
        if remaining <= 0:
            break
        # largest n s.t. base[e] + conv[e] + n*tail[e-d] <= budget for all e>=d
        n_max = float("inf")
        upper = min(H, d + tlen - 1)
        for e in range(d, upper + 1):
            tk = tail[e - d]
            if tk <= 0:
                continue
            headroom = budget_seconds - base[e] - conv[e]
            n_max = min(n_max, headroom / tk)
        if n_max == float("inf"):
            n = 0
        else:
            n = int(math.floor(n_max + 1e-9))
        n = max(0, min(n, remaining, daily_new_cap))
        if n > 0:
            schedule[d] = n
            for k in range(tlen):
                if d + k <= H:
                    conv[d + k] += n * tail[k]
            remaining -= n

    predicted = [base[i] + conv[i] for i in range(H + 1)]
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
    daily_new_cap: int = 9999,
    first_rating: int = GOOD,
) -> Plan:
    """Convenience wrapper: build base + tail, then plan. The live add-on calls
    this once per day and applies `plan.today()` as the new-cards/day limit."""
    fc = Forecaster(kernel, cost)
    base = fc.base_load(existing, horizon)
    tail = fc.new_card_tail(horizon, first_rating=first_rating)
    return plan_schedule(
        base=base,
        tail=tail,
        budget_seconds=budget_minutes * 60.0,
        total_new_cards=total_new_cards,
        horizon=horizon,
        daily_new_cap=daily_new_cap,
    )
