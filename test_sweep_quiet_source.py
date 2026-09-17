#!/usr/bin/env python3
"""A source with nothing to say must not destroy the sweep that heard it.

WHY THIS EXISTS — 2026-09-17, her question: *"is it only slack's sweep that's
failing? or emails and buildertrend as well?"* Reading the run's own transcript
(last_sweep_output.txt, 14:05) answered it: NEITHER was failing. Slack read 25
channels across 2 pages plus 5 DMs and honestly reported SLACK_OK=0 — a quiet 73
minutes on a Thursday afternoon. Gmail read 2 threads. The run wrote a VALIDATED
briefing: 3 board items + 1 finance lead. Then the zero-check aborted before the
ingest and threw all four away.

THE ORIGINAL RULE WAS DELIBERATE and its worry is real: a connector that breaks
silently returns nothing, and from outside that looks exactly like a quiet window.
But the module docstring states the mechanism that actually discriminates — "a
MISSING marker is a failure, never a zero" — and STEP 5 of the prompt orders the
run to "REPORT these on their own lines, exactly, EVEN WHEN THE COUNT IS 0". The
protocol asks for a zero and the parser then treats it as death. That is the bug.

THE PRINCIPLE THIS ENCODES: detection must never be implemented as data
destruction. A silently-dead connector is still caught — by the missing marker, by
a negative count, and by a quiet STREAK — but none of those discards a good run.

    python3 test_sweep_quiet_source.py
"""
import json, pathlib, sys, tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pm_state as S                                          # noqa: E402
import pm_sweep_run as R                                      # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    (print(f"  ok   {name}") if cond else
     (FAILS.append(name), print(f"  FAIL {name}  {detail}")))

def ev(slack, gmail):
    return {"slack": {"checked": slack, "detail": "polled since 1789662816"},
            "gmail": {"checked": gmail, "detail": "polled since 1789663942"}}

def fresh_state(tmp):
    p = pathlib.Path(tmp) / "pm_state.json"
    p.write_text(json.dumps({"items": [], "_schema": 1}))
    return p


print("§1  a ZERO is quiet, not dead — the run must survive it")
check("SLACK_OK=0 is NOT unhealthy", R._unhealthy({"slack": 0, "gmail": 2}) == [],
      f"got {R._unhealthy({'slack': 0, 'gmail': 2})}")
check("both zero is still NOT unhealthy", R._unhealthy({"slack": 0, "gmail": 0}) == [])

print("§2  the ways a source is genuinely broken — all still fatal")
check("a NEGATIVE count is unhealthy", R._unhealthy({"slack": -1, "gmail": 2}) == ["slack"],
      f"got {R._unhealthy({'slack': -1, 'gmail': 2})}")
check("a MISSING marker is still a failure (the real discriminator)",
      R._parse_markers("GMAIL_OK=2\n")[2] == ["slack"],
      f"got {R._parse_markers('GMAIL_OK=2')[2]}")
check("both markers present -> nothing missing",
      R._parse_markers("SLACK_OK=0\nGMAIL_OK=2\n")[2] == [])
check("a zero parses as 0, not as absent",
      R._parse_markers("SLACK_OK=0\nGMAIL_OK=2\n")[0] == {"slack": 0, "gmail": 2})

print("§3  mark_swept — the TWIN gate; `detail` is the proof the source was queried")
with tempfile.TemporaryDirectory() as t:
    p = fresh_state(t)
    try:
        S.mark_swept(ev(0, 2), path=p); ok = True; why = ""
    except ValueError as exc:
        ok = False; why = str(exc)[:90]
    check("a quiet source with a DETAIL can be stamped", ok, why)
    if ok:
        st = json.loads(p.read_text())
        check("and the sweep is actually recorded", bool(st.get("last_swept_at")))
        check("a success clears any prior failure banner",
              st.get("last_sweep_failure") is None)
with tempfile.TemporaryDirectory() as t:
    p = fresh_state(t)
    try:
        S.mark_swept({"slack": {"checked": 0, "detail": ""},
                      "gmail": {"checked": 2, "detail": "x"}}, path=p)
        ok = False
    except ValueError:
        ok = True
    check("a source with NO detail is still refused (unproven, not quiet)", ok)
with tempfile.TemporaryDirectory() as t:
    p = fresh_state(t)
    try:
        S.mark_swept({"gmail": {"checked": 2, "detail": "x"}}, path=p); ok = False
    except ValueError:
        ok = True
    check("a source MISSING from the evidence is still refused", ok)
with tempfile.TemporaryDirectory() as t:
    p = fresh_state(t)
    try:
        S.mark_swept({"slack": {"detail": "x"},
                      "gmail": {"checked": 2, "detail": "x"}}, path=p)
        ok = False
    except ValueError:
        ok = True
    check("a source with NO `checked` key at all is refused", ok)

print("§4  the replacement safety net — a quiet STREAK is visible, and never discards")
with tempfile.TemporaryDirectory() as t:
    p = fresh_state(t)
    for _ in range(S.QUIET_STREAK_WARN):
        S.mark_swept(ev(0, 2), path=p)
    st = json.loads(p.read_text())
    check(f"slack quiet {S.QUIET_STREAK_WARN}x in a row is SURFACED",
          "slack" in (st.get("quiet_sources") or {}),
          f"quiet_sources={st.get('quiet_sources')}")
    check("a busy gmail is NOT surfaced", "gmail" not in (st.get("quiet_sources") or {}))
    check("every one of those sweeps still LANDED", bool(st.get("last_swept_at")))
    S.mark_swept(ev(4, 2), path=p)
    st = json.loads(p.read_text())
    check("one real message clears the streak", not (st.get("quiet_sources") or {}),
          f"quiet_sources={st.get('quiet_sources')}")

print()
if FAILS:
    print(f"🔴 {len(FAILS)} FAILED: " + ", ".join(FAILS)); sys.exit(1)
print("✅ all checks passed"); sys.exit(0)
