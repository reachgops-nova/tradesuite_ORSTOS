"""
==========================================================
TradeSuite
installer/embed_server_config.py
==========================================================

Build-time step: bakes the operator's server settings (mail relay
account, admin address) into `licensing/_server_blob.py` so the shipped
exe carries them, without the plaintext ever living in a source file.

Usage:
    python installer/embed_server_config.py --secrets C:\\path\\to\\tradesuite_server_secrets.json
    python installer/embed_server_config.py --clear        # back to the empty placeholder

Secrets file format:
    {
      "rev": 1,
      "admin_email": "you@example.com",
      "mail": {
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 465,
        "username": "relay-account@gmail.com",
        "password": "<16-char Gmail app password, no spaces>",
        "from_name": "TradeSuite"
      }
    }

**Keep that file OUT of the repo and off any shared drive.**

## `rev` is the whole upgrade mechanism -- bump it every time

`licensing/server_config.py` picks whichever config source has the
highest `rev`. So when the relay account changes, bump `rev`, rebuild,
and ship: every customer who installs the new version picks up the new
credentials automatically, and their existing settings/history/license
are untouched because none of that lives in the program directory.

Forget to bump it and a customer's older same-rev override file could
keep winning -- which is exactly the silent failure this is designed to
prevent, so the script refuses to embed a rev that isn't higher than the
one currently embedded unless you pass --force.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from licensing.server_config import pack, unpack, REQUIRED_MAIL_FIELDS  # noqa: E402

BLOB_FILE = PROJECT_ROOT / "licensing" / "_server_blob.py"
PLACEHOLDER_HEADER = '"""\n==========================================================\nTradeSuite\nlicensing/_server_blob.py\n=========================================================='


def current_rev() -> int:
    try:
        text = BLOB_FILE.read_text()
    except OSError:
        return 0
    for line in text.splitlines():
        if line.startswith("SERVER_BLOB"):
            blob = line.split("=", 1)[1].strip().strip('"')
            cfg = unpack(blob) or {}
            return int(cfg.get("rev", 0))
    return 0


def write_blob(blob: str, note: str) -> None:
    BLOB_FILE.write_text(
        '"""\n'
        "==========================================================\n"
        "TradeSuite\n"
        "licensing/_server_blob.py\n"
        "==========================================================\n\n"
        "GENERATED FILE -- do not edit by hand, and do not commit a populated\n"
        "version of it. Written by installer/embed_server_config.py.\n\n"
        f"{note}\n"
        '"""\n\n'
        f'SERVER_BLOB = "{blob}"\n'
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Embed TradeSuite server settings into the build.")
    ap.add_argument("--secrets", type=Path, help="Path to the secrets JSON (kept outside the repo).")
    ap.add_argument("--clear", action="store_true", help="Reset to the empty placeholder.")
    ap.add_argument("--force", action="store_true", help="Allow embedding a rev that isn't higher than the current one.")
    args = ap.parse_args()

    if args.clear:
        write_blob("", "Empty placeholder: no mail relay configured in this build.")
        print(f"Cleared {BLOB_FILE} -- this build ships with no mail relay.")
        return 0

    if not args.secrets:
        ap.error("one of --secrets or --clear is required")
    if not args.secrets.exists():
        print(f"ERROR: secrets file not found: {args.secrets}", file=sys.stderr)
        return 1

    cfg = json.loads(args.secrets.read_text())

    mail = cfg.get("mail") or {}
    missing = [f for f in REQUIRED_MAIL_FIELDS if not mail.get(f)]
    if missing:
        print(f"ERROR: mail settings missing required field(s): {', '.join(missing)}", file=sys.stderr)
        return 1

    rev = int(cfg.get("rev", 0))
    existing = current_rev()
    if rev <= existing and not args.force:
        print(f"ERROR: rev {rev} is not higher than the currently embedded rev {existing}.\n"
              f"       Bump \"rev\" in {args.secrets.name} so this build supersedes existing installs,\n"
              f"       or pass --force if you really mean to re-embed the same revision.", file=sys.stderr)
        return 1

    write_blob(pack(cfg), f"Embedded rev {rev}: relay {mail['username']} via {mail['smtp_host']}.")
    print(f"Embedded server config rev {rev} ({mail['username']}) into {BLOB_FILE.name}.")
    print("Reminder: this is obfuscated, NOT encrypted -- use a dedicated relay account, never a personal mailbox.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
