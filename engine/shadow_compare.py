"""
==========================================================
TradeSuite
engine/shadow_compare.py
==========================================================

Post-hoc reconstruction of "what would ORB's OTHER exit mode have done"
for an already-closed trade, using real historical minute bars. Purely
for the History tab's own-reference comparison -- never touches live
trading, never places an order, never affects the actual recorded trade.

Adapted from the proven shadow_exit_comparison.py pattern used
operationally for months on the production ORB strategy (same reversal-
streak + peak-giveback logic, same known limitation: an older option
contract can expire and become unfetchable via OpenAlgo's /history once
it's rolled off, in which case this simply returns None rather than
guessing -- the History tab shows "not available" for those rather than
a fabricated number).

Results are meant to be computed once per trade and cached back onto the
trade's own JSON record (see gui/history_panel.py's background
enrichment) -- fetching historical bars on every UI refresh would be
slow and pointless once a closed trade's outcome can never change.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from engine.openalgo_client import OpenAlgoClient

OTHER_MODE = {"reversal": "pct_3", "pct_3": "reversal"}


def simulate_other_mode(client: OpenAlgoClient, cfg: dict, trade: dict, quote_exchange: str, opt_exchange: str,
                         index_symbol: str) -> dict | None:
    """trade: a closed ORB trade record (entry_time, entry_price, symbol,
    option_type, quantity). Returns {"mode", "exit_price", "exit_reason",
    "net_pnl"} for whichever mode ISN'T trade's own exit_mode, or None if
    the historical data isn't available (expired contract, no data yet,
    etc.) -- callers must handle None as "not available", not as zero."""
    actual_mode = trade.get("exit_mode") or cfg.get("exit_mode", "reversal")
    other_mode = OTHER_MODE[actual_mode]

    entry_time = pd.Timestamp(trade["entry_time"])
    day = entry_time.strftime("%Y-%m-%d")
    opt_type = trade["option_type"]
    adverse_sign = -1 if opt_type == "CE" else 1
    entry_price = trade["entry_price"]
    quantity = trade["quantity"]

    idx = client.get_minute(index_symbol, day, day, exchange=quote_exchange)
    opt = client.get_minute(trade["symbol"], day, day, exchange=opt_exchange)
    if idx.empty or opt.empty:
        return None

    idx_future = idx[idx.index > entry_time]
    opt_future = opt[opt.index > entry_time]
    if idx_future.empty or opt_future.empty:
        return None

    reversal_bars = cfg.get("reversal_bars", 2)
    max_hold_min = cfg.get("max_hold_min", 45)
    pct_3_value = cfg.get("pct_3_value", 0.03)
    flat_cost = cfg.get("flat_cost_per_lot", 60)

    deadline = entry_time + pd.Timedelta(minutes=max_hold_min)
    diffs = idx_future["close"].diff().dropna()

    peak = entry_price
    streak = 0
    exit_time = exit_price = exit_reason = None

    for t, d in diffs.items():
        if t > deadline:
            break
        opt_now = opt_future[opt_future.index <= t]
        if not opt_now.empty:
            peak = max(peak, opt_now["high"].iloc[-1])

        streak = streak + 1 if np.sign(d) == adverse_sign else 0

        if streak >= reversal_bars:
            opt_at_t = opt_future[opt_future.index <= t]
            current_close = opt_at_t["close"].iloc[-1] if not opt_at_t.empty else entry_price

            if other_mode == "reversal":
                exit_time, exit_reason = t, "reversal"
            else:  # pct_3
                giveback = peak - current_close
                required = peak * pct_3_value
                if giveback < required:
                    streak = 0  # signal fired but not enough giveback yet -- keep holding
                    continue
                exit_time, exit_reason = t, "reversal"

            exit_bar = opt[opt.index >= t]
            exit_price = exit_bar["close"].iloc[0] if not exit_bar.empty else current_close
            break

    if exit_time is None:
        last = opt_future[opt_future.index <= deadline]
        if last.empty:
            return None
        exit_price, exit_reason = last["close"].iloc[-1], "45min_backstop"

    gross = (exit_price - entry_price) * quantity
    net = gross - flat_cost
    return {
        "mode": other_mode,
        "exit_price": round(float(exit_price), 2),
        "exit_reason": exit_reason,
        "net_pnl": round(float(net), 2),
    }
