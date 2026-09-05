"""
==========================================================
TradeSuite
gui/trade_log_panel.py
==========================================================

Live trade log: polls today's per-day JSON files that both strategy
engines rewrite in place as positions open/close (same files
daily_pnl_report.py / tamil_option_seller_pnl_report.py read for
end-of-day reports), and shows entries/exits as they happen. No push
events exist for this (confirmed: OpenAlgo itself has none either), so
polling is the correct approach here, not a shortcoming.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytz
from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem, QLabel, QPushButton, QMessageBox,
)

from engine.config_store import ConfigStore
from engine.openalgo_client import OpenAlgoClient
from engine.process_manager import ProcessManager
from gui.labels import ORB_SHORT, TAMIL_SHORT
from gui.table_utils import enable_table_copy

IST = pytz.timezone("Asia/Kolkata")
POLL_MS = 5_000
COLUMNS = ["Date", "Strategy", "Underlying", "Type", "Entry", "Entry Price",
           "LTP", "Exit", "Exit Price", "P&L", "Status"]

PROFIT_COLOR = QColor(220, 245, 220)   # soft green
LOSS_COLOR = QColor(255, 224, 224)     # soft red
OPEN_COLOR = QColor(255, 247, 214)     # soft amber -- still live, nothing booked yet

NL = chr(10)  # newline for multi-line dialog text


def _read_day(path) -> list[dict]:
    if not path.exists():
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


class TradeLogPanel(QWidget):
    def __init__(self, config: ConfigStore, process_manager: ProcessManager | None = None):
        super().__init__()
        self.config = config
        self.pm = process_manager
        self._quote_cache: dict[str, float] = {}
        self._open_strategies: set[str] = set()

        layout = QVBoxLayout(self)
        header_row = QHBoxLayout()
        header_row.addWidget(QLabel("<b>Today's trades</b> (paper/sandbox mode)"))
        header_row.addStretch()
        self.live_label = QLabel("")
        header_row.addWidget(self.live_label)
        self.close_btn = QPushButton("Close open positions now")
        self.close_btn.clicked.connect(self._close_positions)
        self.close_btn.setEnabled(False)
        header_row.addWidget(self.close_btn)
        layout.addLayout(header_row)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        copy_all_btn = enable_table_copy(self.table)  # Ctrl+C for a selection, this button for everything
        header_row.addWidget(copy_all_btn)
        layout.addWidget(self.table)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(POLL_MS)
        self.refresh()

    def _fetch_ltps(self, open_rows) -> dict:
        """One quote per open position per refresh. Falls back to the last
        known price on a failed fetch rather than blanking the column -- a
        transient quote error should not make a live position look like it
        has no price at all."""
        if not open_rows or not self.config.is_broker_configured():
            return {}
        client = OpenAlgoClient(self.config.openalgo_host, self.config.openalgo_api_key,
                                 self.config.openalgo_exchange)
        for _, t in open_rows:
            sym, exch = t.get("symbol"), t.get("exchange")
            if not sym or not exch:
                continue
            ltp = client.get_quote(sym, exch)
            if ltp is not None:
                self._quote_cache[sym] = ltp
        return dict(self._quote_cache)

    def _close_positions(self):
        """Hands the request to the RUNNING ENGINE rather than placing exit
        orders from the GUI. The engine owns the in-memory position state,
        so a GUI-side sell would race its monitor and could double-sell the
        same position."""
        if self.pm is None:
            return
        reply = QMessageBox.question(
            self, "Close open positions",
            "Place exit orders for every open position right now?" + NL + NL +
            "The running strategy does the closing, so its own exit rule stops "
            "watching them. Positions already closed are unaffected.")
        if reply != QMessageBox.Yes:
            return
        asked = [name for key, name in (("orb", ORB_SHORT), ("tamil", TAMIL_SHORT))
                 if name in self._open_strategies and self.pm.request_manual_exit(key)]
        if asked:
            QMessageBox.information(self, "Closing",
                                     "Exit requested for: " + ", ".join(asked) + "." + NL +
                                     "Watch the Activity tab for the orders.")
        else:
            QMessageBox.warning(self, "Nothing to close",
                                 "No matching strategy is running -- a strategy has to be running to "
                                 "close its positions, since it owns them.")

    def refresh(self):
        # Must match the engines' own IST-based filename, not the host
        # machine's local date -- see engine/orb_strategy.py's _today_ist().
        today = datetime.now(IST).date()
        rows = []
        for t in _read_day(self.config.trades_dir / f"{today}.json"):
            if t.get("traded"):
                rows.append((ORB_SHORT, t))
        for t in _read_day(self.config.tamil_trades_dir / f"{today}.json"):
            if t.get("entered"):
                rows.append((TAMIL_SHORT, t))

        open_rows = [(s, t) for s, t in rows if not t.get("exited")]
        ltps = self._fetch_ltps(open_rows)

        booked = sum(t.get("net_pnl") or 0 for _, t in rows if t.get("exited"))
        unrealised = 0.0
        flat_cost = self.config.orb_settings().get("flat_cost_per_lot", 60)

        self.table.setRowCount(len(rows))
        for i, (strategy, t) in enumerate(rows):
            is_open = not t.get("exited")
            entry = t.get("entry_price")
            qty = t.get("quantity") or 0
            ltp = ltps.get(t.get("symbol")) if is_open else None

            if is_open and ltp is not None and entry is not None and qty:
                pnl = (ltp - entry) * qty - flat_cost
                unrealised += pnl
            else:
                pnl = t.get("net_pnl")

            values = [
                t.get("date", str(today)),
                strategy,
                t.get("underlying", ""),
                t.get("option_type", ""),
                (t.get("entry_time") or "")[11:19],
                f"{entry:.2f}" if entry is not None else "",
                f"{ltp:.2f}" if ltp is not None else "",
                "OPEN" if is_open else (t.get("exit_time") or "")[11:19],
                f"{t.get('exit_price'):.2f}" if t.get("exit_price") is not None else "",
                f"{pnl:+.2f}" if pnl is not None else "",
                ("LIVE" if is_open else (t.get("exit_reason") or "closed")),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                # Colour the P&L cell by outcome; an open row stays amber
                # whatever its sign, because nothing is banked until it closes.
                if col == 9 and pnl is not None:
                    item.setBackground(OPEN_COLOR if is_open else (PROFIT_COLOR if pnl > 0 else LOSS_COLOR))
                self.table.setItem(i, col, item)

        total = booked + unrealised
        self.live_label.setText(
            f"booked {booked:+,.2f} | open {unrealised:+,.2f} | <b>day {total:+,.2f}</b>")
        self._open_strategies = {st for st, _ in open_rows}
        self.close_btn.setEnabled(bool(open_rows) and self.pm is not None)
