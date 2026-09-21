#!/usr/bin/env python3
"""D17 control: an authentication code must NEVER become a PM card.

Her ruling, 2026-09-21, on two cards the same day:
  rbaadmin Cloudflare codes → "Yes, me - no need to open cards ever because of these ones."
  Citizens one-time passcodes → "Me - don't put this on cards as well."

This pins the STRUCTURAL gate in pm_ingest.ingest(), not a line of prompt guidance.
Run against the pre-fix source to prove it has teeth:
    RFS_INGEST_MODULE=/tmp/prefix_ingest.py python3 test_login_code_never_carded.py
"""
import importlib.util, json, os, shutil, sys, tempfile

MOD = os.environ.get("RFS_INGEST_MODULE",
                     os.path.join(os.path.dirname(os.path.abspath(__file__)), "pm_ingest.py"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("ingest_under_test", MOD)
I = importlib.util.module_from_spec(spec)
spec.loader.exec_module(I)
import pm_state as S

fails = []
def check(name, cond, detail=""):
    (print("  ok   " + name) if cond else
     (fails.append(name), print("  FAIL " + name + ("  — " + detail if detail else ""))))

# ── 1. the predicate itself, both directions ────────────────────────────────
SUPPRESS = [
    "rbaadmin Cloudflare access code arrived 08:56",
    "Citizens one-time passcode 14:23",
    "Your verification code is 449201",
    "Login code for financial.rfsbuilders.app",
    "Capital One two-factor prompt",
    "Here is your security code",
    "448210 is your Citizens code",
    "Confirm your identity to continue",
]
KEEP = [
    "Andersen remittance advice 09/18 $32,144.98",
    "70 Robin inv 0007 overdue 7 days",
    "Marga approved the change order",
    "Access road blocked at 51 Cedar",           # 'access' but not a code
    "Security deposit returned to the client",   # 'security' but not a code
    "Rafael asked for the weekly report again",
]
if hasattr(I, "is_login_code"):
    for s in SUPPRESS:
        check("suppresses: " + s[:46], I.is_login_code({"subject": s}))
    for s in KEEP:
        check("keeps:      " + s[:46], not I.is_login_code({"subject": s}))
    # the phrase can land in any field a collector fills
    check("reads ctx_sum too",
          I.is_login_code({"subject": "rbaadmin login", "ctx_sum": "one-time passcode sent"}))
    check("reads ctx_body too",
          I.is_login_code({"subject": "rbaadmin login", "ctx_body": ["your code is 8891"]}))
else:
    check("is_login_code exists", False, "pre-fix source has no predicate")

# ── 2. the GATE — the part that actually protects her board ─────────────────
tmp = tempfile.mkdtemp()
statep = os.path.join(tmp, "pm_state.json")
st = S.blank_state()
json.dump(st, open(statep, "w"))

items = [
    {"id": "otp_citizens_x", "subject": "Citizens one-time passcode 14:23",
     "lane": "noise", "source": "gmail"},
    {"id": "real_work_x", "subject": "70 Robin inv 0007 overdue 7 days",
     "lane": "action", "source": "gmail", "claude_done": []},
]
try:
    res = I.ingest(items, path=statep)
    after = json.load(open(statep))
    ids = {i["id"] for i in after["items"]}
    check("the OTP card was NOT written", "otp_citizens_x" not in ids,
          "pre-fix source writes it — that is the defect")
    check("the real card WAS written", "real_work_x" in ids)
    check("suppression is reported, not silent",
          res.get("suppressed_login_codes") == ["otp_citizens_x"],
          "got %r" % (res.get("suppressed_login_codes"),))
    logf = os.path.join(os.path.dirname(os.path.abspath(MOD)), "logs",
                        "suppressed_login_codes.jsonl")
    check("suppressed item is logged for audit", os.path.exists(logf))
except Exception as e:
    check("ingest ran", False, repr(e))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n%s — %d failure(s)" % ("PASS" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
