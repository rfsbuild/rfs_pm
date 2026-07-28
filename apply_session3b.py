#!/usr/bin/env python3
"""Session-3 pass 2 — Hadassa's corrections + the full-day Gmail sweep.

Her rules applied here:
  · items are ONE SHORT BULLET each — FD and Rafael read this on a phone
  · when something sits "with Rafael", say what SHE did to put it there
  · nothing invented: anything unsourced is left blank and asked about

Sweep basis: every Slack channel + DM for 2026-07-28, and every Gmail thread
`after:2026/07/28` (38 threads). That full-day pass is what surfaced the four
Andersen extra-labor submissions and the Sky Tech activation — no targeted
query would have found them, because they were not on the board to query for.
"""
import json
import sys

import pm_state as S

DID = {
    # ── 104 Child ──
    "ar_104_invoices":
        "Recorded Tess's $2,148.03 payment. Confirmed final invoice 0020 is "
        "still a draft, so it stays unbilled.",
    "p_104_co_payment":
        "Emailed Tess and resent change order #0015 — she's holding payment "
        "until the bedroom door is finished, so the crew hold stays on.",

    # ── 14 Guernsey ── Slack #14-guernsey, her "done" 15:36.
    "s3_guernsey_plumbing":
        "Asked Chris Heaton to confirm the shower valve and drain arrived.",

    # ── 26 Lee ── BT: Rafael updated inv 0002 $17,789.20 at 13:39.
    "slack_lee_invoice002":
        "Wrote and sent the 30% deposit description for invoice 0002 "
        "($17,789.20).",

    # ── 299 Ocean ── her correction: she asked ME to double-check, and the
    # result is on the permit record (EP-26-104, attachment added 7/22).
    "p_299_inspection":
        "Confirmed the inspection passed.",
    "ar_299_drafts":
        "Sent invoice 0007. Rafael revised four more this afternoon.",

    # ── 3 Arborview ── her correction: we tell the client WHEN Gui can be
    # there, and she reaches back out once Gui gives her the date.
    "slack_ray_followup":
        "Followed up with Ray and passed it to Rafael and Gui. Once Gui gives "
        "me a date, I'll let Ray know when he'll be there.",

    # ── 43 Haverford ──
    "ar_haverford_final":
        "Told the client the electrician will confirm a date, and I'll follow "
        "up as soon as we have one.",

    # ── 51 Cedar ── her correction: it was SCHEDULED to send Friday.
    "p_cedar_paula":
        "Wrote the invoice 0006 description and scheduled it to send Friday.",

    # ── 70 Robin ── her correction: she already emailed the customer back.
    "slack_robin_basement":
        "Asked Bob to clear the basement for demo, and emailed him back the "
        "start timeline.",
    "p_robin_basecamp":
        "Logged into Basecamp to read the Maegan and Bob thread first-hand.",
    "p_robin_plans":
        "Uploaded Maegan's six revised plans to BuilderTrend Specs and Drive.",
    "p_mgp_co":
        "Asked Rafael to review the MGP attic-demo change order.",

    # ── 98 Wyman ── her correction: she already replied to both subs.
    "wyman_quotes":
        "Got Rafael's answers on the added scope and replied to Masterpiece "
        "and Mister G ahead of tomorrow's quote deadline.",
    "s3_gerson_wc":
        "Checked Gerson's workers' comp question with Rafael — we require an "
        "active policy, and paperwork is collected at contract, not at quote.",

    # ── RBA / Andersen ──
    "p_lotz_jcc":
        "Closed the Meg Lotz item — there's no JCC to submit.",

    # ── Office & admin ──
    "s3_morning_routine": "Ran the morning routine and the daily bank reconcile.",
    "s3_payroll":
        "Ran payroll for the week ending 7/25 — 13 people, $19,347.99.",
    "s3_amazon_supplies":
        "Ordered office supplies from Amazon; delivered the same afternoon.",
    "s3_ederson_pay":
        "Messaged Ederson about paying through his company and a $3/hr raise.",
    "s3_truck2_card": "Added the Truck 2 card to QuickBooks and Drive.",
}

# Shorter dismiss reasons — the long version was three sentences of internal
# caveats on a page meant to be read on a phone.
DISMISS_SHORT = {
    "p_lee_signature": "Already signed — Gillian accepted the proposal on 7/8.",
    "coi_one_service": "98 Wyman is still at quote stage, so no certificates "
                       "are needed yet.",
}

# `s3_arborview_ray` duplicated the existing `slack_ray_followup` card.
REMOVE = ["s3_arborview_ray"]

NEW = [
    # Gmail: 4 Formstack submissions to Andersen, 13:08-14:05 EDT. Pure catch
    # from the full-day sweep — this was on no list anywhere.
    dict(id="s3_rba_extras", project="RBA / Andersen", lane="week", kind="action",
         subject="Andersen extra labor and material claims",
         did=("Submitted four extra labor and material claims to Andersen — "
              "Kendra Neville, Diane & William Lewos, Timothy Oleary and "
              "Jenny Sears.")),
    # Gmail 18:16. Left WITHOUT a `did`: the acceptance is Sky Tech's action,
    # not hers, so it belongs on the board but not in her work log.
    dict(id="s3_skytech", project="Office & admin", lane="week", kind="fyi",
         subject="Sky Tech activated as a subcontractor in BuilderTrend",
         action="Confirm which job they're being brought on for, and that COI "
                "and W-9 are on file before they're scheduled."),
]


def apply(state):
    idx = {i["id"]: i for i in state["items"]}
    out = {"did": [], "dismiss": [], "removed": [], "added": [], "missing": []}

    for iid, text in DID.items():
        it = idx.get(iid)
        if it is None:
            out["missing"].append(iid)
            continue
        it["did"] = text
        out["did"].append(iid)

    for iid, reason in DISMISS_SHORT.items():
        it = idx.get(iid)
        if it is None:
            out["missing"].append(iid)
            continue
        it["dismiss_reason"] = reason
        out["dismiss"].append(iid)

    state["items"] = [i for i in state["items"] if i["id"] not in REMOVE]
    out["removed"] = [r for r in REMOVE if r in idx]

    for spec in NEW:
        if spec["id"] in idx:
            idx[spec["id"]].update(spec)
            out["added"].append(spec["id"] + " (refreshed)")
            continue
        it = S.new_item(spec["id"], spec["subject"])
        for k, v in spec.items():
            if k in S.ITEM_FIELDS:
                it[k] = v
        it["source"] = "session3-sweep"
        it["first_seen"] = it["last_seen"] = S._now_iso()
        if spec.get("did"):
            it["status"] = "done"
            it["done_at"] = S._now_iso()
        state["items"].append(it)
        out["added"].append(spec["id"])
    return out


if __name__ == "__main__":
    res = S._mutate(apply)[1]

    # The sweep stamp goes through mark_swept() WITH evidence — never by
    # direct assignment. Direct assignment is exactly how the gate got
    # bypassed earlier today.
    S.mark_swept({
        "gmail": {"checked": 38,
                  "detail": "every thread after:2026/07/28"},
        "slack": {"checked": 25,
                  "detail": "all 7 project channels + the Rafael DM, full day"},
    })

    st, _ = S.load_state()
    fresh, reason = S.swept_today(st)
    assert fresh, reason
    for iid in DID:
        it = S.get_item(st, iid)
        assert it is None or it["did"] == DID[iid], "did mismatch on %s" % iid
    assert all(S.get_item(st, r) is None for r in REMOVE), "removal failed"
    longest = max((len(v), k) for k, v in DID.items())
    assert longest[0] <= 160, "bullet too long for a phone: %s" % longest[1]

    print(json.dumps(res, indent=1))
    print("gate:", reason)
    print("longest bullet: %d chars (%s)" % longest)
    if res["missing"]:
        print("MISSING: %s" % ", ".join(res["missing"]), file=sys.stderr)
