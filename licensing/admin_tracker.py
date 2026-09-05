"""
==========================================================
TradeSuite
licensing/admin_tracker.py
==========================================================

Local, no-backend customer ledger -- a CSV file next to this script,
same approach as SureFramePro's admin_tracker.py/SFP_Customers.csv.
Runs on Gopinath's own machine, not shipped to customers.

Usage as a library (called by qr_payment.py / admin_keygen.py) or
directly as a CLI:
    python admin_tracker.py --list
    python admin_tracker.py --machine ABCD1234
    python admin_tracker.py --email someone@example.com
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
from pathlib import Path

CSV_PATH = Path(__file__).resolve().parent / "TS_Customers.csv"
FIELDS = [
    "MachineID", "Name", "Email", "Phone", "RegisterDate",
    "KeysGenerated", "TotalDaysGranted", "TxnIDs", "PendingTxnIDs",
    "LastActivity", "Status", "Notes",
]


def _read_all() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    with open(CSV_PATH, newline="") as f:
        return list(csv.DictReader(f))


def _write_all(rows: list[dict]) -> None:
    with open(CSV_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def lookup_customer(machine_id: str = None, email: str = None) -> dict | None:
    for row in _read_all():
        if machine_id and row.get("MachineID") == machine_id:
            return row
        if email and row.get("Email", "").lower() == email.lower():
            return row
    return None


def record_registration(machine_id: str, name: str, email: str, phone: str) -> None:
    rows = _read_all()
    existing = next((r for r in rows if r["MachineID"] == machine_id), None)
    if existing:
        existing.update({"Name": name, "Email": email, "Phone": phone, "LastActivity": _now()})
    else:
        rows.append({
            "MachineID": machine_id, "Name": name, "Email": email, "Phone": phone,
            "RegisterDate": _now(), "KeysGenerated": "0", "TotalDaysGranted": "0",
            "TxnIDs": "", "PendingTxnIDs": "", "LastActivity": _now(),
            "Status": "Registered - Awaiting Activation", "Notes": "",
        })
    _write_all(rows)


def record_payment_request(machine_id: str, txn_id: str) -> None:
    rows = _read_all()
    row = next((r for r in rows if r["MachineID"] == machine_id), None)
    if row is None:
        return
    pending = [t for t in row.get("PendingTxnIDs", "").split(";") if t]
    pending.append(txn_id)
    row["PendingTxnIDs"] = ";".join(pending)
    row["Status"] = "Pending Payment Verification"
    row["LastActivity"] = _now()
    _write_all(rows)


def record_key_sent(machine_id: str, grant_days: int, txn_id: str = None) -> None:
    rows = _read_all()
    row = next((r for r in rows if r["MachineID"] == machine_id), None)
    if row is None:
        return
    row["KeysGenerated"] = str(int(row.get("KeysGenerated") or 0) + 1)
    row["TotalDaysGranted"] = str(int(row.get("TotalDaysGranted") or 0) + grant_days)
    if txn_id:
        confirmed = [t for t in row.get("TxnIDs", "").split(";") if t]
        confirmed.append(txn_id)
        row["TxnIDs"] = ";".join(confirmed)
        pending = [t for t in row.get("PendingTxnIDs", "").split(";") if t and t != txn_id]
        row["PendingTxnIDs"] = ";".join(pending)
    row["Status"] = "Active"
    row["LastActivity"] = _now()
    _write_all(rows)


def print_all_customers() -> None:
    rows = _read_all()
    if not rows:
        print("No customers recorded yet.")
        return
    print(f"{'MachineID':<10} {'Name':<20} {'Email':<28} {'Status':<28} {'Days':<6}")
    for r in rows:
        print(f"{r['MachineID']:<10} {r['Name'][:20]:<20} {r['Email'][:28]:<28} "
              f"{r['Status'][:28]:<28} {r['TotalDaysGranted']:<6}")


def main() -> None:
    ap = argparse.ArgumentParser(description="TradeSuite customer ledger")
    ap.add_argument("--machine", help="Look up by machine ID")
    ap.add_argument("--email", help="Look up by email")
    ap.add_argument("--list", action="store_true", help="List all customers")
    args = ap.parse_args()

    if args.machine or args.email:
        row = lookup_customer(machine_id=args.machine, email=args.email)
        print(row or "Not found.")
    else:
        print_all_customers()


if __name__ == "__main__":
    main()
