"""
==========================================================
TradeSuite
engine/orb_strategy.py
==========================================================

Productized ORB (volume-imbalance) strategy. Adapted from the original
OrbStrategy's three-script split (paper_trade_daily.py orchestrator ->
paper_trade_entry.py one-shot entry -> paper_trade_exit.py long-running
exit monitor) into one GUI-controllable module.

Signal (unchanged from the original, validated logic): during 09:15-09:25,
sum ATM CE volume and ATM PE volume separately; buy whichever side has
more volume. Strike = ATM (round to nearest step). No index-price
direction used -- pure option-chain volume.

Exit -- now selectable via ConfigStore's orb.exit_mode, per the plan's
decision to expose both rather than pick a single winner prematurely:
  "reversal" (default, the original/proven rule): watches the underlying
      INDEX for a 2-consecutive-adverse-close reversal and exits
      immediately. Backtested net PF 2.73 over 57 trades.
  "pct_3": same 2-bar reversal trigger, but additionally requires the
      OPTION premium to have given back >= 3% of its peak-since-entry
      before exiting -- a candidate from the ongoing shadow-exit-tracker
      comparison (see memory: orb-shadow-exit-tracker), still being
      live-compared against "reversal", not yet a declared winner. Ported
      from shadow_exit_live_tracker.py's already-validated pct_3 logic.

Differences from the original beyond config-injection:
  - GUI-driven, not Task-Scheduler-driven: run_orb_day() can be started at
    any point BEFORE the 09:26 entry window (it waits, exactly as the
    original orchestrator did) or during it, and stops cleanly via a
    threading.Event instead of being killed externally. It will not
    enter after give_up_time.
  - The safety gate is unchanged and must never be weakened: refuses to
    place any order unless OpenAlgo's analyzer/sandbox (paper) mode is on.
==========================================================
"""

from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytz

from engine.openalgo_client import OpenAlgoClient
from engine.config_store import ConfigStore
from engine.trail_tracker import TrailTracker

IST = pytz.timezone("Asia/Kolkata")
STRATEGY_NAME = "ORB_Paper_v1"


def _log(log_fn, msg: str) -> None:
    line = f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} IST] {msg}"
    (log_fn or print)(line)


def _log_change(log_fn, state: dict, key: str, msg: str) -> None:
    """Logs a condition-progress line only when it actually changes.

    The monitor re-evaluates every poll_interval_sec (30s by default), so
    logging each pass unconditionally would bury the handful of lines
    that matter -- the entries, the exits, the moment a condition is
    met -- under hundreds of identical "still holding" repeats. Keyed
    per-symbol so two underlyings can't suppress each other."""
    if state.get(key) == msg:
        return
    state[key] = msg
    _log(log_fn, msg)


def _today_ist() -> date:
    """The calendar date in IST, NOT the host machine's local date.
    date.today()/datetime.now() without a timezone use the machine's own
    local clock -- correct by accident on Gopinath's own server (its
    IST-3:30 offset never crosses midnight during NSE trading hours,
    05:30-12:30 server-local), but genuinely wrong on a customer's machine
    in a different timezone, e.g. one where "now" in IST has already
    rolled to the next calendar day while the host's own local date
    hasn't (or vice versa). Every trading-day decision must go through
    this, not date.today(), for TradeSuite to work correctly regardless
    of what timezone it's installed in."""
    return datetime.now(IST).date()


def _wait_until_ist(target_hhmm: str, log_fn, stop_event, label: str, give_up_hhmm: str | None = None) -> bool:
    """Block until `target_hhmm` IST before doing anything that needs live
    market data. Returns False if the day should be abandoned.

    This exists because both engines are data-driven from the first call:
    started before the market opens, they find no bars, conclude "no
    signal", write a no-trade file and exit FOR THE DAY. That is exactly
    what happened on 2026-08-26 when the app was opened at 08:01 IST --
    silent, looked like a legitimate "no trades today" result, and a
    restart would then resume into an empty day.

    The original Task-Scheduler scripts never hit this because they were
    launched early and waited internally (production's Tamil logs
    "waiting 74.9 min until 09:15 IST"); that wait was lost in the port
    into TradeSuite, which is a regression, not a design choice. A
    customer opening the app in the morning is the NORMAL case and must
    work.

    Waits in short chunks via stop_event so Stop stays responsive.
    """
    hh, mm = (int(x) for x in target_hhmm.split(":"))
    now = datetime.now(IST)
    target = now.replace(hour=hh, minute=mm, second=2, microsecond=0)

    if give_up_hhmm:
        gh, gm = (int(x) for x in give_up_hhmm.split(":"))
        if now > now.replace(hour=gh, minute=gm, second=0, microsecond=0):
            _log(log_fn, f"Too late to start {label} today (past {give_up_hhmm} IST) -- not entering.")
            return False

    if now >= target:
        return True

    wait_min = (target - now).total_seconds() / 60
    _log(log_fn, f"Current IST time {now.strftime('%H:%M:%S')} -- waiting {wait_min:.1f} min "
                  f"until {target_hhmm} IST before {label}.")
    while not stop_event.is_set():
        remaining = (target - datetime.now(IST)).total_seconds()
        if remaining <= 0:
            return True
        stop_event.wait(min(30, remaining))
    _log(log_fn, "Stopped while waiting for the market.")
    return False


def _nearest_strike(spot: float, step: int) -> float:
    return round(spot / step) * step


def _compute_volume_signal(client: OpenAlgoClient, cfg: dict, underlying: dict) -> tuple[str | None, str]:
    today_str = _today_ist().strftime("%Y-%m-%d")
    idx_df = client.get_minute(underlying["index_symbol"], today_str, today_str, exchange=underlying["quote_exchange"])
    if idx_df.empty:
        return None, "no index data for today"

    window_end = cfg["volume_window_end"]
    window_bars = idx_df.between_time(cfg["volume_window_start"], window_end)
    opening = window_bars[window_bars.index.time < pd.Timestamp(window_end).time()]
    if opening.empty:
        return None, f"no {cfg['volume_window_start']}-{window_end} bars yet"

    spot = opening["close"].iloc[-1]
    atm = _nearest_strike(spot, underlying["strike_step"])

    expiry = client.get_nearest_expiry(underlying["name"], underlying["opt_exchange"])
    if expiry is None:
        return None, "could not resolve nearest expiry"

    ce_sym, _ = client.resolve_option_symbol(underlying["name"], expiry, atm, "CE", underlying["opt_exchange"])
    pe_sym, _ = client.resolve_option_symbol(underlying["name"], expiry, atm, "PE", underlying["opt_exchange"])
    if ce_sym is None or pe_sym is None:
        return None, f"could not resolve ATM {atm:.0f} CE/PE symbols"

    ce_df = client.get_minute(ce_sym, today_str, today_str, exchange=underlying["opt_exchange"])
    pe_df = client.get_minute(pe_sym, today_str, today_str, exchange=underlying["opt_exchange"])
    if ce_df.empty or pe_df.empty:
        return None, f"no option minute data yet for ATM {atm:.0f} CE/PE"

    ce_window = ce_df[ce_df.index.time < pd.Timestamp(window_end).time()]
    pe_window = pe_df[pe_df.index.time < pd.Timestamp(window_end).time()]
    ce_vol, pe_vol = ce_window["volume"].sum(), pe_window["volume"].sum()
    if ce_vol <= 0 or pe_vol <= 0:
        return None, f"zero volume on one side (CE={ce_vol:.0f}, PE={pe_vol:.0f})"

    heavier = "CE" if ce_vol > pe_vol else "PE"
    ratio = max(ce_vol, pe_vol) / max(min(ce_vol, pe_vol), 1)
    return heavier, f"{heavier} heavier (CE_vol={ce_vol:.0f} PE_vol={pe_vol:.0f} ratio={ratio:.2f})"


def _run_entries(client: OpenAlgoClient, cfg: dict, log_fn) -> list[dict]:
    today = _today_ist()
    day_log = []
    for underlying in cfg["underlyings"]:
        _log(log_fn, f"--- {underlying['name']} ---")
        option_type, reason = _compute_volume_signal(client, cfg, underlying)
        _log(log_fn, f"  signal: {reason}")
        if option_type is None:
            day_log.append({"date": str(today), "underlying": underlying["name"], "traded": False, "reason": reason})
            continue

        expiry = client.get_nearest_expiry(underlying["name"], underlying["opt_exchange"])
        # lot_size is the exchange's contract size; lots is how many of them
        # the customer wants. Always a whole multiple -- index options can't
        # be traded in part-lots.
        lots = max(1, int(cfg.get("lots", 1)))
        quantity = underlying["lot_size"] * lots
        resp, status = client.place_options_order(
            STRATEGY_NAME, underlying["name"], underlying["quote_exchange"], expiry, option_type, quantity,
        )
        _log(log_fn, f"  order response ({status}): {resp}")
        if resp.get("status") != "success":
            day_log.append({"date": str(today), "underlying": underlying["name"], "traded": False,
                             "reason": f"order failed: {resp.get('message')}"})
            continue

        symbol = resp.get("symbol")
        entry_price = client.get_quote(symbol, underlying["opt_exchange"])
        day_log.append({
            "date": str(today), "underlying": underlying["name"], "traded": True,
            "symbol": symbol, "exchange": underlying["opt_exchange"], "option_type": option_type,
            "quantity": quantity, "lots": lots, "entry_time": datetime.now(IST).isoformat(),
            "entry_price": entry_price, "entry_orderid": resp.get("orderid"),
            "underlying_ltp_at_entry": resp.get("underlying_ltp"), "signal_detail": reason,
            "exited": False,
        })
    return day_log


_INDEX_MAP_KEYS = ("index_symbol", "quote_exchange")


def _underlying_lookup(cfg: dict) -> dict:
    return {u["name"]: u for u in cfg["underlyings"]}


def _exit_position(client: OpenAlgoClient, trade: dict, log_fn, exit_mode: str) -> None:
    _log(log_fn, f"  EXIT TRIGGERED: {trade['symbol']} ({trade['exchange']}) qty={trade['quantity']}")
    resp, status = client.place_order(STRATEGY_NAME, trade["symbol"], trade["exchange"], trade["quantity"], "SELL")
    _log(log_fn, f"    order response ({status}): {resp}")

    exit_price = client.get_quote(trade["symbol"], trade["exchange"])
    entry_price = trade.get("entry_price")
    trade["exited"] = True
    trade["exit_time"] = datetime.now(IST).isoformat()
    trade["exit_price"] = exit_price
    trade["exit_orderid"] = resp.get("orderid")
    trade["exit_status"] = resp.get("status")
    # Which exit rule actually closed this trade -- needed by
    # engine/shadow_compare.py to know which mode to compare AGAINST
    # (a customer can switch modes day to day, so this can't just be
    # read from the current config at display time).
    trade["exit_mode"] = exit_mode

    if entry_price is not None and exit_price is not None:
        gross = (exit_price - entry_price) * trade["quantity"]
        net = gross - trade.get("_flat_cost", 60)
        trade["gross_pnl"] = round(gross, 2)
        trade["net_pnl"] = round(net, 2)
        _log(log_fn, f"    entry={entry_price} exit={exit_price} gross_pnl={gross:.2f} net_pnl={net:.2f}")


def _run_exit_monitor(client: OpenAlgoClient, cfg: dict, day_log: list[dict], outfile: Path,
                       log_fn, stop_event: threading.Event,
                       manual_exit_event: threading.Event | None = None,
                       trail_study_dir: Path | None = None) -> None:
    underlyings = _underlying_lookup(cfg)
    exit_mode = cfg.get("exit_mode", "reversal")
    reversal_bars = cfg["reversal_bars"]
    max_hold_min = cfg["max_hold_min"]
    poll_interval = cfg["poll_interval_sec"]
    flat_cost = cfg["flat_cost_per_lot"]

    for t in day_log:
        if t.get("traded"):
            t["_flat_cost"] = flat_cost
            t["_peak"] = t.get("entry_price") or 0.0

    start = datetime.now(IST)
    deadline_sec = max_hold_min * 60
    last_state: dict[str, str] = {}  # per-symbol dedupe for the live condition narrative
    # Observation only -- scores group-level trailing-stop variants against
    # today's real path so the idea can be judged on measured data later.
    # Never places, changes or cancels an order.
    trail = TrailTracker("ORB", str(_today_ist()))
    _log(log_fn, f"Monitoring for {reversal_bars}-candle index reversal (mode={exit_mode}), "
                  f"backstop at {max_hold_min} min, checking every {poll_interval}s...")

    while not stop_event.is_set():
        open_trades = [t for t in day_log if t.get("traded") and not t.get("exited")]
        if not open_trades:
            _log(log_fn, "All positions closed.")
            break

        # Record the combined mark-to-market of everything still open, and
        # keep the day's maximum. This is the real measurement behind any
        # future "close the day at +X" rule -- reconstructing it afterwards
        # from historical bars only works while the contracts are still
        # fetchable, which for these weekly options is a few days at best.
        open_mtm = 0.0
        for trade in open_trades:
            ltp = client.get_quote(trade["symbol"], trade["exchange"])
            if ltp is not None and trade.get("entry_price") is not None:
                open_mtm += (ltp - trade["entry_price"]) * trade["quantity"] - flat_cost
        booked = sum(t.get("net_pnl") or 0 for t in day_log if t.get("exited"))
        combined = booked + open_mtm
        for t in day_log:
            if t.get("traded"):
                t["day_peak_combined"] = round(max(t.get("day_peak_combined", combined), combined), 2)
        trail.update(combined)
        _log_change(log_fn, last_state, "_daypeak", f"  group P&L: {trail.summary_line()}")

        if manual_exit_event is not None and manual_exit_event.is_set():
            _log(log_fn, f"MANUAL EXIT requested -- closing {len(open_trades)} open position(s) at "
                          f"combined P&L {combined:+.2f}")
            for trade in open_trades:
                _exit_position(client, trade, log_fn, exit_mode)
                trade["exit_reason"] = "manual"
            manual_exit_event.clear()
            continue

        elapsed = (datetime.now(IST) - start).total_seconds()
        if elapsed >= deadline_sec:
            _log(log_fn, f"Backstop hit ({max_hold_min} min) -- force-exiting remaining open positions.")
            for trade in open_trades:
                _exit_position(client, trade, log_fn, exit_mode)
            break

        for trade in open_trades:
            underlying = underlyings.get(trade["underlying"])
            if underlying is None:
                continue
            opt_type = trade["option_type"]
            adverse_sign = -1 if opt_type == "CE" else 1

            key = trade["symbol"]
            name = trade["underlying"]
            today_str = _today_ist().strftime("%Y-%m-%d")
            idx_df = client.get_minute(underlying["index_symbol"], today_str, today_str, exchange=underlying["quote_exchange"])
            if idx_df.empty:
                _log_change(log_fn, last_state, key, f"  {name}: no index candles back from OpenAlgo yet -- waiting")
                continue
            entry_time = pd.Timestamp(trade["entry_time"])
            if entry_time.tzinfo is None:
                entry_time = entry_time.tz_localize(idx_df.index.tz)
            future = idx_df[idx_df.index > entry_time]
            if len(future) < reversal_bars:
                _log_change(log_fn, last_state, key,
                             f"  {name}: {len(future)} candle(s) since entry, need {reversal_bars} before the "
                             f"exit rule can trigger -- holding")
                continue

            # One quote per trade per poll, used for both the peak tracking
            # and the live narrative below (the reversal mode didn't used to
            # need it, but showing the customer the LTP it's watching is the
            # whole point of the activity log).
            opt_now = client.get_quote(trade["symbol"], trade["exchange"])
            if opt_now is not None and exit_mode == "pct_3":
                trade["_peak"] = max(trade["_peak"], opt_now)

            diffs = future["close"].diff().dropna()
            streak = 0
            for d in diffs:
                streak = streak + 1 if np.sign(d) == adverse_sign else 0

            ltp_txt = f"{opt_now:.2f}" if opt_now is not None else "?"
            entry_txt = f"{trade.get('entry_price'):.2f}" if trade.get("entry_price") is not None else "?"

            if streak >= reversal_bars and exit_mode == "reversal":
                _log(log_fn, f"  CONDITION MET -- {name} {key}: {streak} consecutive adverse index candles "
                              f"({opt_type} position), ltp={ltp_txt} vs entry {entry_txt}")
                _exit_position(client, trade, log_fn, exit_mode)
                last_state.pop(key, None)
            elif exit_mode == "reversal":
                _log_change(log_fn, last_state, key,
                             f"  {name} {key}: ltp={ltp_txt} (entry {entry_txt}) -- adverse candle streak "
                             f"{streak}/{reversal_bars}, holding")
            else:  # pct_3
                if opt_now is None:
                    _log_change(log_fn, last_state, key, f"  {name}: no quote for {key} right now -- holding")
                    continue
                giveback = trade["_peak"] - opt_now
                required = trade["_peak"] * cfg.get("pct_3_value", 0.03)
                if streak >= reversal_bars and giveback >= required:
                    _log(log_fn, f"  CONDITION MET -- {name} {key}: {streak} consecutive adverse index candles "
                                  f"AND gave back {giveback:.2f} from peak {trade['_peak']:.2f} "
                                  f"(>= {required:.2f} required), ltp={ltp_txt}")
                    _exit_position(client, trade, log_fn, exit_mode)
                    last_state.pop(key, None)
                elif streak >= reversal_bars:
                    # Signal fired but not enough giveback yet -- keep holding, don't reset the
                    # streak here; the next poll recomputes it fresh from the same index data.
                    _log_change(log_fn, last_state, key,
                                 f"  {name} {key}: {streak} adverse candles BUT giveback {giveback:.2f} "
                                 f"< {required:.2f} required (peak {trade['_peak']:.2f}, ltp={ltp_txt}) -- holding")
                else:
                    _log_change(log_fn, last_state, key,
                                 f"  {name} {key}: ltp={ltp_txt} (entry {entry_txt}, peak {trade['_peak']:.2f}) -- "
                                 f"adverse candle streak {streak}/{reversal_bars}, giveback "
                                 f"{giveback:.2f}/{required:.2f} needed, holding")

        # Strip the private "_"-prefixed working fields out of what gets
        # persisted, then put back EXACTLY what was there. The previous
        # version restored _peak with setdefault() after popping it --
        # which, since the key was gone, silently reset the running peak to
        # the entry price on EVERY poll. That made `giveback = peak - ltp`
        # permanently ~0, so pct_3 could never trigger and every trade fell
        # through to the 45-minute backstop instead (observed live
        # 2026-08-27: all three positions, peak tracking ltp downward).
        # Save-and-restore rather than pop-and-recreate, so the value is
        # carried across writes instead of being reconstructed.
        stashed = {id(t): (t.pop("_flat_cost", None), t.pop("_peak", None)) for t in day_log}
        with open(outfile, "w") as f:
            json.dump(day_log, f, indent=2)
        for t in day_log:
            saved_cost, saved_peak = stashed[id(t)]
            if t.get("traded"):
                t["_flat_cost"] = flat_cost
                t["_peak"] = saved_peak if saved_peak is not None else (t.get("entry_price") or 0.0)

        if any(t.get("traded") and not t.get("exited") for t in day_log):
            stop_event.wait(poll_interval)

    for t in day_log:
        t.pop("_flat_cost", None)
        t.pop("_peak", None)
    with open(outfile, "w") as f:
        json.dump(day_log, f, indent=2)

    legs_traded = sum(1 for t in day_log if t.get("traded"))
    saved = trail.save(trail_study_dir,
                        actual_net=sum(t.get("net_pnl") or 0 for t in day_log if t.get("traded")),
                        legs_traded=legs_traded, legs_total=len(day_log))
    if saved:
        _log(log_fn, f"Trailing-stop study for today written to {saved.name} "
                      f"(peak {trail.peak:+.2f}, {len(trail.curve)} samples, "
                      f"{legs_traded}/{len(day_log)} legs traded)")


def run_orb_day(config: ConfigStore, log_fn=None, stop_event: threading.Event | None = None,
                 manual_exit_event: threading.Event | None = None) -> None:
    """Entry point the GUI calls (in its own thread) when the user starts
    ORB. Start it any time from early morning up to give_up_time: before
    the entry window it waits (see _wait_until_ist), inside it enters
    immediately, after give_up_time it declines rather than entering
    late."""
    stop_event = stop_event or threading.Event()
    cfg = config.orb_settings()
    client = OpenAlgoClient(config.openalgo_host, config.openalgo_api_key, config.openalgo_exchange)
    today = _today_ist()

    ok, reason = client.is_trading_day(today)
    if not ok:
        _log(log_fn, f"Skipping: {reason}")
        return

    if not client.is_analyzer_mode_on():
        _log(log_fn, "ABORT: analyzer/sandbox mode is OFF -- refusing to place orders (would be LIVE/real). "
                      "Enable sandbox mode in OpenAlgo before starting this strategy.")
        return

    outfile = config.trades_dir / f"{today}.json"
    if not outfile.exists():
        # Only gate a FRESH day. A file already on disk means entries ran
        # (or were correctly skipped) earlier, so this is a resume and must
        # go straight to monitoring rather than waiting for a window that
        # has already passed.
        if not _wait_until_ist(cfg["entry_target_time"], log_fn, stop_event,
                                "the ORB entry window", give_up_hhmm=cfg.get("give_up_time")):
            return

    if outfile.exists():
        with open(outfile) as f:
            day_log = json.load(f)
        _log(log_fn, f"Resuming -- loaded {len(day_log)} existing record(s) from {outfile}")
        if not any(t.get("traded") for t in day_log):
            _log(log_fn, "No trades entered yet today -- nothing to resume into monitoring; re-run entries manually if needed.")
    else:
        _log(log_fn, f"=== ORB entry run for {today} ===")
        day_log = _run_entries(client, cfg, log_fn)
        with open(outfile, "w") as f:
            json.dump(day_log, f, indent=2)
        _log(log_fn, f"Wrote {outfile}")

    if any(t.get("traded") and not t.get("exited") for t in day_log):
        _run_exit_monitor(client, cfg, day_log, outfile, log_fn, stop_event, manual_exit_event,
                           trail_study_dir=config.trail_study_dir)

    _log(log_fn, "=== ORB done for today ===")
