"""
==========================================================
TradeSuite
licensing/renewal_report.py
==========================================================

At each renewal boundary, the customer's install sends the operator a
summary of how that subscription period actually went -- so a month
later there's a real answer to "what P&L did they make on the
subscription they're about to renew?"

Design points that matter:

**Aggregates, not a data grab.** The emailed body carries per-strategy
totals; the CSV attachment carries one row per trade, which is what
makes the number auditable rather than a bare assertion. It is the
customer's own trading data either way, so the registration screen
discloses that this is sent -- see gui/license_screen.py. Sending a
customer's financial records anywhere without telling them would be the
wrong call regardless of it being technically easy.

**Sent once per boundary.** Tracked in `renewal_reports.json` alongside
the license file. If the machine is offline at the boundary, the
boundary stays pending and is retried on later launches rather than
being silently skipped -- an install that was off for a week must not
lose the period's report.

**Never blocks anything.** Runs on a worker thread, fails soft, and no
failure here can affect trading or the license itself.
"""

from __future__ import annotations

import csv
import io
import json
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from licensing import license_core, mail_relay, server_config

STATE_FILENAME = "renewal_reports.json"
DATE_FMT = "%Y%m%d"


def _state_path() -> Path:
    from engine.config_store import _install_dir
    return _install_dir() / STATE_FILENAME


def _load_state() -> dict:
    try:
        with open(_state_path()) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"reported": [], "pending": []}
    data.setdefault("reported", [])
    data.setdefault("pending", [])
    return data


def _save_state(state: dict) -> None:
    try:
        with open(_state_path(), "w") as f:
            json.dump(state, f, indent=2)
    except OSError:
        pass


def queue_period_end(boundary: date) -> None:
    """Called when a renewal EXTENDS the licence past a boundary that
    would otherwise never be seen as "passed" -- without this, redeeming
    a key a day early means the closing period is never reported."""
    state = _load_state()
    key = boundary.strftime(DATE_FMT)
    if key not in state["reported"] and key not in state["pending"]:
        state["pending"].append(key)
        _save_state(state)


# -- gathering ---------------------------------------------------------

def _collect_trades(config, start: date, end: date) -> list[tuple[str, dict]]:
    from gui.labels import ORB_SHORT, TAMIL_SHORT
    rows = []
    for label, trades_dir, entered_key in (
        (ORB_SHORT, config.trades_dir, "traded"),
        (TAMIL_SHORT, config.tamil_trades_dir, "entered"),
    ):
        if not trades_dir.exists():
            continue
        for day_file in sorted(trades_dir.glob("*.json")):
            try:
                with open(day_file) as f:
                    day = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            for t in day:
                if not t.get(entered_key):
                    continue
                d = t.get("date", "")
                if start.isoformat() <= d <= end.isoformat():
                    rows.append((label, t))
    rows.sort(key=lambda r: (r[1].get("date", ""), r[1].get("entry_time", "")))
    return rows


def _summarize(rows: list[tuple[str, dict]]) -> dict:
    closed = [(s, t) for s, t in rows if t.get("net_pnl") is not None]
    per_strategy: dict[str, dict] = {}
    for s, t in closed:
        d = per_strategy.setdefault(s, {"trades": 0, "net": 0.0, "wins": 0})
        d["trades"] += 1
        d["net"] += t["net_pnl"]
        if t["net_pnl"] > 0:
            d["wins"] += 1
    return {
        "total": len(rows),
        "closed": len(closed),
        "net": sum(t["net_pnl"] for _, t in closed),
        "wins": sum(1 for _, t in closed if t["net_pnl"] > 0),
        "per_strategy": per_strategy,
    }


def _csv_of(rows: list[tuple[str, dict]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "strategy", "underlying", "symbol", "option_type", "quantity",
                 "entry_time", "entry_price", "exit_time", "exit_price", "exit_mode", "net_pnl"])
    for label, t in rows:
        w.writerow([t.get("date", ""), label, t.get("underlying", ""), t.get("symbol", ""),
                     t.get("option_type", ""), t.get("quantity", ""), t.get("entry_time", ""),
                     t.get("entry_price", ""), t.get("exit_time", ""), t.get("exit_price", ""),
                     t.get("exit_mode", ""), t.get("net_pnl", "")])
    return buf.getvalue()


def build_report(config, start: date, end: date) -> tuple[str, str, str]:
    """Returns (subject, body, csv_text) for the period [start, end]."""
    status = license_core.check_status()
    data = license_core._load_raw()
    rows = _collect_trades(config, start, end)
    s = _summarize(rows)

    win_rate = f"{s['wins'] / s['closed'] * 100:.1f}%" if s["closed"] else "--"
    lines = [
        f"TradeSuite subscription period report: {start.isoformat()} to {end.isoformat()}",
        "",
        f"Customer : {data.get('name') or '(not given)'} <{data.get('email') or 'no email'}>",
        f"Phone    : {data.get('phone') or '(not given)'}",
        f"Machine  : {status.machine_id}",
        f"Licence  : expiry {data.get('expiry_date') or 'none'} | keys redeemed {len(data.get('used_keys', []))}",
        "",
        f"Trades   : {s['total']} entered | {s['closed']} closed | win rate {win_rate}",
        f"Net P&L  : {s['net']:+.2f}",
        "",
        "Per strategy:",
    ]
    if s["per_strategy"]:
        for name in sorted(s["per_strategy"]):
            d = s["per_strategy"][name]
            wr = d["wins"] / d["trades"] * 100 if d["trades"] else 0
            lines.append(f"  {name}: {d['trades']} trades, {wr:.0f}% win, net {d['net']:+.2f}")
    else:
        lines.append("  (no closed trades in this period)")
    lines += ["", "Full per-trade detail is attached as CSV.",
              "", "-- sent automatically by TradeSuite at the subscription renewal boundary."]

    # Plain ASCII in the Subject on purpose -- it survives every mail client
    # and mailbox search unencoded, and there's nothing here worth a
    # =?utf-8?...?= header for.
    subject = (f"TradeSuite period report - {data.get('name') or status.machine_id[:8]} - "
               f"{end.isoformat()} - net {s['net']:+.2f}")
    return subject, "\n".join(lines), _csv_of(rows)


# -- the boundary check -------------------------------------------------

def _due_boundaries(state: dict) -> list[date]:
    """Every period end that has passed and hasn't been reported yet."""
    due = []
    for key in state["pending"]:
        try:
            due.append(datetime.strptime(key, DATE_FMT).date())
        except ValueError:
            continue

    data = license_core._load_raw()
    expiry = license_core._parse_expiry(data.get("expiry_date"))
    today = date.today()  # host-local on purpose: the licence calendar is the customer's own, not IST
    if expiry and today >= expiry and expiry.strftime(DATE_FMT) not in state["reported"]:
        due.append(expiry)
    return sorted(set(due))


def _period_start(state: dict, boundary: date) -> date:
    """Start of the period ending at `boundary`: the previous reported
    boundary if there is one, else a month back -- which is the right
    default given a 30-day trial and 30-day renewals."""
    previous = []
    for key in state["reported"]:
        try:
            d = datetime.strptime(key, DATE_FMT).date()
            if d < boundary:
                previous.append(d)
        except ValueError:
            continue
    return max(previous) + timedelta(days=1) if previous else boundary - timedelta(days=30)


def check_and_send(config, log_fn=None) -> list[str]:
    """Sends any due period reports. Returns a list of human-readable
    outcomes (empty if nothing was due). Safe to call on every launch."""
    results = []
    if not server_config.is_configured():
        return results

    state = _load_state()
    due = _due_boundaries(state)
    if not due:
        return results

    for boundary in due:
        key = boundary.strftime(DATE_FMT)
        start = _period_start(state, boundary)
        try:
            subject, body, csv_text = build_report(config, start, boundary)
            ok, msg = mail_relay.send_to_admin(subject, body, [(f"tradesuite_{key}.csv", csv_text)])
        except Exception as e:  # a reporting bug must never break launch
            ok, msg = False, f"{type(e).__name__}: {e}"

        if ok:
            state["reported"].append(key)
            if key in state["pending"]:
                state["pending"].remove(key)
            results.append(f"Period report for {boundary.isoformat()} sent.")
        else:
            # Stays pending and is retried on the next launch -- an offline
            # machine at the boundary must not lose the period entirely.
            if key not in state["pending"]:
                state["pending"].append(key)
            results.append(f"Period report for {boundary.isoformat()} not sent yet: {msg}")
        _save_state(state)

    if log_fn:
        for r in results:
            log_fn(r)
    return results


def check_and_send_async(config) -> None:
    """Fire-and-forget from the GUI thread -- SMTP can block for the full
    timeout, which must never freeze the window."""
    threading.Thread(target=check_and_send, args=(config,),
                      name="tradesuite-renewal-report", daemon=True).start()
