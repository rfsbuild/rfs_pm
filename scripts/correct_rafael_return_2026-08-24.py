#!/usr/bin/env python3
"""Correct the "Rafael is back today" claim across every card that carries it.

WHY
    The 13:57 sweep concluded Rafael returned from vacation on Mon 2026-08-24
    and wrote that into FIVE cards. Hadassa's correction, 2026-08-24: "he's
    probably gonna be back on wednesday, not today."

    Five carriers, found by grepping the board rather than by recalling which
    cards mentioned it -- a first pass from memory would have found three.
    [[feedback_sweep_every_carrier_when_she_rules]]

TWO THINGS THIS IS CAREFUL ABOUT

    1. "PROBABLY" IS NOT A DATE. She said probably Wednesday. So no card is
       given "Rafael returns 2026-08-26" as a fact -- every mention is written
       as HER EXPECTATION, unconfirmed. [[feedback_dont_state_inference_as_fact]]

    2. THE CLIENT DRAFT GETS NO DATE AT ALL. The Bob Rochford draft said
       "Rafael is back from travel today and will come back to you on the
       technical items". That is a false statement to a client AND a delivery
       promise resting on it. Putting "Wednesday" in its place would just make
       a probable date into a commitment Bob could hold her to, so the claim is
       removed and the sentence carries no date. She can add one if she wants.

    The vacation-spend tracking is deliberately NOT touched: per
    [[project_rafael_vacation_spend_tracking]] it runs "till she says he's
    back", and "probably Wednesday" is not that. See the FLAG at the end.
"""
import argparse
import datetime
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import pm_ingest as I   # noqa: E402
import pm_state as S    # noqa: E402

EXPECT = "Hadassa expects him back Wed 2026-08-26, not confirmed"

# (card id, field, old substring, new substring). Every anchor is asserted to
# match exactly once before anything is written.
EDITS = [
    # ── the client-facing draft: claim removed, NO date put in its place ──
    ("sw0824-bob-approved-and-asks-about-bundling", "draft.body",
     "Rafael is back from travel today and will come back to you on the "
     "technical items in your note — the rafter hangers, the living-room-only "
     "mini-split option, the attic/steam re-plumb question, the roofing "
     "project management percentage, and your CAT6E plan.",
     "Rafael will come back to you on the technical items in your note — the "
     "rafter hangers, the living-room-only mini-split option, the attic/steam "
     "re-plumb question, the roofing project management percentage, and your "
     "CAT6E plan — once he is back from travel."),

    ("sw0824-bob-five-open-questions", "meta",
     "Rafael drove back from vacation this morning",
     "Rafael is STILL TRAVELLING — %s" % EXPECT),
    ("sw0824-bob-five-open-questions", "ctx_needed",
     "Sit with Rafael now that he is back and get a position on each of the "
     "five before replying.",
     "Sit with Rafael once he is back (%s) and get a position on each of the "
     "five before replying." % EXPECT),

    ("sw0824-guilherme-qbtime-clockin", "claude_done",
     "Flagged the deferred item: Rafael promised to sit down about the "
     "Andersen/RFS hours split once he is back, and he came back today.",
     "Flagged the deferred item: Rafael promised to sit down about the "
     "Andersen/RFS hours split once he is back. ⚠️ He is NOT back — %s."
     % EXPECT),
    ("sw0824-guilherme-qbtime-clockin", "hadassa_todo",
     "Now that Rafael is back, book the conversation he promised about the "
     "Andersen vs RFS hours split.",
     "Once Rafael is back (%s), book the conversation he promised about the "
     "Andersen vs RFS hours split." % EXPECT),

    ("rba_gui_truck_load_4_30am", "hadassa_todo",
     "Get a named person for next week's truck loading from Rafael, now that "
     "he is back.",
     "Get a named person for next week's truck loading from Rafael. ⚠️ He is "
     "not back yet — %s — and the loading is needed NEXT WEEK, so this has to "
     "be settled before Monday." % EXPECT),

    ("sw0824-99concord-subs-unavailable", "claude_done",
     "Noted Rafael is back today, which unblocks TJ Painting.",
     "⚠️ CORRECTED: Rafael is NOT back today — %s — so TJ Painting stays "
     "blocked until he returns." % EXPECT),
    ("sw0824-99concord-subs-unavailable", "hadassa_todo",
     "Now Rafael is back, schedule TJ Painting's visit.",
     "Schedule TJ Painting's visit once Rafael is back (%s)." % EXPECT),
]


# `_no_draft` is a CONTROL key, asserted per briefing and never stored on the
# card — so any later ingest touching a chase card has to restate it. That is
# deliberate in pm_ingest: an absent draft must always be an explicit claim,
# never a silent gap.
NO_DRAFT = {
    "sw0824-guilherme-qbtime-clockin":
        "The next step is a LOOKUP she must run in QuickBooks — did Guilherme "
        "accept the invite, and have per-job entries appeared since 8/18 — and "
        "only a 'no' produces a message. The second half (the Andersen/RFS "
        "hours conversation Rafael promised) cannot be drafted either: he is "
        "still travelling.",
}


def apply_edits(state, dry):
    items, misses = {}, []
    for iid, field, old, new in EDITS:
        card = S.get_item(state, iid)
        if card is None:
            misses.append("%s: not on the board" % iid)
            continue
        spec = items.setdefault(iid, {"id": iid})

        if field == "draft.body":
            src = dict(spec.get("draft") or card.get("draft") or {})
            body = src.get("body", "")
            n = body.count(old)
            if n != 1:
                misses.append("%s draft.body: anchor matched %d times, want 1"
                              % (iid, n))
                continue
            src["body"] = body.replace(old, new)
            spec["draft"] = src
        else:
            cur = spec.get(field, card.get(field))
            if isinstance(cur, list):
                n = sum(s.count(old) for s in cur if isinstance(s, str))
                if n != 1:
                    misses.append("%s %s: anchor matched %d times, want 1"
                                  % (iid, field, n))
                    continue
                spec[field] = [s.replace(old, new) if isinstance(s, str) else s
                               for s in cur]
            else:
                n = (cur or "").count(old)
                if n != 1:
                    misses.append("%s %s: anchor matched %d times, want 1"
                                  % (iid, field, n))
                    continue
                spec[field] = cur.replace(old, new)
        if not dry:
            print("   ✓ %-44s %s" % (iid, field))
    for iid, why in NO_DRAFT.items():
        if iid in items:
            items[iid]["_no_draft"] = why
    return list(items.values()), misses


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    state, _ = S.load_state(S.STATE_PATH)
    items, misses = apply_edits(state, dry=not a.apply)
    if misses:
        print("🔴 ANCHOR CHECK FAILED — nothing written:")
        for m in misses:
            print("   -", m)
        return 1
    print("   all %d anchors matched exactly once, across %d cards"
          % (len(EDITS), len(items)))

    errs = I.validate(items, existing={i["id"]: i for i in state["items"]})
    if errs:
        print("\n🔴 VALIDATION FAILED — nothing written:")
        for e in errs:
            print("   -", e)
        return 1
    print("   ✅ validate() clean")

    if not a.apply:
        print("\n(dry run — pass --apply to write)")
        return 0

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = S.STATE_PATH.with_suffix(".json.bak_%s" % stamp)
    shutil.copy2(S.STATE_PATH, bak)
    print("\n   backup → %s" % bak.name)
    I.ingest(items, path=S.STATE_PATH)

    # ── post-condition: the claim must be GONE from the whole board ──
    import json
    import re
    after, _ = S.load_state(S.STATE_PATH)
    # The check must find the AFFIRMATIVE claim only. A first version matched
    # "back today" anywhere and flagged this script's OWN corrective sentence
    # ("Rafael is NOT back today") as a survivor. The fix is the negative
    # lookbehind, not rewording the data to dodge the regex — tuning a check
    # until it goes green is how a real finding gets lost.
    pat = re.compile(r"(?<!not )(?:back today|back from vacation this morning"
                     r"|drove back|came back today)"
                     r"|(?<!once )he is back(?! \()"
                     r"|Now Rafael is back", re.I)
    survivors = [i["id"] for i in after["items"]
                 if pat.search(json.dumps(i, ensure_ascii=False))]
    if survivors:
        print("\n🔴 THE CLAIM SURVIVES ON: %s" % survivors)
        return 1
    print("   ✅ post-condition: the claim is gone from all %d cards"
          % len(after["items"]))
    print("\n📌 FLAG, deliberately NOT auto-applied:")
    print("   project_rafael_vacation_spend_tracking models Aug 6–30 = 25 days")
    print("   at $160.00/day. If he is off the road Wed 8/26 that is ~21 days,")
    print("   not 25 — but the memory fixes the basis and names _cash_forecast")
    print("   as the system of record, and tracking runs 'till she says he's")
    print("   back'. 'Probably Wednesday' is not that. HER CALL.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
