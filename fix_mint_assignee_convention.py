#!/usr/bin/env python3
"""Normalise the 4 cards minted today that stored the literal "hadassa".

The board's storage convention is assignee=None FOR HER — set_assignee() enforces it
(`None if who in (None,"hadassa") else who`). new_item(**mint) bypasses that, so today's
mints stored the string. Route them through set_assignee so they match every other card.

Also reports whether any card is assigned to ALICE, who left 2026-09-15 but is still a
selectable value in ASSIGNEES — the exact case of the rule written today
(feedback_verify_who_did_it_before_crediting, "THE THIRD TENSE").
"""
import sys, collections
sys.path.insert(0, '/Users/Hadassa/rfs_pm')
import pm_state as P

st, status = P.load_state()
assert status == 'ok', status

literal = [i['id'] for i in st['items'] if i.get('assignee') == 'hadassa']
print("cards storing the literal 'hadassa': %d" % len(literal))
for cid in literal:
    P.set_assignee(cid, 'hadassa')       # normalises to None
    print("   normalised %s" % cid)

st2, _ = P.load_state()
still = [i['id'] for i in st2['items'] if i.get('assignee') == 'hadassa']
print("remaining literal 'hadassa': %d" % len(still))

alice = [(i['id'], i.get('status'), (i.get('subject') or '')[:70])
         for i in st2['items'] if i.get('assignee') == 'alice']
print("\ncards assigned to ALICE (departed 2026-09-15): %d" % len(alice))
for a in alice:
    print("   %s [%s] %s" % a)
print("   ALICE is still in ASSIGNEES %s — a departed person is still selectable in the UI."
      % (P.ASSIGNEES,))

c = collections.Counter(i.get('assignee') for i in st2['items'] if i.get('status') == 'open')
print("\nOPEN BY OWNER (None == Hadassa, by convention): "
      + " · ".join("%s %d" % (k, n) for k, n in c.most_common()))
print("total open:", sum(c.values()))
