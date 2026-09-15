#!/usr/bin/env python3
"""Turn the audit's OWNER chips into real assignees on the board.

Her instruction, 2026-09-15: "You can assign the other 50 considering everything
you know that each of us do, and if the case when I'm rereading them all I'll
reassign, no problem."

WHY THIS IS A SEPARATE STEP FROM THE OVERLAY. apply_audit_overlay.py deliberately
touches no card state — it only puts evidence where the decision gets made. An
owner chip is a recommendation; `assignee` is the board actually routing work to
a person, and it changes what the By-person view and the tiles say. So it waited
for her word, and it is its own reversible script.

The names come from the audit's per-card OWNER map, which was built from who
actually does what: Rafael runs the client/vendor relationships and the RBA
paperwork, Guilherme runs the crews and what happens on site.

`claude` assignments also stamp claude_queued_at, matching what apply_click does
when she picks "claude" in the dropdown — otherwise the card sits in the queue
with no clock and the board cannot say how long it has waited.

    python3 scripts/apply_audit_owners.py --dry-run
    python3 scripts/apply_audit_owners.py
    python3 scripts/apply_audit_owners.py --unassign      # full undo
"""
import argparse
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import pm_state as S                                            # noqa: E402

# the board's own vocabulary — the assignee dropdown's values are lowercase
NAME = {"Rafael": "rafael", "Guilherme": "guilherme", "Claude": "claude"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--unassign", action="store_true",
                    help="clear assignee on every card this script set")
    ap.add_argument("--state", default=None)
    a = ap.parse_args()
    path = a.state or S.STATE_PATH

    with open(path) as fh:
        state = json.load(fh)

    plan, skipped, already = [], [], []
    for it in state["items"]:
        owner = (it.get("audit") or {}).get("owner")
        if not owner or it.get("status") != "open":
            continue
        who = NAME.get(owner)
        if not who:
            skipped.append((it["id"], owner))
            continue
        cur = it.get("assignee")
        if a.unassign:
            if cur == who:
                plan.append((it["id"], None, cur))
            continue
        if cur == who:
            already.append(it["id"])
            continue
        # Never overwrite an assignment SHE made. The audit is a recommendation;
        # a name already on the card is a decision, and a recommendation must
        # not quietly overrule one.
        if cur:
            skipped.append((it["id"], "already assigned to %s — left alone" % cur))
            continue
        plan.append((it["id"], who, cur))

    verb = "unassign" if a.unassign else "assign"
    print("cards to %s: %d" % (verb, len(plan)))
    print("  " + ", ".join("%s %d" % (k, v) for k, v in
                           sorted(collections.Counter(w for _, w, _ in plan if w).items())))
    if already:
        print("already correct, untouched: %d" % len(already))
    if skipped:
        print("skipped: %d" % len(skipped))
        for i, why in skipped:
            print("    %-46s %s" % (i, why))
    if a.dry_run:
        print("\n(dry run — nothing written)")
        return 0

    def _fn(st):
        n = 0
        for iid, who, _prev in plan:
            it = S.get_item(st, iid)
            if it is None:
                continue
            it["assignee"] = who
            # mirror apply_click: the queue clock starts when the work is handed over
            if who == "claude" and not it.get("claude_queued_at"):
                it["claude_queued_at"] = S._now_iso()
            elif who != "claude":
                it["claude_queued_at"] = None
            n += 1
        return {"changed": n}

    res = S._mutate(_fn, path)[1]
    print("\n✅ %sed %d card(s)" % (verb, res["changed"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
