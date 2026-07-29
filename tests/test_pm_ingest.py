#!/usr/bin/env python3
"""Tests for pm_ingest — the durable briefing loader.

The bugs these lock down are the ones that made hand-written `apply_sessionN.py`
scripts dangerous: a half-applied briefing, a collector overwriting her own
edits, and a typo'd field name that produced a silently blank card.

Run:  cd ~/rfs_pm && python3 -m pytest tests -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pm_state as S  # noqa: E402
import pm_ingest as I  # noqa: E402


@pytest.fixture
def path(tmp_path, monkeypatch):
    p = tmp_path / "pm_state.json"
    monkeypatch.setattr(S, "LOCK_PATH", tmp_path / ".lock")
    monkeypatch.setattr(S, "HISTORY_DIR", tmp_path / "history")
    st = S.blank_state(brief_date="2026-07-29")
    st["items"] = [
        S.new_item("keep", "Original subject", lane="week", kind="pay"),
    ]
    # simulate HER edits on that card
    it = st["items"][0]
    it["status"] = "done"
    it["did"] = "Called the sub and settled it"
    it["note"] = "her private note"
    it["assignee"] = "rafael"
    it["age"] = 4
    S.save_state(st, p)
    return p


# ── validation ──

def test_unknown_field_is_rejected():
    errs = I.validate([{"id": "x", "subject": "s", "subjekt": "typo"}])
    assert any("unknown field 'subjekt'" in e for e in errs)


def test_collector_may_not_write_her_fields():
    for f in ("status", "did", "note", "assignee", "done_at"):
        errs = I.validate([{"id": "x", "subject": "s", f: "whatever"}])
        assert any("which is hers" in e for e in errs), f


def test_missing_id_or_subject_is_rejected():
    assert any("no string `id`" in e for e in I.validate([{"subject": "s"}]))
    assert any("no `subject`" in e for e in I.validate([{"id": "x"}]))


def test_duplicate_ids_rejected():
    errs = I.validate([{"id": "x", "subject": "a"}, {"id": "x", "subject": "b"}])
    assert any("duplicate id" in e for e in errs)


def test_bad_lane_and_bad_due_rejected():
    assert any("not in" in e for e in
               I.validate([{"id": "x", "subject": "s", "lane": "nope"}]))
    assert any("YYYY-MM-DD" in e for e in
               I.validate([{"id": "x", "subject": "s", "due": "next friday"}]))


def test_list_fields_must_be_lists():
    errs = I.validate([{"id": "x", "subject": "s", "pills": "not a list"}])
    assert any("pills must be a list" in e for e in errs)


def test_control_keys_allowed_but_typos_are_not():
    assert I.validate([{"id": "x", "subject": "s", "_note": "hi"}]) == []
    assert any("unknown field '_nope'" in e for e in
               I.validate([{"id": "x", "subject": "s", "_nope": 1}]))


def test_relane_without_a_lane_is_rejected():
    errs = I.validate([{"id": "x", "subject": "s", "_relane": True}])
    assert any("_relane but gives no `lane`" in e for e in errs)


# ── the all-or-nothing guarantee ──

def test_invalid_briefing_writes_nothing(path):
    before = json.loads(open(path).read())
    with pytest.raises(ValueError):
        I.ingest([{"id": "ok", "subject": "fine"},
                  {"id": "bad", "subject": "s", "bogus": 1}], path=path)
    assert json.loads(open(path).read()) == before


# ── her edits survive a re-ingest ──

def test_refresh_keeps_her_fields_and_updates_source_fields(path):
    res = I.ingest([{"id": "keep", "subject": "NEW subject from the collector",
                     "meta": "fresh meta", "action": "do the new thing"}],
                   path=path)
    assert res == {"added": [], "updated": ["keep"]}
    it = S.get_item(S.load_state(path)[0], "keep")
    assert it["subject"] == "NEW subject from the collector"
    assert it["meta"] == "fresh meta"
    # hers, untouched
    assert it["status"] == "done"
    assert it["did"] == "Called the sub and settled it"
    assert it["note"] == "her private note"
    assert it["assignee"] == "rafael"
    assert it["age"] == 4


def test_refresh_does_NOT_move_her_lane(path):
    """The regression that shipped once: `lane` in SOURCE_OWNED silently undid
    a re-prioritisation on the next ingest."""
    I.ingest([{"id": "keep", "subject": "s", "lane": "urgent"}], path=path)
    assert S.get_item(S.load_state(path)[0], "keep")["lane"] == "week"


def test_relane_opt_in_does_move_it(path):
    I.ingest([{"id": "keep", "subject": "s", "lane": "urgent",
               "_relane": True}], path=path)
    assert S.get_item(S.load_state(path)[0], "keep")["lane"] == "urgent"


def test_due_IS_refreshed_because_a_deadline_is_source_truth(path):
    I.ingest([{"id": "keep", "subject": "s", "due": "2026-08-14"}], path=path)
    assert S.get_item(S.load_state(path)[0], "keep")["due"] == "2026-08-14"


# ── inserts ──

def test_new_item_lands_with_defaults_and_seen_stamps(path):
    I.ingest([{"id": "fresh", "subject": "A new card", "lane": "urgent",
               "kind": "compliance", "pills": [{"cls": "", "t": "x"}]}],
             path=path)
    it = S.get_item(S.load_state(path)[0], "fresh")
    assert it["lane"] == "urgent" and it["kind"] == "compliance"
    assert it["status"] == "open" and it["did"] is None
    assert it["first_seen"] and it["last_seen"]
    # defaults filled from new_item, so the UI can never read a missing key
    for k in S.ITEM_FIELDS:
        assert k in it


def test_ingest_is_idempotent(path):
    spec = [{"id": "fresh", "subject": "A new card", "lane": "action"}]
    assert I.ingest(spec, path=path)["added"] == ["fresh"]
    assert I.ingest(spec, path=path)["updated"] == ["fresh"]
    assert len(S.load_state(path)[0]["items"]) == 2


# ── the CLI guard rails ──

def test_cli_refuses_a_brief_date_mismatch(path, tmp_path, capsys):
    bf = tmp_path / "b.json"
    bf.write_text(json.dumps({"brief_date": "2026-07-28",
                              "items": [{"id": "x", "subject": "s"}]}))
    rc = I.main([str(bf), "--state", str(path)])
    assert rc == 2
    assert "brief_date mismatch" in capsys.readouterr().out
    assert S.get_item(S.load_state(path)[0], "x") is None


def test_cli_dry_run_writes_nothing(path, tmp_path, capsys):
    bf = tmp_path / "b.json"
    bf.write_text(json.dumps({"items": [{"id": "x", "subject": "s"}]}))
    before = open(path).read()
    assert I.main([str(bf), "--state", str(path), "--dry-run"]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert open(path).read() == before


def test_source_owned_stays_in_sync_with_her_fields():
    """No field may be both collector-refreshable and hers."""
    assert not set(I.SOURCE_OWNED) & set(I.HER_FIELDS)
    for f in I.SOURCE_OWNED:
        assert f in S.ITEM_FIELDS, f
    for f in I.HER_FIELDS:
        assert f in S.ITEM_FIELDS, f
