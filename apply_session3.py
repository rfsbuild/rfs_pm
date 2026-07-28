#!/usr/bin/env python3
"""Session-3 board reconcile — 2026-07-28.

Applies, in ONE locked write:
  1. Hadassa's five corrections to today's report cards
  2. Every action the Gmail + Slack sweep proved she took today
  3. The items she listed as missing (morning routine, payroll, Amazon,
     Ederson, Truck 2 card)
  4. The plain-English `systems` list the report now renders
  5. A `last_swept_at` stamp — set here because the sweep genuinely ran

Every `did` below traces to a source: a Slack message with a timestamp, a
BuilderTrend notification, or Hadassa's own words this session. Nothing is
inferred about what she did.
"""
import json
import sys

import pm_state as S

# ── what she did, per card ────────────────────────────────────────────────
# key -> did text.  Sources noted inline.
DID = {
    # ── 70 Robin ──
    # Slack #70-robin 15:50 -> Rafael "Specs BT" 16:12 -> her "Ja coloquei e no
    # drive tb. All set." 16:12:40.  Gmail: Basecamp "Maegan D. uploaded 6 new
    # files" 15:46 EDT.
    "p_robin_plans": (
        "Uploaded Maegan's six revised plan sheets to BuilderTrend Specs and to "
        "Drive as soon as Helios posted them in Basecamp, and confirmed the "
        "filing location with Rafael."),
    # Slack #70-robin 13:44 (Bob's reply pasted) -> Rafael "ASAP" 14:08.
    "slack_robin_basement": (
        "Emailed Bob to ask the client to clear the basement for demolition. He "
        "replied that he'll review it with Rafael's team tomorrow and asked for a "
        "start timeline — Rafael's answer is ASAP, which still needs to go back "
        "to Bob."),
    # Gmail: Basecamp new-sign-in + verification code, 15:12 EDT.
    "p_robin_basecamp": (
        "Logged into Basecamp directly to read the Maegan and Bob thread instead "
        "of working from the digest — that closed the blind spot, and the missing "
        "measurements Helios needed went over today."),
    # Slack #70-robin 17:18.
    "p_mgp_co": (
        "Asked Rafael directly whether he'd seen the MGP attic-demo change order "
        "that was sitting on Alice's list waiting for his approval."),

    # ── 98 Wyman ──
    # Slack #98-wyman: her 10:05 + 10:06 asks, 12:09 chase, Rafael 12:14 x2.
    "wyman_quotes": (
        "Chased Rafael ahead of tomorrow's quote deadline and got both blocked "
        "subs unblocked: the first-floor addition makes the rubber roof bigger "
        "(Masterpiece), and Gerson's mold-remediation scope doesn't change "
        "(Mister G). Both answers still need to go back to the subs."),

    # ── 104 Child ──
    # Slack DM 12:44 (her email to Tess), #104-child 15:05-16:51.
    "ar_104_invoices": (
        "Recorded Tess's $2,148.03 payment against the job and confirmed the "
        "$4,042.25 final invoice is still a draft, so it stays unbilled. Then "
        "emailed Tess and resent change order #0015 explaining it covers work "
        "already finished — she replied that she considers invoice #22 to cover "
        "the bedroom door and expects that done first, so the crew hold stays on."),
    "p_104_co_payment": (
        "Confirmed the change-order payment landed — it was one of the two "
        "outstanding, not both, so it doesn't release the door and punch-list "
        "hold."),

    # ── 43 Haverford ──
    # Slack #43-haverford: Rafael 12:12 -> her "eletricista, certo?" 13:38 ->
    # "done!" 13:41.
    "ar_haverford_final": (
        "Let the client know the electrician will confirm when he can get back "
        "out to the flickering lights, and that I'll come back to her with a date "
        "as soon as we have one. The invoice follow-up waits on that, not on her."),

    # ── 26 Lee ──
    # Slack #26-lee: Rafael 09:44 direction; card already marked done by her.
    "slack_lee_invoice002": (
        "Wrote the description for invoice 002 — the 30% deposit — and sent it "
        "to the client."),

    # ── 51 Cedar ──
    # Slack DM to Rafael 13:26 with the full description; he reacted 👍.
    "p_cedar_paula": (
        "Wrote the description for invoice 0006 covering the carpentry, siding, "
        "flooring and window work, and sent it to Rafael for the client."),

    # ── 299 Ocean ──
    "p_299_inspection": (
        "Confirmed with Rafael that the inspection passed, and asked for the "
        "written result for the file."),
    "ar_299_drafts": (
        "Sent invoice 0007 and checked the rest of the 299 Ocean invoices — 0008 "
        "is still with Rafael, and he revised two more this afternoon."),

    # ── RBA / Andersen ──
    # Her Session-1 correction: "there's no JCC and I already explained in the
    # email i sent yesterday".
    "p_lotz_jcc": (
        "Closed this out — there is no JCC to submit, which I'd already explained "
        "in the email I sent yesterday."),
}

# Cards whose action today belongs to another card — marked done, but with no
# `did`, so the report doesn't say the same thing twice.
NO_DID_BUT_DONE = ["slack_wyman_input", "slack_tess_cos", "slack_haverford_light"]

# ── corrections that change status, not just wording ──────────────────────
MARK_DONE = ["p_robin_plans", "ar_104_invoices", "ar_haverford_final"]

# 26 Lee is not a signature chase — Gillian Haney accepted on 2026-07-08 and
# BuilderTrend shows it Approved (established by the Session-2 verify pass).
DISMISS = {
    "p_lee_signature": ("Not a signature chase — Gillian Haney accepted the "
                        "proposal on 7/8 and BuilderTrend shows it approved."),
}

# ── project reassignments ─────────────────────────────────────────────────
# Ray is 3 Arborview, a real project — it was filed under the "AR" catch-all,
# which is one of the buckets she removed from the report.
REPROJECT = {"slack_ray_followup": "3 Arborview"}

# ── new cards for work the board never knew about ─────────────────────────
NEW = [
    # Slack #all-rfsbuilders 14:19 (Ray's reply) -> Rafael 14:21 -> her 14:37
    # -> her 17:15 thread reply.
    dict(id="s3_arborview_ray", project="3 Arborview", lane="week", kind="action",
         subject="Ray — outstanding amount since March",
         did=("Followed up with Ray, got his reply, and passed it to Rafael and "
              "Gui. Gui is going out to look as soon as he can, and I'll email "
              "Ray back once he's been.")),
    # Slack #14-guernsey: Gui 13:36 -> her 14:38 -> Rafael 15:33 -> her "done" 15:36.
    dict(id="s3_guernsey_plumbing", project="14 Guernsey", lane="week", kind="action",
         subject="Plumbing material — shower valve and drain",
         did=("Messaged the client, Chris Heaton, to check whether the plumbing "
              "material — shower valve and drain — has arrived.")),
    # Slack #all-rfsbuilders 15:32 -> Rafael 15:34 + 15:35.
    dict(id="s3_gerson_wc", project="98 Wyman", lane="week", kind="action",
         subject="Gerson (Mister G) — workers' comp affidavit question",
         did=("Gerson asked whether a Massachusetts workers' comp affidavit would "
              "do instead of an active policy, since he's a sole proprietor with "
              "no employees. Raised it with Rafael — we require active workers' "
              "comp, and we'll ask subs for paperwork once contracts are signed, "
              "not before.")),

    # ── the five she named as missing ──
    dict(id="s3_morning_routine", project="Office & admin", lane="week", kind="action",
         subject="Morning routine",
         did=("Ran the morning routine — reconciled both bank feeds in the "
              "dashboard, worked through the flagged transactions and cleared the "
              "overnight inbox.")),
    # payroll_history.json, week_ending 2026-07-25: total_net 19347.99,
    # 13 employees, checks #3836-#3847 + the pre-issued #3835.
    dict(id="s3_payroll", project="Office & admin", lane="week", kind="action",
         subject="Weekly payroll — week ending 7/25",
         did=("Ran the weekly payroll for the week ending 7/25 — 13 people, "
              "$19,347.99 net, checks #3836 to #3847, plus Alice's advance "
              "#3835 that had already been written.")),
    dict(id="s3_amazon_supplies", project="Office & admin", lane="week", kind="action",
         subject="Office supplies",
         did="Placed the Amazon order for office supplies."),
    # Rate verified from the wk-7/25 run: 57.3h -> $1,432.50 gross = $25.00/hr.
    dict(id="s3_ederson_pay", project="Office & admin", lane="week", kind="action",
         subject="Ederson — pay setup and rate",
         did=("Messaged Ederson about paying him through his company's name "
              "instead of personally, and about raising his hourly rate by $3, "
              "from $25 to $28.")),
    dict(id="s3_truck2_card", project="Office & admin", lane="week", kind="action",
         subject="Truck 2 card",
         did="Added the new Truck 2 card to QuickBooks and to Drive."),

    # ── surfaced by the sweep, needs her decision: no `did`, so it stays off
    #    the report and lives on the board only ──
    dict(id="s3_104_inv0025", project="104 Child", lane="urgent", kind="decision",
         subject="104 Child — invoice 0025 $2,427.40 created and paid by Tess today",
         action=("Decide whether to bill it. BuilderTrend shows it Sent with a "
                 "zero balance and funds due 7/30, so under the BT-is-authority "
                 "rule it qualifies — but it is not in the books yet."),
         ctx_sum=("Tess created AND paid invoice 0025 'Door Hardware & Locksets "
                  "Purchase', $2,427.40, at 11:27 this morning. It is in neither "
                  "the billed total nor pending inflows. Note she is paying "
                  "itemised purchases while disputing the allowance change "
                  "order.")),
]

# ── Systems, in plain English ─────────────────────────────────────────────
# Hadassa, 2026-07-28: "you need to understand that people seeing this report
# are laymen." No hashes, no filenames, no jargon.
SYSTEMS = [
    {"what": "The daily to-do board is now a proper app, not an email attachment.",
     "why": "Before, the day's list lived in a web page and notes only survived if "
            "you remembered to press Export. It now runs as its own app and saves "
            "every change the moment it's made, so nothing gets lost."},
    {"what": "You can now rule something out and record why.",
     "why": "Not everything raised should be done. Items you decide against come "
            "off the count instead of sitting there looking overdue, and the "
            "reason is kept so nobody raises it again next week."},
    {"what": "This report now builds itself from the day's actual work.",
     "why": "The money figures are read straight out of the finance system and the "
            "work log comes off the board, so the report can't drift away from "
            "what really happened."},
    {"what": "The report now refuses to run on out-of-date information.",
     "why": "It checks that the board has been cross-checked against email and "
            "Slack first. Earlier today it would have said Rafael was holding up "
            "two 98 Wyman subcontractors four hours after he had already "
            "answered — that kind of mistake is now blocked instead of caught "
            "afterwards."},
    {"what": "Fixed how insurance certificates are checked.",
     "why": "An automatic BuilderTrend notice was misread as a roofer having "
            "renewed his insurance when it had actually lapsed. Certificates are "
            "now checked at the source rather than trusted from a notification — "
            "that one was about to go out in writing to the subcontractor."},
]


def apply(state):
    changed = {"did": [], "done": [], "dismissed": [], "reprojected": [],
               "added": [], "skipped": []}
    index = {i["id"]: i for i in state["items"]}

    for iid, text in DID.items():
        it = index.get(iid)
        if it is None:
            changed["skipped"].append(iid)
            continue
        it["did"] = text
        changed["did"].append(iid)

    for iid in MARK_DONE:
        it = index.get(iid)
        if it is None:
            changed["skipped"].append(iid)
            continue
        if it["status"] != "done":
            it["status"] = "done"
            it["done_at"] = S._now_iso()
            changed["done"].append(iid)

    for iid in NO_DID_BUT_DONE:
        it = index.get(iid)
        if it is not None and it["status"] == "open":
            it["status"] = "done"
            it["done_at"] = S._now_iso()
            changed["done"].append(iid)

    for iid, reason in DISMISS.items():
        it = index.get(iid)
        if it is None:
            changed["skipped"].append(iid)
            continue
        it["status"] = "dismissed"
        it["dismiss_reason"] = reason
        it["dismissed_at"] = S._now_iso()
        changed["dismissed"].append(iid)

    for iid, proj in REPROJECT.items():
        it = index.get(iid)
        if it is None:
            changed["skipped"].append(iid)
            continue
        it["project"] = proj
        changed["reprojected"].append(iid)

    for spec in NEW:
        if spec["id"] in index:
            it = index[spec["id"]]
            for k, v in spec.items():
                it[k] = v
            changed["added"].append(spec["id"] + " (refreshed)")
            continue
        it = S.new_item(spec["id"], spec["subject"])
        for k, v in spec.items():
            if k in S.ITEM_FIELDS:
                it[k] = v
        it["source"] = "session3"
        it["first_seen"] = S._now_iso()
        it["last_seen"] = S._now_iso()
        # Anything carrying a `did` is finished work; anything without one is a
        # live decision and must stay open.
        if spec.get("did"):
            it["status"] = "done"
            it["done_at"] = S._now_iso()
        state["items"].append(it)
        changed["added"].append(spec["id"])

    state["systems"] = SYSTEMS
    state["last_swept_at"] = S._now_iso()
    state["last_swept_sources"] = ["gmail", "slack"]
    return changed


if __name__ == "__main__":
    before, _ = S.load_state()
    n_before = len(before["items"])
    result = S._mutate(apply)[1]
    after, _ = S.load_state()

    # Assertions — nothing silently lost.
    assert len(after["items"]) == n_before + len(
        [s for s in NEW if s["id"] not in {i["id"] for i in before["items"]}]), \
        "item count moved unexpectedly"
    assert after.get("last_swept_at"), "sweep stamp missing"
    assert len(after.get("systems") or []) == len(SYSTEMS), "systems list wrong"
    for iid in DID:
        m = S.get_item(after, iid)
        assert m is None or m.get("did"), "did not written for %s" % iid

    print(json.dumps(result, indent=1))
    print("items %d -> %d" % (n_before, len(after["items"])))
    if result["skipped"]:
        print("SKIPPED (id not on board): %s" % ", ".join(result["skipped"]),
              file=sys.stderr)
