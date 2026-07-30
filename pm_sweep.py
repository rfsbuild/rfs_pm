#!/usr/bin/env python3
"""Sweep planner for the PM board — the source registry and the sweep contract.

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT.

It is NOT a fetcher. Gmail and Slack are reached through claude.ai connectors
that are authenticated per-session.

⚠️ SUPERSEDED IN PART, 2026-07-30. This file originally went on to say that a
subprocess or cron job "has no access to them at all", so a collector "cannot
exist on this machine today". That was the honest read on 2026-07-29 and it is
now wrong: a headless `claude -p` run loaded both connectors and returned real
data (SLACK_TOOL_LOADED=yes SLACK_CALL=ok RESULTS=11, GMAIL_TOOL_LOADED=yes
GMAIL_CALL=ok THREADS=5). The catch is that it ONLY works when the prompt
explicitly tells the model to ToolSearch-load them first — they are deferred
tools, so a prompt that merely says "search Slack" fails silently and returns a
confident empty result, which is precisely the "worse than no job" outcome this
paragraph warned about. The scheduled runner lives in pm_sweep_run.py; the
registry and the contract stay HERE, and it reads them from this module rather
than keeping a second copy.

What it IS: the part of a sweep that must not live in Claude's head.

    python3 pm_sweep.py --plan       # what to read, since when, what's known
    python3 pm_sweep.py --sources    # the registry alone
    python3 pm_sweep.py --check-new 'C0BL...,C0BK...'   # ids not in the registry

The failure this fixes happened on 2026-07-29. A full Slack sweep at 08:40 was
believed complete. It missed six task assignments, because the channel
`#99-concord` had been created at 09:13 that morning and nothing told Claude to
look for channels it did not already know about. Hadassa caught it by prompting
a re-check. A registry plus an explicit "list channels first, diff against the
registry" step is what makes that structural rather than lucky.

THE SWEEP CONTRACT — the order matters:

  1. LIST channels/DMs live (slack_search_channels with
     channel_types=public_channel,private_channel). Diff against the registry.
     A channel not in the registry is a finding in itself.
  2. READ each source since `last_swept_at`, not since "this morning".
  3. DIFF against the board's existing item ids (printed by --plan) so a known
     item is refreshed, not duplicated.
  4. WRITE a briefing JSON and load it with pm_ingest.py, which enforces that
     every chase card ships a ready-to-send draft.
  5. The sweep stamp is written by pm_ingest from the briefing's `sweep` block,
     and pm_state.mark_swept REFUSES a stamp without per-source evidence.

READ TONE, NOT JUST IMPERATIVES. Hadassa's requirement for this app: it should
notice when something is needed, not only when someone writes an instruction.
On 2026-07-29 the eleven tasks Rafael assigned were all imperative and easy;
the message that actually mattered was "Estou preocupado de fazer e ela não
pagar" — a worry, no verb, no ask, and it changed who owned a $10,843.09
standoff. Keyword matching would never surface it. Things to treat as signal:
worry or hesitation about a client or a payment · a decision stated in passing
· a number, date or name mentioned once · someone saying a thing is already
done (that CLOSES a card) · a question left unanswered · an authorisation given
verbally that will need to hit payroll or the ledger later.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_state as S  # noqa: E402

REGISTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "sweep_sources.json")


def load_sources():
    with open(REGISTRY) as f:
        return json.load(f)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", action="store_true", help="full sweep plan")
    ap.add_argument("--sources", action="store_true", help="the registry only")
    ap.add_argument("--check-new", metavar="IDS",
                    help="comma-separated channel ids; reports any not in the registry")
    args = ap.parse_args(argv)

    src = load_sources()
    if args.check_new:
        known = {c["id"] for group in src["slack"].values() for c in group}
        seen = [i.strip() for i in args.check_new.split(",") if i.strip()]
        new = [i for i in seen if i not in known]
        gone = [c["id"] for group in src["slack"].values() for c in group
                if c["id"] not in seen and not c.get("archived")]
        print("🆕 NOT in the registry (investigate + add): %s"
              % (", ".join(new) if new else "none"))
        print("👻 in the registry but not in your list: %s"
              % (", ".join(gone) if gone else "none"))
        return 1 if new else 0

    if args.sources or args.plan:
        print("── SLACK SOURCES (registry v%s, updated %s) ──"
              % (src["version"], src["updated"]))
        for group, chans in src["slack"].items():
            print("\n  %s" % group.upper())
            for c in chans:
                note = (" — %s" % c["note"]) if c.get("note") else ""
                print("    %-13s %-28s%s" % (c["id"], c["name"], note))
        print("\n── MAILBOXES ──")
        for m in src["email"]:
            print("    %-34s %s" % (m["address"], m["note"]))
        print("\n── OTHER SOURCES ──")
        for o in src["other"]:
            print("    %-22s %s" % (o["name"], o["note"]))

    if args.plan:
        state, _ = S.load_state()
        print("\n── SWEEP WINDOW ──")
        print("    board date   : %s" % state.get("brief_date"))
        print("    last swept   : %s" % (state.get("last_swept_at") or "NEVER"))
        print("    → read every source since that timestamp, not since 'today'")
        print("\n── BOARD ALREADY KNOWS %d ITEMS ──" % len(state["items"]))
        print("    (refresh these by id; do NOT create a second card)")
        for it in sorted(state["items"], key=lambda i: (i.get("project") or "", i["id"])):
            print("    %-24s %-16s %s" % (it["id"], (it.get("project") or "-")[:16],
                                          it["subject"][:62]))
        print("\n── THE CONTRACT ──")
        for i, line in enumerate(src["contract"], 1):
            print("    %d. %s" % (i, line))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
