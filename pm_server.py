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
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_state as S  # noqa: E402

HOST, PORT = "127.0.0.1", 8789
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
        }
    return items, clicks


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):     # quiet; launchd captures stderr
        pass

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

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ── routes ──
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
            items, clicks = to_ui(state)
            return self._send(200, {
                "items": items, "state": clicks,
                "brief_date": state.get("brief_date"),
                "updated_at": state.get("updated_at"),
                # Sweep provenance. `updated_at` moves on any click of hers, so it
                # answers "when was this file last written", NOT "how current is
                # the information" — which is the only question the header asks.
                "last_swept_at": state.get("last_swept_at"),
                "last_swept_sources": state.get("last_swept_sources") or [],
                "last_swept_evidence": state.get("last_swept_evidence") or {},
                "stale_day": state.get("brief_date") != S._today(),
                "today": S._today(),
                "counts": S.counts(state),
                "claude_queue": len(S.claude_queue(state)),
                "lanes": list(S.LANES),
                "assignees": list(S.ASSIGNEES),
                "defer_reasons": S.DEFER_REASONS,
            })
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
                                   set_did=bool(body.get("set_did")))
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
            res = S.apply_click(iid, body)
            if res is None:
                return self._send(404, {"error": "no such item"})
            return self._send(200, res)

        return self._send(404, {"error": "not found"})


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    sys.stderr.write("pm_server on http://%s:%d\n" % (HOST, PORT))
    sys.stderr.flush()
    srv.serve_forever()


if __name__ == "__main__":
    main()
