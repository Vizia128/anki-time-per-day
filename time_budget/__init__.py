"""
time_budget — set a daily study-time budget instead of a fixed new-cards/day.

Anki add-on entry point. The actual code lives in:
    scheduler.py  — FSRS-6 kernel + planning math (no Anki imports)
    adapter.py    — reads from / writes to a live Anki collection
    ui.py         — the Time Budget dialog (Tools → Time Budget)
    hooks.py      — auto-apply on profile open / after sync
    constants.py  — shared constants (add-on package name, defaults)
"""

from __future__ import annotations

import os


def init() -> None:
    import aqt
    from aqt.qt import QAction, QMenu, qconnect

    from .hooks import register_hooks
    from .ui import show_dialog

    register_hooks()

    menu = QMenu("Time Budget", aqt.mw)
    aqt.mw.form.menuTools.addMenu(menu)
    open_action = QAction("Open…", menu)
    menu.addAction(open_action)
    qconnect(open_action.triggered, show_dialog)


# The TEST guard lets the test-suite import scheduler.py/adapter.py without
# a running Anki GUI (aqt is unavailable or headless under pytest).
if not os.environ.get("TEST"):
    init()
