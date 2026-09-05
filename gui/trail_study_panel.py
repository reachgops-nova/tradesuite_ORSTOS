"""
==========================================================
TradeSuite
gui/trail_study_panel.py
==========================================================

Plain-language report on the group-level trailing-stop study that
engine/trail_tracker.py records live each day.

Deliberately shows ONE answer per day by default -- "would a trailing
stop have beaten what actually happened, and by how much" -- rather than
a grid of every variant. The first version of this tab put eight variant
columns on screen at once; the columns were too narrow to read their own
headers and every cell looked alike, which buried the one number that
matters. The full grid is still available behind a checkbox for when the
question is "which variant", not "is this worth pursuing".

Nothing here trades. It reports on observation-only data.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem, QComboBox, QCheckBox,
)

from engine.config_store import ConfigStore
from engine.trail_tracker import score_curve
from gui.labels import ORB_SHORT, TAMIL_SHORT
from gui.table_utils import enable_table_copy

POLL_MS = 60_000

GAIN = QColor(220, 245, 220)     # trailing did better
LOSS = QColor(255, 224, 224)     # trailing did worse
NEUTRAL = QColor(245, 245, 245)  # identical -- the stop never fired

# Below this many ARMED days any "winner" is noise. Stated in the UI
# rather than left for the reader to infer -- the failure mode this
# guards against is acting on one good-looking day.
MIN_ARMED_DAYS = 10

SIMPLE_COLUMNS = ["Date", "Actual", "Best moment", "Best method that day",
                  "Would have banked", "Difference", ""]

# The full sweep scored against every stored curve. This is re-run at
# DISPLAY time rather than baked in when the day was recorded, so adding a
# trail distance here immediately applies to every day already collected --
# which is the whole reason the raw curve is stored rather than just the
# variant outcomes.
SWEEP_TRAILS = [250, 500, 750, 1000, 1250, 1500, 2000, 2500]
SWEEP_ARMS = [500, 1000, 1500, 2000]
SWEEP_LADDERS = [(500, 500), (500, 1000), (1000, 500), (1000, 1000)]


# Target levels used for the hit-ratio view. The question these answer is
# "where could a fixed daily target realistically sit for this group" --
# a target the group only reaches on 1 day in 10 is not a target, it is a
# lottery ticket.
HIT_TARGETS = [500, 1000, 1500, 2000, 2500, 3000, 4000]

BY_SETTING_COLUMNS = ["Trailing SL", "Arm at", "Days fired", "Total banked",
                      "vs actual", "Avg/day", "Best day", "Worst day"]

# Gopinath's own suggested settings, 2026-08-31: start the stop at
# breakeven once the group is up 1000, and try the same 250 trail distance
# at three arm levels so the effect of arming EARLY vs LATE is visible on
# its own, without also varying the trail width. Kept as a permanent,
# always-visible table (not folded into the 32-setting sweep) because
# these are the three he specifically wants tracked day to day.
SUGGESTED_COMBOS = [(1000, 250), (1500, 250), (2000, 250)]
SUGGESTED_COLUMNS_LABEL = ["Setting"]
HIT_COLUMNS = ["Target", "Days reached", "Hit ratio", "Median time reached", "Median peak that day"]


def build_sweep() -> list[tuple]:
    out = [("trail", arm, tr, None) for arm in SWEEP_ARMS for tr in SWEEP_TRAILS]
    out += [("ladder", 1000, step, gap) for step, gap in SWEEP_LADDERS]
    return out


def sweep_label(spec: tuple) -> str:
    kind, arm, a, b = spec
    if kind == "ladder":
        return f"ladder {a:.0f}/{b:.0f} (arm {arm:.0f})"
    return f"trail {a:.0f} (arm {arm:.0f})"


def _variant_label(key: str) -> str:
    """act1000_trail500 -> 'trail 500'; ladder_act1000_step500_gap500 -> 'ladder 500/500'.
    The arm level is dropped from the short form on purpose -- it is the
    trail distance that distinguishes the variants in practice."""
    try:
        if key.startswith("ladder_"):
            _, act, step, gap = key.split("_")
            return f"ladder {step[4:]}/{gap[3:]}"
        act, trail = key.split("_")
        return f"trail {trail[5:]}"
    except Exception:
        return key


def _long_label(key: str) -> str:
    try:
        if key.startswith("ladder_"):
            _, act, step, gap = key.split("_")
            return f"ladder {step[4:]}/{gap[3:]} (arm {act[3:]})"
        act, trail = key.split("_")
        return f"trail {trail[5:]} (arm {act[3:]})"
    except Exception:
        return key


def _bar(value: float, scale: float, width: int = 18) -> str:
    """A crude but instantly readable magnitude bar. Unicode blocks keep
    it inside a normal table cell -- no custom painting, no chart library,
    and it still copies to the clipboard as text."""
    if scale <= 0 or value <= 0:
        return ""
    filled = max(1, min(width, round(value / scale * width)))
    return "█" * filled


class TrailStudyPanel(QWidget):
    def __init__(self, config: ConfigStore):
        super().__init__()
        self.config = config

        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Trailing-stop study</b> &mdash; observation only, no orders are placed"))
        header.addSpacing(12)
        header.addWidget(QLabel("Strategy:"))
        self.group_combo = QComboBox()
        self.group_combo.addItems([ORB_SHORT, TAMIL_SHORT])
        self.group_combo.setMinimumWidth(120)   # the old one clipped "ORB" to "ORE"
        self.group_combo.currentIndexChanged.connect(self.refresh)
        header.addWidget(self.group_combo)
        header.addSpacing(12)
        header.addWidget(QLabel("View:"))
        self.view_combo = QComboBox()
        self.view_combo.addItems(["Day by day", "By trailing SL", "Per-day by SL", "Peak hit-ratio", "Every variant"])
        self.view_combo.setMinimumWidth(150)
        self.view_combo.currentIndexChanged.connect(self.refresh)
        header.addWidget(self.view_combo)
        header.addStretch()
        layout.addLayout(header)

        # Gopinath, 2026-09-01: not every day trades all 3 legs of a group
        # (confirmed in the real data: ORB ~10% of days, TOS-30 ~38%). A
        # partial day has a genuinely smaller combined-P&L ceiling than a
        # full one, so mixing the two into one ranking can make a trailing
        # setting look better or worse than it really is -- not because the
        # data is wrong, just because it isn't like-for-like. Default ON:
        # the comparison should be fair unless deliberately widened.
        filter_row = QHBoxLayout()
        self.full_group_check = QCheckBox("Full group only (all legs traded) -- recommended for comparing settings")
        self.full_group_check.setChecked(True)
        self.full_group_check.stateChanged.connect(self.refresh)
        filter_row.addWidget(self.full_group_check)
        filter_row.addStretch()
        layout.addLayout(filter_row)

        layout.addWidget(QLabel(
            "<b>Suggested settings</b> &mdash; arm at 1000 / 1500 / 2000, all trailing by 250, "
            "compared day by day against what actually happened:"))
        suggest_row = QHBoxLayout()
        suggest_row.addStretch()
        self.suggested_table = QTableWidget(0, 0)
        self.suggested_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.suggested_table.setMaximumHeight(140)
        suggest_row.addWidget(enable_table_copy(self.suggested_table))
        layout.addLayout(suggest_row)
        layout.addWidget(self.suggested_table)

        self.headline = QLabel("")
        self.headline.setWordWrap(True)
        f = QFont(); f.setPointSize(11)
        self.headline.setFont(f)
        layout.addWidget(self.headline)

        self.verdict = QLabel("")
        self.verdict.setWordWrap(True)
        layout.addWidget(self.verdict)

        copy_row = QHBoxLayout()
        copy_row.addStretch()
        self.table = QTableWidget(0, 0)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        copy_row.addWidget(enable_table_copy(self.table))
        layout.addLayout(copy_row)
        layout.addWidget(self.table)

        self.footnote = QLabel(
            "<small>&ldquo;Best moment&rdquo; is the most the whole group was up at any single instant "
            "&mdash; not the sum of each trade&rsquo;s own best, which happen at different times and could "
            "never all be captured at once. &ldquo;(ran)&rdquo; means the stop never fired that day, so the "
            "real result stands.</small>")
        self.footnote.setWordWrap(True)
        layout.addWidget(self.footnote)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(POLL_MS)
        self.refresh()

    # -- data ---------------------------------------------------------

    def _load_days(self, group: str) -> list[dict]:
        out = []
        d: Path = self.config.trail_study_dir
        if not d.exists():
            return out
        for f in sorted(d.glob("*.json")):
            try:
                data = json.load(open(f))
            except (json.JSONDecodeError, OSError):
                continue
            rec = data.get(group)
            if rec:
                out.append(rec)
        return out

    @staticmethod
    def _banked(variant: dict, actual: float) -> tuple[float, bool]:
        """(amount, did the stop actually fire). A variant that never armed,
        or armed but never stopped out, simply ends where the real trades
        ended -- counting it as anything else would invent a result."""
        exit_pnl = variant.get("exit_pnl")
        return (exit_pnl, True) if exit_pnl is not None else (actual, False)

    @staticmethod
    def _score_day(rec: dict) -> dict:
        """Every sweep setting scored against this day's stored curve.
        Falls back to the variants recorded on the day if no curve was
        saved (older files), so nothing silently disappears."""
        actual = rec.get("actual_net") or 0.0
        curve = [v for _, v in (rec.get("curve") or [])]
        out = {}
        if curve:
            for spec in build_sweep():
                amount, fired = score_curve(curve, spec[0], spec[1], spec[2], spec[3])
                out[sweep_label(spec)] = (amount if fired else actual, fired)
        else:
            for k, v in (rec.get("variants") or {}).items():
                exit_pnl = v.get("exit_pnl")
                out[_long_label(k)] = ((exit_pnl, True) if exit_pnl is not None else (actual, False))
        return out

    # -- display ------------------------------------------------------

    @staticmethod
    def _day_label(rec: dict) -> str:
        """Date plus leg count, e.g. '2026-08-27 (2/3)' -- so even with the
        filter off, a partial day is never silently indistinguishable from
        a full one in the raw tables."""
        date = rec.get("day", "")
        traded, total = rec.get("legs_traded"), rec.get("legs_total")
        if traded is not None and total is not None:
            return f"{date} ({traded}/{total})" if traded != total else date
        return date

    @staticmethod
    def _is_full_group(rec: dict) -> bool:
        """True only when we can CONFIRM every leg traded. Missing leg data
        (older records, or a save that failed before the field was added)
        is treated as NOT confirmed full -- excluding an unknown day from
        the fair comparison is the safe direction; silently assuming it
        was full is the direction that could reintroduce the exact bias
        this filter exists to remove."""
        traded, total = rec.get("legs_traded"), rec.get("legs_total")
        return traded is not None and total is not None and traded == total

    def refresh(self):
        group = self.group_combo.currentText()
        all_days = self._load_days(group)
        excluded = 0

        if self.full_group_check.isChecked():
            days = [d for d in all_days if self._is_full_group(d)]
            excluded = len(all_days) - len(days)
        else:
            days = all_days

        if not all_days:
            self.headline.setText(f"<b>No days recorded yet for {group}.</b>")
            self.verdict.setText(
                "The study starts collecting the first time this strategy runs with the current build. "
                "One row will appear per trading day.")
            self.table.setRowCount(0)
            self.table.setColumnCount(0)
            self.suggested_table.setRowCount(0)
            self.suggested_table.setColumnCount(0)
            return

        if not days:
            self.headline.setText(
                f"<b>{len(all_days)} day(s) recorded for {group}, but none had every leg trade.</b>")
            self.verdict.setText(
                "<span style='color:#b35400'>Untick &ldquo;Full group only&rdquo; above to see them -- "
                "partial-leg days aren't discarded, just kept out of the setting comparison by default.</span>")
            self.table.setRowCount(0)
            self.table.setColumnCount(0)
            self.suggested_table.setRowCount(0)
            self.suggested_table.setColumnCount(0)
            return

        self._render_suggested(days)

        view = self.view_combo.currentText()
        if view == "By trailing SL":
            self._render_by_setting(days)
        elif view == "Per-day by SL":
            self._render_per_day_by_setting(days)
        elif view == "Peak hit-ratio":
            self._render_hit_ratio(days)
        elif view == "Every variant":
            self._render_detail(days)
        else:
            self._render_simple(days)
        self._render_verdict(days, excluded)

    def _render_suggested(self, days: list[dict]):
        """Always-visible, regardless of which view is selected -- these
        are the three specific settings Gopinath asked to track, not just
        one option among 32. Same per-day layout as "Per-day by SL" (one
        column per day) so a setting that only wins because of a single
        good day is visible, not hidden inside a summed total."""
        dates = [self._day_label(rec) for rec in days]
        columns = SUGGESTED_COLUMNS_LABEL + dates + ["Total"]
        self.suggested_table.setColumnCount(len(columns))
        self.suggested_table.setHorizontalHeaderLabels(columns)

        actual_per_day = [rec.get("actual_net") or 0.0 for rec in days]
        rows = [("Actual (no trailing stop)", actual_per_day, sum(actual_per_day))]

        for arm, trail in SUGGESTED_COMBOS:
            per_day = []
            for rec, actual in zip(days, actual_per_day):
                curve = [v for _, v in (rec.get("curve") or [])]
                if curve:
                    amt, fired = score_curve(curve, "trail", arm, trail)
                    per_day.append(amt if fired else actual)
                else:
                    per_day.append(actual)
            rows.append((f"Arm {arm} / trail {trail}", per_day, sum(per_day)))

        self.suggested_table.setRowCount(len(rows))
        for i, (label, per_day, total) in enumerate(rows):
            item = QTableWidgetItem(label)
            if i == 0:
                f = item.font(); f.setBold(True); item.setFont(f)
            self.suggested_table.setItem(i, 0, item)
            for j, amt in enumerate(per_day):
                cell = QTableWidgetItem(f"{amt:+,.0f}")
                if i > 0:
                    diff = amt - actual_per_day[j]
                    cell.setBackground(GAIN if diff > 0.5 else (LOSS if diff < -0.5 else NEUTRAL))
                self.suggested_table.setItem(i, 1 + j, cell)
            total_item = QTableWidgetItem(f"{total:+,.0f}")
            total_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if i > 0:
                diff_total = total - rows[0][2]
                total_item.setBackground(GAIN if diff_total > 0.5 else (LOSS if diff_total < -0.5 else NEUTRAL))
            self.suggested_table.setItem(i, 1 + len(per_day), total_item)
        self.suggested_table.resizeColumnsToContents()

    def _render_simple(self, days: list[dict]):
        rows = []
        for rec in days:
            actual = rec.get("actual_net") or 0.0
            scored = self._score_day(rec)
            if scored:
                best_key = max(scored, key=lambda k: scored[k][0])
                best_amt, fired = scored[best_key]
            else:
                best_key, best_amt, fired = "--", actual, False
            rows.append({
                "date": self._day_label(rec),
                "actual": actual,
                "peak": rec.get("peak_combined"),
                "best_key": best_key,
                "best_amt": best_amt,
                "fired": fired,
                "diff": best_amt - actual,
            })

        scale = max((r["diff"] for r in rows), default=0.0)
        self.table.setColumnCount(len(SIMPLE_COLUMNS))
        self.table.setHorizontalHeaderLabels(SIMPLE_COLUMNS)
        self.table.setRowCount(len(rows))

        for i, r in enumerate(rows):
            diff = r["diff"]
            cells = [
                r["date"],
                f"{r['actual']:+,.0f}",
                f"{r['peak']:+,.0f}" if r["peak"] is not None else "--",
                r["best_key"] if r["fired"] else "none fired",
                f"{r['best_amt']:+,.0f}" + ("" if r["fired"] else " (ran)"),
                f"{diff:+,.0f}" if abs(diff) >= 0.5 else "same",
                _bar(diff, scale),
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if col in (4, 5, 6):
                    item.setBackground(GAIN if diff > 0.5 else (LOSS if diff < -0.5 else NEUTRAL))
                if col == 6:
                    # Solid black blocks read as a redaction bar; tint them.
                    item.setForeground(QColor(30, 107, 52))
                if col in (1, 2, 4, 5):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(i, col, item)
        self.table.resizeColumnsToContents()

    def _render_detail(self, days: list[dict]):
        scored_days = [self._score_day(rec) for rec in days]
        keys = sorted({k for sd in scored_days for k in sd})
        columns = ["Date", "Actual", "Best moment"] + keys
        self.table.setColumnCount(len(columns))
        self.table.setHorizontalHeaderLabels(columns)
        self.table.setRowCount(len(days))

        for i, rec in enumerate(days):
            actual = rec.get("actual_net") or 0.0
            peak = rec.get("peak_combined")
            scored = scored_days[i]
            cells = [self._day_label(rec), f"{actual:+,.0f}",
                     f"{peak:+,.0f}" if peak is not None else "--"]
            flags = []
            for k in keys:
                amt, fired = scored.get(k, (actual, False))
                cells.append(f"{amt:+,.0f}" + ("" if fired else " (ran)"))
                flags.append(amt - actual)
            for col, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if col >= 3:
                    d = flags[col - 3]
                    item.setBackground(GAIN if d > 0.5 else (LOSS if d < -0.5 else NEUTRAL))
                self.table.setItem(i, col, item)
        self.table.resizeColumnsToContents()

    def _render_by_setting(self, days: list[dict]):
        """One row per trailing setting: how it did across every recorded
        day. This is the decision table -- a single setting applied to all
        days, which is the only way it could actually be traded."""
        scored_days = [self._score_day(rec) for rec in days]
        actuals = [rec.get("actual_net") or 0.0 for rec in days]
        actual_total = sum(actuals)

        rows = []
        for spec in build_sweep():
            label = sweep_label(spec)
            amounts, fired_count = [], 0
            for scored, actual in zip(scored_days, actuals):
                amt, fired = scored.get(label, (actual, False))
                amounts.append(amt)
                fired_count += 1 if fired else 0
            if not amounts:
                continue
            kind, arm, a, b = spec
            rows.append({
                "sl": f"ladder {a:.0f}/{b:.0f}" if kind == "ladder" else f"{a:.0f}",
                "arm": f"{arm:.0f}",
                "fired": fired_count,
                "total": sum(amounts),
                "vs": sum(amounts) - actual_total,
                "avg": sum(amounts) / len(amounts),
                "best": max(amounts),
                "worst": min(amounts),
            })
        rows.sort(key=lambda r: r["total"], reverse=True)

        self.table.setColumnCount(len(BY_SETTING_COLUMNS))
        self.table.setHorizontalHeaderLabels(BY_SETTING_COLUMNS)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            cells = [r["sl"], r["arm"], f"{r['fired']}/{len(days)}", f"{r['total']:+,.0f}",
                     f"{r['vs']:+,.0f}", f"{r['avg']:+,.0f}", f"{r['best']:+,.0f}", f"{r['worst']:+,.0f}"]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if col >= 3:
                    item.setBackground(GAIN if r["vs"] > 0.5 else (LOSS if r["vs"] < -0.5 else NEUTRAL))
                if col >= 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(i, col, item)
        self.table.resizeColumnsToContents()

    def _render_per_day_by_setting(self, days: list[dict]):
        """The consolidated 'By trailing SL' view ranks settings by their
        TOTAL across every day, which can make a setting look like a clear
        winner even if it only won because of one big day and actually
        LOST on others -- exactly the risk with a high arm level like 2000,
        which only fires on a big move and can sit at a loss on a smaller
        day. This view puts every day in its own column so that is visible
        directly, one setting per row."""
        scored_days = [self._score_day(rec) for rec in days]
        dates = [self._day_label(rec) for rec in days]
        keys = sorted({k for sd in scored_days for k in sd})

        columns = ["Trailing SL", "Arm at"] + dates + ["Total", "Days negative"]
        self.table.setColumnCount(len(columns))
        self.table.setHorizontalHeaderLabels(columns)

        rows = []
        for spec in build_sweep():
            label = sweep_label(spec)
            per_day = []
            for rec, scored in zip(days, scored_days):
                actual = rec.get("actual_net") or 0.0
                amt, fired = scored.get(label, (actual, False))
                per_day.append((amt, fired))
            total = sum(a for a, _ in per_day)
            negative_days = sum(1 for a, _ in per_day if a < 0)
            kind, arm, a, b = spec
            sl_text = f"ladder {a:.0f}/{b:.0f}" if kind == "ladder" else f"{a:.0f}"
            rows.append((sl_text, f"{arm:.0f}", per_day, total, negative_days))
        rows.sort(key=lambda r: r[3], reverse=True)

        self.table.setRowCount(len(rows))
        for i, (sl_text, arm_text, per_day, total, neg) in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(sl_text))
            self.table.setItem(i, 1, QTableWidgetItem(arm_text))
            for j, (amt, fired) in enumerate(per_day):
                item = QTableWidgetItem(f"{amt:+,.0f}" + ("" if fired else " (ran)"))
                item.setBackground(LOSS if amt < 0 else (GAIN if fired else NEUTRAL))
                self.table.setItem(i, 2 + j, item)
            total_item = QTableWidgetItem(f"{total:+,.0f}")
            total_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(i, 2 + len(per_day), total_item)
            neg_item = QTableWidgetItem(f"{neg}/{len(per_day)}")
            neg_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if neg:
                neg_item.setBackground(LOSS)
            self.table.setItem(i, 3 + len(per_day), neg_item)
        self.table.resizeColumnsToContents()

    def _render_hit_ratio(self, days: list[dict]):
        """How often the group's combined P&L actually reached each level,
        and when. This is what a fixed daily target should be set from --
        a level reached on 1 day in 10 is not a target."""
        peaks, curves = [], []
        for rec in days:
            pk = rec.get("peak_combined")
            if pk is not None:
                peaks.append(pk)
            curves.append(rec.get("curve") or [])

        rows = []
        for target in HIT_TARGETS:
            reached, times, day_peaks = 0, [], []
            for pk, curve in zip(peaks + [None] * (len(curves) - len(peaks)), curves):
                hit_at = next((ts for ts, v in curve if v >= target), None)
                if hit_at is not None:
                    reached += 1
                    times.append(hit_at[11:16] if len(hit_at) >= 16 else hit_at)
                    day_peaks.append(max((v for _, v in curve), default=0))
            rows.append({
                "target": target,
                "reached": reached,
                "ratio": (reached / len(days) * 100) if days else 0.0,
                "time": sorted(times)[len(times) // 2] if times else "--",
                "peak": sorted(day_peaks)[len(day_peaks) // 2] if day_peaks else None,
            })

        self.table.setColumnCount(len(HIT_COLUMNS))
        self.table.setHorizontalHeaderLabels(HIT_COLUMNS)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            cells = [f"+{r['target']:,}", f"{r['reached']}/{len(days)}", f"{r['ratio']:.0f}%",
                     r["time"], f"{r['peak']:+,.0f}" if r["peak"] is not None else "--"]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                # Green where the level is reached often enough to be a
                # credible target, red where it essentially never is.
                if r["ratio"] >= 60:
                    item.setBackground(GAIN)
                elif r["ratio"] < 30:
                    item.setBackground(LOSS)
                else:
                    item.setBackground(NEUTRAL)
                if col >= 1:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(i, col, item)
        self.table.resizeColumnsToContents()

    def _render_verdict(self, days: list[dict], excluded: int = 0):
        scored_days = [self._score_day(rec) for rec in days]
        keys = sorted({k for sd in scored_days for k in sd})
        totals = {k: 0.0 for k in keys}
        actual_total = 0.0
        fired_days = 0
        hindsight_total = 0.0

        for rec, scored in zip(days, scored_days):
            actual = rec.get("actual_net") or 0.0
            actual_total += actual
            if any(f for _, f in scored.values()):
                fired_days += 1
            for k in keys:
                amt, _ = scored.get(k, (actual, False))
                totals[k] += amt
            hindsight_total += max((a for a, _ in scored.values()), default=actual)

        ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        best_key, best_total = ranked[0] if ranked else ("--", actual_total)
        gain = best_total - actual_total

        top = " &nbsp;&middot;&nbsp; ".join(
            f"{k}: <b>{v:+,.0f}</b>" for k, v in ranked[:3])
        excluded_note = (f" <span style='color:#b35400'>({excluded} partial-leg day(s) excluded from "
                          f"this comparison -- untick the box above to include them.)</span>" if excluded else "")
        day_desc = "full-group day(s)" if self.full_group_check.isChecked() else "day(s)"
        self.headline.setText(
            f"Over <b>{len(days)}</b> {day_desc} the strategy actually made <b>{actual_total:+,.0f}</b>."
            f"{excluded_note}<br>"
            f"Best single setting applied to every day: <b>{best_key}</b> &rarr; "
            f"<b>{best_total:+,.0f}</b> ({gain:+,.0f}).<br>"
            f"<small>Runners-up &mdash; {top}. "
            f"Picking each day's best in hindsight would give {hindsight_total:+,.0f}, "
            f"but that cannot be traded: you would have to know the winner in advance.</small>"
        )

        if fired_days < MIN_ARMED_DAYS:
            self.verdict.setText(
                f"<span style='color:#b35400'><b>Too early to choose.</b> The stop has actually fired on "
                f"{fired_days} of {len(days)} day(s); {MIN_ARMED_DAYS} are needed before a winner means "
                f"anything. Days where it never fired just repeat the real result and separate nothing. "
                f"{len(keys)} settings are being scored against every recorded day.</span>")
        else:
            self.verdict.setText(
                f"<span style='color:#1e6b34'><b>Best so far: {best_key}</b>, {gain:+,.0f} versus what "
                f"actually happened, across {fired_days} day(s) where the stop fired "
                f"({len(keys)} settings compared).</span>")
