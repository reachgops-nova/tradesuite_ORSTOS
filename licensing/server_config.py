"""
==========================================================
TradeSuite
licensing/server_config.py
==========================================================

"Server settings" -- the operator-side configuration (currently the mail
relay account) that ships WITH the product rather than being entered by
the customer.

## Read this before putting a real password anywhere near it

The embedded blob below is **obfuscated, not encrypted**. The unlocking
key ships in the same binary, because it has to -- the app must be able
to read it unattended on a machine we don't control. Anyone holding the
installer can recover the plaintext. That is not a flaw in this module;
it is the unavoidable property of shipping a shared secret to end users,
and no amount of crypto changes it.

So: the account whose credentials go in here must be a **dedicated relay
account that holds nothing of value** -- not a personal mailbox. A Gmail
app password grants IMAP read of the entire mailbox, not just outbound
send, so shipping a personal account's app password hands every customer
a copy of the inbox. Gopinath's plan (2026-08-25) is to move to a common
operator ID; this module is built so that switch is a config change, not
a code change.

## Precedence: highest `rev` wins

Three sources, merged by revision number rather than by fixed priority:

1. `%LOCALAPPDATA%\\TradeSuite\\server_config.json` -- a plain-JSON
   override that can be sent to an existing customer to rotate
   credentials WITHOUT reshipping the app.
2. `TRADESUITE_SERVER_CONFIG` env var (a path) -- for development and
   testing, so nobody has to touch a real relay account to run tests.
3. The blob embedded in this build.

Whichever carries the highest `rev` wins outright. That satisfies both
directions Gopinath asked for: bump `rev` in a NEW BUILD and the new
build's credentials supersede whatever an old install already had (his
"send a revised version to customer" case); bump `rev` in a pushed
override file and it supersedes the build, no rebuild needed. Ties go to
the embedded build config, so a stale same-rev file can never shadow it.

The plaintext must never be committed to source. `_server_blob.py` is
generated at build time by `installer/embed_server_config.py` from a
secrets file kept outside the repo; the placeholder checked in here is
empty, and an empty/absent blob simply disables the relay rather than
breaking the app.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

# Obfuscation key. Deliberately NOT the license-signing secret from
# license_core.py -- recovering this one (which every customer can do)
# must not hand anyone the key that signs licenses.
_OBFUSCATION_SALT = b"TradeSuite/server_config/v1"

REQUIRED_MAIL_FIELDS = ("smtp_host", "smtp_port", "username", "password", "from_name")


def _keystream(length: int) -> bytes:
    """Deterministic keystream from the salt. SHA-256 chained so the key
    doesn't repeat every 32 bytes -- a repeating XOR key over structured
    text like JSON is trivially recoverable even by someone not looking
    for it, and there's no reason to make it easier than it already is."""
    out, block = bytearray(), _OBFUSCATION_SALT
    while len(out) < length:
        block = hashlib.sha256(block).digest()
        out.extend(block)
    return bytes(out[:length])


def pack(config: dict) -> str:
    """dict -> the obfuscated string that goes into _server_blob.py."""
    raw = json.dumps(config, sort_keys=True).encode()
    key = _keystream(len(raw))
    return base64.b64encode(bytes(a ^ b for a, b in zip(raw, key))).decode()


def unpack(blob: str) -> dict | None:
    if not blob:
        return None
    try:
        raw = base64.b64decode(blob)
        key = _keystream(len(raw))
        return json.loads(bytes(a ^ b for a, b in zip(raw, key)).decode())
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None  # corrupt/placeholder blob disables the relay; never crashes the app


def _embedded() -> dict | None:
    try:
        from licensing._server_blob import SERVER_BLOB
    except ImportError:
        return None
    return unpack(SERVER_BLOB)


def _from_file(path: Path) -> dict | None:
    if not path or not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def override_path() -> Path:
    from engine.config_store import _install_dir
    return _install_dir() / "server_config.json"


def load() -> dict:
    """The winning config, or {} if nothing is configured. Never raises --
    a missing or malformed relay config must degrade to "email features
    off", never to a customer-visible crash on launch."""
    candidates = [c for c in (
        _embedded(),
        _from_file(override_path()),
        _from_file(Path(os.environ["TRADESUITE_SERVER_CONFIG"])) if os.environ.get("TRADESUITE_SERVER_CONFIG") else None,
    ) if isinstance(c, dict)]
    if not candidates:
        return {}
    # Highest rev wins; the embedded build config is first in the list, so
    # max() with its default first-wins-on-tie behaviour keeps it ahead of a
    # stale same-rev override file.
    return max(candidates, key=lambda c: int(c.get("rev", 0)))


def mail_settings() -> dict | None:
    """The relay account's SMTP settings, or None if not configured."""
    mail = load().get("mail")
    if not isinstance(mail, dict):
        return None
    if any(not mail.get(f) for f in REQUIRED_MAIL_FIELDS):
        return None
    return mail


def admin_email() -> str | None:
    """Where customer renewal summaries are sent. Falls back to the relay
    account itself (send-to-self), which is the common case."""
    cfg = load()
    return cfg.get("admin_email") or (cfg.get("mail") or {}).get("username")


def is_configured() -> bool:
    return mail_settings() is not None


def describe() -> str:
    """One line for the admin/status UI -- never includes the password."""
    mail = mail_settings()
    if not mail:
        return "Mail relay: not configured (license emails and renewal reports are off)."
    return (f"Mail relay rev {load().get('rev', 0)}: {mail['username']} "
            f"via {mail['smtp_host']}:{mail['smtp_port']} -> admin {admin_email()}")
