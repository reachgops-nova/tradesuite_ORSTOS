"""
==========================================================
TradeSuite
gui/labels.py
==========================================================

Customer-facing strategy names, in ONE place so the Strategies tab,
Trade Log, History and Activity log can never drift apart.

These are display labels only. The internal strategy identifiers --
orb_strategy.STRATEGY_NAME ("ORB_Paper_v1") and
tamil_strategy.STRATEGY_NAME ("TamilOptionSeller30_v1") -- are
deliberately NOT renamed: they're the tag every order carries into
OpenAlgo's orderbook/tradebook, so renaming them would split each
strategy's own order history into "before" and "after" buckets at the
broker end for zero customer-visible benefit. Config keys ("orb",
"tamil") stay put for the same reason -- they're what every saved
tradesuite_settings.json on every existing install is already keyed on.

Each strategy has a LONG form (headings, group boxes, summary lines --
where there's room to say what the strategy actually does) and a SHORT
form (table cells, where a 40-character label would blow the column
out).
"""

from __future__ import annotations

ORB_LABEL = "ORB (Volume Imbalance -- 2-candle & PCT)"
ORB_SHORT = "ORB"

TAMIL_LABEL = "TOS-30 (Imbalance)"
TAMIL_SHORT = "TOS-30"

# Read-only in TradeSuite: WhatsApp signals depend on Gopinath's own private
# groups and are deliberately NOT a tradeable strategy in the product (see the
# plan). Their history is shown so the reporting covers all three strategies
# he actually runs, but there is no Start button for them.
WHATSAPP_LABEL = "WhatsApp Signals (history only)"
WHATSAPP_SHORT = "WA-Signal"

# ORB exit rules, named the way the customer picks them in the UI.
EXIT_MODE_LABELS = {
    "reversal": "2-candle",
    "pct_3": "PCT",
}


def exit_mode_label(mode: str | None) -> str:
    return EXIT_MODE_LABELS.get(mode or "", mode or "")
