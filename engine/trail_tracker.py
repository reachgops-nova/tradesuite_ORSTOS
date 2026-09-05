"""
==========================================================
TradeSuite
engine/trail_tracker.py
==========================================================

Records the intraday COMBINED P&L path of a strategy group, and scores
group-level trailing-stop variants against it -- live, as the day runs,
without ever touching a real order.

## Why this exists as live instrumentation rather than a backtest

A trailing stop is path-dependent: the day's peak alone cannot tell you
what it would have done, you need the order the P&L moved in. That path
is only reconstructable from the option's own minute bars, and the
broker stops serving weekly contracts once they roll off. Measured
2026-08-27: of 31 past ORB days on file, exactly **2** still had full
minute data; everything before 21 Aug returned nothing at all. So this
cannot be answered by looking backwards -- it can only be answered by
recording forwards, starting now.

## What is stored

`curve` -- the raw (timestamp, combined P&L) series, which is the
durable asset here. Any rule can be re-scored against a stored curve
later, including thresholds nobody has thought of yet, without needing
the instrument to be re-run or the contracts to still exist.

`variants` -- a few fixed (activation, trail) pairs scored live, so the
answer is visible day to day rather than only after an offline pass.

Nothing in this module places, modifies or cancels an order. It is
observation only, in the same spirit as the ORB shadow-exit tracker that
has run alongside production for months.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

import pytz

IST = pytz.timezone("Asia/Kolkata")

# (activation, trail). Activation is the profit at which the stop arms at
# breakeven; trail is how far behind the running peak it then follows.
# With trail=1000: peak 1000 -> stop 0, peak 1500 -> stop 500, peak 2000
# -> stop 1000 -- the "breakeven then scale up" shape.
DEFAULT_VARIANTS = [
    ("trail", 1000, 500, None),
    ("trail", 1000, 750, None),
    ("trail", 1000, 1000, None),
    ("trail", 1500, 500, None),
    ("trail", 2000, 1000, None),
    # Ladder: the stop only ratchets at whole steps, which is the rule as
    # Gopinath described it -- reach +1000 go to breakeven, +2000 lock
    # 1500, +2500 lock 2000, +4000 lock 3500. It is deliberately kept
    # separate from the continuous trail rather than treated as the same
    # thing: a ladder waits for the next whole rung before moving, so it
    # sits slightly further behind the peak and gives back a little more.
    # Measured on 2026-08-27: ladder 500/500 banked 3,000 where a
    # continuous 500 trail banked 3,195.
    ("ladder", 1000, 500, 500),
    ("ladder", 1000, 1000, 500),
    ("ladder", 1000, 500, 1000),
]


def score_curve(values, kind: str, activation: float, a: float, b: float | None = None) -> tuple[float, bool]:
    """Score ONE rule against an already-recorded P&L path.

    Returns (amount banked, whether the stop actually fired). Pure and
    self-contained so the same logic serves three callers: the live
    tracker, the report panel re-scoring stored curves against settings
    that did not exist when the day was recorded, and offline analysis.

    Never looks ahead -- peak and stop at each step are computed only
    from values already seen.
    """
    peak = float("-inf")
    armed = False
    last = 0.0
    for v in values:
        last = float(v)
        peak = max(peak, last)
        if not armed and peak >= activation:
            armed = True
        if armed:
            if kind == "ladder":
                stop = max(0.0, math.floor(peak / a) * a - (b or 0))
            else:
                stop = max(0.0, peak - a)
            if last <= stop:
                # Bank what was actually on screen when the breach was seen,
                # not the stop level itself. The engine polls every 30s, so a
                # real exit happens at the first observed price at or below
                # the stop -- assuming a fill exactly at the stop would
                # flatter tight trails, which are precisely the ones most
                # likely to be tripped between polls.
                return round(last, 2), True
    return round(last, 2), False


class TrailTracker:
    """One per strategy group per day. Fed the combined P&L on every poll."""

    def __init__(self, group: str, day: str, variants=None):
        self.group = group
        self.day = day
        self.variants = [self._normalise(v) for v in (variants if variants is not None else DEFAULT_VARIANTS)]
        self.curve: list[tuple[str, float]] = []
        self.peak = float("-inf")
        self.last = 0.0
        # per-variant state: armed flag, and the simulated exit if it fired
        self._state = {self._key(*v): {"armed": False, "exit_pnl": None, "exit_time": None}
                       for v in self.variants}

    @staticmethod
    def _normalise(v) -> tuple:
        """Accepts the old 2-tuple (activation, trail) as well as the
        4-tuple form, so a caller written against the earlier shape keeps
        working."""
        if len(v) == 2:
            return ("trail", v[0], v[1], None)
        return tuple(v)

    @staticmethod
    def _key(kind, activation, a, b) -> str:
        if kind == "ladder":
            return f"ladder_act{int(activation)}_step{int(a)}_gap{int(b)}"
        return f"act{int(activation)}_trail{int(a)}"

    def _stop_for(self, kind, a, b) -> float:
        """Where the stop sits right now, given the running peak."""
        if kind == "ladder":
            rung = math.floor(self.peak / a) * a
            return max(0.0, rung - b)
        return max(0.0, self.peak - a)

    def update(self, combined_pnl: float, at: str | None = None) -> None:
        """Call once per poll with the group's combined mark-to-market.

        `at` overrides the timestamp, which lets a historical curve be
        replayed through this exact code path rather than through a
        second copy of the logic that could drift away from it."""
        now = datetime.now(IST)
        self.last = float(combined_pnl)
        stamp = at or now.isoformat(timespec="seconds")
        self.curve.append((stamp, round(self.last, 2)))
        self.peak = max(self.peak, self.last)

        for kind, activation, a, b in self.variants:
            st = self._state[self._key(kind, activation, a, b)]
            if st["exit_pnl"] is not None:
                continue                      # this variant has already stopped out
            if not st["armed"] and self.peak >= activation:
                st["armed"] = True
            if st["armed"]:
                stop = self._stop_for(kind, a, b)
                if self.last <= stop:
                    # See score_curve(): record the observed value, not the
                    # stop level, so the study matches what a real exit at
                    # this poll would have banked.
                    st["exit_pnl"] = round(self.last, 2)
                    st["exit_time"] = stamp

    def summary_line(self) -> str:
        """Short, human-readable state for the activity log."""
        fired = [k for k, st in self._state.items() if st["exit_pnl"] is not None]
        armed = [k for k, st in self._state.items() if st["armed"] and st["exit_pnl"] is None]
        bits = f"now {self.last:+.0f}, peak {self.peak:+.0f}"
        if armed:
            bits += f", armed: {len(armed)}"
        if fired:
            bits += f", would-have-exited: {', '.join(fired)}"
        return bits

    def result(self, actual_net: float | None = None,
               legs_traded: int | None = None, legs_total: int | None = None) -> dict:
        """legs_traded/legs_total -- e.g. 2 of 3 underlyings actually
        entered a position today. Recorded so the comparison tooling can
        tell a genuine full-group day apart from a partial one: a day
        where only 1 of 3 legs fired has a much smaller combined-P&L
        ceiling than a full day, and silently mixing the two into one
        ranking makes a trailing setting look better or worse than it
        really is for the scenario it's actually being judged against.
        See gui/trail_study_panel.py's "Full group only" filter."""
        return {
            "group": self.group,
            "day": self.day,
            "peak_combined": round(self.peak, 2) if self.peak != float("-inf") else None,
            "final_combined": round(self.last, 2),
            "actual_net": actual_net,
            "legs_traded": legs_traded,
            "legs_total": legs_total,
            "variants": {
                self._key(*v): {**self._state[self._key(*v)],
                                 "kind": v[0], "activation": v[1], "a": v[2], "b": v[3]}
                for v in self.variants
            },
            "curve": self.curve,
        }

    def save(self, directory: Path, actual_net: float | None = None,
              legs_traded: int | None = None, legs_total: int | None = None) -> Path | None:
        """Merges into the day's file so ORB and TOS groups sit side by side.
        Fail-soft: instrumentation must never break a running strategy."""
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{self.day}.json"
            existing = {}
            if path.exists():
                try:
                    existing = json.load(open(path))
                except (json.JSONDecodeError, OSError):
                    existing = {}
            existing[self.group] = self.result(actual_net, legs_traded, legs_total)
            with open(path, "w") as f:
                json.dump(existing, f, indent=2)
            return path
        except OSError:
            return None
