#!/usr/bin/env python3
"""Tests for the three-line context contract (2026-07-30).

HAPPENED (the fact) · MATTERS (the consequence) · NEEDED (the ask).

Why it exists, measured on her live board before anything was built: 36 open
cards carried a MEDIAN of 888 characters of context, 25 of them over 600, the
worst 2,497. A card that takes a paragraph to read is a card she skips.

What these tests LOCK:

  1. The cap is enforced in the PIPELINE, not in a style note. That is the
     CHASE_WORDS lesson — a rule that is not in the pipeline gets skipped on a
     busy morning, and context length is exactly what drifts when the sweep is
     long and the writer is tired.
  2. Over-long lines are TRUNCATED AND FLAGGED (her choice), never rejected and
     never trimmed silently. A silent trim is indistinguishable from a naturally
     short sentence.
  3. Nothing is destroyed — ctx_body still holds the raw text verbatim, so the
     `▸ source` disclosure can always show what the sweep actually found.
  4. Cards written BEFORE the contract are NOT touched (her decision: "leave
     them, contract applies going forward").

Run:  cd ~/rfs_pm && python3 -m pytest tests -q
"""
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
    monkeypatch.setattr(I, "STATE_PATH", p, raising=False)
    st = S.blank_state(brief_date="2026-07-30")
    st["items"] = [S.new_item("legacy", "A card from before the contract",
                              ctx_sum="One flowing paragraph of context.")]
    st["items"][0]["ctx_body"] = ["<p>raw one</p>", "<p>raw two</p>"]
    S.save_state(st, p)
    return p


def _get(path, iid):
    st, _ = S.load_state(path)
    return S.get_item(st, iid)


# ── the fields exist and default honestly ──

def test_new_item_defaults_the_three_lines_to_none_not_blank(path):
    """None means "predates the contract"; "" would mean "left blank"."""
    it = _get(path, "legacy")
    assert it["ctx_happened"] is None
    assert it["ctx_matters"] is None
    assert it["ctx_needed"] is None
    assert it["ctx_trimmed"] is False


def test_a_collector_may_write_them(path):
    I.ingest([{"id": "fresh", "subject": "New card",
               "ctx_happened": "Rafael asked for the weekly report.",
               "ctx_matters": "The feature exists and the send list is ready.",
               "ctx_needed": "Start with 14 Guernsey."}], path=path)
    it = _get(path, "fresh")
    assert it["ctx_happened"] == "Rafael asked for the weekly report."
    assert it["ctx_matters"].startswith("The feature exists")
    assert it["ctx_needed"] == "Start with 14 Guernsey."
    assert it["ctx_trimmed"] is False


# ── the cap: truncate AND flag ──

def test_an_over_long_line_is_truncated_and_the_card_flagged(path):
    long = "x" * 400
    res = I.ingest([{"id": "fat", "subject": "Wordy card",
                     "ctx_happened": long}], path=path)
    it = _get(path, "fat")
    assert len(it["ctx_happened"]) <= I.CTX_LINE_CAP + 1   # +1 for the ellipsis
    assert it["ctx_happened"].endswith("…")
    assert it["ctx_trimmed"] is True, "a trim must be visible, never silent"
    assert res["trimmed"] == ["fat"], "the pipeline reports what it trimmed"


def test_a_line_at_the_cap_is_left_exactly_alone(path):
    exact = "y" * I.CTX_LINE_CAP
    I.ingest([{"id": "edge", "subject": "Edge", "ctx_happened": exact}],
             path=path)
    it = _get(path, "edge")
    assert it["ctx_happened"] == exact, "the cap is inclusive"
    assert it["ctx_trimmed"] is False
    assert not it["ctx_happened"].endswith("…")


def test_truncation_cuts_on_a_word_boundary(path):
    """A mid-word cut reads as corruption rather than as an abbreviation."""
    words = ("alpha " * 60).strip()
    I.ingest([{"id": "w", "subject": "s", "ctx_matters": words}], path=path)
    got = _get(path, "w")["ctx_matters"]
    assert got.endswith("…")
    assert "alph…" not in got, "cut mid-word"
    assert got[:-1].endswith("alpha")


def test_every_one_of_the_three_lines_is_capped(path):
    long = "z" * 400
    I.ingest([{"id": "all3", "subject": "s", "ctx_happened": long,
               "ctx_matters": long, "ctx_needed": long}], path=path)
    it = _get(path, "all3")
    for k in ("ctx_happened", "ctx_matters", "ctx_needed"):
        assert len(it[k]) <= I.CTX_LINE_CAP + 1, "%s escaped the cap" % k


def test_the_raw_text_is_never_destroyed_by_a_trim(path):
    """ctx_body is the evidence behind `▸ source` — the cap must not touch it."""
    raw = ["<p>%s</p>" % ("long paragraph " * 40)]
    I.ingest([{"id": "keepraw", "subject": "s", "ctx_happened": "q" * 400,
               "ctx_body": raw}], path=path)
    it = _get(path, "keepraw")
    assert it["ctx_body"] == raw, "the source paragraphs are untouched"
    assert it["ctx_trimmed"] is True


def test_a_short_card_is_not_flagged(path):
    I.ingest([{"id": "lean", "subject": "s",
               "ctx_happened": "Short and clear."}], path=path)
    assert _get(path, "lean")["ctx_trimmed"] is False


def test_whitespace_only_line_becomes_none_not_an_empty_string(path):
    I.ingest([{"id": "blank", "subject": "s", "ctx_happened": "   "}],
             path=path)
    assert _get(path, "blank")["ctx_happened"] is None


# ── her decision: existing cards are left alone ──

def test_a_legacy_card_keeps_its_old_context_untouched(path):
    """"Leave them, contract applies going forward" — hers, 2026-07-30."""
    before = _get(path, "legacy")
    I.ingest([{"id": "legacy", "subject": "Refreshed by a later sweep"}],
             path=path)
    after = _get(path, "legacy")
    assert after["ctx_sum"] == before["ctx_sum"]
    assert after["ctx_body"] == before["ctx_body"]
    assert after["ctx_happened"] is None, "no backfill was asked for"
    assert after["subject"] == "Refreshed by a later sweep"


def test_the_cap_helper_is_actually_wired_into_ingest(path):
    """Guards against the 'applied but inert' class: a helper nothing calls.

    Asserts the effect through the public ingest() path, not by calling
    _cap_ctx_lines directly — which is the whole point.
    """
    I.ingest([{"id": "wired", "subject": "s", "ctx_needed": "n" * 500}],
             path=path)
    assert _get(path, "wired")["ctx_trimmed"] is True


def test_the_cap_is_not_applied_to_unrelated_text_fields(path):
    """`action` runs a median of 131 chars and has its own job — leave it."""
    long_action = "do the thing " * 40
    I.ingest([{"id": "act", "subject": "s", "action": long_action}], path=path)
    assert _get(path, "act")["action"] == long_action
