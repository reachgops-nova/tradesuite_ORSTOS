"""
==========================================================
TradeSuite
licensing/license_core.py
==========================================================

Calendar-based license: local expiry date + QR-UPI renewal, mechanics
ported from SureFramePro_v17_FIXED's license_core.py (device fingerprint,
HMAC-signed redemption keys, local JSON license file) with two things
SureFramePro didn't have at all, added here on purpose:

1. Real calendar expiry. SureFramePro is credit/consumption-based -- no
   date field, no expiry check anywhere. TradeSuite needs "pay monthly,
   renew before it runs out," so this adds expiry_date and checks it
   against today on every launch.
2. Tamper resistance on the license file itself. SureFramePro signs only
   its redemption *keys* -- the local sf_license.json is a bare unsigned
   JSON blob, trivially hand-edited (confirmed: e.g. bump "credits" in
   Notepad and the app trusts it). Here the whole license file is
   HMAC-signed, and a monotonically-advancing last_seen timestamp guards
   against the classic "roll the system clock back" bypass. Still simple
   (one HMAC compare, one timestamp compare, no network call) -- this is
   a cheap correctness fix, not real DRM, and isn't meant to be one.

Redemption keys stay a manual, offline flow exactly like SureFramePro's:
admin_keygen.py (run by Gopinath after he manually verifies a UPI
payment) generates a signed key; the customer pastes it into the app.
No payment gateway, no server round-trip -- matches the explicit
"keep it simple" instruction.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from licensing.device_fingerprint import get_machine_id

# ==========================================================
# TESTING MODE -- superseded 2026-08-24 by DEFAULT_TRIAL_DAYS (see
# register() below): registration now auto-grants a real 30-day trial
# instead of bypassing the license check entirely, so testers see the
# same flow a real customer will. Left here, defaulted off, as an escape
# hatch if a future session needs to skip licensing entirely again.
# ==========================================================
TESTING_MODE = False

# NOTE: change this before shipping to real customers, and keep it out of
# version control / anywhere customers can read it (same constraint
# SureFramePro's KEY_SECRET has -- it must match between this file and
# admin_keygen.py, and nowhere else).
KEY_SECRET = "TradeSuite_2026_@Gopinath_SecretSalt_#$%"

LICENSE_FILENAME = "ts_license.json"
DATE_FMT = "%Y%m%d"


def _install_dir() -> Path:
    """Must match engine/config_store.py's _install_dir() exactly -- both
    files' data need to live in the same per-user directory, not split
    between Program Files and %LOCALAPPDATA% depending on which module
    happens to resolve the path."""
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        data_dir = base / "TradeSuite"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir
    return Path(__file__).resolve().parent.parent


def _license_path() -> Path:
    return _install_dir() / LICENSE_FILENAME


def _sign(*parts: str) -> str:
    payload = "|".join(parts) + "|" + KEY_SECRET
    digest = hmac.new(KEY_SECRET.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.b32encode(digest).decode().rstrip("=")[:16]


# ------------------------------------------------------------------
# Redemption keys -- format: TS-<MID8>-<GRANTDAYS>-<REDEEMBY_YYYYMMDD>-<SIG16>
# ------------------------------------------------------------------

def generate_key(machine_id: str, grant_days: int, redeem_by: date) -> str:
    mid8 = machine_id[:8]
    redeem_by_str = redeem_by.strftime(DATE_FMT)
    sig = _sign(mid8, str(grant_days), redeem_by_str)
    return f"TS-{mid8}-{grant_days}-{redeem_by_str}-{sig}"


def _parse_key(raw: str) -> dict | None:
    parts = raw.strip().split("-")
    if len(parts) != 5 or parts[0] != "TS":
        return None
    _, mid8, days_str, redeem_by_str, sig = parts
    try:
        grant_days = int(days_str)
        redeem_by = datetime.strptime(redeem_by_str, DATE_FMT).date()
    except ValueError:
        return None
    return {"mid8": mid8, "grant_days": grant_days, "redeem_by": redeem_by, "redeem_by_str": redeem_by_str, "sig": sig}


# ------------------------------------------------------------------
# License file: load / sign / save
# ------------------------------------------------------------------

@dataclass
class LicenseStatus:
    licensed: bool
    days_remaining: int
    reason: str
    machine_id: str
    expiry_date: str | None
    registered: bool


def _signable_fields(data: dict) -> str:
    return "|".join([
        data.get("machine_id", ""), data.get("expiry_date") or "", data.get("last_seen") or "",
        data.get("name") or "", data.get("email") or "", data.get("phone") or "",
        ",".join(sorted(data.get("used_keys", []))),
    ])


def _sign_license_data(data: dict) -> str:
    return _sign(_signable_fields(data))


def _default_license_data() -> dict:
    return {
        "machine_id": get_machine_id(), "name": None, "email": None, "phone": None,
        "expiry_date": None, "last_seen": None, "used_keys": [],
    }


def _load_raw() -> dict:
    path = _license_path()
    if not path.exists():
        return _default_license_data()
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _default_license_data()

    if data.get("machine_id") != get_machine_id():
        # license file for a different machine (e.g. copied over) -- don't honor it
        return _default_license_data()

    expected_sig = _sign_license_data(data)
    if not hmac.compare_digest(data.get("signature", ""), expected_sig):
        # tampered or foreign file -- reset to unlicensed rather than trust any field in it
        fresh = _default_license_data()
        fresh["name"], fresh["email"], fresh["phone"] = data.get("name"), data.get("email"), data.get("phone")
        return fresh
    return data


def _save(data: dict) -> None:
    data["signature"] = _sign_license_data(data)
    with open(_license_path(), "w") as f:
        json.dump(data, f, indent=2)


DEFAULT_TRIAL_DAYS = 30


def register(name: str, email: str, phone: str) -> None:
    """First-run: capture contact info AND auto-grant a free trial (30
    days by default -- Gopinath's instruction 2026-08-24: "1 month free by
    default", replacing the earlier design where even the trial required
    an admin-issued key before any access). Renewal after the trial still
    goes through the normal QR-UPI + admin-keygen flow -- this only
    changes what happens at first registration."""
    data = _load_raw()
    data["name"], data["email"], data["phone"] = name, email, phone
    data.setdefault("used_keys", [])
    if not data.get("expiry_date"):  # only grant once -- don't reset an existing/expired license on re-registration
        data["expiry_date"] = (date.today() + timedelta(days=DEFAULT_TRIAL_DAYS)).strftime(DATE_FMT)
    _save(data)


def redeem_key(raw_key: str) -> tuple[bool, str]:
    parsed = _parse_key(raw_key)
    if parsed is None:
        return False, "Key format not recognized."

    data = _load_raw()
    machine_id = data.get("machine_id") or get_machine_id()
    if parsed["mid8"] != machine_id[:8]:
        return False, "This key was issued for a different machine."

    expected_sig = _sign(parsed["mid8"], str(parsed["grant_days"]), parsed["redeem_by_str"])
    if not hmac.compare_digest(parsed["sig"], expected_sig):
        return False, "Key signature invalid -- it may have been altered."

    if raw_key in data.get("used_keys", []):
        return False, "This key has already been redeemed."

    if date.today() > parsed["redeem_by"]:
        return False, f"This key expired {parsed['redeem_by']} and can no longer be redeemed -- request a new one."

    current_expiry = _parse_expiry(data.get("expiry_date"))
    base = max(current_expiry, date.today()) if current_expiry else date.today()
    new_expiry = base + timedelta(days=parsed["grant_days"])

    data["expiry_date"] = new_expiry.strftime(DATE_FMT)
    data.setdefault("used_keys", []).append(raw_key)
    _save(data)
    return True, f"License extended to {new_expiry.isoformat()} ({parsed['grant_days']} days added)."


def _parse_expiry(expiry_str: str | None) -> date | None:
    if not expiry_str:
        return None
    try:
        return datetime.strptime(expiry_str, DATE_FMT).date()
    except ValueError:
        return None


def check_status() -> LicenseStatus:
    if TESTING_MODE:
        return LicenseStatus(True, 9999, "Testing mode -- license check bypassed.",
                              get_machine_id(), None, True)

    data = _load_raw()
    machine_id = data.get("machine_id") or get_machine_id()
    registered = bool(data.get("email"))
    expiry = _parse_expiry(data.get("expiry_date"))

    now = datetime.now()
    last_seen = None
    if data.get("last_seen"):
        try:
            last_seen = datetime.fromisoformat(data["last_seen"])
        except ValueError:
            last_seen = None

    clock_rolled_back = last_seen is not None and now < last_seen
    data["last_seen"] = max(now, last_seen).isoformat() if last_seen else now.isoformat()
    _save(data)

    if clock_rolled_back:
        return LicenseStatus(False, 0, "System clock appears to have been moved backward -- license check failed.",
                              machine_id, data.get("expiry_date"), registered)
    if not registered:
        return LicenseStatus(False, 0, "Not registered yet.", machine_id, None, registered)
    if expiry is None:
        # Registered but never granted anything -- auto-grant the trial here
        # too, not just inside register(). Covers both a genuinely fresh
        # registration AND an already-registered record from before the
        # trial-grant existed (confirmed live 2026-08-24: Gopinath's own
        # test install had registered=True/expiry_date=null from an earlier
        # build and was stuck on the renewal screen forever, since
        # RegistrationView -- the only place register() gets called from --
        # never shows again once registered=True).
        expiry = date.today() + timedelta(days=DEFAULT_TRIAL_DAYS)
        data["expiry_date"] = expiry.strftime(DATE_FMT)
        _save(data)

    days_remaining = (expiry - now.date()).days
    if days_remaining < 0:
        return LicenseStatus(False, 0, f"License expired {expiry.isoformat()}.", machine_id,
                              data.get("expiry_date"), registered)
    return LicenseStatus(True, days_remaining, "Licensed.", machine_id, data.get("expiry_date"), registered)
