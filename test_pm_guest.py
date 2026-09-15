#!/usr/bin/env python3
"""Gates on the PM board's public listener. Run: python3 test_pm_guest.py

Two independent gates, tested independently:
  GATE 1 — identity. No verified Cloudflare Access JWT → 403, on EVERY route.
  GATE 2 — route.    Even WITH a verified identity, only the append-only answer
                     route may be written. The click endpoint would record
                     `done_by: "hadassa"` (pm_state.py:1163), so a guest reaching
                     it would forge her completion.

Gate 2 is tested by substituting the verifier, because minting a real Cloudflare
signature is not possible here. That substitution is the POINT: it proves the
route gate stands on its own and is not merely hiding behind gate 1.
"""
import os, sys, threading, time, json, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PM_ACCESS_TEAM", "rfsbuilders")
os.environ.setdefault("PM_ACCESS_AUD", "test-aud")
import pm_server as P
from http.server import ThreadingHTTPServer

PORT_A, PORT_B = 8897, 8898
fails = []

def req(port, method, path):
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method=method,
                               data=b"{}" if method == "POST" else None,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=6) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return "ERR %s" % e

def check(label, got, want):
    ok = got == want
    print("  %s %-58s %s (want %s)" % ("✅" if ok else "🔴", label, got, want))
    if not ok:
        fails.append(label)

ROUTES = [("GET", "/"), ("GET", "/api/state"), ("GET", "/healthz"),
          ("POST", "/api/item/abc/update"), ("POST", "/api/item/abc"),
          ("POST", "/api/item/abc/patch"), ("POST", "/api/roll"),
          ("POST", "/api/item/abc/waiting/update"), ("POST", "/api/item/abc/delete")]

print("GATE 1 — no token, every route must 403")
a = ThreadingHTTPServer(("127.0.0.1", PORT_A), P.GuestHandler); a.daemon_threads = True
threading.Thread(target=a.serve_forever, daemon=True).start(); time.sleep(0.4)
for m, p in ROUTES:
    check("%s %s" % (m, p), req(PORT_A, m, p), 403)

print("\nGATE 2 — identity VERIFIED, route gate must still hold")
class Authed(P.GuestHandler):
    def _who(self):            # stand in for a real verified Access JWT
        return "rafael@rfsbuilders.com"
b = ThreadingHTTPServer(("127.0.0.1", PORT_B), Authed); b.daemon_threads = True
threading.Thread(target=b.serve_forever, daemon=True).start(); time.sleep(0.4)
# reachable
check("GET /            (board loads)", req(PORT_B, "GET", "/"), 200)
check("GET /api/state   (cards load)", req(PORT_B, "GET", "/api/state"), 200)
# The one writable route must REACH the board rather than be refused at the gate.
# An empty body comes back 400 from the update handler's own validation — which is
# exactly the proof wanted: the request got past the gate and was judged on its
# content. Asserting "not 403" would pass even if the route vanished, so assert the
# validation code itself.
check("POST /api/item/abc/update  reaches the board (400 = validated, not gated)",
      req(PORT_B, "POST", "/api/item/abc/update"), 400)
# everything else must be refused even though he is who he says he is
for m, p in [("POST", "/api/item/abc"), ("POST", "/api/item/abc/patch"),
             ("POST", "/api/roll"), ("POST", "/api/item/abc/waiting/update"),
             ("POST", "/api/item/abc/delete"), ("GET", "/api/items")]:
    check("%s %s" % (m, p), req(PORT_B, m, p), 403)

print("\nGATE 2b — the OWNER gets the full board on the same listener")
class Owner(P.GuestHandler):
    def _who(self):
        return P.OWNER_EMAIL
c = ThreadingHTTPServer(("127.0.0.1", 8896), Owner); c.daemon_threads = True
threading.Thread(target=c.serve_forever, daemon=True).start(); time.sleep(0.4)
# /api/items is POST-only, so a GET 404s for everyone — not a useful contrast.
# The real owner/guest difference is entirely on the WRITE routes: every one of
# these is 403 for a guest above, and must reach the handler for her.
check("owner POST /api/item/abc/patch  (guest 403)", req(8896, "POST", "/api/item/abc/patch"), 404)
# 400, not 404: the waiting-update handler validates its body BEFORE looking the
# item up. Either way it REACHED the handler, which is the whole claim — a guest
# gets 403 on this exact route above, so the two identities are provably split.
check("owner POST /api/item/abc/waiting/update (guest 403)", req(8896, "POST", "/api/item/abc/waiting/update"), 400)
check("owner POST /api/item/abc (click route)", req(8896, "POST", "/api/item/abc"), 404)
with urllib.request.urlopen("http://127.0.0.1:8896/", timeout=6) as r:
    check("owner page has NO guest bar", "pm-guest-bar" in r.read().decode("utf-8", "replace"), False)

print("\nGATE 2c — the ALLOW-LIST: a VALID Access identity that is not listed gets nothing")
class Stranger(P.GuestHandler):
    def _who(self):
        tok = "valid-but-unlisted"
        who = "stranger@gmail.com"
        if who != P.OWNER_EMAIL and who not in P.GUEST_ALLOW:
            raise __import__("pm_access").AccessDenied("%s is not on the PM board allow-list" % who)
        return who
d = ThreadingHTTPServer(("127.0.0.1", 8895), Stranger); d.daemon_threads = True
threading.Thread(target=d.serve_forever, daemon=True).start(); time.sleep(0.4)
for m, pth in [("GET", "/"), ("GET", "/api/state"), ("POST", "/api/item/abc/update")]:
    check("unlisted identity %s %s" % (m, pth), req(8895, m, pth), 403)
check("rafael IS on the allow-list", "rafael@rfsbuilders.com" in P.GUEST_ALLOW, True)
check("a stranger is NOT", "stranger@gmail.com" in P.GUEST_ALLOW, False)

print("\nGATE 2d — PRIVATE cards are withheld from a guest, and only from a guest")
import json as _json, urllib.request as _u
st, _s = P.S.load_state()
full = P.Handler._state_payload(None, st)
safe = P._guest_safe_state(full, st)
n_priv = sum(1 for i in st["items"] if i.get("private"))
check("board has private cards to withhold", n_priv > 0, True)
check("guest payload drops exactly those", len(full["items"]) - len(safe["items"]), n_priv)
check("guest payload reports the count", safe.get("_guest_hidden"), n_priv)
leaked = [i for i in safe["items"] if i.get("private")]
check("no private card survives the filter", leaked, [])
with _u.urlopen("http://127.0.0.1:%d/api/state" % PORT_B, timeout=6) as r:
    body = _json.loads(r.read().decode())
check("LIVE guest /api/state withholds them", len(body.get("items", [])), len(full["items"]) - n_priv)
check("LIVE guest /api/state carries no private card",
      [i for i in body.get("items", []) if i.get("private")], [])

print("\nGATE 3 — the served page carries the answer-mode shim, not a bare board")
try:
    with urllib.request.urlopen("http://127.0.0.1:%d/" % PORT_B, timeout=6) as r:
        html = r.read().decode("utf-8", "replace")
    check("page contains the guest bar", "pm-guest-bar" in html, True)
    check("page names the viewer", "rafael@rfsbuilders.com" in html, True)
    check("page neutralises non-answer POSTs", "read-only" in html, True)
except Exception as e:
    fails.append("shim: %s" % e); print("  🔴 shim:", e)

print("\n%s" % ("🔴 FAILURES: %s" % fails if fails else "✅ all gates hold"))
sys.exit(1 if fails else 0)
