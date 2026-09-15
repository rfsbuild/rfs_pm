#!/usr/bin/env node
/* The Triage view, tested against the REAL payload the server is serving.
 *
 * It renders auditBlock() for every banded card and asserts that each band asks
 * the right question with the right evidence — because the failure mode here is
 * not a crash, it is a card quietly showing the wrong question ("close it?" on a
 * card whose finding is that her own note was contradicted) or an empty box.
 *
 *   node tests/test_triage_render.js          # reads http://127.0.0.1:8789
 *   node tests/test_triage_render.js file.json
 */
const fs = require("fs"), path = require("path"), cp = require("child_process");
const UI = path.join(__dirname, "..", "pm_ui.html");
const src = fs.readFileSync(UI, "utf8");

function slice(from, to) {
  const i = src.indexOf(from), j = src.indexOf(to);
  if (i < 0 || j < 0 || j <= i) {
    console.error("🔴 ANCHORS MOVED in pm_ui.html (" + from.slice(0, 40) + ") — fix them, do not delete the test.");
    process.exit(2);
  }
  // `const` inside eval() stays in the eval's own scope — rewrite the top-level
  // declarations to `var` so the extracted functions are callable from here.
  return src.slice(i, j).replace(/^const /gm, "var ");
}

const payload = process.argv[2]
  ? JSON.parse(fs.readFileSync(process.argv[2], "utf8"))
  : JSON.parse(cp.execSync("curl -s http://127.0.0.1:8789/api/state",
      { maxBuffer: 64 * 1024 * 1024 }).toString());   /* the payload is ~1.1 MB */

let ITEMS = payload.items || [], CLICK = payload.state || {};
let VIEW = "triage";
const esc = s => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
  .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
eval(slice("const auditOf =id=>", "const byId=id=>"));          // the band helpers
eval(slice("function auditBlock(it){", "/* Her ruling. "));   // auditBlock()

const cs = id => CLICK[id] || {};
let fails = 0;
const ok = (l, c) => { console.log((c ? "  ✅ " : "  ❌ ") + l); if (!c) fails++; };

const banded = ITEMS.filter(i => auditBand(i.id));
const byBand = {};
banded.forEach(i => { const b = auditBand(i.id); (byBand[b] = byBand[b] || []).push(i); });

// A finished triage is a legitimate state, not a failure: once she has ruled on
// every card, auditBand() returns null for all of them and there is nothing to
// render. The test said so by crashing on banded[0] the first time it happened
// (2026-09-15, minutes after she finished). Skip cleanly and say why.
if (!banded.length) {
  const ruled = ITEMS.filter(i => auditOf(i.id) && ruledOf(i.id)).length;
  console.log("Triage is empty — " + ruled + " card(s) carry a ruling, so there is "
            + "nothing left to render. Nothing to assert; this is the finished state.");
  console.log("\n✅ SKIPPED (triage complete)");
  process.exit(0);
}
console.log("Triage holds " + banded.length + " cards across " + Object.keys(byBand).length + " bands");
BANDS.forEach(b => console.log("   " + b.padEnd(14) + " " + (byBand[b] || []).length));
console.log();

const EXPECT = {
  "CONTRADICTED": { head: /note is contradicted/i, must: [/You wrote: “/, /→ /], btn: ["kept", "closed"] },
  "LOST TICK":    { head: /tick did not survive/i, must: [/You wrote: “/],            btn: ["closed", "kept"] },
  "NOISE":        { head: /needs an action from anyone/i, must: [],                   btn: ["dismissed", "kept"] },
  "LIKELY DONE":  { head: /already done/i,         must: [],                          btn: ["closed", "kept"] },
  "CANNOT TELL":  { head: /could not tell/i,       must: [],                          btn: ["kept", "closed", "dismissed"] },
};

BANDS.forEach(b => {
  const arr = byBand[b] || [];
  if (!arr.length) { ok(b + ": has cards", false); return; }
  console.log(b + " (" + arr.length + ")");
  let headOK = 0, evOK = 0, btnOK = 0, emptyBody = [];
  arr.forEach(it => {
    const h = auditBlock(it);
    if (EXPECT[b].head.test(h)) headOK++;
    if (EXPECT[b].must.every(r => r.test(h))) evOK++;
    if (EXPECT[b].btn.every(v => h.indexOf('data-rv="' + v + '"') >= 0)) btnOK++;
    // the box must carry real evidence, not an empty shell
    const text = h.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
    if (text.length < 80) emptyBody.push(it.id);
  });
  ok("  asks this band's question on all " + arr.length,       headOK === arr.length);
  ok("  carries the band's required evidence",                 evOK === arr.length);
  ok("  offers the right buttons (" + EXPECT[b].btn.join("/") + ")", btnOK === arr.length);
  ok("  every box carries real content",                       emptyBody.length === 0);
  if (emptyBody.length) console.log("       thin: " + emptyBody.join(", "));
});

console.log("\nCross-band invariants");
const seen = {};
banded.forEach(i => { seen[i.id] = (seen[i.id] || 0) + 1; });
ok("no card appears in two bands", Object.values(seen).every(n => n === 1));
ok("every contradicted card quotes HER, from her own card",
   (byBand["CONTRADICTED"] || []).every(i => {
     const a = auditOf(i.id); return a.contra && a.contra.quoted_from === "card"; }));
ok("nothing is rendered outside Triage",
   (() => { VIEW = "today"; const h = auditBlock(banded[0]); VIEW = "triage"; return h === ""; })());
ok("a ruled card leaves Triage",
   (() => { const id = banded[0].id; CLICK[id].auditRuled = "kept";
            const gone = !auditBand(id); CLICK[id].auditRuled = null; return gone; })());

console.log(fails ? "\n🔴 " + fails + " FAILED" : "\n✅ ALL PASS");
process.exit(fails ? 1 : 0);
