#!/usr/bin/env python3
"""The live watermarked sweep RUNNER (2026-07-30).

Companion to pm_sweep.py, which stays the PLANNER — it owns `sweep_sources.json`
(the channel/mailbox registry) and the sweep contract. This module owns the
part that runs on a schedule: the watermarks, the subprocess, the evidence
gate, and the failure record. The registry is read from there, never restated
here — a second copy of the channel list is exactly how `#99-concord` got
missed on 2026-07-29.

⚠️ A CORRECTION TO pm_sweep.py's ORIGINAL PREMISE, and the reason this exists.
    That module (2026-07-29) states the connectors "cannot exist on this machine
    today" from a subprocess, and at the time that was the honest read. It was
    disproven on 2026-07-30: a headless `claude -p` run loaded both connectors
    and returned real data (SLACK_TOOL_LOADED=yes SLACK_CALL=ok RESULTS=11,
    GMAIL_TOOL_LOADED=yes GMAIL_CALL=ok THREADS=5). The catch is that it only
    works when the prompt explicitly tells the model to ToolSearch-load them
    first — they are DEFERRED tools, so a prompt that just says "search Slack"
    fails silently and returns a confident empty result. That is the single
    most important line in the prompt below.

WHY THE MODEL IS NOT TRUSTED TO SAY THE SWEEP HAPPENED
    The model decides what a message MEANS. It does not decide whether the
    sweep counts. That is S.mark_swept(), which raises unless every required
    source reports a non-zero count and names what it queried — a gate that
    exists because a caller once self-asserted a sweep covering only Slack.

ZERO IS A FAILURE, NOT "ALL QUIET"
    A poll that reaches Gmail but gets nothing from Slack yields a board that
    is both freshly stamped and missing half the day. From the outside that is
    indistinguishable from a genuinely quiet half-hour, which is why the
    difference is FORCED rather than inferred: the run must echo SLACK_OK=<n>
    and GMAIL_OK=<n>, and a MISSING marker is a failure, never a zero.

WATERMARKS ADVANCE ONLY AFTER A SUCCESSFUL INGEST
    Advancing on a successful fetch means a crash between fetch and write loses
    those messages permanently and silently — nothing would ever look wrong.
    Re-reading a few is free; the ingest is idempotent by id. Losing one is not.

Usage:
    python3 pm_sweep_run.py                # poll; ingest if anything arrived
    python3 pm_sweep_run.py --open-browser  # ... and open the board (07:45)
    python3 pm_sweep_run.py --wrap          # the 15:45 run
    python3 pm_sweep_run.py --dry-run       # print the prompt, write nothing
"""
import argparse
import datetime
import json
import os
import re
import time
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import pm_state as S          # noqa: E402
import pm_ingest as I         # noqa: E402
import pm_sweep as PLAN       # noqa: E402  — the registry lives THERE

BOARD_URL = "http://127.0.0.1:8789"

# ── finding the `claude` binary, which is NOT on PATH on this machine ──
# Verified 2026-07-30: `which claude` returns nothing, and it is absent from
# every usual install location. The only copy is the one bundled inside the
# VSCode extension:
#   ~/.vscode/extensions/anthropic.claude-code-<VERSION>-darwin-arm64/
#       resources/native-binary/claude
# That path carries a VERSION NUMBER, and two versions are already installed
# side by side (2.1.218 and 2.1.220) — so it churns on every extension update.
# A LaunchAgent with this path baked in would keep working until the next
# update and then fail silently forever, which is precisely the
# "scheduled job that silently returns nothing" failure pm_sweep.py warns
# about. So it is resolved at RUN time, newest first, and a failure to find it
# is a loud, recorded failure rather than an empty sweep.
_EXT_GLOB = "anthropic.claude-code-*/resources/native-binary/claude"


def _resolve_claude():
    override = os.environ.get("CLAUDE_BIN")
    if override:
        return override
    from shutil import which
    found = which("claude")
    if found:
        return found
    cands = sorted((Path.home() / ".vscode" / "extensions").glob(_EXT_GLOB))

    def _ver(p):
        m = re.search(r"claude-code-(\d+)\.(\d+)\.(\d+)", str(p))
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)

    cands = [c for c in cands if os.access(c, os.X_OK)]
    return str(max(cands, key=_ver)) if cands else "claude"


CLAUDE_BIN = _resolve_claude()
# Generous on purpose. The model loads two MCP schemas and may read threads. A
# hung poll is a failure, but a poll killed at 60s on a slow morning is a false
# alarm — and false alarms are how a red banner turns into wallpaper.
#
# 600 → 1200 after MEASURING it (2026-07-30): a cold run with no watermark reads
# the whole day across 12 channels plus Gmail and took **12m06s** (16:37:29 →
# 16:49:35, SLACK_OK=53 GMAIL_OK=30). 600s killed it mid-flight. A cold run is
# the 07:45 case — the overnight window is the widest one — so the ceiling has to
# clear it. The :00/:30 polls read a 30-minute window and should be far quicker;
# if they are not, the cadence is wrong, not the timeout.
TIMEOUT_S = int(os.environ.get("PM_SWEEP_TIMEOUT", "1200"))
# Where a failed run's output is kept so a timeout can be diagnosed at all.
# One file, overwritten each time: the interesting run is always the last one,
# and an unbounded log on a job that fires 19×/day is its own problem.
LOG_PATH = ROOT / "last_sweep_output.txt"

# ANCHORED to line starts, deliberately. An unanchored SLACK_OK=(-?\d+) is
# satisfied by any *prose sentence* that happens to mention a count — which is
# exactly what happened on 2026-07-30: run 3 wrote "Run 3's result: SLACK_OK=51
# · GMAIL_OK=33, 23 items…" instead of the standalone marker lines STEP 5 asks
# for, the counts matched out of the prose by luck, and no cursor was parsed at
# all. The mirror risk is worse: a sentence containing SLACK_OK=0 would
# fabricate a failure on a healthy run.
OK_RE = {"slack": re.compile(r"^\s*SLACK_OK=(-?\d+)\s*$", re.M),
         "gmail": re.compile(r"^\s*GMAIL_OK=(-?\d+)\s*$", re.M)}
CURSOR_RE = {"slack": re.compile(r"^\s*SLACK_CURSOR=(\S+)\s*$", re.M),
             "gmail": re.compile(r"^\s*GMAIL_CURSOR=(\S+)\s*$", re.M)}


def _fallback_cursor(source, started_at):
    """A watermark derived from the run's START — never its end.

    Missing cursors are what make every run a COLD run: with no watermark the
    prompt says "read today only", so the sweep re-reads the whole day across
    12+ channels and costs ~12 minutes, 19× a day on the approved schedule.

    Requiring the model to emit a cursor (the way counts are required) would
    fix the slowness but reintroduce the 2026-07-30 run-2 failure mode: one
    unmet formatting rule discarding a briefing that took 12 minutes to earn.
    So counts stay a hard gate — a count is the *evidence a connector was
    reached* — and a missing cursor instead falls back to this value.

    START, not end, is the safe bound: the run read everything newer than the
    old watermark up to whenever it looked, so anything that landed mid-run is
    either already captured or will be re-read next time. Overlap is harmless
    (the ingest dedupes); a gap would be silent data loss.
    """
    if source == "slack":
        return "%.6f" % started_at
    return datetime.datetime.fromtimestamp(
        started_at, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

PROMPT = """\
You are the RFS PM board sweep. Read what has arrived since the watermarks
below, write a briefing file, and report what you actually retrieved. Do not
address the user; produce the file and the markers.

WATERMARKS — read only what is NEWER than these:
  slack: {slack_wm}
  gmail: {gmail_wm}

STEP 1 — LOAD THE TOOLS FIRST. The Slack and Gmail connectors are DEFERRED
tools: their schemas are NOT loaded and calling them directly fails with an
InputValidationError. You MUST call ToolSearch before any connector call, e.g.
  ToolSearch("select:mcp__claude_ai_Slack__slack_search_public_and_private")
  ToolSearch("select:mcp__claude_ai_Gmail__search_threads")
If ToolSearch returns nothing for a connector, that connector is UNAVAILABLE:
report 0 for it and say why. NEVER report a count you did not get from a tool
result — an invented zero and a real zero are indistinguishable downstream.

STEP 2 — LIST BEFORE YOU READ. Call slack_search_channels with
channel_types=public_channel,private_channel and diff the result against the
registry below. A channel that is NOT in the registry is itself a finding, and
must still be read. This step exists because a sweep on 2026-07-29 was believed
complete while missing six task assignments in a channel created that morning.

REGISTRY (known sources — not a limit on what to read):
{registry}

STEP 3 — READ everything newer than the watermark, Slack and Gmail both.
Where a message leaves "who does what" ambiguous, READ THE THREAD before
deciding — a card with the wrong owner is worse than no card.

STEP 3b — READ HER OWN SENDS. Query `label:SENT` — NOT `in:sent`, which
returns empty and is not a statement about the mailbox. On 2026-07-31 the
SENT label held 233 messages while `in:sent` returned nothing, and that one
bad query was reported to her as "no outbound is confirmable from Gmail" in a
PUBLISHED report. Her sends are how a chase gets CLOSED: a reply in a thread
proves the outreach happened, and so does her own message in it.
  KNOWN GAP, state it rather than paper over it: `label:SENT` covers only mail
  sent FROM this account. When she replies from inside office@, billing@,
  info@ or rfscarpentryservices@ (separate logins), that send is visible ONLY
  if an RFS address is on To or CC. If a card turns on whether she sent
  something and no send is visible, say "not visible from Gmail" — never
  "she did not send it".
Read TONE, not only imperatives: a worry about a client or a payment, a
decision stated in passing, a number or name mentioned once, someone saying a
thing is already done (that CLOSES a card), an unanswered question, a verbal
authorisation that will later hit payroll or the ledger.

STEP 4 — WRITE {out} as JSON: {{"items": [ ... ]}}
Each item follows the board contract:
  id, subject, source (a SHORT token: "slack" / "gmail" / "bt"), lane, kind,
  project, meta, action
  ctx_happened / ctx_matters / ctx_needed — ONE SENTENCE EACH, max 160 chars.
      HAPPENED = the fact · MATTERS = the consequence · NEEDED = the ask.
      The verbatim text goes in ctx_body, NOT in these three.
  ctx_body: [...]  — the source lines, inline HTML, no <p> wrappers.
  claude_done: [...]  — what you already did for her; [] if genuinely nothing.
  hadassa_todo: [...] — what only she can do.
  Any card asking her to get something from a person MUST carry a `draft`,
      and a draft is an OBJECT, never a string:
        "draft": {{"to": "...", "subject": "...", "body": "..."}}
      All three keys are required. pm_ingest REFUSES the whole briefing if any
      draft is a bare string — measured on 2026-07-30, when a 12-minute run
      produced 19 good items and every one was discarded because this line said
      only "MUST carry a draft" and left the shape to be guessed.
      `to` may be a name when the address is unknown ("Rafael", "Saba + MK").
Only NEW or genuinely-changed items. Do not restate cards the board has.

STEP 5 — REPORT these on their own lines, exactly, even when the count is 0:
  SLACK_OK=<slack messages you actually retrieved>
  GMAIL_OK=<gmail threads you actually retrieved>
  SLACK_CURSOR=<newest slack ts seen, or the watermark if none>
  GMAIL_CURSOR=<newest gmail internalDate or ISO seen, or the watermark>
A machine reads these lines.
"""


def _registry_text():
    """Render the registry from pm_sweep.py — never a second hardcoded copy."""
    try:
        src = PLAN.load_sources()
    except Exception as exc:
        return "  (registry unreadable: %s — read every channel you can find)" % exc
    out = []
    for group, chans in src.get("slack", {}).items():
        for c in chans:
            note = (" — %s" % c["note"]) if c.get("note") else ""
            out.append("  slack %-13s %s%s" % (c["id"], c["name"], note))
    for m in src.get("email", []):
        out.append("  mail  %s — %s" % (m["address"], m.get("note", "")))
    return "\n".join(out) or "  (registry empty)"


def _parse_markers(text):
    """(counts, cursors, missing) — an ABSENT marker is not a zero."""
    counts, cursors, missing = {}, {}, []
    for src, rx in OK_RE.items():
        m = rx.search(text or "")
        if m:
            counts[src] = int(m.group(1))
        else:
            missing.append(src)
    for src, rx in CURSOR_RE.items():
        m = rx.search(text or "")
        if m:
            cursors[src] = m.group(1)
    return counts, cursors, sorted(missing)


def build_prompt(state):
    wms = {s: S.get_watermark(state, s) or "" for s in S.REQUIRED_SWEEP_SOURCES}
    out = Path(tempfile.gettempdir()) / "pm_sweep_briefing.json"
    return wms, out, PROMPT.format(
        slack_wm=wms.get("slack") or "(none yet — read today only)",
        gmail_wm=wms.get("gmail") or "(none yet — read today only)",
        registry=_registry_text(), out=out)


def _fail(path, reason, sources):
    S.record_sweep_failure(reason, sources, path=path)
    return {"ok": False, "reason": reason, "sources": sources}


def run_sweep(path=None, dry_run=False, open_browser=False, wrap=False):
    path = path or S.STATE_PATH
    state, status = S.load_state(path)
    if state is None:
        return {"ok": False, "reason": "state unreadable (%s)" % status}

    wms, out, prompt = build_prompt(state)
    if dry_run:
        return {"ok": True, "dry_run": True, "watermarks": wms,
                "out": str(out), "prompt_chars": len(prompt), "prompt": prompt}

    # Stamped BEFORE the call: this is the conservative fallback watermark for
    # any source the run forgets to report a cursor for. See _fallback_cursor.
    started_at = time.time()
    try:
        proc = subprocess.run([CLAUDE_BIN, "-p", prompt], capture_output=True,
                              text=True, timeout=TIMEOUT_S, cwd=str(ROOT))
        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        # Keep whatever the run had already produced. Discarding it made the
        # first real timeout (2026-07-30 16:35) completely undiagnosable: the
        # failure said "timed out" and nothing else, so there was no way to tell
        # a stuck connector from a run that was simply still working. The
        # markers are printed LAST, so a timed-out run has no counts — but the
        # tail shows how far it got, which is the whole question.
        partial = ((exc.stdout or "") if isinstance(exc.stdout, str)
                   else (exc.stdout or b"").decode("utf-8", "replace"))
        perr = ((exc.stderr or "") if isinstance(exc.stderr, str)
                else (exc.stderr or b"").decode("utf-8", "replace"))
        tail = ((partial + "\n" + perr).strip() or "(the run produced no output at all)")
        try:
            LOG_PATH.write_text(tail)
        except Exception:
            pass
        return _fail(path, "the sweep timed out after %ds — last output: %s"
                     % (TIMEOUT_S, tail[-400:].replace("\n", " ⏎ ")),
                     list(S.REQUIRED_SWEEP_SOURCES))
    except FileNotFoundError:
        return _fail(path, "the `%s` CLI is not on PATH — a LaunchAgent does "
                           "not inherit your shell profile" % CLAUDE_BIN,
                     list(S.REQUIRED_SWEEP_SOURCES))

    # Kept on EVERY run, not only failures: "no result reported by slack" is
    # unactionable without the transcript that failed to report it.
    try:
        LOG_PATH.write_text(text)
    except Exception:
        pass

    counts, cursors, missing = _parse_markers(text)
    if missing:
        return _fail(path, "no result reported by: %s — the run never said what "
                           "it read" % ", ".join(missing), missing)
    dead = [s for s, n in counts.items() if n <= 0]
    if dead:
        return _fail(path, "a required source returned nothing: %s"
                     % ", ".join(dead), dead)

    items = []
    if out.exists():
        try:
            items = (json.loads(out.read_text()) or {}).get("items") or []
        except Exception as exc:
            return _fail(path, "the briefing file was unreadable: %s" % exc, [])

    res = {"ok": True, "counts": counts, "ingested": 0, "trimmed": [],
           "wrap": bool(wrap)}
    if items:
        try:
            got = I.ingest(items, path=path)
        except ValueError as exc:
            # `.splitlines()[0]` alone was useless: pm_ingest puts the headline on
            # line 1 and every actual defect on the lines after it, so the recorded
            # failure read "briefing is invalid, nothing was written:" and stopped
            # at the colon. Keep the first few defects — that is the whole message.
            lines = [l.strip() for l in str(exc).splitlines() if l.strip()]
            detail = "; ".join(lines[1:5]) or (lines[0] if lines else "no detail")
            more = "" if len(lines) <= 5 else " (+%d more)" % (len(lines) - 5)
            return _fail(path, "the briefing failed validation: %s%s"
                         % (detail, more), [])
        res["ingested"] = len(got["added"]) + len(got["updated"])
        res["trimmed"] = got.get("trimmed") or []

    # Evidence FIRST, watermarks second. If mark_swept() refuses, the cursors
    # must not have moved — otherwise a refused sweep still consumes the
    # messages it declined to record, and they are never seen again.
    S.mark_swept({s: {"checked": counts[s],
                      "detail": "polled since %s" % (wms.get(s) or "start of day")}
                  for s in S.REQUIRED_SWEEP_SOURCES}, path=path)
    # Every required source gets a watermark, reported or derived — otherwise
    # the next run is cold again and the 12-minute full-day re-read repeats.
    derived = []
    for src in S.REQUIRED_SWEEP_SOURCES:
        cur = cursors.get(src)
        if not cur:
            cur = _fallback_cursor(src, started_at)
            derived.append(src)
        if cur and cur != wms.get(src):
            S.advance_watermark(src, cur, path=path)
    if derived:
        # Loud, not silent: a derived watermark is correct but coarser than a
        # real cursor, and a run that never emits markers is a prompt defect
        # that should stay visible instead of being quietly absorbed.
        res["cursor_derived"] = sorted(derived)
        res["cursor_note"] = ("no cursor reported by %s — watermark set from the "
                              "run's start time instead" % ", ".join(sorted(derived)))

    if open_browser:
        subprocess.run(["/usr/bin/open", BOARD_URL], check=False)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="the PM board's polling sweep")
    ap.add_argument("--open-browser", action="store_true",
                    help="open the board on success (the 07:45 run)")
    ap.add_argument("--wrap", action="store_true", help="the 15:45 wrap run")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would run; write nothing")
    a = ap.parse_args(argv)
    res = run_sweep(dry_run=a.dry_run, open_browser=a.open_browser, wrap=a.wrap)
    if a.dry_run:
        print(res.pop("prompt", ""))
    print(json.dumps(res, indent=1, default=str))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
