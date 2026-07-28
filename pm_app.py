#!/usr/bin/env python3
"""RFS PM Command Center — Hadassa's daily board.

Phase 1 of the plan approved 2026-07-28 (`~/.claude/plans/shimmying-foraging-planet.md`).

The one architectural rule: **there is no Export button.** Every interaction
writes straight to `pm_state.json` through `pm_state._mutate()`, which holds a
lock for the whole read-modify-write. The HTML pilot kept state in the browser
and lost a day when the manual save was skipped; that failure mode is designed
out rather than warned about.

Design follows the `rfs-dashboard-design` discipline: quiet, legible,
number-first. Palette tokens are the 2026-07-13 "RFS Financial Command" system.
"""
import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_state as S  # noqa: E402

st.set_page_config(page_title="RFS PM", page_icon="📋", layout="wide")

CSS = """
<style>
:root{
  --brand:#7b68ee; --brand-deep:#5a48d0; --brand-soft:#efecfe;
  --paper:#f5f6fb; --card:#fff; --rule:#e9ebf2; --rule-soft:#f1f2f8;
  --ink:#1c2130; --body:#495061; --muted:#6b7385; --faint:#98a0b3;
  --ok:#2fa36b; --ok-bg:#e6f5ee; --bad:#dc4638; --bad-bg:#fbe7e5;
  --warn:#c07d10; --warn-bg:#fbf1dc;
}
.block-container{padding-top:1.6rem;max-width:1180px;}
h1,h2,h3{color:var(--ink);letter-spacing:-.01em;}
.pm-card{background:var(--card);border:1px solid var(--rule);border-radius:10px;
  padding:.85rem 1rem;margin-bottom:.6rem;}
.pm-card.done{opacity:.5;}
.pm-card.deferred{border-left:3px solid var(--warn);}
.pm-card.urgent{border-left:3px solid var(--bad);}
.pm-subject{font-weight:650;color:var(--ink);font-size:.98rem;line-height:1.35;}
.pm-subject.struck{text-decoration:line-through;color:var(--muted);}
.pm-meta{color:var(--muted);font-size:.8rem;margin-top:.15rem;}
.pm-action{background:var(--brand-soft);border-radius:7px;padding:.5rem .7rem;
  margin-top:.5rem;font-size:.88rem;color:var(--ink);}
.pm-action-label{font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;
  color:var(--brand-deep);font-weight:700;margin-bottom:.15rem;}
.tag{display:inline-block;font-size:.68rem;padding:.1rem .45rem;border-radius:20px;
  margin-right:.28rem;border:1px solid var(--rule);color:var(--muted);background:var(--rule-soft);}
.tag.src{background:#eef1f8;}
.tag.new{background:var(--ok-bg);color:var(--ok);border-color:transparent;font-weight:650;}
.tag.age{background:var(--warn-bg);color:var(--warn);border-color:transparent;}
.tag.asg{background:var(--brand-soft);color:var(--brand-deep);border-color:transparent;font-weight:650;}
.tag.defer{background:var(--warn-bg);color:var(--warn);border-color:transparent;font-weight:650;}
.tag.unconf{background:var(--bad-bg);color:var(--bad);border-color:transparent;}
.pm-note{background:#fffdf5;border-left:3px solid var(--warn);padding:.4rem .6rem;
  border-radius:5px;font-size:.85rem;color:var(--body);margin-top:.45rem;}
.dt-row{display:flex;align-items:center;gap:.55rem;padding:.4rem .6rem;
  border-bottom:1px solid var(--rule-soft);font-size:.9rem;}
.dt-rank{background:var(--brand);color:#fff;width:20px;height:20px;border-radius:50%;
  display:inline-flex;align-items:center;justify-content:center;font-size:.72rem;font-weight:700;flex:0 0 auto;}
.lane-head{font-size:.78rem;text-transform:uppercase;letter-spacing:.07em;
  color:var(--muted);font-weight:700;margin:1.1rem 0 .4rem;}
.stButton>button{border-radius:7px;}
div[data-testid="stExpander"]{border:none;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ── load ──

state, status = S.load_state()
if status == "unreadable":
    st.error(
        "**pm_state.json is unreadable and no usable backup exists.** "
        "Nothing has been overwritten — the file needs looking at by hand before "
        "the board can load. Do not click anything."
    )
    st.stop()
if status == "recovered_backup":
    st.warning("Loaded from `pm_state.json.bak` — the main file was corrupt. Verify today's work is present.")
if status == "missing":
    st.info("No `pm_state.json` yet. Run `python3 pm_migrate.py --items <items.json>` to seed the board.")
    st.stop()


def rerun():
    st.rerun()


def _esc(s):
    """HTML-escape AND escape `$`.

    Streamlit still runs its markdown/KaTeX pass over strings passed with
    unsafe_allow_html=True, so an unescaped pair of dollar amounts in one line
    ("$4,210.67 ... $2,148.03") gets swallowed as inline LaTeX. Every money
    figure on this board would hit that.

    The escape must be the HTML entity `&#36;`, NOT a markdown `\\$`: inside a
    raw-HTML block markdown never unescapes the backslash, so `\\$` renders as
    a literal "\\$2,148.03" on the card. Caught in browser 2026-07-28.
    Plain-markdown sinks use _cap()/_strip_tags(), which DO use `\\$`.
    See [[feedback_html_vs_markdown_dollar_escape]].
    """
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("$", "&#36;"))


def _strip_tags(s):
    import re
    return re.sub(r"<[^>]+>", "", str(s or "")).replace("$", "\\$")


def _cap(s):
    """Plain text for st.caption / st.markdown — dollars escaped, no HTML escaping."""
    return str(s or "").replace("$", "\\$")


# ── header ──

c = S.counts(state)
left, right = st.columns([3, 2])
with left:
    st.markdown("### 📋 PM Command Center")
    st.caption(
        "%s · %d of %d done · saved %s"
        % (state.get("brief_date"), c["done"], c["actionable"],
           (state.get("updated_at") or "")[11:16] or "—")
    )
with right:
    st.progress(c["pct"] / 100.0, text="%d%% of today's actionable work" % c["pct"])

# Rolling the day forward is destructive-ish (done items drop), so it is an
# explicit two-click action, never automatic on load.
today = S._today()
if state.get("brief_date") != today:
    st.warning(
        "This board is still dated **%s** — today is **%s**. Rolling forward drops "
        "completed items, ages the rest by a day, and escalates anything deferred 3+ days."
        % (state.get("brief_date"), today)
    )
    if st.button("Roll the board forward to %s" % today, type="primary"):
        res = S.roll_forward(today)
        st.success("Carried %s · dropped %s done · escalated %s"
                   % (res.get("carried"), res.get("dropped"), len(res.get("escalated") or [])))
        rerun()

st.divider()


# ── do-today strip ──

focus = S.do_today(state, limit=5)
if focus:
    st.markdown("#### Today — the five that matter")
    for n, it in enumerate(focus, 1):
        st.markdown(
            '<div class="dt-row"><span class="dt-rank">%d</span>'
            '<span>%s</span><span class="tag">%s</span></div>'
            % (n, _esc(it["subject"]), _esc(it.get("project") or it.get("kind") or "")),
            unsafe_allow_html=True,
        )
    st.caption("Everything else is below, grouped by lane. Finished and deferred work sinks to the bottom of each lane.")
    st.divider()


# ── callbacks (each writes to disk immediately) ──

def _cb_done(item_id, key):
    S.set_done(item_id, st.session_state[key])


def _cb_note(item_id, key):
    S.set_note(item_id, st.session_state[key])


def _cb_assign(item_id, key):
    who = st.session_state[key]
    S.set_assignee(item_id, None if who == "hadassa (me)" else who)


def _cb_defer(item_id, key):
    label = st.session_state[key]
    if label == "— not deferred —":
        S.set_defer(item_id, None)
        return
    for code, text in S.DEFER_REASONS.items():
        if text == label:
            cur = S.get_item(S.load_state()[0], item_id) or {}
            if cur.get("defer") != code:      # only a CHANGE bumps defer_days
                S.set_defer(item_id, code)
            return


def _cb_project(item_id, key):
    S.set_project(item_id, st.session_state[key])


def render_card(it):
    lane = S.effective_lane(it)
    done = it.get("status") == "done"
    klass = "pm-card" + (" done" if done else "") + \
            (" deferred" if it.get("defer") and not done else "") + \
            (" urgent" if lane == "urgent" and not done else "")

    st.markdown('<div class="%s">' % klass, unsafe_allow_html=True)

    head, ctrl = st.columns([6, 1])
    with head:
        st.markdown(
            '<div class="pm-subject%s">%s</div><div class="pm-meta">%s</div>'
            % (" struck" if done else "", _esc(it["subject"]), _esc(it.get("meta") or "")),
            unsafe_allow_html=True,
        )
        tags = ['<span class="tag src">%s</span>' % _esc(it.get("source") or "")]
        if it.get("project"):
            tags.append('<span class="tag">%s</span>' % _esc(it["project"]))
        if it.get("is_new"):
            tags.append('<span class="tag new">NEW</span>')
        if int(it.get("age") or 0) >= 1:
            tags.append('<span class="tag age">↩ day %d</span>' % (int(it["age"]) + 1))
        if it.get("assignee"):
            tags.append('<span class="tag asg">→ %s</span>' % _esc(it["assignee"].title()))
        if it.get("defer"):
            d = S.DEFER_REASONS.get(it["defer"], it["defer"])
            extra = " · day %d" % it["defer_days"] if int(it.get("defer_days") or 0) > 1 else ""
            tags.append('<span class="tag defer">%s%s</span>' % (_esc(d), extra))
        if it.get("unconfirmed"):
            tags.append('<span class="tag unconf">unconfirmed</span>')
        st.markdown("".join(tags), unsafe_allow_html=True)
    with ctrl:
        k = "done_%s" % it["id"]
        st.checkbox("done", value=done, key=k,
                    on_change=_cb_done, args=(it["id"], k),
                    label_visibility="collapsed")

    if it.get("action"):
        st.markdown(
            '<div class="pm-action"><div class="pm-action-label">Your action</div>%s</div>'
            % _esc(it["action"]), unsafe_allow_html=True)

    if it.get("note"):
        st.markdown('<div class="pm-note">📝 %s</div>' % _esc(it["note"]), unsafe_allow_html=True)

    with st.expander("Context · note · assign · links"):
        if it.get("ctx_sum"):
            st.caption(_cap(it["ctx_sum"]))
        for p in (it.get("ctx_body") or []):
            st.markdown(_strip_tags(p))

        a, b, d = st.columns(3)
        with a:
            opts = ["hadassa (me)"] + [x for x in S.ASSIGNEES if x != "hadassa"]
            cur = it.get("assignee") or "hadassa (me)"
            k = "asg_%s" % it["id"]
            st.selectbox("Assign to", opts, index=opts.index(cur) if cur in opts else 0,
                         key=k, on_change=_cb_assign, args=(it["id"], k))
        with b:
            dopts = ["— not deferred —"] + list(S.DEFER_REASONS.values())
            curd = S.DEFER_REASONS.get(it.get("defer")) or "— not deferred —"
            k = "def_%s" % it["id"]
            st.selectbox("Defer", dopts, index=dopts.index(curd) if curd in dopts else 0,
                         key=k, on_change=_cb_defer, args=(it["id"], k))
        with d:
            k = "prj_%s" % it["id"]
            st.text_input("Project", value=it.get("project") or "", key=k,
                          on_change=_cb_project, args=(it["id"], k))

        k = "note_%s" % it["id"]
        st.text_area("Note — what you found out, what you told them",
                     value=it.get("note") or "", key=k, height=80,
                     on_change=_cb_note, args=(it["id"], k))

        links = it.get("links") or []
        if links:
            st.markdown(" · ".join(
                "[%s ↗](%s)" % (_esc(l.get("label", "link")), l.get("url", "#")) for l in links))
        where = it.get("where") or []
        if where:
            st.caption("📍 " + _cap(" · ".join(str(w) for w in where)))

        dr = it.get("draft")
        if dr:
            st.markdown("**✉️ Suggested email** — to %s" % _esc(dr.get("to", "")))
            st.caption("Subject: %s" % _cap(dr.get("subject", "")))
            st.code(dr.get("body", ""), language=None)

    st.markdown("</div>", unsafe_allow_html=True)


# ── lanes ──

for lane in S.LANES:
    items = S.ordered_items(state, lane)
    if not items:
        continue
    open_n = sum(1 for i in items if i.get("status") != "done")
    st.markdown('<div class="lane-head">%s — %d open of %d</div>'
                % (S.LANE_LABELS[lane], open_n, len(items)), unsafe_allow_html=True)
    for it in items:
        render_card(it)

with st.sidebar:
    st.markdown("### Board")
    st.metric("Open", c["actionable"] - c["done"])
    st.metric("Done today", c["done"])
    st.caption("State: `~/rfs_pm/pm_state.json`")
    st.caption("Every click saves immediately — there is no Export button.")
    st.divider()
    for ln in S.LANES:
        n = c["per_lane"].get(ln, 0)
        if n:
            st.caption("%s · %d" % (S.LANE_LABELS[ln], n))
