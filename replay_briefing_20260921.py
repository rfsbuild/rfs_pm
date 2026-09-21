#!/usr/bin/env python3
"""Replay the 2026-09-21 08:00 sweep's briefing, which was EARNED and then discarded.

WHY THIS EXISTS. The 08:00 run reached both connectors and said so on its own
anchored marker lines (SLACK_OK=9 GMAIL_OK=17, cursors emitted). It produced 11
board items and 7 finance leads. The finance leads routed — they land BEFORE the
board ingest by design. The BOARD ingest was then refused wholesale because ONE
item, sw0918-registry-70westcedar-unreadable, asks her to "Ask Rafael to add the
sweep's Slack app to #12-eastside, #28-old and #70-west-cedar" and shipped no
`draft`. The gate is CORRECT ([[feedback_chase_card_ships_the_followup_email]]);
the missing draft is the defect. So the draft gets WRITTEN, not the gate weakened.

NOT a re-sweep. Re-running costs ~13 minutes, is non-deterministic, and would
likely reproduce the same omission. This replays the briefing the run already
earned, through the real pm_ingest.ingest() with its full validation.

EVIDENCE IS THE RUN'S OWN, NEVER INVENTED. counts and cursors are parsed out of
last_sweep_output.txt with pm_sweep_run's own anchored regexes — the same source
run_sweep() parses. This script fabricates nothing; it adds one draft.

Usage:  python3 replay_briefing_20260921.py            # dry run, writes nothing
        python3 replay_briefing_20260921.py --apply
"""
import json, pathlib, shutil, sys, datetime, tempfile

ROOT = pathlib.Path("/Users/Hadassa/rfs_pm")
sys.path.insert(0, str(ROOT))
import pm_ingest as I
import pm_state as S
import pm_sweep_run as R

APPLY = "--apply" in sys.argv
BRIEF = pathlib.Path(tempfile.gettempdir()) / "pm_sweep_briefing.json"
RAW   = ROOT / "last_sweep_output.txt"

# ── the draft that was missing ───────────────────────────────────────────────
# Portuguese, because it is addressed to Rafael. Short, because it is for Rafael
# ([[feedback_rafael_pages_are_short_and_never_duplicate]]). It asks for all
# three channels because the card's own claude_done established that all three
# are absent from the live channel listing — i.e. non-membership — even though
# two of them now surface a transient-looking internal_error.
DRAFT = {
    "to": "Rafael",
    "subject": "Slack — adicionar o app do sweep em 3 canais",
    "body": (
        "Rafael,\n\n"
        "O sweep automático não consegue ler 3 canais, então o que passa neles "
        "não entra no board:\n\n"
        "· #12-eastside (você abriu em 08/09)\n"
        "· #28-old\n"
        "· #70-west-cedar\n\n"
        "Os três não aparecem na lista de canais que o app enxerga — ou seja, "
        "ele não é membro. Dá pra adicionar o app nos três?\n\n"
        "No canal, é só digitar: /invite @Claude\n\n"
        "Obrigada,\nHadassa"
    ),
}

def main():
    data = json.loads(BRIEF.read_text())
    items = data["items"]

    # 1. Patch the one offending item — by id, never by index.
    tgt = [i for i in items if i.get("id") == "sw0918-registry-70westcedar-unreadable"]
    if len(tgt) != 1:
        sys.exit("expected exactly 1 target item, found %d" % len(tgt))
    if tgt[0].get("draft"):
        sys.exit("target already has a draft — nothing to repair, investigate first")
    tgt[0]["draft"] = DRAFT
    print("patched draft onto %s" % tgt[0]["id"])

    # 2. Parse the run's OWN evidence out of its raw output.
    raw = RAW.read_text()
    counts  = {s: int(m.group(1)) for s, rx in R.OK_RE.items()
               if (m := rx.search(raw))}
    cursors = {s: m.group(1) for s, rx in R.CURSOR_RE.items()
               if (m := rx.search(raw))}
    print("evidence parsed from the run itself: counts=%s cursors=%s" % (counts, cursors))
    for s in S.REQUIRED_SWEEP_SOURCES:
        if s not in counts:
            sys.exit("no marker for %r in last_sweep_output.txt — refusing to "
                     "stamp a sweep whose evidence I cannot read" % s)

    # 3. Validate through the real ingest. Dry run stops here.
    before = json.loads((ROOT / "pm_state.json").read_text())
    print("board BEFORE: %d items (%s)" % (
        len(before["items"]),
        {k: sum(1 for i in before["items"] if i.get("status") == k)
         for k in ("open", "done", "dismissed")}))

    if not APPLY:
        # Mirror what ingest() itself does: validate the patch against the
        # RESULTING card, i.e. merged over what is already on the board. Calling
        # validate(items) with no `existing` checks a different thing and would
        # give a green that means nothing.
        errs = I.validate(items, {i["id"]: i for i in before["items"]})
        if errs:
            print("\n🔴 WOULD FAIL — %d error(s):" % len(errs))
            for e in errs:
                print("   -", e)
        else:
            print("\n✅ validate() returned 0 errors against the live board.")
            print("DRY RUN — nothing written. Re-run with --apply.")
        return

    shutil.copy2(ROOT / "pm_state.json",
                 ROOT / ("pm_state.json.bak_%s_pre_replay"
                         % datetime.datetime.now().strftime("%Y%m%d_%H%M%S")))
    got = I.ingest(items)
    print("ingested: %d added · %d updated" % (len(got["added"]), len(got["updated"])))

    wms = (before.get("sweep_watermarks") or {})
    S.mark_swept({s: {"checked": counts[s],
                      "detail": "replay of the 08:00 run's own briefing; "
                                "polled since %s" % ((wms.get(s) or {}).get("cursor") or "start of day")}
                  for s in S.REQUIRED_SWEEP_SOURCES})
    for src, cur in cursors.items():
        S.advance_watermark(src, cur)
        print("watermark %s -> %s" % (src, cur))

    after = json.loads((ROOT / "pm_state.json").read_text())
    print("board AFTER : %d items (%s)" % (
        len(after["items"]),
        {k: sum(1 for i in after["items"] if i.get("status") == k)
         for k in ("open", "done", "dismissed")}))
    print("last_swept_at:", after.get("last_swept_at"))
    print("last_sweep_failure:", after.get("last_sweep_failure"))

main()
