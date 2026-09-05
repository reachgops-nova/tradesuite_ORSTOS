"""
==========================================================
TradeSuite
gui/license_screen.py
==========================================================

Shows one of three views depending on licensing/license_core's status:
  - Registration form (first run: name/email/phone -> stored locally,
    displays this machine's ID to give the admin).
  - Awaiting-activation / renewal (registered but unlicensed or expired):
    QR code to pay + a box to paste the key the admin sends back.
  - Licensed: a compact "N days remaining" strip with a Renew button.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QFormLayout, QMessageBox, QStackedWidget, QFrame,
)

from licensing import license_core, qr_payment, renewal_report


class RegistrationView(QWidget):
    registered = Signal()

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>Welcome to TradeSuite</h2>"))
        layout.addWidget(QLabel("Enter your details to request access. Your admin will "
                                 "issue you a license key once you're approved."))

        form = QFormLayout()
        self.name_input = QLineEdit()
        self.email_input = QLineEdit()
        self.phone_input = QLineEdit()
        form.addRow("Name", self.name_input)
        form.addRow("Email", self.email_input)
        form.addRow("Phone", self.phone_input)
        layout.addLayout(form)

        self.machine_id_label = QLabel(f"Your machine ID: <b>{license_core.get_machine_id()}</b>")
        layout.addWidget(self.machine_id_label)
        layout.addWidget(QLabel("Send this ID to your admin along with the details above."))

        # Disclosure, shown before they register rather than buried in a
        # document nobody reads. This is the customer's own financial data
        # leaving their machine -- sending it without saying so would be the
        # wrong call regardless of how easy it is to do quietly.
        privacy = QLabel(
            "<small>At each renewal date, this app emails your subscription-period trade summary "
            "(trades, entry/exit prices and P&amp;L) to your TradeSuite administrator, so your results "
            "can be reviewed with you at renewal. Nothing is sent to anyone else, and your broker "
            "credentials are never transmitted -- they stay on this machine.</small>"
        )
        privacy.setWordWrap(True)
        layout.addWidget(privacy)

        submit = QPushButton("Register")
        submit.clicked.connect(self._submit)
        layout.addWidget(submit)
        layout.addStretch()

    def _submit(self):
        name, email, phone = self.name_input.text().strip(), self.email_input.text().strip(), self.phone_input.text().strip()
        if not name or not email:
            QMessageBox.warning(self, "Missing details", "Name and email are required.")
            return
        license_core.register(name, email, phone)
        self.registered.emit()


class RenewalView(QWidget):
    renewed = Signal()

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        self.title_label = QLabel()
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)

        qr_row = QHBoxLayout()
        self.qr_label = QLabel()
        self.qr_label.setAlignment(Qt.AlignCenter)
        qr_row.addWidget(self.qr_label)
        layout.addLayout(qr_row)

        layout.addWidget(QLabel(f"Scan to pay via UPI (₹{qr_payment.PRICE_RS:.0f}/month launch offer). "
                                 f"Then enter the UPI transaction ID below and send the request."))

        form = QFormLayout()
        self.txn_input = QLineEdit()
        form.addRow("UPI Transaction ID", self.txn_input)
        layout.addLayout(form)

        send_btn = QPushButton("Email renewal request to admin")
        send_btn.clicked.connect(self._send_request)
        layout.addWidget(send_btn)

        layout.addWidget(QLabel("Once your admin verifies payment, they'll send you a key. Paste it here:"))
        key_row = QHBoxLayout()
        self.key_input = QLineEdit()
        self.key_input.setPlaceholderText("TS-XXXXXXXX-30-YYYYMMDD-XXXXXXXXXXXXXXXX")
        redeem_btn = QPushButton("Activate")
        redeem_btn.clicked.connect(self._redeem)
        key_row.addWidget(self.key_input)
        key_row.addWidget(redeem_btn)
        layout.addLayout(key_row)
        layout.addStretch()

        self.refresh()

    def refresh(self):
        status = license_core.check_status()
        self.title_label.setText(f"<h2>Renew your license</h2>{status.reason}")
        png = qr_payment.upi_qr_png_bytes()
        pix = QPixmap()
        pix.loadFromData(png)
        self.qr_label.setPixmap(pix.scaled(220, 220, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _send_request(self):
        txn = self.txn_input.text().strip()
        if not txn:
            QMessageBox.warning(self, "Missing transaction ID", "Enter the UPI transaction ID first.")
            return
        status = license_core.check_status()
        data = license_core._load_raw()
        qr_payment.open_payment_request_email(
            data.get("name") or "", data.get("email") or "", data.get("phone") or "", status.machine_id, txn,
        )
        QMessageBox.information(self, "Email drafted", "Your email app should now have a message ready to send. "
                                                          "Send it, and wait for your admin's key.")

    def _redeem(self):
        key = self.key_input.text().strip()
        if not key:
            return
        # Capture the outgoing period's end BEFORE redeeming extends it --
        # a customer who renews a day early would otherwise never have that
        # boundary pass, and its report would never be sent.
        previous_expiry = license_core._parse_expiry(license_core._load_raw().get("expiry_date"))
        ok, msg = license_core.redeem_key(key)
        (QMessageBox.information if ok else QMessageBox.warning)(self, "Activation", msg)
        if ok:
            if previous_expiry:
                renewal_report.queue_period_end(previous_expiry)
            self.renewed.emit()


class LicensedStrip(QFrame):
    """Compact status strip shown above the main app once licensed."""
    renew_clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setFrameShape(QFrame.StyledPanel)
        layout = QHBoxLayout(self)
        self.label = QLabel()
        layout.addWidget(self.label)
        layout.addStretch()
        renew_btn = QPushButton("Renew")
        renew_btn.clicked.connect(self.renew_clicked.emit)
        layout.addWidget(renew_btn)
        self.refresh()

    def refresh(self):
        status = license_core.check_status()
        if license_core.TESTING_MODE:
            self.label.setText("⚠ Testing mode — license check disabled")
        else:
            urgency = "" if status.days_remaining > 7 else " -- renew soon"
            self.label.setText(f"License: {status.days_remaining} day(s) remaining{urgency}")


class LicenseGate(QStackedWidget):
    """Top-level widget MainWindow shows instead of the app until licensed.
    Call is_licensed() before letting anything trade."""
    licensed_changed = Signal(bool)

    REGISTRATION, RENEWAL = range(2)

    def __init__(self):
        super().__init__()
        self.registration_view = RegistrationView()
        self.renewal_view = RenewalView()
        self.addWidget(self.registration_view)
        self.addWidget(self.renewal_view)

        self.registration_view.registered.connect(self.refresh)
        self.renewal_view.renewed.connect(self.refresh)
        self.refresh()

    def is_licensed(self) -> bool:
        return license_core.check_status().licensed

    def refresh(self):
        status = license_core.check_status()
        if not status.registered:
            self.setCurrentIndex(self.REGISTRATION)
        else:
            self.renewal_view.refresh()
            self.setCurrentIndex(self.RENEWAL)
        self.licensed_changed.emit(status.licensed)
