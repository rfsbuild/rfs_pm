#!/usr/bin/env python3
"""Consistency gate for the PM Command Board's state.

WHY (2026-09-15). On 2026-09-14 Hadassa ticked 14 cards. The next morning 11 of
them were open again, still carrying the `did` line she typed at tick time. No
one noticed until the board was audited card by card, because nothing compared
the card's own record against its status. This gate does that comparison.

THE CENTRAL CHECK — a card that says it was done but is not done.
    A `kind:"done"` update is written ONLY by the tick-time prompt, which
    renders only after a checkbox is checked (pm_ui.html: updBlock() -> ASK_DID
    -> the [data-done] handler). So a card carrying one was ticked. If its
    status is `open`, the tick did not survive, and the board is now showing her
    work she already did.

    The ONE legitimate exception is the routine lane: pm_routine.py reseeds it
    every day and deliberately resets the tick ("her note survives; the tick
    does not — that is what makes it a daily checklist"). It also POPS `did`,
    so a routine card reopened by design is distinguishable from a lost tick:
    the lost tick keeps its `did`.

Exit 0 = clean. Exit 1 = at least one FAIL. Exit 2 = could not run.
"""
import datetime
import json
import os
import re
import sys

STATE = os.environ.get("PM_STATE") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pm_state.json")


def check(state):
    fails, warns, oks = [], [], []
    items = state.get("items") or []
    oks.append("%d items loaded" % len(items))

    lost = []
    for it in items:
        done_updates = [u for u in (it.get("updates") or [])
                        if isinstance(u, dict) and u.get("kind") == "done"]
        if not done_updates:
            continue
        if it.get("status") != "open":
            continue
        if it.get("lane") == "routine" and not it.get("did"):
            continue          # reseeded by design — pm_routine pops `did`
        if it.get("audit_ruled"):
            continue          # she has ruled on it in Triage — answered, not lost
        lost.append((it["id"], done_updates[-1].get("at", "?"),
                     (done_updates[-1].get("text") or "")[:60]))
    if lost:
        fails.append("%d card(s) were TICKED but are OPEN and UNRULED — the completion "
                     "did not survive. Rule them in the board's 🔍 Triage tab "
                     "(127.0.0.1:8789); ruling one clears it from here:" % len(lost))
        for i, at, txt in sorted(lost, key=lambda r: r[1]):
            fails.append("    %-46s ticked %s  %r" % (i, at[:16], txt))
    else:
        oks.append("no ticked-but-open cards")

    # A done card must say WHO finished it, or the daily report cannot tell her
    # work from Claude's. set_done/apply_click always stamp it; a card missing it
    # was written by something that bypassed them.
    nobody = [it["id"] for it in items
              if it.get("status") == "done" and not it.get("done_by")]
    if nobody:
        warns.append("%d done card(s) carry no done_by (who finished it is "
                     "unrecorded): %s" % (len(nobody), ", ".join(nobody[:6])))
    else:
        oks.append("every done card records who finished it")

    # A dismissal without a reason is exactly what apply_click refuses to write,
    # so one in the file means something wrote around the server.
    noreason = [it["id"] for it in items
                if it.get("status") == "dismissed" and not it.get("dismiss_reason")]
    if noreason:
        fails.append("%d dismissed card(s) carry no reason: %s"
                     % (len(noreason), ", ".join(noreason)))
    else:
        oks.append("every dismissal carries a written reason")

    # ── the board is ONLY for things to be done (her ruling, 2026-09-15) ──
    # "why do we still have cards with 'your action - nothing'? if it has a
    # reason for real, let me know."  All five such cards had a real task sitting
    # underneath a line that opened by saying there was nothing to do — the card
    # arguing with itself, on a board whose whole premise is that every card is
    # work. An `action` that opens with "Nothing" is therefore always a defect:
    # either it is wrong and the action must be stated, or it is right and the
    # card does not belong on the board (close it, or park it in Waiting-on with
    # a chase date).
    nothing = [it["id"] for it in items
               if it.get("status") == "open"
               and re.match(r"\s*nothing\b", str(it.get("action") or ""), re.I)]
    if nothing:
        fails.append("%d open card(s) have an `action` that opens with \"Nothing\" — state the "
                     "action, or take the card off the board: %s"
                     % (len(nothing), ", ".join(nothing)))
    else:
        oks.append("no open card's action opens with \"Nothing\"")

    # ── she wrote on a card and nobody answered (2026-09-15) ──
    # Her words: "i had already told you this was solved." She had — on
    # 2026-09-14 16:38 she wrote "already sent on Aug 18" on the Andersen
    # JCC/LSWP card, and it sat for a day because the triage detector only read
    # `kind:"done"` updates (the tick-time prompt) and hers was a plain work-log
    # note. The blocker I claimed was that updates carry no author; the answer
    # was to ADD one, which pm_state.add_update now does.
    #
    # The test is mechanical on purpose — no reading of what the note MEANS.
    # A card is flagged when the last thing written on it is hers (or of unknown
    # authorship, which is never assumed to be Claude's) and Claude has not
    # answered since. That is the shape of "she said something and nothing
    # happened", regardless of what she said.
    stale_notes = []
    now = datetime.datetime.now().astimezone()
    for it in items:
        if it.get("status") != "open":
            continue
        ups = [u for u in (it.get("updates") or [])
               if isinstance(u, dict) and u.get("text")]
        if not ups or ups[-1].get("by") == "claude":
            continue
        try:
            at = datetime.datetime.fromisoformat(ups[-1]["at"])
        except (ValueError, KeyError, TypeError):
            continue
        hours = (now - at).total_seconds() / 3600.0
        if hours >= 12:
            stale_notes.append((it["id"], round(hours / 24.0, 1),
                                (ups[-1].get("text") or "")[:56]))
    if stale_notes:
        fails.append("%d open card(s) where SHE wrote the last word and nothing answered "
                     "— read each and either act on it or rule on it:" % len(stale_notes))
        for i, days, txt in sorted(stale_notes, key=lambda r: -r[1]):
            fails.append("    %-46s %4.1f days   %r" % (i, days, txt))
    else:
        oks.append("no card is sitting on an unanswered note of hers")

    ids = [it.get("id") for it in items]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        fails.append("duplicate card ids: %s" % ", ".join(sorted(dupes)))
    else:
        oks.append("card ids are unique")

    return fails, warns, oks


def main():
    try:
        with open(STATE) as fh:
            state = json.load(fh)
    except Exception as exc:
        print("🔴 cannot read %s: %s" % (STATE, exc))
        return 2
    fails, warns, oks = check(state)
    for line in fails:
        print("FAIL  " + line if not line.startswith("    ") else line)
    for line in warns:
        print("WARN  " + line)
    for line in oks:
        print("ok    " + line)
    print("\n%d FAIL · %d WARN · %d ok" % (
        len([f for f in fails if not f.startswith("    ")]), len(warns), len(oks)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
