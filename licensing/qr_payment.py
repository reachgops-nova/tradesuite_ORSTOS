"""
==========================================================
TradeSuite
licensing/qr_payment.py
==========================================================

Customer-facing renewal flow: static UPI QR (scan, pay any amount you've
agreed) -> customer types their UPI transaction ID into the app -> app
hands the admin everything needed to verify and issue a key. Mechanics
match SureFramePro's app.py payment dialog (qrcode-rendered upi://pay
deep link, manual transaction-ID entry, no payment gateway) -- matches
the explicit "keep it simple" instruction.

Deliberate change from SureFramePro's pattern: SureFramePro emails the
admin via SMTP using credentials hardcoded in config.py -- fine for a
tool only Gopinath runs, but wrong here, since this module ships inside
every customer's installed binary. Embedding an admin Gmail app password
in a binary handed to third parties means every customer's copy can have
that password extracted from it. Instead, this opens the customer's own
default mail client with the request pre-filled (mailto: link) -- the
customer's own email account sends it, no credential is ever shipped.

Launch pricing and contact details confirmed by Gopinath 2026-08-19.
Note: he also mentioned a second, Gmail-linked UPI ID ("or gmail
9952000447") without the exact @handle suffix -- not encoded here since
guessing a payment identifier wrong would misdirect real customer
payments. Add it as a second QR/option once he confirms the exact VPA.
"""

from __future__ import annotations

import io
import urllib.parse
import webbrowser

import qrcode

UPI_ID = "9952000447@yescred"
PRICE_RS = 1999.0                    # launch offer, per month
PAYMENT_NAME = "TradeSuite"          # shown in the customer's UPI app
ADMIN_CONTACT_EMAIL = "reachgops@gmail.com"


def upi_qr_png_bytes(amount_rs: float = PRICE_RS) -> bytes:
    upi_url = (
        f"upi://pay?pa={UPI_ID}&pn={urllib.parse.quote(PAYMENT_NAME)}"
        f"&am={amount_rs:.2f}&cu=INR"
    )
    img = qrcode.make(upi_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def open_payment_request_email(name: str, email: str, phone: str, machine_id: str, txn_id: str) -> None:
    """Opens the customer's default mail client with the renewal request
    pre-filled, addressed to the admin. The customer clicks send from
    their own email account -- nothing is sent automatically, and no
    credential of any kind is embedded in this app."""
    subject = f"TradeSuite renewal - {machine_id}"
    body = (
        f"Name: {name}\n"
        f"Email: {email}\n"
        f"Phone: {phone}\n"
        f"Machine ID: {machine_id}\n"
        f"UPI Transaction ID: {txn_id}\n"
        f"\n(Sent from the TradeSuite app's renewal screen.)"
    )
    mailto = (
        f"mailto:{ADMIN_CONTACT_EMAIL}"
        f"?subject={urllib.parse.quote(subject)}&body={urllib.parse.quote(body)}"
    )
    webbrowser.open(mailto)
