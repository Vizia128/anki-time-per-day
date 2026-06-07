"""
time_budget/__init__.py
=======================
Anki add-on entry point.

Single unified UI:  Tools → Time Budget → Open…
  • Deck dropdown  (dirty-checked on switch)
  • Settings:
      – Daily budget  ↔  Finish in   (bidirectional, edit either)
      – Today's budget override
      – Daily cap
  • Live forecast  (debounced 400 ms)
  • Dirty tracking + "unsaved changes" popup on close / deck switch
  • Save (config only) | Apply Now (config + limit) | Cancel (dirty check)
  • Auto Apply: fires on dialog close, profile open, and after sync —
    always uses the last-saved settings, never the live spinbox values.
"""

from __future__ import annotations

import os


def init() -> None:
    import aqt
    import aqt.qt as qt
    from aqt import gui_hooks
    from aqt.operations import QueryOp
    from aqt.utils import qconnect, tooltip

    from . import adapter
    from .adapter import DeckResult
    from .scheduler import FsrsKernel, make_plan

    # ------------------------------------------------------------------
    # Background "apply all" — used by hooks and "Apply All Decks" menu
    # ------------------------------------------------------------------
    def run_all(col, *, force: bool = False) -> list[DeckResult]:
        config = aqt.mw.addonManager.getConfig("time_budget") or {}
        results: list[DeckResult] = []
        for deck_obj in col.decks.all_names_and_ids(include_filtered=False):
            entry = adapter.match_deck_configs(config, deck_obj.name)
            if entry is None:
                continue
            if not entry.get("autoApply", False) and not force:
                continue
            budget = float(entry.get("budgetMinutes", 30))
            cap    = int(entry.get("dailyNewCap") or 9999)
            did    = int(deck_obj.id)
            try:
                dr, params = adapter.read_fsrs_params(col, did)
                if params is None:
                    result = DeckResult(
                        deck_name=deck_obj.name, did=did,
                        today_new_limit=0, completion_day=-1,
                        feasible=False, cards_unscheduled=0,
                        peak_minutes=0.0, base_peak_minutes=0.0,
                        cost=adapter.CostModel(), plan=None,
                        fsrs_disabled=True,
                    )
                else:
                    kernel    = FsrsKernel(params=params, desired_retention=dr)
                    cost      = adapter.read_cost_model(col, did)
                    existing  = adapter.read_existing_cards(col, did)
                    total_new = adapter.count_new_cards(col, did)
                    horizon   = _adaptive_horizon(
                        total_new, budget, cost, existing, kernel
                    )
                    plan = make_plan(existing, total_new, budget,
                                     kernel, cost, horizon, cap)
                    adapter.set_today_new_limit(col, did, plan.today())
                    result = DeckResult(
                        deck_name=deck_obj.name, did=did,
                        today_new_limit=plan.today(),
                        completion_day=plan.completion_day,
                        feasible=plan.feasible,
                        cards_unscheduled=plan.cards_unscheduled,
                        peak_minutes=plan.peak_minutes(),
                        base_peak_minutes=0.0,
                        cost=cost, plan=plan,
                    )
            except Exception as exc:
                result = DeckResult(
                    deck_name=deck_obj.name, did=did,
                    today_new_limit=0, completion_day=-1,
                    feasible=False, cards_unscheduled=0,
                    peak_minutes=0.0, base_peak_minutes=0.0,
                    cost=adapter.CostModel(), plan=None,
                    error=str(exc),
                )
            results.append(result)
        return results

    def _on_run_all_done(results: list[DeckResult]) -> None:
        for r in results:
            if r.error:
                tooltip(f"Time Budget — {r.deck_name}: {r.error}")
            elif r.fsrs_disabled:
                tooltip(f"Time Budget — {r.deck_name}: FSRS not enabled, skipped.")
            elif not r.feasible:
                tooltip(
                    f"Time Budget — {r.deck_name}: "
                    f"{r.cards_unscheduled} cards won't fit in horizon."
                )
        if aqt.mw.state in ("deckBrowser", "overview"):
            aqt.mw.reset()

    # ------------------------------------------------------------------
    # Pure-math helpers (safe to call inside a QueryOp thread)
    # ------------------------------------------------------------------
    def _studied_today_minutes(col, did: int) -> float:
        """Minutes already spent on this deck's cards today (from revlog)."""
        dids = adapter.subdeck_ids_csv(col, did)
        start_ms = (col.sched.day_cutoff - 86400) * 1000
        ms = col.db.scalar(
            f"SELECT COALESCE(SUM(time), 0) FROM revlog "
            f"WHERE id >= {start_ms} "
            f"AND cid IN (SELECT id FROM cards WHERE did IN {dids})"
        )
        return (ms or 0) / 1000.0 / 60.0

    def _find_budget_for_target(
        existing, total_new: int, target_days: int,
        kernel, cost, cap: int,
    ) -> float:
        """Binary search: minimum budget (min/day) whose plan completes in target_days."""
        lo, hi = 0.0, 24.0 * 60.0
        for _ in range(35):
            mid = (lo + hi) / 2.0
            plan = make_plan(
                existing=existing,
                total_new_cards=total_new,
                budget_minutes=mid,
                kernel=kernel,
                cost=cost,
                horizon=target_days,
                daily_new_cap=cap,
            )
            if plan.feasible:
                hi = mid
            else:
                lo = mid
        return hi

    def _adaptive_horizon(
        total_new: int, budget_minutes: float, cost, existing, kernel,
    ) -> int:
        """Estimate a planning horizon that comfortably fits the full deck.

        Rough estimate: (new cards / estimated daily throughput) * 3 + 60,
        clamped to [30, 3650]. The 3× factor absorbs review-tail overhead
        and base-load underestimation; the +60 adds a minimum buffer.
        """
        if total_new == 0:
            return 30
        per_review = kernel.dr * cost.sec_pass + (1.0 - kernel.dr) * cost.sec_lapse
        base_est   = sum(s.mass for s in existing if s.due == 0) * per_review
        avail_sec  = max(1.0, budget_minutes * 60.0 - base_est)
        daily_new  = avail_sec / max(1.0, cost.sec_new)
        rough_days = total_new / max(0.01, daily_new)
        return max(30, min(3650, int(rough_days * 3) + 60))

    # ------------------------------------------------------------------
    # Unified dialog
    # ------------------------------------------------------------------
    def show_main_dialog() -> None:
        col = aqt.mw.col
        if col is None:
            tooltip("Please open a profile first.")
            return

        all_decks = sorted(
            col.decks.all_names_and_ids(include_filtered=False),
            key=lambda d: d.name,
        )
        deck_names  = [d.name for d in all_decks]
        deck_id_map = {d.name: int(d.id) for d in all_decks}

        config: dict = aqt.mw.addonManager.getConfig("time_budget") or {"decks": []}

        # ── Dialog shell ──────────────────────────────────────────────
        dialog = qt.QDialog(aqt.mw)
        dialog.setWindowTitle("Time Budget")
        dialog.setMinimumWidth(520)

        # ── Deck picker ───────────────────────────────────────────────
        deck_combo = qt.QComboBox()
        deck_combo.addItems(deck_names)
        deck_row = qt.QHBoxLayout()
        deck_row.addWidget(qt.QLabel("Deck:"))
        deck_row.addWidget(deck_combo, 1)

        # ── Settings group ────────────────────────────────────────────
        settings_group = qt.QGroupBox("Settings")

        budget_spin = qt.QDoubleSpinBox()
        budget_spin.setRange(0.5, 9999.0)
        budget_spin.setDecimals(1)
        budget_spin.setSuffix(" min/day")
        budget_spin.setToolTip(
            "Daily study-time budget (new + review). "
            "Or edit 'Finish in' to compute this automatically."
        )

        finish_spin = qt.QSpinBox()
        finish_spin.setRange(1, 9999)
        finish_spin.setSuffix(" days")
        finish_spin.setToolTip(
            "Target days to finish the deck. Edit to compute the required daily budget."
        )

        link_lbl = qt.QLabel("← edit either one; the other updates automatically")
        link_lbl.setStyleSheet("color: gray; font-size: 10px;")

        today_same_chk = qt.QCheckBox("Same as daily budget")
        today_same_chk.setChecked(True)
        today_spin = qt.QDoubleSpinBox()
        today_spin.setRange(0.5, 9999.0)
        today_spin.setDecimals(1)
        today_spin.setSuffix(" min")
        today_spin.setEnabled(False)
        today_spin.setToolTip(
            "One-off budget for today only. "
            "The long-term daily budget is unchanged."
        )

        today_row_layout = qt.QHBoxLayout()
        today_row_layout.setContentsMargins(0, 0, 0, 0)
        today_row_layout.addWidget(today_same_chk)
        today_row_layout.addWidget(today_spin, 1)
        today_widget = qt.QWidget()
        today_widget.setLayout(today_row_layout)

        cap_spin = qt.QSpinBox()
        cap_spin.setRange(0, 9999)
        cap_spin.setSpecialValueText("no cap")
        cap_spin.setToolTip(
            "Hard ceiling on new cards per day regardless of budget headroom. "
            "0 = no ceiling."
        )

        sf = qt.QFormLayout()
        sf.addRow("Daily budget:", budget_spin)
        sf.addRow("Finish in:", finish_spin)
        sf.addRow("", link_lbl)
        sf.addRow("Today's budget:", today_widget)
        sf.addRow("Daily cap:", cap_spin)
        settings_group.setLayout(sf)

        # ── Forecast group ────────────────────────────────────────────
        forecast_group = qt.QGroupBox("Forecast")

        def _ro_label() -> qt.QLabel:
            lbl = qt.QLabel("—")
            lbl.setTextInteractionFlags(
                qt.Qt.TextInteractionFlag.TextSelectableByMouse
            )
            return lbl

        limit_val   = _ro_label()
        studied_val = _ro_label()
        peak_val    = _ro_label()
        base_val    = _ro_label()
        cost_val    = _ro_label()
        warn_lbl    = qt.QLabel("")
        warn_lbl.setWordWrap(True)
        warn_lbl.setStyleSheet("color: #e8a020; font-weight: bold;")

        ff = qt.QFormLayout()
        ff.addRow("Today's new-card limit:", limit_val)
        ff.addRow("Already studied today (this deck):", studied_val)
        ff.addRow("Peak load:", peak_val)
        ff.addRow("Base load (existing):", base_val)
        ff.addRow("Cost model:", cost_val)
        fc_vbox = qt.QVBoxLayout()
        fc_vbox.addLayout(ff)
        fc_vbox.addWidget(warn_lbl)
        forecast_group.setLayout(fc_vbox)

        # ── Bottom row ────────────────────────────────────────────────
        auto_check = qt.QCheckBox("Auto Apply  (on open, sync & close)")
        auto_check.setToolTip(
            "Write the new-card limit automatically when Anki opens, after sync, "
            "or when this dialog closes. Always uses the last-saved settings."
        )
        save_btn   = qt.QPushButton("Save")
        save_btn.setToolTip("Save settings without writing today's limit")
        apply_btn  = qt.QPushButton("Apply Now")
        apply_btn.setDefault(True)
        apply_btn.setToolTip("Save settings and write today's new-card limit immediately")
        cancel_btn = qt.QPushButton("Cancel")

        bottom = qt.QHBoxLayout()
        bottom.addWidget(auto_check)
        bottom.addStretch()
        bottom.addWidget(save_btn)
        bottom.addWidget(apply_btn)
        bottom.addWidget(cancel_btn)

        # ── Assemble ──────────────────────────────────────────────────
        main_layout = qt.QVBoxLayout()
        main_layout.addLayout(deck_row)
        main_layout.addWidget(settings_group)
        main_layout.addWidget(forecast_group)
        main_layout.addLayout(bottom)
        dialog.setLayout(main_layout)

        # ── Internal state ────────────────────────────────────────────
        _gen         = [0]      # monotonic; stale QueryOp results are discarded
        _dirty       = [False]  # True when spinboxes differ from saved config
        _updating    = [False]  # suppress signal re-entry during programmatic updates
        _force_close = [False]  # skip dirty check when we initiate the close ourselves
        _prev_deck   = [deck_names[0] if deck_names else ""]

        # ── Config helpers ────────────────────────────────────────────
        def _current_name() -> str:
            return deck_combo.currentText()

        def _build_entry(name: str) -> dict:
            cap_val = cap_spin.value()
            return {
                "deckNames": [name],
                "budgetMinutes": round(float(budget_spin.value()), 1),
                "horizonDays": int(finish_spin.value()),
                "dailyNewCap": int(cap_val) if cap_val > 0 else None,
                "desiredRetentionOverride": None,
                "autoApply": auto_check.isChecked(),
            }

        def _save_config() -> None:
            name = _current_name()
            new_entry = _build_entry(name)
            other = [
                e for e in config.get("decks", [])
                if not (
                    (isinstance(e.get("deckNames"), list) and name in e["deckNames"])
                    or e.get("deckNames") == name
                )
            ]
            config["decks"] = [new_entry] + other
            aqt.mw.addonManager.writeConfig("time_budget", config)
            _dirty[0] = False

        def _save_auto_apply_flag() -> None:
            """Persist only the autoApply toggle immediately; don't touch dirty."""
            if _updating[0]:
                return
            name = _current_name()
            for e in config.get("decks", []):
                pattern = e.get("deckNames", "")
                if (isinstance(pattern, list) and name in pattern) or pattern == name:
                    e["autoApply"] = auto_check.isChecked()
                    aqt.mw.addonManager.writeConfig("time_budget", config)
                    return
            # No entry yet — create one but restore dirty state afterward.
            was_dirty = _dirty[0]
            _save_config()
            _dirty[0] = was_dirty

        def _load_deck(name: str) -> None:
            """Populate widgets from saved config (no signals, no dirty change)."""
            entry = adapter.match_deck_configs(config, name) or {}
            _updating[0] = True
            for w in (budget_spin, finish_spin, cap_spin,
                      today_same_chk, today_spin, auto_check):
                w.blockSignals(True)
            budget_spin.setValue(float(entry.get("budgetMinutes", 30)))
            finish_spin.setValue(int(entry.get("horizonDays", 365)))
            cap_spin.setValue(int(entry.get("dailyNewCap") or 0))
            today_same_chk.setChecked(True)
            today_spin.setValue(float(entry.get("budgetMinutes", 30)))
            today_spin.setEnabled(False)
            auto_check.setChecked(bool(entry.get("autoApply", False)))
            for w in (budget_spin, finish_spin, cap_spin,
                      today_same_chk, today_spin, auto_check):
                w.blockSignals(False)
            _updating[0] = False
            _dirty[0] = False

        # ── Forecast helpers ──────────────────────────────────────────
        def _clear_forecast() -> None:
            for lbl in (limit_val, studied_val, peak_val, base_val, cost_val):
                lbl.setText("…")
            warn_lbl.setText("")

        def _populate_forecast(r: DeckResult, studied_min: float, horizon_days: int = 3650) -> None:
            if r.fsrs_disabled:
                for lbl in (limit_val, studied_val, peak_val, base_val, cost_val):
                    lbl.setText("—")
                warn_lbl.setText("⚠  FSRS not enabled for this deck.")
                return
            if r.error:
                for lbl in (limit_val, studied_val, peak_val, base_val, cost_val):
                    lbl.setText("—")
                warn_lbl.setText(f"⚠  Error: {r.error}")
                return

            today_budget = (
                float(budget_spin.value())
                if today_same_chk.isChecked()
                else float(today_spin.value())
            )
            remaining = max(0.0, today_budget - studied_min)
            limit_val.setText(str(r.today_new_limit))
            studied_val.setText(
                f"{studied_min:.1f} min  "
                f"({remaining:.1f} min remaining of {today_budget:.1f} min budget)"
            )
            peak_val.setText(f"{r.peak_minutes:.1f} min/day")
            base_val.setText(f"{r.base_peak_minutes:.1f} min/day")
            cost_val.setText(
                f"new={r.cost.sec_new:.0f}s  "
                f"pass={r.cost.sec_pass:.0f}s  "
                f"lapse={r.cost.sec_lapse:.0f}s"
            )
            if not r.feasible:
                warn_lbl.setText(
                    f"⚠  INFEASIBLE — {r.cards_unscheduled} new cards won't fit "
                    f"in {horizon_days} days at this budget. "
                    f"Raise the budget or extend the horizon."
                )
            else:
                warn_lbl.setText("")

        # ── Forward forecast: budget → forecast + update finish_spin ──
        def _run_forward_forecast() -> None:
            name = _current_name()
            did  = deck_id_map.get(name)
            if did is None:
                return
            _gen[0] += 1
            gen = _gen[0]
            _clear_forecast()

            budget       = float(budget_spin.value())
            cap          = int(cap_spin.value()) or 9999
            today_same   = today_same_chk.isChecked()
            today_budget = budget if today_same else float(today_spin.value())

            def _do(col) -> tuple:
                dr, params = adapter.read_fsrs_params(col, did)
                studied    = _studied_today_minutes(col, did)
                if params is None:
                    deck_name = col.decks.get(did)["name"]
                    dummy = DeckResult(
                        deck_name=deck_name, did=did,
                        today_new_limit=0, completion_day=-1,
                        feasible=False, cards_unscheduled=0,
                        peak_minutes=0.0, base_peak_minutes=0.0,
                        cost=adapter.CostModel(), plan=None,
                        fsrs_disabled=True,
                    )
                    return dummy, studied, -1, 30

                kernel    = FsrsKernel(params=params, desired_retention=dr)
                cost      = adapter.read_cost_model(col, did)
                existing  = adapter.read_existing_cards(col, did)
                total_new = adapter.count_new_cards(col, did)
                horizon   = _adaptive_horizon(total_new, budget, cost, existing, kernel)

                effective  = max(0.5, today_budget - studied)
                plan_eff   = make_plan(existing, total_new, effective,
                                       kernel, cost, horizon, cap)
                plan_full  = make_plan(existing, total_new, budget,
                                       kernel, cost, horizon, cap)
                deck_name  = col.decks.get(did)["name"]
                base_peak  = (
                    max(plan_full.base_seconds) / 60.0
                    if plan_full.base_seconds else 0.0
                )
                r = DeckResult(
                    deck_name=deck_name, did=did,
                    today_new_limit=plan_eff.today(),
                    completion_day=plan_full.completion_day,
                    feasible=plan_full.feasible,
                    cards_unscheduled=plan_full.cards_unscheduled,
                    peak_minutes=plan_full.peak_minutes(),
                    base_peak_minutes=base_peak,
                    cost=cost,
                    plan=plan_eff,
                )
                return r, studied, plan_full.completion_day, horizon

            def _done(tup: tuple) -> None:
                if _gen[0] != gen or not dialog.isVisible():
                    return
                r, studied, completion, horizon = tup
                _populate_forecast(r, studied, horizon)
                if not r.fsrs_disabled and not r.error and completion >= 0:
                    _updating[0] = True
                    finish_spin.blockSignals(True)
                    finish_spin.setValue(max(1, completion))
                    finish_spin.blockSignals(False)
                    _updating[0] = False

            QueryOp(parent=dialog, op=_do, success=_done).run_in_background()

        # ── Reverse forecast: finish_spin → binary-search budget + forecast ──
        def _run_reverse_forecast() -> None:
            name = _current_name()
            did  = deck_id_map.get(name)
            if did is None:
                return
            _gen[0] += 1
            gen = _gen[0]
            _clear_forecast()

            target_days    = int(finish_spin.value())
            cap            = int(cap_spin.value()) or 9999
            today_same     = today_same_chk.isChecked()
            today_override = None if today_same else float(today_spin.value())

            def _do(col) -> tuple:
                dr, params = adapter.read_fsrs_params(col, did)
                if params is None:
                    deck_name = col.decks.get(did)["name"]
                    dummy = DeckResult(
                        deck_name=deck_name, did=did,
                        today_new_limit=0, completion_day=-1,
                        feasible=False, cards_unscheduled=0,
                        peak_minutes=0.0, base_peak_minutes=0.0,
                        cost=adapter.CostModel(), plan=None,
                        fsrs_disabled=True,
                    )
                    return dummy, 0.0, 0.0

                kernel    = FsrsKernel(params=params, desired_retention=dr)
                cost      = adapter.read_cost_model(col, did)
                existing  = adapter.read_existing_cards(col, did)
                total_new = adapter.count_new_cards(col, did)
                studied   = _studied_today_minutes(col, did)

                required = _find_budget_for_target(
                    existing, total_new, target_days, kernel, cost, cap
                )
                today_budget = today_override if today_override is not None else required
                effective    = max(0.5, today_budget - studied)

                r = adapter.update_deck(
                    did=did, budget_minutes=effective,
                    horizon=target_days, daily_new_cap=cap, dry_run=True, col=col,
                )
                r_full = adapter.update_deck(
                    did=did, budget_minutes=required,
                    horizon=target_days, daily_new_cap=cap, dry_run=True, col=col,
                )
                r.peak_minutes      = r_full.peak_minutes
                r.base_peak_minutes = r_full.base_peak_minutes
                r.cards_unscheduled = r_full.cards_unscheduled
                r.feasible          = r_full.feasible
                return r, studied, required

            def _done(tup: tuple) -> None:
                if _gen[0] != gen or not dialog.isVisible():
                    return
                r, studied, required_budget = tup
                _populate_forecast(r, studied, target_days)
                if not r.fsrs_disabled and not r.error:
                    _updating[0] = True
                    budget_spin.blockSignals(True)
                    budget_spin.setValue(round(required_budget, 1))
                    budget_spin.blockSignals(False)
                    _updating[0] = False

            QueryOp(parent=dialog, op=_do, success=_done).run_in_background()

        # ── Debounce timers ───────────────────────────────────────────
        _debounce_fwd = qt.QTimer(dialog)
        _debounce_fwd.setSingleShot(True)
        _debounce_fwd.setInterval(400)
        qconnect(_debounce_fwd.timeout, _run_forward_forecast)

        _debounce_rev = qt.QTimer(dialog)
        _debounce_rev.setSingleShot(True)
        _debounce_rev.setInterval(400)
        qconnect(_debounce_rev.timeout, _run_reverse_forecast)

        # ── Signal handlers ───────────────────────────────────────────
        def _on_budget_changed(_val) -> None:
            if _updating[0]:
                return
            _dirty[0] = True
            # Keep the greyed hint in today_spin current when same-as-daily is on.
            if today_same_chk.isChecked():
                _updating[0] = True
                today_spin.blockSignals(True)
                today_spin.setValue(budget_spin.value())
                today_spin.blockSignals(False)
                _updating[0] = False
            _debounce_fwd.start()

        def _on_finish_changed(_val) -> None:
            if _updating[0]:
                return
            _dirty[0] = True
            _debounce_rev.start()

        def _on_misc_changed(_=None) -> None:
            if _updating[0]:
                return
            _dirty[0] = True
            _debounce_fwd.start()

        def _on_today_same_toggled(checked: bool) -> None:
            if _updating[0]:
                return
            today_spin.setEnabled(not checked)
            if not checked:
                _updating[0] = True
                today_spin.blockSignals(True)
                today_spin.setValue(budget_spin.value())
                today_spin.blockSignals(False)
                _updating[0] = False
            _on_misc_changed()

        # ── Unsaved-changes popup (matches Anki's native dialog style) ──
        def _ask_unsaved(on_proceed, on_cancel=None) -> None:
            ret = qt.QMessageBox.question(
                dialog,
                "Time Budget",
                "Save changes?",
                qt.QMessageBox.StandardButton.Save
                | qt.QMessageBox.StandardButton.Discard
                | qt.QMessageBox.StandardButton.Cancel,
                qt.QMessageBox.StandardButton.Save,
            )
            if ret == qt.QMessageBox.StandardButton.Save:
                _save_config()
                on_proceed()
            elif ret == qt.QMessageBox.StandardButton.Discard:
                on_proceed()
            elif on_cancel:
                on_cancel()

        # ── Deck switch (with dirty check) ────────────────────────────
        def _on_deck_changed(_index: int) -> None:
            new_name = deck_combo.currentText()
            if new_name == _prev_deck[0]:
                return

            def _switch() -> None:
                _prev_deck[0] = new_name
                _load_deck(new_name)
                _run_forward_forecast()

            def _revert() -> None:
                _updating[0] = True
                deck_combo.blockSignals(True)
                idx = (
                    deck_names.index(_prev_deck[0])
                    if _prev_deck[0] in deck_names else 0
                )
                deck_combo.setCurrentIndex(idx)
                deck_combo.blockSignals(False)
                _updating[0] = False

            if _dirty[0]:
                _ask_unsaved(_switch, _revert)
            else:
                _switch()

        # ── Auto Apply on close ───────────────────────────────────────
        def _trigger_auto_apply() -> None:
            """Fire-and-forget limit write using saved config (parent=mw so it
            survives dialog destruction)."""
            name = _current_name()
            did  = deck_id_map.get(name)
            if did is None:
                return
            entry = adapter.match_deck_configs(config, name) or {}
            if not entry.get("autoApply", False):
                return
            budget = float(entry.get("budgetMinutes", 30))
            cap    = int(entry.get("dailyNewCap") or 0) or 9999

            def _op(col) -> DeckResult:
                dr, params = adapter.read_fsrs_params(col, did)
                if params is None:
                    return DeckResult(
                        deck_name=name, did=did,
                        today_new_limit=0, completion_day=-1,
                        feasible=False, cards_unscheduled=0,
                        peak_minutes=0.0, base_peak_minutes=0.0,
                        cost=adapter.CostModel(), plan=None,
                        fsrs_disabled=True,
                    )
                kernel    = FsrsKernel(params=params, desired_retention=dr)
                cost      = adapter.read_cost_model(col, did)
                existing  = adapter.read_existing_cards(col, did)
                total_new = adapter.count_new_cards(col, did)
                horizon   = _adaptive_horizon(total_new, budget, cost, existing, kernel)
                plan      = make_plan(existing, total_new, budget,
                                      kernel, cost, horizon, cap)
                adapter.set_today_new_limit(col, did, plan.today())
                return DeckResult(
                    deck_name=name, did=did,
                    today_new_limit=plan.today(),
                    completion_day=plan.completion_day,
                    feasible=plan.feasible,
                    cards_unscheduled=plan.cards_unscheduled,
                    peak_minutes=plan.peak_minutes(),
                    base_peak_minutes=0.0,
                    cost=cost, plan=plan,
                )

            def _success(r: DeckResult) -> None:
                if not r.error and aqt.mw.state in ("deckBrowser", "overview"):
                    aqt.mw.reset()

            QueryOp(
                parent=aqt.mw, op=_op, success=_success,
            ).run_in_background()

        def _do_close() -> None:
            """Actually close the dialog, bypassing the dirty check."""
            _trigger_auto_apply()
            _force_close[0] = True
            dialog.close()

        def _close_event(event) -> None:
            if _force_close[0]:
                event.accept()
                return
            event.ignore()
            if _dirty[0]:
                _ask_unsaved(_do_close)
            else:
                _do_close()

        dialog.closeEvent = _close_event

        # ── Button handlers ───────────────────────────────────────────
        def _on_save() -> None:
            _save_config()

        def _on_apply() -> None:
            _save_config()
            name = _current_name()
            did  = deck_id_map.get(name)
            if did is None:
                return
            budget     = float(budget_spin.value())
            cap        = int(cap_spin.value()) or 9999
            today_same = today_same_chk.isChecked()
            today_bgt  = budget if today_same else float(today_spin.value())

            def _do(col) -> tuple:
                dr, params = adapter.read_fsrs_params(col, did)
                studied    = _studied_today_minutes(col, did)
                if params is None:
                    deck_name = col.decks.get(did)["name"]
                    return DeckResult(
                        deck_name=deck_name, did=did,
                        today_new_limit=0, completion_day=-1,
                        feasible=False, cards_unscheduled=0,
                        peak_minutes=0.0, base_peak_minutes=0.0,
                        cost=adapter.CostModel(), plan=None,
                        fsrs_disabled=True,
                    ), studied

                kernel    = FsrsKernel(params=params, desired_retention=dr)
                cost      = adapter.read_cost_model(col, did)
                existing  = adapter.read_existing_cards(col, did)
                total_new = adapter.count_new_cards(col, did)
                horizon   = _adaptive_horizon(total_new, budget, cost, existing, kernel)
                effective = max(0.5, today_bgt - studied)
                plan      = make_plan(existing, total_new, effective,
                                      kernel, cost, horizon, cap)
                adapter.set_today_new_limit(col, did, plan.today())
                deck_name = col.decks.get(did)["name"]
                base_peak = (
                    max(plan.base_seconds) / 60.0 if plan.base_seconds else 0.0
                )
                r = DeckResult(
                    deck_name=deck_name, did=did,
                    today_new_limit=plan.today(),
                    completion_day=plan.completion_day,
                    feasible=plan.feasible,
                    cards_unscheduled=plan.cards_unscheduled,
                    peak_minutes=plan.peak_minutes(),
                    base_peak_minutes=base_peak,
                    cost=cost, plan=plan,
                )
                return r, studied

            def _done(tup: tuple) -> None:
                r, _ = tup
                if r.fsrs_disabled:
                    tooltip(f"FSRS not enabled for '{name}'.")
                elif r.error:
                    tooltip(f"Error applying limit: {r.error}")
                else:
                    tooltip(
                        f"Applied: {r.today_new_limit} new cards/day for '{name}'."
                    )
                    if aqt.mw.state in ("deckBrowser", "overview"):
                        aqt.mw.reset()

            QueryOp(parent=dialog, op=_do, success=_done).run_in_background()

        def _on_cancel() -> None:
            if _dirty[0]:
                _ask_unsaved(_do_close)
            else:
                _do_close()

        # ── Wire signals ──────────────────────────────────────────────
        qconnect(deck_combo.currentIndexChanged, _on_deck_changed)
        qconnect(budget_spin.valueChanged,       _on_budget_changed)
        qconnect(finish_spin.valueChanged,       _on_finish_changed)
        qconnect(cap_spin.valueChanged,          _on_misc_changed)
        qconnect(today_spin.valueChanged,        _on_misc_changed)
        qconnect(
            today_same_chk.stateChanged,
            lambda _: _on_today_same_toggled(today_same_chk.isChecked()),
        )
        qconnect(auto_check.stateChanged, lambda _: _save_auto_apply_flag())
        qconnect(save_btn.clicked,   _on_save)
        qconnect(apply_btn.clicked,  _on_apply)
        qconnect(cancel_btn.clicked, _on_cancel)

        # ── Initialise with first deck ────────────────────────────────
        if deck_names:
            _prev_deck[0] = deck_names[0]
            _load_deck(deck_names[0])
            _run_forward_forecast()

        dialog.resize(520, 580)
        dialog.show()

    # ------------------------------------------------------------------
    # Hook registration
    # ------------------------------------------------------------------
    def _hook_run() -> None:
        QueryOp(
            parent=aqt.mw,
            op=lambda col: run_all(col, force=False),
            success=_on_run_all_done,
        ).run_in_background()

    gui_hooks.profile_did_open.append(_hook_run)
    gui_hooks.sync_did_finish.append(_hook_run)

    # ------------------------------------------------------------------
    # Tools menu
    # ------------------------------------------------------------------
    menu = qt.QMenu("Time Budget", aqt.mw)
    aqt.mw.form.menuTools.addMenu(menu)

    open_action = qt.QAction("Open…", menu)
    menu.addAction(open_action)

    qconnect(open_action.triggered, show_main_dialog)


if not os.environ.get("TEST"):
    init()
