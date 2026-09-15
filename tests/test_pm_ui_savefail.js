#!/usr/bin/env node
/* Regression test for the 2026-09-14 SILENT TICK LOSS.
 *
 * WHAT HAPPENED: Hadassa ticked 14 cards between 15:51 and 16:47 on 2026-09-14.
 * The next morning 11 of them were open again, with her `did` text still on the
 * card. Every server-side reopen path was ruled out on 2026-09-15 — pm_routine
 * only touches the routine lane and pops `did` (these kept theirs), set_waiting
 * refuses done->open, roll_forward last ran 2026-07-29 and DELETES done cards,
 * and apply_click({done:true}) reproduces clean on a copy of the state. So the
 * write did not land; and the UI had no way to show that, because save()'s
 * .catch() set a 12px header pill and resolved, leaving `.then(()=>render())`
 * to repaint the card as ticked. She had no signal at all.
 *
 * WHAT THIS ASSERTS: a write that does not land must revert the card, withdraw
 * the tick-time question, and raise a bar that names what was lost and does not
 * auto-dismiss — and "Try again" must re-send what she wanted, not undo it.
 *
 * It reads the LIVE pm_ui.html, so it goes stale only if the anchors move,
 * in which case it fails loudly rather than silently testing nothing.
 *
 *   node tests/test_pm_ui_savefail.js      # exit 0 = pass
 */
const fs = require("fs"), path = require("path");
const UI = path.join(__dirname, "..", "pm_ui.html");
const src = fs.readFileSync(UI, "utf8");

const START = "const SAVE_FAILED=new Map();", END = "function dismiss(id,reason){";
const i = src.indexOf(START), j = src.indexOf(END);
if (i < 0 || j < 0 || j <= i) {
  console.error("🔴 ANCHORS MOVED in pm_ui.html — this test is no longer testing "
              + "the save() failure path. Fix the anchors, do not delete the test.");
  process.exit(2);
}
const block = src.slice(i, j).replace(/^const SAVE_FAILED/m, "var SAVE_FAILED")
                             .replace(/^let CONFIRMED/m, "var CONFIRMED");

// ── harness ──
let FAIL = true, rendered = 0, pill = null;
let CLICK = { c1: { done:false, status:"open", done_at:null, assignee:null,
                    defer:null, deferDays:0, note:null, project:null, due:null } };
const cs = id => CLICK[id];
let ITEMS = [{ id:"c1", subject:"Report site is public — bank figures reachable unauthenticated" }];
let ASK_DID = new Set();
function setSave(k, m) { pill = { k, m }; }
function render() { rendered++; }
function setLocalDone(id, v) { const s = cs(id);
  s.done = !!v; s.status = v ? "done" : "open"; s.done_at = v ? "LOCAL_GUESS" : null; }
const DOM = { savefail:{hidden:true}, "savefail-h":{textContent:""}, "savefail-b":{textContent:""} };
global.document = { getElementById: id => DOM[id] || null };
global.fetch = () => FAIL
  ? Promise.reject(new Error("Failed to fetch"))
  : Promise.resolve({ ok:true, json:() => Promise.resolve({ done_at:"SERVER_TS", defer_days:0 }) });

// eval the real code from pm_ui.html into this scope
eval(block);

let failures = 0;
const ok = (label, cond) => { console.log((cond ? "  ✅ " : "  ❌ ") + label); if (!cond) failures++; };

CONFIRMED = {}; Object.keys(CLICK).forEach(markConfirmed);   // what boot() does

console.log("TEST 1 — she ticks a card and the POST never lands");
ASK_DID.add("c1"); setLocalDone("c1", true);
ok("precondition: card locally reads done", cs("c1").done === true);
save("c1").then(() => {
  ok("card no longer looks done",           cs("c1").done === false);
  ok("status back to open",                 cs("c1").status === "open");
  ok("optimistic done_at cleared",          cs("c1").done_at === null);
  ok("tick-time prompt withdrawn",          !ASK_DID.has("c1"));
  ok("failure recorded",                    SAVE_FAILED.has("c1"));
  ok("bar is VISIBLE",                      DOM.savefail.hidden === false);
  ok("bar names the card",                  /Report site is public/.test(DOM["savefail-b"].textContent));
  ok("bar says it was put back",            /put back the way they were/.test(DOM["savefail-b"].textContent));
  ok("header pill shows error",             pill.k === "error");
  ok("re-rendered so she sees it",          rendered > 0);

  console.log("TEST 2 — she hits Try again and the server is back");
  FAIL = false; retryFailedSaves();
  setTimeout(() => {
    ok("tick re-applied and saved",         cs("c1").done === true && cs("c1").status === "done");
    ok("server timestamp taken, not guess", cs("c1").done_at === "SERVER_TS");
    ok("failure cleared",                   SAVE_FAILED.size === 0);
    ok("bar hidden again",                  DOM.savefail.hidden === true);
    ok("pill back to saved",                pill.k === "saved");
    ok("CONFIRMED holds the saved state",   CONFIRMED.c1.done === true);

    console.log("TEST 3 — a SECOND failure reverts to the SAVED state, not to open");
    FAIL = true; setLocalDone("c1", false);
    save("c1").then(() => {
      ok("reverts to done (the confirmed state)", cs("c1").done === true && cs("c1").status === "done");
      ok("does NOT fall back to open",            cs("c1").status !== "open");
      console.log(failures ? "\n🔴 " + failures + " FAILED" : "\n✅ ALL PASS");
      process.exit(failures ? 1 : 0);
    });
  }, 20);
});
