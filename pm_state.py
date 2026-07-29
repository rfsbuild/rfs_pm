#!/usr/bin/env python3
"""PM state — the data layer for pm_state.json (schema v1).

Pure module: NO Streamlit imports, stdlib only. pm_app.py wraps it for the UI;
a future ingest daemon (Phase 2) imports the same module — one source of truth.

WHY THIS EXISTS
    The HTML briefing kept Hadassa's clicks in browser localStorage and relied
    on her pressing an Export button so the next morning could carry them
    forward. On 2026-07-28 the button was never pressed and the read-back came
    back RED — a whole day of edits had to be reconstructed from source data.
    A manual save step in a daily-use tool fails eventually, so state moves
    server-side: every click persists immediately, there is no Export.

SCHEMA v1
  _schema      : 1
  brief_date   : "YYYY-MM-DD" — the day this state represents
  created_at   : ISO8601 **with local offset** (never bare UTC — see below)
  updated_at   : ISO8601 with local offset
  items[]      : see ITEM_FIELDS
  roll_log[]   : append-only {at, from_date, to_date, carried, dropped, escalated}

TIMESTAMP RULE
    Every timestamp is written with `_now_iso()` = local time WITH offset.
    The HTML pilot used JS `toISOString()`, which is bare UTC; on 2026-07-28
    that made her 10:08 completion read as 14:08 and shifted every "when does
    she work" inference by four hours. Offsets are mandatory here.

CONCURRENCY
    Streamlit reruns on every widget interaction and she may have two tabs
    open. All mutations go through `_mutate()`, which holds an exclusive
    flock for the whole read-modify-write, so a click can never clobber a
    click made a moment earlier in another tab.
"""
import datetime
import fcntl
import json
import re
import os
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "pm_state.json"
LOCK_PATH = ROOT / ".pm_state.lock"
# Finished work is archived here per day at the roll — see roll_forward().
HISTORY_DIR = ROOT / "history"

SCHEMA = 1

# Lanes, in display order. `claude` = she delegated it to me for the next session.
LANES = ("urgent", "action", "week", "rafael", "guilherme", "claude", "noise")
LANE_LABELS = {
    "urgent": "🔴 Urgent",
    "action": "🟠 Action Today",
    "week": "🟡 This Week",
    "rafael": "Pending Rafael",
    "guilherme": "Pending Gui",
    "claude": "🤖 Claude",
    "noise": "Noise / FYI",
}
# Lanes that represent work she is personally on the hook for today.
ACTIONABLE_LANES = ("urgent", "action", "week", "rafael", "guilherme", "claude")

DEFER_REASONS = {
    "no-time": "⏰ No time today",
    "waiting": "👤 Waiting for someone",
    "later-week": "📅 Later this week",
    "needs-rafael": "⚪ Needs Rafael",
}

ASSIGNEES = ("hadassa", "rafael", "guilherme", "claude", "alice")

ITEM_FIELDS = (
    "id", "source", "project", "lane", "kind", "subject", "meta",
    "ctx_sum", "ctx_body", "action", "where", "links", "draft", "pills",
    "unconfirmed", "is_new", "moved", "age", "due",
    "status", "done_at", "defer", "defer_days", "assignee", "note", "followup",
    "dismiss_reason", "dismissed_at",
    "first_seen", "last_seen",
    # `did` = what SHE actually did about this, in her voice. The daily report
    # is a record of her actions, not a status board, so a card with no `did`
    # has nothing to report even when it is done. Set 2026-07-28.
    "did",
)

# status values. `dismissed` = "this task isn't needed" — it is NOT the same as
# done, and it always carries a written reason (Hadassa 2026-07-28: "whenever I
# click it I need to tell why it's not so it makes sense and it's recorded").
# Keeping it distinct from done matters: the daily report should be able to say
# "3 items were judged unnecessary, and here is why" rather than quietly
# inflating the completion count.
STATUSES = ("open", "done", "dismissed")

# Fields the UI may edit in place. Deliberately excludes status/defer/assignee —
# those go through apply_click so defer_days accounting stays correct.
PATCHABLE = ("subject", "meta", "action", "project", "lane", "kind", "due",
             "ctx_sum", "did")


# ── time ──

def _now_iso(now=None):
    """Local time WITH offset. Never bare UTC — see TIMESTAMP RULE above."""
    return (now or datetime.datetime.now().astimezone()).isoformat(timespec="seconds")


def _today(now=None):
    return (now or datetime.datetime.now().astimezone()).date().isoformat()


def parse_dt(s):
    """Tolerant ISO parse. Accepts a trailing 'Z' (py3.9's fromisoformat won't)."""
    if not s:
        return None
    try:
        s2 = s[:-1] + "+00:00" if isinstance(s, str) and s.endswith("Z") else s
        return datetime.datetime.fromisoformat(s2)
    except Exception:
        return None


# ── persistence ──

def _atomic_write_json(path, data):
    path = Path(path)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=1, ensure_ascii=False, default=str)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            shutil.copy2(str(path), str(path) + ".bak")
        os.replace(tmp_name, str(path))
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def blank_state(brief_date=None, now=None):
    return {
        "_schema": SCHEMA,
        "brief_date": brief_date or _today(now),
        "created_at": _now_iso(now),
        "updated_at": _now_iso(now),
        "items": [],
        "roll_log": [],
    }


def load_state(path=STATE_PATH):
    """Return (state, status). status ∈ 'ok' | 'recovered_backup' | 'missing' | 'unreadable'.

    NEVER fabricates an empty state over an existing-but-corrupt file — that
    would silently destroy her day's work. The caller decides.
    """
    path = Path(path)
    if not path.exists():
        return blank_state(), "missing"
    try:
        with open(path) as f:
            return normalize(json.load(f)), "ok"
    except Exception:
        bak = Path(str(path) + ".bak")
        if bak.exists():
            try:
                with open(bak) as f:
                    return normalize(json.load(f)), "recovered_backup"
            except Exception:
                pass
        return None, "unreadable"


def save_state(state, path=STATE_PATH, now=None):
    state["updated_at"] = _now_iso(now)
    _atomic_write_json(path, state)
    return state


def _mutate(fn, path=STATE_PATH, now=None):
    """Read-modify-write under an exclusive lock. `fn(state)` mutates in place.

    The lock spans the whole cycle, so two Streamlit tabs clicking at the same
    moment serialise instead of one overwriting the other's item.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state, status = load_state(path)
            if status == "unreadable":
                raise RuntimeError(
                    "pm_state.json is unreadable and no usable .bak exists — "
                    "refusing to overwrite. Inspect the file by hand."
                )
            result = fn(state)
            save_state(state, path, now=now)
            return state, result
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


# ── items ──

def new_item(id, subject, **kw):
    it = {
        "id": id, "source": "manual", "project": None, "lane": "action",
        "kind": "action", "subject": subject, "meta": "",
        "ctx_sum": "", "ctx_body": [], "action": "", "where": [], "links": [],
        "draft": None, "pills": [], "unconfirmed": False, "is_new": True,
        "moved": None, "age": 0, "due": None,
        "status": "open", "done_at": None, "defer": None, "defer_days": 0,
        "assignee": None, "note": None, "followup": None,
        "dismiss_reason": None, "dismissed_at": None,
        "first_seen": None, "last_seen": None,
    }
    for k, v in kw.items():
        if k in ITEM_FIELDS:
            it[k] = v
    return it


def normalize(state):
    """Fill defaults so an older/partial file can't crash the UI."""
    state.setdefault("_schema", SCHEMA)
    state.setdefault("items", [])
    state.setdefault("roll_log", [])
    state.setdefault("brief_date", _today())
    ref = new_item("", "")
    for it in state["items"]:
        for k, v in ref.items():
            it.setdefault(k, v)
        # legacy/rogue values -> safe defaults
        if it.get("lane") not in LANES:
            it["lane"] = "action"
        if it.get("status") not in STATUSES:
            it["status"] = "open"
        try:
            it["age"] = int(it.get("age") or 0)
        except (TypeError, ValueError):
            it["age"] = 0
        try:
            it["defer_days"] = int(it.get("defer_days") or 0)
        except (TypeError, ValueError):
            it["defer_days"] = 0
    return state


def get_item(state, item_id):
    for it in state["items"]:
        if it["id"] == item_id:
            return it
    return None


def effective_lane(it):
    """Lane after assignment/defer overrides — mirrors the HTML pilot's render().

    Order matters: a 3+-day-open item escalates to urgent even if it was
    assigned away, because that is exactly the case that goes stale silently.
    """
    lane = it.get("lane") or "action"
    asg = it.get("assignee")
    if asg == "rafael":
        lane = "rafael"
    elif asg == "guilherme":
        lane = "guilherme"
    elif asg == "claude":
        lane = "claude"
    if it.get("defer") == "needs-rafael":
        lane = "rafael"
    elif it.get("defer") == "later-week" and lane not in ("rafael", "guilherme", "claude"):
        lane = "week"
    if int(it.get("defer_days") or 0) >= 3:
        lane = "urgent"
    return lane


def sink_rank(it):
    """Open work first, then deferred, then done.

    Hadassa 2026-07-28: "completed (or deferred) tasks go down the page to let
    only the incomplete ones on the top." The top of the page is the scarce
    resource; finished work keeps the record but loses the attention slot.
    """
    if it.get("status") == "dismissed":
        return 3
    if it.get("status") == "done":
        return 2
    if it.get("defer"):
        return 1
    return 0


_KIND_RANK = {"compliance": 0, "pay": 1, "overdue": 1, "action": 2}


def ordered_items(state, lane=None):
    """Display order: sink rank, then kind priority, then age (oldest first)."""
    items = [it for it in state["items"] if lane is None or effective_lane(it) == lane]
    return sorted(
        items,
        key=lambda it: (
            sink_rank(it),
            _KIND_RANK.get(it.get("kind"), 3),
            -int(it.get("age") or 0),
            it.get("subject") or "",
        ),
    )


def do_today(state, limit=5):
    """The short commitment list at the top of the page.

    Capped deliberately. On 2026-07-28 she was shown 32 cards and cleared 12;
    20 were never touched. A page that commits to five is a page she can finish.
    Only OPEN items appear — done work has no claim on the top of the page.
    """
    live = [
        it for it in state["items"]
        if it.get("status") == "open" and effective_lane(it) in ("urgent", "action")
    ]
    live.sort(key=lambda it: (
        0 if effective_lane(it) == "urgent" else 1,
        _KIND_RANK.get(it.get("kind"), 3),
        -int(it.get("age") or 0),
    ))
    return live[:limit]


def counts(state):
    per_lane = {ln: 0 for ln in LANES}
    for it in state["items"]:
        per_lane[effective_lane(it)] = per_lane.get(effective_lane(it), 0) + 1
    actionable = [it for it in state["items"] if effective_lane(it) in ACTIONABLE_LANES]
    done = [it for it in actionable if it.get("status") == "done"]
    dismissed = [it for it in actionable if it.get("status") == "dismissed"]
    # A dismissed item is neither done nor outstanding, so it leaves the
    # denominator entirely — otherwise the day's progress bar would be gamed by
    # dismissing work rather than doing it.
    denom = len(actionable) - len(dismissed)
    return {
        "per_lane": per_lane,
        "total": len(state["items"]),
        "actionable": len(actionable),
        "done": len(done),
        "dismissed": len(dismissed),
        "open": denom - len(done),
        "pct": round(len(done) / denom * 100) if denom else 0,
    }


# ── mutations (each persists immediately — there is no Export button) ──

def set_done(item_id, done, path=STATE_PATH, now=None):
    def _fn(state):
        it = get_item(state, item_id)
        if it is None:
            return False
        it["status"] = "done" if done else "open"
        it["done_at"] = _now_iso(now) if done else None
        return True
    return _mutate(_fn, path, now)[1]


def set_defer(item_id, reason, path=STATE_PATH, now=None):
    """reason=None clears the defer. Each *distinct* defer bumps defer_days,
    which is what drives the 3-day escalation to urgent."""
    def _fn(state):
        it = get_item(state, item_id)
        if it is None:
            return False
        if reason is None:
            it["defer"], it["defer_days"] = None, 0
        else:
            it["defer"] = reason
            it["defer_days"] = int(it.get("defer_days") or 0) + 1
        return True
    return _mutate(_fn, path, now)[1]


def set_assignee(item_id, who, path=STATE_PATH, now=None):
    def _fn(state):
        it = get_item(state, item_id)
        if it is None:
            return False
        it["assignee"] = None if who in (None, "hadassa") else who
        return True
    return _mutate(_fn, path, now)[1]


def set_note(item_id, text, path=STATE_PATH, now=None):
    """Her notes are the single highest-value data in this system — on
    2026-07-28, three of eight corrected facts the briefing had wrong."""
    def _fn(state):
        it = get_item(state, item_id)
        if it is None:
            return False
        it["note"] = (text or "").strip() or None
        return True
    return _mutate(_fn, path, now)[1]


def set_project(item_id, project, path=STATE_PATH, now=None):
    def _fn(state):
        it = get_item(state, item_id)
        if it is None:
            return False
        it["project"] = (project or "").strip() or None
        return True
    return _mutate(_fn, path, now)[1]


def set_followup(item_id, text, when, path=STATE_PATH, now=None):
    def _fn(state):
        it = get_item(state, item_id)
        if it is None:
            return False
        it["followup"] = {"text": text, "when": when, "set_at": _now_iso(now)} if text else None
        return True
    return _mutate(_fn, path, now)[1]


def upsert_item(item, path=STATE_PATH, now=None):
    """Add, or refresh a still-live item without destroying her edits.

    Only source-owned fields are refreshed. status/defer/assignee/note/age are
    HERS and are never overwritten by a collector.
    """
    SOURCE_OWNED = ("subject", "meta", "ctx_sum", "ctx_body", "action",
                    "where", "links", "draft", "pills", "unconfirmed", "moved",
                    "source", "kind")

    def _fn(state):
        existing = get_item(state, item["id"])
        if existing is None:
            it = dict(item)
            it["first_seen"] = it.get("first_seen") or _now_iso(now)
            it["last_seen"] = _now_iso(now)
            state["items"].append(it)
            return "added"
        for k in SOURCE_OWNED:
            if k in item:
                existing[k] = item[k]
        if item.get("project") and not existing.get("project"):
            existing["project"] = item["project"]
        existing["last_seen"] = _now_iso(now)
        return "updated"
    return _mutate(_fn, path, now)[1]


def slug_id(text, prefix="new"):
    base = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:40] or "item"
    return "%s_%s" % (prefix, base)


def patch_content(item_id, patch, path=STATE_PATH, now=None):
    """Inline edits from the UI (subject, action, project, due, lane…)."""
    def _fn(state):
        it = get_item(state, item_id)
        if it is None:
            return False
        for k in PATCHABLE:
            if k in patch:
                v = patch[k]
                if isinstance(v, str):
                    v = v.strip() or None
                if k == "lane" and v not in LANES:
                    continue
                it[k] = v
        return True
    return _mutate(_fn, path, now)[1]


def apply_click(item_id, payload, path=STATE_PATH, now=None):
    """Apply a whole UI interaction in ONE locked write.

    The UI sends the item's full click-state on every change. Doing that as
    five separate set_* calls would take five locks and leave four windows
    where the file is half-updated; one call keeps a click atomic.

    `defer_days` only increments when the defer reason actually CHANGES —
    re-sending the same reason (a re-render, a double click) must not inflate
    the counter, because that counter drives the 3-day escalation to urgent.
    """
    def _fn(state):
        it = get_item(state, item_id)
        if it is None:
            return None
        # "Not needed" — always carries a written reason. The UI must collect it;
        # the server refuses the transition without one so a dismissal can never
        # end up in the daily report as an unexplained disappearance.
        if "dismiss" in payload:
            if payload["dismiss"]:
                reason = (payload.get("dismiss_reason") or "").strip()
                if not reason:
                    return {"error": "a reason is required to mark something not needed"}
                it["status"] = "dismissed"
                it["dismiss_reason"] = reason
                it["dismissed_at"] = _now_iso(now)
                it["done_at"] = None
            else:
                it["status"] = "open"
                it["dismiss_reason"] = None
                it["dismissed_at"] = None
        if "done" in payload:
            want = bool(payload["done"])
            if want != (it.get("status") == "done"):
                it["status"] = "done" if want else "open"
                it["done_at"] = _now_iso(now) if want else None
                if want:   # completing something clears a prior dismissal
                    it["dismiss_reason"] = None
                    it["dismissed_at"] = None
        if "assignee" in payload:
            who = payload["assignee"]
            it["assignee"] = None if who in (None, "", "hadassa") else who
        if "defer" in payload:
            new = payload["defer"] or None
            if new != it.get("defer"):
                it["defer"] = new
                it["defer_days"] = (int(it.get("defer_days") or 0) + 1) if new else 0
        if "note" in payload:
            it["note"] = (payload["note"] or "").strip() or None
        if "project" in payload and payload["project"] is not None:
            it["project"] = (payload["project"] or "").strip() or None
        if "followup" in payload:
            it["followup"] = payload["followup"] or None
        if "due" in payload:
            it["due"] = payload["due"] or None
        return {
            "ok": True, "id": item_id,
            "done_at": it.get("done_at"),
            "defer_days": it.get("defer_days"),
            "lane": effective_lane(it),
        }
    return _mutate(_fn, path, now)[1]


def remove_item(item_id, path=STATE_PATH, now=None):
    def _fn(state):
        before = len(state["items"])
        state["items"] = [it for it in state["items"] if it["id"] != item_id]
        return before != len(state["items"])
    return _mutate(_fn, path, now)[1]


# ── carry-forward (ported from scripts/pm_brief_ingest.py) ──

def roll_forward(to_date=None, path=STATE_PATH, now=None):
    """Roll the board into a new day.

    The rules are lifted verbatim from `scripts/pm_brief_ingest.py::carry_forward`,
    which was correct and tested — what changes is the mechanism: a state
    transition instead of a browser export handed through the filesystem.

      · finished items (done + dismissed) are ARCHIVED to history/, then leave
      · every surviving item ages +1
      · defer_days >= 3 escalates the item to the urgent lane
      · is_new clears — nothing carried is "new overnight"

    Idempotent per day: calling it twice for the same brief_date is a no-op,
    so a double app restart can't silently age everything twice.

    THE ARCHIVE IS NOT OPTIONAL. The first version dropped completed items and
    kept only a COUNT in the roll log, so the moment she pressed "Start new
    day" the subject, her `note` and her `did` — the whole record of what she
    actually did — were destroyed. That record is the raw material for the
    daily report AND for the weekly per-project customer report, so it has to
    outlive the board. Archived first, inside the same lock, before the state
    that references it is rewritten.
    """
    def _fn(state):
        target = to_date or _today(now)
        if state.get("brief_date") == target:
            return {"skipped": "already rolled", "brief_date": target}
        from_date = state.get("brief_date") or _today(now)
        carried, dropped, escalated, dismissed, finished = [], 0, [], [], []
        for it in state["items"]:
            if it.get("status") == "dismissed":
                dismissed.append({"id": it["id"], "subject": it.get("subject"),
                                  "reason": it.get("dismiss_reason")})
                finished.append(it)
                continue
            if it.get("status") == "done":
                dropped += 1
                finished.append(it)
                continue
            nit = dict(it)
            nit["age"] = int(nit.get("age") or 0) + 1
            nit["is_new"] = False
            if int(nit.get("defer_days") or 0) >= 3:
                nit["lane"] = "urgent"
                nit["moved"] = "3+ days open"
                escalated.append(nit["id"])
            carried.append(nit)
        archived_to = archive_finished(from_date, finished) if finished else None
        entry = {
            "at": _now_iso(now),
            "from_date": from_date,
            "to_date": target,
            "carried": len(carried),
            "dropped": dropped,
            "escalated": escalated,
            "dismissed": dismissed,
            "archived": len(finished),
            "archive_file": archived_to,
        }
        state["items"] = carried
        state["brief_date"] = target
        state["roll_log"].append(entry)
        return entry
    return _mutate(_fn, path, now)[1]


# ── history (the permanent record of finished work, per day) ──

def archive_finished(day, items, root=None):
    """Append finished items to history/<day>.json, merging by id.

    Returns the path written. Merge-by-id rather than overwrite so a re-run can
    never truncate a day that already has records.
    """
    hdir = Path(root or HISTORY_DIR)
    hdir.mkdir(parents=True, exist_ok=True)
    fp = hdir / ("%s.json" % day)
    existing = {}
    if fp.exists():
        try:
            with open(fp) as f:
                for rec in json.load(f).get("items", []):
                    existing[rec.get("id")] = rec
        except Exception:
            pass          # a corrupt archive must not block the roll
    for it in items:
        existing[it.get("id")] = dict(it)
    _atomic_write_json(fp, {"date": day, "archived_at": _now_iso(),
                            "items": list(existing.values())})
    return str(fp)


def load_history(day, root=None):
    """Finished items for one day. [] when that day has no archive."""
    fp = Path(root or HISTORY_DIR) / ("%s.json" % day)
    if not fp.exists():
        return []
    try:
        with open(fp) as f:
            return json.load(f).get("items", [])
    except Exception:
        return []


def history_between(start, end, root=None):
    """Finished items with start <= day <= end, each tagged with its `day`.

    This is what a weekly per-project customer report reads: the work that was
    actually completed in a date range, grouped however the caller wants.
    """
    hdir = Path(root or HISTORY_DIR)
    if not hdir.exists():
        return []
    out = []
    for fp in sorted(hdir.glob("*.json")):
        day = fp.stem
        if start <= day <= end:
            for rec in load_history(day, root=hdir):
                rec = dict(rec)
                rec["day"] = day
                out.append(rec)
    return out


# Both must carry evidence before the board counts as swept. Slack alone is
# not a sweep: on 2026-07-28 a thorough Slack pass plus a cursory ten-thread
# Gmail glance was stamped as "gmail, slack" and six items went out wrong.
REQUIRED_SWEEP_SOURCES = ("gmail", "slack")


def mark_swept(evidence, path=STATE_PATH, now=None):
    """Record a sweep — but only against EVIDENCE, never a bare assertion.

    `evidence` is {source: {"checked": int, "detail": str}}. Every source in
    REQUIRED_SWEEP_SOURCES must be present with a non-zero `checked` count and
    a non-empty `detail` saying what was actually queried, or this raises.

    Why it is shaped this way: the first version took a `sources` tuple and
    trusted the caller to be honest. Three hours later the caller set the field
    by direct assignment, claimed both sources, and the gate passed on a sweep
    that had only covered Slack. A precondition the caller self-asserts is
    decoration. Making the argument expensive to fake — you have to name what
    you queried and how much came back — is the point.
    """
    if not isinstance(evidence, dict):
        raise ValueError("mark_swept needs an evidence dict, not %r" % type(evidence))
    missing = []
    for src in REQUIRED_SWEEP_SOURCES:
        ev = evidence.get(src) or {}
        if not ev.get("checked") or not (ev.get("detail") or "").strip():
            missing.append(src)
    if missing:
        raise ValueError(
            "cannot mark the board swept — no evidence for: %s. "
            "Each source needs {'checked': <non-zero>, 'detail': '<what you queried>'}."
            % ", ".join(missing))

    def _fn(state):
        state["last_swept_at"] = _now_iso(now)
        state["last_swept_sources"] = sorted(evidence)
        state["last_swept_evidence"] = evidence
        return state["last_swept_at"]
    return _mutate(_fn, path, now)[1]


def swept_today(state, now=None):
    """(is_fresh, human_reason) — the gate the report generator enforces."""
    stamp = state.get("last_swept_at")
    if not stamp:
        return False, "the board has never been swept against Gmail + Slack"
    if stamp[:10] != _today(now):
        return False, "the board was last swept on %s, not today" % stamp[:10]
    ev = state.get("last_swept_evidence") or {}
    thin = [s for s in REQUIRED_SWEEP_SOURCES
            if not (ev.get(s) or {}).get("checked")]
    if thin:
        return False, ("the board carries no evidence of a %s sweep — a stamp "
                       "alone is not a sweep" % " or ".join(thin))
    return True, "board swept %s (%s)" % (
        stamp[11:16],
        "; ".join("%s: %s" % (s, (ev[s].get("detail") or "").strip())
                  for s in sorted(ev)))


def health(path=STATE_PATH):
    """Cheap status for the header + the launchd smoke test."""
    state, status = load_state(path)
    if state is None:
        return {"ok": False, "status": status}
    c = counts(state)
    return {
        "ok": True,
        "status": status,
        "schema": state.get("_schema"),
        "brief_date": state.get("brief_date"),
        "updated_at": state.get("updated_at"),
        "items": c["total"],
        "done": c["done"],
        "actionable": c["actionable"],
    }


if __name__ == "__main__":
    print(json.dumps(health(), indent=2))
