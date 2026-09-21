#!/usr/bin/env python3
"""HER RULING 2026-09-21 — Claude may close HER cards, but only on her instruction.

Verbatim: *"I need you to change the rule you mentioned previously about you not
being able to change anything that's mine in the PM command. If I give you a
specific command in here, you can do it. If I didn't tell you to change any
cards, you can't deal with them."*

Two halves, and a test that only checks the permissive half is worthless — it is
the RESTRICTIVE half that protects her board. So both are pinned here:

  1. WITH her instruction  -> a card she never delegated CAN be closed.
  2. WITHOUT her instruction -> the same card still REFUSES. Unchanged.

And the attribution law is pinned alongside, because widening a permission is
exactly when attribution quietly rots:

  3. `done_by` is "claude" on BOTH paths — never "hadassa". Her instruction is
     not her click, and the daily report is a record of HER actions.
  4. Her words are stored VERBATIM in `authorization`, so the close can be read
     back and checked against what she actually said.
  5. The new fields survive a save/load round trip — a field missing from the
     whitelist is silently dropped, which would make the audit trail vanish
     while every assertion above still passed in memory.

Run against the PRE-FIX source to prove the test has teeth:
    RFS_PM_MODULE=/tmp/prefix_pm_state.py python3 test_claude_authorized_close.py
Tests 1, 4 and 5 must FAIL there.
"""
import importlib.util, json, os, sys, tempfile

MOD = os.environ.get("RFS_PM_MODULE",
                     os.path.join(os.path.dirname(os.path.abspath(__file__)), "pm_state.py"))
spec = importlib.util.spec_from_file_location("pm_state_under_test", MOD)
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)

HER_WORDS = ("If I give you a specific command in here, you can do it. "
             "If I didn't tell you to change any cards, you can't deal with them.")

ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ✅ %s" % label)
    else:
        fail += 1
        print("  \U0001f534 %s%s" % (label, ("  -- " + detail) if detail else ""))


def fresh(assignee=None):
    """A board holding one open card, optionally delegated to Claude."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    item = {"id": "card1", "status": "open", "title": "a card of hers",
            "assignee": assignee, "done_at": None, "done_by": None,
            "claude_result": None, "dismiss_reason": None, "dismissed_at": None,
            "updates": []}
    json.dump({"_schema": "pm v1", "items": [item]}, open(path, "w"))
    return path


def load(path):
    return json.load(open(path))["items"][0]


print("1. WITH her instruction, a card she never delegated CAN be closed")
p = fresh(assignee=None)
r = S.complete_by_claude("card1", "closed it: the thread already settled this",
                         path=p, on_her_instruction=HER_WORDS)
check("no error returned", not (r or {}).get("error"), repr(r))
check("status is done", load(p)["status"] == "done", load(p)["status"])

print("2. WITHOUT her instruction, the SAME card still refuses")
p2 = fresh(assignee=None)
r2 = S.complete_by_claude("card1", "closing it because I feel like it", path=p2)
check("an error IS returned", bool((r2 or {}).get("error")), repr(r2))
check("the card is untouched", load(p2)["status"] == "open", load(p2)["status"])
check("done_by was not set", load(p2)["done_by"] is None, load(p2)["done_by"])

print("3. done_by is 'claude' on BOTH paths, never 'hadassa'")
check("authorised close -> done_by == 'claude'", load(p)["done_by"] == "claude", load(p)["done_by"])
p3 = fresh(assignee="claude")
S.complete_by_claude("card1", "did the delegated thing", path=p3)
check("delegated close -> done_by == 'claude'", load(p3)["done_by"] == "claude", load(p3)["done_by"])
check("authorised close is NOT attributed to her",
      load(p)["done_by"] != "hadassa" and load(p).get("authorized_by") != "claude")

print("4. her words are stored VERBATIM, not paraphrased")
check("authorization == her exact words", load(p).get("authorization") == HER_WORDS,
      repr(load(p).get("authorization")))
check("authorized_by == 'hadassa'", load(p).get("authorized_by") == "hadassa",
      repr(load(p).get("authorized_by")))

print("5. a delegated close carries NO authorisation (nothing invented)")
check("no authorization field on the delegated path",
      not load(p3).get("authorization"), repr(load(p3).get("authorization")))

print("6. the audit fields survive the whitelist (a dropped field = a vanished trail)")
if hasattr(S, "ALLOWED_FIELDS"):
    wl = set(S.ALLOWED_FIELDS)
else:                                   # the tuple is module-level but may be named otherwise
    wl = {n for n in dir(S)}
    wl = set(next((v for k, v in vars(S).items()
                   if isinstance(v, tuple) and "done_by" in v), ()))
check("'authorized_by' is whitelisted", "authorized_by" in wl)
check("'authorization' is whitelisted", "authorization" in wl)

print("\n%d passed, %d failed" % (ok, fail))
sys.exit(1 if fail else 0)
