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

from .scheduler import (
    GOOD,
    NO_DAILY_CAP,
    CostModel,
    FsrsKernel,
    Plan,
    Seed,
    adaptive_horizon,
    make_plan,
)

# Floor for "remaining budget today": even when the user has already studied
# past their budget, the planner still gets a sliver so it returns a valid
# (usually zero-new-cards) plan instead of degenerating.
MINIMUM_EFFECTIVE_BUDGET_MINUTES = 0.5


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
    deck_id: int
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
    studied_minutes: float = 0.0   # minutes already studied today (from revlog)
    horizon_days: int = 0          # planning horizon the forecast used


def fsrs_disabled_result(deck_name: str, deck_id: int) -> DeckResult:
    """Placeholder result for a deck whose preset has no FSRS parameters."""
    return DeckResult(
        deck_name=deck_name,
        deck_id=deck_id,
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


def error_result(deck_name: str, deck_id: int, message: str) -> DeckResult:
    """Placeholder result for a deck whose planning raised an exception."""
    return DeckResult(
        deck_name=deck_name,
        deck_id=deck_id,
        today_new_limit=0,
        completion_day=-1,
        feasible=False,
        cards_unscheduled=0,
        peak_minutes=0.0,
        base_peak_minutes=0.0,
        cost=CostModel(),
        plan=None,
        error=message,
    )


# ---------------------------------------------------------------------------
# Reading inputs from the collection
# ---------------------------------------------------------------------------
def subdeck_ids_csv(col, deck_id: int) -> str:
    """Parenthesised CSV of the deck's id plus all child deck ids, for SQL IN."""
    from anki.utils import ids2str
    return ids2str(col.decks.deck_and_child_ids(deck_id))


def read_fsrs_params(col, deck_id: int):
    """Return (desired_retention, params_list_or_None) from the deck's preset.

    FSRS-6 requires exactly 21 parameters. We accept the value only when the
    stored list has that length; anything shorter (FSRS-4/5 legacy weights) is
    ignored. When FSRS is globally enabled but the deck has no optimised params
    yet, we return the FSRS-6 defaults so planning still works.
    """
    from .scheduler import FSRS6_DEFAULT_PARAMS
    preset = col.decks.config_dict_for_deck_id(deck_id)
    desired_retention = preset.get("desiredRetention", 0.9)

    for key in ("fsrsParams6", "fsrsWeights"):
        candidate = preset.get(key)
        if candidate and len(candidate) == 21:
            return desired_retention, list(candidate)

    # FSRS enabled globally but not yet optimised for this deck → use defaults
    if col.get_config("fsrs"):
        return desired_retention, list(FSRS6_DEFAULT_PARAMS)

    return desired_retention, None  # SM-2 / FSRS disabled


def is_fsrs_enabled(col, deck_id: int) -> bool:
    """True if the deck's preset contains FSRS parameters."""
    _, params = read_fsrs_params(col, deck_id)
    return params is not None


def read_cost_model(col, deck_id: int) -> CostModel:
    """Personalised study seconds derived from revlog medians.

    type: 0=learn 1=review 2=relearn 3=filtered; ease/rating: 1=Again..4=Easy.
    Medians are robust to the occasional 'walked-away-from-screen' card.
    """
    deck_ids = subdeck_ids_csv(col, deck_id)
    card_filter = f"cid IN (SELECT id FROM cards WHERE did IN {deck_ids})"

    def median_seconds(where: str, default: float) -> float:
        rows = col.db.list(
            f"SELECT time/1000.0 FROM revlog WHERE {card_filter} AND {where} "
            f"AND time > 0 AND time < 120000 ORDER BY time"
        )
        return float(statistics.median(rows)) if rows else default

    sec_pass = median_seconds("type IN (1,2) AND ease >= 2", 7.0)
    sec_lapse = median_seconds("type IN (1,2) AND ease = 1", 14.0)
    # new-card cost = median total learning time per newly-introduced card
    per_new_card_totals = col.db.list(
        f"SELECT SUM(time)/1000.0 FROM revlog WHERE {card_filter} AND type = 0 "
        f"AND time > 0 AND time < 120000 GROUP BY cid"
    )
    sec_new = (
        float(statistics.median(per_new_card_totals))
        if per_new_card_totals
        else 20.0
    )
    return CostModel(sec_new=sec_new, sec_pass=sec_pass, sec_lapse=sec_lapse)


def read_existing_cards(col, deck_id: int) -> list[Seed]:
    """One Seed per non-new, non-suspended card. FSRS state from cards.data,
    due expressed as whole days from today (clamped to 0 for overdue/learning)."""
    today = col.sched.today
    deck_ids = subdeck_ids_csv(col, deck_id)
    rows = col.db.all(
        f"""
        SELECT
            json_extract(data, '$.s'),
            json_extract(data, '$.d'),
            COALESCE(json_extract(data, '$.decay'), 0.1542),
            due, queue
        FROM cards
        WHERE did IN {deck_ids}
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
        due_offset = max(0, int(due) - today) if queue in (2, 3) else 0
        seeds.append(Seed(mass=1.0, s=float(s), d=float(d or 5.0), due=due_offset))
    return seeds


def count_new_cards(col, deck_id: int) -> int:
    deck_ids = re.sub(r"[()]", "", subdeck_ids_csv(col, deck_id))
    return len(col.find_cards(f"is:new -is:suspended did:{deck_ids}"))


def studied_today_minutes(col, deck_id: int) -> float:
    """Minutes already spent on this deck's cards today (from revlog)."""
    deck_ids = subdeck_ids_csv(col, deck_id)
    start_ms = (col.sched.day_cutoff - 86400) * 1000
    milliseconds = col.db.scalar(
        f"SELECT COALESCE(SUM(time), 0) FROM revlog "
        f"WHERE id >= {start_ms} "
        f"AND cid IN (SELECT id FROM cards WHERE did IN {deck_ids})"
    )
    return (milliseconds or 0) / 1000.0 / 60.0


# ---------------------------------------------------------------------------
# Bundled inputs for planning
# ---------------------------------------------------------------------------
@dataclass
class DeckInputs:
    """Everything the planner needs, read from the collection in one pass.

    kernel is None when the deck's preset has no FSRS parameters; in that
    case the remaining fields hold cheap placeholder values.
    """
    deck_name: str
    deck_id: int
    desired_retention: float
    params: Optional[list[float]]
    kernel: Optional[FsrsKernel]
    cost: CostModel
    existing: list[Seed]
    total_new_cards: int
    studied_minutes: float


def read_deck_inputs(col, deck_id: int) -> DeckInputs:
    deck_name = col.decks.get(deck_id)["name"]
    desired_retention, params = read_fsrs_params(col, deck_id)
    if params is None:
        return DeckInputs(
            deck_name=deck_name,
            deck_id=deck_id,
            desired_retention=desired_retention,
            params=None,
            kernel=None,
            cost=CostModel(),
            existing=[],
            total_new_cards=0,
            studied_minutes=0.0,
        )
    return DeckInputs(
        deck_name=deck_name,
        deck_id=deck_id,
        desired_retention=desired_retention,
        params=params,
        kernel=FsrsKernel(params=params, desired_retention=desired_retention),
        cost=read_cost_model(col, deck_id),
        existing=read_existing_cards(col, deck_id),
        total_new_cards=count_new_cards(col, deck_id),
        studied_minutes=studied_today_minutes(col, deck_id),
    )


# ---------------------------------------------------------------------------
# Writing the limit (same mechanism as Limit-New-by-Young)
# ---------------------------------------------------------------------------
def set_today_new_limit(col, deck_id: int, limit: int) -> None:
    """Write a today-only new-card limit. Never touches the deck's preset."""
    deck = col.decks.get(deck_id)
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
# Main entry points
# ---------------------------------------------------------------------------
def compute_deck_plan(
    col,
    deck_id: int,
    *,
    budget_minutes: float,
    today_budget_minutes: float | None = None,
    daily_new_cap: int | None = None,
    horizon: int | None = None,
    write_limit: bool = False,
    inputs: DeckInputs | None = None,
) -> DeckResult:
    """The single planning pipeline behind every UI action and hook.

    Builds two plans: one at the long-term daily budget (providing the
    completion/feasibility/peak statistics) and one at today's remaining
    budget (providing today's new-card limit). Optionally writes the limit
    to the collection.

    today_budget_minutes: one-off budget for today (None = same as
        budget_minutes). Time already studied today is subtracted before
        planning today's limit.
    daily_new_cap: hard ceiling on new cards/day (None or <= 0 = no cap).
    horizon: planning window in days (None = adaptive estimate).
    inputs: pre-read collection data, to avoid a second read when the
        caller already has it.
    """
    if inputs is None:
        inputs = read_deck_inputs(col, deck_id)
    if inputs.kernel is None:
        return fsrs_disabled_result(inputs.deck_name, deck_id)

    cap = daily_new_cap if daily_new_cap and daily_new_cap > 0 else NO_DAILY_CAP
    if horizon is None:
        horizon = adaptive_horizon(
            inputs.existing,
            inputs.total_new_cards,
            budget_minutes,
            inputs.kernel,
            inputs.cost,
        )
    if today_budget_minutes is None:
        today_budget_minutes = budget_minutes

    full_plan = make_plan(
        existing=inputs.existing,
        total_new_cards=inputs.total_new_cards,
        budget_minutes=budget_minutes,
        kernel=inputs.kernel,
        cost=inputs.cost,
        horizon=horizon,
        daily_new_cap=cap,
        first_rating=GOOD,
    )

    effective_today = max(
        MINIMUM_EFFECTIVE_BUDGET_MINUTES,
        today_budget_minutes - inputs.studied_minutes,
    )
    if abs(effective_today - budget_minutes) < 1e-9:
        today_plan = full_plan
    else:
        today_plan = make_plan(
            existing=inputs.existing,
            total_new_cards=inputs.total_new_cards,
            budget_minutes=effective_today,
            kernel=inputs.kernel,
            cost=inputs.cost,
            horizon=horizon,
            daily_new_cap=cap,
            first_rating=GOOD,
        )

    today_limit = today_plan.today()
    if write_limit:
        set_today_new_limit(col, deck_id, today_limit)

    base_peak = max(full_plan.base_seconds) / 60.0 if full_plan.base_seconds else 0.0

    return DeckResult(
        deck_name=inputs.deck_name,
        deck_id=deck_id,
        today_new_limit=today_limit,
        completion_day=full_plan.completion_day,
        feasible=full_plan.feasible,
        cards_unscheduled=full_plan.cards_unscheduled,
        peak_minutes=full_plan.peak_minutes(),
        base_peak_minutes=base_peak,
        cost=inputs.cost,
        plan=today_plan,
        studied_minutes=inputs.studied_minutes,
        horizon_days=horizon,
    )


def update_deck(
    deck_id: int,
    budget_minutes: float,
    horizon: int = 365,
    daily_new_cap: int = NO_DAILY_CAP,
    desired_retention_override: float | None = None,
    dry_run: bool = False,
    col=None,
) -> DeckResult:
    """Compute (and optionally write) today's new-card limit for one deck.

    Unlike compute_deck_plan, this plans at the full given budget without
    subtracting time already studied today — the stable, scriptable API.

    dry_run=True: reads everything, computes the plan, but skips the write.
    col=None: falls back to aqt.mw.col (for use from hooks/menu actions).
    """
    col = _get_col(col)
    inputs = read_deck_inputs(col, deck_id)
    if inputs.kernel is None:
        return fsrs_disabled_result(inputs.deck_name, deck_id)
    if desired_retention_override is not None:
        inputs.kernel = FsrsKernel(
            params=inputs.params, desired_retention=desired_retention_override
        )
    inputs.studied_minutes = 0.0
    return compute_deck_plan(
        col,
        deck_id,
        budget_minutes=budget_minutes,
        daily_new_cap=daily_new_cap,
        horizon=horizon,
        write_limit=not dry_run,
        inputs=inputs,
    )
