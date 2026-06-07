"""
adapter.py
==========
Thin layer between a live Anki collection and the time_budget scheduler.

Every Anki API used here was verified against Anki's pylib and two reference
add-ons (lune-stone/anki-addon-limit-new-by-young, open-spaced-repetition/
fsrs4anki-helper).

Designed to be unit-testable: every public function accepts an optional `col=`
argument so tests can pass a real anki.collection.Collection directly without
going through aqt.mw.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Optional

from .scheduler import CostModel, FsrsKernel, Plan, Seed, make_plan, GOOD


# ---------------------------------------------------------------------------
# Collection access helper
# ---------------------------------------------------------------------------
def _get_col(col=None):
    """Return `col` if provided, otherwise fall back to aqt.mw.col."""
    if col is not None:
        return col
    import aqt
    return aqt.mw.col


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass
class DeckResult:
    deck_name: str
    did: int
    today_new_limit: int
    completion_day: int
    feasible: bool
    cards_unscheduled: int
    peak_minutes: float
    base_peak_minutes: float
    cost: CostModel
    plan: Optional[Plan]
    fsrs_disabled: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Reading inputs from the collection
# ---------------------------------------------------------------------------
def subdeck_ids_csv(col, did: int) -> str:
    from anki.utils import ids2str
    return ids2str(col.decks.deck_and_child_ids(did))


def read_fsrs_params(col, did: int):
    """Return (desired_retention, params_list_or_None) from the deck's preset.

    FSRS-6 requires exactly 21 parameters. We accept the value only when the
    stored list has that length; anything shorter (FSRS-4/5 legacy weights) is
    ignored. When FSRS is globally enabled but the deck has no optimised params
    yet, we return the FSRS-6 defaults so planning still works.
    """
    from .scheduler import FSRS6_DEFAULT_PARAMS
    conf = col.decks.config_dict_for_deck_id(did)
    dr = conf.get("desiredRetention", 0.9)

    for key in ("fsrsParams6", "fsrsWeights"):
        candidate = conf.get(key)
        if candidate and len(candidate) == 21:
            return dr, list(candidate)

    # FSRS enabled globally but not yet optimised for this deck → use defaults
    if col.get_config("fsrs"):
        return dr, list(FSRS6_DEFAULT_PARAMS)

    return dr, None  # SM-2 / FSRS disabled


def is_fsrs_enabled(col, did: int) -> bool:
    """True if the deck's preset contains FSRS parameters."""
    _, params = read_fsrs_params(col, did)
    return params is not None


def read_cost_model(col, did: int) -> CostModel:
    """Personalised study seconds derived from revlog medians.

    type: 0=learn 1=review 2=relearn 3=filtered; ease/rating: 1=Again..4=Easy.
    Medians are robust to the occasional 'walked-away-from-screen' card.
    """
    dids = subdeck_ids_csv(col, did)
    cid_filter = f"cid IN (SELECT id FROM cards WHERE did IN {dids})"

    def med(where: str, default: float) -> float:
        rows = col.db.list(
            f"SELECT time/1000.0 FROM revlog WHERE {cid_filter} AND {where} "
            f"AND time > 0 AND time < 120000 ORDER BY time"
        )
        return float(statistics.median(rows)) if rows else default

    sec_pass = med("type IN (1,2) AND ease >= 2", 7.0)
    sec_lapse = med("type IN (1,2) AND ease = 1", 14.0)
    # new-card cost = median total learning time per newly-introduced card
    per_card = col.db.list(
        f"SELECT SUM(time)/1000.0 FROM revlog WHERE {cid_filter} AND type = 0 "
        f"AND time > 0 AND time < 120000 GROUP BY cid"
    )
    sec_new = float(statistics.median(per_card)) if per_card else 20.0
    return CostModel(sec_new=sec_new, sec_pass=sec_pass, sec_lapse=sec_lapse)


def read_existing_cards(col, did: int) -> list[Seed]:
    """One Seed per non-new, non-suspended card. FSRS state from cards.data,
    due expressed as whole days from today (clamped to 0 for overdue/learning)."""
    today = col.sched.today
    dids = subdeck_ids_csv(col, did)
    rows = col.db.all(
        f"""
        SELECT
            json_extract(data, '$.s'),
            json_extract(data, '$.d'),
            COALESCE(json_extract(data, '$.decay'), 0.1542),
            due, queue
        FROM cards
        WHERE did IN {dids}
          AND queue != -1
          AND type  != 0
          AND data  != ''
          AND json_extract(data, '$.s') IS NOT NULL
        """
    )
    seeds: list[Seed] = []
    for s, d, _decay, due, queue in rows:
        if s is None:
            continue
        # queue 2 = review, queue 3 = day-learning — both use day-number `due`
        # all other queues (intraday learning) have epoch-second `due` → due now
        due_off = max(0, int(due) - today) if queue in (2, 3) else 0
        seeds.append(Seed(mass=1.0, s=float(s), d=float(d or 5.0), due=due_off))
    return seeds


def count_new_cards(col, did: int) -> int:
    dids = re.sub(r"[()]", "", subdeck_ids_csv(col, did))
    return len(col.find_cards(f"is:new -is:suspended did:{dids}"))


# ---------------------------------------------------------------------------
# Writing the limit (same mechanism as Limit-New-by-Young)
# ---------------------------------------------------------------------------
def set_today_new_limit(col, did: int, limit: int) -> None:
    """Write a today-only new-card limit. Never touches the deck's preset."""
    deck = col.decks.get(did)
    deck["newLimitToday"] = {"limit": int(max(0, limit)), "today": col.sched.today}
    col.decks.save(deck)


# ---------------------------------------------------------------------------
# Config matching
# ---------------------------------------------------------------------------
def match_deck_configs(config: dict, deck_name: str) -> dict | None:
    """Return the first config entry whose deckNames matches deck_name, or None.

    deckNames can be a regex string (fullmatch) or a list of exact names.
    """
    for entry in config.get("decks", []):
        pattern = entry.get("deckNames", "")
        if isinstance(pattern, str):
            try:
                if re.fullmatch(pattern, deck_name):
                    return entry
            except re.error:
                pass
        elif isinstance(pattern, list):
            if deck_name in pattern:
                return entry
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def update_deck(
    did: int,
    budget_minutes: float,
    horizon: int = 365,
    daily_new_cap: int = 9999,
    desired_retention_override: float | None = None,
    dry_run: bool = False,
    col=None,
) -> DeckResult:
    """Compute (and optionally write) today's new-card limit for one deck.

    dry_run=True: reads everything, computes the plan, but skips the write.
    col=None: falls back to aqt.mw.col (for use from hooks/menu actions).
    """
    col = _get_col(col)
    deck_name = col.decks.get(did)["name"]
    dr, params = read_fsrs_params(col, did)

    if params is None:
        return DeckResult(
            deck_name=deck_name,
            did=did,
            today_new_limit=0,
            completion_day=-1,
            feasible=False,
            cards_unscheduled=0,
            peak_minutes=0.0,
            base_peak_minutes=0.0,
            cost=CostModel(),
            plan=None,
            fsrs_disabled=True,
        )

    if desired_retention_override is not None:
        dr = desired_retention_override

    kernel = FsrsKernel(params=params, desired_retention=dr)
    cost = read_cost_model(col, did)
    existing = read_existing_cards(col, did)
    total_new = count_new_cards(col, did)

    plan = make_plan(
        existing=existing,
        total_new_cards=total_new,
        budget_minutes=budget_minutes,
        kernel=kernel,
        cost=cost,
        horizon=horizon,
        daily_new_cap=daily_new_cap,
        first_rating=GOOD,
    )

    if not dry_run:
        set_today_new_limit(col, did, plan.today())

    base_peak = max(plan.base_seconds) / 60.0 if plan.base_seconds else 0.0

    return DeckResult(
        deck_name=deck_name,
        did=did,
        today_new_limit=plan.today(),
        completion_day=plan.completion_day,
        feasible=plan.feasible,
        cards_unscheduled=plan.cards_unscheduled,
        peak_minutes=plan.peak_minutes(),
        base_peak_minutes=base_peak,
        cost=cost,
        plan=plan,
    )
