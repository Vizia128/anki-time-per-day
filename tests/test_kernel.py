"""
test_kernel.py
==============
1. Verify scheduler.py imports with no anki/aqt installed.
2. Replay 400 random rating sequences through FsrsKernel vs py-fsrs reference.
   Asserts: max relative S error < 1e-6, max D error < 1e-6, max interval error < 1 day.
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timezone


def test_no_anki_import():
    """scheduler.py must be importable with no anki/aqt package on sys.path."""
    # Remove anki from sys.modules if somehow present (it's installed in the venv
    # for integration tests, but scheduler.py must not depend on it at import time).
    saved = {k: v for k, v in sys.modules.items() if k in ("anki", "aqt")}
    for k in saved:
        sys.modules.pop(k)
    try:
        import importlib

        import time_budget.scheduler as sched_mod

        importlib.reload(sched_mod)
        assert hasattr(sched_mod, "FsrsKernel")
        assert hasattr(sched_mod, "make_plan")
        assert hasattr(sched_mod, "plan_schedule")
        assert hasattr(sched_mod, "FSRS6_DEFAULT_PARAMS")
    finally:
        sys.modules.update(saved)


def test_kernel_matches_py_fsrs():
    """Port of ideas/validate_kernel.py — kernel must agree with py-fsrs to < 1e-6."""
    from fsrs import Card, Rating, Scheduler
    from time_budget.scheduler import AGAIN, EASY, GOOD, HARD, FsrsKernel

    DR = 0.9
    ref_sched = Scheduler(desired_retention=DR, enable_fuzzing=False)
    k = FsrsKernel(desired_retention=DR)

    rng = random.Random(0)
    max_s_err = max_d_err = max_ivl_err = 0.0

    for _trial in range(400):
        card = Card()
        now = datetime(2025, 1, 1, tzinfo=timezone.utc)
        our_s = our_d = None
        seq = [rng.choice([AGAIN, HARD, GOOD, EASY]) for _ in range(rng.randint(1, 8))]
        for rating in seq:
            if card.due is not None:
                now = max(now, card.due)
            elapsed = (
                (now - card.last_review).total_seconds() / 86400.0
                if card.last_review is not None
                else 0.0
            )

            if our_s is None:
                our_s = k.initial_stability(rating)
                our_d = k.initial_difficulty(rating)
            else:
                r = k.retrievability(elapsed, our_s)
                if elapsed < 1.0:
                    our_s = k.short_term_stability(our_s, rating)
                elif rating == AGAIN:
                    our_s = k.stability_after_forget(our_d, our_s, r)
                else:
                    our_s = k.stability_after_recall(our_d, our_s, r, rating)
                our_d = k.next_difficulty(our_d, rating)

            card, _ = ref_sched.review_card(card, Rating(rating), review_datetime=now)
            max_s_err = max(
                max_s_err, abs(card.stability - our_s) / max(card.stability, 1e-6)
            )
            max_d_err = max(max_d_err, abs(card.difficulty - our_d))

    for s in [0.5, 1, 3, 10, 35, 100, 400]:
        iv_ours = k.next_interval(s)
        decay = ref_sched.parameters[20]
        iv_ref = round((s / (0.9 ** (1 / (-decay)) - 1)) * (DR ** (1 / (-decay)) - 1))
        max_ivl_err = max(max_ivl_err, abs(iv_ours - max(1, iv_ref)))

    assert max_s_err < 1e-6, f"S error {max_s_err:.2e} exceeds 1e-6"
    assert max_d_err < 1e-6, f"D error {max_d_err:.2e} exceeds 1e-6"
    assert max_ivl_err < 1, f"Interval error {max_ivl_err} days"
