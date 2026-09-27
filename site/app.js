// Canon DB browser: loads canon.db into the page with sql.js and renders a few views.
// Views are chosen by the URL hash: #/fighters, #/fighter/<id>, #/events, #/event/<id>,
// #/fight/<id>, #/leaders, #/sql.

const SQLJS = "https://cdn.jsdelivr.net/npm/sql.js@1.14.2/dist/";
const DB_URLS = ["canon.db.gz", "../data/canon.db"];  // deployed site, then a local checkout

const app = document.getElementById("app");
let db;

// --- helpers -------------------------------------------------------------------------

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function q(sql, params = []) {
  const st = db.prepare(sql);
  st.bind(params);
  const rows = [];
  while (st.step()) rows.push(st.getAsObject());
  st.free();
  return rows;
}

const dash = (v) => (v == null ? "—" : v);
const of = (l, a) => (l == null ? "—" : `${l} of ${a}`);
const pct = (l, a) => (l == null || !a ? "—" : Math.round((100 * l) / a) + "%");
const mmss = (s) => (s == null ? "—" : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`);
const hmm = (s) => (s == null ? "—" : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`);
const num = (n) => (n == null ? "—" : Number(n).toLocaleString());
const badge = (r) => (r ? `<span class="res res-${esc(r)}">${esc(r)}</span>` : "");
const fighterLink = (id, name) => `<a href="#/fighter/${esc(id)}">${esc(name)}</a>`;
const eventLink = (id, name) => `<a href="#/event/${esc(id)}">${esc(name)}</a>`;
const fightLink = (id, text) => `<a href="#/fight/${esc(id)}">${esc(text)}</a>`;
const table = (head, rows) =>
  `<div class="scroll"><table><thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead>` +
  `<tbody>${rows.join("") || `<tr><td colspan="${head.length}" class="muted">Nothing found.</td></tr>`}</tbody></table></div>`;
const record = (r) => `${r.w || 0}-${r.l || 0}-${r.d || 0}` + (r.nc ? ` (${r.nc} NC)` : "");

function debounce(fn, ms = 150) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// Query-string state inside the hash (#/leaders?stat=kd), so filters survive back/refresh.
function params() { return new URLSearchParams(location.hash.split("?")[1] || ""); }
function setParams(p) {
  const base = location.hash.split("?")[0];
  history.replaceState(null, "", `${base}?${p.toString()}`);
}

// Weight classes are stored as the bout title ("UFC Lightweight Title Bout");
// division() reduces them to the division name.
const DIVISIONS = ["Women's Strawweight", "Women's Flyweight", "Women's Bantamweight",
  "Women's Featherweight", "Flyweight", "Bantamweight", "Featherweight", "Lightweight",
  "Welterweight", "Middleweight", "Light Heavyweight", "Heavyweight", "Catch Weight", "Open Weight"];
function division(wc) {
  if (!wc) return "Other";
  const s = wc.replace(/Catchweight/i, "Catch Weight");
  return DIVISIONS.find((d) => s.includes(d)) || "Other";
}
const norm = (s) => (s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

// Seconds actually fought, from "3 Rnd (5-5-5)" + end round + end time.
function fightSeconds(timeFormat, endRound, endTime) {
  if (!endRound || !endTime) return null;
  const [m, s] = endTime.split(":").map(Number);
  const last = m * 60 + s;
  if (endRound === 1) return last;
  const lens = ((timeFormat || "").match(/\(([\d-]+)\)/) || [])[1];
  if (!lens) return null;
  const mins = lens.split("-").map(Number);
  if (mins.length < endRound - 1) return null;
  return mins.slice(0, endRound - 1).reduce((a, b) => a + b, 0) * 60 + last;
}

// --- loading -------------------------------------------------------------------------

async function fetchBytes(url, onProgress) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  const total = Number(r.headers.get("content-length")) || 0;
  const reader = r.body.getReader();
  const chunks = [];
  let loaded = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    loaded += value.length;
    onProgress(loaded, total);
  }
  let bytes = new Uint8Array(loaded);
  let off = 0;
  for (const c of chunks) { bytes.set(c, off); off += c.length; }
  if (bytes[0] === 0x1f && bytes[1] === 0x8b) {  // gzip: unpack in the browser
    const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
    bytes = new Uint8Array(await new Response(stream).arrayBuffer());
  }
  return bytes;
}

async function loadDb() {
  const bar = document.getElementById("progress");
  const onProgress = (loaded, total) => { if (total) { bar.max = total; bar.value = loaded; } };
  let lastErr;
  for (const url of DB_URLS) {
    try { return await fetchBytes(url, onProgress); } catch (e) { lastErr = e; }
  }
  throw lastErr;
}

(async function start() {
  try {
    const [SQL, bytes] = await Promise.all([
      initSqlJs({ locateFile: (f) => SQLJS + f }),
      loadDb(),
    ]);
    db = new SQL.Database(bytes);
    db.create_function("division", division);
    db.create_function("norm", norm);
    const last = q("SELECT name, date FROM events ORDER BY date DESC LIMIT 1")[0];
    document.getElementById("footer").innerHTML =
      `Data from <a href="http://ufcstats.com">UFC Stats</a>, updated daily. ` +
      `Latest event: ${esc(last.name)} (${esc(last.date)}).`;
    window.addEventListener("hashchange", route);
    route();
  } catch (e) {
    app.innerHTML = `<p class="error">Couldn't load the database: ${esc(e.message)}</p>`;
  }
})();

// --- routing -------------------------------------------------------------------------

function route() {
  const path = (location.hash.slice(1) || "/").split("?")[0];
  const [page = "", id = ""] = path.split("/").filter(Boolean);
  const views = { "": home, fighters, fighter, events, event, fight, leaders, sql };
  (views[page] || home)(id);
  window.scrollTo(0, 0);
}

// --- views ---------------------------------------------------------------------------

function home() {
  const c = q(`SELECT (SELECT COUNT(*) FROM events) ev, (SELECT COUNT(*) FROM fights) fi,
               (SELECT COUNT(*) FROM fighters) fr, (SELECT MIN(date) FROM events) first`)[0];
  const recent = q("SELECT event_id, name, date, location FROM events ORDER BY date DESC LIMIT 10");
  app.innerHTML = `
    <h1>Every UFC fight, round by round</h1>
    <p>${num(c.ev)} events, ${num(c.fi)} fights and ${num(c.fr)} fighters since ${esc(c.first)}.
       Search a fighter, open any card, or rank fighters by their stats.</p>
    <form id="s"><input type="search" name="q" placeholder="Search fighters…" autofocus></form>
    <h2>Latest events</h2>
    ${table(["Date", "Event", "Location"], recent.map((e) =>
      `<tr><td>${esc(e.date)}</td><td>${eventLink(e.event_id, e.name)}</td><td>${esc(e.location)}</td></tr>`))}
    <p><a href="#/events">All events →</a></p>`;
  document.getElementById("s").onsubmit = (ev) => {
    ev.preventDefault();
    location.hash = `#/fighters?q=${encodeURIComponent(ev.target.q.value)}`;
  };
}

function fighters() {
  const p = params();
  app.innerHTML = `
    <h1>Fighters</h1>
    <input type="search" id="q" placeholder="Search by name…" value="${esc(p.get("q") || "")}" autofocus>
    <div id="results"></div>`;
  const input = document.getElementById("q");
  const render = () => {
    const term = norm(input.value.trim());
    setParams(new URLSearchParams(term ? { q: input.value.trim() } : {}));
    const rows = q(`
      SELECT f.fighter_id, f.name, f.nickname, COUNT(p.fight_id) n,
             SUM(p.result = 'W') w, SUM(p.result = 'L') l, SUM(p.result = 'D') d, SUM(p.result = 'NC') nc
      FROM fighters f LEFT JOIN fight_participants p USING (fighter_id)
      WHERE ?1 = '' OR norm(f.name) LIKE ?2
         OR f.fighter_id IN (SELECT fighter_id FROM fighter_aliases WHERE norm(name) LIKE ?2)
      GROUP BY f.fighter_id ORDER BY n DESC, f.name LIMIT 50`, [term, `%${term}%`]);
    document.getElementById("results").innerHTML =
      (term ? "" : `<p class="muted">Most UFC fights:</p>`) +
      table(["Fighter", "Record", "UFC fights"], rows.map((r) =>
        `<tr><td>${fighterLink(r.fighter_id, r.name)}${r.nickname ? ` <span class="muted">“${esc(r.nickname)}”</span>` : ""}</td>
             <td>${record(r)}</td><td class="num">${r.n}</td></tr>`));
  };
  input.oninput = debounce(render);
  render();
}

function fighter(id) {
  const f = q("SELECT * FROM fighters WHERE fighter_id = ?", [id])[0];
  if (!f) { app.innerHTML = "<p>Fighter not found.</p>"; return; }
  const fights = q(`
    SELECT e.date, e.event_id, e.name AS event, x.fight_id, x.weight_class, x.method, x.end_round,
           x.end_time, x.time_format, me.result, o.fighter_id AS opp_id, of.name AS opp
    FROM fight_participants me
    JOIN fights x USING (fight_id) JOIN events e USING (event_id)
    JOIN fight_participants o ON o.fight_id = me.fight_id AND o.corner <> me.corner
    JOIN fighters of ON of.fighter_id = o.fighter_id
    WHERE me.fighter_id = ? ORDER BY e.date DESC`, [id]);
  const own = q(`SELECT fight_id, SUM(knockdowns) kd, SUM(sig_str_land) sl, SUM(sig_str_att) sa,
                        SUM(td_land) tl, SUM(td_att) ta, SUM(sub_att) sub, SUM(ctrl_sec) ctrl
                 FROM round_stats WHERE fighter_id = ? GROUP BY fight_id`, [id]);
  const opp = q(`SELECT SUM(r.sig_str_land) sl, SUM(r.sig_str_att) sa FROM round_stats r
                 JOIN fight_participants p ON p.fight_id = r.fight_id AND p.fighter_id = ?
                 WHERE r.fighter_id <> ?`, [id, id])[0];

  const rec = { w: 0, l: 0, d: 0, nc: 0 };
  fights.forEach((x) => { if (x.result) rec[x.result.toLowerCase()]++; });
  const t = { kd: 0, sl: 0, sa: 0, tl: 0, ta: 0, sub: 0, ctrl: 0 };
  let secs = 0, slTimed = 0;
  const byFight = Object.fromEntries(fights.map((x) => [x.fight_id, x]));
  for (const r of own) {
    for (const k in t) t[k] += r[k] || 0;
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
  ].filter(Boolean).join(" · ");
  const stat = (label, value) => `<div class="stat"><small class="muted">${label}</small><b>${value}</b></div>`;

  app.innerHTML = `
    <h1>${esc(f.name)}</h1>
    <p>${bio}</p>
    <div class="grid2">
      ${stat("UFC record", record(rec))}
      ${stat("Sig. strikes / min", secs ? (slTimed / (secs / 60)).toFixed(2) : "—")}
      ${stat("Sig. strike accuracy", pct(t.sl, t.sa))}
      ${stat("Sig. strike defense", opp.sa ? pct(opp.sa - opp.sl, opp.sa) : "—")}
      ${stat("Takedowns", `${t.tl} of ${t.ta}`)}
      ${stat("Knockdowns", t.kd)}
      ${stat("Sub. attempts", t.sub)}
      ${stat("Control time", hmm(t.ctrl))}
    </div>
    <h2>Fights</h2>
    ${table(["", "Opponent", "Method", "Rd", "Time", "Event", "Date"], fights.map((x) => `
      <tr><td>${badge(x.result)}</td><td>${fighterLink(x.opp_id, x.opp)}</td>
          <td>${fightLink(x.fight_id, x.method || "—")}</td><td>${dash(x.end_round)}</td>
          <td>${dash(x.end_time)}</td><td>${eventLink(x.event_id, x.event)}</td><td>${esc(x.date)}</td></tr>`))}
    <p><small><a href="http://ufcstats.com/fighter-details/${esc(id)}">View on UFC Stats</a></small></p>`;
}

function events() {
  const all = q(`SELECT e.event_id, e.name, e.date, e.location, COUNT(f.fight_id) n
                 FROM events e LEFT JOIN fights f USING (event_id)
                 GROUP BY e.event_id ORDER BY e.date DESC`);
  const years = [...new Set(all.map((e) => e.date.slice(0, 4)))];
  const p = params();
  app.innerHTML = `
    <h1>Events</h1>
    <div class="filters">
      <input type="search" id="q" placeholder="Search events or places…" value="${esc(p.get("q") || "")}">
      <select id="y"><option value="">All years</option>${years.map((y) =>
        `<option ${p.get("y") === y ? "selected" : ""}>${y}</option>`).join("")}</select>
    </div>
    <div id="results"></div>`;
  const qi = document.getElementById("q"), yi = document.getElementById("y");
  const render = () => {
    const term = norm(qi.value.trim()), year = yi.value;
    setParams(new URLSearchParams({ ...(qi.value.trim() && { q: qi.value.trim() }), ...(year && { y: year }) }));
    const rows = all.filter((e) => (!year || e.date.startsWith(year)) &&
      (!term || norm(e.name).includes(term) || norm(e.location).includes(term)));
    document.getElementById("results").innerHTML =
      `<p class="muted">${rows.length} events</p>` +
      table(["Date", "Event", "Location", "Fights"], rows.slice(0, 300).map((e) =>
        `<tr><td>${esc(e.date)}</td><td>${eventLink(e.event_id, e.name)}</td>
             <td>${esc(e.location)}</td><td class="num">${e.n}</td></tr>`));
  };
  qi.oninput = debounce(render);
  yi.onchange = render;
  render();
}

function event(id) {
  const e = q("SELECT * FROM events WHERE event_id = ?", [id])[0];
  if (!e) { app.innerHTML = "<p>Event not found.</p>"; return; }
  const fights = q(`
    SELECT x.fight_id, x.weight_class, x.method, x.end_round, x.end_time,
           a.fighter_id a_id, fa.name a_name, a.result a_res,
           b.fighter_id b_id, fb.name b_name, b.result b_res
    FROM fights x
    JOIN fight_participants a ON a.fight_id = x.fight_id AND a.corner = 0
    JOIN fight_participants b ON b.fight_id = x.fight_id AND b.corner = 1
    JOIN fighters fa ON fa.fighter_id = a.fighter_id
    JOIN fighters fb ON fb.fighter_id = b.fighter_id
    WHERE x.event_id = ? ORDER BY x.rowid`, [id]);
  app.innerHTML = `
    <h1>${esc(e.name)}</h1>
    <p>${esc(e.date)} · ${esc(e.location)}</p>
    ${table(["Fighters", "Weight class", "Method", "Rd", "Time", ""], fights.map((x) => `
      <tr><td>${badge(x.a_res)} ${fighterLink(x.a_id, x.a_name)}<br>${badge(x.b_res)} ${fighterLink(x.b_id, x.b_name)}</td>
          <td>${esc(division(x.weight_class))}</td><td>${esc(x.method || "—")}</td>
          <td>${dash(x.end_round)}</td><td>${dash(x.end_time)}</td>
          <td>${fightLink(x.fight_id, "Stats →")}</td></tr>`))}
    <p><small><a href="http://ufcstats.com/event-details/${esc(id)}">View on UFC Stats</a></small></p>`;
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

function fight(id) {
  const x = q(`SELECT x.*, e.name event, e.date FROM fights x JOIN events e USING (event_id)
               WHERE fight_id = ?`, [id])[0];
  if (!x) { app.innerHTML = "<p>Fight not found.</p>"; return; }
  const ps = q(`SELECT p.corner, p.result, p.fighter_id, f.name FROM fight_participants p
                JOIN fighters f USING (fighter_id) WHERE fight_id = ? ORDER BY corner`, [id]);
  const rounds = q("SELECT * FROM round_stats WHERE fight_id = ? ORDER BY round", [id]);
  const nums = [...new Set(rounds.map((r) => r.round))];

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

  app.innerHTML = `
    <p>${eventLink(x.event_id, x.event)} · ${esc(x.date)}</p>
    <h1>${ps.map((p) => `${fighterLink(p.fighter_id, p.name)} ${badge(p.result)}`).join(" vs ")}</h1>
    <p>${esc(x.weight_class || "")}<br>
       ${esc(x.method || "—")} · Round ${dash(x.end_round)} · ${dash(x.end_time)}
       ${x.time_format ? ` · ${esc(x.time_format)}` : ""}${x.referee ? ` · Referee: ${esc(x.referee)}` : ""}
       ${x.details ? `<br><small class="muted">${esc(x.details)}</small>` : ""}</p>
    ${rounds.length ? `
      <select id="rsel"><option value="t">Totals</option>${nums.map((n) => `<option value="${n}">Round ${n}</option>`).join("")}</select>
      <div id="stats"></div>` : `<p class="muted">No round-by-round stats recorded for this fight.</p>`}
    <p><small><a href="http://ufcstats.com/fight-details/${esc(id)}">View on UFC Stats</a></small></p>`;
  if (!rounds.length) return;
  const sel = document.getElementById("rsel");
  const render = () => {
    const [a, b] = pick(sel.value);
    document.getElementById("stats").innerHTML = table(
      ["", esc(ps[0].name), esc(ps[1]?.name || "")],
      ROUND_ROWS.map(([label, fmt]) =>
        `<tr><td class="muted">${label}</td><td class="num">${fmt(a)}</td><td class="num">${fmt(b)}</td></tr>`));
  };
  sel.onchange = render;
  render();
}

// Leaderboards. Each stat is a fixed SQL expression over the per-fighter aggregates below.
const LEADER_STATS = {
  wins:   { label: "Wins", expr: "per.w" },
  fin:    { label: "Finishes (KO/TKO + submission wins)", expr: "per.fin" },
  ko:     { label: "KO/TKO wins", expr: "per.ko" },
  subw:   { label: "Submission wins", expr: "per.subw" },
  kd:     { label: "Knockdowns", expr: "rs.kd" },
  sig:    { label: "Sig. strikes landed", expr: "rs.sl" },
  sigacc: { label: "Sig. strike accuracy (min. 100 attempted)", expr: "1.0 * rs.sl / rs.sa", where: "rs.sa >= 100", fmt: "pct" },
  td:     { label: "Takedowns landed", expr: "rs.tl" },
  tdacc:  { label: "Takedown accuracy (min. 10 attempted)", expr: "1.0 * rs.tl / rs.ta", where: "rs.ta >= 10", fmt: "pct" },
  subatt: { label: "Submission attempts", expr: "rs.suba" },
  ctrl:   { label: "Control time", expr: "rs.ctrl", fmt: "time" },
  fights: { label: "UFC fights", expr: "per.n" },
};

function leaders() {
  const p = params();
  const [minY, maxY] = Object.values(q("SELECT MIN(substr(date,1,4)) a, MAX(substr(date,1,4)) b FROM events")[0]);
  const years = [];
  for (let y = Number(maxY); y >= Number(minY); y--) years.push(String(y));
  const opt = (v, label, cur) => `<option value="${esc(v)}" ${cur === v ? "selected" : ""}>${esc(label)}</option>`;
  const cur = { stat: p.get("stat") || "wins", div: p.get("div") || "", from: p.get("from") || minY,
                to: p.get("to") || maxY, min: p.get("min") || "1" };
  app.innerHTML = `
    <h1>Leaders</h1>
    <div class="filters">
      <label>Stat<select id="stat">${Object.entries(LEADER_STATS).map(([k, s]) => opt(k, s.label, cur.stat)).join("")}</select></label>
      <label>Division<select id="div">${opt("", "All divisions", cur.div)}${DIVISIONS.map((d) => opt(d, d, cur.div)).join("")}</select></label>
      <label>From<select id="from">${years.map((y) => opt(y, y, cur.from)).join("")}</select></label>
      <label>To<select id="to">${years.map((y) => opt(y, y, cur.to)).join("")}</select></label>
      <label>Min. fights<input type="number" id="min" min="1" value="${esc(cur.min)}"></label>
    </div>
    <div id="results"></div>`;
  const val = (k) => document.getElementById(k).value;
  const render = () => {
    const f = { stat: val("stat"), div: val("div"), from: val("from"), to: val("to"), min: val("min") || "1" };
    setParams(new URLSearchParams(f));
    const s = LEADER_STATS[f.stat];
    const rows = q(`
      WITH ff AS (
        SELECT x.fight_id, x.method FROM fights x JOIN events e USING (event_id)
        WHERE (?1 = '' OR division(x.weight_class) = ?1) AND substr(e.date, 1, 4) BETWEEN ?2 AND ?3),
      per AS (
        SELECT p.fighter_id, COUNT(*) n, SUM(p.result = 'W') w,
               SUM(p.result = 'W' AND (ff.method LIKE '%KO%' OR ff.method = 'Submission')) fin,
               SUM(p.result = 'W' AND ff.method LIKE '%KO%') ko,
               SUM(p.result = 'W' AND ff.method = 'Submission') subw
        FROM fight_participants p JOIN ff USING (fight_id) GROUP BY p.fighter_id),
      rs AS (
        SELECT r.fighter_id, SUM(knockdowns) kd, SUM(sig_str_land) sl, SUM(sig_str_att) sa,
               SUM(td_land) tl, SUM(td_att) ta, SUM(sub_att) suba, SUM(ctrl_sec) ctrl
        FROM round_stats r JOIN ff USING (fight_id) GROUP BY r.fighter_id)
      SELECT f.fighter_id, f.name, per.n, ${s.expr} AS val
      FROM per JOIN fighters f USING (fighter_id) LEFT JOIN rs USING (fighter_id)
      WHERE per.n >= ?4 AND ${s.expr} IS NOT NULL ${s.where ? `AND ${s.where}` : ""}
      ORDER BY val DESC, per.n ASC LIMIT 50`, [f.div, f.from, f.to, Number(f.min)]);
    const show = (v) => s.fmt === "pct" ? Math.round(v * 1000) / 10 + "%" : s.fmt === "time" ? hmm(v) : num(v);
    document.getElementById("results").innerHTML = table(["#", "Fighter", "Fights", esc(s.label)],
      rows.map((r, i) => `<tr><td class="num">${i + 1}</td><td>${fighterLink(r.fighter_id, r.name)}</td>
                          <td class="num">${r.n}</td><td class="num">${show(r.val)}</td></tr>`));
  };
  app.querySelectorAll("select, input").forEach((el) => (el.onchange = render));
  render();
}

const EXAMPLES = {
  "Latest events": "SELECT date, name, location FROM events ORDER BY date DESC LIMIT 20;",
  "How fights end": "SELECT method, COUNT(*) AS fights FROM fights GROUP BY method ORDER BY fights DESC;",
  "Most knockdowns in one fight": `SELECT f.name, e.name AS event, e.date, SUM(r.knockdowns) AS kd, r.fight_id
FROM round_stats r
JOIN fighters f USING (fighter_id)
JOIN fights x USING (fight_id)
JOIN events e USING (event_id)
GROUP BY r.fight_id, r.fighter_id
ORDER BY kd DESC LIMIT 20;`,
  "Title fights per year": `SELECT substr(e.date, 1, 4) AS year, COUNT(*) AS title_fights
FROM fights x JOIN events e USING (event_id)
WHERE x.title_fight = 1 GROUP BY year ORDER BY year;`,
  "Fights per division": "SELECT division(weight_class) AS division, COUNT(*) AS fights FROM fights GROUP BY division ORDER BY fights DESC;",
  "All tables and columns": "SELECT name, sql FROM sqlite_master WHERE type = 'table';",
};

function sql() {
  const first = Object.values(EXAMPLES)[0];
  app.innerHTML = `
    <h1>SQL</h1>
    <p>Query the whole database directly. It runs in your browser, so you can't break anything:
       reloading the page resets it. Columns named <code>fighter_id</code>, <code>event_id</code>
       or <code>fight_id</code> become links. <code>division(weight_class)</code> gives a fight's division.</p>
    <select id="ex"><option value="">Examples…</option>${Object.keys(EXAMPLES).map((k) => `<option>${esc(k)}</option>`).join("")}</select>
    <textarea id="sqltext" class="sql" spellcheck="false">${esc(params().get("q") || first)}</textarea>
    <button id="run">Run (Ctrl+Enter)</button>
    <div id="out"></div>`;
  const ta = document.getElementById("sqltext");
  const run = () => {
    const out = document.getElementById("out");
    setParams(new URLSearchParams({ q: ta.value }));
    try {
      const res = db.exec(ta.value);
      if (!res.length) { out.innerHTML = `<p class="muted">Done. No rows returned.</p>`; return; }
      const { columns, values } = res[res.length - 1];
      const cell = (c, v) => v == null ? "—"
        : c === "fighter_id" ? fighterLink(v, v) : c === "event_id" ? eventLink(v, v)
        : c === "fight_id" ? fightLink(v, v) : esc(v);
      out.innerHTML = `<p class="muted">${values.length} rows${values.length > 1000 ? " (showing 1,000)" : ""}</p>` +
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
  run();
}
