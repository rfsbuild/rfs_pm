#!/usr/bin/env python3
"""The routine lane must RESET every morning, and only ever touch its own cards.

WHY THE LANE IS SEEDED RATHER THAN ROLLED
-----------------------------------------
The routine resets daily; the board's cards persist until finished. The board's
own daily reset — roll_forward() — has run exactly ONCE in its life (`roll_log`
holds one entry, `brief_date` froze at 2026-07-29, 26 days before this was
written). A daily-resetting lane hung off a mechanism that has not fired in 26
days would be right on day one and wrong every day after, so the seeding runs
from the 08:00 job that already fires and already knows the day.

WHAT THESE LOCK
---------------
1. RESET IS THE POINT. A ticked card comes back open tomorrow. pm_ingest
   refuses to touch `status` on purpose — that is correct for a collector and
   fatal here, so this module writes state directly, and that must stay true.
2. HER NOTE SURVIVES. A note on "Check bank info and reconcile" is a standing
   remark about the task, not about one Tuesday. Wiping it each morning would
   teach her not to write them.
3. IT CLEANS UP ONLY AFTER ITSELF. Yesterday's plan items go; a card someone
   else put in the routine lane does NOT, because this module owns the `rt_`
   prefix and nothing else.
4. A BROKEN FIXED-ITEMS FILE IS LOUD. Returning [] would seed a thin lane that
   looks like a quiet morning — the same silent-empty failure the sweep treats
   as an error.
5. THE FOUR FIXED ITEMS ARE NOT RESTATED HERE. They are read from the same
   routine_morning.json that daily_routine.html reads. A second copy is how
   two surfaces drift apart.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pm_state as S      # noqa: E402
import pm_routine as RT   # noqa: E402


@pytest.fixture
def board(tmp_path):
    p = tmp_path / "pm_state.json"
    S.save_state(S.blank_state("2026-08-24"), p)
    return p


@pytest.fixture
def sources(tmp_path, monkeypatch):
    """Point the module at throwaway morning/plan files."""
    morning = tmp_path / "routine_morning.json"
    morning.write_text(json.dumps({"items": [
        {"id": "m1", "name": "Check bank info and reconcile", "sub": "Citizens + Cap One"},
        {"id": "m2", "name": "Update receipts to QB", "sub": "then cross-check"},
    ]}))
    plans = tmp_path / "routine"
    plans.mkdir()
    plans.joinpath("plan-2026-08-24.json").write_text(json.dumps({"items": [
        {"id": "payroll_hours_check", "name": "Check the generator's hours",
         "sub": "before Approve & Save", "why": "wk-8/15 stored 46.25h"},
    ]}))
    plans.joinpath("plan-2026-08-25.json").write_text(json.dumps({"items": [
        {"id": "rafael_weekly", "name": "Rafael's weekly report", "sub": "one repo push"},
    ]}))
    monkeypatch.setattr(RT, "MORNING_PATH", morning)
    monkeypatch.setattr(RT, "PLAN_DIR", plans)
    return tmp_path


def _ids(path):
    st, _ = S.load_state(path)
    return {i["id"] for i in st["items"]}


def _item(path, iid):
    st, _ = S.load_state(path)
    return S.get_item(st, iid)


def test_seeds_fixed_items_and_the_days_plan(board, sources):
    RT.seed("2026-08-24", path=board)
    assert _ids(board) == {"rt_m1", "rt_m2", "rt_payroll_hours_check"}
    assert _item(board, "rt_m1")["lane"] == "routine"
    assert _item(board, "rt_m1")["status"] == "open"


def test_a_ticked_card_comes_back_open_the_next_morning(board, sources):
    """The whole point. pm_ingest would preserve `done` and break the checklist."""
    RT.seed("2026-08-24", path=board)
    S.set_done("rt_m1", True, path=board)
    assert _item(board, "rt_m1")["status"] == "done"
    RT.seed("2026-08-25", path=board)
    assert _item(board, "rt_m1")["status"] == "open", (
        "a ticked routine card stayed done into the next day — the lane is a "
        "checklist that can be ticked once, ever")
    assert not _item(board, "rt_m1").get("done_at"), "stale done_at survived the reset"


def test_her_note_survives_the_daily_reset(board, sources):
    RT.seed("2026-08-24", path=board)
    S.set_note("rt_m1", "ask Rafael about the Citizens hold", path=board)
    RT.seed("2026-08-25", path=board)
    assert _item(board, "rt_m1")["note"] == "ask Rafael about the Citizens hold", (
        "her standing note was destroyed by the morning reset")


def test_yesterdays_plan_items_are_removed(board, sources):
    RT.seed("2026-08-24", path=board)
    assert "rt_payroll_hours_check" in _ids(board)
    RT.seed("2026-08-25", path=board)
    ids = _ids(board)
    assert "rt_payroll_hours_check" not in ids, (
        "yesterday's plan item survived; the lane becomes an archive of every "
        "ad-hoc item ever planned")
    assert "rt_rafael_weekly" in ids
    assert {"rt_m1", "rt_m2"} <= ids, "the FIXED items must never be removed"


def test_it_never_deletes_a_card_it_did_not_mint(board, sources):
    """Ownership is the `rt_` prefix, not the lane."""
    st, _ = S.load_state(board)
    st["items"].append(S.new_item("someone_elses_card", "not mine"))
    st["items"][-1]["lane"] = "routine"
    S.save_state(st, board)
    RT.seed("2026-08-24", path=board)
    assert "someone_elses_card" in _ids(board), (
        "a routine-lane card this module did not create was deleted")


def test_an_unreadable_fixed_items_file_raises_rather_than_seeding_thin(board, sources, tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text("{not json")
    RT.MORNING_PATH = bad
    with pytest.raises(RuntimeError):
        RT.seed("2026-08-24", path=board)


def test_an_empty_fixed_items_file_raises(board, sources, tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"items": []}))
    RT.MORNING_PATH = empty
    with pytest.raises(RuntimeError):
        RT.seed("2026-08-24", path=board)


def test_a_missing_plan_file_is_a_normal_day(board, sources):
    """No plan is normal — the four fixed items still stand on their own."""
    res = RT.seed("2026-12-25", path=board)
    assert res["ok"]
    assert _ids(board) == {"rt_m1", "rt_m2"}


def test_dry_run_writes_nothing(board, sources):
    before = _ids(board)
    res = RT.seed("2026-08-24", path=board, dry_run=True)
    assert res["dry_run"] and res["added"]
    assert _ids(board) == before, "--dry-run mutated the board"


def test_the_fixed_items_are_not_restated_in_this_repo(board):
    """One source of truth: rfs_dashboard/routine_morning.json."""
    src = (ROOT / "pm_routine.py").read_text()
    assert "routine_morning.json" in src
    for phrase in ("Check bank info", "Update receipts", "QuickBooks Time",
                   "Upload photos"):
        assert phrase not in src, (
            "%r is hardcoded in pm_routine.py — that is a second copy of the "
            "morning list, and two copies drift" % phrase)


def test_routine_is_a_real_lane_and_counts_toward_her_day():
    assert "routine" in S.LANES
    assert S.LANE_LABELS.get("routine")
    assert "routine" in S.ACTIONABLE_LANES, (
        "ticking all four morning items would move the day's progress ring by "
        "nothing")
    assert S.LANES.index("routine") == 1, (
        "routine must sit directly below urgent — it refills every morning, so "
        "first place would push a red item down the page every day")


def test_the_ui_renders_the_lane_it_is_given():
    """pm_ui.html keeps its OWN lane map; Python alone is not enough."""
    ui = (ROOT / "pm_ui.html").read_text()
    assert 'routine:"' in ui, (
        "pm_ui.html's LANE_LABEL has no `routine` key, so the board view would "
        "render its group heading as `undefined`")
    assert 'const TODAY_LANES=' in ui, "the Today tab's lane list is not a constant"
    assert '"routine"' in ui.split("const TODAY_LANES=")[1].split("\n")[0], (
        "TODAY_LANES omits `routine`, so the morning checklist would not appear "
        "on the tab she opens at 08:00")
    # The badge and the body must be the SAME expression. The "five that matter"
    # strip keeps its own urgent+action literal on purpose, so this counts the
    # TODAY_LANES uses rather than banning the literal outright.
    assert ui.count("TODAY_LANES.includes") == 1 and ui.count("TODAY_LANES.forEach") == 1, (
        "the Today badge and the Today body must each read TODAY_LANES exactly "
        "once — two separate literals is how a tab badged 4 rendered 11")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
