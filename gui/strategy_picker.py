"""
==========================================================
TradeSuite
gui/strategy_picker.py
==========================================================

Lets the customer choose which strategy/strategies to run (independent
on/off, they can run both at once -- same as they already do today as
separate scheduled tasks) and start/stop each. ORB also exposes the
exit-rule toggle from the plan: the original 2-bar-reversal exit vs. the
pct_3 giveback-buffer candidate, since pct_3 is still being live-compared
against actual and isn't a declared winner yet.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QPushButton, QLabel,
    QRadioButton, QButtonGroup, QMessageBox, QSpinBox,
)

from engine.config_store import ConfigStore
from engine.process_manager import ProcessManager
from gui.labels import ORB_LABEL, TAMIL_LABEL


class StrategyBox(QGroupBox):
    def __init__(self, title: str, strategy_key: str, config: ConfigStore, process_manager: ProcessManager):
        super().__init__(title)
        self.strategy_key = strategy_key
        self.config = config
        self.pm = process_manager

        layout = QVBoxLayout(self)

        underlyings = config.settings[strategy_key]["underlyings"]
        names = ", ".join(u["name"] for u in underlyings)
        layout.addWidget(QLabel(f"Trades: {names} options only."))

        # Position size, expressed in LOTS -- never a free-form quantity.
        # Index options only trade in whole exchange lots, so offering a
        # raw quantity box would let a customer type a number the exchange
        # simply rejects. The per-underlying lot size is the contract spec
        # and stays fixed; this multiplies it.
        lots_row = QHBoxLayout()
        lots_row.addWidget(QLabel("Lots per trade:"))
        self.lots_spin = QSpinBox()
        self.lots_spin.setRange(1, 100)
        self.lots_spin.setValue(config.lots(strategy_key))
        self.lots_spin.valueChanged.connect(self._on_lots_changed)
        lots_row.addWidget(self.lots_spin)
        self.lots_hint = QLabel()
        lots_row.addWidget(self.lots_hint)
        lots_row.addStretch()
        layout.addLayout(lots_row)

        self.status_label = QLabel()
        layout.addWidget(self.status_label)
        self._refresh_lots_hint()

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.stop_btn = QPushButton("Stop")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        layout.addLayout(btn_row)

        self.refresh()

    def _start(self):
        if not self.config.is_broker_configured():
            QMessageBox.warning(self, "Broker not set up", "Connect your broker via OpenAlgo before starting a strategy.")
            return
        self.pm.start(self.strategy_key)
        # Remembered as "chosen" so a future launch (see MainWindow's
        # auto-start) starts this strategy on its own without the button
        # being clicked again -- persists across stop/close since a manual
        # stop mid-day (e.g. autoclose) shouldn't un-choose it for tomorrow.
        self.config.set_strategy_enabled(self.strategy_key, True)
        self.refresh()

    def _stop(self):
        self.pm.stop(self.strategy_key)
        self.refresh()

    def _lot_key(self) -> str:
        return "lot_size" if self.strategy_key == "orb" else "lot"

    def _refresh_lots_hint(self):
        lots = self.lots_spin.value()
        parts = [f"{u['name']} {u[self._lot_key()] * lots}"
                 for u in self.config.settings[self.strategy_key]["underlyings"]]
        self.lots_hint.setText(f"<i>= quantity {', '.join(parts)}</i>")

    def _on_lots_changed(self, value: int):
        self.config.set_lots(self.strategy_key, value)
        self._refresh_lots_hint()

    def refresh(self):
        running = self.pm.is_running(self.strategy_key)
        self.status_label.setText("Status: RUNNING" if running else "Status: stopped")
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        # Size changes mid-run would not affect an already-open position and
        # would silently disagree with what the running engine read at start.
        self.lots_spin.setEnabled(not running)


class OrbStrategyBox(StrategyBox):
    def __init__(self, config: ConfigStore, process_manager: ProcessManager):
        super().__init__(ORB_LABEL, "orb", config, process_manager)

        exit_row = QHBoxLayout()
        exit_row.addWidget(QLabel("Exit rule:"))
        self.reversal_radio = QRadioButton("2-candle reversal (proven default)")
        self.pct3_radio = QRadioButton("PCT giveback buffer (experimental)")
        group = QButtonGroup(self)
        group.addButton(self.reversal_radio)
        group.addButton(self.pct3_radio)
        exit_row.addWidget(self.reversal_radio)
        exit_row.addWidget(self.pct3_radio)
        self.layout().addLayout(exit_row)

        current = config.orb_settings().get("exit_mode", "reversal")
        (self.pct3_radio if current == "pct_3" else self.reversal_radio).setChecked(True)
        self.reversal_radio.toggled.connect(self._on_exit_mode_changed)

    def _on_exit_mode_changed(self):
        if self.pm.is_running("orb"):
            return  # changing mid-run would affect an already-open position's monitor logic; apply on next start
        mode = "reversal" if self.reversal_radio.isChecked() else "pct_3"
        self.config.set_orb_exit_mode(mode)


class StrategyPicker(QWidget):
    def __init__(self, config: ConfigStore, process_manager: ProcessManager):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Launch offer: ₹1999/month.</b> TradeSuite currently trades "
                                 "NIFTY, BANKNIFTY, and SENSEX options only."))
        self.orb_box = OrbStrategyBox(config, process_manager)
        self.tamil_box = StrategyBox(TAMIL_LABEL, "tamil", config, process_manager)
        layout.addWidget(self.orb_box)
        layout.addWidget(self.tamil_box)
        layout.addStretch()

    def refresh(self):
        self.orb_box.refresh()
        self.tamil_box.refresh()
