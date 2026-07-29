#!/usr/bin/env python3
"""Durable ingest for the PM board — the path that was missing.

Until now, loading a briefing into the board meant hand-writing yet another
`apply_sessionN.py`: a one-off script, thrown away, with the field names
retyped from memory every time. Three sessions in a row the board went unloaded
for exactly that reason — "load the board" was quietly a code-writing task.

This replaces that with data. A briefing is a JSON file; ingesting it is one
command:

    python3 pm_ingest.py briefings/2026-07-29.json --dry-run
    python3 pm_ingest.py briefings/2026-07-29.json

Two properties that the one-off scripts did not have:

  · ALL-OR-NOTHING. Every item lands inside a single `_mutate`, so one lock
    acquire, one file write. A validation error on item 14 leaves the board
    exactly as it was — a half-ingested briefing is worse than an unloaded one,
    because she would trust it.
  · HER EDITS SURVIVE. Re-running the same briefing refreshes only
    source-owned fields (the same SOURCE_OWNED set `pm_state.upsert_item`
    uses). Her status, note, did, assignee, defer and age are never touched by
    a collector. That makes ingest idempotent and safe to re-run after fixing a
    typo in the briefing.

Briefing file shape:

    {
      "brief_date": "2026-07-29",          # optional; must match the board
      "items": [ {item}, ... ],
      "sweep": {                           # optional; stamps mark_swept()
        "gmail": {"checked": 50, "detail": "..."},
        "slack": {"checked": 16, "detail": "..."}
      }
    }

Each item needs at least `id` and `subject`; everything else defaults through
`pm_state.new_item`. Unknown keys are a hard error rather than a silent drop,
because a typo'd field name is how "I loaded it" becomes "the card is blank".
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_state as S  # noqa: E402

# Fields a collector owns and may refresh on re-ingest. Mirrors
# pm_state.upsert_item — kept in sync deliberately, asserted in tests.
#
# `due` IS here because a deadline is a fact the source owns: if BuilderTrend
# moves a change-order expiry, the board must say so.
#
# `lane` is deliberately NOT here. Lane is a PRIORITY judgement, and once a card
# exists that judgement is hers — she re-lanes cards, and `patch_content` lets
# the UI do it. A collector that refreshed `lane` would silently undo every
# re-prioritisation on the next ingest, which is the same class of bug as a tile
# that opens something other than what it counted. Lane is set on INSERT only.
# A briefing that genuinely needs to move an existing card must say so per item
# with `"_relane": true` — so moving her card is always a deliberate, visible
# act recorded in the briefing file, never a side effect of re-running it.
SOURCE_OWNED = ("subject", "meta", "ctx_sum", "ctx_body", "action",
                "where", "links", "draft", "pills", "unconfirmed", "moved",
                "source", "kind", "due")

# Fields that are HERS. A briefing that tries to set one is rejected outright
# rather than obeyed: a collector must never be able to mark her work done.
HER_FIELDS = ("status", "done_at", "did", "note", "assignee", "defer",
              "defer_days", "dismiss_reason", "dismissed_at", "followup")


# Control keys a briefing may carry that are not item fields. Anything else
# starting with "_" is rejected, so a typo'd control key is loud, not ignored.
CONTROL_KEYS = ("_note", "_relane")


def validate(items):
    """Return a list of error strings. Empty list = the briefing is loadable."""
    errs, seen = [], set()
    for i, it in enumerate(items):
        where = "item[%d]" % i
        if not isinstance(it, dict):
            errs.append("%s is not an object" % where)
            continue
        iid = it.get("id")
        if not iid or not isinstance(iid, str):
            errs.append("%s has no string `id`" % where)
        elif iid in seen:
            errs.append("%s duplicate id %r" % (where, iid))
        else:
            seen.add(iid)
            where = "item[%d] %s" % (i, iid)
        if not it.get("subject"):
            errs.append("%s has no `subject`" % where)
        for k in it:
            if k in CONTROL_KEYS:
                continue
            if k not in S.ITEM_FIELDS:
                errs.append("%s unknown field %r" % (where, k))
            elif k in HER_FIELDS:
                errs.append("%s sets %r, which is hers — a collector may not "
                            "write it" % (where, k))
        if it.get("_relane") and not it.get("lane"):
            errs.append("%s sets _relane but gives no `lane` to move it to"
                        % where)
        if it.get("lane") and it["lane"] not in S.LANES:
            errs.append("%s lane %r not in %s" % (where, it["lane"], S.LANES))
        if it.get("kind") and not isinstance(it["kind"], str):
            errs.append("%s kind must be a string" % where)
        for k in ("ctx_body", "where", "links", "pills"):
            if k in it and not isinstance(it[k], list):
                errs.append("%s %s must be a list" % (where, k))
        due = it.get("due")
        if due is not None and not (isinstance(due, str) and len(due) == 10):
            errs.append("%s due must be YYYY-MM-DD or null, got %r"
                        % (where, due))
    return errs


def ingest(items, path=S.STATE_PATH, now=None):
    """Upsert every item in ONE locked transaction. Returns a summary dict."""
    errs = validate(items)
    if errs:
        raise ValueError("briefing is invalid, nothing was written:\n  - "
                         + "\n  - ".join(errs))

    added, updated = [], []

    def _fn(state):
        for spec in items:
            existing = S.get_item(state, spec["id"])
            if existing is None:
                it = S.new_item(spec["id"], spec["subject"])
                for k, v in spec.items():
                    if k in S.ITEM_FIELDS:
                        it[k] = v
                it["first_seen"] = S._now_iso(now)
                it["last_seen"] = S._now_iso(now)
                state["items"].append(it)
                added.append(spec["id"])
            else:
                for k in SOURCE_OWNED:
                    if k in spec:
                        existing[k] = spec[k]
                if spec.get("project") and not existing.get("project"):
                    existing["project"] = spec["project"]
                # lane only moves on an explicit, per-item opt-in — see the
                # SOURCE_OWNED comment. Silence here is what protects her
                # re-prioritisations from being undone by a re-run.
                if spec.get("_relane") and spec.get("lane"):
                    existing["lane"] = spec["lane"]
                existing["last_seen"] = S._now_iso(now)
                updated.append(spec["id"])
        return {"added": added, "updated": updated}

    return S._mutate(_fn, path, now)[1]


def load_briefing(fp):
    with open(fp) as f:
        data = json.load(f)
    if isinstance(data, list):
        return {"items": data}
    if not isinstance(data, dict) or "items" not in data:
        raise ValueError("briefing must be a list of items, or an object with "
                         "an `items` key")
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("briefing", help="path to the briefing JSON")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and report what would change; write nothing")
    ap.add_argument("--state", default=None, help="override state path (tests)")
    args = ap.parse_args(argv)

    path = args.state or S.STATE_PATH
    data = load_briefing(args.briefing)
    items = data["items"]

    errs = validate(items)
    if errs:
        print("❌ briefing is INVALID — nothing written:")
        for e in errs:
            print("   -", e)
        return 2

    state, _ = S.load_state(path)
    if data.get("brief_date") and data["brief_date"] != state.get("brief_date"):
        print("❌ brief_date mismatch: briefing says %s, board is on %s.\n"
              "   Roll the board first (pm_state.roll_forward) or fix the "
              "briefing — loading yesterday's items onto today's board is how "
              "a stale card looks current."
              % (data["brief_date"], state.get("brief_date")))
        return 2

    known = {it["id"] for it in state["items"]}
    would_add = [it["id"] for it in items if it["id"] not in known]
    would_update = [it["id"] for it in items if it["id"] in known]

    if args.dry_run:
        print("✅ briefing VALID — %d items (%d new, %d refresh)"
              % (len(items), len(would_add), len(would_update)))
        for iid in would_add:
            print("   + ", iid)
        for iid in would_update:
            print("   ~ ", iid, "(source fields only; her edits kept)")
        print("\nDRY RUN — nothing written.")
        return 0

    res = ingest(items, path=path)
    print("✅ ingested %d items: %d added, %d refreshed"
          % (len(items), len(res["added"]), len(res["updated"])))

    if data.get("sweep"):
        try:
            ev = S.mark_swept(data["sweep"], path=path)
            print("✅ sweep stamped:", json.dumps(ev.get("sources", ev)
                                                 if isinstance(ev, dict) else ev))
        except Exception as e:
            print("⚠️  items landed, but the sweep stamp was REFUSED: %s" % e)
            print("   The board is loaded; it is just not marked swept.")
            return 1

    state, _ = S.load_state(path)
    c = S.counts(state)
    print("board now: %s · %d items · open %d · done %d"
          % (state["brief_date"], c["total"], c["open"], c["done"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
