"""
constants.py
============
Constants shared across the add-on's modules.
"""

# The add-on's package/folder name, resolved at runtime. When installed from
# AnkiWeb the folder is a numeric ID, so this must never be hardcoded.
ADDON_PACKAGE = __name__.split(".")[0]

# Fallback daily budget (minutes) when a config entry omits budgetMinutes.
DEFAULT_BUDGET_MINUTES = 30.0

# Fallback planning horizon (days) when a config entry omits horizonDays.
DEFAULT_HORIZON_DAYS = 365
