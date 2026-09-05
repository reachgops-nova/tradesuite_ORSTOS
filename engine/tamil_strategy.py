"""
==========================================================
TradeSuite
engine/tamil_strategy.py
==========================================================

Productized Tamil Option Seller 30% strategy. Adapted from the original
TamilOptionSeller30/tamil_option_seller_trader.py.

Rule (unchanged, validated logic): trades Tue/Wed/Thu only. Strike = ATM
(previous day's index close, rounded to nearest step). CE_range =
(CE's-yesterday-low + PE's-yesterday-high)/2, PE_range mirrored.
Confirmation: spot/CE/PE all move the same direction relative to their own
today's-open. Entry once the confirmed side's option trades above its own
range. SL 25 points, target 48.5 points, EOD backstop.

Differences from the original beyond config-injection:
  - No hard-coded sibling-project path for self-healing OpenAlgo (the
    original's self_heal_dependencies() imported
    C:\\...\\flattrade\\ensure_openalgo_and_login.py, which only exists on
    Gopinath's own machine and would break on a customer's install). This
    version only detects and logs an OpenAlgo outage; restarting OpenAlgo
    itself is the GUI's process_manager's job, not the strategy engine's.
  - GUI-driven start/stop via a threading.Event instead of a Task
    Scheduler-launched process; PID file replaced by the GUI owning the
    subprocess/thread handle directly.
  - Same unweakened safety gate: refuses to place any order unless
    OpenAlgo's analyzer/sandbox (paper) mode is on.
==========================================================
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta

import pytz

from engine.openalgo_client import OpenAlgoClient
from engine.config_store import ConfigStore
from engine.trail_tracker import TrailTracker

IST = pytz.timezone("Asia/Kolkata")
STRATEGY_NAME = "TamilOptionSeller30_v1"


def _log(log_fn, msg: str) -> None:
    line = f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} IST] {msg}"
    (log_fn or print)(line)


def _log_change(log_fn, setup: dict, msg: str) -> None:
    """Live condition narrative, deduped per underlying -- the poll loop
    runs every poll_interval_sec all day, so only state CHANGES get
    logged. The marker lives on `setup`, which (unlike `trade`) is never
    serialized to the day's JSON, so it can't leak into the trade
    record."""
    if setup.get("_last_log") == msg:
        return
    setup["_last_log"] = msg
    _log(log_fn, msg)


def _today_ist() -> date:
    """The calendar date in IST, not the host machine's own local date --
    see orb_strategy.py's _today_ist() for why date.today() is unsafe on
    a customer's machine in a different timezone."""
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


def _build_setup(client: OpenAlgoClient, underlying: dict, today: date, prev_day: date) -> tuple[dict | None, str | None]:
    prev_str = prev_day.strftime("%Y-%m-%d")
    idx_daily = client.get_daily(underlying["index_symbol"], prev_str, prev_str, exchange=underlying["quote_exchange"])
    if idx_daily.empty:
        return None, f"no prior-day daily close for {underlying['index_symbol']}"
    prev_close = idx_daily["close"].iloc[-1]
    atm = round(prev_close / underlying["step"]) * underlying["step"]

    expiry = client.get_nearest_expiry(underlying["name"], underlying["opt_exchange"])
    if expiry is None:
        return None, "could not resolve nearest expiry"

    ce_sym, _ = client.resolve_option_symbol(underlying["name"], expiry, atm, "CE", underlying["opt_exchange"])
    pe_sym, _ = client.resolve_option_symbol(underlying["name"], expiry, atm, "PE", underlying["opt_exchange"])
    if ce_sym is None or pe_sym is None:
        return None, f"could not resolve ATM {atm:.0f} CE/PE symbols"

    ce_yday = client.get_minute(ce_sym, prev_str, prev_str, exchange=underlying["opt_exchange"])
    pe_yday = client.get_minute(pe_sym, prev_str, prev_str, exchange=underlying["opt_exchange"])
    if ce_yday.empty or pe_yday.empty:
        return None, f"no prior-day minute data for {ce_sym}/{pe_sym} (rollover day?)"

    ce_range = (ce_yday["low"].min() + pe_yday["high"].max()) / 2
    pe_range = (pe_yday["low"].min() + ce_yday["high"].max()) / 2

    return {
        "underlying": underlying["name"], "lot": underlying["lot"], "atm": atm, "expiry": expiry,
        "ce_sym": ce_sym, "pe_sym": pe_sym, "opt_exch": underlying["opt_exchange"],
        "index_symbol": underlying["index_symbol"], "quote_exch": underlying["quote_exchange"],
        "ce_range": round(ce_range, 2), "pe_range": round(pe_range, 2),
        "spot_open": None, "ce_open": None, "pe_open": None,
        "entered": False, "exited": False,
    }, None


def _capture_todays_opens(client: OpenAlgoClient, setup: dict, today_str: str) -> bool:
    idx_df = client.get_minute(setup["index_symbol"], today_str, today_str, exchange=setup["quote_exch"])
    ce_df = client.get_minute(setup["ce_sym"], today_str, today_str, exchange=setup["opt_exch"])
    pe_df = client.get_minute(setup["pe_sym"], today_str, today_str, exchange=setup["opt_exch"])
    if idx_df.empty or ce_df.empty or pe_df.empty:
        return False
    setup["spot_open"] = idx_df["open"].iloc[0]
    setup["ce_open"] = ce_df["open"].iloc[0]
    setup["pe_open"] = pe_df["open"].iloc[0]
    return True


def _try_entry(client: OpenAlgoClient, cfg: dict, setup: dict, trade: dict, today: date, log_fn) -> None:
    name = setup["underlying"]
    s = client.get_quote(setup["index_symbol"], setup["quote_exch"])
    c = client.get_quote(setup["ce_sym"], setup["opt_exch"])
    p = client.get_quote(setup["pe_sym"], setup["opt_exch"])
    if s is None or c is None or p is None:
        _log_change(log_fn, setup, f"  {name}: waiting -- no live quote for spot/CE/PE yet")
        return

    bullish = s > setup["spot_open"] and c > setup["ce_open"] and p < setup["pe_open"]
    bearish = s < setup["spot_open"] and p > setup["pe_open"] and c < setup["ce_open"]

    entry_type = entry_sym = entry_price = entry_range = None
    if bullish and c > setup["ce_range"]:
        entry_type, entry_sym, entry_price, entry_range = "CE", setup["ce_sym"], c, setup["ce_range"]
    elif bearish and p > setup["pe_range"]:
        entry_type, entry_sym, entry_price, entry_range = "PE", setup["pe_sym"], p, setup["pe_range"]
    else:
        # Say WHICH half of the condition is missing -- "no entry" on its own
        # tells the customer nothing about how close it came.
        if bullish:
            detail = (f"bullish confirmed (spot {s:.2f} > open {setup['spot_open']:.2f}, CE up, PE down) "
                       f"but CE {c:.2f} has not crossed range {setup['ce_range']:.2f}")
        elif bearish:
            detail = (f"bearish confirmed (spot {s:.2f} < open {setup['spot_open']:.2f}, PE up, CE down) "
                       f"but PE {p:.2f} has not crossed range {setup['pe_range']:.2f}")
        else:
            detail = (f"no directional confirmation yet -- spot {s:.2f} vs open {setup['spot_open']:.2f}, "
                       f"CE {c:.2f} vs {setup['ce_open']:.2f}, PE {p:.2f} vs {setup['pe_open']:.2f}")
        _log_change(log_fn, setup, f"  {name}: {detail}")
        return

    _log(log_fn, f"--- {setup['underlying']}: CONDITION MET -- entry signal -- {entry_type} {entry_sym} @ {entry_price} "
                  f"(range={entry_range}, spot={s} ce={c} pe={p}) ---")
    lots = max(1, int(cfg.get("lots", 1)))
    quantity = setup["lot"] * lots  # whole lots only -- see config_store's orb.lots comment
    resp, status = client.place_order(STRATEGY_NAME, entry_sym, setup["opt_exch"], quantity, "BUY")
    _log(log_fn, f"    order response ({status}): {resp}")
    if resp.get("status") != "success":
        trade["entry_error"] = resp.get("message")
        return

    trade.update({
        "underlying": setup["underlying"], "date": str(today), "atm": setup["atm"],
        "option_type": entry_type, "symbol": entry_sym, "exchange": setup["opt_exch"],
        "quantity": quantity, "lots": lots, "ce_range": setup["ce_range"], "pe_range": setup["pe_range"],
        "entered": True, "exited": False,
        "entry_time": datetime.now(IST).isoformat(), "entry_price": entry_price,
        "entry_orderid": resp.get("orderid"),
        "sl_price": round(entry_price - cfg["sl_points"], 2),
        "target_price": round(entry_price + cfg["target_points"], 2),
    })
    setup["entered"] = True


def _monitor_position(client: OpenAlgoClient, cfg: dict, setup: dict, trade: dict, log_fn, reason_override: str | None = None) -> None:
    px = client.get_quote(trade["symbol"], trade["exchange"])
    if px is None:
        _log_change(log_fn, setup, f"  {trade['underlying']}: holding {trade['symbol']} -- no quote right now")
        return

    hit_stop = px <= trade["sl_price"]
    hit_target = px >= trade["target_price"]
    if reason_override is None and not hit_stop and not hit_target:
        _log_change(log_fn, setup,
                     f"  {trade['underlying']} {trade['symbol']}: ltp={px:.2f} (entry {trade['entry_price']:.2f}) -- "
                     f"stop {trade['sl_price']:.2f} / target {trade['target_price']:.2f}, holding")
        return

    reason = reason_override or ("stop" if hit_stop else "target")
    exit_price = trade["sl_price"] if reason == "stop" else (trade["target_price"] if reason == "target" else px)

    _log(log_fn, f"--- {trade['underlying']}: CONDITION MET ({reason}) at ltp={px:.2f} -- EXIT "
                  f"{trade['symbol']} exit={exit_price} ---")
    resp, status = client.place_order(STRATEGY_NAME, trade["symbol"], trade["exchange"], trade["quantity"], "SELL")
    _log(log_fn, f"    order response ({status}): {resp}")

    trade["exited"] = True
    trade["exit_time"] = datetime.now(IST).isoformat()
    trade["exit_price"] = exit_price
    trade["exit_reason"] = reason
    trade["exit_orderid"] = resp.get("orderid")

    gross = (exit_price - trade["entry_price"]) * trade["quantity"]
    net = gross - cfg["flat_cost_per_lot"]
    trade["gross_pnl"] = round(gross, 2)
    trade["net_pnl"] = round(net, 2)
    _log(log_fn, f"    entry={trade['entry_price']} exit={exit_price} gross_pnl={gross:.2f} net_pnl={net:.2f}")
    setup["exited"] = True


def run_tamil_day(config: ConfigStore, log_fn=None, stop_event: threading.Event | None = None,
                   manual_exit_event: threading.Event | None = None) -> None:
    """Entry point the GUI calls (in its own thread) when the user starts
    Tamil. Resumable: if today's trade file already exists (e.g. GUI was
    restarted mid-day), picks up where it left off instead of re-entering."""
    stop_event = stop_event or threading.Event()
    cfg = config.tamil_settings()
    client = OpenAlgoClient(config.openalgo_host, config.openalgo_api_key, config.openalgo_exchange)
    today = _today_ist()

    _log(log_fn, f"=== Tamil Option Seller 30% starting for {today} ===")

    if today.weekday() >= 5:
        _log(log_fn, "Skipping: weekend")
        return
    if today.weekday() not in cfg["trade_weekdays"]:
        _log(log_fn, "Skipping: not a configured trading weekday for this strategy")
        return
    trading_ok, reason = client.is_trading_day(today)
    if not trading_ok:
        _log(log_fn, f"Skipping: {reason}")
        return

    if not _wait_until_ist(cfg.get("market_open", "09:15"), log_fn, stop_event,
                            "the market open"):
        return

    if not client.is_analyzer_mode_on():
        _log(log_fn, "ABORT: analyzer/sandbox mode is OFF -- refusing to place orders (would be LIVE/real). "
                      "Enable sandbox mode in OpenAlgo before starting this strategy.")
        return

    prev_day = today - timedelta(days=1)
    while prev_day.weekday() >= 5:
        prev_day -= timedelta(days=1)

    infile = config.tamil_trades_dir / f"{today}.json"
    day_log = []
    if infile.exists():
        with open(infile) as f:
            day_log = json.load(f)
        _log(log_fn, f"Resuming -- loaded {len(day_log)} existing record(s) from {infile}")

    setups, trades = {}, {}
    for underlying in cfg["underlyings"]:
        name = underlying["name"]
        existing = next((t for t in day_log if t.get("underlying") == name), None)
        setup, err = _build_setup(client, underlying, today, prev_day)
        if setup is None:
            _log(log_fn, f"{name}: SKIP -- {err}")
            if existing is None:
                day_log.append({"underlying": name, "date": str(today), "entered": False, "reason": err})
            continue
        if existing is not None:
            setup["entered"] = existing.get("entered", False)
            setup["exited"] = existing.get("exited", False)
            trades[name] = existing
        else:
            trade = {"underlying": name, "date": str(today), "entered": False, "exited": False}
            day_log.append(trade)
            trades[name] = trade
        setups[name] = setup
        _log(log_fn, f"{name}: ATM={setup['atm']:.0f} expiry={setup['expiry']} CE={setup['ce_sym']} PE={setup['pe_sym']} "
                      f"CE_range={setup['ce_range']} PE_range={setup['pe_range']}")

    with open(infile, "w") as f:
        json.dump(day_log, f, indent=2)

    today_str = today.strftime("%Y-%m-%d")
    for name, setup in setups.items():
        if setup["entered"]:
            continue
        for _ in range(20):
            if stop_event.is_set():
                return
            if _capture_todays_opens(client, setup, today_str):
                _log(log_fn, f"{name}: today's opens -- spot={setup['spot_open']} ce={setup['ce_open']} pe={setup['pe_open']}")
                break
            stop_event.wait(3)
        else:
            _log(log_fn, f"{name}: WARNING -- could not capture today's opens after retries, will keep trying in the poll loop")

    market_close = cfg.get("market_close", "15:30")
    close_h, close_m = (int(x) for x in market_close.split(":"))
    poll_interval = cfg["poll_interval_sec"]

    # Observation only -- scores group-level trailing-stop variants against
    # today's real path. Never places, changes or cancels an order.
    trail = TrailTracker("TOS-30", str(today))

    while not stop_event.is_set():
        now = datetime.now(IST)
        market_closed = now >= now.replace(hour=close_h, minute=close_m, second=0, microsecond=0)

        manual = manual_exit_event is not None and manual_exit_event.is_set()
        if manual:
            _log(log_fn, "MANUAL EXIT requested -- closing any open TOS-30 position(s) now.")

        all_done = True
        for name, setup in setups.items():
            trade = trades[name]

            if setup["spot_open"] is None:
                _capture_todays_opens(client, setup, today_str)
                if setup["spot_open"] is None:
                    _log_change(log_fn, setup, f"  {name}: waiting for today's opening candle before anything can trigger")
                    all_done = False
                    continue

            if not setup["entered"] and not market_closed:
                _try_entry(client, cfg, setup, trade, today, log_fn)
                all_done = False
            elif setup["entered"] and not setup["exited"]:
                _monitor_position(client, cfg, setup, trade, log_fn,
                                    reason_override=("manual" if manual else ("eod" if market_closed else None)))
                if not setup["exited"]:
                    all_done = False
            elif not setup["entered"] and market_closed:
                trade["reason"] = trade.get("reason") or "market closed -- confirmation+range-cross never aligned today"

        if manual:
            manual_exit_event.clear()

        # Combined mark-to-market of the TOS-30 group: everything already
        # booked today, plus the live value of anything still open.
        combined = 0.0
        for t in day_log:
            if not t.get("entered"):
                continue
            if t.get("exited"):
                combined += t.get("net_pnl") or 0
                continue
            ltp = client.get_quote(t.get("symbol"), t.get("exchange")) if t.get("symbol") else None
            if ltp is not None and t.get("entry_price") is not None:
                combined += (ltp - t["entry_price"]) * (t.get("quantity") or 0) - cfg.get("flat_cost_per_lot", 60)
        trail.update(combined)

        with open(infile, "w") as f:
            json.dump(day_log, f, indent=2)

        if all_done:
            _log(log_fn, "All underlyings resolved for today (entered+exited, or no entry).")
            break
        if market_closed and all(setups[u]["exited"] or not setups[u]["entered"] for u in setups):
            _log(log_fn, "Market closed and no open positions remain.")
            break
        if not client.ping():
            _log(log_fn, "[warning] OpenAlgo is not reachable right now -- will keep retrying on the next poll.")

        stop_event.wait(poll_interval)

    with open(infile, "w") as f:
        json.dump(day_log, f, indent=2)
    _log(log_fn, f"Wrote {infile}")

    legs_traded = sum(1 for t in day_log if t.get("entered"))
    saved = trail.save(config.trail_study_dir,
                        actual_net=sum(t.get("net_pnl") or 0 for t in day_log if t.get("entered")),
                        legs_traded=legs_traded, legs_total=len(day_log))
    if saved:
        _log(log_fn, f"Trailing-stop study written to {saved.name} "
                      f"(peak {trail.peak:+.2f}, {len(trail.curve)} samples, "
                      f"{legs_traded}/{len(day_log)} legs traded)")
    _log(log_fn, "=== Tamil done for today ===")
