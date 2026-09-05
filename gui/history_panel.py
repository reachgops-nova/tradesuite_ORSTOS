"""
==========================================================
TradeSuite
gui/history_panel.py
==========================================================

Historical trade report: scans every saved day's JSON (not just today,
which trade_log_panel.py already covers) and shows the full trade history
with per-strategy totals -- meant to be the customer's ongoing source of
truth for "how are my strategies doing," not something they need to ask
for separately. Self-contained on purpose -- the original FlattradeLive
scripts' *_pnl_report.py generators aren't part of what shipped into
TradeSuite, so this reads the same per-day JSON files those scripts read,
directly, rather than depending on a report generator that doesn't exist
in a customer's install.

For ORB trades specifically, also runs a background "what would the
OTHER exit mode have done" reconstruction (engine/shadow_compare.py)
against real historical bars, cached back onto the trade's own JSON
record so it's a one-time computation per trade, not refetched every
poll. Purely a reference comparison -- never affects the actual trade,
never places an order.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QTableWidget, QTableWidgetItem,
)

from engine.config_store import ConfigStore
from engine.openalgo_client import OpenAlgoClient
from engine.shadow_compare import simulate_other_mode, OTHER_MODE
from gui.labels import ORB_SHORT, TAMIL_SHORT, exit_mode_label
from gui.table_utils import enable_table_copy

IST = pytz.timezone("Asia/Kolkata")
POLL_MS = 30_000
ENRICH_POLL_MS = 10_000
STAT_COLUMNS = ["Strategy", "Trades", "Wins", "Losses", "Win %", "Profit Factor",
                "Gross Profit", "Gross Loss", "Net P&L", "Avg Win", "Avg Loss", "Best", "Worst"]
ORB_EXIT_COLUMNS = ["Compared trades", "2-candle Net", "2-candle Win %", "PCT Net", "PCT Win %",
                    "Leading", "Currently running", "Not comparable"]
COLUMNS = ["Date", "Strategy", "Underlying", "Strike", "Type", "Entry", "Entry Price",
           "Exit", "Exit Price", "Profit/Lot", "Net P&L", "2-candle Net P&L", "PCT Net P&L"]
COL_NET, COL_2CANDLE, COL_PCT = 10, 11, 12  # named, not magic, after the peak-reset bug taught that lesson

_STRIKE_RE = re.compile(r"\d{2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{2}(\d+)(?:CE|PE)$")

MISSED_OUT_COLOR = QColor(255, 235, 205)  # soft amber -- the chosen mode left money on the table
BETTER_COLOR = QColor(220, 245, 220)      # soft green -- the chosen mode was actually the better one


def _extract_strike(symbol: str) -> str:
    """Pulls the strike out of an OpenAlgo option symbol, e.g.
    'NIFTY04AUG2624550CE' -> '24550'. Anchored on the 3-letter month code
    (DDMONYY) rather than just grabbing trailing digits, since the
    expiry's own year digits sit immediately next to the strike with no
    separator -- a naive trailing-digit grab would misparse "2624550" as
    one number instead of year "26" + strike "24550"."""
    if not symbol:
        return ""
    m = _STRIKE_RE.search(symbol)
    return m.group(1) if m else ""


def _actual_exit_mode(trade: dict) -> str:
    """Which exit rule actually closed this ORB trade.

    Prefers deriving it from the cached shadow_mode rather than reading
    exit_mode directly: shadow_mode is by definition the OTHER mode from
    whatever the enrichment pass assumed, so inverting it can never
    disagree with the cached shadow P&L sitting next to it. Trades
    recorded before exit_mode existed as a field (the backfilled August
    production trades) fall back to "reversal", which is what production
    genuinely ran on -- NOT to the current config's exit_mode, which the
    customer may have since switched.
    """
    shadow_mode = trade.get("shadow_mode")
    if shadow_mode in OTHER_MODE:
        return OTHER_MODE[shadow_mode]
    return trade.get("exit_mode") or "reversal"


def _orb_mode_totals(orb_trades: list[dict]) -> dict:
    """Splits ORB's closed trades into what the 2-candle rule made vs.
    what the PCT rule made, over the SAME set of trades -- one side is
    the real recorded result, the other the reconstruction from
    shadow_compare. Trades where the other side couldn't be
    reconstructed (expired contract, no history) are excluded from both
    totals rather than counted as zero, so the two figures always
    compare like with like."""
    totals = {"reversal": 0.0, "pct_3": 0.0, "compared": 0, "not_comparable": 0}
    for t in orb_trades:
        net, shadow_net = t.get("net_pnl"), t.get("shadow_net_pnl")
        if net is None:
            continue
        if shadow_net is None:
            totals["not_comparable"] += 1
            continue
        actual = _actual_exit_mode(t)
        other = OTHER_MODE[actual]
        totals[actual] += net
        totals[other] += shadow_net
        totals["compared"] += 1
    return totals


def _metrics(trades: list[dict]) -> dict:
    """Standard trading stats for one strategy.

    Profit factor is gross profit / gross loss -- the ratio that says how
    many rupees are won per rupee lost, independent of trade count. It is
    deliberately reported as "inf" when there are no losses at all rather
    than as a number: dividing by zero would print something like 999
    that reads as a real measurement.
    """
    closed = [t for t in trades if t.get("net_pnl") is not None]
    wins = [t["net_pnl"] for t in closed if t["net_pnl"] > 0]
    losses = [t["net_pnl"] for t in closed if t["net_pnl"] < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(closed) * 100) if closed else 0.0,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": (gross_profit / gross_loss) if gross_loss else (float("inf") if gross_profit else 0.0),
        "net": sum(t["net_pnl"] for t in closed),
        "avg_win": (gross_profit / len(wins)) if wins else 0.0,
        "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
        "best": max(wins) if wins else 0.0,
        "worst": min(losses) if losses else 0.0,
    }


def _fmt_pf(pf: float) -> str:
    if pf == float("inf"):
        return "inf"
    return f"{pf:.2f}"


def _load_day(path: Path) -> list[dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


class HistoryPanel(QWidget):
    def __init__(self, config: ConfigStore):
        super().__init__()
        self.config = config
        self._enrich_thread: threading.Thread | None = None

        layout = QVBoxLayout(self)
        header_row = QHBoxLayout()
        header_row.addWidget(QLabel("<b>Trade History</b>"))
        header_row.addWidget(QLabel("Show:"))
        self.range_combo = QComboBox()
        self.range_combo.addItems(["Last 7 days", "Last 4 weeks", "Last 30 days", "All time"])
        self.range_combo.setCurrentText("Last 7 days")
        self.range_combo.currentIndexChanged.connect(self.refresh)
        header_row.addWidget(self.range_combo)
        header_row.addStretch()
        layout.addLayout(header_row)

        self.leading_label = QLabel("")
        f = self.leading_label.font(); f.setPointSize(f.pointSize() + 1); f.setBold(True)
        self.leading_label.setFont(f)
        layout.addWidget(self.leading_label)

        self.summary_label = QLabel("")
        layout.addWidget(self.summary_label)
        self.stats_table = QTableWidget(0, len(STAT_COLUMNS))
        self.stats_table.setHorizontalHeaderLabels(STAT_COLUMNS)
        self.stats_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.stats_table.setMaximumHeight(140)
        self.stats_copy_btn = enable_table_copy(self.stats_table)
        layout.addWidget(self.stats_table)
        layout.addWidget(QLabel("<b>ORB: 2-candle vs PCT</b> (same closed trades, scored both ways)"))
        self.orb_exit_table = QTableWidget(1, len(ORB_EXIT_COLUMNS))
        self.orb_exit_table.setHorizontalHeaderLabels(ORB_EXIT_COLUMNS)
        self.orb_exit_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.orb_exit_table.setMaximumHeight(70)
        self.orb_exit_table.verticalHeader().setVisible(False)
        enable_table_copy(self.orb_exit_table)
        layout.addWidget(self.orb_exit_table)
        self.legend_label = QLabel(
            '<span style="background-color:#ffebcd">&nbsp;&nbsp;</span> chosen ORB exit mode missed out vs. the other '
            '&nbsp;&nbsp; <span style="background-color:#dcf5dc">&nbsp;&nbsp;</span> chosen mode was the better one'
        )
        layout.addWidget(self.legend_label)

        copy_row = QHBoxLayout()
        copy_row.addStretch()
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        copy_all_btn = enable_table_copy(self.table)
        copy_row.addWidget(copy_all_btn)
        layout.addLayout(copy_row)
        layout.addWidget(self.table)

        self._month_prefixes: dict[str, str] = {}   # "August 2026" -> "2026-08"
        self._refresh_month_options()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(POLL_MS)

        self.enrich_timer = QTimer(self)
        self.enrich_timer.timeout.connect(self._start_enrichment_if_idle)
        self.enrich_timer.start(ENRICH_POLL_MS)

        self.refresh()
        self._start_enrichment_if_idle()

    def _cutoff_days(self) -> int | None:
        text = self.range_combo.currentText()
        if text == "Last 7 days":
            return 7
        if text == "Last 4 weeks":
            return 28
        if text == "Last 30 days":
            return 30
        return None

    def _month_prefix(self) -> str | None:
        """The 'YYYY-MM' this selection means, if a calendar month is
        picked rather than a rolling window."""
        return self._month_prefixes.get(self.range_combo.currentText())

    def _refresh_month_options(self):
        """Scans the trade directories for which calendar months actually
        have data, and offers each as its own entry -- 'August 2026',
        'September 2026' -- so a customer (or Gopinath, comparing his own
        months) can pull up one specific month's consolidated report."""
        months = set()
        for trades_dir in (self.config.trades_dir, self.config.tamil_trades_dir):
            if not trades_dir.exists():
                continue
            for f in trades_dir.glob("*.json"):
                if len(f.stem) >= 7 and f.stem[4] == "-":
                    months.add(f.stem[:7])
        wanted = {datetime.strptime(m, "%Y-%m").strftime("%B %Y"): m for m in sorted(months, reverse=True)}
        if wanted == self._month_prefixes:
            return
        self._month_prefixes = wanted
        current = self.range_combo.currentText()
        self.range_combo.blockSignals(True)
        self.range_combo.clear()
        self.range_combo.addItems(["Last 7 days", "Last 4 weeks", "Last 30 days", "All time"] + list(wanted))
        if self.range_combo.findText(current) >= 0:
            self.range_combo.setCurrentText(current)
        self.range_combo.blockSignals(False)

    # -- background shadow-mode enrichment for ORB trades ------------------

    def _start_enrichment_if_idle(self):
        if self._enrich_thread is not None and self._enrich_thread.is_alive():
            return
        if not self.config.is_broker_configured():
            return  # needs a live OpenAlgo connection to fetch historical bars
        self._enrich_thread = threading.Thread(target=self._enrich_worker, name="tradesuite-shadow-enrich", daemon=True)
        self._enrich_thread.start()

    def _enrich_worker(self):
        client = OpenAlgoClient(self.config.openalgo_host, self.config.openalgo_api_key, self.config.openalgo_exchange)
        cfg = self.config.orb_settings()
        underlyings = {u["name"]: u for u in cfg["underlyings"]}
        if not self.config.trades_dir.exists():
            return

        for day_file in self.config.trades_dir.glob("*.json"):
            try:
                with open(day_file) as f:
                    day_trades = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            changed = False
            for t in day_trades:
                if not t.get("traded") or not t.get("exited"):
                    continue
                if "shadow_net_pnl" in t:
                    continue  # already computed -- closed trades' outcomes never change
                underlying = underlyings.get(t.get("underlying"))
                if underlying is None:
                    continue
                if not t.get("exit_mode"):
                    # Records from before exit_mode was written per-trade (the
                    # backfilled August production trades) -- pin them to what
                    # production actually ran, so the comparison can't silently
                    # invert later just because the customer switched modes.
                    t["exit_mode"] = "reversal"
                result = simulate_other_mode(
                    client, cfg, t, underlying["quote_exchange"], underlying["opt_exchange"], underlying["index_symbol"],
                )
                if result is None:
                    t["shadow_net_pnl"] = None  # tried, genuinely unavailable (e.g. contract expired) -- don't retry every cycle
                    t["shadow_mode"] = OTHER_MODE.get(t.get("exit_mode") or cfg.get("exit_mode", "reversal"))
                else:
                    t["shadow_mode"] = result["mode"]
                    t["shadow_exit_price"] = result["exit_price"]
                    t["shadow_net_pnl"] = result["net_pnl"]
                changed = True

            if changed:
                with open(day_file, "w") as f:
                    json.dump(day_trades, f, indent=2)

    # -- display -------------------------------------------------------

    def _populate_stats(self, rows: list[tuple[str, dict]]):
        """One row per strategy plus an ALL row -- the "how are my
        strategies actually doing" summary, in the standard terms a trader
        already reads (win rate, profit factor, net)."""
        by_strategy: dict[str, list[dict]] = {}
        for label, t in rows:
            by_strategy.setdefault(label, []).append(t)

        # Fixed order rather than sorted(): the two tradeable strategies
        # first, history-only WhatsApp last, ALL at the bottom.
        order = [x for x in (ORB_SHORT, TAMIL_SHORT) if x in by_strategy]
        entries = [(name, _metrics(by_strategy[name])) for name in order]
        if len(entries) > 1:
            entries.append(("ALL", _metrics([t for _, t in rows])))

        self.stats_table.setRowCount(len(entries))
        for i, (name, m) in enumerate(entries):
            values = [
                name, m["trades"], m["wins"], m["losses"], f"{m['win_rate']:.1f}%",
                _fmt_pf(m["profit_factor"]),
                f"{m['gross_profit']:,.2f}", f"{m['gross_loss']:,.2f}", f"{m['net']:+,.2f}",
                f"{m['avg_win']:+,.2f}", f"{m['avg_loss']:+,.2f}",
                f"{m['best']:+,.2f}", f"{m['worst']:+,.2f}",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col == 8:  # Net P&L -- the column people look at first
                    item.setBackground(BETTER_COLOR if m["net"] > 0 else MISSED_OUT_COLOR)
                self.stats_table.setItem(i, col, item)

    def _update_orb_mode_label(self, orb_closed: list[dict]):
        """ORB's 2-candle vs PCT comparison, as a one-row table with the two
        rules in their own columns -- easier to scan at a glance than the
        earlier inline sentence, and still one glance to see which is
        ahead and which one is actually running right now."""
        totals = _orb_mode_totals(orb_closed)
        n = totals["compared"]
        chosen = self.config.orb_settings().get("exit_mode", "reversal")

        if n == 0:
            values = ["0", "--", "--", "--", "--", "--", exit_mode_label(chosen), str(totals["not_comparable"])]
            for col, v in enumerate(values):
                self.orb_exit_table.setItem(0, col, QTableWidgetItem(v))
            self.leading_label.setText("ORB exit rules: not enough closed, comparable trades yet.")
            return

        rev_wins = sum(1 for t in orb_closed if t.get("shadow_net_pnl") is not None
                       and (t["net_pnl"] if _actual_exit_mode(t) == "reversal" else t["shadow_net_pnl"]) > 0)
        pct_wins = sum(1 for t in orb_closed if t.get("shadow_net_pnl") is not None
                       and (t["net_pnl"] if _actual_exit_mode(t) == "pct_3" else t["shadow_net_pnl"]) > 0)
        rev, pct = totals["reversal"], totals["pct_3"]
        lead = "2-candle" if rev > pct else ("PCT" if pct > rev else "level")

        # The prominent top-of-page line -- "who's leading" without having
        # to read the table below at all.
        if lead == "level":
            self.leading_label.setText(f"ORB: 2-candle and PCT are dead level, {rev:+,.2f} each, over {n} compared trades.")
        else:
            self.leading_label.setText(
                f"ORB: {lead} is leading by ₹{abs(rev - pct):,.2f} over {n} compared trades "
                f"(2-candle {rev:+,.2f} vs PCT {pct:+,.2f}).")

        values = [
            str(n),
            f"{rev:+,.2f}", f"{rev_wins/n*100:.1f}%",
            f"{pct:+,.2f}", f"{pct_wins/n*100:.1f}%",
            lead, exit_mode_label(chosen), str(totals["not_comparable"]),
        ]
        for col, v in enumerate(values):
            item = QTableWidgetItem(v)
            if col in (1, 2) and lead == "2-candle":
                item.setBackground(BETTER_COLOR)
            if col in (3, 4) and lead == "PCT":
                item.setBackground(BETTER_COLOR)
            self.orb_exit_table.setItem(0, col, item)
        self.orb_exit_table.resizeColumnsToContents()

    def refresh(self):
        self._refresh_month_options()
        cutoff_days = self._cutoff_days()
        month_prefix = self._month_prefix()
        rows = []

        # WhatsApp Signals deliberately excluded from this report: it isn't
        # a tradeable strategy in TradeSuite (no Start button, depends on
        # Gopinath's own private groups) and Gopinath asked for it out of
        # the reporting entirely, not just off the trailing-stop study.
        for strategy_label, trades_dir, entered_key in (
            (ORB_SHORT, self.config.trades_dir, "traded"),
            (TAMIL_SHORT, self.config.tamil_trades_dir, "entered"),
        ):
            if not trades_dir.exists():
                continue
            for day_file in sorted(trades_dir.glob("*.json"), reverse=True):
                for t in _load_day(day_file):
                    if not t.get(entered_key):
                        continue
                    if not t.get("date"):
                        t = {**t, "date": day_file.stem}
                    rows.append((strategy_label, t))

        if month_prefix is not None:
            rows = [(s, t) for s, t in rows if t.get("date", "").startswith(month_prefix)]
        elif cutoff_days is not None:
            # IST-based cutoff, not the host machine's own local date --
            # see engine/orb_strategy.py's _today_ist() for why this
            # matters once TradeSuite runs outside IST.
            cutoff = (datetime.now(IST).date() - timedelta(days=cutoff_days)).isoformat()
            rows = [(s, t) for s, t in rows if t.get("date", "") >= cutoff]

        rows.sort(key=lambda st: (st[1].get("date", ""), st[1].get("entry_time", "")), reverse=True)

        # History is the CLOSED record -- an open position belongs to the
        # Trade Log tab, which already shows it live with a running P&L.
        # Gopinath, 2026-08-31: "Only closed ... trades will be part of
        # trade history summary." Filtering here rather than just in the
        # stats maths means an open trade also can't sit in the per-trade
        # table below looking like a finished result.
        closed = [(s, t) for s, t in rows if t.get("net_pnl") is not None]
        open_count = len(rows) - len(closed)
        total_net = sum(t.get("net_pnl") or 0 for _, t in closed)
        wins = sum(1 for _, t in closed if t["net_pnl"] > 0)
        win_rate = f"{wins/len(closed)*100:.1f}%" if closed else "--"
        open_note = f" · {open_count} still open (see Trade Log)" if open_count else ""
        self.summary_label.setText(
            f"{len(closed)} closed trades · win rate {win_rate} · net P&L {total_net:+.2f}{open_note}"
        )

        self._populate_stats(closed)

        self._update_orb_mode_label([t for s, t in closed if s == ORB_SHORT])

        self.table.setRowCount(len(closed))
        for i, (strategy, t) in enumerate(closed):
            qty = t.get("quantity") or 0
            net = t.get("net_pnl")
            per_lot = (net / qty) if (net is not None and qty) else None
            shadow_net = t.get("shadow_net_pnl")

            # Gopinath, 2026-08-31: show the closed value under an explicit
            # "2-candle Net P&L" / "PCT Net P&L" heading rather than the
            # generic "Other Exit Mode" + "Other Mode P&L" pair -- one of
            # the two is always the trade's own REAL recorded result, the
            # other the shadow_compare reconstruction; which is which
            # depends on which rule actually closed this specific trade.
            two_candle_val = pct_val = None
            if strategy == ORB_SHORT and net is not None:
                actual_mode = _actual_exit_mode(t)
                if actual_mode == "reversal":
                    two_candle_val = net
                    pct_val = shadow_net
                else:
                    pct_val = net
                    two_candle_val = shadow_net

            values = [
                t.get("date", ""),
                strategy,
                t.get("underlying", ""),
                _extract_strike(t.get("symbol", "")) or (str(int(t["atm"])) if t.get("atm") else ""),
                t.get("option_type", ""),
                (t.get("entry_time") or "")[11:19],
                f"{t.get('entry_price'):.2f}" if t.get("entry_price") is not None else "",
                "OPEN" if not t.get("exited") else (t.get("exit_time") or "")[11:19],
                f"{t.get('exit_price'):.2f}" if t.get("exit_price") is not None else "",
                f"{per_lot:.2f}" if per_lot is not None else "",
                f"{net:.2f}" if net is not None else "",
                f"{two_candle_val:.2f}" if two_candle_val is not None else ("n/a" if strategy == ORB_SHORT else ""),
                f"{pct_val:.2f}" if pct_val is not None else ("n/a" if strategy == ORB_SHORT else ""),
            ]
            for col, value in enumerate(values):
                self.table.setItem(i, col, QTableWidgetItem(str(value)))

            # Highlight whichever of the two was actually better -- only
            # where both are real numbers, never against a placeholder "n/a".
            if strategy == ORB_SHORT and two_candle_val is not None and pct_val is not None:
                self.table.item(i, COL_2CANDLE).setBackground(
                    BETTER_COLOR if two_candle_val >= pct_val else MISSED_OUT_COLOR)
                self.table.item(i, COL_PCT).setBackground(
                    BETTER_COLOR if pct_val >= two_candle_val else MISSED_OUT_COLOR)
