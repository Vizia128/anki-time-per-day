# Time Budget Scheduler — Configuration

## `decks`

A list of per-deck rules. Each card deck is matched against the rules in order;
the **first** match wins.

### `deckNames` *(string or list of strings, required)*

- **String**: treated as a Python `re.fullmatch` regex pattern applied to the
  deck's full name (e.g. `".*Korean.*"` matches any deck containing "Korean").
- **List of strings**: matched by exact equality (e.g. `["My Deck", "Another Deck"]`).

The default `".*"` matches every deck — replace it with a specific pattern.

### `budgetMinutes` *(number, required)*

Daily study-time budget in minutes. The add-on will introduce as many new cards
as possible each day while keeping predicted study time at or under this value.

### `horizonDays` *(integer, default 365)*

Planning window in days. New cards that cannot fit within this window are reported
as unscheduled. Increasing this value lets the scheduler spread new cards over a
longer period.

### `dailyNewCap` *(integer or null, default null)*

Hard ceiling on new cards per day, regardless of budget headroom. Useful for
avoiding extreme front-loading at the start of a deck. `null` means no cap.

### `desiredRetentionOverride` *(number or null, default null)*

Override the desired retention used for planning. When `null` (default), the
value from the deck's FSRS preset is used. Set to e.g. `0.85` to plan for a
lower retention target without changing the preset.

### `active` *(boolean, default false)*

When `false` (the default), the add-on only writes the new-card limit when you
press **Save** in the Time Budget dialog.

When `true`, the limit is also written automatically when Anki opens and after
each sync.

## Example

```json
{
  "decks": [
    {
      "deckNames": "Japanese.*",
      "budgetMinutes": 20,
      "horizonDays": 365,
      "dailyNewCap": 15,
      "desiredRetentionOverride": null,
      "active": true
    },
    {
      "deckNames": ".*",
      "budgetMinutes": 30,
      "horizonDays": 365,
      "dailyNewCap": null,
      "desiredRetentionOverride": null,
      "active": false
    }
  ]
}
```

## Infeasibility

If your new-card backlog cannot fit within `horizonDays` at the given budget,
the add-on will warn you. The three levers to fix this are:

1. **Increase `budgetMinutes`** — study more per day.
2. **Increase `horizonDays`** — allow more time to introduce all cards.
3. **Reduce the deck size** — suspend cards you don't need now.
