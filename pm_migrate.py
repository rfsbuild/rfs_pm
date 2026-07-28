#!/usr/bin/env python3
"""One-time migration: HTML briefing + Chrome localStorage -> pm_state.json.

Hadassa's 2026-07-28 work lives in two places that are about to stop being the
source of truth:

  1. the ITEMS array baked into `briefing-2026-07-28.html` (the card content), and
  2. the `pmbrief_state_2026-07-28` key in Chrome's localStorage (HER work —
     done/deferred/assigned marks and her typed notes).

Losing (2) would be losing a day. This script merges them into pm_state.json so
the standalone app opens on exactly the board she left.

  python3 pm_migrate.py --items <items.json> [--dry-run]

`--items` is a JSON dump of `{items: ITEMS, projects: PROJECTS}` pulled out of
the live page (the ITEMS array is a JS object literal, not JSON, so it is read
by evaluating it in a browser rather than regex-parsing the file).

localStorage is read straight off Chrome's LevelDB — proven 2026-07-28. Chrome
does NOT need to be closed. If the key can't be found the script REFUSES to
write rather than silently producing a board with her day's work missing.
"""
import argparse
import json
import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_state  # noqa: E402
import chrome_localstorage  # noqa: E402

def read_localstorage(brief_date):
    """Her clicks, straight out of Chrome's LevelDB. See chrome_localstorage.py
    for why this needs a real SST reader and not a byte scan."""
    return chrome_localstorage.read_key("pmbrief_state_" + brief_date)


def to_item(src, clicks):
    """One briefing card + her clicks on it -> one pm_state item."""
    s = clicks.get(src["id"], {}) if clicks else {}
    return pm_state.new_item(
        id=src["id"],
        subject=src.get("subject", ""),
        source=src.get("source", "email"),
        # her inline project rename wins over the generator's guess
        project=s.get("project") or src.get("project"),
        lane=src.get("lane", "action"),
        kind=src.get("kind", "action"),
        meta=src.get("meta", ""),
        ctx_sum=src.get("ctxSum", ""),
        ctx_body=src.get("ctxBody", []) or [],
        action=src.get("action", ""),
        where=src.get("where", []) or [],
        links=src.get("links", []) or [],
        draft=src.get("draft"),
        pills=src.get("pills", []) or [],
        unconfirmed=bool(src.get("unconfirmed")),
        is_new=bool(src.get("isNew")),
        moved=src.get("moved"),
        age=int(src.get("age") or 0),
        status="done" if s.get("done") else "open",
        # done_at came from JS toISOString() = bare UTC. Convert to local+offset
        # so it means what it says (the 4-hour bug caught 2026-07-28).
        done_at=_localise(s.get("done_at")) if s.get("done") else None,
        defer=s.get("defer"),
        defer_days=int(s.get("deferDays") or 0),
        assignee=None if s.get("assignee") in (None, "hadassa") else s.get("assignee"),
        note=(s.get("note") or None),
        followup=s.get("followup"),
    )


def _localise(iso_z):
    dt = pm_state.parse_dt(iso_z)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone().isoformat(timespec="seconds")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", required=True, help="JSON with {items:[...], projects:[...]}")
    ap.add_argument("--brief-date", default="2026-07-28")
    ap.add_argument("--out", default=str(pm_state.STATE_PATH))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-missing-clicks", action="store_true",
                    help="proceed even if localStorage can't be read (loses her edits)")
    args = ap.parse_args()

    payload = json.load(open(args.items))
    src_items = payload["items"]

    clicks = read_localstorage(args.brief_date)
    if clicks is None and not args.allow_missing_clicks:
        print("REFUSING: could not read pmbrief_state_%s from Chrome's LevelDB.\n"
              "Her done/defer/assign/note edits would be silently lost. Pass\n"
              "--allow-missing-clicks only if you accept that." % args.brief_date,
              file=sys.stderr)
        return 2
    clicks = clicks or {}

    state = pm_state.blank_state(brief_date=args.brief_date)
    now = pm_state._now_iso()
    for src in src_items:
        it = to_item(src, clicks)
        it["first_seen"] = now
        it["last_seen"] = now
        state["items"].append(it)

    # Every click must land. A card she acted on that didn't make it across is
    # a silent data loss, so assert rather than trust the join.
    click_ids = set(clicks)
    item_ids = {i["id"] for i in state["items"]}
    orphans = sorted(click_ids - item_ids)

    c = pm_state.counts(pm_state.normalize(state))
    exp_done = sum(1 for v in clicks.values() if v.get("done"))
    got_done = sum(1 for i in state["items"] if i["status"] == "done")
    exp_def = sum(1 for v in clicks.values() if v.get("defer"))
    got_def = sum(1 for i in state["items"] if i["defer"])
    exp_asg = sum(1 for v in clicks.values()
                  if v.get("assignee") and v["assignee"] != "hadassa")
    got_asg = sum(1 for i in state["items"] if i["assignee"])
    exp_note = sum(1 for v in clicks.values() if v.get("note"))
    got_note = sum(1 for i in state["items"] if i["note"])

    print("items      : %d" % len(state["items"]))
    print("done       : %d (localStorage said %d)" % (got_done, exp_done))
    print("deferred   : %d (localStorage said %d)" % (got_def, exp_def))
    print("assigned   : %d (localStorage said %d)" % (got_asg, exp_asg))
    print("notes      : %d (localStorage said %d)" % (got_note, exp_note))
    print("actionable : %d  ·  %d%% complete" % (c["actionable"], c["pct"]))
    if orphans:
        print("ORPHAN CLICKS (in localStorage, no matching card): %s" % orphans)

    bad = [m for m, (a, b) in {
        "done": (got_done, exp_done), "defer": (got_def, exp_def),
        "assignee": (got_asg, exp_asg), "note": (got_note, exp_note),
    }.items() if a != b]
    if bad or orphans:
        print("MISMATCH in %s — refusing to write." % (bad or "orphans"), file=sys.stderr)
        return 3

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    pm_state.save_state(state, args.out)
    print("\nwrote %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
