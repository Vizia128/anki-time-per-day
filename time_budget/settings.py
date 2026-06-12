"""
settings.py
===========
Typed, validated view of the user-editable add-on config.

The config is editable as raw JSON in Anki's add-on manager, so nothing in it
can be trusted: every field must tolerate missing keys, wrong types, and
out-of-range values. All helpers here are pure (no Anki imports) so they can
be unit-tested directly.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional

from .constants import DEFAULT_BUDGET_MINUTES, DEFAULT_HORIZON_DAYS

MINUTES_PER_DAY = 24.0 * 60.0


@dataclass
class DeckSettings:
    """One config entry's settings, coerced to safe types and ranges."""
    budget_minutes: float = DEFAULT_BUDGET_MINUTES
    horizon_days: int = DEFAULT_HORIZON_DAYS
    daily_new_cap: int = 0          # 0 = no cap
    desired_retention_override: Optional[float] = None
    active: bool = False


def _coerce_float(value, default: float, minimum: float, maximum: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(result) or math.isinf(result):
        return default
    return min(max(result, minimum), maximum)


def _coerce_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(result, minimum), maximum)


def parse_deck_settings(entry: dict) -> DeckSettings:
    """Coerce one raw config entry into a DeckSettings, applying defaults
    and clamping out-of-range values. Never raises."""
    if not isinstance(entry, dict):
        return DeckSettings()
    retention = entry.get("desiredRetentionOverride")
    if retention is not None:
        retention = _coerce_float(retention, 0.9, 0.5, 0.995)
    return DeckSettings(
        budget_minutes=_coerce_float(
            entry.get("budgetMinutes"),
            DEFAULT_BUDGET_MINUTES, 0.5, MINUTES_PER_DAY,
        ),
        horizon_days=_coerce_int(
            entry.get("horizonDays"), DEFAULT_HORIZON_DAYS, 1, 3650
        ),
        daily_new_cap=_coerce_int(entry.get("dailyNewCap") or 0, 0, 0, 9999),
        desired_retention_override=retention,
        active=bool(entry.get("active", False)),
    )


def _deck_entries(config: dict) -> list:
    entries = config.get("decks")
    return entries if isinstance(entries, list) else []


def match_deck_entry(config: dict, deck_name: str) -> dict | None:
    """Return the first config entry whose deckNames matches deck_name.

    deckNames can be a regex string (re.fullmatch) or a list of exact names.
    Invalid regex patterns are skipped (see invalid_deck_patterns).
    """
    for entry in _deck_entries(config):
        if not isinstance(entry, dict):
            continue
        pattern = entry.get("deckNames", "")
        if isinstance(pattern, str):
            try:
                if re.fullmatch(pattern, deck_name):
                    return entry
            except re.error:
                continue
        elif isinstance(pattern, list):
            if deck_name in pattern:
                return entry
    return None


def invalid_deck_patterns(config: dict) -> list[str]:
    """Regex deckNames patterns that fail to compile (so they never match)."""
    invalid: list[str] = []
    for entry in _deck_entries(config):
        if not isinstance(entry, dict):
            continue
        pattern = entry.get("deckNames", "")
        if isinstance(pattern, str):
            try:
                re.compile(pattern)
            except re.error:
                invalid.append(pattern)
    return invalid


def today_override_minutes(
    config: dict, deck_name: str, day_cutoff: int
) -> float | None:
    """The deck's one-off budget for today, if one is stored and still
    valid for the current Anki day. Malformed entries return None."""
    overrides = config.get("todayOverrides")
    if not isinstance(overrides, dict):
        return None
    entry = overrides.get(deck_name)
    if not isinstance(entry, dict) or entry.get("dayCutoff") != day_cutoff:
        return None
    try:
        return float(entry["budgetMinutes"])
    except (KeyError, TypeError, ValueError):
        return None


def prune_stale_overrides(config: dict, day_cutoff: int) -> bool:
    """Drop today-overrides from previous days (and malformed ones) so the
    config doesn't grow forever. Returns True if anything was removed."""
    overrides = config.get("todayOverrides")
    if not isinstance(overrides, dict):
        return False
    stale = [
        deck_name
        for deck_name, entry in overrides.items()
        if not isinstance(entry, dict) or entry.get("dayCutoff") != day_cutoff
    ]
    for deck_name in stale:
        del overrides[deck_name]
    return bool(stale)
