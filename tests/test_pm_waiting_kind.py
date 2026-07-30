#!/usr/bin/env python3
"""Tests for `waiting.kind` — a reply, or the work (2026-07-30).

Her distinction, in her words: a person-lane card is "either 'pending response'
or 'pending task done by the person' — do you get the difference?"

Both mean she cannot move the card, so both belong in the waiting space. The
only reason to record WHICH is that they are not chased the same way, so what
these tests lock is that the label actually changes behaviour rather than
decorating the card:

  1. The kind sets the FUSE. "a reply" comes back in 2 days, "the work" in 3.
     A label that does not change when the card resurfaces is not worth a click.
  2. An EXPLICIT date always beats the kind's default. She can say "chase
     Friday" about anything, and a convenience default must never overwrite a
     decision she made herself.
  3. An unknown or missing kind is None, never a guess and never a crash. Every
     card that predates the field lands here, and her rule for skipping the
     question was that it stays unlabelled — an absent answer must stay absent.
  4. Labelling APPENDS to the chase log. "What am I even waiting for" is
     precisely the question that log exists to answer three weeks later, and the
     answer arriving is itself an event.
  5. `renudge=False` leaves her date alone. Re-labelling an old card must not
     silently move a chase date she chose — the quiet UI control passes this.
  6. Nothing here may shorten the log. The append-only guarantee of the waiting
     space extends to every new writer, or it stops being evidence.

Run:  cd ~/rfs_pm && python3 -m pytest tests -q
"""
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pm_state as S  # noqa: E402

NOW = datetime.datetime(2026, 7, 30, 16, 30).astimezone()


@pytest.fixture
def path(tmp_path, monkeypatch):
    p = tmp_path / "pm_state.json"
    monkeypatch.setattr(S, "LOCK_PATH", tmp_path / ".lock")
    monkeypatch.setattr(S, "HISTORY_DIR", tmp_path / "history")
    st = S.blank_state(brief_date="2026-07-30")
    st["items"] = [
        S.new_item("eliezer", "Eliezer's trade — 99 Concord", lane="action"),
        S.new_item("robin", "70 Robin inv 0004 rewrite", lane="action"),
        S.new_item("plain", "Something she can just do", lane="action"),
    ]
    S.save_state(st, p)
    return p


def _w(path, iid):
    st, _ = S.load_state(path)
    return S.get_item(st, iid)["waiting"]


# ── 1. the kind sets the fuse ──

@pytest.mark.parametrize("kind,due", [("response", "2026-08-01"),
                                      ("task", "2026-08-02")])
def test_kind_sets_the_chase_fuse(path, kind, due):
    """A reply rots faster than work in progress, so it comes back sooner."""
    S.set_waiting("eliezer", "Guilherme", kind=kind, path=path, now=NOW)
    w = _w(path, "eliezer")
    assert w["kind"] == kind
    assert w["nudge_on"] == due, "the label must change when the card resurfaces"


def test_no_kind_keeps_the_default_fuse(path):
    S.set_waiting("eliezer", "Guilherme", path=path, now=NOW)
    w = _w(path, "eliezer")
    assert w["kind"] is None
    assert w["nudge_on"] == "2026-08-02"      # DEFAULT_NUDGE_DAYS = 3


def test_the_two_fuses_actually_differ(path):
    """Guards against both kinds collapsing onto one number in a later edit —
    at which point the question would cost her a click and buy nothing."""
    assert S.WAITING_KINDS["response"] != S.WAITING_KINDS["task"]
    assert set(S.WAITING_KINDS) == set(S.WAITING_KIND_LABELS)


# ── 2. an explicit date wins ──

def test_explicit_nudge_beats_the_kind_default(path):
    S.set_waiting("eliezer", "Guilherme", kind="response",
                  nudge_on="2026-08-14", path=path, now=NOW)
    assert _w(path, "eliezer")["nudge_on"] == "2026-08-14", \
        "a convenience default must never overwrite a date she chose"


# ── 3. unknown/missing is None, never a guess ──

@pytest.mark.parametrize("bad", ["", "reply", "RESPONSE", "garbage", 0, [], {}])
def test_an_unrecognised_kind_becomes_none_on_entry(path, bad):
    S.set_waiting("eliezer", "Guilherme", kind=bad, path=path, now=NOW)
    w = _w(path, "eliezer")
    assert w["kind"] is None
    assert w["nudge_on"] == "2026-08-02", "and it falls back to the default fuse"


def test_normalize_heals_a_kind_written_by_hand(path):
    """A card edited on disk, or written by an older build, must not crash the
    board or render a kind the UI has no label for."""
    st, _ = S.load_state(path)
    it = S.get_item(st, "eliezer")
    it["waiting"] = {"who": "Guilherme", "kind": "whatever-this-is", "log": []}
    S.save_state(st, path)
    assert _w(path, "eliezer")["kind"] is None


def test_a_waiting_card_predating_the_field_reads_as_unlabelled(path):
    st, _ = S.load_state(path)
    S.get_item(st, "eliezer")["waiting"] = {
        "who": "Guilherme", "what": "", "since": "2026-07-29T10:00:00-04:00",
        "nudge_on": "2026-08-05", "log": [{"at": "2026-07-29T10:00:00-04:00",
                                          "text": "asked him"}]}
    S.save_state(st, path)
    w = _w(path, "eliezer")
    assert w["kind"] is None, "no kind is not the same as a kind of None-ish"
    assert w["nudge_on"] == "2026-08-05", "and healing must not move her date"
    assert len(w["log"]) == 1, "nor touch the log"


# ── 4/5. labelling after the fact ──

def test_set_waiting_kind_labels_renudges_and_appends(path):
    S.set_waiting("eliezer", "Guilherme", first_update="Parked with Guilherme",
                  path=path, now=NOW)
    before = _w(path, "eliezer")
    assert before["nudge_on"] == "2026-08-02"

    res = S.set_waiting_kind("eliezer", "response", path=path, now=NOW)
    assert res["ok"] is True
    w = _w(path, "eliezer")
    assert w["kind"] == "response"
    assert w["nudge_on"] == "2026-08-01", "answering re-arms the fuse it implies"
    assert len(w["log"]) == 2, "the answer is itself an event on the log"
    assert "a reply" in w["log"][-1]["text"]
    assert w["log"][0]["text"] == "Parked with Guilherme", "append-only"


def test_renudge_false_leaves_her_own_date_alone(path):
    S.set_waiting("eliezer", "Guilherme", nudge_on="2026-08-20",
                  path=path, now=NOW)
    S.set_waiting_kind("eliezer", "task", renudge=False, path=path, now=NOW)
    w = _w(path, "eliezer")
    assert w["kind"] == "task"
    assert w["nudge_on"] == "2026-08-20", \
        "re-labelling an old card must not silently move a date she picked"


def test_set_waiting_kind_refuses_a_kind_it_has_no_label_for(path):
    S.set_waiting("eliezer", "Guilherme", path=path, now=NOW)
    for bad in (None, "", "reply", "both", 3):
        res = S.set_waiting_kind("eliezer", bad, path=path, now=NOW)
        assert res["error"], "a kind the UI cannot render must not be stored"
    assert _w(path, "eliezer")["kind"] is None


def test_set_waiting_kind_refuses_a_card_not_in_the_space(path):
    res = S.set_waiting_kind("plain", "response", path=path, now=NOW)
    assert res["error"], "there is no fuse to set on a card that is not waiting"
    st, _ = S.load_state(path)
    assert S.get_item(st, "plain")["waiting"] is None


def test_unknown_item_is_distinguishable_from_a_refusal(path):
    """None (→ HTTP 404) and {"error": …} (→ 400) must not be conflated, or a
    typo'd id reads to the UI as a rejected value."""
    assert S.set_waiting_kind("no-such-card", "response", path=path) is None


# ── 6. append-only, under repetition ──

def test_relabelling_only_ever_grows_the_log(path):
    S.set_waiting("robin", "Rafael", first_update="asked for the rewrite",
                  path=path, now=NOW)
    lens = []
    for k in ("response", "task", "response"):
        S.set_waiting_kind("robin", k, path=path, now=NOW)
        lens.append(len(_w(path, "robin")["log"]))
    assert lens == [2, 3, 4], "no writer in this space may shorten the log"
    assert _w(path, "robin")["log"][0]["text"] == "asked for the rewrite"
    assert _w(path, "robin")["kind"] == "response", "last answer wins on the field"


def test_the_kind_survives_a_save_load_round_trip(path):
    S.set_waiting("robin", "Rafael", kind="task", path=path, now=NOW)
    st, _ = S.load_state(path)
    S.save_state(st, path)
    assert _w(path, "robin")["kind"] == "task"


def test_labelling_does_not_disturb_the_rest_of_the_block(path):
    S.set_waiting("robin", "Rafael", what="the rewritten invoice",
                  path=path, now=NOW)
    since = _w(path, "robin")["since"]
    S.set_waiting_kind("robin", "task", path=path, now=NOW)
    w = _w(path, "robin")
    assert w["who"] == "Rafael"
    assert w["what"] == "the rewritten invoice"
    assert w["since"] == since, "the clock on how long she has waited must not reset"
    assert w["_needs_who"] is False


def test_a_labelled_card_is_still_waiting_and_still_open(path):
    """The whole space depends on waiting being a sub-state of open — the roll,
    the archive and the daily report all read `status`."""
    S.set_waiting("robin", "Rafael", kind="response", path=path, now=NOW)
    st, _ = S.load_state(path)
    it = S.get_item(st, "robin")
    assert S.is_waiting(it) is True
    assert it["status"] == "open"
