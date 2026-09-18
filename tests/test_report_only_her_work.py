#!/usr/bin/env python3
"""Her daily report must contain ONLY her own work (2026-09-18).

WHAT WENT WRONG
---------------
`pm()` builds "HER OWN RECORD OF WHAT SHE DID". Her scope, verbatim on
2026-07-30: *"this is about ME, MY work for the entire day, NOT Rafael, NOT
anyone else."*

The COMPLETION filter honoured that from the start (`done_by` had to be hers).
The UPDATE lines never did — a note printed whenever the CARD passed the filter,
no matter who wrote it. That was harmless while she was the only writer. It
stopped being harmless on 2026-09-14, when Rafael got write access to the guest
listener.

Measured on 2026-09-18: her report announced **"12 things you did, across 6
areas"** — and all twelve were Rafael's notes, several in Portuguese
("Ja foi feita a inspecao e ja pegamos o Certificate of occupancy"). Her own
line count that day was zero.

A report that attributes someone else's work to her is worse than an empty one:
it is wrong in the direction she cannot detect by reading it.

WHAT THIS LOCKS
---------------
1. An update written by another actor never appears in her report.
2. A card CLOSED by another actor never appears either.
3. A blank author still counts as hers — `by` was only stored from the guest
   listener onward, so older lines genuinely were her own. This is the single
   place a blank may default to her, and only because history makes it true.
"""
import json, os, sys, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "gdr", os.path.join(HERE, "..", "gen_daily_report.py"))
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)

DAY = "2026-09-18"


def _state():
    return {"items": [
        {"id": "a", "subject": "s", "project": "70 Robin", "status": "open",
         "done_by": None, "done_at": None,
         "updates": [
             {"at": DAY + "T09:00:00-04:00", "by": "hadassa", "text": "HER OWN LINE"},
             {"at": DAY + "T10:21:00-04:00", "by": "rafael@rfsbuilders.com",
              "text": "RAFAEL LINE"},
             {"at": DAY + "T10:30:00-04:00", "by": None, "text": "LEGACY LINE"},
         ]},
        {"id": "b", "subject": "closed by Rafael", "project": "104 Child",
         "status": "done", "done_by": "rafael@rfsbuilders.com",
         "done_at": DAY + "T10:35:00-04:00", "did": "RAFAEL CLOSED IT",
         "updates": []},
        {"id": "c", "subject": "closed by her", "project": "51 Cedar",
         "status": "done", "done_by": "hadassa",
         "done_at": DAY + "T11:00:00-04:00", "did": "SHE CLOSED IT",
         "updates": []},
    ]}


def test_another_actors_update_never_prints_in_her_report():
    html = G.pm(_state(), DAY)
    assert "RAFAEL LINE" not in html, "Rafael's note printed as her work"


def test_a_card_closed_by_another_actor_never_prints():
    html = G.pm(_state(), DAY)
    assert "RAFAEL CLOSED IT" not in html, "Rafael's completion printed as her work"


def test_her_own_work_still_prints():
    html = G.pm(_state(), DAY)
    assert "HER OWN LINE" in html
    assert "SHE CLOSED IT" in html


def test_a_blank_author_still_counts_as_hers():
    """64 of 81 lines on the live board predate the `by` field."""
    html = G.pm(_state(), DAY)
    assert "LEGACY LINE" in html


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print("  ✅ %s" % name)
            except AssertionError as exc:
                fails += 1; print("  ❌ %s — %s" % (name, exc))
    sys.exit(1 if fails else 0)
