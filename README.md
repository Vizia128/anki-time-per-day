# Time Budget Scheduler

An Anki add-on that replaces the fixed "new cards per day" setting with a **daily study-time budget**. You decide how many minutes per day you want to study; the add-on automatically computes how many new cards to introduce so your predicted study time stays within that budget.

## How it works

Anki's FSRS scheduler tracks each card's memory state. This add-on uses that data to simulate your future review load and find the new-card introduction rate that keeps your daily study time at or below your budget — without ever overloading a future day.

The core algorithm is a **greedy receding-horizon controller** built on FSRS-6:

1. For each new card, compute the expected "review tail" — how many seconds that card will consume on each future day as it matures from learning to mature.
2. Simulate the baseline load from your existing cards over the planning horizon.
3. On each day, introduce as many new cards as possible without pushing any current or future day over the budget (convolution-based greedy MPC).

This gives you a sustainable pace that uses every minute of your budget without burning yourself out.

## Requirements

- **Anki 25.02 or newer** (the add-on reads FSRS-6 parameters, which older versions don't store).
- **FSRS enabled** for the decks you want to manage (Deck Options → FSRS). Decks still on the older SM-2 scheduler are skipped with a warning.

## Installation

### From release

Download the latest `time_budget.ankiaddon` from the releases page and double-click to install, or go to **Tools → Add-ons → Install from file**.

### From source

```bash
git clone https://github.com/julianzia/anki-time-per-day
cd anki-time-per-day
python build.py          # produces time_budget.ankiaddon
```

Then install the `.ankiaddon` file in Anki.

## Usage

Open **Tools → Time Budget → Open…** to configure a deck.

### Settings

| Setting | Description |
|---|---|
| **Daily budget** | Minutes per day you want to spend studying (new + review). |
| **Finish in** | Target days to finish all new cards. Edit either field; the other updates automatically via binary search. |
| **Today's budget** | One-off override for today only; takes effect when you press Save. The long-term daily budget is unchanged. |
| **Daily cap** | Hard ceiling on new cards per day regardless of budget headroom. Useful if you want to pace introduction even when you have spare time. |
| **Apply automatically** | When enabled, the add-on also sets your new-card limit when Anki opens and after each sync, using the saved settings. |

### Buttons

- **Save** — Save the settings and set today's new-card limit. The dialog stays open.
- **Cancel** — Close without saving or changing anything. If you have unsaved changes, you'll be asked whether to save them first.

Nothing is written to your collection until you press Save (or, for decks with "Apply automatically" enabled, until Anki starts or finishes a sync). The forecast panel is always a preview.

### Forecast panel

The forecast shows:
- **Today's new-card limit** — Cards the add-on would allow today.
- **Already studied today** — Minutes logged in your revlog for this deck today, and how much budget remains.
- **Peak load** — The busiest day in the long-term plan (should stay near your budget).
- **Base load** — Predicted load from cards already in your collection (no new cards).
- **Cost model** — Median time per new card, pass, and lapse derived from your actual review history.

## Configuration

Settings are stored in Anki's add-on config system (`config.json` / the Add-ons manager). Each entry in the `decks` array matches one or more decks by name.

```json
{
  "decks": [
    {
      "deckNames": ["My Deck"],
      "budgetMinutes": 30,
      "horizonDays": 365,
      "dailyNewCap": null,
      "active": false
    }
  ]
}
```

| Key | Type | Default | Description |
|---|---|---|---|
| `deckNames` | string or list | required | Deck name(s) to match. A bare string is treated as a regex (`re.fullmatch`). |
| `budgetMinutes` | float | `30` | Daily study-time budget in minutes. |
| `horizonDays` | int | `365` | Planning horizon. The add-on also auto-estimates a minimum horizon based on deck size. |
| `dailyNewCap` | int or null | `null` | Maximum new cards per day. `null` means no cap. |
| `active` | bool | `false` | Automatically apply limits when Anki opens and after sync. Pressing Save in the dialog always applies immediately, regardless of this setting. |

## Development

### Requirements

```bash
pip install anki pytest
```

### Running tests

```bash
pytest -v
```

Tests are split into two groups:
- **Pure logic** (`tests/test_kernel.py`, `tests/test_controller.py`) — no Anki dependency, fast.
- **Integration** (`tests/test_fake_collection.py`) — uses a headless in-memory Anki collection.

### Building

```bash
python build.py
```

Produces `time_budget.ankiaddon` in the project root. The script zips the `time_budget/` package flat (files at zip root, no parent directory prefix), which is required by Anki's add-on installer.

### Project layout

```
time_budget/
  __init__.py     # Entry point: Tools menu + hook registration
  scheduler.py    # FSRS-6 kernel + MPC controller (no Anki imports)
  adapter.py      # Anki collection interface + planning pipeline
  ui.py           # The Time Budget dialog
  hooks.py        # Auto-apply on profile open / after sync
  constants.py    # Shared constants
  config.json     # Default config
  manifest.json   # Add-on metadata

tests/
  conftest.py
  fake_collection_builder.py
  test_kernel.py
  test_controller.py
  test_fake_collection.py

build.py          # Packaging script
```

## License

MIT
