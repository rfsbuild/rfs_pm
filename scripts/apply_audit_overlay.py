#!/usr/bin/env python3
"""Write the 2026-09-14/15 board-audit verdicts ONTO the board's own cards.

HER RULING (D8, 2026-09-15): "why did you come up with the new pm board if you
know it's supposed to be just like the http://127.0.0.1:8789/ one???" — a
re-assertion of D12 (2026-09-14). The audit's output is not a page; it is card
state on the board she already opens. A static page can DESCRIBE a verdict but
cannot CLOSE a card.

WHAT IT WRITES. One `audit` object per card. Nothing else is touched — no
status, no lane, no assignee. She decides every close; this only puts the
evidence where the decision gets made.

    audit = {
      verdict     NOISE | LIKELY DONE | KEEP | CANNOT TELL   (None if unaudited)
      conf        the auditor's confidence, 0-100, or None
      why         one line of evidence, in plain English
      owner       who the audit says owns it (Rafael / Guilherme / Claude)
      urgent_why  why it is today/this-week, if it is
      contra      {her_words, evidence, do} where her own note is contradicted
      lost_tick   {at, text} where she ticked it and the tick did not survive
      band        the single triage band this card belongs to
      run, at     provenance
    }

WHY IT SURVIVES THE NIGHTLY SWEEP. pm_state.upsert_item() refreshes only
SOURCE_OWNED fields on an existing card, and `audit` is not one of them.
Verified on 2026-09-15 before this was written, not assumed.

SAFE TO RE-RUN. Idempotent: it rewrites the overlay wholesale each time and
removes it from cards that no longer carry a verdict, so a corrected verdicts.json
fully replaces the last one rather than layering on top.

    python3 scripts/apply_audit_overlay.py --dry-run
    python3 scripts/apply_audit_overlay.py
"""
import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import pm_state as S                                            # noqa: E402

VERDICTS = os.path.join(os.path.expanduser("~"), "rfs_dashboard", "reports",
                        "pm_audit_2026-09-15", "verdicts.json")
AUDITED = os.path.join(os.path.dirname(VERDICTS), "audited_ids.json")

# The label the board shows. "DONE" as a verdict reads as "this IS done", which
# is a claim the audit does not make — it says the evidence points that way and
# she rules. The bucket key stays DONE; the words she reads are hers to trust.
LABEL = {"NOISE": "NOISE", "DONE": "LIKELY DONE", "CANNOT TELL": "CANNOT TELL"}

# One card, one band. Priority matters: a card whose note is contradicted must
# not be filed under "likely done" just because it also has that verdict — the
# contradiction is the thing she has to see.
BAND_ORDER = ["CONTRADICTED", "LOST TICK", "NOISE", "LIKELY DONE", "CANNOT TELL"]


def lost_ticks(items):
    """Cards she ticked whose completion did not survive. See scripts/check_pm_state.py."""
    out = {}
    for it in items:
        du = [u for u in (it.get("updates") or [])
              if isinstance(u, dict) and u.get("kind") == "done"]
        if not du or it.get("status") != "open":
            continue
        if it.get("lane") == "routine" and not it.get("did"):
            continue          # reseeded daily by design, not a lost tick
        out[it["id"]] = {"at": du[-1].get("at"), "text": du[-1].get("text")}
    return out


def build(state, data, audited):
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    V, CONTRA = data["verdicts"], data["contra"]
    URGENT, OWNER = data["urgent"], data["owner"]
    lost = lost_ticks(state["items"])
    overlay, bands, missing = {}, {}, []

    for iid in set(V) | set(CONTRA) | set(URGENT) | set(OWNER):
        if not S.get_item(state, iid):
            missing.append(iid)

    for it in state["items"]:
        iid = it["id"]
        if it.get("status") != "open":
            continue
        v = V.get(iid)
        verdict = LABEL[v["verdict"]] if v else ("KEEP" if iid in audited else None)
        band = None
        if iid in CONTRA:
            band = "CONTRADICTED"
        elif iid in lost:
            band = "LOST TICK"
        elif v:
            band = LABEL[v["verdict"]]
        if not (v or iid in CONTRA or iid in URGENT or iid in OWNER
                or iid in lost or iid in audited):
            continue
        # QUOTE HER EXACTLY. The audit stored a paraphrase of her note
        # ("no problems, you should've known by now"); the card holds what she
        # actually typed ("no problems with that. you should've known by now.").
        # Reading a misquote of herself back off her own board is its own small
        # betrayal of the record, so the card always wins over the summary.
        contra = CONTRA.get(iid)
        if contra:
            # `did` first (her headline), then the last thing she typed on the
            # card whatever its kind — two of the five wrote theirs as a plain
            # update rather than at tick time, and they deserve the same accuracy.
            ups = [u for u in (it.get("updates") or []) if isinstance(u, dict) and u.get("text")]
            exact = it.get("did") or (ups[-1]["text"] if ups else None)
            contra = dict(contra)
            contra["her_words"] = exact or contra["her_words"]
            contra["quoted_from"] = "card" if exact else "audit summary"
        overlay[iid] = {
            "verdict": verdict,
            "conf": (v or {}).get("conf"),
            "why": (v or {}).get("why"),
            "owner": OWNER.get(iid),
            "urgent_why": URGENT.get(iid),
            "contra": contra,
            "lost_tick": lost.get(iid),
            "band": band,
            "run": data["_run"],
            "at": now,
        }
        if band:
            bands[band] = bands.get(band, 0) + 1
    return overlay, bands, sorted(missing)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--state", default=None)
    a = ap.parse_args()
    path = a.state or S.STATE_PATH

    data = json.load(open(VERDICTS))
    audited = set(json.load(open(AUDITED)))
    with open(path) as fh:
        overlay, bands, missing = build(json.load(fh), data, audited)

    print("audited ids (authority): %d" % len(audited))
    print("cards receiving an overlay: %d" % len(overlay))
    for b in BAND_ORDER:
        print("   %-14s %3d" % (b, bands.get(b, 0)))
    print("   %-14s %3d" % ("needs her call", sum(bands.values())))
    if missing:
        print("\n⚠️  %d id(s) in the verdict data have NO card on the board — "
              "they are skipped, not invented:" % len(missing))
        for m in missing:
            print("      " + m)
    if a.dry_run:
        print("\n(dry run — nothing written)")
        return 0

    def _fn(state):
        n_set = n_clear = 0
        for it in state["items"]:
            if it["id"] in overlay:
                it["audit"] = overlay[it["id"]]
                n_set += 1
            elif "audit" in it:
                del it["audit"]          # a verdict withdrawn must leave the card
                n_clear += 1
        return {"set": n_set, "cleared": n_clear}

    res = S._mutate(_fn, path)[1]
    print("\n✅ overlay written: %d set, %d cleared" % (res["set"], res["cleared"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
