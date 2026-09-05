"""
==========================================================
TradeSuite
gui/setup_wizard.py
==========================================================

First-run broker setup. Hands off to OpenAlgo's OWN already-built web UI
for most of this (its /setup wizard and /broker connect/TOTP flow) rather
than re-building credential-entry screens from scratch -- and for the
legal reason in the plan: OpenAlgo is fetched fresh per-install by
openalgo_bootstrap.py (see engine/openalgo_manager.py), a genuinely
separate AGPLv3 component, not something merged into TradeSuite's own
code.

Flow, corrected 2026-08-24 after hitting the real sequencing live:
OpenAlgo hard-refuses to even START without a validly-formatted broker
API key already in its .env (confirmed: "Error: Invalid Flattrade API
key format detected!" fires at startup, before the web server -- and so
/setup and /broker -- is reachable at all). So broker credentials have
to be collected by TradeSuite itself BEFORE first bootstrap, not
through OpenAlgo's web UI afterward as originally designed.
  Step 1 (new): enter Flattrade client ID / API key / API secret here.
  Step 2: bootstrap + start OpenAlgo (needs step 1 done first).
  Step 3: open /setup, create OpenAlgo's own local admin account, copy its API key.
  Step 4: paste that key here (TradeSuite <-> OpenAlgo's own auth, separate from step 1's broker key).
  Step 5: open /broker to complete the actual broker login/TOTP session.
  Step 6: Test Connection.
"""

from __future__ import annotations

import webbrowser

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QMessageBox, QTextEdit, QFormLayout,
)

from engine.config_store import ConfigStore
from engine.openalgo_client import OpenAlgoClient
from engine.openalgo_manager import OpenAlgoManager

LOG_POLL_MS = 1_000


class SetupWizard(QWidget):
    def __init__(self, config: ConfigStore, openalgo_manager: OpenAlgoManager):
        super().__init__()
        self.config = config
        self.oam = openalgo_manager

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "<h2>Connect your broker</h2>"
            "TradeSuite runs its own local copy of OpenAlgo (open-source, AGPLv3) to talk to your "
            "broker -- your broker login never touches TradeSuite itself."
        ))

        layout.addWidget(QLabel(
            "<b>Step 1.</b> Enter your broker's name and API credentials "
            "(from your broker's own developer/API portal). <small>Only Flattrade's "
            "credential format has been fully verified in this app -- for another broker, "
            "use the exact broker code from "
            "<a href='https://docs.openalgo.in'>docs.openalgo.in</a> and check the fields "
            "match what OpenAlgo expects for it.</small>"))
        cred_form = QFormLayout()
        self.broker_name_input = QLineEdit("flattrade")
        self.client_id_input = QLineEdit()
        self.broker_key_input = QLineEdit()
        self.broker_key_input.setEchoMode(QLineEdit.Password)
        self.broker_secret_input = QLineEdit()
        self.broker_secret_input.setEchoMode(QLineEdit.Password)
        cred_form.addRow("Broker", self.broker_name_input)
        cred_form.addRow("Client ID", self.client_id_input)
        cred_form.addRow("API Key", self.broker_key_input)
        cred_form.addRow("API Secret", self.broker_secret_input)
        layout.addLayout(cred_form)
        save_creds_btn = QPushButton("Save Credentials")
        save_creds_btn.clicked.connect(self._save_broker_credentials)
        layout.addWidget(save_creds_btn)

        layout.addWidget(QLabel("<b>Step 2.</b> Set up and start OpenAlgo (first time only takes a few minutes):"))
        step2_row = QHBoxLayout()
        self.setup_btn = QPushButton("Set Up OpenAlgo")
        self.setup_btn.clicked.connect(self._start_bootstrap)
        self.start_btn = QPushButton("Start OpenAlgo")
        self.start_btn.clicked.connect(self._start_process)
        step2_row.addWidget(self.setup_btn)
        step2_row.addWidget(self.start_btn)
        layout.addLayout(step2_row)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(100)
        layout.addWidget(self.log_view)

        layout.addWidget(QLabel("<b>Step 3.</b> Open OpenAlgo's setup page and create your local admin account. "
                                 "It will show you an API key -- copy it."))
        open_setup_btn = QPushButton("Open OpenAlgo Setup")
        open_setup_btn.clicked.connect(self._open_setup)
        layout.addWidget(open_setup_btn)

        layout.addWidget(QLabel("<b>Step 4.</b> Paste that API key here (this is OpenAlgo's own key for "
                                 "TradeSuite to talk to it -- different from your broker API key above):"))
        key_row = QHBoxLayout()
        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.Password)
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._save_key)
        key_row.addWidget(self.key_input)
        key_row.addWidget(save_btn)
        layout.addLayout(key_row)

        layout.addWidget(QLabel("<b>Step 5.</b> Complete your broker login/session through OpenAlgo's broker page:"))
        open_broker_btn = QPushButton("Open Broker Connect")
        open_broker_btn.clicked.connect(self._open_broker)
        layout.addWidget(open_broker_btn)

        layout.addWidget(QLabel("<b>Step 6.</b> Confirm everything actually works end-to-end:"))
        test_row = QHBoxLayout()
        test_btn = QPushButton("Test Connection")
        test_btn.clicked.connect(self._test_connection)
        self.test_result_label = QLabel("")
        test_row.addWidget(test_btn)
        test_row.addWidget(self.test_result_label)
        test_row.addStretch()
        layout.addLayout(test_row)

        layout.addWidget(QLabel("Once connected, check the status strip above -- it should turn green "
                                 "and show your broker is connected in PAPER (sandbox) mode."))
        layout.addStretch()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(LOG_POLL_MS)
        self._poll()

    def _save_broker_credentials(self):
        broker_name = self.broker_name_input.text().strip()
        client_id = self.client_id_input.text().strip()
        api_key = self.broker_key_input.text().strip()
        api_secret = self.broker_secret_input.text().strip()
        if not (broker_name and client_id and api_key and api_secret):
            QMessageBox.warning(self, "Missing details", "All four fields are required.")
            return
        self.config.set_broker_credentials(client_id, api_key, api_secret, name=broker_name)
        QMessageBox.information(self, "Saved", "Broker credentials saved. Now set up OpenAlgo (step 2).")

    def _start_bootstrap(self):
        if self.oam.is_bootstrapped():
            QMessageBox.information(self, "Already set up", "OpenAlgo is already set up -- use Start OpenAlgo.")
            return
        if not self.config.is_broker_credentials_set():
            QMessageBox.warning(self, "Missing credentials", "Enter and save your broker credentials first (step 1) "
                                                                "-- OpenAlgo can't start without them.")
            return
        self.oam.start_bootstrap_async()

    def _start_process(self):
        try:
            self.oam.start_process()
        except RuntimeError as ex:
            QMessageBox.warning(self, "Not ready", str(ex))

    def _poll(self):
        for line in self.oam.drain_logs():
            self.log_view.append(line)
        self.setup_btn.setEnabled(not self.oam.is_bootstrapped() and not self.oam.is_bootstrapping())
        self.start_btn.setEnabled(self.oam.is_bootstrapped() and not self.oam.is_running())

    def _open_setup(self):
        if not self.oam.is_running():
            QMessageBox.warning(self, "Not running", "Start OpenAlgo first (step 2).")
            return
        webbrowser.open(f"{self.config.openalgo_host}/setup")

    def _open_broker(self):
        if not self.oam.is_running():
            QMessageBox.warning(self, "Not running", "Start OpenAlgo first (step 2).")
            return
        webbrowser.open(f"{self.config.openalgo_host}/broker")

    def _save_key(self):
        key = self.key_input.text().strip()
        if not key:
            QMessageBox.warning(self, "Missing key", "Paste the API key from OpenAlgo's setup page first.")
            return
        self.config.set_openalgo(api_key=key)
        QMessageBox.information(self, "Saved", "API key saved. Now complete your broker login (step 5).")

    def _test_connection(self):
        if not self.config.is_broker_configured():
            self.test_result_label.setText("❌ No API key saved yet (step 4)")
            return
        client = OpenAlgoClient(self.config.openalgo_host, self.config.openalgo_api_key, self.config.openalgo_exchange)
        ok, msg = client.test_connection()
        self.test_result_label.setText(("✅ " if ok else "❌ ") + msg)
