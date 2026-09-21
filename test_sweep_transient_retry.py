#!/usr/bin/env python3
"""Pins the 2026-09-21 defect: an API 529 reported as "gmail and slack said nothing".

Every assertion here FAILS against pm_sweep_run.py.bak_20260921_pre_retry.
Point RFS_SWEEP_MODULE at that backup to prove it:

    RFS_SWEEP_MODULE=pm_sweep_run.py.bak_20260921_pre_retry python3 test_sweep_transient_retry.py
"""
import importlib.util, json, os, pathlib, shutil, subprocess, sys, tempfile

ROOT = pathlib.Path("/Users/Hadassa/rfs_pm")
MOD  = os.environ.get("RFS_SWEEP_MODULE", "pm_sweep_run.py")
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("sweep_under_test", pathlib.Path(MOD) if os.path.isabs(MOD) else ROOT / MOD)
R = importlib.util.module_from_spec(spec); spec.loader.exec_module(R)

FAILS = []
def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -> " + detail) if not cond and detail else ""))
    if not cond: FAILS.append(name)

OVERLOAD = "API Error: 529 Overloaded. This is a server-side issue, usually temporary — try again in a moment.\n"
GOOD     = "did the thing\nSLACK_OK=3\nGMAIL_OK=4\nSLACK_CURSOR=1.5\nGMAIL_CURSOR=9\n"

class Fake:
    """Returns each scripted output in turn; records how many times it ran."""
    def __init__(self, outs): self.outs, self.calls = list(outs), 0
    def __call__(self, *a, **k):
        out = self.outs[min(self.calls, len(self.outs) - 1)]
        self.calls += 1
        class P: pass
        P.stdout, P.stderr, P.returncode = out, "", 0
        return P

def fresh_state():
    d = pathlib.Path(tempfile.mkdtemp())
    p = d / "pm_state.json"
    shutil.copy2(ROOT / "pm_state.json", p)
    return p

print("MODULE UNDER TEST:", MOD)

# ── 1. the detector exists and reads the real 529 text ─────────────────────
print("\n1. transient_error()")
has = hasattr(R, "transient_error")
check("transient_error() exists", has)
if has:
    check("detects the real 529 line", bool(R.transient_error(OVERLOAD)))
    check("clean marker output is NOT transient", R.transient_error(GOOD) is None)
    check("non-zero exit is transient", bool(R.transient_error("", 3)))

# ── 2. a 529 is RETRIED ────────────────────────────────────────────────────
print("\n2. retry on a transient failure")
R.RETRY_BACKOFF_S = [0, 0] if hasattr(R, "RETRY_BACKOFF_S") else []
fake = Fake([OVERLOAD]); R.subprocess.run = fake
res = R.run_sweep(path=fresh_state())
check("retried rather than giving up after one call", fake.calls >= 3,
      "called %d time(s)" % fake.calls)

# ── 3. the reason names the API and CLEARS the connectors ──────────────────
print("\n3. the failure reason points at the right system")
reason = (res or {}).get("reason", "")
check("reason mentions the API error", "529" in reason or "Overloaded" in reason,
      repr(reason[:120]))
check("reason does NOT blame gmail/slack for saying nothing",
      "never said what it read" not in reason, repr(reason[:120]))
check("reason states neither connector was reached",
      "NOT gmail or slack" in reason or "neither was reached" in reason,
      repr(reason[:120]))

# ── 4. a run that EARNS markers is never retried over ──────────────────────
print("\n4. a good result is never discarded to retry")
fake2 = Fake([GOOD]); R.subprocess.run = fake2
R.run_sweep(path=fresh_state())
check("stopped after the first successful attempt", fake2.calls == 1,
      "called %d time(s)" % fake2.calls)

# ── 5. a transient blip that then SUCCEEDS is recovered ────────────────────
print("\n5. recovery: 529 then a good run")
fake3 = Fake([OVERLOAD, GOOD]); R.subprocess.run = fake3
res3 = R.run_sweep(path=fresh_state())
check("second attempt was made", fake3.calls == 2, "called %d time(s)" % fake3.calls)
check("the recovered run is reported ok", (res3 or {}).get("ok") is True, repr(res3)[:150])

# ── 6. failed outputs are retained, not overwritten to one ─────────────────
print("\n6. failed-output retention")
check("FAIL_DIR / _keep_output exist", hasattr(R, "_keep_output") and hasattr(R, "FAIL_DIR"))

print("\n%s  (%d failure(s))" % ("ALL PASS" if not FAILS else "FAILURES: " + ", ".join(FAILS), len(FAILS)))
sys.exit(1 if FAILS else 0)
