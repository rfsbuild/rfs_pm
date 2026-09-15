#!/usr/bin/env python3
"""A guest must not read the audit's critique of HER.

The 2026-09-15 board audit writes an `audit` overlay onto every open card
(scripts/apply_audit_overlay.py). Most of it is about the WORK — a verdict, an
evidence line, who owns it — and is useful to whoever picks the card up, Rafael
included. Two parts are not:

    contra      quotes her own note back and says the evidence disagrees
    lost_tick   records that a completion of hers did not survive

Rafael is on the allow-list for the board's CONTENT. That is not the same as
being an audience for feedback about how she works, so those two are stripped
from the guest payload, along with the band that names them.

ASSERTED ON THE SERVED PAYLOAD, not on the filter's intent — the 2026-09-15
private-card leak happened because the filter read the wrong object and matched
nothing while looking correct.

    python3 tests/test_guest_audit_scrub.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import pm_server as P                                            # noqa: E402

# the evidence / recommended-action text of all five contradicted cards
CRITIQUE = [
    "An unauthenticated fetch on 9/14 returned HTTP 200",
    "matches the SEPARATE Beresford story",
    "Contradicted the same evening",
    "Justine's call window was still unbooked",
    "Speculative in your own words",
]


def main():
    with open(os.path.join(HERE, "pm_state.json")) as fh:
        state = json.load(fh)
    items, clicks = P.to_ui(state)
    safe = P._guest_safe_state({"items": items, "state": clicks}, state)
    g, blob = safe["state"], json.dumps(safe)
    fails = []

    owner_contra = [k for k, v in clicks.items() if (v.get("audit") or {}).get("contra")]
    owner_lost = [k for k, v in clicks.items() if (v.get("audit") or {}).get("lost_tick")]
    if not owner_contra or not owner_lost:
        fails.append("the OWNER payload carries no contra/lost_tick — either the "
                     "overlay was never applied or this test is vacuous")

    for name, key in (("contra", "contra"), ("lost_tick", "lost_tick")):
        leaked = [k for k, v in g.items() if (v.get("audit") or {}).get(key)]
        if leaked:
            fails.append("guest payload exposes `%s` on %d card(s): %s"
                         % (name, len(leaked), ", ".join(leaked[:5])))

    banded = [k for k, v in g.items()
              if (v.get("audit") or {}).get("band") in ("CONTRADICTED", "LOST TICK")]
    if banded:
        fails.append("guest payload still bands %d card(s) as CONTRADICTED/LOST TICK: %s"
                     % (len(banded), ", ".join(banded[:5])))

    for frag in CRITIQUE:
        # `audit` is the only place this test owns. Pre-existing card content
        # (action / hadassaTodo / ctx_*) is board content and is out of scope —
        # so search the overlay, not the whole blob.
        hits = [k for k, v in g.items() if frag in json.dumps((v or {}).get("audit"))]
        if hits:
            fails.append("critique text %r reaches a guest via `audit` on %s" % (frag, hits))

    # The useful half must SURVIVE — a scrub that removes everything is not a
    # scrub, it is a regression.
    kept = {k for k, v in g.items() if (v.get("audit") or {}).get("verdict")}
    own = {k for k, v in clicks.items() if (v.get("audit") or {}).get("verdict")}
    priv = {i["id"] for i in state["items"] if i.get("private")}
    if not kept:
        fails.append("no verdicts survive for a guest at all")
    elif (own - kept) != (own & priv):
        fails.append("a guest lost verdicts for a reason other than privacy: %s"
                     % ", ".join(sorted((own - kept) - priv))[:200])

    for f in fails:
        print("FAIL  " + f)
    if not fails:
        print("ok    owner sees contra on %d and lost_tick on %d" % (len(owner_contra), len(owner_lost)))
        print("ok    guest sees neither, on any card")
        print("ok    none of the 5 critique texts reach a guest through `audit`")
        print("ok    %d verdicts survive for a guest; the %d missing are all private cards"
              % (len(kept), len(own - kept)))
    print("\n%d FAIL" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
