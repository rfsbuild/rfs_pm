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
        S.new_item("keep", "Original subject", lane="week", kind="pay",
                   claude_done=["read the ledger"], hadassa_todo=["decide"]),
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


def test_collector_may_not_write_the_waiting_chase_log():
    """The strongest case of "hers" — the log is evidence, not content.

    A briefing that could set `waiting` could rewrite the record of how many
    times she chased someone. Asserted separately from the loop above because
    this one protects an append-only guarantee, not just an edit.
    """
    errs = I.validate([{"id": "x", "subject": "s",
                        "waiting": {"who": "Rafael", "log": [{"at": "now",
                                                              "text": "made up"}]}}])
    assert any("which is hers" in e for e in errs)


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
    assert res == {"added": [], "updated": ["keep"], "trimmed": []}
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
    I.ingest([{"id": "keep", "subject": "s", "lane": "urgent",
               "claude_done": []}], path=path)
    assert S.get_item(S.load_state(path)[0], "keep")["lane"] == "week"


def test_relane_opt_in_does_move_it(path):
    I.ingest([{"id": "keep", "subject": "s", "lane": "urgent",
               "claude_done": [], "_relane": True}], path=path)
    assert S.get_item(S.load_state(path)[0], "keep")["lane"] == "urgent"


def test_due_IS_refreshed_because_a_deadline_is_source_truth(path):
    I.ingest([{"id": "keep", "subject": "s", "due": "2026-08-14"}], path=path)
    assert S.get_item(S.load_state(path)[0], "keep")["due"] == "2026-08-14"


# ── inserts ──

def test_new_item_lands_with_defaults_and_seen_stamps(path):
    I.ingest([{"id": "fresh", "subject": "A new card", "lane": "urgent",
               "kind": "compliance", "pills": [{"cls": "", "t": "x"}],
               "claude_done": ["pulled the certificate dates"],
               "hadassa_todo": ["make the call"]}],
             path=path)
    it = S.get_item(S.load_state(path)[0], "fresh")
    assert it["lane"] == "urgent" and it["kind"] == "compliance"
    assert it["status"] == "open" and it["did"] is None
    assert it["first_seen"] and it["last_seen"]
    # defaults filled from new_item, so the UI can never read a missing key
    for k in S.ITEM_FIELDS:
        assert k in it


def test_ingest_is_idempotent(path):
    spec = [{"id": "fresh", "subject": "A new card", "lane": "action",
             "claude_done": []}]
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


# ── the chase/draft contract (2026-07-29) ──

def test_is_chase_is_word_boundary_not_substring():
    """The first matcher used `in`, so "coi" matched "cost coding" and a
    receipt-coding card was flagged as a sub-chase. A rule that cries wolf gets
    ignored, so precision is the requirement."""
    assert not I.is_chase({"subject": "Three Home Depot receipts",
                           "action": "Code the purchases to the right cost code"})
    assert not I.is_chase({"subject": "Cost Inbox is the job-costing blocker",
                           "action": "Decide which jobs matter"})
    assert I.is_chase({"subject": "98 Wyman bid status",
                       "action": "Chase the 16 open bids"})
    assert I.is_chase({"subject": "LLO electrical date",
                       "action": "Ask LLO for the date and the certificates"})


def test_chase_card_without_a_draft_is_rejected():
    errs = I.validate([{"id": "x", "subject": "Chase the subs for quotes",
                        "lane": "action", "claude_done": []}])
    assert any("ships no `draft`" in e for e in errs)


def test_chase_card_with_a_draft_passes():
    assert I.validate([{"id": "x", "subject": "Chase the subs for quotes",
                        "lane": "action", "claude_done": [],
                        "draft": {"to": "a@b.com", "subject": "s",
                                  "body": "Please send your quote."}}]) == []


def test_no_draft_escape_hatch_needs_no_draft_but_is_recorded():
    """Some chases are answered in Slack or face to face. Legitimate — but it
    has to be written down, not left as a silent absence."""
    assert I.validate([{"id": "x", "subject": "Ask Rafael about the scope",
                        "lane": "urgent", "claude_done": [],
                        "_no_draft": "already a Slack draft in her Drafts"}]) == []


def test_stub_drafts_are_rejected():
    for bad in ({"to": "", "body": "hi"}, {"to": "a@b", "body": "  "},
                "not a dict"):
        errs = I.validate([{"id": "x", "subject": "Chase the quote",
                            "lane": "action", "claude_done": [], "draft": bad}])
        assert errs, bad


def test_bare_fill_in_placeholder_is_rejected():
    errs = I.validate([{"id": "x", "subject": "Chase the quote", "lane": "action",
                        "claude_done": [],
                        "draft": {"to": "a@b.com", "subject": "s",
                                  "body": "The size is [FILL IN]. Thanks."}}])
    assert any("[FILL IN]" in e for e in errs)
    # ...but fine when the card says who supplies it
    assert I.validate([{"id": "x", "subject": "Chase the quote", "lane": "action",
                        "claude_done": [],
                        "draft": {"to": "a@b.com", "subject": "s",
                                  "body": "Size: [FILL IN once Rafael confirms]."}}]) == []


# ── the claude_done contract ──

def test_actionable_card_must_state_what_was_done_for_her():
    errs = I.validate([{"id": "x", "subject": "Do a thing", "lane": "urgent"}])
    assert any("claude_done" in e for e in errs)
    # an explicit empty is a valid answer
    assert I.validate([{"id": "x", "subject": "Do a thing", "lane": "urgent",
                        "claude_done": []}]) == []


def test_week_and_noise_lanes_do_not_require_claude_done():
    for lane in ("week", "noise", "rafael"):
        assert I.validate([{"id": "x", "subject": "s", "lane": lane}]) == [], lane


def test_split_fields_must_be_lists():
    errs = I.validate([{"id": "x", "subject": "s", "claude_done": "a string"}])
    assert any("claude_done must be a list" in e for e in errs)


def test_collector_may_write_the_split_but_not_her_did():
    assert "claude_done" in I.SOURCE_OWNED and "hadassa_todo" in I.SOURCE_OWNED
    assert "did" in I.HER_FIELDS


# ── patching an existing card ──

def test_patch_without_subject_is_allowed_when_the_card_exists(path):
    """An addendum that only adds claude_done must not be forced to resend the
    subject, draft and lane it isn't touching."""
    res = I.ingest([{"id": "keep", "claude_done": ["did the research"]}],
                   path=path)
    assert res["updated"] == ["keep"]
    it = S.get_item(S.load_state(path)[0], "keep")
    assert it["claude_done"] == ["did the research"]
    assert it["subject"] == "Original subject"


def test_patch_on_a_NEW_card_still_requires_a_subject(path):
    with pytest.raises(ValueError):
        I.ingest([{"id": "brandnew", "claude_done": ["x"]}], path=path)


# ── clearing a field vs omitting it (2026-07-29) ──

def test_OMITTING_a_field_preserves_it_and_that_is_the_trap(path):
    """Bit twice on 2026-07-29. A card's draft went stale, the refresh rewrote
    subject/action but OMITTED draft, and yesterday's email survived underneath.
    Then a Brookvale collection email that had become dangerous to send was
    "removed" by omission — and stayed on the card. Omitting is not clearing."""
    I.ingest([{"id": "keep", "draft": {"to": "a@b.com", "subject": "s",
                                      "body": "original"}}], path=path)
    I.ingest([{"id": "keep", "subject": "rewritten, no draft supplied"}],
             path=path)
    it = S.get_item(S.load_state(path)[0], "keep")
    assert it["subject"] == "rewritten, no draft supplied"
    assert it["draft"]["body"] == "original"     # <-- survived. The trap.


def test_EXPLICIT_null_clears_a_dangerous_draft(path):
    """The only way to de-fang a card whose draft must not be sent."""
    I.ingest([{"id": "keep", "draft": {"to": "a@b.com", "subject": "s",
                                      "body": "do not send this"}}], path=path)
    I.ingest([{"id": "keep", "draft": None,
               "_no_draft": "cleared: chasing this client would be wrong"}],
             path=path)
    assert S.get_item(S.load_state(path)[0], "keep")["draft"] is None


def test_clearing_a_draft_on_a_chase_card_needs_the_written_reason(path):
    """Clearing must be deliberate: draft:None on a chase card without
    _no_draft is rejected, so a draft can never quietly vanish."""
    with pytest.raises(ValueError):
        I.ingest([{"id": "c", "subject": "Chase the client for the balance",
                   "lane": "action", "claude_done": [], "draft": None}],
                 path=path)
