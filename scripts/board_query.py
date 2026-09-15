#!/usr/bin/env python3
"""Query pm_state.json items WITHOUT the silent-fail-open trap.

🔴 WHY THIS EXISTS (2026-09-15). A board cross-check filtered cards with
`c.get('done')` and `c.get('dropped')`. NEITHER FIELD EXISTS — the real field is
`status` ('open' | 'done' | 'dismissed'). Every `.get()` returned None, so the
filter passed everything and the run reported "305 total, 305 open" while 127
done and 15 dismissed cards were being treated as live. The first pass of a
cross-check against Alice's handoff consequently marked ALL 19 of her flagged
items as already covered, when four were genuine gaps — one of them the only
safety item in her list.

A filter keyed on a field name that does not exist does not raise. It returns a
plausible number. That is the whole danger, and it is why `fields()` below
refuses a name that no card carries.

Use:
    from board_query import load, items_where, counts
    open_cards = items_where(load(), status='open')

CLI:
    python3 scripts/board_query.py --counts
    python3 scripts/board_query.py --field status
    python3 scripts/board_query.py --where status=open --grep guernsey
"""
import argparse
import json
import pathlib
import sys
import unicodedata

STATE = pathlib.Path(__file__).resolve().parent.parent / "pm_state.json"

# The lanes a card can actually be in. Stated here so a typo in a caller's
# expected value is caught too, not just a typo in the field name.
KNOWN_STATUS = {"open", "done", "dismissed"}

TEXT_FIELDS = ("subject", "ctx_sum", "ctx_body", "action", "project", "where",
               "ctx_happened", "ctx_matters", "note")


def load(path=None):
    return json.load(open(path or STATE))["items"]


def fields(items):
    """Every field name that at least one card carries."""
    out = set()
    for c in items:
        out.update(c.keys())
    return out


def require_fields(items, *names):
    """Raise unless EVERY name is a real field on at least one card.

    This is the control. Without it a filter on a misspelled or imagined field
    silently selects everything (or nothing) and reports a confident number.
    """
    have = fields(items)
    missing = [n for n in names if n not in have]
    if missing:
        raise KeyError(
            "field(s) not present on any of the %d cards: %s\n"
            "  the board's real fields include: %s"
            % (len(items), ", ".join(missing),
               ", ".join(sorted(n for n in have if not n.startswith("_"))[:18])))


def items_where(items, **kw):
    """Filter by exact field values, after proving every field name is real."""
    require_fields(items, *kw.keys())
    if "status" in kw and kw["status"] not in KNOWN_STATUS:
        raise ValueError("status=%r is not one of %s"
                         % (kw["status"], sorted(KNOWN_STATUS)))
    return [c for c in items if all(c.get(k) == v for k, v in kw.items())]


def counts(items, field="status"):
    require_fields(items, field)
    out = {}
    for c in items:
        out[c.get(field)] = out.get(c.get(field), 0) + 1
    return out


def _fold(t):
    t = unicodedata.normalize("NFKD", str(t).lower())
    return "".join(ch for ch in t if not unicodedata.combining(ch))


def blob(card):
    return _fold(" ".join(str(card.get(k) or "") for k in TEXT_FIELDS))


def grep(items, term):
    t = _fold(term)
    return [c for c in items if t in blob(c)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--counts", action="store_true")
    ap.add_argument("--field", default="status")
    ap.add_argument("--where", action="append", default=[])
    ap.add_argument("--grep")
    a = ap.parse_args()
    items = load()
    if a.counts:
        for k, v in sorted(counts(items, a.field).items(), key=lambda kv: -kv[1]):
            print("  %-12s %d" % (k, v))
        return 0
    sel = items
    for w in a.where:
        k, _, v = w.partition("=")
        sel = items_where(sel, **{k: v})
    if a.grep:
        sel = grep(sel, a.grep)
    for c in sel:
        print("[%-9s] %-46s %s" % (c.get("status"), c["id"][:46],
                                   str(c.get("subject"))[:88]))
    print("\n%d card(s) of %d" % (len(sel), len(items)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
