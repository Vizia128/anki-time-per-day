"""
hooks.py
========
Background auto-apply: writes today's new-card limit for every deck whose
config entry has active=true, on profile open and after each sync.

Always plans from the last-saved config — never from live dialog values.
"""

from __future__ import annotations

import aqt
from aqt import gui_hooks
from aqt.operations import QueryOp
from aqt.utils import tooltip

from . import adapter
from .adapter import DeckResult
from .constants import ADDON_PACKAGE, DEFAULT_BUDGET_MINUTES


def apply_all_active_decks(col, *, force: bool = False) -> list[DeckResult]:
    """Apply saved budgets to every config-matched deck.

    force=True also processes decks whose entry has active=false.
    """
    config = aqt.mw.addonManager.getConfig(ADDON_PACKAGE) or {}
    day_cutoff = col.sched.day_cutoff
    overrides = config.get("todayOverrides", {})
    results: list[DeckResult] = []
    for deck in col.decks.all_names_and_ids(include_filtered=False):
        entry = adapter.match_deck_configs(config, deck.name)
        if entry is None:
            continue
        if not entry.get("active", False) and not force:
            continue
        deck_id = int(deck.id)
        try:
            budget = float(entry.get("budgetMinutes", DEFAULT_BUDGET_MINUTES))
            daily_cap = int(entry.get("dailyNewCap") or 0)
            override = overrides.get(deck.name)
            today_budget = (
                float(override["budgetMinutes"])
                if override and override.get("dayCutoff") == day_cutoff
                else budget
            )
            result = adapter.compute_deck_plan(
                col,
                deck_id,
                budget_minutes=budget,
                today_budget_minutes=today_budget,
                daily_new_cap=daily_cap,
                write_limit=True,
            )
        except Exception as exc:
            result = adapter.error_result(deck.name, deck_id, str(exc))
        results.append(result)
    return results


def _report_results(results: list[DeckResult]) -> None:
    for result in results:
        if result.error:
            tooltip(f"Time Budget — {result.deck_name}: {result.error}")
        elif result.fsrs_disabled:
            tooltip(f"Time Budget — {result.deck_name}: FSRS not enabled, skipped.")
        elif not result.feasible:
            tooltip(
                f"Time Budget — {result.deck_name}: "
                f"{result.cards_unscheduled} cards won't fit in horizon."
            )
    if aqt.mw.state in ("deckBrowser", "overview"):
        aqt.mw.reset()


def _apply_in_background() -> None:
    QueryOp(
        parent=aqt.mw,
        op=lambda col: apply_all_active_decks(col),
        success=_report_results,
    ).run_in_background()


def register_hooks() -> None:
    gui_hooks.profile_did_open.append(_apply_in_background)
    gui_hooks.sync_did_finish.append(_apply_in_background)
