"""
==========================================================
TradeSuite
engine/fb_mock_trades.py
==========================================================

Paper ("mock") journal for trade calls posted in an outside group (e.g.
a Facebook trading group). Nothing here places, modifies or cancels an
order -- it only records what the call said and what the market did.

Every call gets three timestamped price marks:

  open  -- when the call was posted, and the option's price at that
           moment (the "open price" of the idea, before any trigger)
  entry -- when the call's entry condition was met, and the fill price
  exit  -- when SL / target / manual exit / EOD hit, and the exit price

Marks can be entered by hand (always works, even with no broker), or
filled from real 1-minute bars via OpenAlgo with `simulate`, which walks
forward from the call time and never looks ahead.

Usage (from the project root):

  python -m engine.fb_mock_trades add "BUY NIFTY 25000 CE ABOVE 120 SL 100 TGT 150" --time 09:32
  python -m engine.fb_mock_trades open  1 118.5 --time 09:32
  python -m engine.fb_mock_trades entry 1 121   --time 09:41
  python -m engine.fb_mock_trades exit  1 150   --time 10:05 --reason target
  python -m engine.fb_mock_trades add-post post.txt --trade-date 2026-09-24 --time 22:54 --date 2026-09-23
                                                   # every call in a multi-call post (BTST detected)
  python -m engine.fb_mock_trades simulate 1      # fill marks from OpenAlgo bars
  python -m engine.fb_mock_trades fbbtst          # tabular report
==========================================================
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pytz

IST = pytz.timezone("Asia/Kolkata")
TS_FMT = "%Y-%m-%d %H:%M:%S"
EOD = "15:15"

# "BUY NIFTY 25000 CE ABOVE 120 SL 100 TGT 150/180" and common variants
# ("BANKNIFTY 52000 PE @ 240-245 SL 210 TARGET 280", "SELL ... BELOW ...",
# "Nifty 23,400 Put 85 TARGET 180"). A price with no keyword, or after
# @/AT/NEAR, is a limit buy; ABOVE/BELOW is a breakout trigger.
_CALL_RE = re.compile(
    r"(?P<side>BUY|SELL)?\s*(?P<underlying>[A-Z&]+)\s+(?P<strike>\d+(?:\.\d+)?)\s*(?P<opt>CE|PE)"
    r"(?:\s*(?P<kw>ABOVE|BELOW|@|NEAR|AT|ENTRY)?\s*[:\-]?\s*(?P<entry>\d+(?:\.\d+)?))?"
    r"(?:.*?(?:SL|STOPLOSS|STOP\s*LOSS)\s*[:\-]?\s*(?P<sl>\d+(?:\.\d+)?))?"
    r"(?:.*?(?:TGT|TARGET|TP)S?\s*[:\-]?\s*(?P<tgt>\d+(?:\.\d+)?))?",
    re.IGNORECASE | re.DOTALL,
)

BSE_UNDERLYINGS = {"SENSEX", "BANKEX", "SENSEX50"}


def _normalize(text: str) -> str:
    t = text.upper()
    t = re.sub(r"(?<=\d),(?=\d)", "", t)          # 23,400 -> 23400
    t = re.sub(r"\bBANK\s+NIFTY\b", "BANKNIFTY", t)
    t = re.sub(r"\bFIN\s+NIFTY\b", "FINNIFTY", t)
    t = re.sub(r"\bPUT\b", "PE", t)
    t = re.sub(r"\bCALL\b", "CE", t)
    return t


def opt_exchange_for(underlying: str) -> str:
    return "BFO" if underlying in BSE_UNDERLYINGS else "NFO"


def journal_path() -> Path:
    from engine.config_store import _install_dir
    return _install_dir() / "output" / "fb_mock_trades.json"


def _now() -> datetime:
    return datetime.now(IST)


def _stamp(day: str | None, hhmm: str | None) -> str:
    """HH:MM[:SS] on `day` (default today, IST) -> full timestamp string."""
    if not hhmm:
        return _now().strftime(TS_FMT)
    day = day or _now().strftime("%Y-%m-%d")
    if hhmm.count(":") == 1:
        hhmm += ":00"
    return f"{day} {hhmm}"


def _from_match(m: re.Match) -> dict:
    num = lambda k: float(m.group(k)) if m.group(k) else None
    kw = (m.group("kw") or "").upper()
    return {
        "side": (m.group("side") or "BUY").upper(),
        "underlying": m.group("underlying"),
        "strike": num("strike"),
        "option_type": m.group("opt"),
        "entry_trigger": num("entry"),
        "entry_kind": {"ABOVE": "above", "BELOW": "below"}.get(kw, "limit"),
        "sl": num("sl"),
        "target": num("tgt"),
    }


def parse_call(text: str) -> dict:
    m = _CALL_RE.search(_normalize(text))
    if not m:
        raise ValueError(f"could not read a strike + CE/PE from: {text!r}")
    return _from_match(m)


def parse_post(text: str) -> list[dict]:
    """Every call in a multi-call post. Each call's own SL/TARGET is only
    searched up to where the next call starts, so they can't bleed over."""
    t = _normalize(text)
    starts = [m.start() for m in re.finditer(r"(?:BUY\s+|SELL\s+)?[A-Z&]+\s+\d+(?:\.\d+)?\s*(?:CE|PE)\b", t)]
    calls = []
    for i, st in enumerate(starts):
        chunk = t[st:starts[i + 1] if i + 1 < len(starts) else len(t)]
        m = _CALL_RE.search(chunk)
        if m:
            calls.append(_from_match(m))
    if not calls:
        raise ValueError("no calls found in post")
    return calls


class MockJournal:
    def __init__(self, path: Path | None = None):
        self.path = path or journal_path()
        self.trades: list[dict] = []
        if self.path.exists():
            self.trades = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.trades, indent=2), encoding="utf-8")

    def get(self, trade_id: int) -> dict:
        for t in self.trades:
            if t["id"] == trade_id:
                return t
        raise KeyError(f"no mock trade #{trade_id}")

    def add(self, text: str, call_time: str, qty: int = 1, symbol: str | None = None, source: str = "facebook",
            trade_date: str | None = None, hold_days: int = 0, parsed: dict | None = None) -> dict:
        parsed = parsed or parse_call(text)
        trade = {
            "id": max((t["id"] for t in self.trades), default=0) + 1,
            "source": source,
            "call_text": text,
            "call_time": call_time,
            **parsed,
            "trade_date": trade_date or call_time[:10],
            "hold_days": hold_days,             # 0 intraday, 1 BTST/STBT
            "symbol": symbol,
            "opt_exchange": opt_exchange_for(parsed["underlying"]),
            "quantity": qty,
            "open_time": None, "open_price": None,
            "entry_time": None, "entry_price": None,
            "exit_time": None, "exit_price": None, "exit_reason": None,
            "status": "called",
        }
        self.trades.append(trade)
        return trade

    def mark(self, trade_id: int, which: str, price: float, ts: str, reason: str | None = None) -> dict:
        t = self.get(trade_id)
        t[f"{which}_time"], t[f"{which}_price"] = ts, round(float(price), 2)
        if which == "entry":
            t["status"] = "open"
        elif which == "exit":
            t["exit_reason"] = reason or "manual"
            t["status"] = "closed"
        return t

    @staticmethod
    def pnl(t: dict) -> float | None:
        if t.get("entry_price") is None or t.get("exit_price") is None:
            return None
        sign = 1 if t["side"] == "BUY" else -1
        return round((t["exit_price"] - t["entry_price"]) * sign * t["quantity"], 2)

    # --------------------------------------------------
    # Fill marks from real minute bars (optional)
    # --------------------------------------------------

    def simulate(self, trade_id: int, client, opt_exchange: str | None = None, eod: str = EOD) -> dict:
        """Walk the option's 1m bars forward from the later of call_time
        and trade_date's open. open = first bar, entry = first bar that
        fills the call (limit: trades at/through the price; above/below:
        breaks it; no price: first bar's open) within trade_date's
        session, exit = first bar touching SL / target up to the close of
        trade_date + hold_days trading sessions (BTST = next day), else
        that session's EOD bar. SL and target in one bar -> SL first."""
        import pandas as pd
        from datetime import timedelta

        t = self.get(trade_id)
        exch = opt_exchange or t.get("opt_exchange") or opt_exchange_for(t["underlying"])
        if not t.get("symbol"):
            expiry = client.get_nearest_expiry(t["underlying"], exch)
            sym, _ = client.resolve_option_symbol(t["underlying"], expiry, t["strike"], t["option_type"], exch) \
                if expiry else (None, None)
            if not sym:
                raise ValueError("could not resolve the option symbol; pass it with add --symbol")
            t["symbol"] = sym
        start = pd.Timestamp(t["trade_date"])
        span_end = (start + timedelta(days=4 + 2 * t.get("hold_days", 0))).strftime("%Y-%m-%d")
        bars = client.get_minute(t["symbol"], start.strftime("%Y-%m-%d"), span_end, exchange=exch)
        if bars.empty:
            raise RuntimeError("no minute data (contract expired or no data yet)")
        if bars.index.tz is not None:
            bars.index = bars.index.tz_localize(None)
        bars = bars[bars.index >= max(pd.Timestamp(t["call_time"]).floor("min"), start)]
        sessions = sorted({ts.normalize() for ts in bars.index})
        if not sessions:
            raise RuntimeError("no bars after the call time")
        hold = t.get("hold_days", 0)
        if len(sessions) <= hold:
            raise RuntimeError(f"only {len(sessions)} session(s) of data yet; need {hold + 1}")
        entry_day, exit_day = sessions[0], sessions[hold]
        bars = bars[bars.index < exit_day + pd.Timedelta(days=1)]

        fmt = lambda ts: ts.strftime(TS_FMT)
        first = bars.iloc[0]
        self.mark(trade_id, "open", first["open"], fmt(bars.index[0]))

        buy = t["side"] == "BUY"
        trig, kind = t.get("entry_trigger"), t.get("entry_kind", "above")
        entry_i = None
        for i, (ts, b) in enumerate(bars.iterrows()):
            if ts.normalize() != entry_day:
                break
            if trig is None:
                price = b["open"]
            elif kind == "limit" and (b["low"] <= trig if buy else b["high"] >= trig):
                price = min(trig, b["open"]) if buy else max(trig, b["open"])
            elif kind != "limit" and (b["high"] >= trig if kind == "above" else b["low"] <= trig):
                price = max(trig, b["open"]) if kind == "above" else min(trig, b["open"])
            else:
                continue
            self.mark(trade_id, "entry", price, fmt(ts))
            entry_i = i
            break
        if entry_i is None:
            t["status"] = "not_triggered"
            return t

        eod_ts = pd.Timestamp(f"{exit_day.strftime('%Y-%m-%d')} {eod}")
        sl, tgt = t.get("sl"), t.get("target")
        for ts, b in bars.iloc[entry_i:].iterrows():
            hit_sl = sl is not None and (b["low"] <= sl if buy else b["high"] >= sl)
            hit_tgt = tgt is not None and (b["high"] >= tgt if buy else b["low"] <= tgt)
            if hit_sl:
                return self.mark(trade_id, "exit", sl, fmt(ts), "sl")
            if hit_tgt:
                return self.mark(trade_id, "exit", tgt, fmt(ts), "target")
            if ts >= eod_ts:
                return self.mark(trade_id, "exit", b["close"], fmt(ts), "eod")
        return self.mark(trade_id, "exit", bars.iloc[-1]["close"], fmt(bars.index[-1]), "eod")

    # --------------------------------------------------
    # fbbtst: tabular report
    # --------------------------------------------------

    def table(self) -> str:
        cols = ["#", "Call time", "Call", "Open @", "Open px", "Entry @", "Entry px",
                "SL", "Tgt", "Exit @", "Exit px", "Reason", "P&L", "Status"]
        hm = lambda s: s[5:16] if s else "-"          # MM-DD HH:MM
        px = lambda v: f"{v:g}" if v is not None else "-"
        rows = []
        for t in self.trades:
            p = self.pnl(t)
            rows.append([
                str(t["id"]), hm(t["call_time"]),
                f'{t["side"]} {t["underlying"]} {t["strike"]:g}{t["option_type"]}'
                + (" BTST" if t.get("hold_days") else ""),
                hm(t["open_time"]), px(t["open_price"]),
                hm(t["entry_time"]), px(t["entry_price"]),
                px(t["sl"]), px(t["target"]),
                hm(t["exit_time"]), px(t["exit_price"]),
                t["exit_reason"] or "-", px(p), t["status"],
            ])
        widths = [max(len(c), *(len(r[i]) for r in rows)) if rows else len(c) for i, c in enumerate(cols)]
        line = lambda r: "| " + " | ".join(v.ljust(w) for v, w in zip(r, widths)) + " |"
        out = [line(cols), "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
        out += [line(r) for r in rows]
        closed = [self.pnl(t) for t in self.trades if self.pnl(t) is not None]
        wins = sum(1 for v in closed if v > 0)
        out.append("")
        out.append(f"Calls: {len(self.trades)}  Closed: {len(closed)}  Wins: {wins}  "
                   f"Net P&L: {sum(closed):g}" if closed else f"Calls: {len(self.trades)}  Closed: 0")
        return "\n".join(out)


def _client():
    from engine.config_store import ConfigStore
    from engine.openalgo_client import OpenAlgoClient
    cfg = ConfigStore()
    return OpenAlgoClient(cfg.openalgo_host, cfg.openalgo_api_key, cfg.openalgo_exchange)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fb_mock_trades", description="Paper journal for group trade calls")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="record a new call")
    a.add_argument("text")
    a.add_argument("--time", help="HH:MM call time (default now, IST)")
    a.add_argument("--date", help="YYYY-MM-DD (default today)")
    a.add_argument("--qty", type=int, default=1)
    a.add_argument("--symbol", help="broker option symbol, needed for simulate")
    a.add_argument("--source", default="facebook")

    p = sub.add_parser("add-post", help="record every call in a post (text file, or - for stdin)")
    p.add_argument("file")
    p.add_argument("--time", help="HH:MM the post was published (default now, IST)")
    p.add_argument("--date", help="YYYY-MM-DD the post was published (default today)")
    p.add_argument("--trade-date", help="YYYY-MM-DD the calls are for (default: post date)")
    p.add_argument("--qty", type=int, default=1)
    p.add_argument("--source", default="facebook")

    for name in ("open", "entry", "exit"):
        m = sub.add_parser(name, help=f"mark {name} price + timestamp")
        m.add_argument("id", type=int)
        m.add_argument("price", type=float)
        m.add_argument("--time")
        m.add_argument("--date")
        if name == "exit":
            m.add_argument("--reason", default="manual")

    s = sub.add_parser("simulate", help="fill open/entry/exit from OpenAlgo 1m bars")
    s.add_argument("id", type=int)
    s.add_argument("--exchange", help="default: NFO, or BFO for SENSEX/BANKEX")

    sub.add_parser("fbbtst", help="show all mock trades as a table")

    args = ap.parse_args(argv)
    j = MockJournal()

    if args.cmd == "add":
        t = j.add(args.text, _stamp(args.date, args.time), args.qty, args.symbol, args.source)
        print(f"#{t['id']} recorded at {t['call_time']}: {t['side']} {t['underlying']} "
              f"{t['strike']:g}{t['option_type']} trig={t['entry_trigger']} sl={t['sl']} tgt={t['target']}")
    elif args.cmd == "add-post":
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
        hold = 1 if re.search(r"\b(BTST|STBT)\b", text, re.IGNORECASE) else 0
        call_time = _stamp(args.date, args.time)
        for c in parse_post(text):
            t = j.add(text.strip(), call_time, args.qty, None, args.source, args.trade_date, hold, parsed=c)
            print(f"#{t['id']} {t['side']} {t['underlying']} {t['strike']:g}{t['option_type']} "
                  f"@{t['entry_trigger']} tgt={t['target']} sl={t['sl']} trade_date={t['trade_date']}"
                  + (" BTST" if hold else ""))
    elif args.cmd in ("open", "entry", "exit"):
        t = j.mark(args.id, args.cmd, args.price, _stamp(args.date, args.time), getattr(args, "reason", None))
        print(f"#{t['id']} {args.cmd} {args.price:g} at {t[args.cmd + '_time']}")
    elif args.cmd == "simulate":
        t = j.simulate(args.id, _client(), args.exchange)
        print(f"#{t['id']} {t['status']}")
    if args.cmd == "fbbtst":
        print(j.table())
    else:
        j.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
