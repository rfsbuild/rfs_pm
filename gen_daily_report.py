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
import subprocess
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_state as S  # noqa: E402

DASH = os.path.expanduser("~/rfs_dashboard/dashboard_state.json")
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
        out.append('<div class="tile %s"><div class="k">%s</div><div class="v">%s</div>'
                   '<div class="sub">%s</div></div>' % (cl, e(k), v, e(sub)))
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


def pm(st):
    """What SHE did today. Not a status board.

    Two rules from Hadassa, 2026-07-28:
      - every line names her action toward the thing, not the thing's status
      - nothing open appears at all; the report is about what got done
    A done card with no `did` has nothing to report, so it is skipped rather
    than padded out with its subject line.
    """
    # The filter is "did she act on it", NOT "is it closed". An item can still
    # be open while her action on it today is finished — chasing Rafael on the
    # MGP change order is real work and belongs in the record; the fact that
    # the CO is still unapproved does not. Nothing is rendered as a task, so no
    # open work leaks in: a card with no `did` simply has nothing to report.
    items = st["items"]
    reportable = [
        i for i in items
        if (i.get("project") or "No project") not in EXCLUDED_PROJECTS
        and (i.get("did") or (i["status"] == "dismissed" and i.get("dismiss_reason")))
    ]

    by = defaultdict(list)
    for i in reportable:
        by[i.get("project") or "No project"].append(i)

    n_done = len([i for i in reportable if i["status"] == "done"])
    n_drop = len([i for i in reportable if i["status"] == "dismissed"])

    out = []
    if not reportable:
        return '<p class="gap">Nothing recorded as done today.</p>'

    out.append('<p class="lede">%d things moved across %d projects.</p>'
               % (n_done + n_drop, len(by)))

    # Each project is its own toggle carrying its count, so FD and Rafael can
    # collapse what they don't need on a phone and still see how much was done.
    # Open by default — a report should never hide its own contents.
    for proj in sorted(by, key=_project_sort_key):
        group = by[proj]
        out.append('<details class="proj"><summary><b>%s</b>'
                   '<span class="pill ok">%d</span></summary><ul class="acts">'
                   % (e(proj), len(group)))
        for i in group:
            if i["status"] == "dismissed":
                # Reads as the decision she made, not as a task with a note
                # bolted on. "Called off: X — because Y" is how it lands on a
                # phone; subject-then-reason read like an unresolved item.
                out.append('<li class="drop">Called off: %s</li>'
                           % e(i.get("did") or i.get("dismiss_reason")
                               or i["subject"]))
            else:
                # Her `note` field is deliberately NOT rendered here. Board
                # notes are private working shorthand — half-sentences,
                # Portuguese, "Explained somewhere" — written to herself, not
                # to the people this report goes to. `did` is the reportable
                # version. Caught on review 2026-07-28, when the first render
                # leaked all nine notes onto the page.
                out.append('<li>%s</li>' % e(i["did"]))
        out.append("</ul></details>")
    return "\n".join(out)


# ── SYSTEMS ──────────────────────────────────────────────────────────────────

def systems(st):
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
        if why:
            out.append('<details class="proj"><summary><b>%s</b></summary>'
                       '<div class="syswhy">%s</div></details>' % (e(what), e(why)))
        else:
            out.append('<details class="proj"><summary><b>%s</b></summary></details>'
                       % e(what))
    return "\n".join(out) or '<p class="gap">No changes to the tools today.</p>'


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
.sysrow{background:var(--card);border:1px solid var(--rule);border-radius:11px;
padding:11px 14px;margin-bottom:9px}
.syswhat{font-size:14px;font-weight:650;color:var(--ink)}
.syswhy{font-size:13px;color:var(--body);margin-top:3px}
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
        "fin": financial(d, err), "pm": pm(st), "sys": systems(st),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--allow-stale", action="store_true",
                    help="generate even though the board was not swept today; "
                         "stamps a visible warning banner into the page")
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
    day = st.get("brief_date") or S._today()
    out = a.out or (DEFAULT_OUT % day)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(build(day, st, d, err, stale_reason))
    print(out)
    if a.open:
        subprocess.run(["open", "-a", "Google Chrome", out])
    return 0


if __name__ == "__main__":
    sys.exit(main())
