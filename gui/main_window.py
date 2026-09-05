"""
==========================================================
TradeSuite
gui/main_window.py
==========================================================

Top-level window. Gates everything behind LicenseGate: nothing else is
even constructed as "live" until check_status().licensed is True. Also
re-checks periodically while running so an app left open across an
expiry boundary actually stops trading and drops back to the renewal
screen, rather than SureFramePro's soft-nag-only behavior -- the plan
calls this out explicitly as a required improvement, not optional
polish, since letting an expired install keep placing trades defeats
the point of having a license at all.
"""

from __future__ import annotations

from datetime import datetime

import pytz
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMainWindow, QWidget, QVBoxLayout, QTabWidget, QMessageBox

from engine.config_store import ConfigStore
from engine.openalgo_manager import OpenAlgoManager
from engine.process_manager import ProcessManager
from gui.license_screen import LicenseGate, LicensedStrip
from gui.setup_wizard import SetupWizard
from gui.status_panel import StatusPanel
from gui.strategy_picker import StrategyPicker
from gui.trade_log_panel import TradeLogPanel
from gui.history_panel import HistoryPanel
from gui.activity_log_panel import ActivityLogPanel
from gui.trail_study_panel import TrailStudyPanel
from licensing import renewal_report
from engine import edition

LICENSE_RECHECK_MS = 60 * 60 * 1000  # hourly is plenty for a calendar-day expiry
IST = pytz.timezone("Asia/Kolkata")

AUTOCLOSE_POLL_MS = 15_000
# Gopinath, 2026-08-31: "Stop the APP as soon our trades are done for the
# day and wait for next day." Leaving it running unattended after trading
# ends serves no purpose -- there is no production task backing it up, so
# an unattended machine gains nothing by staying open, and closing removes
# any chance of it being left running (and someone assuming it is still
# watching the market) overnight. 90s grace so the final result is visible
# on screen for a moment before the window disappears, not an instant
# vanish the instant the last exit lands.
AUTOCLOSE_GRACE_SEC = 90


class MainAppView(QWidget):
    def __init__(self, config: ConfigStore, process_manager: ProcessManager, openalgo_manager: OpenAlgoManager):
        super().__init__()
        layout = QVBoxLayout(self)

        self.licensed_strip = LicensedStrip()
        layout.addWidget(self.licensed_strip)

        self.status_panel = StatusPanel(config)
        layout.addWidget(self.status_panel)

        tabs = QTabWidget()
        self.strategy_picker = StrategyPicker(config, process_manager)
        self.setup_wizard = SetupWizard(config, openalgo_manager)
        self.trade_log_panel = TradeLogPanel(config, process_manager)
        self.history_panel = HistoryPanel(config)
        self.activity_log_panel = ActivityLogPanel(config, process_manager)
        tabs.addTab(self.strategy_picker, "Strategies")
        tabs.addTab(self.setup_wizard, "Broker Setup")
        tabs.addTab(self.activity_log_panel, "Activity")
        tabs.addTab(self.trade_log_panel, "Trade Log")
        tabs.addTab(self.history_panel, "History")
        # Internal research tool, not a customer-facing feature -- see
        # engine/edition.py.
        if edition.SHOW_TRAIL_STUDY:
            self.trail_study_panel = TrailStudyPanel(config)
            tabs.addTab(self.trail_study_panel, "Trail Study")
        else:
            self.trail_study_panel = None
        layout.addWidget(tabs)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TradeSuite")
        self.resize(760, 560)

        self.config = ConfigStore()
        self.process_manager = ProcessManager(self.config)
        self.openalgo_manager = OpenAlgoManager(self.config)

        self.license_gate = LicenseGate()
        self.main_app_view = MainAppView(self.config, self.process_manager, self.openalgo_manager)
        self.license_gate.licensed_changed.connect(self._on_licensed_changed)
        self._auto_started = False

        self.setCentralWidget(self.license_gate)
        # LicenseGate.__init__ already ran its own refresh() (and thus already
        # emitted licensed_changed) before the connect() above existed -- so an
        # already-licensed customer's very first launch would otherwise be
        # stuck showing the registration/renewal gate. Re-sync explicitly now
        # that someone is actually listening.
        self._on_licensed_changed(self.license_gate.is_licensed())

        self.recheck_timer = QTimer(self)
        self.recheck_timer.timeout.connect(self._recheck_license)
        self.recheck_timer.start(LICENSE_RECHECK_MS)

        # Renewal-boundary reporting. Checked at launch and hourly rather
        # than scheduled for the exact moment of expiry, because an install
        # that was simply switched off over its renewal date must still
        # report that period the next time it runs.
        renewal_report.check_and_send_async(self.config)

        # Auto-close once today's trading is done -- see AUTOCLOSE_GRACE_SEC.
        # _trading_seen tracks which strategies were actually started today
        # (is_running() true at least once); once every strategy that was
        # started has finished its own run (thread ended -- backstop hit,
        # gave up entering, or the day fully played out), a close is due.
        # Reset whenever the IST calendar date rolls over, so leaving the
        # app open across midnight doesn't carry yesterday's state into a
        # fresh trading day.
        # "They can start the app manually and close it manually for now"
        # (Gopinath, 2026-08-31, re: the customer edition) -- the timer
        # itself is only ever started for editions that want it; see
        # engine/edition.py.
        self._trading_day = None
        self._trading_seen: set[str] = set()
        self._trading_done_since = None
        self._base_title = self.windowTitle()
        self.autoclose_timer = QTimer(self)
        self.autoclose_timer.timeout.connect(self._check_autoclose)
        if edition.AUTO_CLOSE_DEFAULT:
            self.autoclose_timer.start(AUTOCLOSE_POLL_MS)

    def _on_licensed_changed(self, licensed: bool):
        if licensed:
            self.setCentralWidget(self.main_app_view)
            self.main_app_view.licensed_strip.refresh()
            self._auto_start_chosen_strategies()

    def _auto_start_chosen_strategies(self):
        """Starts whichever strategy/strategies were last started by hand
        (see StrategyBox._start()'s enabled flag), so the app doesn't sit
        idle waiting for a click every morning it's opened. Runs once per
        launch, only after licensing has actually cleared, and only if the
        broker is configured -- same guard the manual Start button uses."""
        if self._auto_started:
            return
        self._auto_started = True
        if not self.config.is_broker_configured():
            return
        for key in ("orb", "tamil"):
            if self.config.settings[key].get("enabled") and not self.process_manager.is_running(key):
                self.process_manager.start(key)
        self.main_app_view.strategy_picker.refresh()

    def _recheck_license(self):
        if self.centralWidget() is self.main_app_view and not self.license_gate.is_licensed():
            self.process_manager.stop_all()
            QMessageBox.warning(self, "License expired", "Your license has expired -- strategies have been "
                                                            "stopped. Renew to continue.")
            self.license_gate.refresh()
            self.setCentralWidget(self.license_gate)
        elif self.centralWidget() is self.main_app_view:
            self.main_app_view.licensed_strip.refresh()
        renewal_report.check_and_send_async(self.config)

    def _check_autoclose(self):
        today = datetime.now(IST).date()
        if self._trading_day != today:
            self._trading_day = today
            self._trading_seen = set()
            self._trading_done_since = None
            self.setWindowTitle(self._base_title)

        for key in ("orb", "tamil"):
            if self.process_manager.is_running(key):
                self._trading_seen.add(key)
        if not self._trading_seen:
            return  # nothing started yet today -- normal before the market opens

        if not all(not self.process_manager.is_running(k) for k in self._trading_seen):
            self._trading_done_since = None
            if self.windowTitle() != self._base_title:
                self.setWindowTitle(self._base_title)
            return

        if self._trading_done_since is None:
            self._trading_done_since = datetime.now(IST)
            self._log_autoclose(f"Today's trading finished ({', '.join(sorted(self._trading_seen))}) -- "
                                 f"closing in {AUTOCLOSE_GRACE_SEC}s.")

        remaining = AUTOCLOSE_GRACE_SEC - (datetime.now(IST) - self._trading_done_since).total_seconds()
        if remaining <= 0:
            self._log_autoclose("Closing now.")
            self.close()
        else:
            self.setWindowTitle(f"{self._base_title} — today's trading is done, closing in {int(remaining)}s")

    def _log_autoclose(self, msg: str) -> None:
        """A small persisted trail for the auto-close decision, separate
        from the per-strategy activity logs -- when it does NOT fire (or
        fires unexpectedly), this is the only record of why, since the
        window title itself disappears the moment the process exits."""
        try:
            line = f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} IST] {msg}\n"
            with open(self.config.logs_dir / "autoclose.log", "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    def closeEvent(self, event):
        self.process_manager.stop_all()
        self.openalgo_manager.stop_process()
        super().closeEvent(event)
