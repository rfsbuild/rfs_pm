#!/usr/bin/env python3
"""Daily report — Financial · Systems · PM per project.

Phase 3 of the PM Command Center plan. Hadassa asked (2026-07-28): "will
everything recorded there in the end of the day be gathered for the daily
report? can we work on the mockup for the new daily report while Alice is away
and I'm taking control of the PM as well?"

Answer: yes — this reads BOTH sources and writes one report.

  ~/rfs_pm/pm_state.json          -> the PM half: what moved, per project,
                                     plus her notes and every "not needed"
                                     decision WITH the reason she gave
  ~/rfs_dashboard/dashboard_state.json -> the Financial half: reconciled
                                     balances, forecast trough vs floor, AR

DISCIPLINE — money
    Every financial figure is READ from dashboard_state.json at run time and
    rendered with its source labelled. Nothing is recalled, recomputed from
    memory, or estimated. If a figure is not present in state, the report says
    so rather than guessing — a report that quietly invents a number is worse
    than one with a gap. Per house rule, no cheap-model output ever supplies a
    figure here; these come straight off the file.

    This generator does NOT write to any state file. It is read-only.

Usage:
    python3 gen_daily_report.py                 # today, -> ~/Desktop/...
    python3 gen_daily_report.py --out FILE
    python3 gen_daily_report.py --open          # open it in Chrome
"""
import argparse
import datetime
import html
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_state as S  # noqa: E402

DASH = os.path.expanduser("~/rfs_dashboard/dashboard_state.json")
# The last APPROVED report is the standard this one is measured against.
# On 2026-07-30 a 11,778-byte report was written and shown to Hadassa without
# anyone opening the 37,648-byte report she had approved the day before. It was
# missing the amounts, the names, the written-vs-sent distinction, the
# waiting-on section and the cash scenarios — a wholesale regression that no
# check would have caught, because nothing compared the two.
PUBLISHED_DIR = os.path.expanduser("~/rfs-dailyreport")
DEFAULT_OUT = os.path.expanduser("~/Desktop/Claude Cowork/Emails/daily-report-%s.html")

e = html.escape


def money(v):
    if v is None:
        return '<span class="gap">not in state</span>'
    try:
        return "$%s" % format(float(v), ",.2f")
    except (TypeError, ValueError):
        return e(str(v))


def load_dash():
    try:
        with open(DASH) as f:
            return json.load(f), None
    except Exception as ex:
        return {}, str(ex)


# ── FINANCIAL ────────────────────────────────────────────────────────────────

def financial(d, err):
    if err:
        return ('<p class="gap">dashboard_state.json could not be read (%s) — '
                'the financial section is intentionally blank rather than estimated.</p>' % e(err))
    bank = d.get("bank_balance")
    card = d.get("cc_capital_one")
    fc = d.get("_cash_forecast") or {}
    days = fc.get("days") or []
    floor = fc.get("buffer_floor")

    trough = tro_date = None
    for day in days:
        v = day.get("eod_planned")
        if v is None:
            continue
        if trough is None or v < trough:
            trough, tro_date = v, day.get("date")

    breach = [dd for dd in days
              if dd.get("eod_planned") is not None and floor is not None
              and dd["eod_planned"] < floor]

    # "ahead" is only true when the low point is a FUTURE day. Hadassa caught
    # this on 2026-07-28: the report read "the tightest day ahead is Tuesday,
    # July 28" on Tuesday, July 28. Computed once here because BOTH the tile
    # sub-label and the outlook sentence below have to respect it.
    _is_today = (tro_date is not None and tro_date == S._today())
    _low_label = "lowest point today" if _is_today else "lowest point ahead"

    # Three tiles, matching the published reports. No A/R table — her
    # published dailies have never carried one ("we didn't have in the other
    # ones", 2026-07-28) and a seven-row table is unreadable on a phone.
    # Cap One pending charges are NOT in the posted balance, so the tile would
    # understate what is really owed. Say it in the sub-label rather than
    # silently adding it to the headline number (Hadassa, 2026-07-28).
    _ccp = ((d.get("_cc_pending") or {}).get("capital_one") or [])
    _ccp_tot = sum(float(p.get("amount") or 0) for p in _ccp)
    _card_sub = "Capital One, owed"
    if _ccp_tot:
        _card_sub += " · plus %s pending, not yet posted" % money(_ccp_tot)

    tiles = [
        ("Cash on hand", money(bank), "",
         "Citizens checking, available balance — pending holds already deducted"),
        ("Card balance", money(card), "", _card_sub),
        ("Projected low", money(trough),
         "bad" if (floor and trough is not None and trough < floor) else "ok",
         "%s%s" % (_low_label, " · %s" % _pretty_date(tro_date) if tro_date else "")),
    ]
    out = ['<div class="tiles">']
    for k, v, cl, sub in tiles:
        # ("tile " + cl).strip() — an empty severity slot was emitting
        # class="tile " with a trailing space (an empty modifier), which reads
        # as a missing class rather than a deliberate neutral tile.
        out.append('<div class="%s"><div class="k">%s</div><div class="v">%s</div>'
                   '<div class="sub">%s</div></div>'
                   % (("tile " + cl).strip(), e(k), v, e(sub)))
    out.append("</div>")

    # One short plain sentence per idea. The forecast `notes` field is NOT
    # printed: it is 1,377 characters of internal reconciliation shorthand, it
    # was being sliced at 600 (cutting mid-word — the stray "Al" she spotted),
    # and she has a standing rule against dense blocks. The outlook below is
    # derived from the same stored numbers instead.
    lines = []
    if bank is not None:
        lines.append("Cash stands at %s this evening." % money(bank))
    if trough is not None and floor is not None:
        # "ahead" is only true if the trough is a FUTURE day. Hadassa caught this
        # on 2026-07-28: the report read "the tightest day ahead is Tuesday,
        # July 28" on Tuesday, July 28. When the low point is today, say so.
        _pretty = _pretty_date(tro_date)
        if trough < floor:
            head = ("The tightest day is today, %s," % _pretty) if _is_today \
                else ("The tightest day ahead is %s" % _pretty)
            lines.append("%s at %s, which is %s under the %s floor."
                         % (head, money(trough), money(round(floor - trough, 2)),
                            money(floor)))
        else:
            head = ("The lowest point is today, %s," % _pretty) if _is_today \
                else ("The lowest point ahead is %s" % _pretty)
            lines.append("%s at %s, staying above the %s floor."
                         % (head, money(trough), money(floor)))
    if len(breach) > 1:
        lines.append("%d days in the window fall below the floor." % len(breach))
    if lines:
        out.append('<div class="outlook">%s</div>'
                   % "".join("<p>%s</p>" % l for l in lines))
    return "\n".join(out)


def _pretty_date(iso, with_year=False):
    """2026-07-31 -> Friday, July 31 (optionally ", 2026").

    A date FD can read at a glance, matching the published reports' header.
    """
    if not iso:
        return ""
    try:
        dt = datetime.date(*[int(x) for x in iso.split("-")])
    except Exception:
        return iso
    return dt.strftime("%A, %B %-d") + (dt.strftime(", %Y") if with_year else "")


# ── PM ───────────────────────────────────────────────────────────────────────

# Admin catch-alls Hadassa removed from the report on 2026-07-28: "we'll leave
# things specifically inside the projects when needed." Anything worth
# reporting hangs off a named project, or off "Office & admin" for her own
# operational work. Items still live on the board — they just aren't reported.
EXCLUDED_PROJECTS = {"AR", "Closed", "Compliance", "Cost coding", "Scheduling",
                     "No project"}

# Rendered first, then everything else alphabetically. Her own admin work reads
# better after the client projects.
PROJECT_ORDER_LAST = ("Office & admin",)


def _project_sort_key(name):
    return (1 if name in PROJECT_ORDER_LAST else 0, name.lower())


_SUBJ_LEAD = re.compile(r"^(?:[\U0001F300-\U0001FAFF☀-➿️‍]+|"
                        r"RESOLVED|ANSWERED|DONE|CLOSED|✅|🔴|⚠️|[\s—\-:·])+",
                        re.IGNORECASE)


def _report_subject(it):
    """A card's subject, fit to read in a report.

    Board subjects are written for her at a glance and carry status furniture —
    leading ✅ / 🔴 / "RESOLVED —" / "ANSWERED —" — which reads as noise to FD
    and Rafael and, worse, states a status in a section that is supposed to be
    about work done. Stripped here rather than in state: the board still wants
    those markers.
    """
    s = (it.get("subject") or "").strip()
    s = _SUBJ_LEAD.sub("", s).strip()
    # NO sentence-splitting. The first attempt cut at the first " — ", which on
    # these subjects amputates the substance and leaves a useless stub: "99
    # Concord — RE-TIMED to 10:00 AM per Hadassa (4 subs)" became "99 Concord",
    # and "Jaar Realty — you emailed Richard about the 2 uncashed checks" became
    # "Jaar Realty". A line that names only the project tells the reader less
    # than nothing. Cap the length on a word boundary and keep the meaning.
    if len(s) > 150:
        s = s[:147].rsplit(" ", 1)[0] + "…"
    return s or (it.get("subject") or "").strip()


def _acted_today(it, day):
    """Did SHE act on this card on `day`?

    THE defect of 2026-07-30, and it shipped in a report: the filter was
    `status == "done"` with no date test at all. The day-roll had not run, so
    every card closed on 07-28 and 07-29 was still sitting on the board marked
    done — and 15 of them printed in the 07-30 report, including one whose own
    text read "Hadassa re-dated it to TOMORROW (7/30)". A daily report that
    contains other days is not a daily report.

    Her scope, verbatim: "i only want TODAY'S things. emails, conversations,
    tasks changed (not only done) ONLY FROM TODAY." So the test is ACTION, not
    closure — a card she logged an update against counts even if it is still
    open, and a card closed yesterday does not count today.

    `first_seen` is deliberately NOT a signal. 36 cards were created today by
    the sweep; a card merely ARRIVING is inbound information, not work she did,
    and counting it would inflate her day with other people's emails.
    """
    if (it.get("done_at") or "")[:10] == day:
        return True
    if (it.get("dismissed_at") or "")[:10] == day:
        return True
    for u in (it.get("updates") or []):
        if (u.get("at") or "")[:10] == day:
            return True
    return False


def pm(st, day):
    """HER OWN RECORD OF WHAT SHE DID, for one day. Not a project status report.

    Scope, in her words on 2026-07-30: "this is MY DAILY DOING REPORTS and I
    only asked you to separate like that cause I wanted to see per gc projects,
    financial, systems" — so the project grouping is an INDEX over her own work,
    not a per-project status brief. And: "this is about ME, MY work for the
    entire day, NOT Rafael, NOT anyone else."

    Three things that follow, each of which was wrong before:

    1. ONLY TODAY. The filter used to be `status == "done"` with no date test,
       and because the day-roll had not run, 15 cards closed on 07-28/07-29 were
       still marked done and printed in the 07-30 report — one of them reading
       "Hadassa re-dated it to TOMORROW (7/30)". See _acted_today().
    2. HER UPDATE STREAM IS THE REPORT. Only `did` was rendered — a single
       headline per card — while the append-only `updates[]` she had been writing
       all afternoon was ignored. Those entries ARE the day: "I contacted him and
       got the company's basic info - already in subs sheet", "I sent him an
       email now asking for the 11:30am slot next Wednesday". Every one of
       today's entries is now a line, with its time.
    3. NOTHING ABOUT THE TOOL. "closed — no note written" was meta-commentary
       about her own board, and she is the only reader: "it's MY system and FD /
       Rafael have absolutely nothing to do with it." Removed.
    """
    items = st["items"]
    reportable = [
        i for i in items
        if (i.get("project") or "No project") not in EXCLUDED_PROJECTS
        and _acted_today(i, day)
        # Work Claude finished is not her work. `done_by` is set to "claude"
        # only by the delegation path, so this cannot mis-drop her own cards.
        and (i.get("done_by") or "hadassa") == "hadassa"
    ]

    by = defaultdict(list)
    for i in reportable:
        by[i.get("project") or "No project"].append(i)

    n_done = len([i for i in reportable if i["status"] == "done"])
    n_drop = len([i for i in reportable if i["status"] == "dismissed"])

    # ── ONE LINE PER THING SHE WROTE, CHRONOLOGICAL, IN HER WORDS ──
    # The card subject is deliberately absent. See the note in _acted_today and
    # her 2026-07-30 verdict: the subjects are Claude-authored narrative from the
    # day each card appeared, so printing them filled a "today" report with
    # yesterday — including "Hand check #3834 TOMORROW as planned" and "Rafael
    # assigned you: …", the latter reporting someone else's instruction in a
    # report that is only about her own work.
    #
    # A project group therefore reads as her diary for that project: the times
    # she wrote, in order. A card she ticked without writing anything contributes
    # NOTHING here, because there is no record of what she did — and inventing
    # one from a stale subject is exactly the failure.
    rows = defaultdict(list)          # project -> [(hhmm, html)]
    for i in reportable:
        proj = i.get("project") or "No project"
        if i["status"] == "dismissed":
            why = (i.get("did") or i.get("dismiss_reason") or "").strip()
            if why:
                rows[proj].append(((i.get("dismissed_at") or "")[11:16],
                                   '<span class="t">%s</span>'
                                   '<span class="drop">Decided against it — %s</span>'
                                   % (e((i.get("dismissed_at") or "")[11:16]), e(why))))
            continue
        seen = set()
        for u in (i.get("updates") or []):
            if (u.get("at") or "")[:10] != day:
                continue
            txt = (u.get("text") or "").strip()
            if not txt or txt in seen:
                continue
            seen.add(txt)
            hhmm = (u.get("at") or "")[11:16]
            rows[proj].append((hhmm, '<span class="t">%s</span>%s'
                                     % (e(hhmm), e(txt))))
        did = (i.get("did") or "").strip()
        if did and did not in seen:
            hhmm = (i.get("done_at") or "")[11:16]
            rows[proj].append((hhmm, '<span class="t">%s</span>%s'
                                     % (e(hhmm), e(did))))

    rows = {k: v for k, v in rows.items() if v}
    if not rows:
        return ('<p class="gap">Nothing was written down today. Ticking a box '
                'records that something closed; the note you add is what says '
                'what you did.</p>')

    n_lines = sum(len(v) for v in rows.values())
    out = []
    out.append('<p class="lede">%d things you did, across %d areas.</p>'
               % (n_lines, len(rows)))
    for proj in sorted(rows, key=_project_sort_key):
        group = sorted(rows[proj], key=lambda r: r[0] or "99:99")
        out.append('<details class="proj" open><summary><b>%s</b>'
                   '<span class="pill ok">%d</span></summary><ul class="acts">'
                   % (e(proj), len(group)))
        for _, htm in group:
            out.append('<li>%s</li>' % htm)
        out.append("</ul></details>")
    return "\n".join(out)


# ── SYSTEMS ──────────────────────────────────────────────────────────────────

def systems(st, day):
    """What changed in the tooling — written for people who don't code.

    This used to print raw git subjects. Hadassa killed that on 2026-07-28:
    "you need to understand that people seeing this report are laymen." A
    commit hash tells a reader nothing; what they need is what changed and why
    it matters to the business. So the section is now curated prose stored in
    pm_state under `systems`, as {what, why} pairs. Git is still the record of
    truth for developers — it just isn't what gets shown here.
    """
    rows = st.get("systems") or []
    if not rows:
        return '<p class="gap">No changes to the tools today.</p>'
    out = []
    for r in rows:
        what = (r.get("what") or "").strip()
        why = (r.get("why") or "").strip()
        if not what:
            continue
        # ── 2026-07-30, hers: "Do not put the same things as the previous day in
        # the 'system tools' if nothing was added new to those." ──
        # These rows carried NO date, so the same five entries reprinted every
        # single day — which makes the section read as filler and trains the
        # reader to skip it. A row must now declare the day it belongs to;
        # an undated row is a row from an earlier report and is not reprinted.
        if (r.get("day") or "") != day:
            continue
        if why:
            out.append('<details class="proj"><summary><b>%s</b></summary>'
                       '<div class="syswhy">%s</div></details>' % (e(what), e(why)))
        else:
            out.append('<details class="proj"><summary><b>%s</b></summary></details>'
                       % e(what))
    return "\n".join(out) or ('<p class="gap">No tool or system changes shipped '
            'today.</p>')


# ── page ─────────────────────────────────────────────────────────────────────

CSS = """
:root{--brand:#7b68ee;--brand-deep:#5a48d0;--brand-soft:#efecfe;--paper:#f5f6fb;
--card:#fff;--rule:#e9ebf2;--ink:#1c2130;--body:#495061;--muted:#6b7385;--faint:#98a0b3;
--ok:#2fa36b;--ok-bg:#e6f5ee;--bad:#dc4638;--bad-bg:#fbe7e5;--warn:#c07d10;--warn-bg:#fbf1dc;}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,system-ui,sans-serif;
-webkit-font-smoothing:antialiased;padding:0 0 60px}
.wrap{max-width:940px;margin:0 auto;padding:0 22px}
header{padding:30px 0 14px}h1{margin:0;font-size:25px;letter-spacing:-.02em}
.sub{color:var(--muted);font-size:13.5px;margin-top:3px}
h2{margin:34px 0 4px;font-size:12px;text-transform:uppercase;letter-spacing:.09em;color:var(--brand-deep)}
h2+.rule{height:2px;background:var(--brand);opacity:.22;margin-bottom:14px}
h3{margin:22px 0 8px;font-size:14.5px}
h3 .muted{font-weight:400;font-size:12px;color:var(--faint)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0}
.tile{background:var(--card);border:1px solid var(--rule);border-radius:11px;padding:12px 14px}
.tile .k{font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:700}
.tile .v{font-size:22px;font-weight:750;letter-spacing:-.02em;margin-top:2px;font-variant-numeric:tabular-nums}
.tile .sub{font-size:11.5px;color:var(--faint);margin-top:1px}
/* A ticked card with no note is still reported — the marker says the DETAIL is
   missing, never that the work is. Muted so it reads as a footnote, but at a
   contrast that passes: #6b7385 on #fff is 5.3:1. */
/* Her own steps, in her words, with the time she wrote each one. */
.steps{margin:5px 0 0;padding-left:0;list-style:none}
.steps li{font-size:13.5px;color:var(--body);margin:3px 0;line-height:1.5}
.steps .t{display:inline-block;min-width:44px;color:var(--muted);
  font-variant-numeric:tabular-nums;font-size:11.5px;font-weight:700}
.tile.bad .v{color:var(--bad)}.tile.ok .v{color:var(--ok)}.tile.warn .v{color:var(--warn)}
.callout{border-radius:9px;padding:10px 13px;font-size:13.5px;margin:10px 0}
.callout.bad{background:var(--bad-bg);color:#8f2b20}
.tblwrap{overflow-x:auto;background:var(--card);border:1px solid var(--rule);border-radius:11px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--rule)}
th{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
td.pos{color:var(--bad);font-weight:650}td.neg{color:var(--ok)}
.proj{background:var(--card);border:1px solid var(--rule);border-radius:11px;padding:11px 14px;margin-bottom:9px}
.ph{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
.pill{font-size:10.5px;padding:2px 8px;border-radius:20px;background:#f1f2f8;color:var(--muted);font-weight:650}
.pill.ok{background:var(--ok-bg);color:var(--ok)}.pill.mut{background:#f6f7fb;color:var(--faint)}
.lede{font-size:13.5px;color:var(--muted);margin:10px 0 12px}
details.proj{background:var(--card);border:1px solid var(--rule);border-radius:11px;
padding:0;margin-bottom:9px}
details.proj>summary{display:flex;gap:8px;align-items:center;cursor:pointer;
padding:11px 14px;list-style:none;font-size:14.5px}
details.proj>summary::-webkit-details-marker{display:none}
details.proj>summary::after{content:"−";margin-left:auto;color:var(--faint);
font-size:16px;line-height:1}
details.proj:not([open])>summary::after{content:"+"}
ul.acts{margin:0;padding:0 14px 12px 32px}
ul.acts li{font-size:13.5px;color:var(--body);padding:3px 0;line-height:1.5}
ul.acts li.drop{color:var(--faint)}
.outlook p{font-size:14px;color:var(--body);margin:6px 0}
.syswhy{font-size:13px;color:var(--body);margin-top:3px}
/* .syswhy was authored to sit inside .sysrow (which supplied padding:11px 14px),
   but render_systems() emits it as a DIRECT CHILD of details.proj{padding:0}.
   .sysrow/.syswhat are emitted nowhere, so nothing supplied the gutter and every
   body paragraph rendered 1px from the card border — 14px LEFT of its own summary
   heading. Invisible to node --check and to check_html_js.py; only visible in a
   browser. Verified in Chromium 2026-07-29: computed padding was 0px on all 18
   instances, textLeftInset 1px vs 15px for the summary. Fixed here at the source
   so tomorrow's report does not reproduce it. */
details.proj>.syswhy{padding:0 14px 12px;margin-top:0}
@media (max-width:520px){.wrap{padding:0 14px}h1{font-size:21px}
.tile .v{font-size:19px}ul.acts{padding-left:26px}}
.note{font-size:12.5px;color:var(--body);background:#fffdf4;border-left:3px solid var(--warn);
padding:5px 9px;border-radius:5px;margin:3px 0 6px 14px}
.muted{color:var(--muted)}.gap{color:var(--bad);font-style:italic}
.quote{background:var(--card);border-left:3px solid var(--rule);padding:9px 13px;
font-size:13px;color:var(--body);border-radius:0 8px 8px 0}
.foot{font-size:12px;color:var(--faint);margin-top:6px}
ul{margin:6px 0;padding-left:20px}li{font-size:13.5px;color:var(--body)}
code{background:#f1f2f8;padding:1px 5px;border-radius:4px;font-size:12px}
footer{margin-top:40px;padding-top:14px;border-top:1px solid var(--rule);
font-size:11.5px;color:var(--faint)}
@media print{body{background:#fff}.tile,.proj,.tblwrap{break-inside:avoid}}
"""


def build(day, st, d, err, stale_reason=None):
    banner = ""
    if stale_reason:
        banner = ('<div class="callout bad"><b>This report may be out of date.</b> '
                  'It was generated from a board that was not reconciled against '
                  'Gmail and Slack first — %s. Anything below described as done or '
                  'outstanding should be checked before it is relied on.</div>'
                  % e(stale_reason))
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RFS Daily Report — %(day)s</title><style>%(css)s</style></head><body><div class="wrap">
<header><h1>RFS daily report</h1>
<div class="sub">%(pretty)s · what got done, and where the cash stands</div></header>
%(banner)s

<h2>Today's work</h2><div class="rule"></div>
%(pm)s

<h2>Cash outlook</h2><div class="rule"></div>
%(fin)s

<h2>Systems &amp; tools</h2><div class="rule"></div>
%(sys)s

<footer>Cash figures are read straight out of the finance system, not typed in or estimated.
Work log cross-checked against email and Slack before publishing.</footer>
</div></body></html>""" % {
        "day": e(day), "pretty": e(_pretty_date(day, with_year=True)),
        "css": CSS, "banner": banner,
        "fin": financial(d, err), "pm": pm(st, day), "sys": systems(st, day),
    }


def _last_published(day):
    """(filename, size) of the most recent published daily BEFORE `day`.

    Read from the repo that actually serves them, so the comparison is against
    what Hadassa signed off and not against a local staging file that any run
    may have already overwritten.
    """
    try:
        names = sorted(n for n in os.listdir(PUBLISHED_DIR)
                       if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.html", n)
                       and n[:10] < day)
    except Exception:
        return None
    if not names:
        return None
    last = names[-1]
    try:
        return last, os.path.getsize(os.path.join(PUBLISHED_DIR, last))
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--allow-stale", action="store_true",
                    help="generate even though the board was not swept today; "
                         "stamps a visible warning banner into the page")
    # `brief_date` answers "has the board been rolled?", not "which day does this
    # report cover?" — two different questions that were coupled here. On
    # 2026-07-30 the roll was deliberately withheld (it archives her done items
    # and needed her word), so the day's report generated stamped 2026-07-29.
    # Reporting on today must not require mutating her board first.
    ap.add_argument("--day", help="the day this report covers (YYYY-MM-DD); "
                                  "defaults to TODAY (never the board's "
                                  "brief_date — see the note at the day= line)")
    a = ap.parse_args()

    st, status = S.load_state()
    if st is None:
        print("pm_state unreadable (%s)" % status, file=sys.stderr)
        return 2

    # ── FRESHNESS GATE ───────────────────────────────────────────────────
    # The board is a cache of the world. On 2026-07-28 this report told
    # Hadassa that Rafael was blocking two 98 Wyman subs four hours after he
    # had answered them in Slack, because it was generated from a board that
    # had not been swept since the morning. The sweep is now a precondition of
    # generating, enforced here rather than left to intention.
    fresh, reason = S.swept_today(st)
    stale_reason = None
    if not fresh:
        if not a.allow_stale:
            print("REFUSING to generate: %s.\n"
                  "Sweep Gmail + Slack, reconcile the board, then stamp it:\n"
                  "    python3 -c \"import pm_state as S; S.mark_swept()\"\n"
                  "Or pass --allow-stale to generate anyway with a warning banner."
                  % reason, file=sys.stderr)
            return 3
        stale_reason = reason
        print("WARNING: %s — generating with a stale banner." % reason, file=sys.stderr)

    d, err = load_dash()
    # 2026-08-24: `brief_date` REMOVED from this fallback chain. The comment on
    # --day above already says brief_date answers "has the board been rolled?",
    # not "which day does this report cover?" — but it was still the default
    # here, so the two questions stayed coupled where it actually mattered.
    # The board's brief_date has read 2026-07-29 since the day-roll last ran
    # (once, ever), so any run without an explicit --day produced a report
    # DATED AND SCOPED TO JULY 29 — and these reports go to FD and Rafael.
    # A stale watermark must never outrank today's date; if the caller does not
    # say which day it wants, the answer is today.
    day = a.day or S._today()
    out = a.out or (DEFAULT_OUT % day)
    os.makedirs(os.path.dirname(out), exist_ok=True)

    html_text = build(day, st, d, err, stale_reason)

    # ── NEVER overwrite a report without keeping the old bytes ──
    # The PreToolUse autobackup hook covers Claude's Write tool; it does NOT
    # cover a python script opening a file for writing. On 2026-07-30 a run with
    # no --day silently overwrote the 07-29 staging copy for exactly this reason.
    if os.path.exists(out):
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = "%s.bak_%s" % (out, stamp)
        try:
            with open(out, "rb") as src, open(bak, "wb") as dst:
                dst.write(src.read())
            print("kept previous version: %s" % bak, file=sys.stderr)
        except Exception as ex:
            print("REFUSING to overwrite %s — could not back it up (%s)"
                  % (out, ex), file=sys.stderr)
            return 4

    # ── REGRESSION GATE against the last approved report ──
    # Not a style opinion: a daily report that is a fraction of the size of the
    # one approved yesterday has almost certainly dropped whole sections.
    prev = _last_published(day)
    if prev:
        pname, psize = prev
        ratio = len(html_text.encode("utf-8")) / float(psize or 1)
        if ratio < 0.6:
            print("\n*** REGRESSION WARNING ***\n"
                  "This report is %d bytes; the last approved one (%s) is %d — "
                  "%.0f%% of it.\nOpen that file and diff the SECTIONS before "
                  "showing this to anyone. A report this much smaller has "
                  "normally lost\nwhole parts (amounts, attribution, the "
                  "waiting-on section, the cash scenarios).\n"
                  % (len(html_text.encode("utf-8")), pname, psize, ratio * 100),
                  file=sys.stderr)

    with open(out, "w", encoding="utf-8") as f:
        f.write(html_text)
    print(out)
    if a.open:
        subprocess.run(["open", "-a", "Google Chrome", out])
    return 0


if __name__ == "__main__":
    sys.exit(main())
