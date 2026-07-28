#!/usr/bin/env python3
"""Tests for pm_state — the carry-forward rules ported from pm_brief_ingest.py,
plus the write-safety guarantees the HTML pilot did not have.

Run:  cd ~/rfs_pm && python3 -m pytest tests -q
"""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pm_state as S  # noqa: E402


@pytest.fixture
def path(tmp_path, monkeypatch):
    p = tmp_path / "pm_state.json"
    monkeypatch.setattr(S, "LOCK_PATH", tmp_path / ".lock")
    st = S.blank_state(brief_date="2026-07-28")
    st["items"] = [
        S.new_item("a", "Open action", lane="action", kind="pay"),
        S.new_item("b", "Done thing", lane="action", status="done"),
        S.new_item("c", "Urgent one", lane="urgent", kind="compliance"),
        S.new_item("d", "Deferred", lane="action", defer="no-time", defer_days=1),
    ]
    S.save_state(st, p)
    return p


# ── carry-forward (the ported rules) ──

def test_roll_forward_drops_done_and_ages_the_rest(path):
    res = S.roll_forward("2026-07-29", path=path)
    st, _ = S.load_state(path)
    assert res["dropped"] == 1
    assert {i["id"] for i in st["items"]} == {"a", "c", "d"}
    assert all(i["age"] == 1 for i in st["items"])
    assert st["brief_date"] == "2026-07-29"


def test_roll_forward_escalates_after_three_defer_days(path):
    S.set_defer("d", "no-time", path=path)   # 2
    S.set_defer("d", "waiting", path=path)   # 3
    S.roll_forward("2026-07-29", path=path)
    st, _ = S.load_state(path)
    d = S.get_item(st, "d")
    assert d["defer_days"] == 3
    assert d["lane"] == "urgent"
    assert d["moved"] == "3+ days open"


def test_roll_forward_is_idempotent_for_one_day(path):
    S.roll_forward("2026-07-29", path=path)
    again = S.roll_forward("2026-07-29", path=path)
    st, _ = S.load_state(path)
    assert again.get("skipped")
    assert all(i["age"] == 1 for i in st["items"]), "a second roll must not double-age"


def test_roll_forward_clears_is_new(path):
    S.roll_forward("2026-07-29", path=path)
    st, _ = S.load_state(path)
    assert not any(i["is_new"] for i in st["items"])


def test_roll_log_records_what_happened(path):
    S.roll_forward("2026-07-29", path=path)
    st, _ = S.load_state(path)
    e = st["roll_log"][-1]
    assert e["from_date"] == "2026-07-28" and e["to_date"] == "2026-07-29"
    assert e["carried"] == 3 and e["dropped"] == 1


# ── ordering: her sink-to-bottom rule ──

def test_done_and_deferred_sink_below_open_work(path):
    st, _ = S.load_state(path)
    ranks = [S.sink_rank(i) for i in S.ordered_items(st, "action")]
    assert ranks == sorted(ranks), "open must precede deferred must precede done"
    assert S.sink_rank({"status": "open"}) == 0
    assert S.sink_rank({"status": "open", "defer": "no-time"}) == 1
    assert S.sink_rank({"status": "done"}) == 2


def test_do_today_excludes_done_and_caps(path):
    st, _ = S.load_state(path)
    for n in range(8):
        st["items"].append(S.new_item("x%d" % n, "extra %d" % n, lane="action"))
    S.save_state(st, path)
    st, _ = S.load_state(path)
    focus = S.do_today(st, limit=5)
    assert len(focus) == 5
    assert all(i["status"] == "open" for i in focus)
    assert focus[0]["id"] == "c", "urgent outranks action"


# ── lane derivation ──

@pytest.mark.parametrize("patch,expected", [
    ({"assignee": "rafael"}, "rafael"),
    ({"assignee": "claude"}, "claude"),
    ({"defer": "needs-rafael"}, "rafael"),
    ({"defer": "later-week"}, "week"),
    ({"defer_days": 3}, "urgent"),
    ({"assignee": "rafael", "defer_days": 4}, "urgent"),
])
def test_effective_lane(patch, expected):
    it = S.new_item("z", "s", lane="action")
    it.update(patch)
    assert S.effective_lane(it) == expected


# ── mutations persist ──

def test_every_mutation_hits_disk(path):
    S.set_done("a", True, path=path)
    S.set_note("a", "  she called back  ", path=path)
    S.set_assignee("a", "rafael", path=path)
    S.set_project("a", "70 Robin", path=path)
    raw = json.loads(open(path).read())
    it = [i for i in raw["items"] if i["id"] == "a"][0]
    assert it["status"] == "done" and it["done_at"]
    assert it["note"] == "she called back"      # trimmed
    assert it["assignee"] == "rafael"
    assert it["project"] == "70 Robin"


def test_assigning_to_hadassa_clears_the_assignee(path):
    S.set_assignee("a", "rafael", path=path)
    S.set_assignee("a", "hadassa", path=path)
    st, _ = S.load_state(path)
    assert S.get_item(st, "a")["assignee"] is None


def test_timestamps_carry_a_local_offset(path):
    """The HTML pilot wrote bare-UTC toISOString(), which made a 10:08 completion
    read as 14:08. Offsets are mandatory here."""
    S.set_done("a", True, path=path)
    st, _ = S.load_state(path)
    dt = S.parse_dt(S.get_item(st, "a")["done_at"])
    assert dt is not None and dt.tzinfo is not None


def test_upsert_never_overwrites_her_edits(path):
    S.set_done("a", True, path=path)
    S.set_note("a", "mine", path=path)
    S.upsert_item({"id": "a", "subject": "Refreshed by collector",
                   "action": "new action text", "status": "open", "note": "clobber"},
                  path=path)
    st, _ = S.load_state(path)
    it = S.get_item(st, "a")
    assert it["subject"] == "Refreshed by collector"   # source-owned: refreshed
    assert it["action"] == "new action text"
    assert it["status"] == "done" and it["note"] == "mine"  # hers: untouched


def test_unreadable_state_is_never_silently_replaced(path):
    open(path, "w").write("{ this is not json")
    os.remove(str(path) + ".bak") if os.path.exists(str(path) + ".bak") else None
    st, status = S.load_state(path)
    assert st is None and status == "unreadable"
    with pytest.raises(RuntimeError):
        S.set_done("a", True, path=path)


def test_corrupt_state_recovers_from_backup(path):
    S.set_note("a", "keep me", path=path)      # writes a .bak
    open(path, "w").write("{ corrupt")
    st, status = S.load_state(path)
    assert status == "recovered_backup"
    assert S.get_item(st, "a") is not None


def test_normalize_repairs_a_rogue_file(path):
    raw = json.loads(open(path).read())
    raw["items"][0]["lane"] = "nonsense"
    raw["items"][0]["age"] = "not-a-number"
    raw["items"][0].pop("defer_days")
    open(path, "w").write(json.dumps(raw))
    st, status = S.load_state(path)
    assert status == "ok"
    it = S.get_item(st, "a")
    assert it["lane"] == "action" and it["age"] == 0 and it["defer_days"] == 0


# ── concurrency: two tabs clicking at once ──

def test_concurrent_writers_do_not_lose_a_click(path, tmp_path):
    """20 processes each set a different item's note. Under a naive
    read-modify-write most would be lost; the flock makes all 20 survive."""
    st, _ = S.load_state(path)
    st["items"] = [S.new_item("i%02d" % n, "item %d" % n) for n in range(20)]
    S.save_state(st, path)

    script = textwrap.dedent("""
        import sys
        sys.path.insert(0, %r)
        import pm_state as S
        S.LOCK_PATH = %r
        S.set_note(sys.argv[1], "written by " + sys.argv[1], path=%r)
    """ % (str(S.ROOT), str(tmp_path / ".lock"), str(path)))
    sf = tmp_path / "w.py"
    sf.write_text(script)

    procs = [subprocess.Popen([sys.executable, str(sf), "i%02d" % n]) for n in range(20)]
    for p in procs:
        assert p.wait(timeout=60) == 0

    st, _ = S.load_state(path)
    written = [i for i in st["items"] if i["note"]]
    assert len(written) == 20, "lost %d concurrent writes" % (20 - len(written))


# ── the real migrated board ──

@pytest.mark.skipif(not S.STATE_PATH.exists(), reason="live board not present")
def test_live_board_is_loadable_and_sane():
    st, status = S.load_state()
    assert status == "ok"
    assert len(st["items"]) > 0
    for it in st["items"]:
        assert it["lane"] in S.LANES
        # Bound to the data layer's own contract, not a hand-copied tuple. The
        # literal ("open", "done") here silently went stale the moment
        # "not needed" shipped, and this test was red for a whole session
        # before anyone re-ran it.
        assert it["status"] in S.STATUSES
        if it["done_at"]:
            assert S.parse_dt(it["done_at"]).tzinfo is not None


# ── the sweep freshness gate ──

def test_swept_today_rejects_never_swept(path):
    st, _ = S.load_state(path)
    fresh, reason = S.swept_today(st)
    assert fresh is False
    assert "never been swept" in reason


def test_swept_today_rejects_yesterdays_stamp(path):
    st, _ = S.load_state(path)
    st["last_swept_at"] = "2026-07-27T09:00:00-04:00"
    fresh, reason = S.swept_today(st, now=datetime.datetime(2026, 7, 28, 17, 0))
    assert fresh is False
    assert "2026-07-27" in reason


GOOD_EVIDENCE = {
    "gmail": {"checked": 38, "detail": "every thread after:2026/07/28"},
    "slack": {"checked": 25, "detail": "all channels + DMs, full day"},
}


def test_mark_swept_makes_it_fresh(path):
    S.mark_swept(GOOD_EVIDENCE, path=path)
    st, _ = S.load_state(path)
    fresh, reason = S.swept_today(st)
    assert fresh is True
    assert "gmail" in reason and "slack" in reason
    # the stamp must carry an offset, like every other timestamp here
    assert S.parse_dt(st["last_swept_at"]).tzinfo is not None


def test_mark_swept_refuses_a_missing_source(path):
    """Slack alone is not a sweep — this is the exact 2026-07-28 failure."""
    with pytest.raises(ValueError) as ex:
        S.mark_swept({"slack": {"checked": 25, "detail": "all channels"}},
                     path=path)
    assert "gmail" in str(ex.value)


def test_mark_swept_refuses_evidence_free_claims(path):
    with pytest.raises(ValueError):
        S.mark_swept({"gmail": {"checked": 0, "detail": "looked"},
                      "slack": {"checked": 25, "detail": "all channels"}},
                     path=path)
    with pytest.raises(ValueError):
        S.mark_swept({"gmail": {"checked": 38, "detail": "  "},
                      "slack": {"checked": 25, "detail": "all channels"}},
                     path=path)


def test_bare_stamp_without_evidence_fails_the_gate(path):
    """Direct assignment must not be able to satisfy the gate."""
    st, _ = S.load_state(path)
    st["last_swept_at"] = S._now_iso()
    st["last_swept_sources"] = ["gmail", "slack"]
    fresh, reason = S.swept_today(st)
    assert fresh is False
    assert "no evidence" in reason
