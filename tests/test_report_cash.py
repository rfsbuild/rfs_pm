"""Cash-outlook wording + tile tests for gen_daily_report.financial().

Hadassa caught the report saying "The tightest day ahead is Tuesday, July 28"
ON Tuesday, July 28 (2026-07-28). "Ahead" is only true for a FUTURE day. These
tests pin both branches so the phrasing can't silently drift back.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gen_daily_report as G  # noqa: E402
import pm_state as S  # noqa: E402


def _text(html_str):
    """Strip tags so assertions read like the sentence Hadassa sees."""
    return " ".join(re.sub(r"<[^>]+>", " ", html_str).split())


def _state(trough_date, eod, bank=14886.94, floor=15000.0, cc_pending=None):
    d = {
        "bank_balance": bank,
        "cc_capital_one": 62618.22,
        "_cash_forecast": {
            "buffer_floor": floor,
            "days": [{"date": trough_date, "eod_planned": eod}],
        },
    }
    if cc_pending is not None:
        d["_cc_pending"] = {"capital_one": cc_pending}
    return d


def test_trough_today_below_floor_does_not_say_ahead():
    """The exact 2026-07-28 bug: trough is TODAY, so it is not 'ahead'."""
    out = _text(G.financial(_state(S._today(), 14006.94), None))
    assert "is today," in out
    assert "tightest day ahead" not in out
    assert "$14,006.94" in out
    assert "$993.06 under the $15,000.00 floor" in out


def test_trough_future_below_floor_says_ahead():
    out = _text(G.financial(_state("2099-08-07", 14006.94), None))
    assert "The tightest day ahead is" in out
    assert "is today," not in out


def test_trough_today_above_floor_does_not_say_ahead():
    out = _text(G.financial(_state(S._today(), 27224.07), None))
    assert "The lowest point is today," in out
    assert "lowest point ahead" not in out
    assert "staying above the $15,000.00 floor" in out


def test_trough_future_above_floor_says_ahead():
    out = _text(G.financial(_state("2099-08-07", 27224.07), None))
    assert "The lowest point ahead is" in out
    assert "is today," not in out


def test_cc_pending_is_disclosed_and_not_added_to_the_headline():
    """Pending card charges must be named, not folded into the balance."""
    out = _text(G.financial(
        _state("2099-08-07", 27224.07,
               cc_pending=[{"amount": 2128.65, "description": "pending"}]), None))
    assert "$62,618.22" in out          # headline stays the POSTED balance
    assert "$2,128.65 pending, not yet posted" in out
    assert "$64,746.87" not in out      # never silently summed


def test_no_cc_pending_leaves_the_sub_label_clean():
    out = _text(G.financial(_state("2099-08-07", 27224.07, cc_pending=[]), None))
    assert "pending, not yet posted" not in out


def test_cash_tile_states_the_available_basis():
    """FD and Rafael must not read the figure as gross of pending holds."""
    out = _text(G.financial(_state("2099-08-07", 27224.07), None))
    assert "available balance" in out
    assert "pending holds already deducted" in out
