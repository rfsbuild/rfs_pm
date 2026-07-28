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
    POST /api/item/<id>       persist one item's click-state
    POST /api/item/<id>/patch update source-owned fields (subject, action, due…)
    POST /api/items           create a new item she typed herself
    POST /api/roll            roll the day forward
    GET  /healthz             liveness for launchd / curl
"""
import json
import os
import sys
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
}


def to_ui(state):
    items, clicks = [], {}
    for it in state["items"]:
        items.append({k: it.get(v) for k, v in _CONTENT_MAP.items()})
        clicks[it["id"]] = {
            "done": it.get("status") == "done",
            "done_at": it.get("done_at"),
            "assignee": it.get("assignee"),
            "defer": it.get("defer"),
            "deferDays": int(it.get("defer_days") or 0),
            "note": it.get("note"),
            "project": it.get("project"),
            "followup": it.get("followup"),
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
                "stale_day": state.get("brief_date") != S._today(),
                "today": S._today(),
                "counts": S.counts(state),
                "lanes": list(S.LANES),
                "assignees": list(S.ASSIGNEES),
                "defer_reasons": S.DEFER_REASONS,
            })
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
