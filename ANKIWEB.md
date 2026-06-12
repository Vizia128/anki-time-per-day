# AnkiWeb listing

Copy-paste material for https://ankiweb.net/shared/addons (Upload Add-on).
Update this file whenever the listing changes so the repo stays the source of truth.

## Title

> Time Budget: study minutes per day, not new cards per day

(58 characters)

Alternatives considered:

- `Time Budget — set daily study time; new cards adjust automatically (FSRS)` (74)
- `Study Time Budget: auto-adjust new cards to hit your minutes/day goal` (70)

## Tags

> time budget fsrs scheduling new-cards limit workload pacing study-time

## Description

---

**Set your study goal in minutes per day. The add-on figures out how many new cards you can afford.**

"30 minutes a day" is how people actually plan their lives. "20 new cards a day" is not — and it quietly compounds: a fixed new-card rate costs almost nothing in week one, then reviews pile up until your daily load has doubled or tripled. This add-on turns the dial around: **you fix the time, and the new-card count adapts.**

## What it does

- You set a **daily study-time budget** per deck (e.g. 30 min/day, new cards + reviews combined).
- The add-on simulates your future review load using **FSRS-6** and your **real review speed** measured from your own history.
- Each day it sets the deck's new-card limit as high as possible **without pushing any current or future day over budget** — so the pace is sustainable from day one, not just this week.
- Or work backwards: enter when you want to **finish the deck** ("90 days") and it computes the required minutes/day.
- **Today's budget** override for one-off busy or free days.
- Optional **auto-apply** when Anki opens and after each sync.

A forecast panel previews everything before you commit: today's new-card limit, time already studied today, the busiest day in the plan, and your measured seconds per card. **Nothing is written until you press Save.**

## Safe by design

The add-on only sets the deck's **today-only new-card limit** — the same field as "New cards today" in the deck options. It never reschedules reviews, never edits your cards, and never changes your deck presets.

## Requirements

- **Anki 25.02 or newer.**
- **FSRS enabled** on the decks you manage (Deck Options → FSRS). Decks still on SM-2 are skipped with a warning.

## Usage

**Tools → Time Budget → Open…** — pick a deck, set a budget, press Save.

Tip: configure either a parent deck *or* its subdecks, not both — two rules writing limits for overlapping decks will fight each other.

## Bugs & contributions

Source code, issue tracker, and full documentation:
**https://github.com/Vizia128/anki-time-per-day**

Please report problems on GitHub rather than in reviews — I can't reply to reviews here. Include your Anki version and the message from the warning bar if there is one.

MIT licensed.

---
