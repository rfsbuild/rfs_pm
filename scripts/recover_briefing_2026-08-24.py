#!/usr/bin/env python3
"""Lane-correct and re-ingest the 2026-08-24 13:57 briefing that was rejected whole.

WHY THIS EXISTS
    The 14m45s sweep produced 30 items and 19 drafts. `pm_ingest.validate()`
    checks `lane` against `pm_state.LANES` all-or-nothing, and the sweep prompt
    asked for a `lane` without ever saying what one was — so the model invented
    six topic names (money/client/ops/process/compliance/risk) and all thirty
    were discarded. The prompt is fixed (19735ee); this recovers the run that
    was already paid for instead of paying for it twice.

THE ROUTING RULE
    A lane is WHO ACTS NEXT, not what the item is about. That is the sentence
    the old prompt never said. Every mapping below is a judgement about the
    next actor, and the reasoning is recorded per item so it can be argued
    with rather than re-derived.

TWO CONTRACTS THIS SCRIPT RESPECTS RATHER THAN WORKS AROUND
    1. For a card that ALREADY EXISTS, `pm_ingest` ignores `lane` unless the
       item carries `_relane: true`. That guard exists so a collector cannot
       silently undo a lane SHE chose. So every move of an existing card is
       opt-in here, listed, and justified — and `ap_southshore_1567` is
       deliberately LEFT in her `urgent` lane rather than demoted to `action`,
       because demoting her own judgement is precisely what the guard forbids.
    2. `rt_petersen_coi` MUST move out of the `routine` lane. Cards there are
       wiped every morning by `pm_routine.py`, and tomorrow's plan file does
       not restate it — the certificate expires 8/29.

Idempotent: re-running changes nothing once the lanes are correct.
"""
import argparse
import datetime
import json
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import pm_ingest as I   # noqa: E402
import pm_state as S    # noqa: E402

REJECTED = HERE / "briefings" / "rejected" / "2026-08-24_1357_lane-rejected.json"
CORRECTED = HERE / "briefings" / "2026-08-24_1357_lane-corrected.json"

# id -> (lane, why this is the next actor)
LANE_MAP = {
    # ── urgent: she must act TODAY — money at risk or a deadline inside 24h ──
    "fin_llinkk_latiles_7486": (
        "urgent", "An authorised payment is pending on two vendor names and two "
        "amounts 22c apart. Money leaves on her action, and the wrong pick is a "
        "wire to the wrong company."),
    "fin_guernsey_offhour_250": (
        "urgent", "The inspection is tomorrow 4:30 PM. Inside 24h."),

    # ── action: hers, but not today-or-else ──
    "rba_vangile_chargeback": (
        "action", "Sept 8 meeting. The file-building is hers; Rafael is needed "
        "for the concede/contest call but not for the next step."),
    "sw0824-bob-approved-and-asks-about-bundling": (
        "action", "Bob asked HER directly whether a progress payment lands this "
        "week. A client is waiting on her answer."),
    "meeting_elisabeth_tbc": (
        "action", "70 West Cedar is LOST — hers to mark in the project sheet. "
        "The live card still reads 'RESOLVED — site visit Wed Aug 5', which is "
        "actively misleading; this ingest corrects the text as well as the lane."),
    "sw0824-26lee-francisca-answers-relay": (
        "action", "Rafael already answered all six questions. The only missing "
        "step is hers: put them in an email. Gillian has emptied her kitchen."),
    "sw0824-guilherme-qbtime-clockin": (
        "action", "Rafael assigned this TO HER on 8/17. Confirming the invite "
        "landed and per-job entries appear is her check, not a wait on anyone."),
    "rt_petersen_coi": (
        "action", "Certificate expires 8/29 — 5 days. 🔴 THE LANE MOVE IS THE "
        "POINT: it currently sits in `routine`, which pm_routine.py wipes every "
        "morning, and plan-2026-08-25.json does not restate it."),
    "rba_conlan_orino_paperwork": (
        "action", "Submitting the JCC + LSWP is hers, and it blocks a payment "
        "INTO RFS. Draft is ready."),
    "sw0824-ocean299-final-walkthrough-sept3": (
        "action", "Confirming Sept 3 to Charles in writing is hers; Rafael has "
        "already approved the date."),
    "sw0824-co-invoice-on-approval-rule": (
        "action", "Checking the flag on the outstanding 70 Robin COs is hers, "
        "and Bob is actively approving them right now."),
    "sw0730-homedepot-card-owner": (
        "action", "MOVED OFF `rafael`: he has already answered — he confirmed on "
        "8/19 that 8418 is Alpha's debit card. Nothing is waiting on him, so "
        "leaving it in his lane hides a live task of hers."),

    # ── week: hers this week, no fixed deadline ──
    "sw0824-guernsey-chris-material-approval": (
        "week", "Wiring a written-approval step into the buying process. A "
        "committed obligation, but no date attached."),
    "sw0731-granola-fireflies-trials": (
        "week", "Pick one notetaker and cancel the redundant trials. Already in "
        "`week` and staying — no _relane needed."),

    # ── rafael: waiting on RAFAEL ──
    "sw0824-bob-five-open-questions": (
        "rafael", "All five are his: rafter hangers, the mini-split re-quote, "
        "the PM fee concession, the CAT6E sign-off."),
    "fin_saturday_1_5x": (
        "rafael", "Her own confirmation this session: 'waiting for him'. Nothing "
        "may change in tomorrow's payroll run until he answers."),
    "fin_eastern_environmental_4481": (
        "rafael", "$1,850 is 13 days past due and Alice's 'bill or estimate?' "
        "has sat unanswered since 8/20. One word from him unblocks it."),
    "fin_mgp_bertwell": (
        "rafael", "Only he or Guilherme can say whether 76 Bertwell is an RFS "
        "job. Held unpaid until then."),
    "sw0824-guernsey-beadboard-co": (
        "rafael", "In scope or change order is his call, and it gates the work."),
    "sw0824-cedar51-hvac-880-reissue": (
        "rafael", "Re-issuing the CO at cost + PM + markup is his decision."),
    "rba_gui_truck_load_4_30am": (
        "rafael", "He said 'Vou ver aqui oq eu consigo fazer' and never came "
        "back. Only he can name who loads the truck."),
    "sw0824-99concord-subs-unavailable": (
        "rafael", "Four of the five stalled items need him, and he is back today."),
    "sw0824-andersen-nov27-availability": (
        "rafael", "Whether crews work Nov 27 is his answer; she only relays it."),

    # ── guilherme: waiting on GUILHERME ──
    "sw0824-guernsey-siding-return-1010": (
        "guilherme", "The entire $1,010.10 return is blocked on one photo of "
        "the siding in the trailer, owed by him since 8/20."),
    "sw0824-cedar51-window-trim-mismatch": (
        "guilherme", "Alice cannot answer Paula until he gives the technical "
        "assessment on matching the trim."),

    # ── claude: finishable without her ──
    "ops_truck_parking_leominster": (
        "claude", "The next productive step is a lookup — commercial lots and "
        "yards in Leominster that take work trucks, with monthly pricing. She "
        "picks from a shortlist; she should not have to build one. Rafael marked "
        "it 'urgente' and the police were called, so the shortlist is the fast "
        "path, not the slow one."),
    "sw0731b-registry-drift-six-channels": (
        "claude", "MOVED OFF `week`: registering channels and splitting DMs from "
        "channels in the gate is code work, not hers. It is also the surviving "
        "half of an F1 duplicate pair — see DEDUPE below."),

    # ── her own lane, left alone (see KEEP_HER_LANE) ──
    "ap_southshore_1567": (
        "urgent", "The sweep's own reading is that chasing Mauricio is `action`. "
        "She had already put it in `urgent`, and $4,516.50 billed at a 50% "
        "deposit against our 30% standard justifies that. Her lane stands; no "
        "lane is sent at all."),

    # ── noise: she should SEE it, nothing owed by anyone ──
    "r_llo_haverford_return": (
        "noise", "43 Haverford electrical is FIXED — Guilherme tested it with "
        "the bidet running. Nothing is owed. Moved off `action`."),
    "sw0824-hadassa-sends-now-via-billing": (
        "noise", "A coverage fact worth knowing (outbound proof lives in the "
        "billing@ copies, not label:SENT). Its own hadassa_todo says 'Nothing "
        "to do.'"),
}

# Existing cards whose lane must actually MOVE. Anything not listed here keeps
# the lane it already has, even if LANE_MAP names a different one.
RELANE = {
    "r_llo_haverford_return",            # action  -> noise   (resolved)
    "meeting_elisabeth_tbc",             # noise   -> action  (lost, needs recording)
    "rt_petersen_coi",                   # routine -> action  (escapes the daily wipe)
    "sw0730-homedepot-card-owner",       # rafael  -> action  (he answered)
    "sw0731b-registry-drift-six-channels",  # week -> claude  (code work)
}

# Deliberately NOT re-laned, recorded so the omission reads as a decision:
#   ap_southshore_1567 — she put it in `urgent`. The sweep would call it
#   `action`. Demoting a lane SHE chose is exactly what the `_relane` guard
#   exists to prevent, and $4,516.50 of mis-billed deposit justifies her call.
KEEP_HER_LANE = {"ap_southshore_1567"}

# `validate()` flags two cards as chases shipping no draft. That rule is right
# to fire — a chase without a message is the drafting job left on her desk — and
# the escape hatch has to be a STATED reason, not a silent flag. These are the
# reasons, and neither is "to make the validator green":
NO_DRAFT = {
    "meeting_elisabeth_tbc":
        "There is nobody to write to. The job is LOST, Alice already replied "
        "gracefully to Elisabeth and already told Pedro, so no outreach is "
        "hanging. What remains is a sheet update, which is not a message.",
    "sw0824-guilherme-qbtime-clockin":
        "The missing fact has to come from her first. The next step is a LOOKUP "
        "in QuickBooks — did Guilherme accept the invite, and have per-job "
        "entries appeared since 8/18 — and only a 'no' produces a message. "
        "Drafting a chase now would be chasing him over a fact nobody has "
        "checked, and Rafael framed this as her holding him to it in person.",
}

# The F1 duplicate pair. Ingesting `sw0731b-...` corrects the count from six to
# eleven; its twin then states a number we know is wrong. Closing the twin is a
# separate, visible step — this script only reports it.
DUPLICATE_TWIN = "sw0730-registry-drift-6-channels"


def build():
    items = json.loads(REJECTED.read_text())["items"]
    state, _ = S.load_state(S.STATE_PATH)
    on_board = {it["id"] for it in state["items"]}

    unmapped = [it["id"] for it in items if it["id"] not in LANE_MAP]
    if unmapped:
        sys.exit("🔴 %d item(s) have no lane mapping: %s" % (len(unmapped), unmapped))

    out = []
    for it in items:
        it = dict(it)
        lane, _why = LANE_MAP[it["id"]]
        if it["id"] in KEEP_HER_LANE:
            it.pop("lane", None)          # send no lane at all — hers stands
        else:
            it["lane"] = lane
            if it["id"] in on_board and it["id"] in RELANE:
                it["_relane"] = True
        if it["id"] in NO_DRAFT:
            it["_no_draft"] = NO_DRAFT[it["id"]]
        out.append(it)
    return out, on_board


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the board (default is validate-only)")
    a = ap.parse_args(argv)

    items, on_board = build()

    print("=== ROUTING (a lane is WHO ACTS NEXT) ===")
    by_lane = {}
    for it in items:
        by_lane.setdefault(it.get("lane", "(hers, unchanged)"), []).append(it["id"])
    for lane in ("urgent", "action", "week", "rafael", "guilherme", "claude",
                 "noise", "(hers, unchanged)"):
        ids = by_lane.get(lane, [])
        if ids:
            print("  %-18s %2d  %s" % (lane, len(ids), ", ".join(ids)))
    print("  %-18s %2d" % ("TOTAL", len(items)))
    print("\n  new cards      : %d" % len([i for i in items if i["id"] not in on_board]))
    print("  existing cards : %d  (of which re-laned: %d)"
          % (len([i for i in items if i["id"] in on_board]),
             len([i for i in items if i.get("_relane")])))

    errs = I.validate(items, existing={i["id"]: i for i in
                                      S.load_state(S.STATE_PATH)[0]["items"]})
    if errs:
        print("\n🔴 VALIDATION FAILED — %d error(s), nothing written:" % len(errs))
        for e in errs:
            print("   -", e)
        return 1
    print("\n✅ validate() clean — all %d items accepted" % len(items))

    CORRECTED.parent.mkdir(parents=True, exist_ok=True)
    CORRECTED.write_text(json.dumps({"items": items}, indent=1, ensure_ascii=False))
    print("   corrected briefing → %s" % CORRECTED)

    if not a.apply:
        print("\n(dry run — pass --apply to write the board)")
        return 0

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = S.STATE_PATH.with_suffix(".json.bak_%s" % stamp)
    shutil.copy2(S.STATE_PATH, bak)
    print("\n   backup → %s" % bak.name)

    pre_state, _ = S.load_state(S.STATE_PATH)
    before = len(pre_state["items"])
    # Capture her lanes BEFORE the write so "unchanged" is proved against the
    # real prior value, not against a hardcoded guess at it.
    her_lanes = {i["id"]: i["lane"] for i in pre_state["items"]
                 if i["id"] in KEEP_HER_LANE}
    res = I.ingest(items, path=S.STATE_PATH)
    after_state, _ = S.load_state(S.STATE_PATH)
    after = len(after_state["items"])
    print("   ingest: %s" % json.dumps(res, default=str)[:400])
    print("   board %d → %d cards" % (before, after))

    # ── post-conditions, asserted rather than eyeballed ──
    by_id = {i["id"]: i for i in after_state["items"]}
    fails = []
    for it in items:
        card = by_id.get(it["id"])
        if not card:
            fails.append("%s is not on the board after ingest" % it["id"])
            continue
        want = LANE_MAP[it["id"]][0]
        if it["id"] in KEEP_HER_LANE:
            if card["lane"] != her_lanes.get(it["id"]):
                fails.append("%s: her lane %r was overwritten (now %r)"
                             % (it["id"], her_lanes.get(it["id"]), card["lane"]))
        elif card["lane"] != want:
            fails.append("%s: lane is %r, expected %r"
                         % (it["id"], card["lane"], want))
    petersen = by_id.get("rt_petersen_coi")
    if petersen and petersen["lane"] == "routine":
        fails.append("rt_petersen_coi is STILL in the routine lane — it will be "
                     "wiped at 08:00 tomorrow")
    if fails:
        print("\n🔴 POST-CONDITIONS FAILED:")
        for f in fails:
            print("   -", f)
        return 1
    print("   ✅ post-conditions: %d lanes verified, Petersen is out of `routine`"
          % len(items))

    twin = by_id.get(DUPLICATE_TWIN)
    if twin and twin.get("status") == "open":
        print("\n⚠️  STILL OPEN — the F1 duplicate twin %r (lane %s) now states "
              "'six channels' when the corrected card says eleven. Closing it is "
              "a separate step." % (DUPLICATE_TWIN, twin.get("lane")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
