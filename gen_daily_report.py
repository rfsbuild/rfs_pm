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

    tiles = [
        ("Citizens available", money(bank), "", "reconciled balance, available basis"),
        ("Cap One card", money(card), "", "balance owed"),
        ("Forecast trough", money(trough), "bad" if (floor and trough is not None and trough < floor) else "ok",
         "lowest projected EOD%s" % (" · %s" % tro_date if tro_date else "")),
        ("Floor", money(floor), "", "%d day%s below it in the window"
         % (len(breach), "" if len(breach) == 1 else "s")),
    ]
    out = ['<div class="tiles">']
    for k, v, cl, sub in tiles:
        out.append('<div class="tile %s"><div class="k">%s</div><div class="v">%s</div>'
                   '<div class="sub">%s</div></div>' % (cl, e(k), v, e(sub)))
    out.append("</div>")

    if breach:
        out.append('<div class="callout bad"><b>Below the floor on %d day%s:</b> %s</div>'
                   % (len(breach), "" if len(breach) == 1 else "s",
                      e(", ".join("%s (%s)" % (b["date"], money(b["eod_planned"]).replace("$", "$"))
                                  for b in breach[:6]))))

    # AR — only projects with money still to collect
    ar = d.get("ar_projects") or []
    rows = []
    for p in ar:
        billed, recv = p.get("billed") or 0, p.get("received") or 0
        outstanding = round(billed - recv, 2)
        if abs(outstanding) < 0.01:
            continue
        rows.append((p.get("name", "?"), p.get("contract"), billed, recv, outstanding))
    rows.sort(key=lambda r: -abs(r[4]))
    if rows:
        out.append('<h3>Outstanding A/R <span class="muted">billed but not yet received</span></h3>')
        out.append('<div class="tblwrap"><table><thead><tr><th>Project</th><th class="n">Contract</th>'
                   '<th class="n">Billed</th><th class="n">Received</th><th class="n">Outstanding</th>'
                   "</tr></thead><tbody>")
        for name, contract, billed, recv, outs in rows:
            out.append("<tr><td>%s</td><td class='n'>%s</td><td class='n'>%s</td>"
                       "<td class='n'>%s</td><td class='n %s'>%s</td></tr>"
                       % (e(name), money(contract), money(billed), money(recv),
                          "neg" if outs < 0 else "pos", money(outs)))
        out.append("</tbody></table></div>")
        out.append('<p class="foot">A negative figure means more was received than billed — a '
                   "customer credit or an un-rolled-up invoice, not an error to ignore.</p>")
    notes = (fc.get("notes") or "").strip()
    if notes:
        out.append('<h3>Forecast note <span class="muted">as stored</span></h3>'
                   '<p class="quote">%s</p>' % e(notes[:600]))
    return "\n".join(out)


# ── PM ───────────────────────────────────────────────────────────────────────

def pm(st):
    items = st["items"]
    done = [i for i in items if i["status"] == "done"]
    dropped = [i for i in items if i["status"] == "dismissed"]
    openi = [i for i in items if i["status"] == "open"]
    waiting = [i for i in openi if i.get("assignee")]
    deferred = [i for i in openi if i.get("defer")]
    urgent = [i for i in openi if S.effective_lane(i) == "urgent"]

    out = ['<div class="tiles">']
    for k, v, cl, sub in [
        ("Closed today", len(done), "ok", "marked done"),
        ("Still open", len(openi), "", "carrying into tomorrow"),
        ("Urgent", len(urgent), "bad" if urgent else "", "needs her first"),
        ("Waiting on others", len(waiting), "warn", "assigned out"),
        ("Judged not needed", len(dropped), "", "each with a reason"),
    ]:
        out.append('<div class="tile %s"><div class="k">%s</div><div class="v">%d</div>'
                   '<div class="sub">%s</div></div>' % (cl, e(k), v, e(sub)))
    out.append("</div>")

    # per project
    by = defaultdict(list)
    for i in items:
        by[i.get("project") or "No project"].append(i)
    out.append("<h3>By project</h3>")
    for proj in sorted(by):
        group = by[proj]
        g_done = [i for i in group if i["status"] == "done"]
        g_open = [i for i in group if i["status"] == "open"]
        g_drop = [i for i in group if i["status"] == "dismissed"]
        if not group:
            continue
        out.append('<div class="proj"><div class="ph"><b>%s</b>'
                   '<span class="pill ok">%d done</span>'
                   '<span class="pill">%d open</span>%s</div>'
                   % (e(proj), len(g_done), len(g_open),
                      '<span class="pill mut">%d dropped</span>' % len(g_drop) if g_drop else ""))
        for i in g_done:
            out.append('<div class="li done">✓ %s</div>' % e(i["subject"]))
        for i in g_open:
            bits = []
            if i.get("assignee"):
                bits.append("waiting on %s" % i["assignee"])
            if i.get("defer"):
                bits.append(S.DEFER_REASONS.get(i["defer"], i["defer"]))
            if i.get("due"):
                bits.append("due %s" % i["due"])
            if (i.get("age") or 0) >= 1:
                bits.append("day %d" % (int(i["age"]) + 1))
            out.append('<div class="li">○ %s%s</div>'
                       % (e(i["subject"]),
                          ' <span class="muted">— %s</span>' % e(" · ".join(bits)) if bits else ""))
            if i.get("note"):
                out.append('<div class="note">📝 %s</div>' % e(i["note"]))
        for i in g_drop:
            out.append('<div class="li drop">🚫 %s <span class="muted">— %s</span></div>'
                       % (e(i["subject"]), e(i.get("dismiss_reason") or "no reason recorded")))
        out.append("</div>")

    if dropped:
        out.append('<h3>Judged not needed — and why</h3><div class="tblwrap"><table>'
                   "<thead><tr><th>Item</th><th>Reason</th></tr></thead><tbody>")
        for i in dropped:
            out.append("<tr><td>%s</td><td>%s</td></tr>"
                       % (e(i["subject"]), e(i.get("dismiss_reason") or "—")))
        out.append("</tbody></table></div>")
    return "\n".join(out)


# ── SYSTEMS ──────────────────────────────────────────────────────────────────

def systems():
    """What shipped today, from git — not from anyone's memory."""
    out = []
    for label, repo in (("Dashboard", "~/rfs_dashboard"), ("PM app", "~/rfs_pm")):
        path = os.path.expanduser(repo)
        if not os.path.isdir(os.path.join(path, ".git")):
            continue
        try:
            log = subprocess.run(
                ["git", "-C", path, "log", "--since=midnight", "--pretty=%h\t%s"],
                capture_output=True, text=True, timeout=15).stdout.strip()
        except Exception:
            continue
        rows = [l.split("\t", 1) for l in log.splitlines() if "\t" in l]
        rows = [r for r in rows if not r[1].startswith("autosave:")]
        if not rows:
            continue
        out.append("<h3>%s</h3><ul>" % e(label))
        for sha, subj in rows:
            out.append("<li><code>%s</code> %s</li>" % (e(sha), e(subj)))
        out.append("</ul>")
    if not out:
        out.append('<p class="gap">No commits today outside autosave.</p>')
    return "\n".join(out)


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
.li{font-size:13.5px;padding:2px 0;color:var(--body)}
.li.done{color:var(--muted);text-decoration:line-through}
.li.drop{color:var(--faint)}
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


def build(day, st, d, err):
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RFS Daily Report — %(day)s</title><style>%(css)s</style></head><body><div class="wrap">
<header><h1>RFS daily report</h1>
<div class="sub">%(day)s · Financial, Systems and PM in one place · generated %(now)s</div></header>

<h2>Financial</h2><div class="rule"></div>
%(fin)s

<h2>PM — what moved today</h2><div class="rule"></div>
%(pm)s

<h2>Systems — what shipped</h2><div class="rule"></div>
%(sys)s

<footer>Financial figures read directly from <code>dashboard_state.json</code>; PM detail from
<code>pm_state.json</code>; systems from the git logs. Nothing on this page is estimated —
where a value is missing it is labelled rather than filled in. Read-only: generating this
report writes to no state file.</footer>
</div></body></html>""" % {
        "day": e(day), "css": CSS, "now": e(S._now_iso()[11:16]),
        "fin": financial(d, err), "pm": pm(st), "sys": systems(),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--open", action="store_true")
    a = ap.parse_args()

    st, status = S.load_state()
    if st is None:
        print("pm_state unreadable (%s)" % status, file=sys.stderr)
        return 2
    d, err = load_dash()
    day = st.get("brief_date") or S._today()
    out = a.out or (DEFAULT_OUT % day)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(build(day, st, d, err))
    print(out)
    if a.open:
        subprocess.run(["open", "-a", "Google Chrome", out])
    return 0


if __name__ == "__main__":
    sys.exit(main())
