"""
==========================================================
TradeSuite
gui/status_panel.py
==========================================================

System-health strip: is OpenAlgo reachable, is a broker connected, is
sandbox/analyzer (paper) mode on -- plus the live/paper toggle itself.

The toggle calls OpenAlgoClient.set_analyzer_mode(), which is real and
does switch live money trading on. Every strategy engine's own safety
gate (is_analyzer_mode_on() checked before every order, unchanged from
the original scripts) stays in place regardless of what this panel does
-- this UI is not a replacement for that gate, just the one legitimate
way a customer can flip it. Switching TO live requires typing a literal
confirmation phrase, not just clicking a toggle -- deliberate friction
against an accidental click turning on real-money trading, since every
other strategy in this whole codebase has treated "paper only" as a
hard rule up to this point.
"""

from __future__ import annotations

import subprocess
import threading
from datetime import datetime

import pytz

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton, QInputDialog, QMessageBox, QLineEdit,
)

from engine.config_store import ConfigStore
from engine.openalgo_client import OpenAlgoClient

IST = pytz.timezone("Asia/Kolkata")
POLL_MS = 15_000
LIVE_CONFIRM_PHRASE = "GO LIVE"

# When a dead session actually matters. Outside this window there is no
# market to trade and the broker's own nightly reset means the session is
# EXPECTED to be down -- shouting about it then trains the eye to ignore
# the banner, which is exactly when it needs to be believed.
TRADING_WINDOW = ((8, 45), (15, 45))


def _in_trading_window() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    (sh, sm), (eh, em) = TRADING_WINDOW
    return now.replace(hour=sh, minute=sm) <= now <= now.replace(hour=eh, minute=em)

# Impossible to miss on a glance across the room -- which is the point. A
# dead broker session is silent: strategies keep "running", the app keeps
# looking healthy, and the day's trades simply never happen. That is
# exactly what 2026-08-25 cost (ORB, all three underlyings, zero trades,
# noticed only afterwards from the logs).
ALERT_STYLE = ("background-color: #c0392b; color: white; font-weight: bold; "
               "padding: 8px; border-radius: 4px;")
INFO_STYLE = ("background-color: #eef2f7; color: #33475b; "
              "padding: 8px; border-radius: 4px; border: 1px solid #c8d3e0;")
WARN_STYLE = ("background-color: #e67e22; color: white; font-weight: bold; "
              "padding: 8px; border-radius: 4px;")


def _dot(ok: bool | None) -> str:
    return "🟢" if ok else ("🔴" if ok is False else "⚪")


class StatusPanel(QFrame):
    def __init__(self, config: ConfigStore):
        super().__init__()
        self.config = config
        self.setFrameShape(QFrame.StyledPanel)

        outer = QVBoxLayout(self)

        # The alert lives ABOVE the status dots, full width, and is hidden
        # entirely when everything is fine -- a banner that is always
        # present becomes furniture and stops being read.
        self.alert_label = QLabel()
        self.alert_label.setWordWrap(True)
        self.alert_label.setVisible(False)
        outer.addWidget(self.alert_label)

        alert_actions = QHBoxLayout()
        self.reconnect_btn = QPushButton("Reconnect broker now")
        self.reconnect_btn.clicked.connect(self._reconnect)
        self.reconnect_btn.setVisible(False)
        alert_actions.addWidget(self.reconnect_btn)
        self.reconnect_status = QLabel("")
        alert_actions.addWidget(self.reconnect_status)
        alert_actions.addStretch()
        outer.addLayout(alert_actions)

        strip = QWidget()
        layout = QHBoxLayout(strip)
        layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(strip)

        self.openalgo_label = QLabel()
        self.broker_label = QLabel()
        self.mode_label = QLabel()
        for lbl in (self.openalgo_label, self.broker_label, self.mode_label):
            layout.addWidget(lbl)
        self.toggle_btn = QPushButton()
        self.toggle_btn.clicked.connect(self._on_toggle_clicked)
        layout.addWidget(self.toggle_btn)
        layout.addStretch()

        self._analyzer_on: bool | None = None

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(POLL_MS)
        self.refresh()

    def _show_alert(self, style: str, text: str):
        self.alert_label.setStyleSheet(style)
        self.alert_label.setText(text)
        self.alert_label.setVisible(True)
        self.reconnect_btn.setVisible(bool(self.config.auto_login_command))

    def _clear_alert(self):
        self.alert_label.setVisible(False)
        self.alert_label.clear()
        self.reconnect_btn.setVisible(False)
        self.reconnect_status.clear()

    def _reconnect(self):
        """Runs the machine's configured broker re-login command. The app
        never handles broker credentials itself -- it only triggers the
        helper the operator already set up and already runs on a schedule."""
        command = self.config.auto_login_command
        if not command:
            return
        self.reconnect_btn.setEnabled(False)
        self.reconnect_status.setText("Reconnecting -- this usually takes 3-5 minutes...")

        def run():
            try:
                proc = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=600)
                ok = proc.returncode == 0
                tail = (proc.stdout or proc.stderr or "").strip().splitlines()
                detail = tail[-1] if tail else ""
            except subprocess.TimeoutExpired:
                ok, detail = False, "timed out after 10 minutes"
            except Exception as e:
                ok, detail = False, f"{type(e).__name__}: {e}"
            self._reconnect_done(ok, detail)

        threading.Thread(target=run, name="tradesuite-reconnect", daemon=True).start()

    def _reconnect_done(self, ok: bool, detail: str):
        self.reconnect_btn.setEnabled(True)
        self.reconnect_status.setText(
            ("Reconnected." if ok else f"Reconnect failed: {detail}")[:160])
        self.refresh()

    def _client(self) -> OpenAlgoClient:
        return OpenAlgoClient(self.config.openalgo_host, self.config.openalgo_api_key, self.config.openalgo_exchange)

    def refresh(self):
        if not self.config.is_broker_configured():
            self.openalgo_label.setText(f"{_dot(None)} OpenAlgo")
            self.broker_label.setText(f"{_dot(False)} Broker not configured yet")
            self.mode_label.setText("")
            self.toggle_btn.setVisible(False)
            self._clear_alert()
            return

        client = self._client()
        state, message = client.check_broker_session()
        up = state != OpenAlgoClient.SESSION_OPENALGO_DOWN
        self.openalgo_label.setText(f"{_dot(up)} OpenAlgo")
        if not up:
            self.broker_label.setText(f"{_dot(None)} Broker")
            self.mode_label.setText("")
            self.toggle_btn.setVisible(False)
            self._show_alert(ALERT_STYLE,
                              "OpenAlgo is not reachable -- no strategy can trade until it is running. " + message)
            return

        if state == OpenAlgoClient.SESSION_OK:
            self.broker_label.setText(f"{_dot(True)} Broker connected")
            self._clear_alert()
        elif state == OpenAlgoClient.SESSION_DEAD:
            self.broker_label.setText(f"{_dot(False)} Broker session EXPIRED")
            if _in_trading_window():
                how = ("Click Reconnect broker now below." if self.config.auto_login_command
                       else "Go to the Broker Setup tab and click Open Broker Connect to log in.")
                self._show_alert(ALERT_STYLE,
                                  "BROKER LOGIN REQUIRED -- the broker session has expired, so no trade "
                                  "can be placed and no market data can be read.\n\n"
                                  f"{how} (or log in directly at {self.config.openalgo_host})\n\n"
                                  f"Broker said: {message}")
            else:
                # Outside market hours this is the normal overnight state:
                # the broker resets the session daily and the scheduled
                # login restores it before the next session.
                self._show_alert(INFO_STYLE,
                                  "Broker session is not active. This is normal outside market hours -- "
                                  "the broker resets it daily and it is re-established before the next "
                                  "session. Nothing to do unless it is still down close to the open.")
        else:
            self.broker_label.setText(f"{_dot(False)} Broker check failed")
            self._show_alert(WARN_STYLE,
                              f"Could not confirm the broker session is alive: {message}\n"
                              "Trades may not go through -- check the Broker Setup tab.")

        self._analyzer_on = client.is_analyzer_mode_on()
        mode_text = "PAPER (sandbox)" if self._analyzer_on else "LIVE -- real money"
        self.mode_label.setText(f"{_dot(self._analyzer_on)} Mode: {mode_text}")

        self.toggle_btn.setVisible(True)
        self.toggle_btn.setText("Switch to LIVE" if self._analyzer_on else "Switch to Paper")

    def _on_toggle_clicked(self):
        if self._analyzer_on is None:
            return

        if self._analyzer_on:
            # Paper -> Live: the consequential direction. Require typing an
            # exact phrase, not just a Yes/No click, since this turns on
            # real-money trading for whichever strategies are running.
            text, ok = QInputDialog.getText(
                self, "Switch to LIVE trading",
                f"This enables REAL MONEY trading on your connected broker account.\n\n"
                f"Type {LIVE_CONFIRM_PHRASE!r} exactly to confirm:",
                QLineEdit.Normal, "",
            )
            if not ok or text.strip() != LIVE_CONFIRM_PHRASE:
                return
        else:
            # Live -> Paper: the safe direction, a normal confirm is enough.
            reply = QMessageBox.question(self, "Switch to Paper mode",
                                          "Switch back to paper (sandbox) trading?")
            if reply != QMessageBox.Yes:
                return

        success, msg = self._client().set_analyzer_mode(not self._analyzer_on)
        (QMessageBox.information if success else QMessageBox.warning)(self, "Mode switch", msg)
        self.refresh()
