#!/usr/bin/env python3
"""Tests for the waiting-on space (2026-07-29).

Hadassa's ask: "the flow to keep updating a task without closing … if i chose
'waiting for someone' it goes to a separate space for me to keep updating until
it's done, so I know I also have to keep track of."

What these tests are here to LOCK, beyond the happy path:

  1. `who` is mandatory — the old defer tag's whole failure was recording no
     owner, and an ownerless waiting item can never be chased.
  2. The log is APPEND-ONLY. No call in this module may shorten it or rewrite an
     entry. A log that can be edited proves nothing about how often she chased.
  3. Waiting leaves the Open count but NOT the board's memory — it stays
     `status == "open"`, survives the roll, and comes back via its nudge date.
  4. The legacy `defer == "waiting"` cards migrate on load, with `who` blank and
     flagged rather than guessed.

Run:  cd ~/rfs_pm && python3 -m pytest tests -q
"""
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pm_state as S  # noqa: E402


@pytest.fixture
def path(tmp_path, monkeypatch):
    p = tmp_path / "pm_state.json"
    monkeypatch.setattr(S, "LOCK_PATH", tmp_path / ".lock")
    monkeypatch.setattr(S, "HISTORY_DIR", tmp_path / "history")
    st = S.blank_state(brief_date="2026-07-29")
    st["items"] = [
        S.new_item("mgp", "MGP COI before the basement demo", lane="urgent"),
        S.new_item("ray", "Ray — skylight model and size", lane="action"),
        S.new_item("plain", "Something she can just do", lane="action"),
    ]
    S.save_state(st, p)
    return p


def _get(path, iid):
    st, _ = S.load_state(path)
    return S.get_item(st, iid)


# ── entering the space ──

def test_who_is_required(path):
    for blank in (None, "", "   "):
        res = S.set_waiting("mgp", blank, path=path)
        assert res["error"], "a waiting item with no owner must be refused"
    assert _get(path, "mgp")["waiting"] is None


def test_set_waiting_records_who_what_and_a_default_nudge(path):
    now = datetime.datetime(2026, 7, 29, 10, 0).astimezone()
    S.set_waiting("mgp", "Marconio (MGP)", what="certificate of insurance",
                  first_update="Emailed asking for the COI", path=path, now=now)
    w = _get(path, "mgp")["waiting"]
    assert w["who"] == "Marconio (MGP)"
    assert w["what"] == "certificate of insurance"
    assert w["_needs_who"] is False
    # Pre-armed, not blank: an un-dated waiting item is invisible work.
    assert w["nudge_on"] == "2026-08-01"          # +3 days
    assert [e["text"] for e in w["log"]] == ["Emailed asking for the COI"]


def test_entering_waiting_clears_any_defer(path):
    S.set_defer("ray", "no-time", path=path)
    S.set_waiting("ray", "Ray Dunetz", path=path)
    it = _get(path, "ray")
    # Both set would let the 3-day defer escalation drag it back to Urgent for
    # something she still cannot do.
    assert it["defer"] is None and it["defer_days"] == 0
    assert S.is_waiting(it)


def test_waiting_stays_open_not_done(path):
    S.set_waiting("mgp", "Marconio", path=path)
    assert _get(path, "mgp")["status"] == "open"


# ── the append-only log ──

def test_updates_append_and_never_replace(path):
    S.set_waiting("mgp", "Marconio", first_update="emailed", path=path)
    S.add_waiting_update("mgp", "no reply — texted him", path=path)
    S.add_waiting_update("mgp", "says his insurer sends it Friday", path=path)
    log = _get(path, "mgp")["waiting"]["log"]
    assert [e["text"] for e in log] == [
        "emailed", "no reply — texted him", "says his insurer sends it Friday"]
    assert all(e["at"] for e in log), "every entry is dated"


def test_update_can_rearm_the_nudge_date(path):
    S.set_waiting("mgp", "Marconio", nudge_on="2026-07-30", path=path)
    S.add_waiting_update("mgp", "insurer sends Friday", nudge_on="2026-08-03",
                         path=path)
    assert _get(path, "mgp")["waiting"]["nudge_on"] == "2026-08-03"


def test_empty_update_is_refused(path):
    S.set_waiting("mgp", "Marconio", path=path)
    assert S.add_waiting_update("mgp", "   ", path=path)["error"]
    assert _get(path, "mgp")["waiting"]["log"] == []


def test_update_on_a_non_waiting_item_is_refused(path):
    assert S.add_waiting_update("plain", "hello", path=path)["error"]


def test_no_public_path_shortens_a_log(path):
    """The append-only guarantee, asserted against the module's real surface.

    If a future function starts removing log entries, this fails — which is the
    point. The value of the chase log is that it can prove she chased three
    times, and that survives only while nothing can rewrite it.
    """
    S.set_waiting("mgp", "Marconio", first_update="one", path=path)
    S.add_waiting_update("mgp", "two", path=path)
    before = len(_get(path, "mgp")["waiting"]["log"])
    # Everything else the UI can do to a waiting card, in one pass.
    S.set_note("mgp", "a note", path=path)
    S.patch_content("mgp", {"subject": "renamed"}, path=path)
    S.apply_click("mgp", {"assignee": "rafael", "due": "2026-08-05"}, path=path)
    S.set_waiting("mgp", "Marconio Pinto", path=path)      # re-entering
    S.set_waiting_who("mgp", "Marconio P.", path=path)
    S.add_waiting_update("mgp", "three", path=path)
    log = _get(path, "mgp")["waiting"]["log"]
    assert len(log) >= before + 1
    assert [e["text"] for e in log][:2] == ["one", "two"]


# ── the nudge — the only thing that brings a waiting item back ──

def test_nudge_due_only_when_the_date_has_come(path):
    S.set_waiting("mgp", "Marconio", nudge_on="2026-01-01", path=path)
    S.set_waiting("ray", "Ray", nudge_on="2099-01-01", path=path)
    st, _ = S.load_state(path)
    assert S.nudge_due(S.get_item(st, "mgp")) is True
    assert S.nudge_due(S.get_item(st, "ray")) is False
    assert [it["id"] for it in S.waiting_needing_nudge(st)] == ["mgp"]


def test_an_unnamed_waiting_item_needs_her_even_with_no_date(path):
    """It cannot be chased at all, so it is the MOST stuck, not the least.

    This is also the count the UI tile shows; both sides must apply one rule or
    the tile claims a number the list does not contain.
    """
    st, _ = S.load_state(path)
    S.get_item(st, "ray")["defer"] = "waiting"     # legacy tag, no owner
    S.save_state(st, path)
    st, _ = S.load_state(path)
    assert [it["id"] for it in S.waiting_needing_nudge(st)] == ["ray"]
    assert S.counts(st)["waiting_due"] == 1
    S.set_waiting_who("ray", "South Shore", path=path)
    st, _ = S.load_state(path)
    assert S.counts(st)["waiting_due"] == 0


def test_days_since_update_measures_the_last_chase_not_board_age(path):
    old = datetime.datetime(2026, 7, 20, 9, 0).astimezone()
    S.set_waiting("mgp", "Marconio", first_update="emailed", path=path, now=old)
    it = _get(path, "mgp")
    now = datetime.datetime(2026, 7, 29, 9, 0).astimezone()
    assert S.days_since_update(it, now=now) == 9
    S.add_waiting_update("mgp", "chased again", path=path, now=now)
    assert S.days_since_update(_get(path, "mgp"), now=now) == 0


def test_ordered_waiting_puts_unchaseable_first_then_due(path):
    S.set_waiting("mgp", "Marconio", nudge_on="2099-01-01", path=path)
    S.set_waiting("ray", "Ray", nudge_on="2026-01-01", path=path)
    S.set_waiting("plain", "Somebody", path=path)
    S.set_waiting_who("plain", "x", path=path)
    # Force the no-name case the migration produces.
    st, _ = S.load_state(path)
    S.get_item(st, "plain")["waiting"]["who"] = ""
    S.save_state(st, path)
    st, _ = S.load_state(path)                     # normalize re-flags it
    assert [it["id"] for it in S.ordered_waiting(st)][0] == "plain"
    assert [it["id"] for it in S.ordered_waiting(st)][1] == "ray"


# ── counts: out of Open, and not gaming the percentage ──

def test_waiting_leaves_the_open_count_and_the_denominator(path):
    st, _ = S.load_state(path)
    before = S.counts(st)
    S.set_waiting("mgp", "Marconio", path=path)
    st, _ = S.load_state(path)
    after = S.counts(st)
    assert after["waiting"] == 1
    assert after["open"] == before["open"] - 1
    # 3 items, 1 waiting, 0 done → 0% of 2, not 0% of 3. The percentage must
    # describe work she can actually finish.
    assert after["pct"] == 0
    S.set_done("ray", True, path=path)
    st, _ = S.load_state(path)
    assert S.counts(st)["pct"] == 50


def test_waiting_is_excluded_from_the_five_that_matter(path):
    S.set_waiting("mgp", "Marconio", path=path)
    st, _ = S.load_state(path)
    assert "mgp" not in [it["id"] for it in S.do_today(st)]


def test_done_leaves_the_waiting_space_immediately(path):
    S.set_waiting("mgp", "Marconio", path=path)
    S.set_done("mgp", True, path=path)
    st, _ = S.load_state(path)
    assert S.waiting_items(st) == []
    # …but the record of how it got done is kept on the card.
    assert S.get_item(st, "mgp")["waiting"]["who"] == "Marconio"


# ── unblocking ──

def test_clear_waiting_preserves_the_log_into_her_note(path):
    S.set_waiting("mgp", "Marconio", what="the COI", first_update="emailed",
                  path=path)
    S.add_waiting_update("mgp", "he says Friday", path=path)
    S.clear_waiting("mgp", reason="COI arrived", path=path)
    it = _get(path, "mgp")
    assert it["waiting"] is None
    for fragment in ("Marconio", "the COI", "emailed", "he says Friday",
                     "COI arrived"):
        assert fragment in it["note"], fragment
    assert S.is_waiting(it) is False


def test_clear_waiting_appends_to_an_existing_note(path):
    S.set_note("mgp", "her earlier note", path=path)
    S.set_waiting("mgp", "Marconio", path=path)
    S.clear_waiting("mgp", path=path)
    assert _get(path, "mgp")["note"].startswith("her earlier note")


def test_clear_on_a_non_waiting_item_is_refused(path):
    assert S.clear_waiting("plain", path=path)["error"]


# ── the roll ──

def test_waiting_survives_the_roll_and_is_counted(path):
    S.set_waiting("mgp", "Marconio", nudge_on="2026-07-31", path=path)
    res = S.roll_forward("2026-07-30", path=path)
    assert res["waiting"] == 1
    it = _get(path, "mgp")
    assert S.is_waiting(it)
    assert it["waiting"]["log"] == []          # nothing invented by the roll
    assert it["waiting"]["nudge_on"] == "2026-07-31"


def test_the_roll_does_not_escalate_a_waiting_item_to_urgent(path):
    """A waiting item must never be shouted about as Urgent.

    That was the old tag's cruelty: three days of "waiting for someone" turned
    into a red card for work she still could not do.
    """
    S.set_waiting("ray", "Ray", path=path)
    for _ in range(4):
        S.roll_forward(path=path)   # same-day rolls are no-ops; explicit below
    S.roll_forward("2026-07-30", path=path)
    S.roll_forward("2026-07-31", path=path)
    S.roll_forward("2026-08-01", path=path)
    it = _get(path, "ray")
    assert it["defer_days"] == 0
    assert it["lane"] != "urgent"


# ── the legacy defer tag ──

def test_legacy_defer_waiting_migrates_on_load(path):
    st, _ = S.load_state(path)
    S.get_item(st, "ray")["defer"] = "waiting"
    S.get_item(st, "ray")["defer_days"] = 2
    S.save_state(st, path)

    it = _get(path, "ray")                     # load_state normalizes
    assert it["defer"] is None and it["defer_days"] == 0
    w = it["waiting"]
    # `who` is UNKNOWN, never guessed — the old tag recorded no person, and
    # inventing one would fabricate a fact about her work.
    assert w["who"] == "" and w["_needs_who"] is True
    assert len(w["log"]) == 1 and "never recorded" in w["log"][0]["text"]
    assert S.is_waiting(it)


def test_migrated_item_can_be_named_afterwards(path):
    st, _ = S.load_state(path)
    S.get_item(st, "ray")["defer"] = "waiting"
    S.save_state(st, path)
    S.load_state(path)
    assert S.set_waiting_who("ray", "South Shore", path=path)["ok"]
    w = _get(path, "ray")["waiting"]
    assert w["who"] == "South Shore" and w["_needs_who"] is False
    assert S.set_waiting_who("ray", "  ", path=path)["error"]


def test_waiting_is_refused_as_a_defer_reason_everywhere(path):
    """Both write paths refuse it, not just the HTTP one.

    Guarding only the server would leave every script free to recreate the
    ownerless-waiting state the space was built to eliminate.
    """
    assert S.set_defer("plain", "waiting", path=path)["error"]
    res = S.apply_click("plain", {"defer": "waiting"}, path=path)
    assert res["error"] and res["needs_waiting"] is True
    it = _get(path, "plain")
    assert it["defer"] is None and it["waiting"] is None
    assert "waiting" not in S.DEFER_REASONS


def test_malformed_waiting_block_cannot_crash_the_board(path):
    st, _ = S.load_state(path)
    S.get_item(st, "mgp")["waiting"] = "not a dict"
    S.get_item(st, "ray")["waiting"] = {"who": "Ray", "log": "not a list"}
    S.save_state(st, path)
    st, _ = S.load_state(path)
    assert S.get_item(st, "mgp")["waiting"] is None
    assert S.get_item(st, "ray")["waiting"]["log"] == []
    S.counts(st)          # must not raise


# ══════════════════════════════════════════════════════════════════════════
# Delegation to Claude (2026-07-29)
#
# Hadassa: "what about the 'assigned away' that goes to claude? shouldn't them be
# as done as well and flagged once done that was Claude by my order? They
# shouldn't be kept together with the other assigned away ones that are for other
# people. and once I click 'claude' to run that task by itself, how will I know
# it'll be done in time? when will you know that you're supposed to do it?"
# ══════════════════════════════════════════════════════════════════════════

def test_delegating_to_claude_stamps_when_the_clock_started(path):
    now = datetime.datetime(2026, 7, 29, 17, 0).astimezone()
    S.apply_click("mgp", {"assignee": "claude"}, path=path, now=now)
    it = _get(path, "mgp")
    assert it["claude_queued_at"].startswith("2026-07-29T17:00")
    # Her question was about TIME. Time is unanswerable without this stamp.
    later = datetime.datetime(2026, 7, 30, 5, 0).astimezone()
    assert round(S.hours_queued(it, now=later)) == 12


def test_requeueing_does_not_reset_the_clock(path):
    """A re-render or a second click must not make an old item look fresh."""
    t0 = datetime.datetime(2026, 7, 29, 9, 0).astimezone()
    S.apply_click("mgp", {"assignee": "claude"}, path=path, now=t0)
    first = _get(path, "mgp")["claude_queued_at"]
    S.apply_click("mgp", {"assignee": "claude"},
                  path=path, now=datetime.datetime(2026, 7, 29, 15, 0).astimezone())
    assert _get(path, "mgp")["claude_queued_at"] == first


def test_taking_it_back_clears_the_clock(path):
    S.apply_click("mgp", {"assignee": "claude"}, path=path)
    S.apply_click("mgp", {"assignee": "hadassa"}, path=path)
    it = _get(path, "mgp")
    assert it["assignee"] is None and it["claude_queued_at"] is None


def test_unstamped_legacy_item_reports_none_not_zero(path):
    """Cards delegated before the field existed must not look freshly queued."""
    st, _ = S.load_state(path)
    S.get_item(st, "mgp")["assignee"] = "claude"
    S.save_state(st, path)
    assert S.hours_queued(_get(path, "mgp")) is None


def test_claude_queue_is_oldest_first_and_open_only(path):
    S.apply_click("ray", {"assignee": "claude"}, path=path,
                  now=datetime.datetime(2026, 7, 29, 14, 0).astimezone())
    S.apply_click("mgp", {"assignee": "claude"}, path=path,
                  now=datetime.datetime(2026, 7, 29, 9, 0).astimezone())
    S.apply_click("plain", {"assignee": "claude"}, path=path)
    S.complete_by_claude("plain", "finished it", path=path)
    st, _ = S.load_state(path)
    assert [i["id"] for i in S.claude_queue(st)] == ["mgp", "ray"]


def test_complete_by_claude_records_who_and_what(path):
    S.apply_click("mgp", {"assignee": "claude"}, path=path)
    res = S.complete_by_claude("mgp", "Sent the COI request and filed the reply.",
                               path=path)
    assert res["ok"] and res["done_by"] == "claude"
    it = _get(path, "mgp")
    assert it["status"] == "done"
    assert it["done_by"] == "claude"
    assert it["claude_result"] == "Sent the COI request and filed the reply."


def test_claude_cannot_close_work_she_never_delegated(path):
    """Otherwise Claude would be deciding her priorities, not executing them."""
    res = S.complete_by_claude("plain", "did it", path=path)
    assert res["error"]
    assert _get(path, "plain")["status"] == "open"


def test_a_bare_done_with_no_result_is_refused(path):
    S.apply_click("mgp", {"assignee": "claude"}, path=path)
    assert S.complete_by_claude("mgp", "   ", path=path)["error"]
    assert _get(path, "mgp")["status"] == "open"


def test_her_own_tick_is_never_credited_to_claude(path):
    """The board is a record of HER day; the daily report is built off it."""
    S.apply_click("mgp", {"assignee": "claude"}, path=path)
    S.apply_click("mgp", {"done": True}, path=path)      # she ticks it herself
    it = _get(path, "mgp")
    assert it["done_by"] == "hadassa"
    assert it["claude_result"] is None


def test_claude_completion_is_not_credited_to_her(path):
    S.apply_click("ray", {"assignee": "claude"}, path=path)
    S.complete_by_claude("ray", "Drafted and filed.", path=path)
    assert _get(path, "ray")["done_by"] == "claude"
    # …and reopening clears the attribution rather than leaving a stale claim.
    S.apply_click("ray", {"done": False}, path=path)
    assert _get(path, "ray")["done_by"] is None
