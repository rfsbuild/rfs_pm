#!/usr/bin/env python3
"""Move dated obligations OUT of the self-wiping `routine` lane.

WHY
    `pm_routine.py` reseeds the routine lane every morning from
    `routine/plan-<day>.json` and deletes any `rt_` card the new day's plan
    does not restate. That is right for "reconcile the bank" and wrong for a
    deadline: three real obligations were seeded into the lane on 2026-08-24
    from that day's plan, and tomorrow's plan file lists neither of them, so
    they were one morning away from silent deletion.

    `rt_petersen_coi` escaped via the briefing recovery (it is a real sweep
    item, now in `action`). These two are not in any briefing, so nothing else
    was going to save them:

      rt_andersen_paperwork_7  — SEVEN Andersen chases, every one blocking a
                                 payment INTO RFS. Oldest asked 2026-08-03.
      rt_jaar_realty_chase     — checks #3775 (June) and #3780 (July), the
                                 $1,490.00 batch, still uncashed.

    The companion fix in `pm_routine.py` makes the lane guard two-part
    (prefix AND lane), so once a card is re-homed the morning wipe leaves it
    alone. Without THAT fix this script would be undone at 08:00.

`claude_done` is filled in from what today's work actually established, because
a card in `action` with an empty claude_done is the thing `pm_ingest.validate`
refuses to accept.

⚠️ It has to go through `pm_ingest`, NOT `patch_content`. `PATCHABLE` is
("subject", "meta", "action", "project", "lane", "kind", "due", "ctx_sum",
"did") — `claude_done` is deliberately absent, because it is collector-owned
rather than a field she edits in the UI. A first version of this script patched
it and the post-condition caught the silent no-op.
"""
import argparse
import datetime
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import pm_state as S   # noqa: E402

# `validate()` reads both subjects as chases ("chases", "follow up") and both
# genuinely ship no message. Stated reasons, per the written escape hatch:
NO_DRAFT = {
    "rt_andersen_paperwork_7":
        "This card's job is submitting DOCUMENTS into Andersen's system — JCC, "
        "LSWP, WOLIs — not sending a message. The covering email for the one "
        "chase that is live ships on its own card, `rba_conlan_orino_paperwork`, "
        "with a ready-to-send draft. ⚠️ The single bundle reply covering all "
        "seven, which the approved plan called for, has NOT been written — "
        "recorded here as still owed rather than quietly counted as done.",
    "rt_jaar_realty_chase":
        "She took this one herself at this morning's reconcile — her words were "
        "that she is following up with Jaar today. Whether that contact has "
        "happened is hers to say, and a drafted email would cut across a "
        "conversation she owns.",
}

MOVES = {
    "rt_andersen_paperwork_7": {
        "lane": "action",
        "claude_done": [
            "Confirmed from Andersen's own 8/21 reply that Conlan Orino is "
            "still outstanding — 'The JCC and LSWP have not been sent over' — "
            "and that it is a THIRD install, distinct from the Meltzer and "
            "Slater pair.",
            "Established Rebecca is on PTO until Aug 31, so Joe Yousef or Alan "
            "Dawson are the live contacts for anything submitted this week.",
            "Confirmed these land on rfscarpentryservices@gmail.com rather than "
            "billing@, which is the likely reason all seven went unworked.",
        ],
    },
    "rt_jaar_realty_chase": {
        "lane": "action",
        "claude_done": [
            "Settled the duplicate question at today's reconcile on her ruling: "
            "the $745.00 is genuinely August's rent and the $1,490.00 batch is "
            "#3775 (June) + #3780 (July). Both are real; there is nothing to "
            "remove from the forecast.",
            "Confirmed the reason for the chase is that Jaar has still not "
            "cashed either check, so the outflow is committed but unlanded.",
        ],
    },
}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    state, _ = S.load_state(S.STATE_PATH)
    plan = []
    for iid, patch in MOVES.items():
        card = S.get_item(state, iid)
        if card is None:
            print("⚠️  %s is not on the board — skipping" % iid)
            continue
        plan.append((iid, card["lane"], patch["lane"]))
        print("  %-26s %s -> %s" % (iid, card["lane"], patch["lane"]))

    if not a.apply:
        print("\n(dry run — pass --apply to write)")
        return 0

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = S.STATE_PATH.with_suffix(".json.bak_%s" % stamp)
    shutil.copy2(S.STATE_PATH, bak)
    print("\n  backup → %s" % bak.name)

    # Collector path, not patch_content: `claude_done` lives here.
    import pm_ingest as I
    items = []
    for iid, patch in MOVES.items():
        if S.get_item(state, iid) is None:
            continue
        it = {"id": iid, "_relane": True}
        it.update(patch)
        if iid in NO_DRAFT:
            it["_no_draft"] = NO_DRAFT[iid]
        items.append(it)

    errs = I.validate(items,
                      existing={i["id"]: i for i in state["items"]})
    if errs:
        print("\n🔴 VALIDATION FAILED — nothing written:")
        for e in errs:
            print("   -", e)
        return 1
    I.ingest(items, path=S.STATE_PATH)

    # ── post-conditions: the point is that TOMORROW deletes neither ──
    import pm_routine as R
    after, _ = S.load_state(S.STATE_PATH)
    fails = []
    for iid, patch in MOVES.items():
        card = S.get_item(after, iid)
        if card is None:
            continue
        if card["lane"] != patch["lane"]:
            fails.append("%s: lane is %r, expected %r"
                         % (iid, card["lane"], patch["lane"]))
        if not card.get("claude_done"):
            fails.append("%s: claude_done did not land" % iid)

    removed = R.seed("2026-08-25", dry_run=True)["removed"]
    still_armed = [i for i in MOVES if i in removed]
    if still_armed:
        fails.append("tomorrow's wipe STILL removes: %s" % still_armed)

    if fails:
        print("\n🔴 POST-CONDITIONS FAILED:")
        for f in fails:
            print("   -", f)
        return 1
    print("  ✅ both re-homed; tomorrow's dry-run removes %s" % (removed or "nothing"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
