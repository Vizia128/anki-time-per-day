"""
test_settings.py
================
Pure-Python tests for the config validation layer. The config is editable as
raw JSON in Anki's add-on manager, so these focus on hostile input: wrong
types, missing keys, out-of-range values, broken regex patterns.
"""

from __future__ import annotations

from time_budget.constants import DEFAULT_BUDGET_MINUTES, DEFAULT_HORIZON_DAYS
from time_budget.settings import (
    invalid_deck_patterns,
    match_deck_entry,
    parse_deck_settings,
    prune_stale_overrides,
    today_override_minutes,
)


# ---------------------------------------------------------------------------
# parse_deck_settings
# ---------------------------------------------------------------------------
def test_parse_empty_entry_gives_defaults():
    parsed = parse_deck_settings({})
    assert parsed.budget_minutes == DEFAULT_BUDGET_MINUTES
    assert parsed.horizon_days == DEFAULT_HORIZON_DAYS
    assert parsed.daily_new_cap == 0
    assert parsed.desired_retention_override is None
    assert parsed.active is False


def test_parse_non_dict_entry_gives_defaults():
    assert parse_deck_settings(None).budget_minutes == DEFAULT_BUDGET_MINUTES
    assert parse_deck_settings("garbage").active is False


def test_parse_garbage_values_fall_back_to_defaults():
    parsed = parse_deck_settings(
        {
            "budgetMinutes": "thirty",
            "horizonDays": [],
            "dailyNewCap": "lots",
            "desiredRetentionOverride": "high",
            "active": 1,
        }
    )
    assert parsed.budget_minutes == DEFAULT_BUDGET_MINUTES
    assert parsed.horizon_days == DEFAULT_HORIZON_DAYS
    assert parsed.daily_new_cap == 0
    # garbage override coerces to the safe default retention, clamped range
    assert parsed.desired_retention_override == 0.9
    assert parsed.active is True


def test_parse_clamps_out_of_range_values():
    parsed = parse_deck_settings(
        {
            "budgetMinutes": -5,
            "horizonDays": 99999,
            "dailyNewCap": -3,
            "desiredRetentionOverride": 1.5,
        }
    )
    assert parsed.budget_minutes == 0.5
    assert parsed.horizon_days == 3650
    assert parsed.daily_new_cap == 0
    assert parsed.desired_retention_override == 0.995


def test_parse_valid_entry_passes_through():
    parsed = parse_deck_settings(
        {
            "budgetMinutes": 20,
            "horizonDays": 180,
            "dailyNewCap": 15,
            "desiredRetentionOverride": 0.85,
            "active": True,
        }
    )
    assert parsed.budget_minutes == 20.0
    assert parsed.horizon_days == 180
    assert parsed.daily_new_cap == 15
    assert parsed.desired_retention_override == 0.85
    assert parsed.active is True


def test_parse_null_cap_means_no_cap():
    assert parse_deck_settings({"dailyNewCap": None}).daily_new_cap == 0


# ---------------------------------------------------------------------------
# match_deck_entry
# ---------------------------------------------------------------------------
def test_match_regex_is_fullmatch():
    config = {"decks": [{"deckNames": "Korean.*", "budgetMinutes": 10}]}
    assert match_deck_entry(config, "Korean Verbs") is not None
    assert match_deck_entry(config, "My Korean Verbs") is None  # not a prefix match


def test_match_list_is_exact():
    config = {"decks": [{"deckNames": ["My Deck"], "budgetMinutes": 10}]}
    assert match_deck_entry(config, "My Deck") is not None
    assert match_deck_entry(config, "My Deck 2") is None


def test_match_first_entry_wins():
    config = {
        "decks": [
            {"deckNames": ["My Deck"], "budgetMinutes": 10},
            {"deckNames": ".*", "budgetMinutes": 99},
        ]
    }
    assert match_deck_entry(config, "My Deck")["budgetMinutes"] == 10
    assert match_deck_entry(config, "Other")["budgetMinutes"] == 99


def test_match_skips_invalid_regex_and_non_dict_entries():
    config = {
        "decks": [
            "garbage",
            {"deckNames": "[unclosed", "budgetMinutes": 1},
            {"deckNames": ".*", "budgetMinutes": 2},
        ]
    }
    assert match_deck_entry(config, "Any Deck")["budgetMinutes"] == 2


def test_match_tolerates_missing_or_non_list_decks():
    assert match_deck_entry({}, "Deck") is None
    assert match_deck_entry({"decks": "oops"}, "Deck") is None


def test_invalid_deck_patterns_reports_broken_regex():
    config = {
        "decks": [
            {"deckNames": "[unclosed"},
            {"deckNames": ".*"},
            {"deckNames": ["literal", "names"]},
        ]
    }
    assert invalid_deck_patterns(config) == ["[unclosed"]


# ---------------------------------------------------------------------------
# today overrides
# ---------------------------------------------------------------------------
CUTOFF = 1_750_000_000


def test_today_override_valid():
    config = {
        "todayOverrides": {"My Deck": {"budgetMinutes": 12.5, "dayCutoff": CUTOFF}}
    }
    assert today_override_minutes(config, "My Deck", CUTOFF) == 12.5


def test_today_override_stale_or_missing_or_garbage():
    config = {
        "todayOverrides": {
            "Stale": {"budgetMinutes": 12.5, "dayCutoff": CUTOFF - 86400},
            "Garbage": "not a dict",
            "BadValue": {"budgetMinutes": "ten", "dayCutoff": CUTOFF},
        }
    }
    assert today_override_minutes(config, "Stale", CUTOFF) is None
    assert today_override_minutes(config, "Garbage", CUTOFF) is None
    assert today_override_minutes(config, "BadValue", CUTOFF) is None
    assert today_override_minutes(config, "Missing", CUTOFF) is None
    assert today_override_minutes({}, "Any", CUTOFF) is None


def test_prune_stale_overrides():
    config = {
        "todayOverrides": {
            "Fresh": {"budgetMinutes": 10, "dayCutoff": CUTOFF},
            "Stale": {"budgetMinutes": 10, "dayCutoff": CUTOFF - 86400},
            "Garbage": 42,
        }
    }
    changed = prune_stale_overrides(config, CUTOFF)
    assert changed is True
    assert list(config["todayOverrides"]) == ["Fresh"]
    # second prune is a no-op
    assert prune_stale_overrides(config, CUTOFF) is False
    assert prune_stale_overrides({}, CUTOFF) is False
