"""
==========================================================
TradeSuite
engine/openalgo_client.py
==========================================================

Single OpenAlgo REST client for both strategy engines. Consolidates two
things that were previously duplicated/scattered in OrbStrategy and
TamilOptionSeller30:

1. Bar-data fetching (history/daily/minute), lifted from data_provider.py's
   OpenAlgoClient -- including the _reanchor_intraday() DST-offset fix,
   which is load-bearing and kept exactly as-is.
2. Every hand-rolled `requests.post(f"{OA_HOST}/api/v1/...", ...)` call
   that was previously copy-pasted across paper_trade_entry.py,
   paper_trade_exit.py, and tamil_option_seller_trader.py (quotes, orders,
   analyzer-mode check, expiry, symbol search, holidays) -- now one method
   each, called by both strategy engines instead of three independent
   copies that could (and did) drift out of sync.

Unlike the original data_provider.py, host/api_key/exchange are
constructor arguments, not module globals read at import time -- required
so each customer's engine talks to *their* local OpenAlgo instance
(ConfigStore-driven), not a hardcoded shared key.

The most important fix preserved verbatim: resolve_option_symbol()'s
exact-name check. OpenAlgo's /search endpoint substring-matches, and
"NIFTY" is literally contained in "FINNIFTY" -- without checking
row["name"] == underlying, a NIFTY query can silently resolve to a
FINNIFTY contract. This caused real production failures in the original
codebase (2026-07-23/24) and must not be dropped in this rewrite.
==========================================================
"""

from __future__ import annotations

import requests
import pandas as pd
import pytz

IST = pytz.timezone("Asia/Kolkata")

INTERVAL_MAP = {
    "1": "1m", "1m": "1m", "2": "2m", "2m": "2m", "3": "3m", "3m": "3m",
    "5": "5m", "5m": "5m", "10": "10m", "10m": "10m", "15": "15m", "15m": "15m",
    "20": "20m", "20m": "20m", "30": "30m", "30m": "30m",
    "60": "1h", "1h": "1h", "120": "2h", "2h": "2h", "180": "3h", "3h": "3h", "240": "4h", "4h": "4h",
    "d": "D", "day": "D", "daily": "D",
    "w": "W", "week": "W", "weekly": "W",
    "m": "M", "month": "M", "monthly": "M",
    "q": "Q", "quarterly": "Q",
    "y": "Y", "yearly": "Y",
}

MARKET_OPEN = "09:15"  # used only by _reanchor_intraday's re-anchor anchor point


class OpenAlgoClient:
    def __init__(self, host: str, api_key: str, exchange: str = "NSE", timeout: int = 15):
        self.host = host.rstrip("/")
        self.api_key = api_key
        self.exchange = exchange
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json"}

    # ------------------------------------------------------
    # Generic POST
    # ------------------------------------------------------

    def _post(self, endpoint: str, payload: dict, timeout: int | None = None) -> dict | None:
        payload = dict(payload)
        payload.setdefault("apikey", self.api_key)
        try:
            r = requests.post(f"{self.host}/api/v1/{endpoint}", json=payload,
                               headers=self.headers, timeout=timeout or self.timeout)
            return r.json()
        except Exception as ex:
            return {"status": "error", "message": f"connection error: {ex}"}

    def ping(self) -> bool:
        try:
            r = requests.get(f"{self.host}/", timeout=10)
            return r.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------
    # Trading-mode safety gate
    # ------------------------------------------------------

    def is_analyzer_mode_on(self) -> bool:
        """True only when OpenAlgo's sandbox/paper mode is active. Every
        strategy engine must check this before placing any order and abort
        if False -- preserves the paper-only safety gate from the original
        paper_trade_entry.py / tamil_option_seller_trader.py exactly."""
        data = self._post("analyzer", {}) or {}
        return data.get("data", {}).get("analyze_mode", False)

    def set_analyzer_mode(self, on: bool) -> tuple[bool, str]:
        """Switches OpenAlgo between paper (sandbox) and live trading via
        its own POST /api/v1/analyzer/toggle endpoint ({"mode": true|false}
        -- confirmed against restx_api/analyzer.py + account_schema.py's
        AnalyzerToggleSchema, not guessed). This is the ONLY place in
        TradeSuite that can turn live trading on -- the GUI must gate calls
        to this behind an explicit customer confirmation, never a bare
        toggle switch."""
        data = self._post("analyzer/toggle", {"mode": on}) or {}
        if data.get("status") == "success":
            return True, "Live trading enabled." if on else "Switched to paper (sandbox) mode."
        return False, data.get("message", "Toggle failed for an unknown reason.")

    # ------------------------------------------------------
    # Calendar
    # ------------------------------------------------------

    def is_trading_day(self, day) -> tuple[bool, str]:
        if day.weekday() >= 5:
            return False, "weekend"
        try:
            data = self._post("market/holidays", {"year": day.year}, timeout=self.timeout) or {}
            holidays = data.get("data", []) if data.get("status") == "success" else []
            holiday_dates = set()
            for h in holidays:
                d = h.get("date") or h.get("holiday_date") or h.get("day")
                if d:
                    holiday_dates.add(str(d)[:10])
            if day.strftime("%Y-%m-%d") in holiday_dates:
                return False, "exchange holiday"
        except Exception as ex:
            return True, f"could not fetch holiday calendar ({ex}); proceeding on weekday-only check"
        return True, "ok"

    # ------------------------------------------------------
    # Symbol resolution
    # ------------------------------------------------------

    def get_nearest_expiry(self, underlying: str, opt_exchange: str) -> str | None:
        data = self._post("expiry", {"symbol": underlying, "exchange": opt_exchange, "instrumenttype": "options"}) or {}
        if data.get("status") != "success" or not data.get("data"):
            return None
        return data["data"][0].replace("-", "")  # "14-JUL-26" -> "14JUL26"

    def resolve_option_symbol(self, underlying: str, expiry: str, strike: float, opt_type: str,
                               opt_exchange: str) -> tuple[str | None, int | None]:
        data = self._post("search", {
            "query": f"{underlying}{expiry}{int(strike)}{opt_type}", "exchange": opt_exchange,
        }) or {}
        if data.get("status") != "success":
            return None, None
        for row in data.get("data", []):
            # exact-name check is load-bearing -- see module docstring
            if row["strike"] == strike and row["instrumenttype"] == opt_type and row.get("name") == underlying:
                return row["symbol"], row.get("lotsize")
        return None, None

    # ------------------------------------------------------
    # Quotes / orders
    # ------------------------------------------------------

    def get_quote(self, symbol: str, exchange: str) -> float | None:
        data = self._post("quotes", {"symbol": symbol, "exchange": exchange}) or {}
        if data.get("status") == "success":
            return data.get("data", {}).get("ltp")
        return None

    def place_options_order(self, strategy_name: str, underlying: str, quote_exchange: str,
                             expiry: str, option_type: str, quantity: int, action: str = "BUY") -> tuple[dict, int]:
        """ATM options order via /optionsorder -- OpenAlgo resolves the
        actual contract itself. Used for ORB entries (matches the original
        paper_trade_entry.py's place_paper_order)."""
        payload = {
            "strategy": strategy_name, "underlying": underlying, "exchange": quote_exchange,
            "expiry_date": expiry, "offset": "ATM", "option_type": option_type,
            "action": action, "quantity": quantity, "pricetype": "MARKET", "product": "MIS",
        }
        try:
            r = requests.post(f"{self.host}/api/v1/optionsorder",
                               json={**payload, "apikey": self.api_key}, timeout=30)
            return r.json(), r.status_code
        except Exception as ex:
            return {"status": "error", "message": f"connection error: {ex}"}, 0

    def place_order(self, strategy_name: str, symbol: str, exchange: str, quantity: int, action: str) -> tuple[dict, int]:
        """Raw symbol order via /placeorder -- used for ORB exits and all
        of Tamil's entries/exits (matches the original place_order/
        place_sell functions)."""
        payload = {
            "strategy": strategy_name, "exchange": exchange, "symbol": symbol,
            "action": action, "quantity": quantity, "pricetype": "MARKET", "product": "MIS",
        }
        try:
            r = requests.post(f"{self.host}/api/v1/placeorder",
                               json={**payload, "apikey": self.api_key}, timeout=30)
            return r.json(), r.status_code
        except Exception as ex:
            return {"status": "error", "message": f"connection error: {ex}"}, 0

    # ------------------------------------------------------
    # Ground-truth trade log (OpenAlgo's own order/trade books)
    # ------------------------------------------------------

    # Words that mean "the BROKER logged us out", as opposed to OpenAlgo
    # itself being unhappy. Flattrade's own wording is
    # "Session Expired :  Invalid Session Key" (seen for real 2026-08-25,
    # when it silently cost ORB a full trading day); the others are here
    # because the exact string is the broker's to change, not ours.
    _SESSION_DEAD_MARKERS = ("session expired", "invalid session", "session key",
                              "not authenticated", "login required", "unauthorized")

    SESSION_OK = "ok"
    SESSION_DEAD = "session_dead"
    SESSION_OPENALGO_DOWN = "openalgo_down"
    SESSION_UNKNOWN = "unknown"

    def check_broker_session(self, probe_symbol: str = "NIFTY", probe_exchange: str = "NSE_INDEX") -> tuple[str, str]:
        """Is the BROKER session actually alive right now? Returns
        (state, human message).

        Probes with a real index quote rather than /funds. That matters:
        in analyzer/sandbox mode /funds answers from OpenAlgo's own
        simulated account and returns a cheerful success with play money
        even when the broker session is completely dead -- so a /funds
        check would have reported "connected" throughout the morning of
        2026-08-25 while every history call was failing with
        "Session Expired". A quote has to reach the broker to be
        answered, so it can't be faked by sandbox mode.
        """
        if not self.ping():
            return self.SESSION_OPENALGO_DOWN, "OpenAlgo isn't running or isn't reachable."

        data = self._post("quotes", {"symbol": probe_symbol, "exchange": probe_exchange}) or {}
        if data.get("status") == "success":
            return self.SESSION_OK, "Broker session is active."

        message = str(data.get("message", "")) or "Broker did not answer a live quote request."
        low = message.lower()
        if any(marker in low for marker in self._SESSION_DEAD_MARKERS):
            return self.SESSION_DEAD, message
        return self.SESSION_UNKNOWN, message

    def test_connection(self) -> tuple[bool, str]:
        """End-to-end check for the setup wizard's Test Connection step:
        confirms OpenAlgo is up AND the broker is genuinely authenticated.
        Delegates to check_broker_session() so the wizard and the status
        strip can never disagree about what "connected" means."""
        state, message = self.check_broker_session()
        if state == self.SESSION_OK:
            return True, "Connected -- broker session is active."
        return False, message

    def get_tradebook(self) -> list[dict]:
        data = self._post("tradebook", {}) or {}
        return data.get("data", []) if data.get("status") == "success" else []

    def get_orderbook(self) -> list[dict]:
        data = self._post("orderbook", {}) or {}
        return data.get("data", []) if data.get("status") == "success" else []

    def get_positionbook(self) -> list[dict]:
        data = self._post("positionbook", {}) or {}
        return data.get("data", []) if data.get("status") == "success" else []

    # ------------------------------------------------------
    # Bar data (from the original data_provider.py OpenAlgoClient)
    # ------------------------------------------------------

    def _reanchor_intraday(self, ts: pd.Series) -> pd.DatetimeIndex:
        """OpenAlgo's Flattrade adapter returns epoch timestamps whose
        time-of-day is wrong by a seasonally-varying amount (a DST bug from
        some non-IST timezone). Prices/dates are correct; only the
        intraday clock label is broken. Fix: re-anchor each day's first bar
        to MARKET_OPEN and keep every other bar's elapsed offset from it.
        Kept exactly as validated in the original data_provider.py."""
        ts = pd.Series(ts.values, index=ts.index) if not isinstance(ts, pd.Series) else ts
        day = ts.dt.normalize()
        open_hour, open_minute = (int(x) for x in MARKET_OPEN.split(":"))
        day_open = day + pd.Timedelta(hours=open_hour, minutes=open_minute)
        first_of_day = ts.groupby(day).transform("min")
        elapsed = ts - first_of_day
        return pd.DatetimeIndex(day_open + elapsed)

    def history(self, symbol: str, interval: str, start_date: str, end_date: str,
                exchange: str | None = None) -> pd.DataFrame:
        payload = {"symbol": symbol, "exchange": exchange or self.exchange,
                   "interval": interval, "start_date": start_date, "end_date": end_date}
        data = self._post("history", payload, timeout=60)
        if data is None or data.get("status") != "success":
            return pd.DataFrame()

        rows = data.get("data", [])
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        if "timestamp" not in df.columns:
            return pd.DataFrame()

        ts = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        ts = ts.dt.tz_convert(IST) if isinstance(ts, pd.Series) else ts.tz_convert(IST)
        if interval not in ("D", "W", "M", "Q", "Y"):
            ts = self._reanchor_intraday(ts)

        df.index = ts
        df.index.name = "datetime"
        columns = ["open", "high", "low", "close", "volume"]
        available = [c for c in columns if c in df.columns]
        df = df[available]
        for col in available:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df.dropna(inplace=True)
        df = df[~df.index.duplicated()]
        df.sort_index(inplace=True)
        return df

    def get_history(self, symbol: str, interval: str, start_date: str, end_date: str,
                     exchange: str | None = None) -> pd.DataFrame:
        key = str(interval).lower()
        if key not in INTERVAL_MAP:
            raise ValueError(f"Unsupported interval: {interval}")
        return self.history(symbol, INTERVAL_MAP[key], start_date, end_date, exchange=exchange)

    def get_daily(self, symbol: str, start_date: str, end_date: str, exchange: str | None = None) -> pd.DataFrame:
        return self.get_history(symbol, "daily", start_date, end_date, exchange=exchange)

    def get_minute(self, symbol: str, start_date: str, end_date: str, interval: str = "1m",
                    exchange: str | None = None) -> pd.DataFrame:
        return self.get_history(symbol, interval, start_date, end_date, exchange=exchange)
