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

# ── transient API failures: RETRY, and never blame the connectors ─────────
# 🔴 2026-09-21. The sweep died 24 consecutive times (Fri 16:00 -> Mon 07:45) and
# the recorded reason every time was "no result reported by: gmail, slack - the
# run never said what it read". That reason was WRONG, and it pointed at the
# wrong system. A reproduction at 08:34 left this output, in full:
#
#     API Error: 529 Overloaded. This is a server-side issue, usually temporary
#     - try again in a moment.
#
# Neither connector was involved. The subprocess exited CLEANLY, printed an API
# error, emitted no markers, and fell through to the generic missing-marker
# branch - which names gmail and slack because those are the REQUIRED sources,
# not because either was asked anything. The banner then sent whoever read it to
# audit Slack permissions, and a live card about unreadable Slack channels made
# that misreading feel confirmed.
#
# Two defects, one line apart:
#   1. A 529 says "try again in a moment" and nothing ever tried again. The next
#      attempt was the next scheduled poll, 30-60 minutes later.
#   2. `proc.returncode` was never read, so a non-zero exit was
#      indistinguishable from a clean run that happened to say nothing.
#
# RETRY ONLY WHEN THE ATTEMPT EARNED NOTHING. An attempt that produced markers
# is a real result and is never thrown away to try for a better one - the
# 2026-09-17 rule (detection must not be implemented as data destruction)
# applied to retries.
_TRANSIENT_RE = re.compile(
    r"API Error:\s*(?:429|500|502|503|504|529)\b"
    r"|\boverloaded_error\b"
    r"|\bOverloaded\b"
    r"|\brate[_ ]limit(?:_error)?\b"
    r"|\bserver-side issue\b",
    re.I)

# Short on purpose: this job fires 19x/day and a cold sweep already costs ~12
# minutes. Two extra attempts at 20s and 60s cover a transient blip without
# letting one bad window eat the next poll's slot.
RETRY_BACKOFF_S = [int(x) for x in
                   os.environ.get("PM_SWEEP_BACKOFF", "20,60").split(",") if x.strip()]

# The last N failed outputs, not the last 1. LOG_PATH being a single overwritten
# file is why the weekend's 24 failures could not be diagnosed on Monday: the
# 08:00 run that finally succeeded destroyed the evidence of the ones that had
# not. Kept small - this is a diagnosis aid, not an archive.
FAIL_DIR = ROOT / "logs" / "failed_sweeps"
FAIL_KEEP = 12


def transient_error(text, returncode=0):
    """The one-line reason this attempt is worth retrying, or None.

    Reads the OUTPUT, not the exit code alone: the CLI exits 0 while printing
    `API Error: 529`, so a returncode check by itself sees a healthy run.
    """
    m = _TRANSIENT_RE.search(text or "")
    if m:
        return m.group(0)
    if returncode not in (0, None):
        return "the CLI exited %s" % returncode
    return None


def _meaningful_tail(text, n=400):
    """The tail of a run's output, with the CLI's own startup lint removed.

    🔴 2026-09-21. Three timed-out sweeps in one day all recorded the reason
    "Permission ask rule (../.claude/settings.json): Write(transaction_db.json)
    is not matched by file permission checks". The sweep does not write financial
    state at all — those are settings-lint warnings the CLI prints at STARTUP,
    and they were simply the last thing in the buffer. The real cause, sitting in
    logs/failed_sweeps/, was `API Error: 529 Overloaded`. A tail that reports
    whatever printed last will impersonate a diagnosis, and this file's own header
    already records one weekend lost to exactly that.
    """
    lines = [ln for ln in (text or "").splitlines()
             if not ln.startswith(("Permission allow rule", "Permission ask rule",
                                   "Permission deny rule"))]
    kept = "\n".join(lines).strip()
    return (kept or "(no output beyond CLI startup warnings)")[-n:].replace("\n", " ⏎ ")


def _keep_output(text, attempt=1):
    """Write LOG_PATH (unchanged contract) AND retain a rotated copy."""
    try:
        LOG_PATH.write_text(text)
    except Exception:
        pass
    try:
        FAIL_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        (FAIL_DIR / ("%s_try%d.txt" % (stamp, attempt))).write_text(text)
        old = sorted(FAIL_DIR.glob("*.txt"))[:-FAIL_KEEP]
        for f in old:
            f.unlink(missing_ok=True)
    except Exception:
        pass


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

THE BOARD AS IT STANDS — {board_count} cards, and this is the COMPLETE list,
not a sample. Every id below is reproduced in full. `[done]` and `[dismissed]`
are CLOSED cards: they are on this list so you do not raise them again.
{board}

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

STEP 4 — WRITE {out} as JSON: {{"items": [ ... ], "finance": [ ... ]}}

STEP 4a — ROUTE FINANCE OUT OF THE BOARD (her ruling, 2026-08-28). An item is
FINANCE when her next action is PAY / RECORD / RECONCILE / CHASE MONEY:
BuilderTrend "sent you a payment" receipts, invoices payable or receivable,
bank/card/loan/statement notices, payroll matters, insurance premiums and COI
expiries, credit applications, Andersen remittance or chargeback threads.
Finance items go in the "finance" list, NOT in "items" — the 2026-08-24 sweep
died inventing a `money` lane for exactly these; the finance list is where
they belong. When one thread is genuinely both (a sub emails an invoice inside
a scheduling thread), the money half is a finance entry and the work half is a
board card; give both the same key so they cross-reference.
Each finance entry:
  key — short stable slug (vendor + invoice/date, e.g. "bt_teresa_3483_0827")
  subject — one line, the dollar figure in it when the message states one
  action — HER next step ("record it", "expect it in the account Aug 29")
  amount — the NUMBER COPIED from the message, or null. NEVER estimate one.
  urgent — true only for money at risk or a deadline inside 48h
  detail — one sentence of context
  source_ref — the gmail thread id or slack ts it came from
These are LEADS for the financial dashboard, not ledger entries — the bank
reconcile stays the only thing that moves a balance.

Each board item in "items" follows the board contract:
  id — MATCH THE BOARD LIST ABOVE. If a card up there already covers this
      thread, reuse that card's id EXACTLY, character for character. That id
      is the ONLY thing that carries her status, note, did, assignee and
      defer forward; a near-miss id (`prism_coi` where the board says
      `s_prism_coi`) creates a SECOND card holding none of her work, and a
      card she already closed comes back as fresh. Mint a new id only when
      nothing on that list covers the thread.
  subject, source (a SHORT token: "slack" / "gmail" / "bt"), kind,
  project, meta, action
  lane — MUST be one of these EXACT strings. There is no other valid value,
      pm_ingest validates every item against this list, and ONE bad lane
      REJECTS THE ENTIRE BRIEFING — every other item with it. Do not invent a
      lane that describes the topic ("money", "client", "ops", "risk"): a lane
      is WHO ACTS NEXT and WHEN, not what the item is about. Put the topic in
      `kind` or `project` instead.
{lanes}
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
Only NEW or genuinely-changed items — the board list above is what "the board
already has" means, so this is now checkable rather than guessed. A `[done]`
or `[dismissed]` card is CLOSED: leave it alone unless something genuinely new
happened in that thread, and if it did, reuse its id so the update lands ON
the closed card instead of beside it as a duplicate.

STEP 5 — REPORT these on their own lines, exactly, even when the count is 0:
  SLACK_OK=<slack messages you actually retrieved>
  GMAIL_OK=<gmail threads you actually retrieved>
  SLACK_CURSOR=<newest slack ts seen, or the watermark if none>
  GMAIL_CURSOR=<newest gmail internalDate or ISO seen, or the watermark>
A machine reads these lines.
"""


def _board_text(state):
    """Render the cards the board ALREADY has, so the sweep can see them.

    STEP 4 has told the model "do not restate cards the board has" since the
    day this ran — an instruction it had no information to obey, because
    build_prompt passed only the watermarks, the registry and the output path.
    The board itself was never in the prompt. So the model had to re-derive an
    id from the subject on every run, and a near-miss — `prism_coi` today for
    the `s_prism_coi` it wrote yesterday — mints a SECOND card for one thread.

    That is not cosmetic. pm_ingest preserves HER_FIELDS (status, done_at,
    did, note, assignee, defer) by MATCHING THE ID; a duplicate under a
    different id inherits none of them. 36 of the 107 cards on the board are
    done or dismissed, and a blind sweep can resurrect any of them as fresh
    open work — her completed work silently undone.

    CLOSED cards are listed too, deliberately: they are exactly the ones that
    must not come back. Subjects are truncated for width; ids never are — the
    id is the thing the model has to match, so it is reproduced in full.
    """
    items = (state or {}).get("items") or []
    if not items:
        return "  (the board is empty — every item you write is new)"
    def _order(i):
        st = i.get("status") or "open"
        return (st != "open", st, i.get("id") or "")

    rows = []
    for it in sorted(items, key=_order):
        subj = " ".join((it.get("subject") or "").split())
        if len(subj) > 110:
            subj = subj[:109] + "…"
        rows.append("  [%-9s] %-28s %s"
                    % (it.get("status") or "open", it.get("id") or "(no id)", subj))
    return "\n".join(rows)


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


def _unhealthy(counts):
    """Sources that are genuinely BROKEN — never merely quiet.

    🔴 2026-09-17. This used to be `n <= 0`, and on that day it cost a real run:
    the 14:05 sweep read 25 Slack channels across 2 pages plus 5 DMs, honestly
    reported SLACK_OK=0 for a quiet 73 minutes, read 2 Gmail threads, and wrote a
    VALIDATED briefing of 3 board items + 1 finance lead — and the zero aborted it
    before the ingest, discarding all four. Her question that surfaced it: *"is it
    only slack's sweep that's failing? or emails and buildertrend as well?"*
    Neither was failing.

    THE ORIGINAL WORRY IS REAL and is not being discarded: a connector that breaks
    silently returns nothing, which from outside looks like a quiet window. But the
    module docstring already names the mechanism that discriminates — "a MISSING
    marker is a failure, never a zero" — and STEP 5 of the prompt orders the run to
    emit the markers "even when the count is 0". The protocol ASKS for a zero and
    the parser then read it as death. Three things still catch a dead connector:
    the missing marker (_parse_markers), a negative count (here), and a quiet
    STREAK (pm_state.QUIET_STREAK_WARN) — and none of the three destroys a good run.

    THE PRINCIPLE: detection must never be implemented as data destruction. A
    detector that throws away verified work to signal a maybe-problem is worse than
    the problem, because the loss is certain and the problem is not.
    """
    return sorted(s for s, n in counts.items() if n < 0)


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


# ── the lane vocabulary, rendered FROM pm_state.LANES ─────────────────────
# 2026-08-24: the prompt never stated the valid lanes. A 14m45s run that
# retrieved 320 Slack messages and 158 Gmail threads produced 30 items — and
# ALL THIRTY were discarded, because the model reasonably invented topical
# lanes (money x10, client x7, ops x7, process x3, compliance x2, risk x1) and
# validation is all-or-nothing. Zero of thirty used a real lane. It could not
# have guessed them: `grep -c urgent pm_sweep_run.py` was 0.
#
# This is the 2026-07-30 draft-shape failure in a different field — that one
# discarded 19 good items because the contract said "MUST carry a draft" and
# left the shape to be guessed. The shape got fixed; the LANE vocabulary was
# left implicit. Same defect, next field over.
#
# Rendered from S.LANES rather than typed out, because a hardcoded list would
# already be stale: `routine` was added TODAY. The assert is the load-bearing
# part — adding a lane to pm_state without describing it here fails loudly at
# build time instead of quietly shipping a prompt that omits it.
LANE_HELP = {
    "urgent":    "she must act TODAY — money at risk, or a deadline inside 24h",
    "routine":   "DO NOT USE. Owned by pm_routine.py, which mints only `rt_` ids "
                 "and resets them every morning; a sweep card here is wiped daily",
    "action":    "hers to do, but not today-or-else",
    "week":      "hers, this week — no fixed deadline",
    "rafael":    "waiting on RAFAEL to act or answer",
    "guilherme": "waiting on GUILHERME to act or answer",
    "claude":    "Claude can finish it without her — research, drafting, a lookup",
    "noise":     "she should SEE it, but nothing is owed by anyone",
}


def _lanes_text():
    missing = [ln for ln in S.LANES if ln not in LANE_HELP]
    assert not missing, (
        "pm_state.LANES gained %s with no LANE_HELP entry — the sweep prompt "
        "would omit it and the model could not use it" % (missing,))
    return "\n".join("        %-11s — %s" % (ln, LANE_HELP[ln]) for ln in S.LANES)


def build_prompt(state):
    wms = {s: S.get_watermark(state, s) or "" for s in S.REQUIRED_SWEEP_SOURCES}
    out = Path(tempfile.gettempdir()) / "pm_sweep_briefing.json"
    return wms, out, PROMPT.format(
        slack_wm=wms.get("slack") or "(none yet — read today only)",
        gmail_wm=wms.get("gmail") or "(none yet — read today only)",
        registry=_registry_text(), out=out,
        board=_board_text(state), lanes=_lanes_text(),
        board_count=len((state or {}).get("items") or []))


def _fail(path, reason, sources):
    S.record_sweep_failure(reason, sources, path=path)
    return {"ok": False, "reason": reason, "sources": sources}


# ── the finance route (her ruling 2026-08-28: one sweep, one router, two sinks) ──
# Finance-classified leads land in the financial dashboard's intake file, which
# needs_you.build_feed() reads into Today's cards. Deliberately ASYMMETRIC with
# pm_ingest: the board briefing stays all-or-nothing (its contract), but a
# malformed finance LEAD is dropped-and-recorded per item — the 2026-07-30 and
# 2026-08-24 failures both destroyed 12-minute runs over one shape defect, and
# a lead list must never re-create that blast radius.
FIN_INTAKE = Path(os.environ.get(
    "PM_FIN_INTAKE", "/Users/Hadassa/rfs_dashboard/finance_intake.json"))
_FIN_REQUIRED = ("key", "subject", "action")


def route_finance(fin_items, path=FIN_INTAKE, now=None):
    """Validate per item and upsert by key into the finance intake file.

    Existing rows keep their `consumed` flag (Claude flips it at the session
    reads); re-seen keys bump last_seen and refresh content. Atomic flock +
    tmp + os.replace, same pattern as needs_you_state.
    """
    import fcntl
    stamp = (now or datetime.datetime.now().astimezone()).isoformat(timespec="seconds")
    good, dropped = [], []
    seen_keys = set()
    for i, r in enumerate(fin_items or []):
        if not isinstance(r, dict):
            dropped.append("item[%d] is not an object" % i)
            continue
        missing = [k for k in _FIN_REQUIRED if not str(r.get(k) or "").strip()]
        if missing:
            dropped.append("item[%d] (%s) missing %s"
                           % (i, r.get("key") or "?", "+".join(missing)))
            continue
        amt = r.get("amount")
        if amt is not None and not isinstance(amt, (int, float)):
            dropped.append("item[%d] (%s) amount is %r — number or null only"
                           % (i, r["key"], amt))
            continue
        key = str(r["key"]).strip()
        if key in seen_keys:
            dropped.append("item[%d] duplicate key %s in one briefing" % (i, key))
            continue
        seen_keys.add(key)
        good.append({"key": key, "subject": str(r["subject"]).strip(),
                     "action": str(r["action"]).strip(),
                     "amount": float(amt) if amt is not None else None,
                     "urgent": bool(r.get("urgent")),
                     "detail": str(r.get("detail") or "").strip(),
                     "source_ref": str(r.get("source_ref") or "").strip()})
    if not good and not dropped:
        return {"routed": 0, "dropped": []}
    lock_path = str(path) + ".lock"
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            try:
                data = json.loads(Path(path).read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                data = {"_schema": "finance_intake v1", "items": []}
            by_key = {it["key"]: it for it in data.get("items", [])}
            for g in good:
                old = by_key.get(g["key"])
                if old:
                    old.update(g)                      # refresh content
                    old["last_seen"] = stamp           # consumed flag survives
                else:
                    g["first_seen"] = g["last_seen"] = stamp
                    g["consumed"] = False
                    by_key[g["key"]] = g
            data["items"] = list(by_key.values())
            data["updated_at"] = stamp
            tmp = str(path) + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(data, fh, indent=1, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, str(path))
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return {"routed": len(good), "dropped": dropped}


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
    attempts, text, rc = [], "", 0
    for _try in range(len(RETRY_BACKOFF_S) + 1):
        try:
            proc = subprocess.run([CLAUDE_BIN, "-p", prompt], capture_output=True,
                                  text=True, timeout=TIMEOUT_S, cwd=str(ROOT))
            text = (proc.stdout or "") + "\n" + (proc.stderr or "")
            rc = proc.returncode
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
            _keep_output(tail, attempt=_try + 1)
            # An API error inside a timed-out run IS the diagnosis. Lead with it;
            # the tail goes after, and only once the CLI's startup lint is stripped.
            _api = transient_error(tail, 0)
            _reason = ("the sweep timed out after %ds — and the run had already hit a "
                       "TRANSIENT API ERROR: %s (this is the cause; the tail below is "
                       "context, not a diagnosis) — last output: %s"
                       % (TIMEOUT_S, _api, _meaningful_tail(tail))) if _api else (
                       "the sweep timed out after %ds with no API error in its output — "
                       "last output: %s" % (TIMEOUT_S, _meaningful_tail(tail)))
            return _fail(path, _reason, list(S.REQUIRED_SWEEP_SOURCES))
        except FileNotFoundError:
            return _fail(path, "the `%s` CLI is not on PATH — a LaunchAgent does "
                               "not inherit your shell profile" % CLAUDE_BIN,
                         list(S.REQUIRED_SWEEP_SOURCES))

        # Kept on EVERY attempt, not only failures: "no result reported by slack"
        # is unactionable without the transcript that failed to report it.
        _keep_output(text, attempt=_try + 1)

        if not _parse_markers(text)[2]:
            break                     # earned a real result — never retry over it
        _why = transient_error(text, rc)
        attempts.append("attempt %d: %s"
                        % (_try + 1, _why or "no markers and no API error"))
        if not _why or _try >= len(RETRY_BACKOFF_S):
            break
        time.sleep(RETRY_BACKOFF_S[_try])

    # A run that only ever hit transient API errors must SAY SO. Reporting
    # "gmail and slack said nothing" for an API 529 sent the diagnosis to the
    # wrong system for three days.
    _why = transient_error(text, rc)
    if _parse_markers(text)[2] and _why:
        return _fail(path,
                     "the model call failed before it could read anything — %s. "
                     "This is the API, NOT gmail or slack; neither was reached. "
                     "Tried %d time(s): %s"
                     % (_why, len(attempts), "; ".join(attempts)),
                     list(S.REQUIRED_SWEEP_SOURCES))

    counts, cursors, missing = _parse_markers(text)
    if missing:
        return _fail(path, "no result reported by: %s — the run never said what "
                           "it read" % ", ".join(missing), missing)
    dead = _unhealthy(counts)
    if dead:
        return _fail(path, "a required source reported an ERROR: %s"
                     % ", ".join(dead), dead)

    items, fin_raw = [], []
    if out.exists():
        try:
            _data = json.loads(out.read_text()) or {}
            items = _data.get("items") or []
            fin_raw = _data.get("finance") or []
        except Exception as exc:
            return _fail(path, "the briefing file was unreadable: %s" % exc, [])

    res = {"ok": True, "counts": counts, "ingested": 0, "trimmed": [],
           "wrap": bool(wrap)}
    if fin_raw:
        # Routed BEFORE the board ingest on purpose: a board-validation refusal
        # must not throw away finance leads that already validated per-item.
        _fin = route_finance(fin_raw)
        res["finance_routed"] = _fin["routed"]
        if _fin["dropped"]:
            res["finance_dropped"] = _fin["dropped"]
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
