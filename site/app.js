// Canon DB browser: reads canon.db with sql.js-httpvfs, which fetches only the pages of the
// database a query touches, one request at a time. The site's copy (scripts/site_db.py) is
// laid out so each view reads a few contiguous ranges: site_fights holds each fight with its
// event and both fighters, and the `site_` tables hold what the lists show.
// Views are chosen by the URL hash: #/fighters, #/fighter/<id>, #/events, #/event/<id>,
// #/fight/<id>, #/leaders, #/sql.

const VFS = "vendor/sql.js-httpvfs/";
const DB_DIR = "db/";
const RELEASES = "https://github.com/jadevit/fight-canon-db/releases/latest";

const app = document.getElementById("app");
let worker;

// --- helpers -------------------------------------------------------------------------

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const q = (sql, params = []) => worker.db.query(sql, params);

const dash = (v) => (v == null ? "—" : v);
const of = (l, a) => (l == null ? (a == null ? "—" : `${a} thrown`) : a == null ? `${l}` : `${l} of ${a}`);
const pct = (l, a) => (l == null || !a ? "—" : Math.round((100 * l) / a) + "%");
const mmss = (s) => (s == null ? "—" : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`);
const hmm = (s) => (s == null ? "—" : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`);
const num = (n) => (n == null ? "—" : Number(n).toLocaleString());
const mb = (bytes) => `${(bytes / 1e6).toFixed(bytes < 1e7 ? 1 : 0)} MB`;
const badge = (r) => (r ? `<span class="res res-${esc(r)}">${esc(r)}</span>` : "");
const fighterLink = (id, name) => `<a href="#/fighter/${esc(id)}">${esc(name)}</a>`;
const eventLink = (id, name) => `<a href="#/event/${esc(id)}">${esc(name)}</a>`;
const fightLink = (id, text) => `<a href="#/fight/${esc(id)}">${esc(text)}</a>`;
const table = (head, rows) =>
  `<div class="scroll"><table><thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead>` +
  `<tbody>${rows.join("") || `<tr><td colspan="${head.length}" class="muted">Nothing found.</td></tr>`}</tbody></table></div>`;
const record = (r) => `${r.w || 0}-${r.l || 0}-${r.d || 0}` + (r.nc ? ` (${r.nc} NC)` : "");
// Pro MMA record from the fighter's UFC Stats page; falls back to the fights in the DB.
const proRecord = (f, fallback) => f.wins != null
  ? record({ w: f.wins, l: f.losses, d: f.draws, nc: f.no_contests }) : record(fallback);
const tag = (text) => `<span class="tag">${esc(text)}</span>`;
const boutTags = (x) => (x.title_fight ? tag("Title") : "");

// Links to the pages a row came from. Only UFC Stats ids (16 hex characters) exist on UFC
// Stats; everything else is on Sherdog, under the id in ours or in the aliases tables.
const isUfcStats = (id) => /^[0-9a-f]{16}$/.test(id);
const ext = (url, label) => `<a href="${esc(url)}" target="_blank" rel="noopener">${label}</a>`;
const sourceLine = (links) => links.length ? `<p><small>Source: ${links.join(" · ")}</small></p>` : "";
const SOURCE_NAMES = { ufcstats: "UFC Stats", pfl: "PFL", oktagon: "Oktagon", sofascore: "Sofascore", sherdog: "Sherdog" };
// What a stats source leaves out, said under a fight's stats.
const STATS_NOTES = {
  oktagon: "Only what landed is counted: no attempts, knockdowns or control time.",
  sofascore: "A third party's count, not the promotion's. Total strikes are only counted thrown, not landed.",
};
async function eventSources(id) {
  const links = isUfcStats(id) ? [ext(`http://ufcstats.com/event-details/${id}`, "UFC Stats")] : [];
  const aliases = await q("SELECT source, source_id FROM event_aliases WHERE event_id = ? AND source IN ('sherdog', 'pfl')", [id]);
  const sherdog = id.startsWith("sherdog:") ? [id.slice(8)]
    : aliases.filter((r) => r.source === "sherdog").map((r) => r.source_id);
  sherdog.forEach((s) => links.push(ext(`https://www.sherdog.com/events/x-${s}`, "Sherdog")));
  aliases.filter((r) => r.source === "pfl")
    .forEach((r) => links.push(ext(`https://pflmma.com/event/${r.source_id}`, "PFL")));
  return links;
}

function debounce(fn, ms = 150) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// Only the latest call's result is used: queries are async, and an older one can finish last.
function latest(fn) {
  let n = 0;
  return async (...a) => {
    const mine = ++n;
    const out = await fn(...a);
    return mine === n ? out : undefined;
  };
}

// Query-string state inside the hash (#/leaders?stat=kd), so filters survive back/refresh.
function params() { return new URLSearchParams(location.hash.split("?")[1] || ""); }
function setParams(p) {
  const base = location.hash.split("?")[0];
  history.replaceState(null, "", `${base}?${p.toString()}`);
}

// Promotions with enough events to list (there are thousands of one-off shows).
const promotions = () => q("SELECT code, promotion FROM site_promotions WHERE listed ORDER BY events DESC");

// Weight classes are stored as the bout title ("UFC Lightweight Title Bout");
// division() reduces them to the division name. Sherdog doesn't say which bouts are women's.
// Keep in step with scripts/site_db.py.
const DIVISIONS = ["Women's Strawweight", "Women's Flyweight", "Women's Bantamweight",
  "Women's Featherweight", "Atomweight", "Strawweight", "Flyweight", "Bantamweight", "Featherweight",
  "Lightweight", "Welterweight", "Middleweight", "Light Heavyweight", "Super Heavyweight", "Heavyweight",
  "Catch Weight", "Open Weight"];
function division(wc) {
  if (!wc) return "Other";
  const s = wc.replace(/Catchweight/i, "Catch Weight");
  return DIVISIONS.find((d) => s.includes(d)) || "Other";
}
const norm = (s) => (s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

// The words of a search term, normalized like the search columns, longest first (fighter
// search looks it up as a prefix of a word of the name); null if none has two letters.
function searchWords(term) {
  const words = (norm(term).match(/[\p{L}\p{N}_]+/gu) || []).sort((a, b) => b.length - a.length);
  if (!words.length || words[0].length < 2) return null;
  const first = words[0];
  const next = first.slice(0, -1) + String.fromCodePoint(first.codePointAt(first.length - 1) + 1);
  return { words, first, next, rest: words.slice(1) };
}

// Seconds actually fought, from "3 Rnd (5-5-5)" + end round + end time.
function fightSeconds(timeFormat, endRound, endTime) {
  if (!endRound || !endTime) return null;
  const [m, s] = endTime.split(":").map(Number);
  const last = m * 60 + s;
  if (endRound === 1) return last;
  const lens = ((timeFormat || "").match(/\(([\d-]+)\)/) || [])[1];
  if (!lens) return timeFormat ? null : (endRound - 1) * 300 + last;  // no format on record: 5-minute rounds
  const mins = lens.split("-").map(Number);
  if (mins.length < endRound - 1) return null;
  return mins.slice(0, endRound - 1).reduce((a, b) => a + b, 0) * 60 + last;
}

// --- loading -------------------------------------------------------------------------

(async function start() {
  try {
    const r = await fetch(DB_DIR + "config.json", { cache: "no-cache" });
    if (!r.ok) throw new Error(`${DB_DIR}config.json: HTTP ${r.status}`);
    const config = await r.json();
    config.urlPrefix = new URL(config.urlPrefix, new URL(DB_DIR, location.href)).href;
    const abs = (f) => new URL(VFS + f, location.href).href;
    worker = await createDbWorker([{ from: "inline", config }], abs("sqlite.worker.js"), abs("sql-wasm.wasm"));
    const [last] = await q("SELECT name, date FROM site_events LIMIT 1");
    document.getElementById("footer").innerHTML =
      `Data from <a href="http://ufcstats.com">UFC Stats</a>, <a href="https://www.sherdog.com">Sherdog</a>, ` +
      `<a href="https://pflmma.com">PFL</a>, <a href="https://oktagonmma.com">Oktagon</a>, ` +
      `<a href="https://www.sofascore.com">Sofascore</a> and ` +
      `<a href="https://www.bestfightodds.com">BestFightOdds</a>, updated daily. ` +
      `Latest event: ${esc(last.name)} (${esc(last.date)}). ` +
      `<a href="https://github.com/jadevit/fight-canon-db">About</a> · <a href="${RELEASES}">Download</a>.`;
    window.addEventListener("hashchange", route);
    route();
  } catch (e) {
    app.innerHTML = `<p class="error">Couldn't load the database: ${esc(e.message)}</p>`;
  }
})();

// --- routing -------------------------------------------------------------------------

let page = 0;  // the page being shown; a view drops its result if another page was opened since

async function route() {
  const path = (location.hash.slice(1) || "/").split("?")[0];
  const [name = "", id = ""] = path.split("/").filter(Boolean);
  const views = { "": home, fighters, fighter, events, event, fight, leaders, sql };
  const mine = ++page;
  const show = (html) => { if (mine !== page) return false; app.innerHTML = html; return true; };
  try {
    await (views[name] || home)(decodeURIComponent(id), show);
  } catch (e) {
    show(`<p class="error">${esc(e.message)}</p>`);
  }
  window.scrollTo(0, 0);
}

// --- views ---------------------------------------------------------------------------
// Each view gets the id from the URL and show(html), which draws the page unless another one
// was opened meanwhile (it returns false then, and the view stops).

async function home(_, show) {
  const [[c], recent] = await Promise.all([
    q("SELECT * FROM site_stats"),
    q("SELECT event_id, name, date, location FROM site_events LIMIT 10"),
  ]);
  if (!show(`
    <h1>MMA fights, round by round</h1>
    <p>${num(c.events)} events, ${num(c.fights)} fights and ${num(c.fighters)} fighters since ${esc(c.first_date)}: the UFC and PRIDE
       round by round, and results from thousands of other promotions.
       Search a fighter, open any card, or rank fighters by their stats.</p>
    <form id="s"><input type="search" name="q" placeholder="Search fighters…" autofocus></form>
    <h2>Latest events</h2>
    ${table(["Date", "Event", "Location"], recent.map((e) =>
      `<tr><td>${esc(e.date)}</td><td>${eventLink(e.event_id, e.name)}</td><td>${esc(e.location)}</td></tr>`))}
    <p><a href="#/events">All events →</a></p>`)) return;
  document.getElementById("s").onsubmit = (ev) => {
    ev.preventDefault();
    location.hash = `#/fighters?q=${encodeURIComponent(ev.target.q.value)}`;
  };
}

const CARD_COLS = "fighter_id, name, nickname, wins, losses, draws, no_contests, n, w, l, d, nc";

async function fighters(_, show) {
  const p = params();
  if (!show(`
    <h1>Fighters</h1>
    <input type="search" id="q" placeholder="Search by name…" value="${esc(p.get("q") || "")}" autofocus>
    <div id="results"></div>`)) return;
  const input = document.getElementById("q");
  // site_names has a row per word of each name, with what the list shows.
  const find = latest(async (term) => {
    if (!term) return q(`SELECT ${CARD_COLS} FROM site_top_fighters ORDER BY n DESC, name`);
    const s = searchWords(term);
    if (!s) return null;
    return q(`SELECT ${CARD_COLS} FROM site_names
              WHERE token >= ? AND token < ? ${s.rest.map(() => "AND instr(search, ?) > 0").join(" ")}
              GROUP BY fighter_id ORDER BY n DESC, name LIMIT 50`, [s.first, s.next, ...s.rest]);
  });
  const render = async () => {
    const term = input.value.trim();
    setParams(new URLSearchParams(term ? { q: term } : {}));
    const rows = await find(term);
    if (rows === undefined) return;  // a newer search is on its way
    document.getElementById("results").innerHTML = rows === null
      ? `<p class="muted">Type at least two letters of a name.</p>`
      : (term ? "" : `<p class="muted">Most fights in the database:</p>`) +
        table(["Fighter", "Record", "Fights here"], rows.map((r) =>
          `<tr><td>${fighterLink(r.fighter_id, r.name)}${r.nickname ? ` <span class="muted">“${esc(r.nickname)}”</span>` : ""}</td>
               <td>${proRecord(r, r)}</td><td class="num">${r.n}</td></tr>`));
  };
  input.oninput = debounce(render, 250);
  await render();
}

// A fighter's fights, from their side of site_fights.
const MY_FIGHTS = `SELECT date, event_id, event, promotion, fight_id, weight_class, method, end_round, end_time,
  time_format, title_fight, %me_res AS result, %opp_id AS opp_id, %opp_name AS opp FROM site_fights WHERE %me_id = ?1`;
const side = (me, opp) => MY_FIGHTS.replace("%me_res", `${me}_res`).replace("%me_id", `${me}_id`)
  .replace("%opp_id", `${opp}_id`).replace("%opp_name", `${opp}_name`);

async function fighter(id, show) {
  const [[f], fights, own, sherdog] = await Promise.all([
    q("SELECT * FROM fighters WHERE fighter_id = ?", [id]),
    q(`${side("a", "b")} UNION ALL ${side("b", "a")} ORDER BY date DESC`, [id]),
    // Some sources count only what landed, so accuracy uses the rounds that have attempts.
    q(`SELECT fight_id, SUM(knockdowns) kd, SUM(sig_str_land) sl, SUM(sig_str_att) sa,
              SUM(CASE WHEN sig_str_att IS NOT NULL THEN sig_str_land END) sla,
              SUM(td_land) tl, SUM(td_att) ta, SUM(CASE WHEN td_att IS NOT NULL THEN td_land END) tla,
              SUM(sub_att) sub, SUM(ctrl_sec) ctrl
       FROM round_stats WHERE fighter_id = ? GROUP BY fight_id`, [id]),
    q("SELECT DISTINCT source_id FROM fighter_aliases WHERE fighter_id = ? AND source = 'sherdog'", [id]),
  ]);
  if (!f) { show("<p>Fighter not found.</p>"); return; }
  // Their opponents' strikes in the same fights, for strike defense.
  const [opp] = own.length
    ? await q(`SELECT SUM(sig_str_land) sl, SUM(sig_str_att) sa FROM round_stats
               WHERE fight_id IN (${own.map(() => "?").join(",")}) AND fighter_id <> ? AND sig_str_att IS NOT NULL`,
              [...own.map((r) => r.fight_id), id])
    : [{}];

  const rec = { w: 0, l: 0, d: 0, nc: 0 };
  fights.forEach((x) => { if (x.result) rec[x.result.toLowerCase()]++; });
  const t = { kd: 0, sl: 0, sa: 0, sla: 0, tl: 0, ta: 0, tla: 0, sub: 0, ctrl: 0 };
  const has = { kd: false, ctrl: false };
  let secs = 0, slTimed = 0;
  const byFight = Object.fromEntries(fights.map((x) => [x.fight_id, x]));
  for (const r of own) {
    for (const k in t) t[k] += r[k] || 0;
    for (const k in has) has[k] = has[k] || r[k] != null;
    const x = byFight[r.fight_id];
    const s = x && fightSeconds(x.time_format, x.end_round, x.end_time);
    if (s && r.sl != null) { secs += s; slTimed += r.sl; }
  }
  const age = f.dob ? Math.floor((Date.now() - new Date(f.dob)) / 3.15576e10) : null;
  const bio = [
    f.nickname && `“${esc(f.nickname)}”`,
    age != null && `Age ${age}`,
    f.height_in && `${Math.floor(f.height_in / 12)}′${f.height_in % 12}″`,
    f.reach_in && `${f.reach_in}″ reach`,
    f.weight_lbs && `${f.weight_lbs} lbs`,
    f.stance && esc(f.stance),
    f.nationality && esc(f.nationality),
  ].filter(Boolean).join(" · ");
  const links = (isUfcStats(id) ? [ext(`http://ufcstats.com/fighter-details/${id}`, "UFC Stats")] : [])
    .concat(sherdog.map((r) => ext(`https://www.sherdog.com/fighter/x-${r.source_id}`, "Sherdog")));
  const stat = (label, value) => `<div class="stat"><small class="muted">${label}</small><b>${value}</b></div>`;

  show(`
    <h1>${esc(f.name)}</h1>
    <p>${bio}</p>
    <div class="grid2">
      ${stat(f.wins != null ? "Pro record" : "Pro record (fights here)", proRecord(f, rec))}
      ${own.length ? `
      ${stat("Sig. strikes / min", secs ? (slTimed / (secs / 60)).toFixed(2) : "—")}
      ${stat("Sig. strike accuracy", pct(t.sla, t.sa))}
      ${stat("Sig. strike defense", opp.sa ? pct(opp.sa - opp.sl, opp.sa) : "—")}
      ${stat("Takedowns", t.ta ? `${t.tl} (${pct(t.tla, t.ta)})` : t.tl)}
      ${stat("Knockdowns", has.kd ? t.kd : "—")}
      ${stat("Sub. attempts", t.sub)}
      ${stat("Control time", has.ctrl ? hmm(t.ctrl) : "—")}` : ""}
    </div>
    ${own.length ? `<p class="muted"><small>Stats are from the ${own.length} of ${fights.length} fights that have round-by-round stats.</small></p>` : ""}
    <h2>Fights</h2>
    ${table(["", "Opponent", "Method", "Rd", "Time", "Event", "Promotion", "Date"], fights.map((x) => `
      <tr><td>${badge(x.result)}</td><td>${fighterLink(x.opp_id, x.opp)} ${boutTags(x)}</td>
          <td>${fightLink(x.fight_id, x.method || "—")}</td><td>${dash(x.end_round)}</td>
          <td>${dash(x.end_time)}</td><td>${eventLink(x.event_id, x.event)}</td>
          <td>${esc(x.promotion)}</td><td>${esc(x.date)}</td></tr>`))}
    ${sourceLine(links)}`);
}

const EVENTS_SHOWN = 300;
const SEARCH_SHOWN = 100;  // a search's matches are scattered over the years: fewer, so fewer lookups

async function events(_, show) {
  const [[c], promos] = await Promise.all([q("SELECT * FROM site_stats"), promotions()]);
  const years = [];
  for (let y = Number(c.last_date.slice(0, 4)); y >= Number(c.first_date.slice(0, 4)); y--) years.push(String(y));
  const p = params();
  if (!show(`
    <h1>Events</h1>
    <div class="filters">
      <input type="search" id="q" placeholder="Search events or places (whole words)…" value="${esc(p.get("q") || "")}">
      <select id="y"><option value="">All years</option>${years.map((y) =>
        `<option ${p.get("y") === y ? "selected" : ""}>${y}</option>`).join("")}</select>
      <select id="pr"><option value="">All promotions</option>${promos.map((x) =>
        `<option ${p.get("pr") === x.promotion ? "selected" : ""}>${esc(x.promotion)}</option>`).join("")}</select>
    </div>
    <div id="results"></div>`)) return;
  const qi = document.getElementById("q"), yi = document.getElementById("y"), pi = document.getElementById("pr");
  // site_events is newest first: a year is a range of it, a promotion a range of its index.
  // A search matches whole words: it walks the events of its rarest word, newest first, and
  // keeps those that have the other words too.
  const find = latest(async (term, year, promo) => {
    const s = term ? searchWords(term) : null;
    if (term && !s) return null;
    const where = [], args = [];
    const col = (c) => (s ? `w.${c}` : c);
    if (year) { where.push(`${col("date")} >= ? AND ${col("date")} < ?`); args.push(year, String(Number(year) + 1)); }
    if (promo) { where.push(`${s ? "e." : ""}promotion = ?`); args.push(promo); }
    if (!s) {
      const clause = where.length ? "WHERE " + where.join(" AND ") : "";
      const rows = q(`SELECT event_id, name, date, location, promotion, n FROM site_events ${clause}
                      ORDER BY date DESC LIMIT ${EVENTS_SHOWN + 1}`, args);
      const count = where.length ? q(`SELECT COUNT(*) n FROM site_events ${clause}`, args).then((r) => r[0].n) : c.events;
      return [await rows, await count, EVENTS_SHOWN];
    }
    const terms = [...new Set(s.words)];
    const counts = Object.fromEntries((await q(
      `SELECT token, COUNT(*) n FROM site_event_words WHERE token IN (${terms.map(() => "?").join(",")}) GROUP BY token`,
      terms)).map((r) => [r.token, r.n]));
    if (terms.some((t) => !counts[t])) return [[], 0, SEARCH_SHOWN];
    const [rarest, ...others] = terms.sort((a, b) => counts[a] - counts[b]);
    others.forEach((t) => {
      where.push("EXISTS (SELECT 1 FROM site_event_words o WHERE o.token = ? AND o.date = w.date AND o.event_id = w.event_id)");
      args.push(t);
    });
    const rows = await q(`
      SELECT e.event_id, e.name, e.date, e.location, e.promotion, e.n
      FROM site_event_words w JOIN site_events e ON e.date = w.date AND e.event_id = w.event_id
      WHERE w.token = ? ${where.map((x) => `AND ${x}`).join(" ")}
      ORDER BY w.date DESC LIMIT ${SEARCH_SHOWN + 1}`, [rarest, ...args]);
    return [rows, null, SEARCH_SHOWN];
  });
  const render = async () => {
    const term = qi.value.trim(), year = yi.value, promo = pi.value;
    setParams(new URLSearchParams({ ...(term && { q: term }), ...(year && { y: year }), ...(promo && { pr: promo }) }));
    const got = await find(term, year, promo);
    if (got === undefined) return;
    if (got === null) { document.getElementById("results").innerHTML = `<p class="muted">Type at least two letters.</p>`; return; }
    const [rows, count, limit] = got;
    const total = count ?? (rows.length > limit ? null : rows.length);
    document.getElementById("results").innerHTML =
      `<p class="muted">${total != null ? `${num(total)} events` : `More than ${limit} events`}${
        rows.length > limit ? ` (showing the latest ${limit})` : ""}</p>` +
      table(["Date", "Event", "Promotion", "Location", "Fights"], rows.slice(0, limit).map((e) =>
        `<tr><td>${esc(e.date)}</td><td>${eventLink(e.event_id, e.name)}</td><td>${esc(e.promotion)}</td>
             <td>${esc(e.location)}</td><td class="num">${e.n}</td></tr>`));
  };
  qi.oninput = debounce(render, 250);
  yi.onchange = render;
  pi.onchange = render;
  await render();
}

async function event(id, show) {
  const [[e], fights, [card], links] = await Promise.all([
    q("SELECT e.*, p.coverage FROM events e LEFT JOIN promotions p USING (promotion) WHERE event_id = ?", [id]),
    q("SELECT * FROM site_fights WHERE event_id = ? ORDER BY card", [id]),
    q("SELECT complete FROM event_cards WHERE event_id = ?", [id]),
    eventSources(id),
  ]);
  if (!e) { show("<p>Event not found.</p>"); return; }
  // The whole card is here if UFC Stats covers the promotion's cards or the card was read whole.
  const whole = (e.coverage === "full" && e.source === "ufcstats") || card?.complete === 1;
  show(`
    <h1>${esc(e.name)}</h1>
    <p>${[e.promotion, e.date, e.location].filter(Boolean).map(esc).join(" · ")}</p>
    ${whole ? "" : `<p class="muted"><small>Partial card: only the bouts of fighters this database follows.</small></p>`}
    ${table(["Fighters", "Weight class", "Method", "Rd", "Time", ""], fights.map((x) => `
      <tr><td>${badge(x.a_res)} ${fighterLink(x.a_id, x.a_name)}<br>${badge(x.b_res)} ${fighterLink(x.b_id, x.b_name)}</td>
          <td>${x.weight_class ? esc(x.division) : "—"} ${boutTags(x)}</td><td>${esc(x.method || "—")}</td>
          <td>${dash(x.end_round)}</td><td>${dash(x.end_time)}</td>
          <td>${fightLink(x.fight_id, x.has_stats ? "Stats →" : "Details →")}</td></tr>`))}
    ${sourceLine(links)}`);
}

const ROUND_ROWS = [
  ["Knockdowns", (r) => dash(r.knockdowns)],
  ["Sig. strikes", (r) => of(r.sig_str_land, r.sig_str_att)],
  ["Sig. strike %", (r) => pct(r.sig_str_land, r.sig_str_att)],
  ["Total strikes", (r) => of(r.total_str_land, r.total_str_att)],
  ["Takedowns", (r) => of(r.td_land, r.td_att)],
  ["Takedown %", (r) => pct(r.td_land, r.td_att)],
  ["Sub. attempts", (r) => dash(r.sub_att)],
  ["Reversals", (r) => dash(r.reversals)],
  ["Control time", (r) => mmss(r.ctrl_sec)],
  ["Head", (r) => of(r.head_land, r.head_att)],
  ["Body", (r) => of(r.body_land, r.body_att)],
  ["Leg", (r) => of(r.leg_land, r.leg_att)],
  ["Distance", (r) => of(r.dist_land, r.dist_att)],
  ["Clinch", (r) => of(r.clinch_land, r.clinch_att)],
  ["Ground", (r) => of(r.ground_land, r.ground_att)],
];

const SMARTCAGE_ROWS = [
  ["Knockdowns", (r) => dash(r.knockdowns)],
  ["Total strikes", (r) => of(r.total_str_land, r.total_str_att)],
  ["Strike %", (r) => pct(r.total_str_land, r.total_str_att)],
  ["Arm strikes", (r) => of(r.arm_land, r.arm_att)],
  ["Leg strikes", (r) => of(r.leg_land, r.leg_att)],
  ["Ground strikes", (r) => of(r.ground_land, r.ground_att)],
  ["Power strikes landed", (r) => dash(r.power_land)],
  ["Takedowns", (r) => of(r.td_land, r.td_att)],
  ["Sub. attempts", (r) => dash(r.sub_att)],
  ["Dominant positions", (r) => dash(r.dominant_positions)],
  ["Time on the ground", (r) => mmss(r.ground_sec)],
  ["Time standing", (r) => mmss(r.standing_sec)],
];

async function fight(id, show) {
  // Round stats in UFC Stats' columns, or PFL's older SmartCage set (its own columns).
  const [[x], ufc, cage] = await Promise.all([
    q("SELECT * FROM site_fights WHERE fight_id = ?", [id]),
    q("SELECT * FROM round_stats WHERE fight_id = ? ORDER BY round", [id]),
    q("SELECT * FROM smartcage_round_stats WHERE fight_id = ? ORDER BY round", [id]),
  ]);
  if (!x) { show("<p>Fight not found.</p>"); return; }
  const ps = [{ fighter_id: x.a_id, name: x.a_name, result: x.a_res }, { fighter_id: x.b_id, name: x.b_name, result: x.b_res }]
    .filter((p) => p.fighter_id);
  const smartcage = !ufc.length;
  const rounds = smartcage ? cage : ufc;
  const nums = [...new Set(rounds.map((r) => r.round))];
  const statsFrom = rounds.length ? (smartcage ? "PFL (SmartCage)" : SOURCE_NAMES[rounds[0].source] || rounds[0].source) : "";
  const links = isUfcStats(id) ? [ext(`http://ufcstats.com/fight-details/${id}`, "UFC Stats")] : [];
  (await eventSources(x.event_id)).filter((l) => !l.includes("ufcstats.com")).forEach((l) => links.push(l));

  // Totals: sum each column over rounds; stays NULL if every round is NULL.
  const total = (fid) => {
    const out = {};
    for (const r of rounds.filter((r) => r.fighter_id === fid)) {
      for (const [k, v] of Object.entries(r)) {
        if (typeof v === "number" && k !== "round") out[k] = (out[k] ?? 0) + v;
        else if (!(k in out)) out[k] = null;
      }
    }
    return out;
  };
  const pick = (sel) => ps.map((p) => sel === "t"
    ? total(p.fighter_id)
    : rounds.find((r) => r.round === Number(sel) && r.fighter_id === p.fighter_id) || {});

  if (!show(`
    <p>${eventLink(x.event_id, x.event)} · ${esc(x.date)}</p>
    <h1>${ps.map((p) => `${fighterLink(p.fighter_id, p.name)} ${badge(p.result)}`).join(" vs ")}</h1>
    <p>${esc(x.weight_class || "")} ${boutTags(x)}<br>
       ${esc(x.method || "—")} · Round ${dash(x.end_round)} · ${dash(x.end_time)}
       ${x.time_format ? ` · ${esc(x.time_format)}` : ""}${x.referee ? ` · Referee: ${esc(x.referee)}` : ""}
       ${x.details ? `<br><small class="muted">${esc(x.details)}</small>` : ""}</p>
    ${rounds.length ? `
      <select id="rsel"><option value="t">Totals</option>${nums.map((n) => `<option value="${n}">Round ${n}</option>`).join("")}</select>
      <div id="stats"></div>
      <p class="muted"><small>Stats counted by ${esc(statsFrom)}.${smartcage
        ? " This older PFL system counts all strikes by arm, leg and ground, not significant strikes."
        : STATS_NOTES[rounds[0].source] ? ` ${STATS_NOTES[rounds[0].source]}` : ""}</small></p>`
      : `<p class="muted">No round-by-round stats recorded for this fight.</p>`}
    ${sourceLine(links)}`) || !rounds.length) return;
  const sel = document.getElementById("rsel");
  const render = () => {
    const [a, b] = pick(sel.value);
    document.getElementById("stats").innerHTML = table(
      ["", esc(ps[0].name), esc(ps[1]?.name || "")],
      (smartcage ? SMARTCAGE_ROWS : ROUND_ROWS).map(([label, fmt]) =>
        `<tr><td class="muted">${label}</td><td class="num">${fmt(a)}</td><td class="num">${fmt(b)}</td></tr>`));
  };
  sel.onchange = render;
  render();
}

// Leaderboards, one board per source of numbers: results come from every fight, and each
// stats source (they count differently) ranks only its own fights, with the stats it has.
// The boards and their stats (SQL over pre-computed per-fighter totals) are defined in
// scripts/site_db.py and read from site_boards.
const LEADER_SUMS = "SUM(n) n, " + ["kd", "sl", "sa", "tsl", "tsa", "tl", "ta", "suba", "ctrl", "arm", "leg", "grd"]
  .map((c) => `SUM(${c}) ${c}`).join(", ");

// The top 50 for a board, stat and filters. The page's opening view (all promotions and
// years, min. 1 fight) is pre-computed in site_top. Otherwise the results board, which has
// all years only, ranks across all promotions through an index per stat or reads one
// promotion's slice; a stats board sums its fighters' years in range.
function leaderQuery(board, stat, s, f, allYears) {
  const keep = `n >= ? AND ${s.expr} IS NOT NULL ${s.where ? `AND ${s.where}` : ""}`;
  const top = `ORDER BY ${s.expr} DESC, n LIMIT 50`;
  const named = (sql) => `SELECT t.fighter_id, f.name, t.n, t.val FROM (${sql}) t
                          JOIN fighters f USING (fighter_id) ORDER BY t.val DESC, t.n`;
  if (f.promo === "0" && (allYears || board === "results") && f.min === "1") {
    return ["SELECT fighter_id, name, n, val FROM site_top WHERE board = ? AND stat = ? AND division = ? ORDER BY rank",
            [board, stat, Number(f.div)]];
  }
  if (board === "results") {
    // Spelled out so SQLite picks the partial indexes.
    const promo = f.promo === "0" ? "promotion = 0" : "promotion = ? AND promotion <> 0";
    return [named(`SELECT fighter_id, n, ${s.expr} val FROM site_results
                   WHERE ${promo} AND division = ? AND ${keep} ${top}`),
            [...(f.promo === "0" ? [] : [Number(f.promo)]), Number(f.div), Number(f.min)]];
  }
  if (allYears) {
    return [named(`SELECT fighter_id, n, ${s.expr} val FROM site_leaders
                   WHERE board = ? AND promotion = ? AND division = ? AND year = 0 AND ${keep} ${top}`),
            [board, Number(f.promo), Number(f.div), Number(f.min)]];
  }
  return [named(`SELECT fighter_id, n, ${s.expr} val FROM (
                   SELECT fighter_id, ${LEADER_SUMS} FROM site_leaders
                   WHERE board = ? AND promotion = ? AND division = ? AND year BETWEEN ? AND ?
                   GROUP BY fighter_id)
                 WHERE ${keep} ${top}`),
          [board, Number(f.promo), Number(f.div), Number(f.from), Number(f.to), Number(f.min)]];
}

async function leaders(_, show) {
  const p = params();
  const [[c], promos, divisions, [boards]] = await Promise.all([
    q("SELECT * FROM site_stats"), promotions(), q("SELECT code, division FROM site_divisions ORDER BY code"),
    q("SELECT json FROM site_boards"),
  ]);
  const BOARDS = JSON.parse(boards.json);
  const [minY, maxY] = [c.first_date.slice(0, 4), c.last_date.slice(0, 4)];
  const years = [];
  for (let y = Number(maxY); y >= Number(minY); y--) years.push(String(y));
  const opt = (v, label, cur) => `<option value="${esc(v)}" ${cur === String(v) ? "selected" : ""}>${esc(label)}</option>`;
  const board0 = BOARDS[p.get("board")] ? p.get("board") : "results";
  const code = (k) => (/^\d+$/.test(p.get(k) || "") ? p.get(k) : "0");  // older links named the promotion
  const cur = { board: board0, stat: p.get("stat") || "", promo: code("promo"), div: code("div"),
                from: p.get("from") || minY, to: p.get("to") || maxY, min: p.get("min") || "1" };
  const statOptions = (board, stat) => Object.entries(BOARDS[board].stats).map(([k, s]) => opt(k, s.label, stat)).join("");
  if (!show(`
    <h1>Leaders</h1>
    <div class="filters">
      <label>Board<select id="board">${Object.entries(BOARDS).map(([k, b]) => opt(k, b.label, cur.board)).join("")}</select></label>
      <label>Stat<select id="stat">${statOptions(cur.board, cur.stat)}</select></label>
      <label>Promotion<select id="promo">${opt(0, "All promotions", cur.promo)}${promos.map((x) => opt(x.code, x.promotion, cur.promo)).join("")}</select></label>
      <label>Division<select id="div">${opt(0, "All divisions", cur.div)}${divisions.map((d) => opt(d.code, d.division, cur.div)).join("")}</select></label>
      <label>From<select id="from">${years.map((y) => opt(y, y, cur.from)).join("")}</select></label>
      <label>To<select id="to">${years.map((y) => opt(y, y, cur.to)).join("")}</select></label>
      <label>Min. fights<input type="number" id="min" min="1" value="${esc(cur.min)}"></label>
    </div>
    <div id="results"></div>`)) return;
  const val = (k) => document.getElementById(k).value;
  const find = latest((sql, args) => q(sql, args));
  const render = async () => {
    const f = { board: val("board"), stat: val("stat"), promo: val("promo"), div: val("div"), from: val("from"),
                to: val("to"), min: String(Math.max(1, Number(val("min")) || 1)) };
    const results = f.board === "results";
    document.getElementById("from").disabled = document.getElementById("to").disabled = results;
    setParams(new URLSearchParams(f));
    const b = BOARDS[f.board];
    const stat = b.stats[f.stat] ? f.stat : Object.keys(b.stats)[0];
    const s = b.stats[stat];
    const rows = await find(...leaderQuery(f.board, stat, s, f, f.from === minY && f.to === maxY));
    if (rows === undefined) return;
    const shown = (v) => s.fmt === "pct" ? Math.round(v * 1000) / 10 + "%" : s.fmt === "time" ? hmm(v) : num(v);
    document.getElementById("results").innerHTML = table(["#", "Fighter", results ? "Fights" : "Fights counted", esc(s.label)],
      rows.map((r, i) => `<tr><td class="num">${i + 1}</td><td>${fighterLink(r.fighter_id, r.name)}</td>
                          <td class="num">${r.n}</td><td class="num">${shown(r.val)}</td></tr>`)) +
      `<p class="muted"><small>${esc(b.note)}</small></p>`;
  };
  app.querySelectorAll("select, input").forEach((el) => (el.onchange = render));
  // Each board has its own stats: changing board refills the stat list.
  document.getElementById("board").onchange = () => {
    const stat = document.getElementById("stat");
    stat.innerHTML = statOptions(val("board"), stat.value);
    render();
  };
  await render();
}

const EXAMPLES = {
  "Latest events": "SELECT date, name, location FROM events ORDER BY date DESC LIMIT 20;",
  "A fighter's fights": `SELECT date, event, method, a_name, a_res, b_name, b_res, fight_id
FROM site_fights
WHERE (SELECT fighter_id FROM site_top_fighters LIMIT 1) IN (a_id, b_id)
ORDER BY date;`,
  "Most knockdowns in one fight": `SELECT f.name, x.event, x.date, SUM(r.knockdowns) AS kd, r.fight_id
FROM round_stats r
JOIN fighters f USING (fighter_id)
JOIN site_fights x USING (fight_id)
GROUP BY r.fight_id, r.fighter_id
ORDER BY kd DESC LIMIT 20;`,
  "Where round stats come from": "SELECT source, COUNT(DISTINCT fight_id) AS fights, COUNT(*) AS fighter_rounds FROM round_stats GROUP BY source;",
  "Promotions with the most events": "SELECT promotion, events FROM site_promotions ORDER BY events DESC LIMIT 30;",
  "How fights end (reads every fight)": "SELECT method, COUNT(*) AS fights FROM fights GROUP BY method ORDER BY fights DESC;",
  "Fights per division (reads every fight)": "SELECT division, COUNT(*) AS fights FROM site_fights GROUP BY division ORDER BY fights DESC;",
  "All tables and columns": "SELECT type, name, sql FROM sqlite_master WHERE type IN ('table', 'view');",
};

async function sql(_, show) {
  const first = Object.values(EXAMPLES)[0];
  if (!show(`
    <h1>SQL</h1>
    <p>Query the whole database directly. It runs in your browser, so you can't break anything.
       Only the parts of the database a query reads are downloaded: lookups by id are quick,
       but a query that reads a whole table downloads that table, which can be tens of MB.
       For heavy work, <a href="${RELEASES}">download the database</a>.</p>
    <p>This copy is arranged for the site: <code>fights</code> and <code>fight_participants</code>
       are views over <code>site_fights</code>, which has each fight with its event, both fighters
       and its <code>division</code>. Columns named <code>fighter_id</code>, <code>event_id</code>
       or <code>fight_id</code> become links.</p>
    <select id="ex"><option value="">Examples…</option>${Object.keys(EXAMPLES).map((k) => `<option>${esc(k)}</option>`).join("")}</select>
    <textarea id="sqltext" class="sql" spellcheck="false">${esc(params().get("q") || first)}</textarea>
    <button id="run">Run (Ctrl+Enter)</button>
    <div id="out"></div>`)) return;
  const ta = document.getElementById("sqltext");
  const run = async () => {
    const out = document.getElementById("out");
    setParams(new URLSearchParams({ q: ta.value }));
    out.innerHTML = `<p class="muted">Running…</p>`;
    const before = (await worker.worker.getStats()).totalFetchedBytes;
    try {
      const res = await worker.db.exec(ta.value);
      const read = (await worker.worker.getStats()).totalFetchedBytes - before;
      const fetched = read ? ` · ${mb(read)} downloaded` : "";
      if (!res.length) { out.innerHTML = `<p class="muted">Done. No rows returned${fetched}.</p>`; return; }
      const { columns, values } = res[res.length - 1];
      const cell = (c, v) => v == null ? "—"
        : c === "fighter_id" ? fighterLink(v, v) : c === "event_id" ? eventLink(v, v)
        : c === "fight_id" ? fightLink(v, v) : esc(v);
      out.innerHTML = `<p class="muted">${values.length} rows${values.length > 1000 ? " (showing 1,000)" : ""}${fetched}</p>` +
        table(columns.map(esc), values.slice(0, 1000).map((row) =>
          `<tr>${row.map((v, i) => `<td class="wrap">${cell(columns[i], v)}</td>`).join("")}</tr>`));
    } catch (e) {
      out.innerHTML = `<p class="error">${esc(e.message)}</p>`;
    }
  };
  document.getElementById("run").onclick = run;
  ta.onkeydown = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) run(); };
  document.getElementById("ex").onchange = (e) => {
    if (EXAMPLES[e.target.value]) { ta.value = EXAMPLES[e.target.value]; run(); }
  };
  await run();
}
