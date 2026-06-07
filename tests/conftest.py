import os

# Prevent time_budget/__init__.py from calling init() (which needs aqt) during tests.
os.environ.setdefault("TEST", "1")
