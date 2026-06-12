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

from . import adapter, settings
from .adapter import DeckResult
from .constants import ADDON_PACKAGE

# How long the aggregated problem report stays on screen (ms).
REPORT_TOOLTIP_MS = 6000


def apply_all_active_decks(col, *, force: bool = False) -> list[DeckResult]:
    """Apply saved budgets to every config-matched deck.

    force=True also processes decks whose entry has active=false.
    """
    config = aqt.mw.addonManager.getConfig(ADDON_PACKAGE) or {}
    day_cutoff = col.sched.day_cutoff
    results: list[DeckResult] = []
    for deck in col.decks.all_names_and_ids(include_filtered=False):
        entry = settings.match_deck_entry(config, deck.name)
        if entry is None:
            continue
        deck_settings = settings.parse_deck_settings(entry)
        if not deck_settings.active and not force:
            continue
        deck_id = int(deck.id)
        try:
            override = settings.today_override_minutes(
                config, deck.name, day_cutoff
            )
            today_budget = (
                override if override is not None else deck_settings.budget_minutes
            )
            result = adapter.compute_deck_plan(
                col,
                deck_id,
                budget_minutes=deck_settings.budget_minutes,
                today_budget_minutes=today_budget,
                daily_new_cap=deck_settings.daily_new_cap,
                desired_retention_override=(
                    deck_settings.desired_retention_override
                ),
                write_limit=True,
            )
        except Exception as exc:
            result = adapter.error_result(deck.name, deck_id, str(exc))
        results.append(result)
    return results


def _report_results(results: list[DeckResult]) -> None:
    """One aggregated tooltip for everything that needs the user's attention
    (per-deck tooltips would overwrite each other)."""
    problems: list[str] = []
    for result in results:
        if result.error:
            problems.append(f"{result.deck_name}: error — {result.error}")
        elif result.fsrs_disabled:
            problems.append(
                f"{result.deck_name}: FSRS not enabled, skipped."
            )
        elif not result.feasible:
            problems.append(
                f"{result.deck_name}: {result.cards_unscheduled} new cards "
                f"don't fit at the saved budget."
            )
    config = aqt.mw.addonManager.getConfig(ADDON_PACKAGE) or {}
    for pattern in settings.invalid_deck_patterns(config):
        problems.append(f"invalid deck pattern in config: {pattern!r}")
    if problems:
        tooltip(
            "Time Budget:<br>" + "<br>".join(problems),
            period=REPORT_TOOLTIP_MS,
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
