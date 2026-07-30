#!/usr/bin/env python3
"""Tests for the card update stream (2026-07-30).

Two of Hadassa's asks turned out to be one mechanism:

  · "an update box on all cards" — record what happened as the day moves.
  · "if they ask me if I did something and I don't remember, I need to have
     that somewhere to confirm."

Both are "append a dated line to this card", so there is one append-only stream
with two entry points: typed any time, or prompted at the tick.

What these tests LOCK, beyond the happy path:

  1. APPEND-ONLY. Nothing in this module may shorten the stream, reorder it, or
     rewrite an entry. This is the entire value: a log that can be edited
     afterwards is not evidence that she did something on a given day.
  2. `set_did` writes BOTH — the `did` headline the daily report reads AND a
     permanent entry. That asymmetry is deliberate: `did` is in PATCHABLE and so
     can be overwritten later, and when it is, the earlier wording must still
     stand in the stream.
  3. Blank text is refused, so a stray Enter cannot mint an empty entry that
     inflates the count on a card with no real record.
  4. The stream survives the day-roll into the archive — looking a FINISHED
     item up later is the whole point of it.

Run:  cd ~/rfs_pm && python3 -m pytest tests -q
"""
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
    st = S.blank_state(brief_date="2026-07-30")
    st["items"] = [
        S.new_item("tess", "104 Child CO 0015 — reissue check", lane="urgent"),
        S.new_item("mgp", "MGP check #3834", lane="action"),
    ]
    S.save_state(st, p)
    return p


def _get(path, iid):
    st, _ = S.load_state(path)
    return S.get_item(st, iid)


# ── the basic write ──

def test_new_item_starts_with_an_empty_stream(path):
    """A list, never None — every reader must be able to iterate unguarded."""
    assert _get(path, "tess")["updates"] == []


def test_add_update_appends_a_stamped_entry(path):
    res = S.add_update("tess", "Called Tess, she reissues Monday", path=path)
    assert res["ok"] and res["updates"] == 1
    log = _get(path, "tess")["updates"]
    assert len(log) == 1
    assert log[0]["text"] == "Called Tess, she reissues Monday"
    assert log[0]["kind"] == "update"
    assert log[0]["at"], "every entry carries its own timestamp"


def test_blank_text_is_refused_and_writes_nothing(path):
    for blank in (None, "", "   ", "\n\t "):
        res = S.add_update("tess", blank, path=path)
        assert res["error"], "an empty entry would inflate the count for nothing"
    assert _get(path, "tess")["updates"] == []


def test_unknown_item_returns_none_not_a_new_card(path):
    assert S.add_update("nope", "text", path=path) is None


def test_text_is_stripped(path):
    S.add_update("tess", "   spoke to her   ", path=path)
    assert _get(path, "tess")["updates"][0]["text"] == "spoke to her"


# ── APPEND-ONLY: the property the whole mechanism rests on ──

def test_entries_accumulate_in_order_and_nothing_is_replaced(path):
    for n in ("first", "second", "third"):
        S.add_update("tess", n, path=path)
    log = _get(path, "tess")["updates"]
    assert [e["text"] for e in log] == ["first", "second", "third"]


def test_no_function_in_the_module_can_shorten_a_stream(path):
    """The guarantee is structural, so assert it against the whole API surface.

    If someone later adds an edit or delete path, this test is what fails.
    """
    for n in ("one", "two", "three"):
        S.add_update("tess", n, path=path)
    before = list(_get(path, "tess")["updates"])

    # Everything else the UI can reach on a card.
    S.patch_content("tess", {"subject": "renamed", "did": "overwritten"}, path=path)
    S.apply_click("tess", {"done": True, "note": "a note"}, path=path)
    S.apply_click("tess", {"done": False}, path=path)
    S.set_waiting("tess", "Tess", what="the signed CO", path=path)
    S.add_waiting_update("tess", "chased her", path=path)
    S.clear_waiting("tess", reason="she signed", path=path)

    after = _get(path, "tess")["updates"]
    assert after == before, "no card operation may rewrite or drop an entry"


def test_a_later_did_edit_leaves_the_earlier_wording_standing(path):
    """`did` is patchable; the stream is not. That is why both exist."""
    S.add_update("tess", "Called her, reissuing Monday",
                 kind="done", set_did=True, path=path)
    S.patch_content("tess", {"did": "Sorted it"}, path=path)
    it = _get(path, "tess")
    assert it["did"] == "Sorted it"
    assert it["updates"][0]["text"] == "Called her, reissuing Monday"


# ── the tick-time entry point ──

def test_set_did_writes_both_the_headline_and_the_record(path):
    res = S.add_update("mgp", "Handed the check to MGP",
                       kind="done", set_did=True, path=path)
    it = _get(path, "mgp")
    assert it["did"] == "Handed the check to MGP"
    assert it["updates"][0]["kind"] == "done"
    assert it["updates"][0]["text"] == "Handed the check to MGP"
    assert res["did"] == "Handed the check to MGP"


def test_without_set_did_the_headline_is_left_alone(path):
    S.add_update("mgp", "Rafael says wait", path=path)
    assert _get(path, "mgp")["did"] is None


def test_an_unknown_kind_falls_back_to_update_rather_than_storing_junk(path):
    S.add_update("mgp", "text", kind="banana", path=path)
    assert _get(path, "mgp")["updates"][0]["kind"] == "update"


# ── it has to survive the day ──

def test_the_stream_survives_the_day_roll_into_the_archive(path):
    """Looking a finished item up LATER is the point; the roll must carry it."""
    S.add_update("mgp", "Handed the check to MGP",
                 kind="done", set_did=True, path=path)
    S.apply_click("mgp", {"done": True}, path=path)
    # An explicit next-day target: the roll is idempotent per brief_date, so
    # rolling to the fixture's own date is a legitimate no-op.
    S.roll_forward(to_date="2026-07-31", path=path)

    rows = S.history_between("0000-00-00", "9999-99-99")
    archived = [r for r in rows if r["id"] == "mgp"]
    assert archived, "the finished card should be in the archive"
    assert archived[0]["updates"][0]["text"] == "Handed the check to MGP"
    assert archived[0]["did"] == "Handed the check to MGP"


def test_normalize_backfills_the_field_on_a_pre_existing_card(path):
    """Her live state predates this field — loading it must not crash a reader."""
    st, _ = S.load_state(path)
    del st["items"][0]["updates"]
    S.save_state(st, path)

    it = _get(path, "tess")
    assert it["updates"] == [], "normalize() heals an older file"
    assert S.add_update("tess", "still writable", path=path)["ok"]


def test_backfilled_cards_do_not_share_one_list(path):
    """REGRESSION 2026-07-30 — the bug this mechanism exposed.

    normalize() built its default item ONCE and handed the same list object to
    every card missing the key, so the first in-place append wrote to all of
    them: one update posted to one card showed up on all 54, same timestamp.

    Reproduced exactly as it happened: strip the key from BOTH cards (the state
    her live board was in), let normalize() backfill, then write to one.
    """
    st, _ = S.load_state(path)
    for it in st["items"]:
        it.pop("updates", None)
    S.save_state(st, path)

    S.add_update("tess", "only this card", path=path)

    assert [e["text"] for e in _get(path, "tess")["updates"]] == ["only this card"]
    assert _get(path, "mgp")["updates"] == [], "an append must not leak across cards"


def test_no_two_cards_share_any_mutable_default(path):
    """The general form — the aliasing was never specific to `updates`.

    ctx_body, links, pills, where, claude_done and hadassa_todo were aliased in
    exactly the same way and survived only because the ingest replaces them
    wholesale instead of appending. Assert identity across the whole item, so a
    future default list cannot reintroduce this.
    """
    st, _ = S.load_state(path)
    for it in st["items"]:
        for k in list(it):
            if isinstance(it[k], (list, dict)):
                it.pop(k, None)
    S.save_state(st, path)

    st, _ = S.load_state(path)
    a, b = S.get_item(st, "tess"), S.get_item(st, "mgp")
    shared = [k for k in a
              if isinstance(a.get(k), (list, dict)) and a[k] is b.get(k)]
    assert not shared, "these defaults are one shared object: %s" % shared
