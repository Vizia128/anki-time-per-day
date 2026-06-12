"""
ui.py
=====
The Time Budget dialog (Tools → Time Budget → Open…).

Layout:
  • Deck dropdown (switching decks prompts if there are unsaved changes)
  • Settings card:
      – Daily budget  ↔  Finish in   (bidirectional: edit either one)
      – Today's budget override
      – Daily cap
      – Active (auto-apply on profile open / after sync)
  • Forecast card: live, debounced re-plan as settings change
  • Save (saves config and applies today's limit) | Cancel

Closing the dialog re-applies the last-saved settings for the selected deck.
Only one dialog instance is shown at a time (see show_dialog).
"""

from __future__ import annotations

from contextlib import contextmanager

import aqt
from aqt.operations import QueryOp
from aqt.qt import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    Qt,
    QTimer,
    QVBoxLayout,
    QWidget,
    qconnect,
)
from aqt.utils import tooltip

from . import adapter
from .adapter import DeckResult
from .constants import ADDON_PACKAGE, DEFAULT_BUDGET_MINUTES, DEFAULT_HORIZON_DAYS
from .scheduler import NO_DAILY_CAP, find_budget_for_target

# Forecasts re-run this long after the last keystroke/spin.
FORECAST_DEBOUNCE_MS = 400
# Today's override writes the limit this long after the last change.
APPLY_TODAY_DEBOUNCE_MS = 600


class TimeBudgetDialog(QDialog):
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.setWindowTitle("Time Budget")
        self.setMinimumWidth(520)

        col = aqt.mw.col
        all_decks = sorted(
            col.decks.all_names_and_ids(include_filtered=False),
            key=lambda deck: deck.name,
        )
        self._deck_names = [deck.name for deck in all_decks]
        self._deck_ids = {deck.name: int(deck.id) for deck in all_decks}
        self._config: dict = aqt.mw.addonManager.getConfig(ADDON_PACKAGE) or {
            "decks": []
        }

        # Monotonic counter; stale forecast results are discarded.
        self._forecast_generation = 0
        # True when the widgets differ from the saved config.
        self._dirty = False
        # Suppresses signal re-entry during programmatic widget updates.
        self._updating = False
        # Skips the dirty check when we initiate the close ourselves.
        self._force_close = False
        self._previous_deck_name = self._deck_names[0] if self._deck_names else ""

        self._build_ui()
        self._connect_signals()

        if self._deck_names:
            self._load_deck(self._deck_names[0])
            self._run_forward_forecast()
        self.resize(520, 580)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.deck_selector = QComboBox()
        self.deck_selector.addItems(self._deck_names)
        deck_row = QHBoxLayout()
        deck_row.addWidget(QLabel("Deck:"))
        deck_row.addWidget(self.deck_selector, 1)

        # ── Settings card ─────────────────────────────────────────────
        self.budget_spinbox = QDoubleSpinBox()
        self.budget_spinbox.setRange(0.5, 9999.0)
        self.budget_spinbox.setDecimals(1)
        self.budget_spinbox.setSuffix(" min/day")
        self.budget_spinbox.setToolTip(
            "Daily study-time budget (new + review). "
            "Or edit 'Finish in' to compute this automatically."
        )

        self.finish_spinbox = QSpinBox()
        self.finish_spinbox.setRange(1, 9999)
        self.finish_spinbox.setSuffix(" days")
        self.finish_spinbox.setToolTip(
            "Target days to finish the deck. "
            "Edit to compute the required daily budget."
        )

        link_label = QLabel("↑ edit either one; the other updates automatically")
        link_label.setStyleSheet(
            "color: gray; font-size: 10px; background: transparent;"
        )

        self.today_same_checkbox = QCheckBox("Same as daily budget")
        self.today_same_checkbox.setChecked(True)
        self.today_spinbox = QDoubleSpinBox()
        self.today_spinbox.setRange(0.5, 9999.0)
        self.today_spinbox.setDecimals(1)
        self.today_spinbox.setSuffix(" min")
        self.today_spinbox.setEnabled(False)
        self.today_spinbox.setToolTip(
            "One-off budget for today only. "
            "The long-term daily budget is unchanged."
        )

        today_row = QHBoxLayout()
        today_row.setContentsMargins(0, 0, 0, 0)
        today_row.addWidget(self.today_same_checkbox)
        today_row.addWidget(self.today_spinbox, 1)
        today_widget = QWidget()
        today_widget.setStyleSheet("background: transparent;")
        today_widget.setLayout(today_row)

        self.cap_spinbox = QSpinBox()
        self.cap_spinbox.setRange(0, 9999)
        self.cap_spinbox.setSpecialValueText("no cap")
        self.cap_spinbox.setToolTip(
            "Hard ceiling on new cards per day regardless of budget headroom. "
            "0 = no ceiling."
        )

        self.active_checkbox = QCheckBox()
        self.active_checkbox.setToolTip(
            "Write the new-card limit automatically when Anki opens or after "
            "sync. Saving or closing this dialog always applies the limit."
        )

        settings_card, settings_form, _ = self._make_section_card("Settings")
        settings_form.addRow("Daily budget:", self.budget_spinbox)
        settings_form.addRow("Finish in:", self.finish_spinbox)
        settings_form.addRow("", link_label)
        settings_form.addRow("Today's budget:", today_widget)
        settings_form.addRow("Daily cap:", self.cap_spinbox)
        settings_form.addRow("Active:", self.active_checkbox)

        # ── Forecast card ─────────────────────────────────────────────
        self.limit_label = self._make_readonly_label()
        self.studied_label = self._make_readonly_label()
        self.peak_label = self._make_readonly_label()
        self.base_load_label = self._make_readonly_label()
        self.cost_label = self._make_readonly_label()
        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet(
            "color: #e8a020; font-weight: bold; background: transparent;"
        )

        forecast_card, forecast_form, forecast_outer = self._make_section_card(
            "Forecast"
        )
        forecast_form.addRow("Today's new-card limit:", self.limit_label)
        forecast_form.addRow(
            "Already studied today (this deck):", self.studied_label
        )
        forecast_form.addRow("Peak load:", self.peak_label)
        forecast_form.addRow("Base load (existing):", self.base_load_label)
        forecast_form.addRow("Cost model:", self.cost_label)
        forecast_outer.addWidget(self.warning_label)

        # ── Bottom row ────────────────────────────────────────────────
        self.save_button = QPushButton("Save")
        self.save_button.setDefault(True)
        self.save_button.setToolTip(
            "Save settings and apply today's new-card limit"
        )
        self.cancel_button = QPushButton("Cancel")

        bottom_row = QHBoxLayout()
        bottom_row.addStretch()
        bottom_row.addWidget(self.save_button)
        bottom_row.addWidget(self.cancel_button)

        # ── Assemble ──────────────────────────────────────────────────
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(16, 12, 16, 12)
        main_layout.setSpacing(12)
        main_layout.addLayout(deck_row)
        main_layout.addWidget(settings_card)
        main_layout.addWidget(forecast_card)
        main_layout.addLayout(bottom_row)
        self.setLayout(main_layout)

        # ── Debounce timers ───────────────────────────────────────────
        self._forward_forecast_timer = QTimer(self)
        self._forward_forecast_timer.setSingleShot(True)
        self._forward_forecast_timer.setInterval(FORECAST_DEBOUNCE_MS)

        self._reverse_forecast_timer = QTimer(self)
        self._reverse_forecast_timer.setSingleShot(True)
        self._reverse_forecast_timer.setInterval(FORECAST_DEBOUNCE_MS)

        self._apply_today_timer = QTimer(self)
        self._apply_today_timer.setSingleShot(True)
        self._apply_today_timer.setInterval(APPLY_TODAY_DEBOUNCE_MS)

    @staticmethod
    def _make_section_card(title: str):
        """A rounded framed section with a heading, styled after Anki's
        native settings screens. Returns (card, form_layout, outer_layout)."""
        card = QFrame()
        card.setStyleSheet(
            "QFrame { border-radius: 8px; background: palette(base);"
            " border: 1px solid palette(mid); }"
        )
        heading = QLabel(title)
        heading.setStyleSheet(
            "font-size: 15px; font-weight: bold; background: transparent;"
            " border: none;"
        )
        form = QFormLayout()
        form.setContentsMargins(0, 10, 0, 0)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        outer = QVBoxLayout(card)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(0)
        outer.addWidget(heading)
        outer.addLayout(form)
        return card, form, outer

    @staticmethod
    def _make_readonly_label() -> QLabel:
        label = QLabel("—")
        label.setStyleSheet("background: transparent;")
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        return label

    def _connect_signals(self) -> None:
        qconnect(self.deck_selector.currentIndexChanged, self._on_deck_changed)
        qconnect(self.budget_spinbox.valueChanged, self._on_budget_changed)
        qconnect(self.finish_spinbox.valueChanged, self._on_finish_changed)
        qconnect(self.cap_spinbox.valueChanged, self._on_setting_changed)
        qconnect(self.today_spinbox.valueChanged, self._on_today_spinbox_changed)
        qconnect(self.today_same_checkbox.stateChanged, self._on_today_same_toggled)
        qconnect(self.active_checkbox.stateChanged, self._on_setting_changed)
        qconnect(self.save_button.clicked, self._on_save_clicked)
        qconnect(self.cancel_button.clicked, self._on_cancel_clicked)
        qconnect(self._forward_forecast_timer.timeout, self._run_forward_forecast)
        qconnect(self._reverse_forecast_timer.timeout, self._run_reverse_forecast)
        qconnect(self._apply_today_timer.timeout, self._apply_today_override)

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------
    @contextmanager
    def _signals_blocked(self, *widgets):
        """Programmatic widget updates: block signals and mark _updating."""
        self._updating = True
        for widget in widgets:
            widget.blockSignals(True)
        try:
            yield
        finally:
            for widget in widgets:
                widget.blockSignals(False)
            self._updating = False

    def _current_deck_name(self) -> str:
        return self.deck_selector.currentText()

    def _current_deck_id(self) -> int | None:
        return self._deck_ids.get(self._current_deck_name())

    def _today_day_cutoff(self) -> int:
        return aqt.mw.col.sched.day_cutoff

    # ------------------------------------------------------------------
    # Config persistence
    # ------------------------------------------------------------------
    def _build_config_entry(self, deck_name: str) -> dict:
        cap_value = self.cap_spinbox.value()
        return {
            "deckNames": [deck_name],
            "budgetMinutes": round(float(self.budget_spinbox.value()), 1),
            "horizonDays": int(self.finish_spinbox.value()),
            "dailyNewCap": int(cap_value) if cap_value > 0 else None,
            "desiredRetentionOverride": None,
            "active": self.active_checkbox.isChecked(),
        }

    def _save_config(self) -> None:
        deck_name = self._current_deck_name()
        new_entry = self._build_config_entry(deck_name)
        other_entries = [
            entry
            for entry in self._config.get("decks", [])
            if not (
                (
                    isinstance(entry.get("deckNames"), list)
                    and deck_name in entry["deckNames"]
                )
                or entry.get("deckNames") == deck_name
            )
        ]
        self._config["decks"] = [new_entry] + other_entries
        aqt.mw.addonManager.writeConfig(ADDON_PACKAGE, self._config)
        self._dirty = False

    def _load_today_override(self, deck_name: str) -> float | None:
        """Stored today-budget if still valid for today, else None."""
        entry = self._config.get("todayOverrides", {}).get(deck_name)
        if entry and entry.get("dayCutoff") == self._today_day_cutoff():
            return float(entry["budgetMinutes"])
        return None

    def _persist_today_override(self, deck_name: str, budget_minutes) -> None:
        """Save or clear today's override. Does not touch deck settings."""
        overrides = self._config.setdefault("todayOverrides", {})
        if budget_minutes is None:
            overrides.pop(deck_name, None)
        else:
            overrides[deck_name] = {
                "budgetMinutes": float(budget_minutes),
                "dayCutoff": self._today_day_cutoff(),
            }
        aqt.mw.addonManager.writeConfig(ADDON_PACKAGE, self._config)

    def _load_deck(self, deck_name: str) -> None:
        """Populate widgets from saved config (no signals, resets dirty)."""
        entry = adapter.match_deck_configs(self._config, deck_name) or {}
        widgets = (
            self.budget_spinbox,
            self.finish_spinbox,
            self.cap_spinbox,
            self.today_same_checkbox,
            self.today_spinbox,
            self.active_checkbox,
        )
        with self._signals_blocked(*widgets):
            budget = float(entry.get("budgetMinutes", DEFAULT_BUDGET_MINUTES))
            self.budget_spinbox.setValue(budget)
            self.finish_spinbox.setValue(
                int(entry.get("horizonDays", DEFAULT_HORIZON_DAYS))
            )
            self.cap_spinbox.setValue(int(entry.get("dailyNewCap") or 0))
            self.active_checkbox.setChecked(bool(entry.get("active", False)))
            override = self._load_today_override(deck_name)
            if override is not None:
                self.today_same_checkbox.setChecked(False)
                self.today_spinbox.setValue(override)
                self.today_spinbox.setEnabled(True)
            else:
                self.today_same_checkbox.setChecked(True)
                self.today_spinbox.setValue(budget)
                self.today_spinbox.setEnabled(False)
        self._dirty = False

    # ------------------------------------------------------------------
    # Forecast display
    # ------------------------------------------------------------------
    def _forecast_labels(self) -> tuple[QLabel, ...]:
        return (
            self.limit_label,
            self.studied_label,
            self.peak_label,
            self.base_load_label,
            self.cost_label,
        )

    def _clear_forecast(self) -> None:
        for label in self._forecast_labels():
            label.setText("…")
        self.warning_label.setText("")

    def _populate_forecast(self, result: DeckResult) -> None:
        if result.fsrs_disabled:
            for label in self._forecast_labels():
                label.setText("—")
            self.warning_label.setText("⚠  FSRS not enabled for this deck.")
            return
        if result.error:
            for label in self._forecast_labels():
                label.setText("—")
            self.warning_label.setText(f"⚠  Error: {result.error}")
            return

        today_budget = (
            float(self.budget_spinbox.value())
            if self.today_same_checkbox.isChecked()
            else float(self.today_spinbox.value())
        )
        remaining = max(0.0, today_budget - result.studied_minutes)
        self.limit_label.setText(str(result.today_new_limit))
        self.studied_label.setText(
            f"{result.studied_minutes:.1f} min  "
            f"({remaining:.1f} min remaining of {today_budget:.1f} min budget)"
        )
        self.peak_label.setText(f"{result.peak_minutes:.1f} min/day")
        self.base_load_label.setText(f"{result.base_peak_minutes:.1f} min/day")
        self.cost_label.setText(
            f"new={result.cost.sec_new:.0f}s  "
            f"pass={result.cost.sec_pass:.0f}s  "
            f"lapse={result.cost.sec_lapse:.0f}s"
        )
        if not result.feasible:
            self.warning_label.setText(
                f"⚠  INFEASIBLE — {result.cards_unscheduled} new cards won't "
                f"fit in {result.horizon_days} days at this budget. "
                f"Raise the budget or extend the horizon."
            )
        else:
            self.warning_label.setText("")

    # ------------------------------------------------------------------
    # Forecasts (background QueryOps)
    # ------------------------------------------------------------------
    def _run_forward_forecast(self) -> None:
        """Budget → forecast, and update 'Finish in' with the completion day."""
        deck_id = self._current_deck_id()
        if deck_id is None:
            return
        self._forecast_generation += 1
        generation = self._forecast_generation
        self._clear_forecast()

        budget = float(self.budget_spinbox.value())
        daily_cap = int(self.cap_spinbox.value())
        today_budget = (
            budget
            if self.today_same_checkbox.isChecked()
            else float(self.today_spinbox.value())
        )

        def compute(col) -> DeckResult:
            return adapter.compute_deck_plan(
                col,
                deck_id,
                budget_minutes=budget,
                today_budget_minutes=today_budget,
                daily_new_cap=daily_cap,
            )

        def done(result: DeckResult) -> None:
            if self._forecast_generation != generation or not self.isVisible():
                return
            self._populate_forecast(result)
            if (
                not result.fsrs_disabled
                and not result.error
                and result.completion_day >= 0
            ):
                with self._signals_blocked(self.finish_spinbox):
                    self.finish_spinbox.setValue(max(1, result.completion_day))

        QueryOp(parent=self, op=compute, success=done).run_in_background()

    def _run_reverse_forecast(self) -> None:
        """'Finish in' → binary-search the required budget, then forecast."""
        deck_id = self._current_deck_id()
        if deck_id is None:
            return
        self._forecast_generation += 1
        generation = self._forecast_generation
        self._clear_forecast()

        target_days = int(self.finish_spinbox.value())
        daily_cap = int(self.cap_spinbox.value())
        today_override = (
            None
            if self.today_same_checkbox.isChecked()
            else float(self.today_spinbox.value())
        )

        def compute(col) -> tuple:
            inputs = adapter.read_deck_inputs(col, deck_id)
            if inputs.kernel is None:
                return adapter.fsrs_disabled_result(inputs.deck_name, deck_id), 0.0

            required_budget = find_budget_for_target(
                inputs.existing,
                inputs.total_new_cards,
                target_days,
                inputs.kernel,
                inputs.cost,
                daily_new_cap=daily_cap if daily_cap > 0 else NO_DAILY_CAP,
            )
            today_budget = (
                today_override if today_override is not None else required_budget
            )
            result = adapter.compute_deck_plan(
                col,
                deck_id,
                budget_minutes=required_budget,
                today_budget_minutes=today_budget,
                daily_new_cap=daily_cap,
                horizon=target_days,
                inputs=inputs,
            )
            return result, required_budget

        def done(payload: tuple) -> None:
            if self._forecast_generation != generation or not self.isVisible():
                return
            result, required_budget = payload
            self._populate_forecast(result)
            if not result.fsrs_disabled and not result.error:
                with self._signals_blocked(self.budget_spinbox):
                    self.budget_spinbox.setValue(round(required_budget, 1))

        QueryOp(parent=self, op=compute, success=done).run_in_background()

    def _apply_today_override(self) -> None:
        """Write today's new-card limit right after the override changes."""
        deck_id = self._current_deck_id()
        if deck_id is None:
            return
        budget = float(self.budget_spinbox.value())
        daily_cap = int(self.cap_spinbox.value())
        today_budget = (
            budget
            if self.today_same_checkbox.isChecked()
            else float(self.today_spinbox.value())
        )

        def apply(col) -> None:
            adapter.compute_deck_plan(
                col,
                deck_id,
                budget_minutes=budget,
                today_budget_minutes=today_budget,
                daily_new_cap=daily_cap,
                write_limit=True,
            )

        QueryOp(parent=self, op=apply, success=lambda _: None).run_in_background()

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------
    def _on_budget_changed(self, _value) -> None:
        if self._updating:
            return
        self._dirty = True
        # Keep the greyed hint in today_spinbox current when same-as-daily is on.
        if self.today_same_checkbox.isChecked():
            with self._signals_blocked(self.today_spinbox):
                self.today_spinbox.setValue(self.budget_spinbox.value())
        self._forward_forecast_timer.start()

    def _on_finish_changed(self, _value) -> None:
        if self._updating:
            return
        self._dirty = True
        self._reverse_forecast_timer.start()

    def _on_setting_changed(self, _value=None) -> None:
        if self._updating:
            return
        self._dirty = True
        self._forward_forecast_timer.start()

    def _on_today_same_toggled(self, _state) -> None:
        if self._updating:
            return
        checked = self.today_same_checkbox.isChecked()
        self.today_spinbox.setEnabled(not checked)
        if not checked:
            with self._signals_blocked(self.today_spinbox):
                self.today_spinbox.setValue(self.budget_spinbox.value())
        deck_name = self._current_deck_name()
        if checked:
            self._persist_today_override(deck_name, None)
        else:
            self._persist_today_override(deck_name, self.today_spinbox.value())
        self._apply_today_timer.start()
        self._forward_forecast_timer.start()

    def _on_today_spinbox_changed(self, _value) -> None:
        if self._updating:
            return
        if not self.today_same_checkbox.isChecked():
            self._persist_today_override(
                self._current_deck_name(), self.today_spinbox.value()
            )
            self._apply_today_timer.start()
        self._forward_forecast_timer.start()

    def _on_deck_changed(self, _index: int) -> None:
        new_name = self.deck_selector.currentText()
        if new_name == self._previous_deck_name:
            return

        def switch() -> None:
            self._previous_deck_name = new_name
            self._load_deck(new_name)
            self._run_forward_forecast()

        def revert() -> None:
            index = (
                self._deck_names.index(self._previous_deck_name)
                if self._previous_deck_name in self._deck_names
                else 0
            )
            with self._signals_blocked(self.deck_selector):
                self.deck_selector.setCurrentIndex(index)

        if self._dirty:
            self._ask_unsaved(switch, revert)
        else:
            switch()

    # ------------------------------------------------------------------
    # Save / close
    # ------------------------------------------------------------------
    def _ask_unsaved(self, on_proceed, on_cancel=None) -> None:
        """Anki-style 'Save changes?' prompt."""
        answer = QMessageBox.question(
            self,
            "Time Budget",
            "Save changes?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Save:
            self._save_config()
            on_proceed()
        elif answer == QMessageBox.StandardButton.Discard:
            on_proceed()
        else:  # [x] or Escape — abort the operation
            if on_cancel:
                on_cancel()

    def _trigger_auto_apply(self) -> None:
        """Fire-and-forget limit write using saved config. Parented to the
        main window so it survives dialog destruction."""
        deck_name = self._current_deck_name()
        deck_id = self._deck_ids.get(deck_name)
        if deck_id is None:
            return
        entry = adapter.match_deck_configs(self._config, deck_name) or {}
        if not entry:
            return
        budget = float(entry.get("budgetMinutes", DEFAULT_BUDGET_MINUTES))
        daily_cap = int(entry.get("dailyNewCap") or 0)
        override = self._config.get("todayOverrides", {}).get(deck_name)
        override_budget = float(override["budgetMinutes"]) if override else None
        override_day_cutoff = override.get("dayCutoff") if override else None

        def apply(col) -> DeckResult:
            today_budget = (
                override_budget
                if override_budget is not None
                and override_day_cutoff == col.sched.day_cutoff
                else budget
            )
            return adapter.compute_deck_plan(
                col,
                deck_id,
                budget_minutes=budget,
                today_budget_minutes=today_budget,
                daily_new_cap=daily_cap,
                write_limit=True,
            )

        def done(result: DeckResult) -> None:
            if not result.error and aqt.mw.state in ("deckBrowser", "overview"):
                aqt.mw.reset()

        QueryOp(parent=aqt.mw, op=apply, success=done).run_in_background()

    def _close_and_apply(self) -> None:
        """Close the dialog (bypassing the dirty check) and re-apply the
        last-saved settings."""
        self._trigger_auto_apply()
        self._force_close = True
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self._force_close:
            event.accept()
            return
        event.ignore()
        if self._dirty:
            self._ask_unsaved(self._close_and_apply)
        else:
            self._close_and_apply()

    def _on_save_clicked(self) -> None:
        self._save_config()
        deck_name = self._current_deck_name()
        deck_id = self._deck_ids.get(deck_name)
        if deck_id is None:
            return
        budget = float(self.budget_spinbox.value())
        daily_cap = int(self.cap_spinbox.value())
        today_budget = (
            budget
            if self.today_same_checkbox.isChecked()
            else float(self.today_spinbox.value())
        )

        def compute(col) -> DeckResult:
            return adapter.compute_deck_plan(
                col,
                deck_id,
                budget_minutes=budget,
                today_budget_minutes=today_budget,
                daily_new_cap=daily_cap,
                write_limit=True,
            )

        def done(result: DeckResult) -> None:
            if result.fsrs_disabled:
                tooltip(f"FSRS not enabled for '{deck_name}'.")
            elif result.error:
                tooltip(f"Error applying limit: {result.error}")
            else:
                tooltip(
                    f"Applied: {result.today_new_limit} new cards/day "
                    f"for '{deck_name}'."
                )
                if aqt.mw.state in ("deckBrowser", "overview"):
                    aqt.mw.reset()

        QueryOp(parent=self, op=compute, success=done).run_in_background()

    def _on_cancel_clicked(self) -> None:
        if self._dirty:
            self._ask_unsaved(self._close_and_apply)
        else:
            self._close_and_apply()


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------
_open_dialog: TimeBudgetDialog | None = None


def show_dialog() -> None:
    """Open the Time Budget dialog, or focus it if it is already open."""
    global _open_dialog
    if aqt.mw.col is None:
        tooltip("Please open a profile first.")
        return
    if _open_dialog is not None and _open_dialog.isVisible():
        _open_dialog.raise_()
        _open_dialog.activateWindow()
        return
    _open_dialog = TimeBudgetDialog(aqt.mw)
    _open_dialog.show()
