#!/usr/bin/env python3
"""Build a searchable "what did I do" log from the PM board — READ ONLY.

WHY THIS EXISTS (Hadassa, 2026-07-30):
    "i need to have the completed items somewhere cause if they ask me if I did
     something and I don't remember, I need to have that somewhere to confirm."

The data was already being kept — `history/<day>.json` holds every item archived
by a day-roll, and the live board holds the ones finished but not yet rolled.
What was missing was any way to LOOK at it: no UI tab, no `/api/history` route.
An archive nobody can read is not a record.

This reads both sources and writes one self-contained, searchable page.

    python3 done_log.py            # build + report the path
    python3 done_log.py --open     # build and open it in Chrome

Never writes to pm_state.json or history/.
"""

import argparse
import glob
import html
import json
import os
import subprocess
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "pm_state.json")
HIST = os.path.join(HERE, "history")
OUT = os.path.expanduser("~/Desktop/RFS_Done_Log.html")


def _rows():
    """Every completed item, newest day first. (day, source_label, item) tuples."""
    out = []

    # 1. the live board — finished but not yet rolled into history
    try:
        st = json.load(open(STATE))
    except Exception:
        st = {"items": []}
    live_day = st.get("brief_date") or "unrolled"
    for it in st.get("items", []):
        if it.get("status") in ("done", "dismissed"):
            out.append((live_day, "on the board now", it))

    # 2. every archived day
    for fp in sorted(glob.glob(os.path.join(HIST, "*.json")), reverse=True):
        day = os.path.splitext(os.path.basename(fp))[0]
        try:
            blob = json.load(open(fp))
        except Exception:
            continue
        items = blob.get("items") if isinstance(blob, dict) else blob
        for it in (items or []):
            if isinstance(it, dict):
                out.append((day, "archived", it))

    out.sort(key=lambda r: r[0], reverse=True)
    return out


def _card(day, src, it):
    e = lambda v: html.escape(str(v or ""))
    subject = e(it.get("subject") or it.get("id"))
    did = str(it.get("did") or "").strip()
    note = str(it.get("note") or "").strip()
    project = e(it.get("project"))
    status = it.get("status") or "done"
    by = it.get("done_by")
    result = str(it.get("claude_result") or "").strip()
    reason = str(it.get("dismiss_reason") or "").strip()
    when = e(str(it.get("done_at") or "")[:16].replace("T", " "))

    bits = []
    if did:
        bits.append('<div class="did"><b>What I did:</b> %s</div>' % e(did))
    else:
        bits.append('<div class="nodid">no note recorded — only that it was closed</div>')
    if note:
        bits.append('<div class="note"><b>My note:</b> %s</div>' % e(note))
    if reason:
        bits.append('<div class="note"><b>Marked not needed because:</b> %s</div>' % e(reason))
    if by == "claude" and result:
        bits.append('<div class="note"><b>Done by Claude, at your order:</b> %s</div>' % e(result))

    pills = ['<span class="p day">%s</span>' % e(day)]
    if project:
        pills.append('<span class="p">%s</span>' % project)
    if when:
        pills.append('<span class="p">closed %s</span>' % when)
    if status == "dismissed":
        pills.append('<span class="p warn">not needed</span>')
    if by == "claude":
        pills.append('<span class="p claude">Claude</span>')
    pills.append('<span class="p src">%s</span>' % e(src))

    hay = " ".join([subject, did, note, project, reason, result, day]).lower()
    return ('<article class="c" data-h="%s">\n  <h3>%s</h3>\n  %s\n  <div class="pills">%s</div>\n</article>'
            % (e(hay), subject, "\n  ".join(bits), "".join(pills)))


CSS = """
:root{--bg:#eef2f7;--card:#fff;--ink:#15212e;--mut:#4a5a6b;--faint:#5c6d7f;--line:#e4eaf1;
--brand:#3b5bdb;--green:#0d7047;--amber:#8f5606;--gut:clamp(16px,2vw,40px)}
@media(prefers-color-scheme:dark){:root{--bg:#0e1217;--card:#171d25;--ink:#e8eef4;--mut:#9aabbc;
--faint:#8b9bac;--line:#262f3a;--brand:#7aa2ff;--green:#5fd39d;--amber:#f0b45c}}
*{box-sizing:border-box}html,body{overflow-x:hidden}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:none;padding:0 var(--gut) 64px}
header{position:sticky;top:0;z-index:20;background:var(--bg);border-bottom:1px solid var(--line);
padding:18px var(--gut) 12px;margin:0 calc(var(--gut)*-1)}
h1{margin:0;font-size:14px;letter-spacing:.14em;text-transform:uppercase;color:var(--mut);font-weight:700}
.big{font-size:clamp(22px,2.2vw,30px);font-weight:800;letter-spacing:-.4px;margin:3px 0 0}
.big span{color:var(--brand)}
.stamp{font-size:12.5px;color:var(--mut);margin-top:4px;font-variant-numeric:tabular-nums}
#q{width:100%;max-width:640px;margin-top:12px;padding:10px 13px;font-size:15px;border:1px solid var(--line);
border-radius:10px;background:var(--card);color:var(--ink)}
#q:focus{outline:2px solid var(--brand);outline-offset:1px}
#count{font-size:12.5px;color:var(--mut);margin-top:7px;font-variant-numeric:tabular-nums}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,420px),1fr));gap:14px;margin-top:22px;align-items:start}
.c{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--green);
border-radius:12px;padding:13px 15px;box-shadow:0 1px 2px rgba(20,40,70,.06)}
.c h3{margin:0 0 7px;font-size:14.5px;font-weight:600;line-height:1.35}
.did{font-size:13px;margin-top:4px}.did b{color:var(--green)}
.note{font-size:13px;color:var(--mut);margin-top:4px}.note b{color:var(--ink)}
.nodid{font-size:12.5px;color:var(--amber);margin-top:4px;font-style:italic}
.pills{display:flex;flex-wrap:wrap;gap:5px;margin-top:9px}
.p{font-size:10.5px;border:1px solid var(--line);border-radius:6px;padding:1px 7px;color:var(--faint);white-space:nowrap}
.p.day{font-weight:700;color:var(--ink)}.p.warn{color:var(--amber);border-color:var(--amber)}
.p.claude{color:var(--brand);border-color:var(--brand);font-weight:600}.p.src{font-style:italic}
footer{margin-top:28px;border-top:1px solid var(--line);padding-top:14px;color:var(--faint);font-size:11.5px;max-width:1000px}
"""

JS = """
var q=document.getElementById('q'),cards=[].slice.call(document.querySelectorAll('.c')),cnt=document.getElementById('count');
function run(){var t=q.value.trim().toLowerCase(),n=0;
 for(var i=0;i<cards.length;i++){var ok=!t||cards[i].dataset.h.indexOf(t)>-1;cards[i].style.display=ok?'':'none';if(ok)n++;}
 cnt.textContent=t?(n+' of '+cards.length+' completed items match "'+q.value.trim()+'"'):(cards.length+' completed items');}
q.addEventListener('input',run);run();q.focus();
"""


def build():
    rows = _rows()
    now = datetime.now()
    cards = "\n".join(_card(d, s, i) for d, s, i in rows)
    nodid = sum(1 for _, _, i in rows if not str(i.get("did") or "").strip())
    days = sorted({d for d, _, _ in rows}, reverse=True)
    doc = (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        "<title>RFS — What I Completed</title>\n<style>%s</style>\n</head>\n<body>\n<div class=\"wrap\">\n"
        "<header>\n  <h1>&#9989; RFS &mdash; What I Completed</h1>\n"
        '  <div class="big">Proof of <span>work done</span></div>\n'
        '  <div class="stamp">Built %s &middot; %d completed items across %d day(s) &middot; live board + archive</div>\n'
        '  <input id="q" type="search" placeholder="Search everything &mdash; a name, a project, an amount, a date&hellip;" autocomplete="off">\n'
        '  <div id="count"></div>\n</header>\n<div class="grid">\n%s\n</div>\n'
        "<footer>Read-only. Built from <code>pm_state.json</code> (finished but not yet rolled) plus every "
        "<code>history/*.json</code> archive. Rebuild any time with "
        "<code>python3 ~/rfs_pm/done_log.py --open</code>.<br><br>"
        "<b>%d of %d items carry no &ldquo;what I did&rdquo; note</b> &mdash; they record only that the card was closed. "
        "Adding a note when you tick something off is what turns this from a checklist into an answer you can give "
        "when somebody asks.</footer>\n</div>\n<script>%s</script>\n</body>\n</html>\n"
        % (CSS, now.strftime("%A, %B %-d, %Y at %-I:%M %p"), len(rows), len(days), cards, nodid, len(rows), JS)
    )
    with open(OUT, "w") as fh:
        fh.write(doc)
    return OUT, len(rows), len(days), nodid


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true", help="open in Chrome when built")
    a = ap.parse_args()
    path, n, d, nd = build()
    print("wrote %s\n  %d completed items across %d day(s); %d have no 'did' note" % (path, n, d, nd))
    if a.open:
        subprocess.run(["open", "-a", "Google Chrome", path], check=False)
