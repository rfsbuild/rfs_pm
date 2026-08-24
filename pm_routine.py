#!/usr/bin/env python3
"""Seed the board's `routine` lane from the daily-routine machinery (2026-08-24).

WHY THIS EXISTS RATHER THAN A DAY-ROLL
    The routine is four fixed morning items plus a per-day plan, and it RESETS
    every morning. The PM board holds one-off cards that persist until she
    finishes them. Those are different shapes, and the board's own daily reset
    — pm_state.roll_forward() — has run exactly ONCE, ever: `roll_log` holds a
    single entry and `brief_date` froze at 2026-07-29. Hanging a daily-resetting
    lane off a mechanism that has not fired in 26 days would give her a lane
    that works on day one and is wrong every day after.

    So the seeding runs from the job that already fires at 08:00 and already
    knows the day: scripts/open_daily_routine.sh → routine_plan.py. The lane is
    a PROJECTION of the routine, refreshed each morning, and it is correct
    whether or not the day-roll is ever revived.

WHY IT DOES NOT GO THROUGH pm_ingest
    pm_ingest deliberately refuses to touch HER_FIELDS — status, done_at, did,
    note, assignee, defer — because a COLLECTOR must never undo her work. That
    is exactly right for the sweep and exactly wrong here: a routine card whose
    status is not reset to open each morning is a checklist that can be ticked
    once, ever. This module is not a collector; it OWNS these cards, and the
    reset is the whole point. That ownership is why `routine` is absent from
    the UI's LANE_PICK — a card she drags in by hand would be wiped at 08:00.

WHAT IT PRESERVES ANYWAY
    Her `note` survives the reset. A note left on a morning item is a standing
    remark about that task, not about one Tuesday, and destroying it every
    morning would teach her not to write them.

WHAT IT REMOVES
    Yesterday's plan items that are not in today's plan. The four MORNING items
    are permanent; the day-specific ones are not, and leaving them behind is
    how the lane silently becomes an archive of every ad-hoc item ever planned.

SOURCES — read, never restated here
    ~/rfs_dashboard/routine_morning.json    the four fixed items
    ~/rfs_dashboard/routine/plan-<date>.json  the day's extras (routine_plan.py)

Usage:
    python3 pm_routine.py                 # seed today
    python3 pm_routine.py 2026-08-25      # seed a specific day
    python3 pm_routine.py --dry-run       # report what would change; write nothing
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import pm_state as S  # noqa: E402

DASH = Path.home() / "rfs_dashboard"
MORNING_PATH = DASH / "routine_morning.json"
PLAN_DIR = DASH / "routine"

LANE = "routine"
# Every card this module owns carries this prefix, so "which cards are mine?"
# is answered by the id and not by the lane alone. If a card ever ends up in
# the routine lane without it, it was not seeded here and is left untouched
# rather than deleted — this module cleans up after itself, nothing else.
PREFIX = "rt_"


def _read_json(path):
    """(data, note). A missing file is NORMAL for the plan and fatal for neither."""
    try:
        return json.loads(Path(path).read_text()), None
    except FileNotFoundError:
        return None, "absent"
    except Exception as exc:
        return None, "unreadable: %s" % exc


def morning_items():
    """The four fixed items. An unreadable file is a hard failure, not an empty list.

    Returning [] on a bad read would seed a lane with only the day's extras and
    look like a quiet morning, which is the same silent-empty failure the sweep
    treats as an error. If the file is broken she should hear about it.
    """
    data, note = _read_json(MORNING_PATH)
    if data is None:
        raise RuntimeError("%s is %s — the fixed morning items cannot be read"
                           % (MORNING_PATH, note))
    items = data.get("items") or []
    bad = [i for i in items if not (i.get("id") and i.get("name"))]
    if not items or bad:
        raise RuntimeError("%s has no usable items (%d entries, %d malformed)"
                           % (MORNING_PATH, len(items), len(bad)))
    return items


def plan_items(day):
    """The day's extras. Genuinely optional — no plan file is a normal day."""
    data, _note = _read_json(PLAN_DIR / ("plan-%s.json" % day))
    if not data:
        return []
    out = []
    for i, it in enumerate(data.get("items") or []):
        if not it.get("name"):
            continue
        # Mirror daily_routine.html's rule exactly: a STABLE id from the plan
        # when it has one, the positional fallback only when it does not. The
        # page fixed positional-only ids on 2026-08-24 because a re-sorted list
        # silently moved her "Done" onto a different task; the same list feeds
        # both surfaces, so the same rule has to apply on both.
        out.append({"id": it.get("id") or ("t%d" % i),
                    "name": it["name"], "sub": it.get("sub") or "",
                    "why": it.get("why") or ""})
    return out


def _card(src, kind):
    """One routine item as a board card."""
    sub = (src.get("sub") or "").strip()
    why = (src.get("why") or "").strip()
    return {
        "id": PREFIX + src["id"],
        "subject": src["name"],
        "lane": LANE,
        "source": "routine",
        "kind": kind,
        "action": sub,
        # `why` explains why this landed on TODAY's list, so she can dismiss it
        # when it is wrong. It is the plan's own justification — kept visible
        # rather than dropped, because a checklist item with no reason attached
        # is one she cannot argue with.
        "ctx_happened": why or sub,
        "ctx_matters": "", "ctx_needed": "",
        "hadassa_todo": [sub] if sub else [],
        "claude_done": [],
    }


def seed(day=None, path=None, dry_run=False):
    day = day or datetime.date.today().isoformat()
    path = path or S.STATE_PATH
    state, status = S.load_state(path)
    if state is None:
        return {"ok": False, "reason": "state unreadable (%s)" % status}

    wanted = ([_card(m, "fixed") for m in morning_items()]
              + [_card(p, "plan") for p in plan_items(day)])
    wanted_ids = {c["id"] for c in wanted}

    existing = {it["id"]: it for it in state["items"]
                if it.get("id", "").startswith(PREFIX)}
    # Only cards THIS module minted are ever removed. A routine-lane card
    # without the prefix was put there by something else and is not ours to
    # delete.
    stale = [i for i in existing if i not in wanted_ids]

    res = {"ok": True, "day": day, "added": [], "reset": [], "removed": stale,
           "kept_notes": [], "dry_run": bool(dry_run)}
    for c in wanted:
        (res["reset"] if c["id"] in existing else res["added"]).append(c["id"])
        if (existing.get(c["id"]) or {}).get("note"):
            res["kept_notes"].append(c["id"])
    if dry_run:
        return res

    def _fn(st):
        by_id = {it["id"]: it for it in st["items"]}
        for c in wanted:
            prior = by_id.get(c["id"])
            if prior is None:
                it = S.new_item(c["id"], c["subject"])
                it.update(c)
                it["status"] = "open"
                it["first_seen"] = it["last_seen"] = S._now_iso()
                st["items"].append(it)
                continue
            # Refresh the wording, then RESET the day. Her note survives; the
            # tick does not — that is what makes it a daily checklist.
            prior.update(c)
            prior["status"] = "open"
            prior["last_seen"] = S._now_iso()
            for k in ("done_at", "done_by", "did", "defer", "defer_days",
                      "dismissed_at", "dismiss_reason", "claude_result"):
                prior.pop(k, None)
            prior["is_new"] = False
            prior["age"] = 0
        if stale:
            st["items"] = [it for it in st["items"] if it["id"] not in set(stale)]
        return {"seeded": len(wanted), "removed": len(stale)}

    S._mutate(_fn, path)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="seed the board's routine lane")
    ap.add_argument("day", nargs="?", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change; write nothing")
    ap.add_argument("--state", default=None, help="override state path (tests)")
    a = ap.parse_args(argv)
    try:
        res = seed(day=a.day, path=a.state, dry_run=a.dry_run)
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "reason": str(exc)}, indent=1))
        return 1
    print(json.dumps(res, indent=1, default=str))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
