#!/usr/bin/env python3
"""The sweep prompt must CONTAIN the board it is told not to restate (2026-08-24).

WHAT WENT WRONG
---------------
`pm_sweep_run.PROMPT` has always ended STEP 4 with:

    Only NEW or genuinely-changed items. Do not restate cards the board has.

But `build_prompt()` formatted the prompt with exactly four values —
`slack_wm`, `gmail_wm`, `registry`, `out`. The board was never in it. Not the
ids, not the subjects, not even the count.

So the model was handed an instruction it had no information to obey. To
decide "does the board already have this?" it had to guess, and to write an
`id` for a thread it had carded before it had to RE-DERIVE that id from the
subject on every run. Semantic naming made the guess land often enough to look
healthy — `wyman_quotes` recurs identically across six briefings — but that is
luck, not a mechanism. One near-miss (`prism_coi` where the board holds
`s_prism_coi`) and the board has two cards for one thread.

WHY A DUPLICATE IS WORSE THAN CLUTTER
-------------------------------------
`pm_ingest` protects HER_FIELDS — status, done_at, did, note, assignee, defer
— by MATCHING THE ID. A duplicate minted under a different id inherits none of
them. So the failure is not "an extra card": it is a card she already worked,
already annotated, already marked **done**, reappearing as untouched new work.
At the time of the fix, 36 of the board's 107 cards were `done` or
`dismissed` — every one of them resurrectable this way, silently.

WHAT THIS LOCKS
---------------
1. The prompt CONTAINS the board — every id, in full, never truncated.
2. CLOSED cards (`done` / `dismissed`) are in it too. They are the ones that
   must not come back, so omitting them to save width would defeat the fix.
3. The rendering is COMPLETE — one row per item, no silent cap. A board that
   grows past some threshold must not start dropping rows, because a dropped
   row reads to the model exactly like "no card exists" and mints a duplicate.
4. `build_prompt` actually wires the board in — a `_board_text()` that renders
   perfectly but is never passed is the same defect wearing a nicer hat.

Tested against a synthetic state, not the live board, so the assertions stay
true as the real board changes.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pm_sweep_run as R  # noqa: E402


def _state(n_open=3, n_done=2, n_dismissed=1):
    items = []
    for i in range(n_open):
        items.append({"id": "open_card_%d" % i, "subject": "an open thing %d" % i,
                      "status": "open"})
    for i in range(n_done):
        items.append({"id": "done_card_%d" % i, "subject": "a finished thing %d" % i,
                      "status": "done"})
    for i in range(n_dismissed):
        items.append({"id": "dismissed_card_%d" % i, "subject": "a dropped thing %d" % i,
                      "status": "dismissed"})
    return {"items": items}


def _prompt(state):
    _wms, _out, prompt = R.build_prompt(state)
    return prompt


def test_every_id_reaches_the_prompt_in_full():
    """The id is the matching key. A truncated id is an unusable one."""
    st = _state()
    prompt = _prompt(st)
    missing = [it["id"] for it in st["items"] if it["id"] not in prompt]
    assert not missing, (
        "these card ids never reached the sweep prompt, so the model cannot "
        "reuse them and will mint duplicates: %r" % missing)


def test_closed_cards_are_shown_so_they_are_not_resurrected():
    """done/dismissed cards are the whole point — they must not come back."""
    st = _state()
    prompt = _prompt(st)
    closed = [it for it in st["items"]
              if (it.get("status") or "open") in ("done", "dismissed")]
    assert closed, "fixture is broken — it must contain closed cards"
    for it in closed:
        assert it["id"] in prompt, (
            "closed card %r is absent from the prompt; a sweep that cannot see "
            "it will raise it again as fresh open work, discarding the status, "
            "note and did that pm_ingest keys off the id" % it["id"])


def test_the_board_rendering_is_complete_not_capped():
    """One row per item at any size. A dropped row == 'no card exists'."""
    for n in (1, 7, 50, 250):
        st = _state(n_open=n, n_done=0, n_dismissed=0)
        rows = R._board_text(st).splitlines()
        assert len(rows) == n, (
            "board rendering dropped rows at %d items (%d rendered). A silently "
            "capped list tells the model a real card does not exist, which is "
            "exactly how the duplicate gets minted." % (n, len(rows)))


def test_build_prompt_actually_wires_the_board_in():
    """_board_text() that is never passed is the original bug, restyled."""
    st = _state()
    prompt = _prompt(st)
    board = R._board_text(st)
    first_row = board.splitlines()[0]
    assert first_row in prompt, (
        "build_prompt() does not embed _board_text() output — the rendering "
        "works but the prompt still does not carry the board")
    assert str(len(st["items"])) in prompt, (
        "the prompt never states how many cards exist, so the model cannot "
        "tell a complete list from a sample")


def test_empty_board_says_so_rather_than_rendering_nothing():
    """A blank section reads as 'the list was omitted', not 'there are none'."""
    text = R._board_text({"items": []})
    assert text.strip(), "an empty board rendered to whitespace"
    assert "empty" in text.lower()


def test_the_do_not_restate_instruction_is_no_longer_unobeyable():
    """The old sentence ordered the model to use knowledge it was never given."""
    assert "Do not restate cards the board has." not in R.PROMPT, (
        "STEP 4 still carries the standalone 'Do not restate cards the board "
        "has.' order. That sentence was the defect: it demanded a comparison "
        "against a board the prompt did not contain. It must be replaced by "
        "wording that points at the embedded board list.")
    assert "{board}" in R.PROMPT, (
        "PROMPT has no {board} placeholder, so no board can ever be formatted "
        "into it")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
