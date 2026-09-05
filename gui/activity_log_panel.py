"""
==========================================================
TradeSuite
gui/activity_log_panel.py
==========================================================

Live activity log -- the running commentary from both strategy engines:
what they're watching right now, how close each condition is to firing,
and the moment one is actually met.

The engines have always produced this narrative (every _log() call in
orb_strategy.py / tamil_strategy.py), and process_manager.py has always
handed each running strategy a log queue -- but until now nothing drained
those queues, so the whole commentary was thrown away. This panel is the
consumer: it drains every running strategy's queue on a timer, and on
launch also loads back today's persisted log files so restarting the app
doesn't lose the morning's activity.

Trade Log answers "what positions do I have"; this answers "what is the
strategy doing right now, and why hasn't it acted yet".
"""

from __future__ import annotations

from datetime import datetime

import pytz
from PySide6.QtCore import QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QCheckBox, QApplication,
)

from engine.config_store import ConfigStore
from engine.process_manager import ProcessManager, log_path_for
from gui.labels import ORB_SHORT, TAMIL_SHORT

IST = pytz.timezone("Asia/Kolkata")
DRAIN_MS = 1_000
MAX_LINES = 5_000  # plenty for a full trading day; keeps memory bounded on a long-running install

TAGS = {"orb": ORB_SHORT, "tamil": TAMIL_SHORT}


class ActivityLogPanel(QWidget):
    def __init__(self, config: ConfigStore, process_manager: ProcessManager):
        super().__init__()
        self.config = config
        self.pm = process_manager

        layout = QVBoxLayout(self)

        header_row = QHBoxLayout()
        header_row.addWidget(QLabel("<b>Live activity</b> -- what each strategy is watching, and what fires when"))
        header_row.addStretch()
        self.autoscroll = QCheckBox("Follow")
        self.autoscroll.setChecked(True)
        header_row.addWidget(self.autoscroll)
        copy_btn = QPushButton("Copy All")
        copy_btn.clicked.connect(self._copy_all)
        header_row.addWidget(copy_btn)
        clear_btn = QPushButton("Clear view")
        clear_btn.clicked.connect(self._clear_view)
        header_row.addWidget(clear_btn)
        layout.addLayout(header_row)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(MAX_LINES)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        layout.addWidget(self.view)

        self.footer = QLabel("")
        layout.addWidget(self.footer)

        self._load_todays_files()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.drain)
        self.timer.start(DRAIN_MS)

    # -- input --------------------------------------------------------

    def _load_todays_files(self):
        """Replays today's persisted logs so the panel isn't blank after an
        app restart mid-session. Merged by timestamp: every engine line is
        already prefixed '[YYYY-MM-DD HH:MM:SS IST] ', so a plain sort puts
        the two strategies' lines into true chronological order."""
        today = datetime.now(IST).date()
        lines = []
        for strategy, tag in TAGS.items():
            path = log_path_for(self.config, strategy, today)
            if not path.exists():
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    lines.extend((line.rstrip("\n"), tag) for line in f if line.strip())
            except OSError:
                continue
        for line, tag in sorted(lines):
            self._append(tag, line)
        if not lines:
            self._append(None, "No activity yet today. Start a strategy on the Strategies tab and its "
                                "live commentary will appear here.")

    def drain(self):
        appended = False
        for strategy, tag in TAGS.items():
            handle = self.pm.handle(strategy)
            if handle is None:
                continue
            for line in handle.drain_logs():
                self._append(tag, line)
                appended = True
        if appended and self.autoscroll.isChecked():
            self.view.moveCursor(QTextCursor.End)
        self._refresh_footer()

    def _append(self, tag: str | None, line: str):
        self.view.appendPlainText(f"{tag:<7}{line}" if tag else line)

    def _refresh_footer(self):
        running = [tag for strategy, tag in TAGS.items() if self.pm.is_running(strategy)]
        self.footer.setText(
            f"Running: {', '.join(running)}" if running
            else "Nothing running -- showing today's saved activity only."
        )

    # -- actions ------------------------------------------------------

    def _copy_all(self):
        QApplication.clipboard().setText(self.view.toPlainText())

    def _clear_view(self):
        """Clears the on-screen view only. The persisted log files are left
        alone on purpose -- they're the after-the-fact record of what the
        strategy actually saw, and are not the customer's to lose by
        clicking a button meant to tidy the display."""
        self.view.clear()
