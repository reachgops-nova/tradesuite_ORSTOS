"""
==========================================================
TradeSuite
engine/config_store.py
==========================================================

Per-install configuration. Replaces the hardcoded-constants pattern from
OrbStrategy/config.py and TamilOptionSeller30/config.py (plaintext
OA_HOST/OA_API_KEY literals duplicated across files, strategy parameters
as bare module constants, zero env/CLI override) with a single JSON file
per customer install, so each copy of TradeSuite points at that
customer's own local OpenAlgo instance and lets the GUI select/tune a
strategy without touching source code.

File location: next to the running executable when frozen (PyInstaller
sets sys.frozen), else next to this project's root during development --
same convention SureFramePro's license_core.py uses for sf_license.json,
kept consistent on purpose.
==========================================================
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

CONFIG_VERSION = 1

DEFAULT_SETTINGS = {
    "version": CONFIG_VERSION,
    "openalgo": {
        "host": "http://127.0.0.1:5001",
        "api_key": "",
        "exchange": "NSE",
    },
    "broker": {
        # OpenAlgo hard-refuses to even START without a validly-formatted
        # broker key already in its .env -- confirmed 2026-08-24 by hitting
        # "Error: Invalid Flattrade API key format detected!" for real, at
        # startup, before the web server (and so /setup and /broker) is
        # even reachable. So this has to be collected by TradeSuite's own
        # wizard BEFORE first bootstrap/launch, not through OpenAlgo's web
        # UI afterward as originally assumed.
        "name": "flattrade",
        "client_id": "",
        "api_key": "",       # Flattrade's own API key (not OpenAlgo's apikey above -- different thing)
        "api_secret": "",
        # Optional: a command this machine can run to re-establish the
        # broker session without a browser. Left EMPTY by default and for
        # every shipped install -- a customer has no such script, and the
        # Reconnect button simply stays hidden for them. It exists because
        # the broker resets the session daily (~03:30 IST for Flattrade),
        # so the recovery is the same few steps every single time and is
        # worth one click rather than a manual web login.
        "auto_login_command": "",
    },
    "orb": {
        "enabled": False,
        "exit_mode": "reversal",  # "reversal" (2-bar index reversal, the proven default) | "pct_3" (giveback-buffer candidate)
        "underlyings": [
            {"name": "NIFTY", "index_symbol": "NIFTY", "quote_exchange": "NSE_INDEX", "opt_exchange": "NFO", "lot_size": 65, "strike_step": 50},
            {"name": "BANKNIFTY", "index_symbol": "BANKNIFTY", "quote_exchange": "NSE_INDEX", "opt_exchange": "NFO", "lot_size": 30, "strike_step": 100},
            {"name": "SENSEX", "index_symbol": "SENSEX", "quote_exchange": "BSE_INDEX", "opt_exchange": "BFO", "lot_size": 20, "strike_step": 100},
        ],
        # How many exchange lots to trade per signal. Quantity is always
        # lot_size x lots -- NEVER a free-form quantity, because index
        # options only trade in whole lots and the exchange rejects
        # anything else. A customer with more capital raises this; the
        # per-underlying lot_size below is the exchange's own contract
        # size and is not theirs to edit.
        "lots": 1,
        "volume_window_start": "09:15",
        "volume_window_end": "09:25",
        "entry_target_time": "09:26",
        "give_up_time": "10:00",
        "reversal_bars": 2,
        "pct_3_value": 0.03,
        "max_hold_min": 45,
        "poll_interval_sec": 30,
        "flat_cost_per_lot": 60,
    },
    "tamil": {
        "enabled": False,
        "underlyings": [
            {"name": "NIFTY", "step": 50, "lot": 65, "index_symbol": "NIFTY", "quote_exchange": "NSE_INDEX", "opt_exchange": "NFO"},
            {"name": "BANKNIFTY", "step": 100, "lot": 30, "index_symbol": "BANKNIFTY", "quote_exchange": "NSE_INDEX", "opt_exchange": "NFO"},
            {"name": "SENSEX", "step": 100, "lot": 20, "index_symbol": "SENSEX", "quote_exchange": "BSE_INDEX", "opt_exchange": "BFO"},
        ],
        "lots": 1,  # see orb.lots -- quantity is always lot x lots
        "sl_points": 25.0,
        "target_points": 48.5,
        "trade_weekdays": [1, 2, 3],  # Python weekday(): Mon=0 .. Sun=6 -> Tue/Wed/Thu
        "poll_interval_sec": 30,
        "flat_cost_per_lot": 60,
    },
}

CONFIG_FILENAME = "tradesuite_settings.json"


def _install_dir() -> Path:
    """Where TradeSuite's own mutable data (settings, license file, trade
    logs, the fetched OpenAlgo runtime) lives. NOT the same as where the
    EXE itself is installed once there's a real installer: Program Files
    (where Inno Setup puts the EXE) isn't writable by a standard user at
    runtime, so frozen builds use %LOCALAPPDATA%\\TradeSuite instead --
    the standard, correct place for a Windows app's own per-user data.
    Dev mode (not frozen) keeps using the project root for convenience."""
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        data_dir = base / "TradeSuite"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir
    return Path(__file__).resolve().parent.parent


def _deep_merge_defaults(loaded: dict, defaults: dict) -> dict:
    """Fills in any keys missing from an older/partial settings file with
    current defaults, so adding a new setting later doesn't break existing
    installs' saved config -- same spirit as OpenAlgo's own .env being
    additive across versions, applied to our own settings file."""
    merged = dict(defaults)
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_defaults(value, merged[key])
        else:
            merged[key] = value
    return merged


class ConfigStore:
    def __init__(self, path: Path | None = None):
        self.install_dir = _install_dir()
        self.path = path or (self.install_dir / CONFIG_FILENAME)
        self.output_dir = self.install_dir / "output"
        self.logs_dir = self.install_dir / "logs"
        self.trades_dir = self.output_dir / "paper_trades"
        self.tamil_trades_dir = self.output_dir / "tamil_option_seller_trades"
        self.whatsapp_trades_dir = self.output_dir / "group_signal_trades"
        self.trail_study_dir = self.output_dir / "trail_study"
        self.reports_dir = self.output_dir / "reports"
        for d in (self.output_dir, self.logs_dir, self.trades_dir, self.tamil_trades_dir,
                   self.whatsapp_trades_dir, self.trail_study_dir, self.reports_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.settings = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return json.loads(json.dumps(DEFAULT_SETTINGS))  # deep copy
        try:
            with open(self.path) as f:
                loaded = json.load(f)
        except (json.JSONDecodeError, OSError):
            # Falling back to defaults means the customer's broker connection
            # silently reverts to blank -- which, hitting right after an
            # upgrade, reads as "the new version lost my settings". Keep the
            # damaged file so it can actually be recovered instead of being
            # overwritten by the next save().
            self._quarantine_bad_file()
            return json.loads(json.dumps(DEFAULT_SETTINGS))
        return _deep_merge_defaults(loaded, DEFAULT_SETTINGS)

    def _quarantine_bad_file(self) -> None:
        try:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.path.replace(self.path.with_suffix(f".corrupt-{stamp}.json"))
        except OSError:
            pass

    def save(self) -> None:
        """Written via a temp file + atomic replace: a crash or power loss
        partway through a save must not leave a half-written settings file
        that reads as corrupt on next launch (which is the main way the
        quarantine path above would ever get exercised)."""
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(self.settings, f, indent=2)
        tmp.replace(self.path)

    # -- convenience accessors -------------------------------------------

    @property
    def openalgo_host(self) -> str:
        return self.settings["openalgo"]["host"].rstrip("/")

    @property
    def openalgo_api_key(self) -> str:
        return self.settings["openalgo"]["api_key"]

    @property
    def openalgo_exchange(self) -> str:
        return self.settings["openalgo"]["exchange"]

    def is_broker_configured(self) -> bool:
        return bool(self.openalgo_api_key)

    def is_broker_credentials_set(self) -> bool:
        """Whether Flattrade's own client_id/api_key/api_secret have been
        entered -- required before OpenAlgo can even start (see the
        "broker" section comment above), separate from
        is_broker_configured() which is about OpenAlgo's own apikey for
        TradeSuite<->OpenAlgo communication, set later via /setup."""
        b = self.settings["broker"]
        return bool(b["client_id"] and b["api_key"] and b["api_secret"])

    @property
    def broker_combined_api_key(self) -> str:
        """The 'client_id:::api_key' format OpenAlgo's .env expects for
        Flattrade specifically (confirmed against utils/env_check.py)."""
        b = self.settings["broker"]
        return f"{b['client_id']}:::{b['api_key']}"

    @property
    def broker_api_secret(self) -> str:
        return self.settings["broker"]["api_secret"]

    def set_broker_credentials(self, client_id: str, api_key: str, api_secret: str, name: str | None = None) -> None:
        update = dict(client_id=client_id, api_key=api_key, api_secret=api_secret)
        if name:
            update["name"] = name.strip().lower()
        self.settings["broker"].update(**update)
        self.save()

    @property
    def broker_name(self) -> str:
        return self.settings["broker"].get("name") or "flattrade"

    @property
    def auto_login_command(self) -> str:
        return (self.settings.get("broker") or {}).get("auto_login_command", "") or ""

    def orb_settings(self) -> dict:
        return self.settings["orb"]

    def tamil_settings(self) -> dict:
        return self.settings["tamil"]

    def set_openalgo(self, host: str = None, api_key: str = None) -> None:
        if host is not None:
            self.settings["openalgo"]["host"] = host
        if api_key is not None:
            self.settings["openalgo"]["api_key"] = api_key
        self.save()

    def set_strategy_enabled(self, strategy: str, enabled: bool) -> None:
        if strategy not in ("orb", "tamil"):
            raise ValueError(f"Unknown strategy: {strategy}")
        self.settings[strategy]["enabled"] = enabled
        self.save()

    def lots(self, strategy: str) -> int:
        """Clamped to >=1: a saved 0 or negative (hand-edited settings file)
        would otherwise silently place zero-quantity orders all day."""
        try:
            return max(1, int(self.settings[strategy].get("lots", 1)))
        except (TypeError, ValueError):
            return 1

    def set_lots(self, strategy: str, lots: int) -> None:
        self.settings[strategy]["lots"] = max(1, int(lots))
        self.save()

    def set_orb_exit_mode(self, mode: str) -> None:
        if mode not in ("reversal", "pct_3"):
            raise ValueError(f"Unknown ORB exit mode: {mode}")
        self.settings["orb"]["exit_mode"] = mode
        self.save()
