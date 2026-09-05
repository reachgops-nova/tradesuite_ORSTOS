"""
==========================================================
TradeSuite
gui/table_utils.py
==========================================================

Shared clipboard-copy support for QTableWidget-based panels. Qt's
QTableWidget supports cell *selection* out of the box but does nothing
with Ctrl+C by default -- copying to the clipboard as paste-into-Excel-
compatible text needs to be wired up explicitly. Shared here since both
trade_log_panel.py and history_panel.py need the same behavior.
"""

from __future__ import annotations

from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QPushButton, QTableWidget

_TSV = "\t"


def _selection_to_tsv(table: QTableWidget) -> str:
    ranges = table.selectedRanges()
    if not ranges:
        return ""
    lines = []
    for r in ranges:
        for row in range(r.topRow(), r.bottomRow() + 1):
            cells = []
            for col in range(r.leftColumn(), r.rightColumn() + 1):
                item = table.item(row, col)
                cells.append(item.text() if item else "")
            lines.append(_TSV.join(cells))
    return "\n".join(lines)


def _table_to_tsv(table: QTableWidget) -> str:
    headers = [table.horizontalHeaderItem(c).text() if table.horizontalHeaderItem(c) else ""
               for c in range(table.columnCount())]
    lines = [_TSV.join(headers)]
    for row in range(table.rowCount()):
        cells = []
        for col in range(table.columnCount()):
            item = table.item(row, col)
            cells.append(item.text() if item else "")
        lines.append(_TSV.join(cells))
    return "\n".join(lines)


def enable_table_copy(table: QTableWidget) -> QPushButton:
    """Wires up Ctrl+C on the table (copies whatever's selected) and
    returns a ready-to-place 'Copy All' button (copies the entire table,
    header included, regardless of selection -- the "can't copy the
    whole thing" gap)."""
    table.setSelectionMode(QTableWidget.ExtendedSelection)

    shortcut = QShortcut(QKeySequence.Copy, table)
    shortcut.activated.connect(lambda: QApplication.clipboard().setText(_selection_to_tsv(table)))

    copy_all_btn = QPushButton("Copy All")
    copy_all_btn.clicked.connect(lambda: QApplication.clipboard().setText(_table_to_tsv(table)))
    return copy_all_btn
