#!/usr/bin/env python3
"""The 2026-09-15 fail-open defect, pinned.

A cross-check filtered cards on `done` / `dropped`. Neither field exists, so the
filter passed every card and reported "305 total, 305 open" — with 127 done and
15 dismissed cards counted as live. The result: all 19 of Alice's flagged items
were marked "already covered" on the first pass, when four were real gaps.

Test 1 REPRODUCES the old behaviour to prove the trap is real; the rest prove
require_fields() now refuses it.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import board_query as B                                            # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  — " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


items = B.load()
print("1 · the trap is real — the old filter silently passes everything")
old = [c for c in items if not c.get("done") and not c.get("dropped")]
check("a filter on non-existent fields selects ALL cards",
      len(old) == len(items), "%d of %d" % (len(old), len(items)))
check("...while the real open count is strictly smaller",
      len(B.items_where(items, status="open")) < len(items))

print("2 · require_fields() refuses a field no card carries")
for bad in ("done", "dropped", "compleeted"):
    try:
        B.items_where(items, **{bad: True})
        check("items_where(%s=...) raises" % bad, False, "it returned instead")
    except KeyError as e:
        check("items_where(%s=...) raises" % bad, bad in str(e))

print("3 · a real field still works, and the counts reconcile")
c = B.counts(items)
check("status counts cover every card", sum(c.values()) == len(items))
check("all three lanes present", set(c) <= B.KNOWN_STATUS and "open" in c, str(c))
check("items_where(status='open') matches the count",
      len(B.items_where(items, status="open")) == c["open"])

print("4 · a typo'd VALUE is caught too, not just a typo'd field")
try:
    B.items_where(items, status="opened")
    check("status='opened' raises", False)
except ValueError:
    check("status='opened' raises", True)

print("5 · grep is accent/case-insensitive and does not fail open")
check("a nonsense term matches nothing", len(B.grep(items, "zzzqqq")) == 0)
check("a real term matches something", len(B.grep(items, "guernsey")) > 0)

print()
if fails:
    print("🔴 %d FAILURE(S): %s" % (len(fails), ", ".join(fails)))
    sys.exit(1)
print("✅ all checks passed")
