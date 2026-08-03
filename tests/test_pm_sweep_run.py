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
import json
import os
import sys
import types

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


# ── the draft contract (2026-07-30, after a 12-minute run was thrown away) ──

def test_the_prompt_states_the_draft_OBJECT_shape():
    """pm_ingest refuses a whole briefing when any `draft` is a bare string, and
    on the first real run every one of them was — 19 good items and 12m06s
    discarded because the prompt said only "MUST carry a draft".

    A contract enforced by the validator but absent from the prompt is not a
    contract, it is a trap. This locks the shape into the instruction itself.
    """
    import pm_sweep_run as R
    st = S.blank_state(brief_date="2026-07-30")
    _, _, prompt = R.build_prompt(st)
    assert '"draft"' in prompt
    for key in ('"to"', '"subject"', '"body"'):
        assert key in prompt, "the prompt must name every required draft key"
    low = prompt.lower()
    assert "object" in low and "never a string" in low, \
        "state the shape explicitly — the failure was a guessed shape, not a missing field"


def test_the_timeout_clears_a_measured_cold_run():
    """A cold run (no watermark → the whole day, 12 channels + Gmail) measured
    12m06s. A ceiling below that kills the 07:45 run every morning, which is the
    widest window of the day."""
    import pm_sweep_run as R
    assert R.TIMEOUT_S >= 780, \
        "600s killed a real 12m06s cold run on 2026-07-30 — keep headroom over it"


def test_a_validation_failure_records_the_actual_defects(path, monkeypatch, tmp_path):
    """Only the exception's FIRST line used to be kept — but pm_ingest puts the
    headline on line 1 and every real defect on the lines after it, so the
    recorded failure read "briefing is invalid, nothing was written:" and stopped
    dead at the colon. It cost a separate investigation to find out that 8 drafts
    were the wrong shape. Behavioural, not a source-string grep: an assertion
    about absent source text trips on the comment explaining the fix.
    """
    out = tmp_path / "briefing.json"
    out.write_text(json.dumps({"items": [{"id": "x", "subject": "x"}]}))
    monkeypatch.setattr(R, "build_prompt",
                        lambda st: ({"slack": "", "gmail": ""}, out, "prompt"))
    monkeypatch.setattr(R.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        stdout="SLACK_OK=3\nGMAIL_OK=2\nSLACK_CURSOR=1\nGMAIL_CURSOR=2\n", stderr=""))

    def _boom(items, path=None):
        raise ValueError("briefing is invalid, nothing was written:\n"
                         "  - item[3] foo draft must be an object with to/subject/body\n"
                         "  - item[9] bar draft must be an object with to/subject/body")
    monkeypatch.setattr(R.I, "ingest", _boom)

    res = R.run_sweep(path=path)
    assert res["ok"] is False
    assert "draft must be an object" in res["reason"], \
        "the recorded reason must name the actual defect, not just the headline"
    assert "item[3]" in res["reason"] and "item[9]" in res["reason"]
    st, _ = S.load_state(path)
    assert "draft must be an object" in st["last_sweep_failure"]["reason"]
    # A refused briefing must not consume the messages it declined to record.
    assert not (st.get("watermarks") or {}).get("slack")


# ---------------------------------------------------------------------------
# 2026-07-31 — the watermark never advanced, so EVERY run was a cold run.
# ---------------------------------------------------------------------------

def test_prose_mentioning_a_count_does_NOT_satisfy_the_gate():
    """The counts on 2026-07-30 parsed out of a PROSE SENTENCE by luck.

    Run 3 wrote "Run 3's result: SLACK_OK=51 · GMAIL_OK=33, 23 items…" instead
    of the standalone marker lines STEP 5 demands. The unanchored regex matched
    inside that sentence, so the run passed the evidence gate while reporting
    no cursor at all — which is precisely why the watermark stayed empty and
    every subsequent run re-read the whole day.
    """
    counts, cursors, missing = R._parse_markers(
        "Run 3's result: SLACK_OK=51 · GMAIL_OK=33, 23 items ingested.")
    assert counts == {} and cursors == {}
    assert missing == ["gmail", "slack"], \
        "a count buried in prose is not a reported count"


def test_prose_cannot_FABRICATE_a_failure_either():
    """The mirror risk, and the worse one: an unanchored match means a sentence
    merely discussing SLACK_OK=0 turns a healthy run into a recorded failure."""
    counts, _, missing = R._parse_markers(
        "if the log had said SLACK_OK=0 we would have known something broke")
    assert counts == {} and missing == ["gmail", "slack"]


def test_real_marker_lines_still_parse():
    counts, cursors, missing = R._parse_markers(
        "chatter\nSLACK_OK=51\nGMAIL_OK=33\n"
        "SLACK_CURSOR=1785443689.909339\nGMAIL_CURSOR=2026-07-30T20:47:38Z\n")
    assert missing == []
    assert counts == {"slack": 51, "gmail": 33}
    assert cursors["slack"] == "1785443689.909339"
    assert cursors["gmail"] == "2026-07-30T20:47:38Z"


def test_a_missing_cursor_still_advances_the_watermark(path, monkeypatch, tmp_path):
    """The actual fix for the slowness.

    A missing cursor must NOT be a hard failure — that would repeat the run-2
    disaster where one unmet formatting rule discarded 12 minutes of good
    items. But it must not leave the watermark empty either, or the next run is
    cold again. It falls back to the run's START time.
    """
    out = tmp_path / "briefing.json"
    out.write_text(json.dumps({"items": []}))
    monkeypatch.setattr(R, "build_prompt",
                        lambda st: ({"slack": "", "gmail": ""}, out, "prompt"))
    # counts reported on their own lines; NO cursor lines at all.
    monkeypatch.setattr(R.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        stdout="SLACK_OK=51\nGMAIL_OK=33\n", stderr=""))

    res = R.run_sweep(path=path)
    assert res["ok"] is True, "a missing cursor must not discard a good run"
    assert res["cursor_derived"] == ["gmail", "slack"]
    assert "start time" in res["cursor_note"], "the derivation must be LOUD"

    st, _ = S.load_state(path)
    assert S.get_watermark(st, "slack"), "slack watermark must not be empty"
    assert S.get_watermark(st, "gmail"), "gmail watermark must not be empty"
    # Formats must match what each source actually speaks, or the next prompt
    # renders a watermark the model cannot use.
    float(S.get_watermark(st, "slack"))          # slack = unix ts
    assert S.get_watermark(st, "gmail").endswith("Z")   # gmail = ISO-8601 Z


def test_a_reported_cursor_still_wins_over_the_fallback(path, monkeypatch, tmp_path):
    out = tmp_path / "briefing.json"
    out.write_text(json.dumps({"items": []}))
    monkeypatch.setattr(R, "build_prompt",
                        lambda st: ({"slack": "", "gmail": ""}, out, "prompt"))
    monkeypatch.setattr(R.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        stdout="SLACK_OK=5\nGMAIL_OK=5\nSLACK_CURSOR=1785443689.909339\n"
               "GMAIL_CURSOR=2026-07-30T20:47:38Z\n", stderr=""))

    res = R.run_sweep(path=path)
    assert res["ok"] is True
    assert "cursor_derived" not in res
    st, _ = S.load_state(path)
    assert S.get_watermark(st, "slack") == "1785443689.909339"
    assert S.get_watermark(st, "gmail") == "2026-07-30T20:47:38Z"


def test_the_fallback_is_the_START_not_the_end():
    """START, not end. A run takes ~12 minutes; a message landing mid-run may or
    may not have been read. Watermarking the END would silently skip it. The
    START can only cause a harmless re-read, which the ingest dedupes."""
    early, late = 1785000000.0, 1785009999.0
    assert float(R._fallback_cursor("slack", early)) < late
    assert R._fallback_cursor("gmail", early) < R._fallback_cursor("gmail", late)


def test_the_prompt_tells_the_sweep_to_read_her_OWN_sends():
    """`in:sent` returns empty and `label:SENT` holds 233 messages. Reporting
    that as "no outbound is confirmable from Gmail" put a false source limit
    into a PUBLISHED report on 2026-07-30."""
    st, _ = S.load_state()
    _, _, prompt = R.build_prompt(st)
    assert "label:SENT" in prompt
    assert "in:sent" in prompt, "the prompt must warn against the query that fails"
    # The known gap must travel with the instruction, or the sweep will report
    # "she did not send it" for a send made from inside office@.
    assert "not visible from Gmail" in prompt
