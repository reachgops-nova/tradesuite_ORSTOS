"""
==========================================================
TradeSuite
licensing/mail_relay.py
==========================================================

Outbound email from the customer's install, via the operator's relay
account (see licensing/server_config.py for why that account must be a
dedicated one).

Two jobs:
  - renewal summaries: the customer's install reports its own P&L for the
    subscription period that just ended, to the admin address.
  - license key delivery: the admin-side tooling emails an issued key to
    the customer as soon as payment is confirmed.

Everything here is fail-soft by design. A blocked port, an offline
machine, a revoked app password -- none of that may ever stop a customer
from trading. Every entry point returns (ok, message) and swallows its
own exceptions; nothing raises into the GUI.
"""

from __future__ import annotations

import smtplib
import socket
import ssl
from email.message import EmailMessage

from licensing import server_config

TIMEOUT_SEC = 20


def send(to: str, subject: str, body: str, attachments: list[tuple[str, str]] | None = None) -> tuple[bool, str]:
    """attachments: list of (filename, text_content). Returns (ok, msg)."""
    mail = server_config.mail_settings()
    if not mail:
        return False, "No mail relay configured in this build."
    if not to:
        return False, "No recipient address."

    msg = EmailMessage()
    msg["From"] = f"{mail['from_name']} <{mail['username']}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    for filename, content in (attachments or []):
        msg.add_attachment(content.encode(), maintype="text", subtype="csv", filename=filename)

    try:
        port = int(mail["smtp_port"])
        if port == 465:
            with smtplib.SMTP_SSL(mail["smtp_host"], port, timeout=TIMEOUT_SEC,
                                   context=ssl.create_default_context()) as s:
                s.login(mail["username"], mail["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(mail["smtp_host"], port, timeout=TIMEOUT_SEC) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(mail["username"], mail["password"])
                s.send_message(msg)
        return True, f"Sent to {to}."
    except smtplib.SMTPAuthenticationError:
        # The single most likely real-world failure: the relay account's app
        # password was rotated/revoked and this build still carries the old
        # one. Named explicitly so the log says what to actually do about it.
        return False, ("Mail relay rejected the login -- the app password in this build is no longer valid. "
                       "Ship a build with a bumped server-config rev, or push a server_config.json override.")
    except (smtplib.SMTPException, socket.error, OSError, ValueError) as e:
        return False, f"Could not send mail: {type(e).__name__}: {e}"


def send_to_admin(subject: str, body: str, attachments: list[tuple[str, str]] | None = None) -> tuple[bool, str]:
    admin = server_config.admin_email()
    if not admin:
        return False, "No admin address configured in this build."
    return send(admin, subject, body, attachments)


def test_relay() -> tuple[bool, str]:
    """Admin-side check that the embedded credentials actually work --
    logs in and disconnects without sending anything."""
    mail = server_config.mail_settings()
    if not mail:
        return False, "No mail relay configured in this build."
    try:
        port = int(mail["smtp_port"])
        if port == 465:
            with smtplib.SMTP_SSL(mail["smtp_host"], port, timeout=TIMEOUT_SEC,
                                   context=ssl.create_default_context()) as s:
                s.login(mail["username"], mail["password"])
        else:
            with smtplib.SMTP(mail["smtp_host"], port, timeout=TIMEOUT_SEC) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(mail["username"], mail["password"])
        return True, f"Relay login OK: {mail['username']} via {mail['smtp_host']}:{port}."
    except Exception as e:  # admin-side diagnostic -- report anything, never raise
        return False, f"Relay login failed: {type(e).__name__}: {e}"
