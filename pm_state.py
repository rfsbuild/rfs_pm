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
import copy
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
#
# `routine` sits SECOND, never first: it refills with the same handful of cards
# every single morning, so putting it above `urgent` would push a genuinely red
# item below a checklist every day of the week. It is seeded by pm_routine.py
# from the 08:00 job — never by the sweep, and never picked by hand (it is
# deliberately absent from the UI's LANE_PICK), because a card parked there by
# hand would be silently reset by the next morning's seed.
LANES = ("urgent", "routine", "action", "week", "rafael", "guilherme", "claude", "noise")
LANE_LABELS = {
    "urgent": "🔴 Urgent",
    "routine": "🔁 Daily Routine",
    "action": "🟠 Action Today",
    "week": "🟡 This Week",
    "rafael": "Pending Rafael",
    "guilherme": "Pending Gui",
    "claude": "🤖 Claude",
    "noise": "Noise / FYI",
}
# Lanes that represent work she is personally on the hook for today.
#
# `routine` IS in here — the morning checklist is work she is on the hook for,
# and leaving it out would mean ticking all four morning items moved the day's
# progress ring by nothing. Checked before including it: counts() feeds only
# the board UI's ring and tiles (pm_server, pm_app, pm_ui) — the FD/Rafael
# daily report does not read it, so recurring chores cannot inflate anything
# that leaves this machine.
ACTIONABLE_LANES = ("urgent", "routine", "action", "week", "rafael", "guilherme", "claude")

DEFER_REASONS = {
    "no-time": "⏰ No time today",
    "later-week": "📅 Later this week",
    "needs-rafael": "⚪ Needs Rafael",
}

# ── Waiting on someone (2026-07-29) ────────────────────────────────────────
# "Waiting for someone" USED to be a defer reason — a tag that left the card on
# the main board, sank it one rank, and after 3 days shouted URGENT at her for
# something she still could not do. It recorded neither WHO she was waiting on
# nor what she had already tried, and `note` is a single string, so every chase
# overwrote the previous one.
#
# Her ask, verbatim: "the flow to keep updating a task without closing … if i
# chose 'waiting for someone' it goes to a separate space for me to keep
# updating until it's done, so I know I also have to keep track of."
#
# So waiting is now a first-class BLOCK on the item, not a defer reason:
#
#   waiting = {
#     "who":      str,   # required — a name. "waiting" with no owner is a wish.
#     "what":     str,   # optional — what specifically is owed to us
#     "kind":     str,   # optional — "response" | "task" | None. See below.
#     "since":    iso,   # when it entered the waiting space
#     "nudge_on": date,  # optional — when to chase again; past = it surfaces red
#     "log":      [ {"at": iso, "text": str} ],   # APPEND-ONLY chase history
#   }
#
# `kind` — her distinction, 2026-07-30, in her words: a person-lane card is
# "either 'pending response' or 'pending task done by the person' — do you get
# the difference?". Both mean she cannot move it, so both belong in this space;
# but they are not chased the same way, and that is the whole reason to record
# which one it is:
#   "response" → they owe an ANSWER. Chase reads "did you see this?" and ships
#                a ready-to-send draft (her standing chase-card rule). Shorter
#                fuse, because an unanswered question rots faster than work in
#                progress.
#   "task"     → they owe the WORK. Chase reads "is it done yet?". Longer fuse.
#   None       → she skipped the question. Deliberately NOT defaulted to either:
#                an absent answer must stay absent rather than be guessed, the
#                same rule the sweep applies to a missing marker. The card still
#                works — generic wording, default fuse.
#
# Design constraints that are load-bearing:
#   · It stays `status == "open"` — the item is not done and must never read as
#     done. Waiting is a sub-state of open, so the roll, the archive and the
#     daily report keep working unchanged.
#   · It leaves the Open count and the main board (her call), which means the
#     ONLY thing standing between a waiting item and silent rot is `nudge_on`.
#     That is why waiting_needing_nudge() exists and why the Today view shows
#     the overdue-nudge strip. A separate space without a resurfacing rule is a
#     drawer things die in.
#   · The log is APPEND-ONLY by construction — there is no edit or delete path,
#     in this module or over HTTP. The value of the record is that it can prove
#     she chased MGP three times; a mutable log cannot prove anything.
WAITING_REQUIRED = ("who",)
DEFAULT_NUDGE_DAYS = 3

# kind → how many days until the next chase. A dict and not two constants
# because the UI reads it to label the buttons, so the fuse and the wording can
# never disagree about what "a reply" means.
WAITING_KINDS = {"response": 2, "task": 3}
WAITING_KIND_LABELS = {"response": "a reply", "task": "the work"}


def clean_kind(kind):
    """A valid kind, or None. Every entry point routes through this.

    The isinstance check is not defensive padding: `kind` arrives straight off a
    JSON body, and `x in WAITING_KINDS` raises TypeError on an unhashable value,
    so a POST carrying `"kind": []` would 500 the server rather than be refused.
    Caught by a parametrised test, not by reading the code.
    """
    return kind if isinstance(kind, str) and kind in WAITING_KINDS else None

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
    # ── the update stream (2026-07-30) ──
    # Her ask, two ways on two days: "i need to have the completed items
    # somewhere cause if they ask me if I did something and I don't remember, I
    # need to have that somewhere to confirm", and an update box on every card
    # so she can record what happened as the day moves.
    #
    # Those are ONE mechanism, not two: both are "append a timestamped entry to
    # this card". One stream, two entry points — written any time, or prompted
    # at tick-time.
    #
    # WHY THIS EXISTS ALONGSIDE `did` RATHER THAN INSTEAD OF IT:
    # `did` is in PATCHABLE, so it can be overwritten — and a field that can be
    # rewritten is not evidence. `updates` is append-only here and over HTTP,
    # exactly like the waiting-space chase log, so it can answer "did I do this,
    # and when?" against someone who remembers differently. `did` stays as the
    # one-line headline the daily report reads; the stream is the record.
    #
    # list[{at, text, kind}] — kind is "update" (written mid-day) or "done"
    # (captured at the tick). Never edited, never deleted.
    "updates",
    # ── the three-line context contract (2026-07-30) ──
    # Measured on her live board before this shipped: 36 open cards carried a
    # MEDIAN of 888 characters of context, 25 of them over 600, the worst 2,497.
    # A card that takes a paragraph to read is a card she skips, so the context
    # had drifted into a dump of everything the sweep found.
    #
    #   ctx_happened — the fact. What occurred.
    #   ctx_matters  — the consequence. Why she should care.
    #   ctx_needed   — the ask. What is required now.
    #
    # One sentence each, capped at CTX_LINE_CAP and enforced in pm_ingest.py,
    # NOT in a style note — a rule that lives only in prose is a rule that gets
    # skipped on a busy morning (the same lesson as CHASE_WORDS). Nothing is
    # discarded: the raw text stays in ctx_body behind a `▸ source` disclosure.
    #
    # Existing cards are deliberately NOT backfilled (her decision): they keep
    # rendering ctx_sum/ctx_body, and the board converges as cards refresh.
    "ctx_happened",
    "ctx_matters",
    "ctx_needed",
    # True when the ingest had to trim a line to the cap. Her choice over a
    # silent cut: a trimmed line must be visibly trimmed, or she cannot tell an
    # abbreviated sentence from a naturally short one.
    "ctx_trimmed",
    # ── The split (2026-07-29) ──
    # Added after Hadassa's instruction: "absolutely everything you can help me
    # with, do beforehand, I will want you to. Final word will always be mine."
    # Before this, a card could only express work assigned TO her — there was no
    # field for work already done FOR her, so every card read as a fresh chore
    # even when the research, the recipient list and the draft were finished.
    #
    #   claude_done  — list[str]: what is ALREADY DONE and sitting on the card,
    #                  waiting for her. Past tense, concrete, no promises.
    #   hadassa_todo — list[str]: what genuinely cannot be done for her —
    #                  a judgement, an approval, a phone call, a signature.
    #
    # The discipline this enforces: writing a card now forces the question
    # "what did I actually do about this?" A `hadassa_todo` with an empty
    # `claude_done` on an actionable card is a smell, not a neutral state.
    "claude_done",
    "hadassa_todo",
    # ── waiting-on space (2026-07-29) — see the WAITING block above ──
    "waiting",
    # ── delegation to Claude (2026-07-29) ──
    # Hadassa: "what about the 'assigned away' that goes to claude? shouldn't
    # them be as done as well and flagged once done that was Claude by my order?
    # They shouldn't be kept together with the other assigned away ones that are
    # for other people. and once I click 'claude' to run that task by itself, how
    # will I know it'll be done in time? when will you know that you're supposed
    # to do it?"
    #
    # Three separate problems in that, and each needs a field:
    #   claude_queued_at — WHEN she delegated it. Without this there is no way to
    #                      say "queued 3 hours ago" or to notice something has sat
    #                      in the queue for two days. Her real question is about
    #                      time, and time needs a timestamp.
    #   done_by          — WHO finished it. A card completed by Claude must not
    #                      read as work she did; the daily report is a record of
    #                      HER actions, so an unmarked Claude completion would
    #                      quietly inflate it.
    #   claude_result    — what Claude actually did, in one line, so "done" is
    #                      auditable rather than asserted.
    #
    # The honest limit, which the UI states rather than hides: Claude does not
    # watch this board. It is seen when a session runs. So the queue is swept at
    # the start of every session, and the card shows how long it has waited.
    "claude_queued_at",
    "done_by",
    "claude_result",
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
        # Per-source read position for the polling sweep — see get_watermark().
        "sweep_watermarks": {},
        # None = the last sweep succeeded (or none has run).
        "last_sweep_failure": None,
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
        # `did` was added to ITEM_FIELDS on 2026-07-28 but not here, so no item
        # created through new_item carried the key — and because normalize()
        # backfills from new_item(), it could not repair it either. The daily
        # report reads i["did"] directly at gen_daily_report.py:246 (safe today
        # only because the reportable filter already proved it truthy). Declared
        # here so the key always exists and normalize() can heal older files.
        "did": None,
        # The append-only update stream — see ITEM_FIELDS. A list here (not
        # None) so every reader can iterate without a guard, and so normalize()
        # backfills it onto the items that already exist in her live state.
        # This is the SAME omission the `did` comment above records, caught by
        # the ITEM_FIELDS-completeness test rather than in production.
        "updates": [],
        # The three-line contract — see ITEM_FIELDS. None, not "", so the UI can
        # tell "this card predates the contract" from "this line was left blank".
        "ctx_happened": None,
        "ctx_matters": None,
        "ctx_needed": None,
        "ctx_trimmed": False,
        # The split — see ITEM_FIELDS. Lists, so the UI can render them as two
        # checklists side by side rather than one undifferentiated blob.
        "claude_done": [],
        "hadassa_todo": [],
        # None = not waiting on anyone. See the WAITING block near the top.
        "waiting": None,
        # Delegation to Claude — see ITEM_FIELDS.
        "claude_queued_at": None,
        "done_by": None,
        "claude_result": None,
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
    # Her live file predates both of these; back-fill so every reader can look
    # them up without a guard.
    state.setdefault("sweep_watermarks", {})
    state.setdefault("last_sweep_failure", None)
    ref = new_item("", "")
    for it in state["items"]:
        for k, v in ref.items():
            # deepcopy, NOT setdefault(k, v) — `ref` is built ONCE above, so
            # handing its value straight over gave every item that was missing
            # a key THE SAME list object. Any in-place append then wrote to
            # every card at once.
            #
            # Found 2026-07-30 the first time a default list was appended to
            # rather than replaced wholesale: one update posted to one card
            # appeared, with an identical timestamp, on all 54. The other list
            # defaults (ctx_body, links, pills, where, claude_done,
            # hadassa_todo) were aliased in exactly the same way and had simply
            # never been mutated in place — the ingest always replaces them.
            # So this is fixed for the whole class, not just for `updates`.
            if k not in it:
                it[k] = copy.deepcopy(v)
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
        _migrate_waiting_defer(it)
        _normalize_waiting(it)
    return state


def _migrate_waiting_defer(it):
    """Legacy `defer == "waiting"` → a real waiting block.

    Two live cards carried it when the space was built (South Shore #1567 and
    Sky Tech's missing COI). Migrating in normalize() rather than a one-shot
    script means an old backup, an old history file or a stale ingest payload
    all heal on load instead of quietly landing in neither space.

    `who` is UNKNOWN, not a guess. The old tag never recorded a person, so
    inventing one here would fabricate a fact about her work — the card asks her
    for the name instead, and `_needs_who` is what the UI flags.
    """
    if it.get("defer") != "waiting":
        return
    it["defer"] = None
    it["defer_days"] = 0
    if not it.get("waiting"):
        it["waiting"] = {
            "who": "",
            "_needs_who": True,
            "what": "",
            "since": it.get("last_seen") or it.get("first_seen") or _now_iso(),
            "nudge_on": None,
            "log": [{"at": it.get("last_seen") or _now_iso(),
                     "text": "Carried over from the old “waiting for someone” "
                             "tag — who we are waiting on was never recorded."}],
        }


def _normalize_waiting(it):
    """Shape-heal a waiting block so a malformed one can't crash the board."""
    w = it.get("waiting")
    if not w:
        it["waiting"] = None
        return
    if not isinstance(w, dict):
        it["waiting"] = None
        return
    w.setdefault("who", "")
    w.setdefault("what", "")
    w.setdefault("since", _now_iso())
    w.setdefault("nudge_on", None)
    # An unrecognised kind becomes None, not a crash and not a silent pass —
    # every card predating this field lands here and must render generically.
    w["kind"] = clean_kind(w.get("kind"))
    log = w.get("log")
    if not isinstance(log, list):
        log = []
    w["log"] = [e for e in log if isinstance(e, dict) and (e.get("text") or "").strip()]
    w["_needs_who"] = not (w.get("who") or "").strip()


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


def is_waiting(it):
    """In the waiting space — blocked on someone else, and still not done.

    The `status == "open"` half matters: once she marks a waiting item done it
    must leave the waiting space immediately, and the block is kept only as the
    record of how it got there.
    """
    return bool(it.get("waiting")) and it.get("status") == "open"


def waiting_items(state):
    return [it for it in state["items"] if is_waiting(it)]


def nudge_due(it, now=None):
    """True when the chase date has arrived or passed. No date = never due."""
    w = it.get("waiting") or {}
    on = w.get("nudge_on")
    return bool(on) and str(on) <= _today(now)


def days_since_update(it, now=None):
    """Days since the last log line (or since it entered waiting if never logged).

    This is the honest staleness measure: `age` counts days on the board, which
    keeps climbing even on an item she chased this morning.
    """
    w = it.get("waiting") or {}
    last = (w.get("log") or [{}])[-1].get("at") or w.get("since")
    dt = parse_dt(last)
    if dt is None:
        return 0
    today = datetime.datetime.fromisoformat(_today(now))
    return max(0, (today.date() - dt.date()).days)


def waiting_needing_nudge(state, now=None):
    """Waiting items that need her now — chase date arrived, OR no owner recorded.

    THE safeguard for the whole feature. Because waiting items leave the main
    board, this list is the only thing that brings one back into her day.

    A card with no `who` counts as needing her even with no date: it cannot be
    chased at all until it is named, which makes it the most stuck kind of
    blocked, not the least. The UI's tile applies exactly this rule — a count and
    the list it opens must be computed by one definition, or they drift.
    """
    due = [it for it in waiting_items(state)
           if nudge_due(it, now) or (it.get("waiting") or {}).get("_needs_who")]
    due.sort(key=lambda it: ((it.get("waiting") or {}).get("nudge_on") or "9999",
                             -days_since_update(it, now)))
    return due


def ordered_waiting(state, now=None):
    """Display order for the waiting space: needs-a-nudge first, then stalest.

    An item with no `who` recorded sorts to the very top — it cannot be chased
    at all until she names the person, so it is the most broken kind of blocked.
    """
    def key(it):
        w = it.get("waiting") or {}
        return (
            0 if w.get("_needs_who") else 1,
            0 if nudge_due(it, now) else 1,
            -days_since_update(it, now),
            (w.get("who") or "").lower(),
        )
    return sorted(waiting_items(state), key=key)


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
        # Blocked on someone else is not a thing she can commit to finishing
        # today. It belongs in the waiting space, chased on its nudge date.
        and not is_waiting(it)
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
    waiting = [it for it in actionable if is_waiting(it)]
    # A dismissed item is neither done nor outstanding, so it leaves the
    # denominator entirely — otherwise the day's progress bar would be gamed by
    # dismissing work rather than doing it.
    #
    # Waiting leaves it too, for the opposite reason: it is real work that is
    # genuinely not hers to move today, so counting it as outstanding makes the
    # day look unfinishable no matter what she does. The anti-gaming control for
    # waiting is not the denominator — it is that every waiting item must name a
    # person and shows its own age and chase count in its own tab.
    denom = len(actionable) - len(dismissed) - len(waiting)
    return {
        "per_lane": per_lane,
        "total": len(state["items"]),
        "actionable": len(actionable),
        "done": len(done),
        "dismissed": len(dismissed),
        "waiting": len(waiting),
        "waiting_due": len(waiting_needing_nudge(state)),
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
    which is what drives the 3-day escalation to urgent.

    "waiting" is refused here for the same reason apply_click refuses it: it is
    its own space now, and it needs a name. Guarding only the HTTP path would
    leave every script free to recreate the ownerless-waiting state.
    """
    if reason == "waiting":
        return {"error": "“waiting for someone” is now its own space — "
                         "use set_waiting(item_id, who=…)"}

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


# ── work she delegated to Claude ──

def claude_queue(state):
    """Open items she has delegated to Claude, oldest-queued first.

    This is the list swept at the start of every session. It is a QUEUE, not a
    lane filter: an item counts because she assigned it, regardless of which lane
    it happens to sit in.
    """
    q = [it for it in state["items"]
         if it.get("assignee") == "claude" and it.get("status") == "open"]
    q.sort(key=lambda it: it.get("claude_queued_at") or "")
    return q


def hours_queued(it, now=None):
    """How long a delegated item has been waiting on Claude. None if unstamped.

    Cards delegated before `claude_queued_at` existed have no stamp — reported as
    None rather than backfilled to now, which would make an old item look fresh.
    """
    dt = parse_dt(it.get("claude_queued_at"))
    if dt is None:
        return None
    ref = now or datetime.datetime.now().astimezone()
    return max(0.0, (ref - dt).total_seconds() / 3600.0)


def complete_by_claude(item_id, result, path=STATE_PATH, now=None):
    """Mark a delegated item done BY CLAUDE, with what was actually done.

    `result` is required. "Done" with no statement of what was done is exactly the
    kind of claim this system exists to prevent — and because these completions
    feed a report about HER day, they have to be separable from her own work.

    Refuses if the item was never delegated to Claude: Claude closing something
    she never handed over would be Claude deciding her priorities.
    """
    result = (result or "").strip()
    if not result:
        return {"error": "say what was actually done — a bare 'done' is not a result"}

    def _fn(state):
        it = get_item(state, item_id)
        if it is None:
            return None
        if it.get("assignee") != "claude":
            return {"error": "this item was not delegated to Claude; only she can "
                             "close her own work"}
        it["status"] = "done"
        it["done_at"] = _now_iso(now)
        it["done_by"] = "claude"
        it["claude_result"] = result
        it["dismiss_reason"], it["dismissed_at"] = None, None
        return {"ok": True, "id": item_id, "done_by": "claude",
                "queued_hours": hours_queued(it, now)}
    return _mutate(_fn, path, now)[1]


# ── the waiting space ──

def default_nudge(now=None, days=DEFAULT_NUDGE_DAYS):
    d = (now or datetime.datetime.now().astimezone()).date()
    return (d + datetime.timedelta(days=days)).isoformat()


def set_waiting(item_id, who, what="", nudge_on=None, first_update=None,
                kind=None, path=STATE_PATH, now=None):
    """Move an item into the waiting space.

    `who` is REQUIRED and rejected when blank. The entire failure mode of the
    old defer tag was that it recorded no owner, so a month later she had a pile
    of cards she was waiting on *somebody* for. A waiting item without a name is
    not trackable, so the transition itself refuses.

    Entering waiting CLEARS any defer: they are different statements ("no time
    today" is about her, "waiting on Marconio" is about him), and leaving both
    set would let the 3-day defer escalation drag the card back to Urgent for
    something she still cannot do.

    `kind` is optional ("response" | "task") and sets the default fuse when
    `nudge_on` is not given — see WAITING_KINDS. An explicit `nudge_on` always
    wins: she can say "chase Friday" regardless of what kind of thing it is.
    """
    who = (who or "").strip()
    if not who:
        return {"error": "name who you are waiting on — a waiting item with no "
                         "owner can never be chased"}
    kind = clean_kind(kind)

    def _fn(state):
        it = get_item(state, item_id)
        if it is None:
            return None
        existing = it.get("waiting") or {}
        log = list(existing.get("log") or [])
        text = (first_update or "").strip()
        if text:
            log.append({"at": _now_iso(now), "text": text})
        it["waiting"] = {
            "who": who,
            "what": (what or "").strip(),
            "kind": kind,
            "since": existing.get("since") or _now_iso(now),
            "nudge_on": nudge_on or default_nudge(
                now, WAITING_KINDS.get(kind, DEFAULT_NUDGE_DAYS)),
            "log": log,
            "_needs_who": False,
        }
        it["defer"], it["defer_days"] = None, 0
        # An open item can be waiting; a done one cannot. Re-opening here would
        # be wrong the other way, so only the done→open direction is refused.
        if it.get("status") == "dismissed":
            it["status"] = "open"
            it["dismiss_reason"], it["dismissed_at"] = None, None
        return {"ok": True, "id": item_id, "waiting": it["waiting"]}
    return _mutate(_fn, path, now)[1]


def add_waiting_update(item_id, text, nudge_on=None, path=STATE_PATH, now=None):
    """Append one dated line to the chase log. APPEND-ONLY — nothing is replaced.

    `nudge_on` is optional and, when given, re-arms the chase date. That pairing
    is deliberate: the moment she records "he says Friday" is exactly the moment
    the next nudge date is known, and asking for it in a second interaction is
    how the date ends up never being set.

    There is no edit and no delete, here or over HTTP. A log that can be
    rewritten cannot serve as evidence that she chased three times.
    """
    text = (text or "").strip()
    if not text:
        return {"error": "an update needs some text"}

    def _fn(state):
        it = get_item(state, item_id)
        if it is None:
            return None
        w = it.get("waiting")
        if not w:
            return {"error": "this item is not in the waiting space"}
        w.setdefault("log", []).append({"at": _now_iso(now), "text": text})
        if nudge_on:
            w["nudge_on"] = nudge_on
        return {"ok": True, "id": item_id, "updates": len(w["log"]),
                "nudge_on": w.get("nudge_on")}
    return _mutate(_fn, path, now)[1]


def set_waiting_who(item_id, who, path=STATE_PATH, now=None):
    """Fill in the missing owner on a migrated card. Blank is still refused."""
    who = (who or "").strip()
    if not who:
        return {"error": "name who you are waiting on"}

    def _fn(state):
        it = get_item(state, item_id)
        if it is None or not it.get("waiting"):
            return None
        it["waiting"]["who"] = who
        it["waiting"]["_needs_who"] = False
        return {"ok": True, "id": item_id, "who": who}
    return _mutate(_fn, path, now)[1]


def set_waiting_kind(item_id, kind, renudge=True, path=STATE_PATH, now=None):
    """Label WHAT is owed — a reply, or the work itself. Her distinction.

    Separate from set_waiting() on purpose: the card moves into the waiting space
    the instant she picks a person (nothing is gated on her answering), and this
    is the skippable follow-up question. A transition that waits for an optional
    answer is a transition she can abandon halfway.

    `renudge` re-arms the chase date to that kind's fuse, which is the entire
    point of asking — a question owed to her rots faster than work in progress.
    Passed False when she is re-labelling an old card whose date she has already
    chosen; silently moving a date she set herself would be the worse surprise.

    The answer is also APPENDED to the chase log, because "what am I even
    waiting for" is exactly the question the log exists to answer later.
    """
    if clean_kind(kind) is None:
        return {"error": "kind must be one of: %s" % ", ".join(sorted(WAITING_KINDS))}
    kind = clean_kind(kind)

    def _fn(state):
        it = get_item(state, item_id)
        if it is None:
            return None
        w = it.get("waiting")
        if not w:
            return {"error": "this item is not in the waiting space"}
        w["kind"] = kind
        if renudge:
            w["nudge_on"] = default_nudge(now, WAITING_KINDS[kind])
        w.setdefault("log", []).append({
            "at": _now_iso(now),
            "text": "Waiting for %s — chase on %s" % (WAITING_KIND_LABELS[kind],
                                                      w["nudge_on"] or "no date set"),
        })
        return {"ok": True, "id": item_id, "kind": kind,
                "nudge_on": w.get("nudge_on")}
    return _mutate(_fn, path, now)[1]


def clear_waiting(item_id, reason=None, path=STATE_PATH, now=None):
    """Unblock — back onto the main board, still open.

    The waiting block is DROPPED but its log is preserved into `note` first,
    because that history is the answer to "why did this take three weeks?" and
    the board is the only place she will ever look for it.
    """
    def _fn(state):
        it = get_item(state, item_id)
        if it is None:
            return None
        w = it.get("waiting") or {}
        if not w:
            return {"error": "this item is not in the waiting space"}
        lines = ["Was waiting on %s%s:" % (w.get("who") or "someone",
                                          " — " + w["what"] if w.get("what") else "")]
        for e in (w.get("log") or []):
            lines.append("  %s  %s" % ((e.get("at") or "")[:16].replace("T", " "),
                                       e.get("text")))
        if (reason or "").strip():
            lines.append("  %s  unblocked: %s" % (_now_iso(now)[:16].replace("T", " "),
                                                  reason.strip()))
        history = "\n".join(lines)
        it["note"] = (it["note"] + "\n\n" + history) if it.get("note") else history
        it["waiting"] = None
        return {"ok": True, "id": item_id}
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


UPDATE_KINDS = ("update", "done")


def add_update(item_id, text, kind="update", set_did=False,
               path=STATE_PATH, now=None):
    """Append one timestamped entry to a card's update stream. APPEND-ONLY.

    The same shape as add_waiting_update(), and for the same reason: this is
    the answer to "did I do that, and when?", so nothing here may be replaced.
    There is no edit and no delete, in this module or over HTTP.

    `set_did` is what makes the tick-time prompt one interaction instead of
    two. When she ticks a card and types what she did, that single sentence is
    both the daily report's headline (`did`) and a line in the permanent record
    (the appended entry). Asking for it twice is how one of them ends up empty
    — which is precisely the state the board was already in: 28 of 49 completed
    items carried no note at all, because nothing in her workflow ever wrote
    one.

    Note the asymmetry that is deliberate: `did` gets OVERWRITTEN (it is the
    current headline) while the entry is APPENDED (it is the history). So
    correcting a `did` later leaves the earlier version standing in the stream,
    which is what makes the stream evidence rather than a draft.
    """
    text = (text or "").strip()
    if not text:
        return {"error": "an update needs some text"}
    if kind not in UPDATE_KINDS:
        kind = "update"

    def _fn(state):
        it = get_item(state, item_id)
        if it is None:
            return None
        it.setdefault("updates", []).append(
            {"at": _now_iso(now), "text": text, "kind": kind})
        if set_did:
            it["did"] = text
        return {"ok": True, "id": item_id, "updates": len(it["updates"]),
                "did": it.get("did")}
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
                # A tick in the UI is HER completion. complete_by_claude() is the
                # only path that sets done_by="claude", so nothing Claude did can
                # ever be silently credited to her, and nothing she did can be
                # credited to Claude.
                it["done_by"] = "hadassa" if want else None
                if want:   # completing something clears a prior dismissal
                    it["dismiss_reason"] = None
                    it["dismissed_at"] = None
        if "assignee" in payload:
            who = payload["assignee"]
            prev = it.get("assignee")
            it["assignee"] = None if who in (None, "", "hadassa") else who
            # Stamp WHEN she delegated to Claude. Her question was about time
            # ("how will I know it'll be done in time?"), and time cannot be
            # answered without recording the moment the clock started. Only on
            # the transition, so a re-render cannot reset the clock.
            if it["assignee"] == "claude" and prev != "claude":
                it["claude_queued_at"] = _now_iso(now)
            elif it["assignee"] != "claude" and prev == "claude":
                it["claude_queued_at"] = None
        if "defer" in payload:
            new = payload["defer"] or None
            # "waiting" is no longer a defer reason — it is its own space, and
            # entering it requires a name. A stale browser tab (or an old script)
            # can still send it, so refuse the write and tell the caller what to
            # do instead of silently storing a reason the board no longer renders.
            if new == "waiting":
                return {"error": "“waiting for someone” is now its own space — "
                                 "POST /api/item/<id>/waiting with who you are "
                                 "waiting on", "needs_waiting": True, "id": item_id}
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
            "waiting": it.get("waiting"),
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
            # Logged so the roll never hides how much of the board is blocked on
            # other people — a day that carries 12 waiting items is a different
            # day from one that carries 12 items she can actually work.
            "waiting": sum(1 for it in carried if is_waiting(it)),
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
        # A success clears any recorded failure. Without this the red banner
        # outlives the problem and starts crying wolf, which is how a warning
        # becomes something she scrolls past.
        state["last_sweep_failure"] = None
        return state["last_swept_at"]
    return _mutate(_fn, path, now)[1]


# ── the live watermarked sweep (2026-07-30) ────────────────────────────────
# `last_swept_at` is a single global stamp: it answers "how old is this board?"
# but not "what have I already read?". Polling every 30 minutes needs the second
# question answered PER SOURCE, or each poll re-reads the whole morning and the
# cheap case stops being cheap.
#
#   sweep_watermarks = {"slack": {"cursor": "1753900000.123", "at": iso},
#                       "gmail": {"cursor": "history-id-or-iso", "at": iso}}
#
# The load-bearing rule: a watermark advances ONLY after a successful ingest.
# Advancing on a successful *fetch* means a crash between fetch and write loses
# those messages permanently and silently — the board would simply never show
# them, and nothing would look wrong. Re-reading a few messages is free; the
# ingest is idempotent by id. Losing one is not.
def get_watermark(state, source):
    return ((state.get("sweep_watermarks") or {}).get(source) or {}).get("cursor")


def advance_watermark(source, cursor, path=STATE_PATH, now=None):
    """Move one source's watermark forward. Call AFTER the ingest succeeded."""
    cursor = (str(cursor) if cursor is not None else "").strip()
    if not cursor:
        return {"error": "a watermark needs a cursor"}

    def _fn(state):
        wm = state.setdefault("sweep_watermarks", {})
        wm[source] = {"cursor": cursor, "at": _now_iso(now)}
        return {"ok": True, "source": source, "cursor": cursor}
    return _mutate(_fn, path, now)[1]


def record_sweep_failure(reason, sources=None, path=STATE_PATH, now=None):
    """Persist that a sweep FAILED, so the board can say so on the page.

    The counterpart to mark_swept()'s refusal. That function correctly declines
    to stamp a sweep it has no evidence for — but a refusal that only raises
    into a log file leaves the board showing older data while looking perfectly
    healthy, which is the confident-partial-brief failure this whole mechanism
    exists to prevent. A failure has to be as visible as a success.

    Cleared by the next successful mark_swept().
    """
    def _fn(state):
        state["last_sweep_failure"] = {
            "at": _now_iso(now),
            "reason": str(reason or "").strip() or "unknown",
            "sources": sorted(sources or []),
        }
        return state["last_sweep_failure"]
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
