# AnkiWeb listing

Copy-paste material for https://ankiweb.net/shared/addons (Upload Add-on).
Update this file whenever the listing changes so the repo stays the source of truth.

## Title

> Time Budget: study minutes per day, not new cards per day

(58 characters)

## Tags

> time budget fsrs scheduling new-cards limit workload pacing study-time

## Description

---

This add-on lets you set a daily study goal in minutes instead of a fixed number of new cards per day. Each day it adjusts the deck's new-card limit so that your predicted study time (new cards plus reviews) stays at your goal.

The problem with a fixed new-card count is that it doesn't correspond to a fixed amount of time. Reviews accumulate, so 20 new cards a day might cost 10 minutes in the first week and 45 minutes a few months later. If what you can actually commit to is "30 minutes a day", this add-on keeps you there: it introduces new cards quickly while the deck is light and slows down as your review load grows.

**How it works.** The add-on uses your FSRS parameters and your review history (how long you actually spend per card) to forecast future review load. It then picks the largest new-card limit that doesn't push today — or any future day — past your budget.

**Usage.** Tools → Time Budget → Open…, pick a deck, set a budget in minutes, press Save. Instead of a budget you can also enter a target in the "Finish in" field and the required minutes/day is computed for you. The dialog shows a forecast before you save: today's new-card limit, time already studied today, and the busiest predicted day. If you enable "Apply automatically", the limit is also refreshed when Anki starts and after each sync, so you don't need to open the dialog again.

**What it changes.** Only the deck's "today only" new-card limit — the same thing you can set by hand in deck options. It doesn't reschedule reviews, modify cards, or touch your presets, and nothing is written until you press Save.

**Requirements.**

- Anki 25.02 or newer
- FSRS enabled on the decks you want to manage (decks on the old SM-2 scheduler are skipped with a warning)

One caveat: configure either a parent deck or its subdecks, not both. Two rules matching overlapping decks will each write their own limit.

Source code and full documentation: https://github.com/Vizia128/anki-time-per-day

---
