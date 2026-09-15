#!/usr/bin/env python3
"""Pins for route_finance() — the one-sweep/one-router/two-sinks contract
(her ruling 2026-08-28). Asymmetry is the load-bearing part: a malformed
finance LEAD is dropped and recorded per item, never allowed to kill a
briefing the way one bad lane killed all 30 items on 2026-08-24."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pm_sweep_run as R  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "finance_intake.json"

    # 1 · a valid lead lands with consumed=False and stamps
    r = R.route_finance([{"key": "bt_teresa_3483_0827",
                          "subject": "Teresa Spillane sent a $3,483.83 payment",
                          "action": "expect it in the account Aug 29",
                          "amount": 3483.83, "urgent": False,
                          "detail": "BT payment processing", "source_ref": "1a04426f"}],
                        path=p)
    d = json.loads(p.read_text())
    check("valid lead routed", r["routed"] == 1 and not r["dropped"])
    it = d["items"][0]
    check("lead lands unconsumed with stamps",
          it["consumed"] is False and it["first_seen"] and it["last_seen"])

    # 2 · re-seen key refreshes content but PRESERVES consumed
    d["items"][0]["consumed"] = True
    p.write_text(json.dumps(d))
    r = R.route_finance([{"key": "bt_teresa_3483_0827",
                          "subject": "Teresa payment UPDATE",
                          "action": "posted", "amount": 3483.83}], path=p)
    d = json.loads(p.read_text())
    check("upsert preserves consumed flag",
          r["routed"] == 1 and d["items"][0]["consumed"] is True
          and d["items"][0]["subject"] == "Teresa payment UPDATE")

    # 3 · per-item validation: bad ones drop with reasons, good ones still land
    r = R.route_finance([
        {"key": "", "subject": "no key", "action": "x"},                     # missing key
        {"key": "k2", "subject": "amount is prose", "action": "x",
         "amount": "about $900"},                                            # bad amount
        {"key": "k3", "subject": "good", "action": "review"},                # good, null amount
        {"key": "k3", "subject": "dup", "action": "review"},                 # dup key
        "not an object",                                                     # junk
    ], path=p)
    d = json.loads(p.read_text())
    check("bad leads dropped with reasons, good one landed",
          r["routed"] == 1 and len(r["dropped"]) == 4
          and any(i["key"] == "k3" for i in d["items"]),
          f"routed={r['routed']} dropped={r['dropped']}")
    check("amount-as-prose is named in the drop reason",
          any("number or null" in x for x in r["dropped"]))

    # 4 · empty input touches nothing
    before = p.read_text()
    r = R.route_finance([], path=p)
    check("empty input is a no-op", r == {"routed": 0, "dropped": []}
          and p.read_text() == before)

    # 5 · the intake file matches what needs_you._produce_email_intake reads
    sys.path.insert(0, "/Users/Hadassa/rfs_dashboard")
    import needs_you as ny
    import importlib
    importlib.reload(ny)
    ny_cards = []
    orig = ny.INTAKE_PATH
    try:
        ny.INTAKE_PATH = p
        ny_cards = ny._produce_email_intake()
    finally:
        ny.INTAKE_PATH = orig
    check("needs_you turns unconsumed leads into mail cards, skips consumed",
          sorted(c["id"] for c in ny_cards) == ["mail_k3"],
          f"got {[c['id'] for c in ny_cards]}")

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
print("ALL PASS")
