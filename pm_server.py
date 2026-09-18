#!/usr/bin/env python3
"""PM Command Center — HTTP server.

Serves the HTML board Hadassa likes (`pm_ui.html`, built by `build_ui.py` from
the briefing design) over a tiny JSON API backed by `pm_state.py`.

Replaces the Streamlit front-end, which she found confusing: "I like the
previous HTML design a LOT better." The architecture from Phase 1 is unchanged
— server-side state, every click persisted, no Export button — only the
presentation layer went back to the HTML.

Stdlib only. Binds 127.0.0.1 exclusively: the board carries client names,
amounts and her private notes and must never be reachable from the LAN.

    GET  /                    the board
    GET  /api/state           {items, state, brief_date, stale_day, counts}
    GET  /api/history         finished items from PAST days (the Done view's
                              "All time" scope — see the route for why)
    POST /api/item/<id>       persist one item's click-state
    POST /api/item/<id>/patch update source-owned fields (subject, action, due…)
    POST /api/items           create a new item she typed herself
    POST /api/roll            roll the day forward
    GET  /healthz             liveness for launchd / curl
"""
import datetime
import json
import os
import re
import threading
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_access
import pm_state as S  # noqa: E402

REQ_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "pm_requests.log")
HOST, PORT = "127.0.0.1", 8789
# ── the public (guest) listener ───────────────────────────────────────────────
# Her board stays on PORT with no auth: only she can reach loopback. Rafael
# answers on GUEST_PORT, which cloudflared fronts and which refuses every
# request that does not carry a verified Cloudflare Access JWT. Two ports, one
# process, one board, one state file — her 2026-09-14 ruling ("why don't we
# just put the one I have") with the one change that makes it safe to hand out.
GUEST_PORT = 8793
ACCESS_TEAM = os.environ.get("PM_ACCESS_TEAM", "")
ACCESS_AUD = os.environ.get("PM_ACCESS_AUD", "")
# The board's owner. On the guest listener SHE gets the full board — the gates
# below are about WHO is asking, not which port they arrived on, so opening
# pm.rfsbuilders.app from her phone gives her the same board as her desk.
OWNER_EMAIL = os.environ.get("PM_OWNER_EMAIL", "hadassa@rfsbuilders.com").lower()
# 🔴 THE GUEST ALLOW-LIST — added 2026-09-15 after the change-audit found it missing.
# Cloudflare Access proves an identity; it does NOT prove that identity belongs here.
# The first cut branched only on `who == OWNER_EMAIL`, so ANY address the Access
# policy admitted got the whole board. The policy is a Cloudflare setting nobody
# here reads back, and a 302 to a login page proves a wall exists, never who it
# admits — so the allow-list lives in code, where it can be tested, and the server
# refuses an unlisted identity even when Access has already said yes.
GUEST_ALLOW = {e.strip().lower() for e in os.environ.get(
    "PM_GUEST_ALLOW", "rafael@rfsbuilders.com").split(",") if e.strip()}

# Cards a guest must never be served. `private: true` on the card is the switch;
# she can unset it on any card at any time. Added 2026-09-15: /api/state was
# serving every card unfiltered, including live Cloudflare Access one-time codes.
def _guest_safe_state(payload, state=None):
    """Strip private cards from the state a guest receives, and say how many.

    The private ids come from the RAW state, never from the UI payload: `to_ui()`
    projects a whitelist of fields and `private` is not one of them, so filtering
    the payload on `it["private"]` silently matched nothing and served every card.
    That is exactly how this leaked the first time — the flag existed, the filter
    read the wrong object. Caught 2026-09-15 by the test, not by reading the code.
    """
    private_ids = set()
    if state is not None:
        private_ids = {i.get("id") for i in state.get("items", []) if i.get("private")}
    items = payload.get("items") or []
    keep, hidden = [], 0
    for it in items:
        if it.get("private") or it.get("id") in private_ids:
            hidden += 1
            continue
        keep.append(it)
    out = dict(payload)
    out["items"] = keep
    clicks = out.get("state")
    if isinstance(clicks, dict):
        ids = {i.get("id") for i in keep}
        out["state"] = {k: v for k, v in clicks.items() if k in ids}
    # ── the audit overlay is filtered too, not just the card list ──
    # `contra` quotes HER note back and says the evidence disagrees with it, and
    # `lost_tick` records that a completion of hers did not survive. Both are
    # feedback to the owner of the board about her own work. Rafael is on the
    # allow-list for the board's CONTENT, which is not the same as being an
    # audience for that. The verdict, the evidence line and the owner stay —
    # those are about the WORK and are useful to whoever picks the card up.
    clicks2 = out.get("state")
    if isinstance(clicks2, dict):
        scrubbed = {}
        for k, v in clicks2.items():
            a = (v or {}).get("audit")
            if isinstance(a, dict) and (a.get("contra") or a.get("lost_tick")):
                v = dict(v)
                a = dict(a)
                a.pop("contra", None)
                a.pop("lost_tick", None)
                a["band"] = None if a.get("band") in ("CONTRADICTED", "LOST TICK") else a.get("band")
                v["audit"] = a
            scrubbed[k] = v
        out["state"] = scrubbed
    out["_guest_hidden"] = hidden
    return out
UI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pm_ui.html")

# item fields the UI renders (camelCase to match the HTML engine)
_CONTENT_MAP = {
    "id": "id", "source": "source", "project": "project", "lane": "lane",
    "kind": "kind", "subject": "subject", "meta": "meta", "ctxSum": "ctx_sum",
    "ctxBody": "ctx_body", "action": "action", "where": "where",
    "links": "links", "draft": "draft", "pills": "pills",
    "unconfirmed": "unconfirmed", "isNew": "is_new", "moved": "moved",
    "age": "age", "due": "due",
    # Sweep timestamps (2026-07-30). Collector output, so _CONTENT_MAP and not
    # `clicks`. Needed because "By project" orders groups by MOST RECENT WORK —
    # without these the comparator has nothing to compare and silently degrades
    # to string order, which puts "104 Child" above "14 Guernsey".
    "lastSeen": "last_seen", "firstSeen": "first_seen",
    # The three-line context contract (2026-07-30). Collector output, so here
    # and not in `clicks` — an ingest SHOULD refresh these when the facts move.
    "ctxHappened": "ctx_happened", "ctxMatters": "ctx_matters",
    "ctxNeeded": "ctx_needed", "ctxTrimmed": "ctx_trimmed",
    # The split (2026-07-29) — what Claude already did vs what only she can do.
    # Must be here or the card renders without it and the whole point is lost.
    "claudeDone": "claude_done", "hadassaTodo": "hadassa_todo",
}


def to_ui(state):
    items, clicks = [], {}
    for it in state["items"]:
        items.append({k: it.get(v) for k, v in _CONTENT_MAP.items()})
        clicks[it["id"]] = {
            "status": it.get("status", "open"),
            "done": it.get("status") == "done",
            "done_at": it.get("done_at"),
            "dismiss_reason": it.get("dismiss_reason"),
            "dismissed_at": it.get("dismissed_at"),
            "assignee": it.get("assignee"),
            "defer": it.get("defer"),
            "deferDays": int(it.get("defer_days") or 0),
            "note": it.get("note"),
            "project": it.get("project"),
            "followup": it.get("followup"),
            # The waiting space (2026-07-29). In `clicks` and not `_CONTENT_MAP`
            # because it is HER record, not collector output — an ingest run must
            # never be able to overwrite a chase log.
            "waiting": it.get("waiting"),
            "waiting_due": S.nudge_due(it),
            "waiting_stale_days": S.days_since_update(it) if it.get("waiting") else 0,
            # Delegation to Claude (2026-07-29). `done_by` must reach the UI or a
            # Claude completion renders identically to hers, and the board stops
            # being a record of HER day.
            "claude_queued_at": it.get("claude_queued_at"),
            "queued_hours": S.hours_queued(it) if it.get("assignee") == "claude" else None,
            "done_by": it.get("done_by"),
            "claude_result": it.get("claude_result"),
            # `did` — what SHE actually did, in her voice (2026-07-30). It has
            # existed in ITEM_FIELDS and PATCHABLE since 2026-07-28 but was in
            # NEITHER _CONTENT_MAP nor here, so it never reached the browser:
            # gen_daily_report.py could read it server-side while the UI could
            # neither show nor set one. That is the whole reason 28 of 49
            # completed items carry no `did` — the board recorded only THAT a
            # card closed, never WHAT was done. Her ask: "if they ask me if I
            # did something and I don't remember, I need to have that
            # somewhere to confirm." In `clicks`, not `_CONTENT_MAP`, for the
            # same reason as `waiting`: it is her record, and an ingest run
            # must never be able to overwrite it.
            "did": it.get("did"),
            # The append-only update stream (2026-07-30). In `clicks` for the
            # same reason as `waiting` and `did`: it is HER record, and an
            # ingest run must never be able to overwrite what she wrote down.
            "updates": it.get("updates") or [],
            # The board audit's verdict overlay (2026-09-15). In `clicks` and
            # not `_CONTENT_MAP` for the same reason as `waiting` and `did`: an
            # ingest run must never be able to overwrite it. It is written only
            # by scripts/apply_audit_overlay.py, and it carries no card state —
            # only evidence for a decision she still makes herself.
            "audit": it.get("audit"),
            # HER ruling on that verdict — "closed" / "kept" / None. Separate
            # from `audit` on purpose: one is the opinion, the other is her
            # answer to it, and an overlay rewrite must never erase her answer.
            "auditRuled": it.get("audit_ruled"),
        }
    return items, clicks


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ── the mutation log (2026-09-15) ──
    # This used to be `pass` — every request, including every write, was
    # silent. On 2026-09-14 Hadassa ticked 14 cards and 11 were open again the
    # next morning, and the question "did that POST ever arrive?" was
    # UNANSWERABLE: no request log, and pm_state.json had no version history
    # either. A board with no record of its own writes cannot be debugged, and
    # she runs the company off this one from 2026-09-16.
    #
    # GETs stay silent on purpose — the UI polls /api/state, so logging reads
    # would bury the writes in noise and teach everyone to ignore the file.
    # Only mutations are logged, with a timestamp and the response code, so a
    # failed or never-arrived write is visible after the fact.
    def log_message(self, fmt, *args):
        if self.command == "GET":
            return
        try:
            # Log the handler's own formatted message rather than picking an
            # index out of *args: log_request() and log_error() pass different
            # shapes, and args[1] is the status code in one and the error text
            # in the other. Formatting it the way the base class would keeps
            # both readable instead of silently mislabelling one.
            msg = fmt % args if args else str(fmt)
            with open(REQ_LOG, "a") as fh:
                fh.write("%s  %s  %s\n" % (
                    datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                    getattr(self, "command", "?"), msg))
        except Exception:
            pass    # a logging fault must never break a write

    # ── helpers ──
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, default=str)
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _actor(self):
        """WHO is writing. The loopback listener is reachable only from her Mac,
        so on this handler the answer is always her. GuestHandler overrides it —
        and it MUST, because /api/item/<id>/update is the one route Rafael can
        write to. Taking the author from the request body instead would let a
        guest's answer be stored as hers, which is the attribution law this board
        is built on. UPDATED 2026-09-18: the guest may now write TWO routes —
        /api/item/<id>/update and the click endpoint /api/item/<id> — and this
        actor is what keeps that honest, because apply_click() now stores
        done_by = actor instead of a hardcoded "hadassa"."""
        return "hadassa"

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ── routes ──
    def _state_payload(self, state):
        """The /api/state body. Extracted 2026-09-15 so the guest listener can
        serve the SAME payload minus private cards — a second, divergent copy is
        how a field quietly stops being filtered."""
        items, clicks = to_ui(state)
        return {
            "items": items, "state": clicks,
            "brief_date": state.get("brief_date"),
            "updated_at": state.get("updated_at"),
            # Sweep provenance. `updated_at` moves on any click of hers, so it
            # answers "when was this file last written", NOT "how current is
            # the information" — which is the only question the header asks.
            "last_swept_at": state.get("last_swept_at"),
            # A failed sweep must reach the page. mark_swept() already
            # REFUSES to stamp a sweep it has no evidence for, but a refusal
            # that only raises into a log leaves the board showing older
            # data while looking perfectly healthy — a confident, partial
            # brief, which is the exact failure this is here to prevent.
            "last_sweep_failure": state.get("last_sweep_failure"),
            "last_swept_sources": state.get("last_swept_sources") or [],
            "last_swept_evidence": state.get("last_swept_evidence") or {},
            "stale_day": state.get("brief_date") != S._today(),
            "today": S._today(),
            "counts": S.counts(state),
            "claude_queue": len(S.claude_queue(state)),
            "lanes": list(S.LANES),
            "assignees": list(S.ASSIGNEES),
            "defer_reasons": S.DEFER_REASONS,
        }

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            return self._send(200, S.health())
        if path in ("/", "/index.html"):
            if not os.path.exists(UI):
                return self._send(500, "pm_ui.html missing — run: python3 build_ui.py",
                                  "text/plain; charset=utf-8")
            with open(UI, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/state":
            state, status = S.load_state()
            if state is None:
                return self._send(500, {"error": "state %s" % status})
            return self._send(200, self._state_payload(state))
        if path == "/api/history":
            # Finished items from days already rolled forward. `archive_finished`
            # and `history_between` have existed since the day-roll shipped, but
            # NO route exposed them — so the archive was unreachable from the
            # board and an archive nobody can read is not a record. The Done
            # view's "All time" scope would otherwise silently show only the
            # current day's items while claiming to show everything, which is
            # the same count-vs-content defect as the Open tile on 2026-07-29.
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            start = (qs.get("start") or ["0000-00-00"])[0]
            end = (qs.get("end") or ["9999-99-99"])[0]
            try:
                rows = S.history_between(start, end)
            except Exception as exc:                     # never 500 the board
                return self._send(200, {"items": [], "error": str(exc)})
            return self._send(200, {"items": rows, "start": start, "end": end})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._body()

        if path == "/api/roll":
            return self._send(200, S.roll_forward() or {})

        if path == "/api/items":
            iid = (body.get("id") or "").strip() or S.slug_id(body.get("subject", "item"))
            item = S.new_item(
                iid, body.get("subject", "(untitled)"),
                source=body.get("source", "manual"),
                lane=body.get("lane", "action"),
                kind=body.get("kind", "action"),
                project=body.get("project") or None,
                action=body.get("action", ""),
                meta=body.get("meta", ""),
                due=body.get("due") or None,
            )
            S.upsert_item(item)
            return self._send(200, {"ok": True, "id": iid})

        if path.startswith("/api/item/"):
            rest = path[len("/api/item/"):]
            # ── the waiting space ──
            # Deliberately NOT folded into apply_click: entering waiting requires
            # a name, and an update must APPEND. Both are refusals that the
            # generic click endpoint has no way to express, and there is
            # intentionally no route that edits or deletes a log line.
            if rest.endswith("/waiting/update"):
                iid = rest[: -len("/waiting/update")]
                res = S.add_waiting_update(iid, body.get("text"),
                                           nudge_on=body.get("nudge_on") or None)
                if res is None:
                    return self._send(404, {"error": "no such item"})
                return self._send(400 if res.get("error") else 200, res)
            # Sits above the bare "/waiting" branch for the same reason the
            # comment below spells out for "/update": keep every longer waiting
            # path ahead of the short one so an id can never absorb a suffix.
            if rest.endswith("/waiting/kind"):
                iid = rest[: -len("/waiting/kind")]
                res = S.set_waiting_kind(iid, body.get("kind"),
                                         renudge=body.get("renudge", True))
                if res is None:
                    return self._send(404, {"error": "no such item"})
                return self._send(400 if res.get("error") else 200, res)
            if rest.endswith("/waiting"):
                iid = rest[: -len("/waiting")]
                if body.get("clear"):
                    res = S.clear_waiting(iid, reason=body.get("reason"))
                elif body.get("who_only"):
                    res = S.set_waiting_who(iid, body.get("who"))
                else:
                    res = S.set_waiting(iid, body.get("who"),
                                        what=body.get("what") or "",
                                        nudge_on=body.get("nudge_on") or None,
                                        kind=body.get("kind") or None,
                                        first_update=body.get("first_update") or None)
                if res is None:
                    return self._send(404, {"error": "no such item"})
                return self._send(400 if res.get("error") else 200, res)
            # ── the update stream (2026-07-30) ──
            # MUST stay below the /waiting/update branch above: that path also
            # ends in "/update", so testing this one first would swallow every
            # chase-log write and parse the id as "<id>/waiting". Ordering is
            # the whole safety here, hence this comment rather than a subtler
            # regex.
            #
            # Append-only, like the chase log — there is deliberately no route
            # that edits or deletes an entry.
            if rest.endswith("/update"):
                iid = rest[: -len("/update")]
                res = S.add_update(iid, body.get("text"),
                                   kind=body.get("kind") or "update",
                                   set_did=bool(body.get("set_did")),
                                   by=self._actor())
                if res is None:
                    return self._send(404, {"error": "no such item"})
                return self._send(400 if res.get("error") else 200, res)
            if rest.endswith("/patch"):
                iid = rest[: -len("/patch")]
                ok = S.patch_content(iid, body)
                return self._send(200 if ok else 404, {"ok": bool(ok)})
            if rest.endswith("/delete"):
                iid = rest[: -len("/delete")]
                return self._send(200, {"ok": bool(S.remove_item(iid))})
            iid = rest
            res = S.apply_click(iid, body, actor=self._actor())
            if res is None:
                return self._send(404, {"error": "no such item"})
            return self._send(200, res)

        return self._send(404, {"error": "not found"})


class GuestHandler(Handler):
    """The internet-facing face of the same board.

    Two gates, and the ORDER matters: identity first, then route. A 403 for an
    unknown caller must never depend on which path they asked for.

    WRITE SURFACE: two routes — POST /api/item/<id>/update (the append-only
    update stream) and POST /api/item/<id> (the click endpoint). The click route
    was blocked until 2026-09-18 because apply_click() hardcoded
    `done_by: "hadassa"`, so a guest tick forged her completion. The fix is at
    the source: apply_click now takes an `actor` and this handler passes the
    Access-verified e-mail, so a guest completion is stored as the guest.
    /patch, /api/roll and /waiting/* stay blocked — none of those is answering.
    """

    GUEST_GET = ("/", "/index.html", "/healthz", "/api/state", "/api/history")

    def _deny(self, why):
        print("[pm-guest] 403 %s %s — %s" % (self.command, self.path, why), flush=True)
        return self._send(403, {"error": "not permitted"})

    def _who(self):
        tok = pm_access.token_from(self.headers, self.headers.get("Cookie", ""))
        who = pm_access.verify(tok, ACCESS_TEAM, ACCESS_AUD)
        # Second gate: Access said WHO you are. This says whether you belong here.
        if who != OWNER_EMAIL and who not in GUEST_ALLOW:
            raise pm_access.AccessDenied("%s is not on the PM board allow-list" % who)
        return who

    def _actor(self):
        try:
            who = self._who()
        except pm_access.AccessDenied:
            return "unknown"
        return "hadassa" if who == OWNER_EMAIL else who

    def do_GET(self):
        try:
            who = self._who()
        except pm_access.AccessDenied as exc:
            return self._deny(exc)
        if who == OWNER_EMAIL:                     # her own board, from anywhere
            return Handler.do_GET(self)
        path = self.path.split("?")[0]
        if path not in self.GUEST_GET:
            return self._deny("GET %s is not on the guest allow-list" % path)
        if path in ("/", "/index.html"):
            if not os.path.exists(UI):
                return self._send(500, "pm_ui.html missing", "text/plain; charset=utf-8")
            with open(UI, "rb") as f:
                html = f.read().decode("utf-8", "replace")
            return self._send(200, (html + _guest_shim(who)).encode("utf-8"),
                              "text/html; charset=utf-8")
        if path == "/api/state":
            state, status = S.load_state()
            if state is None:
                return self._send(500, {"error": "state %s" % status})
            payload = self._state_payload(state)
            safe = _guest_safe_state(payload, state)
            print("[pm-guest] /api/state served to %s (%d card(s) withheld as private)"
                  % (who, safe.get("_guest_hidden", 0)), flush=True)
            return self._send(200, safe)
        print("[pm-guest] %s served to %s" % (path, who), flush=True)
        return Handler.do_GET(self)

    def do_POST(self):
        try:
            who = self._who()
        except pm_access.AccessDenied as exc:
            return self._deny(exc)
        if who == OWNER_EMAIL:                     # her clicks are her clicks
            return Handler.do_POST(self)
        path = self.path.split("?")[0]
        # The ONLY writable route. Note the explicit /update suffix test: the
        # waiting stream also ends in "/update" and must NOT be reachable, so
        # this matches the whole shape rather than the tail.
        # TWO writable routes since 2026-09-18 (HER ruling, incl. "yes" to a guest
        # closing cards flagged HERS):
        #   /api/item/<id>/update  — the append-only answer stream
        #   /api/item/<id>         — the click endpoint (tick / defer / dismiss)
        # The click route was blocked until today for ONE reason: apply_click()
        # hardcoded done_by="hadassa", so a guest tick forged her completion.
        # That is fixed at the source (pm_state.py apply_click(actor=...)) and the
        # actor passed here is the Access-verified e-mail, so the board now records
        # WHO closed it. Opening the route without that fix would re-create the
        # forgery this listener existed to prevent — keep them together.
        # Still refused: /patch, /api/roll, /waiting/* — none of those are answering.
        if not re.match(r"\A/api/item/[^/]+(/update)?\Z", path):
            return self._deny("POST %s is not a guest-writable route" % path)
        print("[pm-guest] answer on %s by %s" % (path, who), flush=True)
        return Handler.do_POST(self)


def _guest_shim(email):
    """Appended to the served page. The server is the control; this only stops
    the guest being shown buttons that would 403 at him."""
    return """
<style id="pm-guest-style">
  #pm-guest-bar{position:sticky;top:0;z-index:99999;background:#2d5f52;color:#fff;
    font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
    padding:7px 14px;display:flex;justify-content:space-between;align-items:center}
  #pm-guest-bar b{font-weight:700}
</style>
<div id="pm-guest-bar"><span><b>Modo resposta</b> — escreva a resposta em
  "Add an update" e marque como concluido quando terminar. Fica registrado no seu
  nome, com a hora.</span><span>%s</span></div>
<script>
(function(){
  window.PM_GUEST = {email: "%s", readonly: false};
  var _f = window.fetch;
  window.fetch = function(u, o){
    var url = String(u && u.url ? u.url : u);
    var m = (o && o.method ? o.method : "GET").toUpperCase();
    /* Mirrors GuestHandler.do_POST exactly — the server is the control, this
       only avoids showing a button that would 403. TWO routes since 2026-09-18:
       the update stream and the click endpoint. Kept as ONE regex so the two
       cannot drift apart. */
    var ok = /^\/api\/item\/[^\/]+(\/update)?$/.test(url.split("?")[0]);
    if (m === "POST" && !ok) {
      return Promise.resolve(new Response(
        JSON.stringify({error:"read-only"}), {status:403,
        headers:{"Content-Type":"application/json"}}));
    }
    return _f.apply(this, arguments);
  };
})();
</script>
""" % (email, email)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    sys.stderr.write("pm_server on http://%s:%d\n" % (HOST, PORT))

    # The guest listener starts ONLY when both Access settings are present.
    # Unconfigured therefore means OFFLINE, never OPEN — there is no flag that
    # serves the board to the internet without a signature to check.
    if ACCESS_TEAM and ACCESS_AUD:
        guest = ThreadingHTTPServer((HOST, GUEST_PORT), GuestHandler)
        guest.daemon_threads = True
        threading.Thread(target=guest.serve_forever, daemon=True).start()
        sys.stderr.write("pm_server GUEST on http://%s:%d — Access team %s, aud %s…\n"
                         % (HOST, GUEST_PORT, ACCESS_TEAM, ACCESS_AUD[:8]))
    else:
        sys.stderr.write("pm_server GUEST not started "
                         "(PM_ACCESS_TEAM / PM_ACCESS_AUD unset)\n")
    sys.stderr.flush()
    srv.serve_forever()


if __name__ == "__main__":
    main()
