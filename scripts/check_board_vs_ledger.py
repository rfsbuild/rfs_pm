#!/usr/bin/env python3
"""Flag open board cards that the LEDGER says are already settled.

HER RULING, 2026-09-15: "this card is also something you already know was sorted
out — whenever I send you something in here you NEED to analyze things and change
how we're doing so things don't happen again. I need that board to be my control
board for the day."

THE FAILURE THIS EXISTS TO CATCH. On 2026-09-15 at 09:30 the session established
that Heaton's $21,000 check had posted and the hold released — verified twice,
written into the forecast, written into the session log. The board card
`guernsey_heaton_check_2586_21000` went on saying "ask Rafael where the physical
check is ... and deposit it" for the rest of the day, and she found it herself.
The fact was established in one carrier and never swept into the others
(see memory: feedback_sweep_every_carrier_when_she_rules — a ruling has six
carriers, and THE BOARD IS ONE OF THEM).

That is a class, not an incident: anything proven in a reconcile, a payment
check or a remittance match can leave a card standing that contradicts it. A
board that asks her to do something already done costs her exactly the time the
board exists to save, and worse, it teaches her not to trust the cards.

WHAT IT DOES. For every OPEN card, pull the dollar amounts out of its text and
ask transaction_db.json whether money of that exact size moved ON OR AFTER the
day the card appeared. The date test is the whole control: an amount alone
collides constantly, but an amount that moved WHILE THE CARD WAS OPEN is the
shape of "this is already done".

🔴 WHY NOT MATCH ON THE CHECK NUMBER — tried first, and it is wrong.
Check numbers are reused across years and across DIRECTIONS. The card that
started all this cites Heaton's incoming check #2586 ($21,000, 2026); the ledger
also holds an RFS-issued OUTGOING check #2586 (-$20,000, 2025-08-01). Matching on
the number would have "confirmed" the card as settled by a payment that is not
merely a different transaction but money moving the opposite way, a year earlier.
So a check number counts here ONLY when its row also postdates the card.

A hit is a LEAD, not a verdict. The output names the ledger row and asks for a
human read; it never closes anything.

    python3 scripts/check_board_vs_ledger.py       # exit 1 if any card is flagged
    python3 scripts/check_board_vs_ledger.py --all # include pre-dating coincidences
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.environ.get("PM_STATE") or os.path.join(HERE, "pm_state.json")
LEDGER = os.path.join(os.path.expanduser("~"), "rfs_dashboard", "transaction_db.json")

# "check #3946", "check 3946", "#3946" — 3-5 digits, not a year, not a dollar figure
CHECK_RE = re.compile(r"(?:che(?:ck|que)\s*#?\s*|#)(\d{3,5})\b", re.I)
MONEY_RE = re.compile(r"\$\s?([\d,]+\.\d{2})")


def card_text(c):
    parts = [c.get("subject") or "", c.get("action") or "", c.get("ctx_sum") or ""]
    parts += [str(x) for x in (c.get("ctx_body") or [])]
    parts += [str(x) for x in (c.get("hadassa_todo") or [])]
    return " \n".join(parts)


def load_rows():
    with open(LEDGER) as fh:
        db = json.load(fh)
    rows = []
    for bank, rs in db.items():
        if isinstance(rs, list):
            for r in rs:
                if isinstance(r, dict):
                    rows.append((bank, r))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="also show amount matches that PREDATE the card (coincidences)")
    a = ap.parse_args()

    with open(STATE) as fh:
        items = json.load(fh)["items"]
    rows = load_rows()

    by_amt = {}
    for bank, r in rows:
        try:
            amt = round(abs(float(r.get("amount") or 0)), 2)
        except (TypeError, ValueError):
            continue
        if amt >= 500:
            by_amt.setdefault(amt, []).append((bank, r))

    hits, older = [], []
    for c in items:
        if c.get("status") != "open":
            continue
        seen = str(c.get("first_seen") or "")[:10]
        txt = card_text(c)
        nums = set(CHECK_RE.findall(txt))
        for m in set(MONEY_RE.findall(txt)):
            amt = round(float(m.replace(",", "")), 2)
            if amt < 500:
                continue
            for bank, r in by_amt.get(amt, []):
                d = str(r.get("date") or "")[:10]
                ref = str(r.get("ref") or "").split(".")[0]
                why = "$%s" % m + (" + check #%s" % ref if ref in nums else "")
                (hits if (seen and d >= seen) else older).append((c, why, bank, r))

    def show(rows_, head):
        """One block per CARD, not per ledger row.

        A recurring amount ($1,250.00 goes out most weeks) matched four separate
        checks and printed the same card four times — which is exactly the
        repetition she said costs her time. Group by card, show the two most
        recent rows, and SAY how many there were: a recurring amount is itself a
        signal that the match is weak, so hiding the count would mislead."""
        if not rows_:
            return
        print(head)
        by_card = {}
        for c, why, bank, r in rows_:
            by_card.setdefault(c["id"], (c, []))[1].append((why, bank, r))
        for cid, (c, ms) in sorted(by_card.items(),
                                   key=lambda kv: max(str(r.get("date") or "") for _, _, r in kv[1][1]),
                                   reverse=True):
            ms.sort(key=lambda m: str(m[2].get("date") or ""), reverse=True)
            print("  %s   (card since %s)" % (cid, str(c.get("first_seen") or "?")[:10]))
            print("     card says : %s" % (c.get("subject") or "")[:100])
            for why, bank, r in ms[:2]:
                print("     matched %-13s %s %s %s %s  ref %s"
                      % (why, bank, r.get("date"), (r.get("type") or "-")[:8],
                         r.get("amount"), r.get("ref")))
            if len(ms) > 2:
                print("     ⚠️  %d matching ledger rows in all — a RECURRING amount, so this "
                      "match is weak" % len(ms))
        print()

    show(hits, "🔴 OPEN cards where money of that exact size MOVED WHILE THE CARD WAS OPEN\n"
               "   — read each one; the thing it asks for may already have happened:\n")
    if a.all:
        show(older, "· amount matches that PREDATE the card (almost always coincidence):\n")

    n = len({c["id"] for c, _, _, _ in hits})
    if n:
        print("%d open card(s) flagged. A hit is a LEAD, not a verdict — open the card and "
              "read it against the ledger row before closing anything." % n)
    else:
        print("ok    no open card names money that has moved since the card appeared")
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main())
