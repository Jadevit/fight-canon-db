// The site has no build step, so a wrong import or a broken helper would only show in a browser.
// This imports every module (a name a module doesn't export fails here) and checks what can run
// without a page. Run with: node --test tests/site/modules.test.mjs

import assert from "node:assert/strict";
import { readdirSync } from "node:fs";
import { test } from "node:test";

const JS = new URL("../../site/js/", import.meta.url);
const load = (name) => import(new URL(`${name}.js`, JS));

test("every module links", async () => {
  const names = readdirSync(JS).filter((f) => f.endsWith(".js")).map((f) => f.slice(0, -3));
  assert.ok(names.includes("router"));
  for (const name of names) await load(name);
});

test("shard agrees with site_json.py (test_site.py checks the same values)", async () => {
  const { shard } = await load("data");
  assert.deepEqual([1, 42, 373731, 999999].map(shard), ["1c", "83", "fd", "0b"]);
});

test("objs turns a file's columns and rows into objects", async () => {
  const { objs } = await load("data");
  assert.deepEqual(objs({ cols: ["a", "b"], rows: [[1, 2]] }), [{ a: 1, b: 2 }]);
  assert.deepEqual(objs(null), []);
});

test("search matches the start of different words", async () => {
  const { searchWords, wordsMatch } = await load("search");
  assert.equal(searchWords("a"), null);
  assert.deepEqual(searchWords("José Al"), { words: ["jose", "al"], first: "jose" });
  assert.ok(wordsMatch(["jon", "jon"], ["jon", "jones"]));
  assert.ok(!wordsMatch(["jon", "jones"], ["edwynn", "jones"]));
});

test("formats", async () => {
  const { esc, mmss, hmm, pct, plural, longDate, monthYear, american, pagePath } = await load("ui");
  assert.equal(esc(`<a href="x">'&`), "&lt;a href=&quot;x&quot;&gt;&#39;&amp;");
  assert.equal(esc(null), "");
  assert.equal(mmss(65), "1:05");
  assert.equal(mmss(null), "—");
  assert.equal(hmm(3660), "1h 1m");
  assert.equal(pct(1, 3), "33%");
  assert.equal(pct(1, 0), null);
  assert.equal(plural(1, "fight"), "1 fight");
  assert.equal(plural(2, "loss", "losses"), "2 losses");
  assert.equal(longDate("2024-11-06"), "6 Nov 2024");
  assert.equal(monthYear("2024-11-06"), "Nov 2024");
  assert.equal(american(150), "+150");
  assert.equal(american(-150), "-150");
  assert.equal(pagePath("fighter", 7, "José O'Neil Jr."), "/fighter/7-jose-o-neil-jr");
  assert.equal(pagePath("event", 7, ""), "/event/7");
});

test("a record says its no contests", async () => {
  const { proRecord } = await load("ui");
  assert.equal(proRecord({ w: 9 }), "9-0-0");
  assert.equal(proRecord({ w: 3, l: 1, d: 1, nc: 2 }), "3-1-1 (2 NC)");
});

test("an older call's result and failure are both dropped", async () => {
  const { latest } = await load("ui");
  const waits = [];
  const fn = latest(() => new Promise((res, rej) => waits.push({ res, rej })));
  const first = fn(), second = fn();
  waits[0].rej(new Error("old"));
  waits[1].res("new");
  assert.equal(await first, undefined);
  assert.equal(await second, "new");
  const only = fn();
  waits[2].rej(new Error("now"));
  await assert.rejects(only, /now/);
});

test("years for a chip, newest first", async () => {
  const { yearOptions } = await load("ui");
  assert.deepEqual(yearOptions({ first_date: "2023-05-01", last_date: "2025-01-01" }), [["2025", "2025"], ["2024", "2024"], ["2023", "2023"]]);
});

test("how a win came, in either source's words", async () => {
  const { winKind } = await load("fighters");
  assert.equal(winKind("KO/TKO"), "ko");
  assert.equal(winKind("TKO (Punches)"), "ko");
  assert.equal(winKind("submission (armbar)"), "sub");
  assert.equal(winKind("Decision - Split"), "dec");
  assert.equal(winKind("Could Not Continue"), "other");
  assert.equal(winKind(null), "other");
});

test("seconds fought, from the format, round and time", async () => {
  const { fightSeconds } = await load("fighters");
  assert.equal(fightSeconds("3 Rnd (5-5-5)", 1, "2:30"), 150);
  assert.equal(fightSeconds("3 Rnd (5-5-5)", 3, "1:00"), 660);
  assert.equal(fightSeconds("2 Rnd (10-5)", 2, "1:00"), 660);
  assert.equal(fightSeconds(null, 2, "1:00"), 360);  // no format on record: 5-minute rounds
  assert.equal(fightSeconds("No Time Limit", 2, "1:00"), null);
  assert.equal(fightSeconds("3 Rnd (5-5-5)", null, null), null);
});

test("a query's numbers come back as Numbers", async () => {
  const { plain } = await load("duck");
  assert.equal(plain(5n), 5);
  assert.equal(plain({ toString: () => "12.5" }), 12.5);
  assert.equal(plain("text"), "text");
  assert.equal(plain(null), null);
});

test("the Query page's headers and values", async () => {
  const { label, kindOf, asDelimited } = await load("query");
  assert.equal(label("fighter_id"), "Fighter ID");
  assert.equal(label("finish_pct"), "Finish %");
  assert.equal(label("Control Time"), "Control Time");
  assert.equal(kindOf("ctrl_sec", [90]), "time");
  assert.equal(kindOf("date", ["2024-01-02"]), "date");
  assert.equal(kindOf("name", ["x"]), "text");
  assert.equal(kindOf("wins", [1, null]), "num");
  const cols = [{ i: 0, label: "Name", kind: "text" }, { i: 1, label: "Wins", kind: "num" }];
  assert.equal(asDelimited(cols, [['Say "hi", ok', 1234], ["x", null]], ","), 'Name,Wins\n"Say ""hi"", ok",1234\nx,');
});
