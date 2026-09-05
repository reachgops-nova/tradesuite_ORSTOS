"""
==========================================================
TradeSuite
licensing/admin_keygen.py
==========================================================

Admin console -- run this by hand (python admin_keygen.py) after
verifying a customer's UPI payment (or when granting a free trial to
someone in the closed trusted group). Adapted from SureFramePro's
admin_keygen.py: same interactive flow and signed-key mechanism, changed
to grant a number of days instead of a credit count.

Not shipped to customers -- lives only on Gopinath's own machine, next to
TS_Customers.csv.
"""

from __future__ import annotations

from datetime import date, timedelta

from licensing import mail_relay, server_config
from licensing.admin_tracker import lookup_customer, record_key_sent, print_all_customers
from licensing.license_core import generate_key

REDEEM_WINDOW_DAYS = 30  # how long a generated key stays valid before it must be redeemed


def _prompt(label: str) -> str:
    return input(f"{label}: ").strip()


def _key_email_body(key: str, grant_days: int, redeem_by: date) -> str:
    return "\n".join([
        "Your TradeSuite licence key is ready.",
        "",
        f"    {key}",
        "",
        f"This grants {grant_days} days of access and must be activated by {redeem_by.isoformat()}.",
        "",
        "To activate: open TradeSuite, paste the key into the box on the licence screen,",
        "and click Activate. Your settings, broker connection and trade history are not",
        "affected -- activation only extends your expiry date.",
        "",
        "The key is tied to the machine ID you registered with, so it will only activate",
        "on that computer.",
    ])


def _offer_to_email(customer: dict | None, key: str, grant_days: int, redeem_by: date) -> None:
    """Emails the key the moment payment is confirmed -- the automated
    delivery step. Falls back to "copy it manually" whenever the relay
    isn't configured or the send fails, so issuing a key never depends on
    mail working."""
    if not server_config.is_configured():
        print("\n(No mail relay configured in this build -- send the key to the customer manually.)")
        return

    default_email = (customer or {}).get("Email", "")
    to = _prompt(f"Email the key to [{default_email or 'address'}] (blank to skip)") or default_email
    if not to:
        print("Skipped emailing -- send the key manually.")
        return

    ok, msg = mail_relay.send(
        to, "Your TradeSuite licence key", _key_email_body(key, grant_days, redeem_by))
    print(f"{'Emailed' if ok else 'NOT emailed'}: {msg}")
    if not ok:
        print("Send the key manually -- it is valid regardless of the email failing.")


def issue_key() -> None:
    print("=" * 60)
    print("TradeSuite -- issue a license key")
    print("=" * 60)
    machine_id = _prompt("Customer machine ID (8+ chars, from their app's registration screen)")
    customer = lookup_customer(machine_id=machine_id)
    if customer:
        print(f"Found: {customer['Name']} <{customer['Email']}> -- status: {customer['Status']}")
    else:
        print("No matching registration found for this machine ID -- issuing anyway, but double-check it's correct.")

    print("\nGrant type:")
    print("  1) Trial (free -- closed trusted group)")
    print("  2) After payment (paste the UPI transaction ID)")
    choice = _prompt("Choice [1/2]")

    txn_id = None
    if choice == "2":
        txn_id = _prompt("UPI transaction ID")

    days_str = _prompt("Days to grant (e.g. 30 for one month, 90 for three)")
    try:
        grant_days = int(days_str)
    except ValueError:
        print("Not a number, aborting.")
        return

    redeem_by = date.today() + timedelta(days=REDEEM_WINDOW_DAYS)
    key = generate_key(machine_id, grant_days, redeem_by)

    print("\n" + "=" * 60)
    print(f"KEY: {key}")
    print(f"Grants {grant_days} days, must be redeemed by {redeem_by.isoformat()}")
    print("=" * 60)

    _offer_to_email(customer, key, grant_days, redeem_by)

    if machine_id:
        record_key_sent(machine_id, grant_days, txn_id=txn_id)
        print("Recorded in TS_Customers.csv.")


def check_relay() -> None:
    print(server_config.describe())
    if server_config.is_configured():
        ok, msg = mail_relay.test_relay()
        print(("OK: " if ok else "FAILED: ") + msg)


def main() -> None:
    while True:
        print("\n1) Issue a key   2) List customers   3) Check mail relay   4) Quit")
        choice = _prompt("Choice")
        if choice == "1":
            issue_key()
        elif choice == "2":
            print_all_customers()
        elif choice == "3":
            check_relay()
        elif choice == "4":
            break


if __name__ == "__main__":
    main()
