#!/usr/bin/env python3
"""Tests for the live watermarked sweep (2026-07-30).

What these LOCK — every one of them is a way the sweep could lie:

  1. A watermark advances ONLY after a successful ingest. Advancing on a
     successful FETCH means a crash between fetch and write loses those
     messages permanently and silently: nothing would ever look wrong.
  2. An ABSENT marker is a failure, not a zero. A run that cannot say what it
     read has not shown it read anything, and treating silence as "all quiet"
     is exactly how a partial board passes for a complete one.
  3. Zero from a required source is a FAILURE. A poll that reaches Gmail but
     gets nothing from Slack yields a board both freshly stamped and missing
     half the day.
  4. A failure is PERSISTED, so the page can say so. mark_swept() already
     refuses to stamp a sweep it has no evidence for, but a refusal that only
     raises into a log leaves her reading a confident, partial board.
  5. A success CLEARS the failure, or the red banner outlives the problem and
     starts crying wolf.
  6. The registry is read from pm_sweep.py, never restated — a second copy of
     the channel list is how #99-concord got missed on 2026-07-29.

Run:  cd ~/rfs_pm && python3 -m pytest tests -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pm_state as S       # noqa: E402
import pm_sweep_run as R   # noqa: E402


@pytest.fixture
def path(tmp_path, monkeypatch):
    p = tmp_path / "pm_state.json"
    monkeypatch.setattr(S, "LOCK_PATH", tmp_path / ".lock")
    monkeypatch.setattr(S, "HISTORY_DIR", tmp_path / "history")
    S.save_state(S.blank_state(brief_date="2026-07-30"), p)
    return p


# ── watermarks ──

def test_a_fresh_board_has_no_watermarks(path):
    st, _ = S.load_state(path)
    assert S.get_watermark(st, "slack") is None
    assert S.get_watermark(st, "gmail") is None


def test_advance_and_read_back(path):
    S.advance_watermark("slack", "1753900000.123", path=path)
    st, _ = S.load_state(path)
    assert S.get_watermark(st, "slack") == "1753900000.123"
    assert S.get_watermark(st, "gmail") is None, "sources are independent"


def test_a_blank_cursor_is_refused(path):
    for blank in (None, "", "   "):
        assert S.advance_watermark("slack", blank, path=path)["error"]
    st, _ = S.load_state(path)
    assert S.get_watermark(st, "slack") is None


def test_normalize_backfills_watermarks_on_a_file_that_predates_them(path):
    st, _ = S.load_state(path)
    del st["sweep_watermarks"]
    del st["last_sweep_failure"]
    S.save_state(st, path)
    st2, _ = S.load_state(path)
    assert st2["sweep_watermarks"] == {}
    assert st2["last_sweep_failure"] is None


# ── marker parsing: absence != zero ──

def test_a_missing_marker_is_reported_missing_not_zero():
    counts, cursors, missing = R._parse_markers("GMAIL_OK=4\n")
    assert counts == {"gmail": 4}
    assert missing == ["slack"], "a silent source must not read as a quiet one"


def test_an_explicit_zero_is_parsed_as_zero_not_missing():
    counts, _, missing = R._parse_markers("SLACK_OK=0\nGMAIL_OK=0\n")
    assert counts == {"slack": 0, "gmail": 0}
    assert missing == []


def test_cursors_are_optional_and_parsed_when_present():
    _, cursors, _ = R._parse_markers(
        "SLACK_OK=2\nGMAIL_OK=1\nSLACK_CURSOR=1753900001.5\n")
    assert cursors["slack"] == "1753900001.5"
    assert "gmail" not in cursors


# ── the failure record ──

def test_a_failure_is_persisted_with_a_reason(path):
    R._fail(path, "Slack returned nothing", ["slack"])
    st, _ = S.load_state(path)
    f = st["last_sweep_failure"]
    assert f["reason"] == "Slack returned nothing"
    assert f["sources"] == ["slack"]
    assert f["at"]


def test_a_successful_sweep_clears_a_previous_failure(path):
    R._fail(path, "Slack returned nothing", ["slack"])
    S.mark_swept({"slack": {"checked": 3, "detail": "polled since X"},
                  "gmail": {"checked": 1, "detail": "polled since X"}},
                 path=path)
    st, _ = S.load_state(path)
    assert st["last_sweep_failure"] is None, "a stale red banner cries wolf"


def test_mark_swept_still_refuses_a_zero_evidence_sweep(path):
    """The gate this whole design leans on — assert it, don't assume it."""
    with pytest.raises(ValueError):
        S.mark_swept({"slack": {"checked": 0, "detail": "nothing"},
                      "gmail": {"checked": 2, "detail": "polled"}}, path=path)


# ── the prompt ──

def test_the_prompt_tells_the_model_to_toolsearch_first(path):
    """Without this the connectors fail silently and return a confident zero."""
    st, _ = S.load_state(path)
    _, _, prompt = R.build_prompt(st)
    assert "ToolSearch" in prompt
    assert "DEFERRED" in prompt


def test_the_prompt_demands_the_markers(path):
    st, _ = S.load_state(path)
    _, _, prompt = R.build_prompt(st)
    for marker in ("SLACK_OK=", "GMAIL_OK=", "SLACK_CURSOR=", "GMAIL_CURSOR="):
        assert marker in prompt


def test_the_prompt_carries_the_registry_from_pm_sweep_not_a_second_copy(path):
    """#99-concord was missed once because nothing listed it. Prove it's there."""
    st, _ = S.load_state(path)
    _, _, prompt = R.build_prompt(st)
    assert "C0BLK0EKK0E" in prompt, "the registry is not reaching the prompt"
    assert "99-concord" in prompt


def test_the_prompt_carries_the_watermarks(path):
    S.advance_watermark("slack", "1753900000.9", path=path)
    st, _ = S.load_state(path)
    wms, _, prompt = R.build_prompt(st)
    assert wms["slack"] == "1753900000.9"
    assert "1753900000.9" in prompt


def test_the_prompt_states_the_160_char_context_cap(path):
    """The sweep is the thing that would drift back to dumping."""
    st, _ = S.load_state(path)
    _, _, prompt = R.build_prompt(st)
    assert "160" in prompt
    assert "ctx_happened" in prompt


# ── binary resolution ──

def test_the_claude_binary_is_resolved_to_something_executable():
    """It is NOT on PATH on this machine; it lives inside the VSCode extension,
    at a path carrying a version number that changes on every update."""
    assert R.CLAUDE_BIN
    assert os.path.isabs(R.CLAUDE_BIN) or R.CLAUDE_BIN == "claude"


def test_an_explicit_override_wins(monkeypatch):
    monkeypatch.setenv("CLAUDE_BIN", "/tmp/some-claude")
    assert R._resolve_claude() == "/tmp/some-claude"


def test_a_missing_binary_fails_loudly_rather_than_sweeping_empty(path, monkeypatch):
    monkeypatch.setattr(R, "CLAUDE_BIN", "/nonexistent/claude-binary")
    res = R.run_sweep(path=path)
    assert res["ok"] is False
    assert "not on PATH" in res["reason"]
    st, _ = S.load_state(path)
    assert st["last_sweep_failure"], "the failure must reach the board"
