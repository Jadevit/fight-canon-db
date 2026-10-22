// Fight Canon. Browsing pages read JSON files written daily by scripts/site_json.py: one per
// fighter and per event, the event lists, a search index split by prefix, and the
// leaderboards' opening views. Only the SQL page and filtered leaderboards query the database:
// DuckDB-WASM over the database as Parquet (scripts/export_parquet.py), reading only the
// columns and parts of the files a query needs; it loads the first time it's needed.
// Views are chosen by the path: / (landing), /fighters, /fighter/<id>, /events, /event/<id>,
// /fight/<id>, /leaders, /query (the server answers every path with this page). How it looks is
// in DESIGN.md.

const DUCKDB = "https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.29.0/+esm";
const DB_DIR = "/db/";
const REPO = "https://github.com/jadevit/fight-canon-db";
const RELEASES = `${REPO}/releases/latest`;

const app = document.getElementById("app");

const CONFIG = fetch(DB_DIR + "config.json", { cache: "no-cache" }).then((r) => {
  if (!r.ok) throw new Error(`${DB_DIR}config.json: HTTP ${r.status}`);
  return r.json();
});
const base = new URL(DB_DIR, location.href);
const API = CONFIG.then((c) => new URL(c.api || "v1/", base).href);

// A JSON file of the API, fetched once per page load; null if there's no such file.
const files = new Map();
function api(path) {
  if (!files.has(path)) {
    files.set(path, (async () => {
      const r = await fetch((await API) + path);
      if (r.status === 404 || r.status === 403) return null;
      if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
      return r.json();
    })().catch((e) => { files.delete(path); throw e; }));
  }
  return files.get(path);
}
const meta = () => api("meta.json");
// {cols, rows} to objects.
const objs = (t) => (t ? t.rows.map((r) => Object.fromEntries(t.cols.map((c, i) => [c, r[i]]))) : []);
const fileKey = (id) => id.replaceAll(":", "~");
const hex = (s) => [...new TextEncoder().encode(s)].map((b) => b.toString(16).padStart(2, "0")).join("");
// Which fx/ file a fight is in: the low byte of its FNV-1a hash (site_json.py does the same).
function shard(id) {
  let h = 0x811c9dc5;
  for (const b of new TextEncoder().encode(id)) h = Math.imul(h ^ b, 0x01000193) >>> 0;
  return (h & 0xff).toString(16).padStart(2, "0");
}

// The database, started the first time something queries it: a DuckDB connection with
// division(). Views over the Parquet files are made as queries need them (views()): each
// one reads its file's header, and the files are far away.
const sqlString = (s) => `'${String(s).replaceAll("'", "''")}'`;
let engine;
function db() {
  engine ||= (async () => {
    const [config, m, duckdb] = await Promise.all([CONFIG, meta(), import(DUCKDB)]);
    const bundle = await duckdb.selectBundle(duckdb.getJsDelivrBundles());
    const url = URL.createObjectURL(new Blob([`importScripts("${bundle.mainWorker}");`], { type: "text/javascript" }));
    const instance = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(duckdb.LogLevel.WARNING), new Worker(url));
    await instance.instantiate(bundle.mainModule, bundle.pthreadWorker);
    URL.revokeObjectURL(url);
    const conn = await instance.connect();
    await conn.query("SET enable_http_metadata_cache = true");
    // The division a bout title names: the first in meta's list (site_json.py builds the same).
    const names = objs(m.divisions).map((d) => d.division).filter((d) => d !== "Other");
    const title = "regexp_replace(coalesce(wc, ''), '(?i)catchweight', 'Catch Weight')";
    await conn.query(`CREATE MACRO division(wc) AS CASE ${names.map((d) =>
      `WHEN contains(${title}, ${sqlString(d)}) THEN ${sqlString(d)}`).join(" ")} ELSE 'Other' END`);
    return conn;
  })();
  engine.catch(() => { engine = null; });  // a failed start is tried again next time
  return engine;
}
// The views a query needs: the tables it names, and bouts' tables if it names bouts (every
// table if it looks at the catalogue). Made one at a time, in turn, whatever asks for them.
const made = new Set();
let making = Promise.resolve();
function views(conn, sql) {
  making = making.catch(() => {}).then(async () => {  // one failure doesn't stop the next
    const config = await CONFIG;
    const at = new URL(config.parquet ?? "", base).href;
    const words = new Set(sql.toLowerCase().match(/[a-z_]+/g) || []);
    const all = words.has("information_schema") || [...words].some((w) => w.startsWith("duckdb_"));
    const bouts = all || words.has("bouts");
    const want = config.tables.filter((t) => all || words.has(t) || (bouts && BOUT_TABLES.includes(t)));
    for (const t of want.filter((t) => !made.has(t))) {
      await conn.query(`CREATE OR REPLACE VIEW ${t} AS SELECT * FROM read_parquet('${at}${t}.parquet')`);
      made.add(t);
    }
    if (bouts && !made.has("bouts")) {
      await conn.query(BOUTS);
      made.add("bouts");
    }
  });
  return making;
}
const BOUT_TABLES = ["fights", "events", "fight_participants", "fighters"];
// Each fight with its event and both fighters.
const BOUTS = `CREATE OR REPLACE VIEW bouts AS
  SELECT x.fight_id, x.event_id, e.date, e.name AS event, e.promotion, division(x.weight_class) AS division,
         a.fighter_id AS a_id, fa.name AS a_name, a.result AS a_res, b.fighter_id AS b_id, fb.name AS b_name,
         b.result AS b_res, x.* EXCLUDE (fight_id, event_id)
  FROM fights x JOIN events e USING (event_id)
  LEFT JOIN fight_participants a ON a.fight_id = x.fight_id AND a.corner = 0
  LEFT JOIN fighters fa ON fa.fighter_id = a.fighter_id
  LEFT JOIN fight_participants b ON b.fight_id = x.fight_id AND b.corner = 1
  LEFT JOIN fighters fb ON fb.fighter_id = b.fighter_id`;
// A query's rows as objects; DuckDB's 64-bit integers come back as BigInt, made Numbers here.
// A SUM of integers is a 128-bit integer (and some results decimals), which come back as
// objects that are numbers only as text: they become Numbers too.
function plain(v) {
  if (typeof v === "bigint") return Number(v);
  if (v && typeof v === "object" && !Array.isArray(v) && !(v instanceof Date)) {
    const s = String(v);
    if (/^-?\d+(\.\d+)?$/.test(s)) return Number(s);
  }
  return v;
}
async function q(sql, params = []) {
  const conn = await db();
  await views(conn, sql);
  const st = await conn.prepare(sql);
  try {
    return (await st.query(...params)).toArray().map((r) =>
      Object.fromEntries(Object.entries(r.toJSON()).map(([k, v]) => [k, plain(v)])));
  } finally {
    await st.close();
  }
}

// --- helpers -------------------------------------------------------------------------

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const num = (n) => (n == null ? "—" : Number(n).toLocaleString("en-US"));
const pct = (l, a) => (l == null || !a ? null : Math.round((100 * l) / a) + "%");
const mmss = (s) => (s == null ? "—" : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`);
const hmm = (s) => (s == null ? "—" : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`);
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const monthYear = (d) => (d ? `${MONTHS[Number(d.slice(5, 7)) - 1]} ${d.slice(0, 4)}` : "");
const longDate = (d) => (d ? `${Number(d.slice(8, 10))} ${MONTHS[Number(d.slice(5, 7)) - 1]} ${d.slice(0, 4)}` : "");
const plural = (n, one, many = one + "s") => `${num(n)} ${n === 1 ? one : many}`;
const american = (v) => (v == null ? null : v > 0 ? `+${v}` : String(v));
const dot = (...parts) => parts.filter(Boolean).join(" · ");

const fighterLink = (id, name, cls = "") => `<a href="/fighter/${esc(id)}"${cls ? ` class="${cls}"` : ""}>${esc(name)}</a>`;
const eventLink = (id, name) => `<a href="/event/${esc(id)}">${esc(name)}</a>`;
const ext = (url, label) => `<a href="${esc(url)}" target="_blank" rel="noopener">${label}</a>`;
const badge = (text) => `<span class="badge">${esc(text)}</span>`;
const res = (r) => r ? `<span class="res res-${esc(r)}" title="${{ W: "Win", L: "Loss", D: "Draw", NC: "No contest" }[r] || ""}">${esc(r)}</span>`
  : `<span class="res res-none">–</span>`;
const nothing = (text = "Nothing found.") => `<p class="muted" style="padding:14px 0">${text}</p>`;
const loading = (text = "Loading…") => `<p class="loading"><span class="spinner"></span>${text}</p>`;

const ICON = {
  chev: `<svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>`,
  terminal: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 8 4 4-4 4M12 17h7"/></svg>`,
  fighters: `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.6 3.4-5.5 6.5-5.5s5.7 1.9 6.5 5.5"/><circle cx="17" cy="9" r="2.5"/><path d="M17 14.5c2.4 0 4 1.5 4.5 4"/></svg>`,
  events: `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/></svg>`,
  rounds: `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M12 6v12M12 9H5M12 15H8M12 9h7M12 15h4"/></svg>`,
  open: `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="6" rx="7.5" ry="3"/><path d="M4.5 6v12c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3V6M4.5 12c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3"/></svg>`,
};

const record = (r) => `${r.w || 0}-${r.l || 0}-${r.d || 0}` + (r.nc ? ` (${r.nc} NC)` : "");
// Pro MMA record from the fighter's UFC Stats page; falls back to the fights in the database.
const proRecord = (f, fallback) => f.wins != null
  ? record({ w: f.wins, l: f.losses, d: f.draws, nc: f.no_contests }) : record(fallback);

// How a win came: UFC Stats writes "KO/TKO", "Submission", "Decision - Split"; Sherdog writes
// "TKO (Punches)", "Submission (Armbar)", "Decision (Unanimous)".
function winKind(method) {
  const m = (method || "").toLowerCase();
  if (/^t?ko\b/.test(m)) return "ko";
  if (m.includes("submission")) return "sub";
  if (m.includes("decision")) return "dec";
  return "other";
}

// Links to the pages a row came from. Only UFC Stats ids (16 hex characters) exist on UFC
// Stats; everything else is on Sherdog, under the id in ours or in the aliases tables.
const isUfcStats = (id) => /^[0-9a-f]{16}$/.test(id);
const SOURCE_NAMES = { ufcstats: "UFC Stats", pfl: "PFL", oktagon: "Oktagon", sofascore: "Sofascore", sherdog: "Sherdog", smartcage: "PFL SmartCage" };
// What a stats source leaves out, said under its stats.
const STATS_NOTES = {
  oktagon: "Oktagon counts only what landed: no attempts, knockdowns or control time.",
  sofascore: "A third party's count, not the promotion's. Total strikes are counted thrown, not landed.",
  smartcage: "PFL's older system counts all strikes by arm, leg and ground, not significant strikes.",
};
// `aliases`: the event file's {sherdog: [ids], pfl: [ids]}.
function eventSources(id, aliases) {
  const links = isUfcStats(id) ? [ext(`http://ufcstats.com/event-details/${id}`, "UFC Stats")] : [];
  const sherdog = id.startsWith("sherdog:") ? [id.slice(8)] : aliases.sherdog || [];
  sherdog.forEach((s) => links.push(ext(`https://www.sherdog.com/events/x-${s}`, "Sherdog")));
  (aliases.pfl || []).forEach((s) => links.push(ext(`https://pflmma.com/event/${s}`, "PFL")));
  return links;
}
const sourceLine = (links) => links.length ? `<p class="note">Source: ${links.join(" · ")}</p>` : "";

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

// Filters live in the query string (/leaders?stat=kd), so they survive back and refresh.
function params() { return new URLSearchParams(location.search); }
function setParams(p) {
  const s = p.toString();
  history.replaceState(null, "", s ? `${location.pathname}?${s}` : location.pathname);
}

// Open a page of the site without reloading it.
function go(href) {
  history.pushState(null, "", href);
  route();
}

// Promotions with enough events to list (there are thousands of one-off shows).
const promotions = async () => objs((await meta()).promotions);

const norm = (s) => (s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

// The words of a search term, normalized like the search index, longest first; null if none
// has two letters.
function searchWords(term) {
  const words = (norm(term).match(/[\p{L}\p{N}_]+/gu) || []).sort((a, b) => b.length - a.length);
  if (!words.length || words[0].length < 2) return null;
  return { words, first: words[0] };
}

// Whether every word of a term starts a different word of a name ("jon jones" isn't
// "Edwynn Jones", though "jon" starts "jones").
function wordsMatch(terms, words, used = new Set()) {
  if (!terms.length) return true;
  const [t, ...rest] = terms;
  return words.some((w, i) => !used.has(i) && w.startsWith(t) && wordsMatch(rest, words, new Set(used).add(i)));
}

// Search a split index (sf/ fighters, se/ events): read the file of the longest word's first
// two letters, a letter deeper while that file is split, and keep the rows in which every
// word of the term starts a different word. Rows come in rank order (most fights; newest).
async function searchIndex(folder, s) {
  let p = s.first.slice(0, 2), file = await api(`${folder}/${hex(p)}.json`);
  while (file?.split && p.length < s.first.length) {
    p = s.first.slice(0, p.length + 1);
    file = await api(`${folder}/${hex(p)}.json`);
  }
  return objs(file).filter((r) => wordsMatch(s.words, r.words.split(" ")));
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

const searchFighters = async (s, limit) => (await searchIndex("sf", s)).slice(0, limit);

// Events, newest first: a search's, a promotion's (listed ones) or a year's, narrowed by year
// and promotion; with none of those, a year's file at a time from the newest. Returns
// {rows, total (null if unknown), more()}: more() adds the next year's events to rows, and
// returns false when there are none left to add.
async function eventList(s, year, promo) {
  const inYear = (e) => !year || e.date.startsWith(year);
  const ofPromo = (e) => !promo || e.promotion === promo;
  const done = async () => false;
  if (s || promo || year) {
    let rows;
    if (s) rows = await searchIndex("se", s);
    else if (promo) {
      const listed = (await promotions()).some((x) => x.promotion === promo);
      rows = listed ? objs(await api(`events/p/${hex(promo)}.json`)) : [];
    } else rows = objs(await api(`events/y/${year}.json`));
    rows = rows.filter((e) => inYear(e) && ofPromo(e));
    return { rows, total: rows.length, more: done };
  }
  const m = await meta();
  let y = Number(m.stats.last_date.slice(0, 4));
  const first = Number(m.stats.first_date.slice(0, 4));
  const list = { rows: [], total: m.stats.events };
  list.more = async () => {
    if (y < first) return false;
    list.rows.push(...objs(await api(`events/y/${y--}.json`)));
    return true;
  };
  await list.more();
  return list;
}

const fighterRow = (r) => `
  <a class="row" href="/fighter/${esc(r.fighter_id)}">
    <div class="main"><div class="title">${esc(r.name)}</div>
      ${r.nickname ? `<div class="sub">“${esc(r.nickname)}”</div>` : ""}</div>
    <div class="end"><b class="num">${proRecord(r, r)}</b>${plural(r.n, "fight")} here</div>
  </a>`;
const eventRow = (e) => `
  <a class="row" href="/event/${esc(e.event_id)}">
    <div class="main"><div class="title">${esc(e.name)}</div>
      <div class="sub">${esc(dot(longDate(e.date), e.promotion, e.location))}</div></div>
    <div class="end">${plural(e.n, "fight")}</div>
  </a>`;

// --- comparison bars -----------------------------------------------------------------
// Mirrored bars, a row per stat: the left fighter's grows left from the middle, the other's
// right. Color marks who won the stat, not the fight: the side that landed more is gold, the
// other pewter (both pewter on a tie), and what was attempted but missed is grey, after the
// landed part. A row is scaled to the larger number of attempts (or of the value, for stats
// with none). A recorded 0 keeps its row, though with 0 on both sides (and 0 attempts) there
// are no bars; a stat not recorded for either side has no row.

// `a`, `b`: [landed (or the count), attempted (null if none)]. `text`, if given, is what each
// end shows instead of fmt(value) (e.g. "12 /30"). `key` names the row's bars, so a redraw
// can move them (fight page).
function mirrorRow(label, a, b, fmt = num, text = null, key = label) {
  if (a[0] == null || b[0] == null) return "";
  const len = ([v, att]) => Math.max(0, att ?? v);  // how long a side's bar is
  const max = Math.max(len(a), len(b));
  const won = a[0] === b[0] ? -1 : a[0] > b[0] ? 0 : 1;
  const side = ([v, att], i) => {
    const n = len([v, att]);
    const bar = max ? (100 * n) / max : 0;  // both sides at 0: no bars
    const hit = n ? (100 * Math.max(0, v)) / n : 0;
    return `<div class="track${i ? "" : " l"}" data-k="${esc(key)}:${i}"><span class="extent${n ? " some" : ""}" style="width:${bar.toFixed(1)}%">` +
      `<span class="hit${won === i ? " win" : ""}${v ? " some" : ""}" style="width:${hit.toFixed(1)}%"></span></span></div>`;
  };
  const end = (v, i) => `<div class="v ${i ? "r" : "l"}${won < 0 ? "" : won === i ? " win" : " lose"}">${
    text ? text[i] : fmt(v)}</div>`;
  return `<div class="cap">${label}</div>${end(a[0], 0)}${side(a, 0)}${side(b, 1)}${end(b[0], 1)}`;
}
// Redraw rows of bars, moving those that were there before from their old lengths.
function redraw(el, html) {
  const was = new Map([...el.querySelectorAll("[data-k]")].map((t) =>
    [t.dataset.k, [...t.querySelectorAll("span")].map((s) => s.style.width)]));
  el.innerHTML = html;
  const moves = [];
  el.querySelectorAll("[data-k]").forEach((t) => {
    const old = was.get(t.dataset.k);
    if (old) t.querySelectorAll("span").forEach((s, i) => { moves.push([s, s.style.width]); s.style.width = old[i]; });
  });
  if (!moves.length) return;
  void el.offsetWidth;  // lay out the old lengths, so the change to the new ones is animated
  moves.forEach(([s, w]) => (s.style.width = w));
}
const landed = (l, a) => (l == null ? (a == null ? "—" : `${a} thr.`) : `${l}${a != null ? ` <small>/${a}</small>` : ""}`);

const eventFile = (eid) => api(`e/${fileKey(eid)}.json`);
const fightLink = (fid, eid) => `/fight/${esc(fid)}?e=${encodeURIComponent(eid)}`;

// A fight's round stats from its event's file: round_stats' columns, or PFL's older SmartCage
// set (its own columns).
function roundStats(ev, fid) {
  const st = ev?.stats[fid];
  return st ? { rows: objs(st), source: st.source, cage: st.cage } : null;
}

// "At a glance": strikes per round, control per round, then knockdowns and takedowns.
async function glance(fid, eid, left, names) {
  const st = roundStats(await eventFile(eid), fid);
  if (!st) return `<div class="glance"><p class="muted">No round stats for this fight.</p></div>`;
  const right = [...new Set(st.rows.map((r) => r.fighter_id))].find((i) => i !== left);
  const rounds = [...new Set(st.rows.map((r) => r.round))];
  const get = (id, n) => st.rows.find((r) => r.fighter_id === id && r.round === n) || {};
  const sum = (id, k) => st.rows.filter((r) => r.fighter_id === id && r[k] != null).reduce((t, r) => (t ?? 0) + r[k], null);
  const key = st.cage ? "total_str_land" : "sig_str_land";
  const per = (k) => rounds.map((n) => [get(left, n)[k], get(right, n)[k]]);
  const strikes = per(key), ctrl = per("ctrl_sec");
  const kd = [sum(left, "knockdowns"), sum(right, "knockdowns")], td = [sum(left, "td_land"), sum(right, "td_land")];
  const row = (label, [a, b], fmt) => mirrorRow(label, [a, null], [b, null], fmt);
  return `<div class="glance">
    <div class="glance-head"><span class="label">At a glance</span>${badge(SOURCE_NAMES[st.source] || st.source)}</div>
    <div class="who"><span class="gold">${esc(names[left] || "")}</span><span class="muted">${esc(names[right] || "")}</span></div>
    <div class="mirror">
      ${strikes.map((p, i) => row(`${st.cage ? "Strikes" : "Sig. Strikes"}, Round ${rounds[i]}`, p)).join("")}
      ${st.cage ? "" : ctrl.map((p, i) => row(`Control, Round ${rounds[i]}`, p, mmss)).join("")}
      ${row("Knockdowns", kd)}
      ${row("Takedowns", td)}
    </div>
    <a class="go" href="${fightLink(fid, eid)}">Full fight page →</a>
  </div>`;
}

// Rows that expand: only fights with round stats. The panel loads when it's first opened.
function expandable(container, names) {
  container.querySelectorAll("details[data-fight]").forEach((d) => {
    d.addEventListener("toggle", async () => {
      if (!d.open || d.dataset.loaded) return;
      d.dataset.loaded = "1";
      const slot = d.querySelector(".slot");
      slot.innerHTML = loading();
      try {
        slot.innerHTML = await glance(d.dataset.fight, d.dataset.event, d.dataset.left, names(d));
      } catch (e) {
        slot.innerHTML = `<p class="error">${esc(e.message)}</p>`;
      }
    });
  });
}

const fightRow = (statsAttrs, inner, hasStats) => hasStats
  ? `<details class="row-x" ${statsAttrs}><summary class="row">${inner}${ICON.chev}</summary><div class="slot"></div></details>`
  : `<div class="row">${inner}<span class="chev-space"></span></div>`;

// --- header --------------------------------------------------------------------------
// One box searches fighters and events together.

const nav = document.getElementById("nav");
const menuBtn = document.getElementById("menu-btn");
menuBtn.onclick = () => {
  const open = nav.classList.toggle("open");
  menuBtn.setAttribute("aria-expanded", String(open));
};

(function headerSearch() {
  const form = document.getElementById("search"), input = document.getElementById("search-q");
  const box = document.getElementById("suggest");
  let links = [], active = -1;
  const close = () => { box.hidden = true; active = -1; };
  const find = latest(async (term) => {
    const s = searchWords(term);
    if (!s) return null;
    const [people, events] = await Promise.all([searchFighters(s, 6), eventList(s, "", "")]);
    return [people, events.rows.slice(0, 5)];
  });
  const render = async () => {
    const term = input.value.trim();
    if (!term) { close(); return; }
    const got = await find(term);
    if (got === undefined) return;
    if (got === null) { box.innerHTML = `<p>Type at least two letters.</p>`; box.hidden = false; return; }
    const [people, events] = got;
    box.innerHTML = (people.length ? `<span class="label">Fighters</span>` + people.map((r) =>
        `<a href="/fighter/${esc(r.fighter_id)}">${esc(r.name)}<small>${proRecord(r, r)}</small></a>`).join("") : "") +
      (events.length ? `<span class="label">Events</span>` + events.map((e) =>
        `<a href="/event/${esc(e.event_id)}">${esc(e.name)}<small>${esc(e.date)}</small></a>`).join("") : "") +
      (people.length || events.length ? "" : `<p>Nothing found.</p>`);
    links = [...box.querySelectorAll("a")];
    active = -1;
    box.hidden = false;
  };
  input.addEventListener("input", debounce(render, 200));
  input.addEventListener("focus", () => { if (input.value.trim()) render(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { close(); input.blur(); return; }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    if (!links.length) return;
    active = (active + (e.key === "ArrowDown" ? 1 : -1) + links.length) % links.length;
    links.forEach((a, i) => a.classList.toggle("active", i === active));
  });
  form.onsubmit = (e) => {
    e.preventDefault();
    const pick = links[active] || links[0];
    close();
    go(pick && !box.hidden ? pick.getAttribute("href") : `/fighters?q=${encodeURIComponent(input.value.trim())}`);
  };
  box.addEventListener("mousedown", (e) => e.preventDefault());  // keep focus while clicking a result
  box.addEventListener("click", (e) => { if (e.target.closest("a")) { close(); input.value = ""; input.blur(); } });
  input.addEventListener("blur", close);
  window.addEventListener("route", close);
})();

meta().then((m) => {
  const [e] = objs(m.latest);
  if (e) document.getElementById("latest").innerHTML = ` · Latest event: ${eventLink(e.event_id, e.name)}`;
}).catch(() => {});

// --- routing -------------------------------------------------------------------------

let page = 0;  // the page being shown; a view drops its result if another page was opened since

async function route() {
  const path = location.pathname;
  window.dispatchEvent(new Event("route"));
  const [name = "", id = ""] = path.split("/").filter(Boolean);
  const views = { "": landing, fighters, fighter, events, event, fight, leaders, query, sql: query };
  const view = views[name] || landing;
  const mine = ++page;
  const show = (html) => { if (mine !== page) return false; app.innerHTML = html; return true; };
  document.body.classList.toggle("landing", view === landing);
  nav.classList.remove("open");
  menuBtn.setAttribute("aria-expanded", "false");
  const section = { fighter: "fighters", event: "events", fight: "events", sql: "query" }[name] || name;
  nav.querySelectorAll("a").forEach((a) =>
    a.toggleAttribute("aria-current", a.getAttribute("href") === `/${section}`));
  if (view !== landing) app.innerHTML = loading();
  window.scrollTo(0, 0);
  try {
    await view(decodeURIComponent(id), show);
  } catch (e) {
    show(`<p class="error">Couldn't load this page: ${esc(e.message)}</p>`);
  }
}

// --- views ---------------------------------------------------------------------------
// Each view gets the id from the URL and show(html), which draws the page unless another one
// was opened meanwhile (it returns false then, and the view stops).

async function landing(_, show) {
  if (!show(`
    <div class="stack">
      <section class="hero">
        <span class="kicker">OPEN MMA DATA</span>
        <h1>Every fight, on the record</h1>
        <p>Pro MMA from the first recorded events to last weekend: the UFC round by round, and
           results from thousands of promotions worldwide.</p>
      </section>
      <section class="tiles" id="tiles">
        ${[["fights", "Fights"], ["fighters", "Fighters"], ["promotions", "Promotions"], ["first", "First recorded event"]]
          .map(([k, label], i) => `<div class="tile"><b class="${i ? "" : "gold"}" data-k="${k}"><span class="skel">—</span></b><span>${label}</span></div>`).join("")}
      </section>
      <section class="ctas">
        <a class="btn primary" href="/fighters">Browse the database</a>
        <a class="btn" href="/query">${ICON.terminal}Ask your own question</a>
      </section>
      <section class="section">
        <span class="label">What's inside</span>
        <div class="cards">
          <a class="card" href="/fighters">${ICON.fighters}<h3>Fighters</h3>
            <p>Careers across every promotion, linked through the fights they share.</p></a>
          <a class="card" href="/events">${ICON.events}<h3>Events</h3>
            <p>Whole cards, newest first, from the UFC to regional shows.</p></a>
          <a class="card" href="/leaders">${ICON.rounds}<h3>Round by round</h3>
            <p>Strikes, takedowns and control for every round that was counted, with who counted it.</p></a>
          <a class="card" href="${RELEASES}">${ICON.open}<h3>Open data</h3>
            <p>The whole database is one free SQLite file, rebuilt daily.</p></a>
        </div>
      </section>
      <section class="card teaser">
        <div>
          <span class="soon">COMING SOON</span>
          <h3 style="font-size:20px;margin-top:12px">Ratings</h3>
          <p>One scale for all of MMA: every fighter, every promotion and every era rated against each
             other. Each rating shows how sure it is, so a thin record or a weak link between
             promotions shows up as a wider range, not false precision.</p>
        </div>
        <svg viewBox="0 0 320 120" aria-hidden="true">
          <path d="M0 108 C60 104 90 70 140 40 S220 6 250 18 S300 70 320 92 L320 40 C300 30 270 -6 250 4 S180 14 140 22 S50 60 0 70 Z" fill="#E3A63B" opacity=".2"/>
          <path d="M0 92 C60 86 95 58 140 32 S220 8 250 12 S295 48 320 66" fill="none" stroke="#E3A63B" stroke-width="2.5" stroke-linecap="round"/>
        </svg>
      </section>
      <section class="section">
        <span class="label">Latest event</span>
        <div id="latest-event">${loading()}</div>
      </section>
    </div>`)) return;
  const m = await meta();
  const c = m.stats, [e] = objs(m.latest);
  const vals = { fights: num(c.fights), fighters: num(c.fighters), promotions: num(c.promotions), first: c.first_date.slice(0, 4) };
  document.querySelectorAll("#tiles b[data-k]").forEach((b) => (b.textContent = vals[b.dataset.k]));
  const slot = document.getElementById("latest-event");
  if (slot) slot.innerHTML = e ? `<a class="card" href="/event/${esc(e.event_id)}">
      <span class="badge">${esc(e.promotion)}</span><h3>${esc(e.name)}</h3>
      <p>${esc(dot(longDate(e.date), e.location, plural(e.n, "fight")))}</p></a>` : "";
}

async function fighters(_, show) {
  const p = params();
  if (!show(`
    <div class="page-head"><h1>Fighters</h1>
      <input type="search" id="q" placeholder="Search by name" value="${esc(p.get("q") || "")}" autofocus></div>
    <div id="results">${loading()}</div>`)) return;
  const input = document.getElementById("q");
  const find = latest(async (term) => {
    if (!term) return objs((await meta()).top_fighters);
    const s = searchWords(term);
    return s ? searchFighters(s, 50) : null;
  });
  const render = async () => {
    const term = input.value.trim();
    setParams(new URLSearchParams(term ? { q: term } : {}));
    const rows = await find(term);
    if (rows === undefined) return;  // a newer search is on its way
    document.getElementById("results").innerHTML = rows === null ? nothing("Type at least two letters of a name.")
      : (term ? "" : `<span class="label" style="display:block;margin-bottom:10px">Most fights in the database</span>`) +
        (rows.length ? `<div class="list">${rows.map(fighterRow).join("")}</div>` : nothing());
  };
  input.oninput = debounce(render, 250);
  await render();
}

const TYPICAL = (d) => d && !["Other", "Catch Weight", "Open Weight"].includes(d);

async function fighter(id, show) {
  // Their file: the fighter, their fights newest first, and per fight their stat totals and
  // their opponent's (strike defense). Some sources count only what landed, so accuracy uses
  // the rounds that have attempts (sla, tla).
  const d = await api(`f/${fileKey(id)}.json`);
  if (!d) { show(nothing("Fighter not found.")); return; }
  const f = d.fighter, fights = objs(d.fights), own = objs(d.stats);

  // Record: from their UFC Stats page if they have one, else the fights here.
  const here = { w: 0, l: 0, d: 0, nc: 0 };
  fights.forEach((x) => { if (x.result) here[x.result.toLowerCase()]++; });
  const r = f.wins != null ? { w: f.wins, l: f.losses, d: f.draws, nc: f.no_contests } : here;
  const kinds = { ko: 0, sub: 0, dec: 0 };
  fights.filter((x) => x.result === "W").forEach((x) => { const k = winKind(x.method); if (k in kinds) kinds[k]++; });

  const age = f.dob ? Math.floor((Date.now() - new Date(f.dob)) / 3.15576e10) : null;
  const division = (fights.find((x) => TYPICAL(x.division)) || {}).division;
  const facts = [
    age != null && `Age ${age}`,
    f.height_in && `${Math.floor(f.height_in / 12)}′${f.height_in % 12}″`,
    f.reach_in && `${f.reach_in}″ reach`,
    f.stance && esc(f.stance),
    f.nationality && esc(f.nationality),
  ].filter(Boolean);
  const links = (isUfcStats(id) ? [ext(`http://ufcstats.com/fighter-details/${id}`, "UFC Stats")] : [])
    .concat(d.sherdog.map((s) => ext(`https://www.sherdog.com/fighter/x-${s}`, "Sherdog")));

  const total = r.w + r.l + r.d || 1;
  const recordBlock = `
    <section class="section">
      <span class="label">${f.wins != null ? "Pro record" : "Record in the database"}</span>
      <div class="big-record num"><span class="gold">${r.w}</span><span class="sep">–</span>${r.l}<span class="sep">–</span>${r.d}${
        r.nc ? `<small>${r.nc} NC</small>` : ""}</div>
      <div class="split">${r.w ? `<span style="width:${(100 * r.w) / total}%;background:var(--gold)"></span>` : ""}${
        r.l ? `<span style="width:${(100 * r.l) / total}%;background:var(--slate-loss)"></span>` : ""}${
        r.d ? `<span class="d" style="width:${(100 * r.d) / total}%"></span>` : ""}</div>
      <div class="legend"><span><i style="background:var(--gold)"></i>${plural(r.w, "win")}</span>
        <span><i style="background:var(--slate-loss)"></i>${plural(r.l, "loss", "losses")}</span>
        ${r.d ? `<span><i style="border:1px solid var(--text-muted)"></i>${plural(r.d, "draw")}</span>` : ""}</div>
    </section>`;
  const winsHere = here.w;
  const howBlock = winsHere ? `
    <section class="section">
      <span class="label">How they win</span>
      <div class="grid4">
        <div class="tile"><b class="num">${kinds.ko}</b><span>KO/TKO</span></div>
        <div class="tile"><b class="num">${kinds.sub}</b><span>Submission</span></div>
        <div class="tile"><b class="num">${kinds.dec}</b><span>Decision</span></div>
        <div class="tile gold"><b class="num">${Math.round((100 * (kinds.ko + kinds.sub)) / winsHere)}%</b><span>Finish Rate</span></div>
      </div>
      ${winsHere !== r.w ? `<p class="note">From the ${plural(winsHere, "win")} in the database.</p>` : ""}
    </section>` : "";

  // Career stats per source (they count differently), the source with the most fights first.
  const bySource = {};
  const fightOf = Object.fromEntries(fights.map((x) => [x.fight_id, x]));
  for (const row of own) (bySource[row.source] ||= []).push(row);
  const sources = Object.keys(bySource).sort((a, b) => bySource[b].length - bySource[a].length);
  const statsFor = (src) => {
    const rows = bySource[src];
    const t = {}, seen = {};
    let secs = 0, slTimed = 0, os = 0, oa = 0;
    for (const row of rows) {
      for (const k of ["kd", "sl", "sa", "sla", "tl", "ta", "tla", "sub", "ctrl"]) {
        if (row[k] != null) { t[k] = (t[k] || 0) + row[k]; seen[k] = true; }
      }
      const x = fightOf[row.fight_id];
      const s = x && fightSeconds(x.time_format, x.end_round, x.end_time);
      if (s && row.sl != null) { secs += s; slTimed += row.sl; }
      if (row.opp_sa != null) { os += row.opp_sl || 0; oa += row.opp_sa; }
    }
    const tiles = [
      secs && ["Sig. Strikes / Min", (slTimed / (secs / 60)).toFixed(2)],
      seen.sa && pct(t.sla, t.sa) && ["Sig. Strike Accuracy", pct(t.sla, t.sa)],
      oa && ["Sig. Strike Defense", pct(oa - os, oa)],
      seen.tl && ["Takedowns", `${t.tl}${seen.ta && t.ta ? ` <small class="muted" style="font:500 14px var(--sans)">${pct(t.tla, t.ta)}</small>` : ""}`],
      seen.kd && ["Knockdowns", t.kd],
      seen.sub && ["Sub. Attempts", t.sub],
      seen.ctrl && ["Control Time", hmm(t.ctrl)],
    ].filter(Boolean);
    return `<div class="grid4">${tiles.map(([label, v], i) =>
        `<div class="tile${i ? "" : " gold"}"><b class="num">${v}</b><span>${label}</span></div>`).join("")}</div>
      <p class="note">From the ${plural(rows.length, "fight")} ${esc(SOURCE_NAMES[src] || src)} counted.
        ${STATS_NOTES[src] ? esc(STATS_NOTES[src]) : ""}</p>`;
  };
  const statsBlock = sources.length ? `
    <section class="section">
      <div class="block-head"><span class="label">Stats</span>${sources.length === 1 ? badge(SOURCE_NAMES[sources[0]] || sources[0]) : ""}</div>
      ${sources.length > 1 ? `<div class="chips" id="src" style="margin-bottom:10px">${sources.map((s, i) =>
        `<button class="chip" data-src="${esc(s)}" aria-pressed="${i === 0}">${esc(SOURCE_NAMES[s] || s)}</button>`).join("")}</div>` : ""}
      <div id="stats">${statsFor(sources[0])}</div>
    </section>` : "";

  const history = fights.map((x) => fightRow(
    `data-fight="${esc(x.fight_id)}" data-event="${esc(x.event_id)}" data-left="${esc(id)}" data-opp="${esc(x.opp_id)}" data-opp-name="${esc(x.opp)}"`,
    `${res(x.result)}
     <div class="main">
       <div class="title">${x.opp_id ? fighterLink(x.opp_id, x.opp) : "Unknown"} ${x.title_fight ? badge("Title") : ""}</div>
       <div class="sub">${esc(dot(x.method, x.end_round && `R${x.end_round} ${x.end_time || ""}`.trim(), monthYear(x.date)))}
         · ${eventLink(x.event_id, x.event)}</div>
     </div>`, x.has_stats)).join("");

  if (!show(`
    <div class="fighter-layout">
      <aside class="stack">
        <section class="person">
          <h1>${esc(f.name)}</h1>
          ${f.nickname || division ? `<p class="sub">${esc(dot(f.nickname && `“${f.nickname}”`, division))}</p>` : ""}
          ${facts.length ? `<div class="facts">${facts.map((x) => `<span class="fact">${x}</span>`).join("")}</div>` : ""}
        </section>
        ${recordBlock}${howBlock}${statsBlock}
      </aside>
      <section class="section stack-top">
        <span class="label">Fight history</span>
        <div class="list">${history || nothing("No fights in the database.")}</div>
        ${sourceLine(links)}
      </section>
    </div>`)) return;
  expandable(app, (d) => ({ [id]: f.name, [d.dataset.opp]: d.dataset.oppName }));
  document.querySelectorAll("#src .chip").forEach((b) => (b.onclick = () => {
    document.querySelectorAll("#src .chip").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    document.getElementById("stats").innerHTML = statsFor(b.dataset.src);
  }));
}

const EVENTS_SHOWN = 300;  // a page of the list; "Show more" adds the next

// A chip around a native select: the label, then the chosen option's text.
const chip = (id, label, options, cur) => {
  const on = options.find(([v]) => String(v) === String(cur)) || options[0];
  return `<label class="chip${String(on[0]) !== String(options[0][0]) ? " on" : ""}">${label}<b>${esc(on[1])}</b>
    <select id="${id}" aria-label="${label}">${options.map(([v, t]) =>
      `<option value="${esc(v)}"${String(v) === String(on[0]) ? " selected" : ""}>${esc(t)}</option>`).join("")}</select></label>`;
};
const syncChip = (sel) => {
  const label = sel.closest(".chip");
  label.querySelector("b").textContent = sel.selectedOptions[0]?.text || "";
  label.classList.toggle("on", sel.selectedIndex > 0);
};

async function events(_, show) {
  const [c, promos] = await Promise.all([meta().then((m) => m.stats), promotions()]);
  const years = [];
  for (let y = Number(c.last_date.slice(0, 4)); y >= Number(c.first_date.slice(0, 4)); y--) years.push([String(y), String(y)]);
  const p = params();
  if (!show(`
    <div class="page-head"><h1>Events</h1>
      <input type="search" id="q" placeholder="Search events or places" value="${esc(p.get("q") || "")}">
      <div class="chips">
        ${chip("y", "Year", [["", "All"], ...years], p.get("y") || "")}
        ${chip("pr", "Promotion", [["", "All"], ...promos.map((x) => [x.promotion, x.promotion])], p.get("pr") || "")}
      </div>
    </div>
    <div id="results">${loading()}</div>`)) return;
  const qi = document.getElementById("q"), yi = document.getElementById("y"), pi = document.getElementById("pr");
  const find = latest(async (term, year, promo) => {
    const s = term ? searchWords(term) : null;
    if (term && !s) return null;
    return eventList(s, year, promo);
  });
  let list, shown;
  const draw = () => {
    const out = document.getElementById("results");
    out.innerHTML = `<p class="note" style="margin:0 0 10px">${plural(list.total, "event")}</p>` +
      (list.rows.length ? `<div class="list">${list.rows.slice(0, shown).map(eventRow).join("")}</div>` : nothing()) +
      (list.rows.length > shown || list.canMore ? `<button class="btn more" id="more">Show more</button>` : "");
    const btn = document.getElementById("more");
    if (btn) btn.onclick = async () => {
      btn.disabled = true;
      shown += EVENTS_SHOWN;
      while (list.rows.length < shown && (list.canMore = await list.more()));
      draw();
    };
  };
  const render = async () => {
    const term = qi.value.trim(), year = yi.value, promo = pi.value;
    [yi, pi].forEach(syncChip);
    setParams(new URLSearchParams({ ...(term && { q: term }), ...(year && { y: year }), ...(promo && { pr: promo }) }));
    const got = await find(term, year, promo);
    if (got === undefined) return;
    if (got === null) { document.getElementById("results").innerHTML = nothing("Type at least two letters."); return; }
    list = got;
    shown = EVENTS_SHOWN;
    list.canMore = !(term || year || promo);  // only the whole list reads on, a year at a time
    while (list.rows.length < shown && (list.canMore = await list.more()));
    draw();
  };
  qi.oninput = debounce(render, 250);
  yi.onchange = pi.onchange = render;
  await render();
}

async function event(id, show) {
  const d = await eventFile(id);
  if (!d) { show(nothing("Event not found.")); return; }
  const e = d.event, fights = objs(d.fights), links = eventSources(id, d.aliases);
  // The whole card is here if UFC Stats covers the promotion's cards or the card was read whole.
  const whole = (e.coverage === "full" && e.source === "ufcstats") || e.card_complete === 1;
  // The winner's side goes left (gold); with no winner, the first fighter listed.
  const left = (x) => (x.b_res === "W" ? "b" : "a");
  const bouts = fights.map((x) => {
    const l = left(x), r = l === "a" ? "b" : "a";
    const name = (s) => x[`${s}_id`]
      ? fighterLink(x[`${s}_id`], x[`${s}_name`], x[`${s}_res`] === "W" ? "won" : "") : "Unknown";
    return fightRow(
      `data-fight="${esc(x.fight_id)}" data-event="${esc(id)}" data-left="${esc(x[`${l}_id`])}" data-names="${esc(JSON.stringify({ [x.a_id]: x.a_name, [x.b_id]: x.b_name }))}"`,
      `<div class="main">
         <div class="title">${name(l)} <span class="faint">vs</span> ${name(r)} ${x.title_fight ? badge("Title") : ""}</div>
         <div class="sub">${esc(dot(x.a_res === "D" ? "Draw" : x.a_res === "NC" ? "No contest" : "", x.method,
           x.end_round && `R${x.end_round} ${x.end_time || ""}`.trim(), x.weight_class && x.division !== "Other" ? x.division : x.weight_class))}
           · <a href="${fightLink(x.fight_id, id)}">Details</a></div>
       </div>`, x.has_stats);
  }).join("");
  if (!show(`
    <div class="stack">
      <section>
        ${badge(e.promotion)}
        <h1 style="margin-top:10px">${esc(e.name)}</h1>
        <p class="muted" style="margin-top:6px">${esc(dot(longDate(e.date), e.location))}</p>
        ${whole ? "" : `<p class="note">Partial card: only the bouts of fighters this database follows.</p>`}
      </section>
      <section class="section">
        <span class="label">The card</span>
        <div class="list">${bouts || nothing("No fights recorded.")}</div>
        ${sourceLine(links)}
      </section>
    </div>`)) return;
  expandable(app, (d) => JSON.parse(d.dataset.names));
}

// Every stat of a fight: [label, landed (or the count), attempted, format].
const ROUND_ROWS = [
  ["Knockdowns", "knockdowns"], ["Sig. Strikes", "sig_str_land", "sig_str_att"],
  ["Total Strikes", "total_str_land", "total_str_att"], ["Takedowns", "td_land", "td_att"],
  ["Sub. Attempts", "sub_att"], ["Reversals", "reversals"], ["Control Time", "ctrl_sec", null, mmss],
  ["Head", "head_land", "head_att"], ["Body", "body_land", "body_att"], ["Leg", "leg_land", "leg_att"],
  ["Distance", "dist_land", "dist_att"], ["Clinch", "clinch_land", "clinch_att"], ["Ground", "ground_land", "ground_att"],
];
const SMARTCAGE_ROWS = [
  ["Knockdowns", "knockdowns"], ["Total Strikes", "total_str_land", "total_str_att"],
  ["Arm Strikes", "arm_land", "arm_att"], ["Leg Strikes", "leg_land", "leg_att"],
  ["Ground Strikes", "ground_land", "ground_att"], ["Power Strikes", "power_land"],
  ["Takedowns", "td_land", "td_att"], ["Sub. Attempts", "sub_att"], ["Dominant Positions", "dominant_positions"],
  ["Time on the Ground", "ground_sec", null, mmss], ["Time Standing", "standing_sec", null, mmss],
];

async function fight(id, show) {
  // Everything is in its event's file; a link without the event (?e=) looks it up in fx/.
  const eid = params().get("e") || (await api(`fx/${shard(id)}.json`))?.[id];
  const d = eid && await eventFile(eid);
  const x = d && objs(d.fights).find((f) => f.fight_id === id);
  if (!x) { show(nothing("Fight not found.")); return; }
  Object.assign(x, { event_id: eid, event: d.event.name, date: d.event.date });
  const st = roundStats(d, id);
  const scores = objs({ cols: d.scores.cols, rows: d.scores.fights[id] || [] });
  const odds = objs({ cols: d.odds.cols, rows: d.odds.fights[id] || [] });
  // The winner goes left, in gold; with no winner, the first fighter listed.
  const sides = [{ id: x.a_id, name: x.a_name, res: x.a_res }, { id: x.b_id, name: x.b_name, res: x.b_res }];
  if (x.b_res === "W") sides.reverse();
  const [L, R] = sides;
  const links = isUfcStats(id) ? [ext(`http://ufcstats.com/fight-details/${id}`, "UFC Stats")] : [];
  eventSources(eid, d.aliases).filter((l) => !l.includes("ufcstats.com")).forEach((l) => links.push(l));
  const outcome = L.res === "W" ? `${esc(L.name)} wins` : L.res === "D" ? "Draw" : L.res === "NC" ? "No contest" : "";
  const nameCls = (s) => (s.res === "W" ? "won" : "lost");

  // Judges' cards, one per judge: each round as the left fighter's score – the right's.
  const judges = [...new Set(scores.map((s) => s.judge))];
  const card = (j) => {
    const rows = scores.filter((s) => s.judge === j);
    const rounds = [...new Set(rows.map((s) => s.round))];
    const sc = (fid, n) => rows.find((s) => s.fighter_id === fid && s.round === n);
    const tot = (fid) => rows.filter((s) => s.fighter_id === fid).reduce((t, s) => t + (s.score ?? 0), 0);
    return `<div class="scorecard"><div class="judge">${esc(j)}</div>
      ${rounds.map((n) => {
        const a = sc(L.id, n), b = sc(R.id, n);
        const ded = (s) => (s?.deduction ? ` <small class="faint">(−${s.deduction})</small>` : "");
        return `<div class="line"><span class="muted">Round ${n}</span><span>${a?.score ?? "—"}${ded(a)} – ${b?.score ?? "—"}${ded(b)}</span></div>`;
      }).join("")}
      <div class="line total"><span>Total</span><span>${tot(L.id)} – ${tot(R.id)}</span></div></div>`;
  };
  const line = (fid) => odds.find((o) => o.fighter_id === fid);
  const oddsTile = (s) => {
    const o = line(s.id);
    if (!o) return "";
    const close = o.close_low != null ? (o.close_low === o.close_high ? american(o.close_low)
      : `${american(o.close_low)} to ${american(o.close_high)}`) : american(o.close);
    return `<div class="tile"><span>${esc(s.name)}</span><b class="num">${close ?? "—"}</b>
      <span>${o.open != null ? `Opened ${american(o.open)}` : ""}</span></div>`;
  };

  if (!show(`
    <div class="stack">
      <section>
        <p class="muted">${eventLink(x.event_id, x.event)} · ${esc(longDate(x.date))}</p>
        <div class="versus" style="margin-top:16px">
          <div class="side">${L.id ? `<a class="name ${nameCls(L)}" href="/fighter/${esc(L.id)}">${esc(L.name)}</a>` : ""}</div>
          <div class="vs">VS</div>
          <div class="side">${R.id ? `<a class="name ${nameCls(R)}" href="/fighter/${esc(R.id)}">${esc(R.name)}</a>` : ""}</div>
        </div>
        <div class="outcome">
          ${outcome ? `<b>${outcome}</b><br>` : ""}
          <span class="muted">${esc(dot(x.method, x.end_round && `Round ${x.end_round}, ${x.end_time || ""}`, x.time_format))}</span>
          <div class="note">${esc(dot(x.weight_class, x.referee && `Referee ${x.referee}`))} ${x.title_fight ? badge("Title") : ""}</div>
          ${x.details ? `<div class="note">${esc(x.details)}</div>` : ""}
        </div>
      </section>
      ${st ? `<section class="section">
        <div class="block-head"><span class="label">Round by round</span>${badge(SOURCE_NAMES[st.source] || st.source)}</div>
        <div class="chips" id="rounds" style="margin-bottom:14px">${["t", ...new Set(st.rows.map((r) => r.round))].map((n, i) =>
          `<button class="chip" data-r="${n}" aria-pressed="${i === 0}">${n === "t" ? "Totals" : `Round ${n}`}</button>`).join("")}</div>
        <div class="who"><span class="gold">${esc(L.name || "")}</span><span class="muted">${esc(R.name || "")}</span></div>
        <div class="mirror" id="bars"></div>
        ${STATS_NOTES[st.source] ? `<p class="note">${esc(STATS_NOTES[st.source])}</p>` : ""}
      </section>` : `<p class="muted">No round-by-round stats recorded for this fight.</p>`}
      ${judges.length ? `<section class="section"><div class="block-head"><span class="label">Judges' scorecards</span>${badge("UFC")}</div>
        <div class="scorecards">${judges.map(card).join("")}</div></section>` : ""}
      ${odds.length ? `<section class="section"><div class="block-head"><span class="label">Closing odds</span>${badge("BestFightOdds")}</div>
        <div class="odds">${oddsTile(L)}${oddsTile(R)}</div>
        <p class="note">Range of closing lines across books, American odds.</p></section>` : ""}
      ${sourceLine(links)}
    </div>`) || !st) return;

  // Totals: sum each column over rounds; stays NULL if every round is NULL.
  const total = (fid) => {
    const out = {};
    for (const r of st.rows.filter((r) => r.fighter_id === fid)) {
      for (const [k, v] of Object.entries(r)) {
        if (typeof v === "number" && k !== "round") out[k] = (out[k] ?? 0) + v;
        else if (!(k in out)) out[k] = null;
      }
    }
    return out;
  };
  const pick = (n, fid) => (n === "t" ? total(fid) : st.rows.find((r) => r.round === Number(n) && r.fighter_id === fid) || {});
  const render = (n) => {
    const a = pick(n, L.id), b = pick(n, R.id);
    redraw(document.getElementById("bars"), (st.cage ? SMARTCAGE_ROWS : ROUND_ROWS).map(([label, l, att, fmt]) => {
      // Sofascore counts total strikes thrown only: then the bar is what was thrown.
      if (att && a[l] == null && b[l] == null) {
        return mirrorRow(`${label} Thrown`, [a[att], null], [b[att], null], num, null, l);
      }
      // Sources that count only what landed have no attempts: one solid bar, like a count.
      const has = att && (a[att] != null || b[att] != null);
      return mirrorRow(label, [a[l], has ? a[att] : null], [b[l], has ? b[att] : null], fmt || num,
        has ? [landed(a[l], a[att]), landed(b[l], b[att])] : null, l);
    }).join(""));
  };
  document.querySelectorAll("#rounds .chip").forEach((btn) => (btn.onclick = () => {
    document.querySelectorAll("#rounds .chip").forEach((c) => c.setAttribute("aria-pressed", String(c === btn)));
    render(btn.dataset.r);
  }));
  render("t");
}

// Leaderboards, one board per source of numbers: results come from every fight, and each
// stats source (they count differently) ranks only its own fights, with the stats it has.
// The boards and their stats (SQL over per-fighter totals: n, w, fin, ko, subw, and the stats
// columns) are defined in scripts/site_json.py and come in meta.json.

// The top 50 for a board, stat and filters, from the fights themselves (the opening view is a
// file: l/<board>/<stat>.json). `promo` and `div` are names ("" for all), `from`/`to` years.
function leaderQuery(b, s, f, promo, div, m) {
  const where = ["e.date IS NOT NULL"], args = [];
  if (promo) { where.push("e.promotion = ?"); args.push(promo); }
  if (div) { where.push("division(x.weight_class) = ?"); args.push(div); }
  where.push("e.date >= ? AND e.date < ?");
  args.push(f.from, String(Number(f.to) + 1));
  // ILIKE: Sherdog writes some methods in lower case ("submission (armbar)").
  const won = "p.result = 'W'", ko = "x.method ILIKE '%KO%'", sub = "x.method ILIKE '%Submission%'";
  const results = `COUNT(*) n, count_if(${won}) w, count_if(${won} AND (${ko} OR ${sub})) fin,
                   count_if(${won} AND ${ko}) ko, count_if(${won} AND ${sub}) subw`;
  let from = "fight_participants p", stats = "", source = [];
  if (b.source) {  // everyone in each fight this source counted, with their totals if they have rows
    const [table, src] = b.source, cols = m.sums[table];  // (a fighter with nothing in any round can have none)
    from = `fight_participants p JOIN (SELECT DISTINCT fight_id FROM ${table} WHERE source = ?) c ON c.fight_id = p.fight_id
            LEFT JOIN (SELECT fight_id, fighter_id, ${Object.entries(cols).map(([k, c]) => `SUM(${c}) ${k}`).join(", ")}
             FROM ${table} WHERE source = ? GROUP BY ALL) r ON r.fight_id = p.fight_id AND r.fighter_id = p.fighter_id`;
    source = [src, src];
    stats = ", " + m.stats_cols.map((k) => (cols[k] ? `SUM(r.${k}) ${k}` : `NULL ${k}`)).join(", ");
  }
  return [`SELECT t.fighter_id, fr.name, t.n, ${s.expr} val FROM (
             SELECT p.fighter_id, ${results}${stats}
             FROM ${from} JOIN fights x ON x.fight_id = p.fight_id JOIN events e ON e.event_id = x.event_id
             WHERE ${where.join(" AND ")} GROUP BY p.fighter_id) t
           JOIN fighters fr ON fr.fighter_id = t.fighter_id
           WHERE t.n >= ? AND ${s.expr} IS NOT NULL ${s.where ? `AND ${s.where}` : ""}
           ORDER BY val DESC, t.n LIMIT 50`,
          [...source, ...args, Number(f.min)]];
}

const MIN_FIGHTS = [1, 3, 5, 10, 15, 20, 30];

async function leaders(_, show) {
  const p = params();
  const [m, promos] = await Promise.all([meta(), promotions()]);
  const c = m.stats, divisions = objs(m.divisions), BOARDS = m.boards;
  const [minY, maxY] = [c.first_date.slice(0, 4), c.last_date.slice(0, 4)];
  const years = [];
  for (let y = Number(maxY); y >= Number(minY); y--) years.push([String(y), String(y)]);
  const code = (k) => (/^\d+$/.test(p.get(k) || "") ? p.get(k) : "0");  // a division's number
  const board0 = BOARDS[p.get("board")] ? p.get("board") : "results";
  const min0 = String(Math.max(1, Number(p.get("min")) || 1));
  const mins = MIN_FIGHTS.includes(Number(min0)) ? MIN_FIGHTS : [...MIN_FIGHTS, Number(min0)].sort((a, b) => a - b);
  const statOptions = (board) => Object.entries(BOARDS[board].stats).map(([k, s]) => [k, s.label]);
  if (!show(`
    <div class="page-head"><h1>Leaders</h1>
      <div class="chips">
        ${chip("board", "Board", Object.entries(BOARDS).map(([k, b]) => [k, b.label]), board0)}
        <span id="stat-chip">${chip("stat", "Stat", statOptions(board0), p.get("stat") || "")}</span>
        ${chip("promo", "Promotion", [["", "All"], ...promos.map((x) => [x.promotion, x.promotion])], p.get("promo") || "")}
        ${chip("div", "Division", [["0", "All"], ...divisions.map((d) => [d.code, d.division])], code("div"))}
        <span id="era">${chip("from", "From", [...years].reverse(), p.get("from") || minY)}
        ${chip("to", "To", years, p.get("to") || maxY)}</span>
        ${chip("min", "Min. fights", mins.map((n) => [String(n), String(n)]), min0)}
      </div>
    </div>
    <div id="results">${loading()}</div>`)) return;
  const val = (k) => document.getElementById(k).value;
  // A board's opening view (all promotions and years, min. 1 fight) is a file; other filters
  // query the database.
  const find = latest(async (f, stat, s, allYears) => {
    if (!f.promo && allYears && f.min === "1") {
      const top = await api(`l/${f.board}/${stat}.json`);
      return (top?.[f.div] || []).map(([fighter_id, name, n, val]) => ({ fighter_id, name, n, val }));
    }
    document.getElementById("results").innerHTML = loading("Loading the query engine…");
    const div = divisions.find((d) => String(d.code) === f.div)?.division || "";
    return q(...leaderQuery(BOARDS[f.board], s, f, f.promo, div, m));
  });
  const render = async () => {
    const f = { board: val("board"), stat: val("stat"), promo: val("promo"), div: val("div"), from: val("from"),
                to: val("to"), min: val("min") };
    app.querySelectorAll(".chip select").forEach(syncChip);
    // Chips light up when they're off their default: From/To when narrowed, Stat never.
    document.getElementById("stat").closest(".chip").classList.remove("on");
    document.getElementById("from").closest(".chip").classList.toggle("on", f.from !== minY);
    document.getElementById("to").closest(".chip").classList.toggle("on", f.to !== maxY);
    document.getElementById("board").closest(".chip").classList.toggle("on", f.board !== "results");
    const results = f.board === "results";
    setParams(new URLSearchParams(f));
    const b = BOARDS[f.board];
    const stat = b.stats[f.stat] ? f.stat : Object.keys(b.stats)[0];
    const s = b.stats[stat];
    const rows = await find(f, stat, s, f.from === minY && f.to === maxY);
    if (rows === undefined) return;
    const shown = (v) => s.fmt === "pct" ? Math.round(v * 1000) / 10 + "%" : s.fmt === "time" ? hmm(v) : num(v);
    document.getElementById("results").innerHTML = (rows.length ? `<div class="list">${rows.map((r, i) => `
        <a class="row${i ? "" : " first"}" href="/fighter/${esc(r.fighter_id)}">
          <span class="rank">${i + 1}</span>
          <div class="main"><div class="title">${esc(r.name)}</div>
            <div class="sub">${plural(r.n, "fight")}${results ? "" : " counted"}</div></div>
          <div class="end"><b class="num">${shown(r.val)}</b>${esc(s.label)}</div>
        </a>`).join("")}</div>` : nothing()) +
      `<p class="note">${esc(b.note)}</p>`;
  };
  app.querySelectorAll(".chip select").forEach((el) => (el.onchange = render));
  // Each board has its own stats: changing board refills the stat list.
  document.getElementById("board").onchange = () => {
    const cur = val("stat");
    document.getElementById("stat-chip").innerHTML = chip("stat", "Stat", statOptions(val("board")), cur);
    document.getElementById("stat").onchange = render;
    render();
  };
  await render();
}

// --- the Query page ------------------------------------------------------------------
// Ask a question, get the query from an AI assistant (the page writes the prompt: the rules
// and tables from query.json, then the question), run it, and read the results as a table
// with readable headers, formatted values and links; or write SQL directly.

// Examples, as the questions they answer. Their columns are named the way the prompt asks
// assistants to name them, with the ids the page turns into links.
const EXAMPLES = {
  "What were the latest events?": `SELECT date AS "Date", name AS "Event", promotion AS "Promotion", location AS "Location", event_id
FROM events ORDER BY date DESC LIMIT 20;`,
  "Who has the most fights in the database?": `SELECT f.name AS "Fighter", COUNT(*) AS "Fights",
       count_if(p.result = 'W') AS "Wins", count_if(p.result = 'L') AS "Losses", p.fighter_id
FROM fight_participants p JOIN fighters f USING (fighter_id)
GROUP BY ALL ORDER BY "Fights" DESC LIMIT 25;`,
  "Who lands the most significant strikes per round in the UFC?": `SELECT f.name AS "Fighter", COUNT(*) AS "Rounds",
       round(SUM(r.sig_str_land) / COUNT(*), 1) AS "Sig. Strikes Per Round", r.fighter_id
FROM round_stats r JOIN fighters f USING (fighter_id) JOIN bouts b USING (fight_id)
WHERE r.source = 'ufcstats' AND b.promotion = 'UFC'
GROUP BY ALL HAVING COUNT(*) >= 30
ORDER BY "Sig. Strikes Per Round" DESC LIMIT 25;`,
  "Which fights had the most knockdowns?": `SELECT b.date AS "Date", b.event AS "Event", f.name AS "Fighter",
       SUM(r.knockdowns) AS "Knockdowns", r.fighter_id, r.fight_id
FROM round_stats r JOIN fighters f USING (fighter_id) JOIN bouts b USING (fight_id)
GROUP BY ALL ORDER BY "Knockdowns" DESC LIMIT 20;`,
  "How has the UFC's finish rate changed?": `SELECT substr(date, 1, 4) AS "Year", COUNT(*) AS "Fights",
       round(100 * avg((method ILIKE '%KO%' OR method ILIKE '%Submission%')::INT), 1) AS "Finish %"
FROM bouts WHERE promotion = 'UFC'
GROUP BY ALL ORDER BY "Year";`,
  "How do fights end?": `SELECT method AS "Method", COUNT(*) AS "Fights" FROM fights GROUP BY ALL ORDER BY "Fights" DESC LIMIT 50;`,
  "How many fights per division?": `SELECT division AS "Division", COUNT(*) AS "Fights" FROM bouts GROUP BY ALL ORDER BY "Fights" DESC;`,
  "Which promotions have held the most events?": `SELECT promotion AS "Promotion", COUNT(*) AS "Events", MIN(date) AS "First Event", MAX(date) AS "Latest Event"
FROM events GROUP BY ALL ORDER BY "Events" DESC LIMIT 30;`,
  "Where do the round stats come from?": `SELECT source AS "Source", COUNT(DISTINCT fight_id) AS "Fights", COUNT(*) AS "Fighter Rounds"
FROM round_stats GROUP BY ALL ORDER BY "Fights" DESC;`,
};

// Headers: our own column names get their label; others are Title Cased ("sig_per_round" ->
// "Sig Per Round", "finish_pct" -> "Finish %"); a name that already has capitals or spaces
// (AS "Control Time") is shown as written.
const LABELS = {
  fighter_id: "Fighter ID", event_id: "Event ID", fight_id: "Fight ID", a_name: "Fighter A", b_name: "Fighter B",
  a_res: "Result A", b_res: "Result B", a_id: "Fighter A ID", b_id: "Fighter B ID", dob: "Date of Birth",
  height_in: "Height", reach_in: "Reach", weight_lbs: "Weight (lbs)", end_round: "Round", end_time: "Time",
  sig_str_land: "Sig. Strikes Landed", sig_str_att: "Sig. Strikes Attempted",
  total_str_land: "Total Strikes Landed", total_str_att: "Total Strikes Attempted",
  td_land: "Takedowns Landed", td_att: "Takedowns Attempted", sub_att: "Submission Attempts",
  ctrl_sec: "Control Time", kd: "Knockdowns", n: "Fights", w: "Wins", l: "Losses", d: "Draws", nc: "No Contests",
};
const UPPER = new Set(["id", "ufc", "ko", "tko", "kd", "pfl", "mma", "td", "dob", "nc"]);
function label(c) {
  if (LABELS[c]) return LABELS[c];
  if (/[A-Z ]/.test(c)) return c;
  return c.split(/[_\s]+/).filter(Boolean).map((w) => (w === "pct" ? "%" : UPPER.has(w) ? w.toUpperCase()
    : w[0].toUpperCase() + w.slice(1))).join(" ");
}
const colKey = (c) => c.toLowerCase().replace(/[^a-z0-9%]+/g, "_").replace(/^_|_$/g, "");

// How a column's values are shown, from its name and values.
function kindOf(c, values) {
  const k = colKey(c), some = values.filter((v) => v != null);
  const numeric = some.length > 0 && some.every((v) => typeof v === "number");
  if (numeric && (/(^|_)(sec|secs|seconds)$/.test(k) || /(^|_)(ctrl|control)(_|$)/.test(k))) return "time";
  if (numeric && k === "height_in") return "height";
  if (numeric && k === "reach_in") return "inches";
  if (some.length && some.every((v) => typeof v === "string" && /^\d{4}-\d\d-\d\d$/.test(v))) return "date";
  if (numeric && /(pct|percent|%)/.test(k)) return "pct";
  if (numeric && /(^|_)(acc|accuracy|rate|ratio)(_|$)/.test(k) && some.every((v) => v >= 0 && v <= 1)) return "share";
  if (numeric && k === "year") return "year";
  return numeric ? "num" : "text";
}
function shown(v, kind) {
  if (v == null) return "—";
  switch (kind) {
    case "time": {
      const s = Math.round(v);
      return s >= 3600 ? `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}` : mmss(s);
    }
    case "height": return `${Math.floor(v / 12)}′${v % 12}″`;
    case "inches": return `${v}″`;
    case "date": return longDate(v);
    case "pct": return `${Number(v).toLocaleString("en-US", { maximumFractionDigits: 1 })}%`;
    case "share": return `${(v * 100).toLocaleString("en-US", { maximumFractionDigits: 1 })}%`;
    case "year": return String(v);
    case "num": return Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 });
    default: return typeof v === "object" ? JSON.stringify(v) : String(v);
  }
}

// Ids become links: on the name beside them (fighter_id + "Fighter"), or a link of their own.
// A fight gets a "Fight" column at the end.
const ID_NAMES = { fighter_id: ["fighter", "name", "fighter_name"], a_id: ["a_name", "fighter_a"],
                   b_id: ["b_name", "fighter_b"], event_id: ["event", "event_name", "name"] };
const HREF = { fighter_id: "/fighter/", a_id: "/fighter/", b_id: "/fighter/", event_id: "/event/", fight_id: "/fight/" };
function layout(columns, rows) {
  const keys = columns.map(colKey), cols = columns.map((c, i) => ({ name: c, i, label: label(c), hidden: false, link: null }));
  for (const [id, names] of Object.entries(ID_NAMES)) {
    const at = keys.indexOf(id);
    if (at < 0) continue;
    const partner = names.map((n) => keys.indexOf(n)).find((j) => j >= 0 && !cols[j].link);
    if (partner !== undefined) { cols[partner].link = { href: HREF[id], from: at }; cols[at].hidden = true; }
    else cols[at].link = { href: HREF[id], from: at };
  }
  const fight = keys.indexOf("fight_id");
  if (fight >= 0) {
    cols[fight].hidden = true;
    cols.push({ name: "", i: fight, label: "Fight", hidden: false, link: { href: "/fight/", from: fight, text: "View →" } });
  }
  for (const c of cols) c.kind = c.link?.text ? "text" : kindOf(c.name, rows.map((r) => r[c.i]));
  return cols.filter((c) => !c.hidden);
}
const cellText = (c, r) => (c.link?.text ? c.link.text : shown(r[c.i], c.kind));
const RIGHT = new Set(["num", "pct", "share", "time"]);

function resultTable(cols, rows) {
  return `<div class="grid-out"><table><thead><tr>${cols.map((c, j) =>
      `<th data-j="${j}" class="${RIGHT.has(c.kind) ? "r" : ""}" title="Sort">${esc(c.label)}</th>`).join("")}</tr></thead>
    <tbody>${rows.slice(0, 1000).map((r) => `<tr>${cols.map((c) => {
      const v = r[c.i], cls = [RIGHT.has(c.kind) ? "r" : "", v == null ? "null" : ""].join(" ").trim();
      const text = esc(cellText(c, r));
      const id = c.link && r[c.link.from];
      return `<td class="${cls}">${id ? `<a href="${c.link.href}${esc(id)}">${text}</a>` : text}</td>`;
    }).join("")}</tr>`).join("")}</tbody></table></div>`;
}

// The table as Markdown (Reddit, Discord, GitHub), CSV, or tab-separated text (pastes into a
// spreadsheet). Markdown keeps the links, to fightcanon.com.
function asMarkdown(cols, rows) {
  const cell = (s) => String(s).replaceAll("|", "\\|").replace(/\s+/g, " ");
  const line = (cells) => `| ${cells.join(" | ")} |`;
  const body = rows.map((r) => line(cols.map((c) => {
    const id = c.link && r[c.link.from], text = cell(cellText(c, r));
    return id ? `[${text}](${location.origin}${c.link.href}${encodeURIComponent(id)})` : text;
  })));
  return [line(cols.map((c) => cell(c.label))), line(cols.map((c) => (RIGHT.has(c.kind) ? "--:" : "---"))), ...body,
          "", `*Data: [Fight Canon](${location.origin})*`].join("\n");
}
// For a spreadsheet: numbers and dates as they are (1234, 2024-11-16), empty for missing, and
// a fight's link instead of "View".
function rawText(c, r) {
  const v = r[c.i];
  if (c.link?.text) return `${location.origin}${c.link.href}${encodeURIComponent(v)}`;
  if (v == null) return "";
  return ["num", "pct", "share", "year", "date"].includes(c.kind) ? String(v) : shown(v, c.kind);
}
function asDelimited(cols, rows, sep) {
  const cell = (s) => (sep === "," && /[",\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s.replace(/[\t\n]/g, " "));
  return [cols.map((c) => cell(c.label)), ...rows.map((r) => cols.map((c) => cell(rawText(c, r))))]
    .map((cells) => cells.join(sep)).join("\n");
}
async function copyText(text, btn, done = "Copied") {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const t = document.createElement("textarea");
    t.value = text;
    document.body.appendChild(t);
    t.select();
    document.execCommand("copy");
    t.remove();
  }
  const was = btn.textContent;
  btn.textContent = done;
  setTimeout(() => (btn.textContent = was), 2000);
}

// What an assistant needs: the rules, the tables, and the question.
function prompt(help, question) {
  const tables = help.tables.map((t) => `\n${t.name}${t.about ? `: ${t.about}` : ""}\n` + t.columns.map(([n, type, about]) =>
    `  ${n}${type ? ` ${type}` : ""}${about ? ` -- ${about}` : ""}`).join("\n")).join("\n");
  return `${help.rules}\nTables:\n${tables}\n\nQuestion: ${question || "(write your question here)"}\n`;
}
const ASSISTANTS = [["ChatGPT", "https://chatgpt.com/", "?q="], ["Claude", "https://claude.ai/new", "?q="]];

async function query(_, show) {
  const p = params();
  const first = Object.values(EXAMPLES)[0];
  if (!show(`
    <div class="page-head"><h1>Query</h1>
      <p class="muted">Ask the database anything: records, streaks, stats, trends. You don't need to know SQL:
        type your question, and this page adds it to a prompt for your own AI assistant (ChatGPT, Claude or
        any other). The assistant writes the query; paste it back here and the page runs it. It runs in your
        browser, so you can't break anything.</p>
    </div>
    <section class="card ask">
      <ol class="steps">
        <li><b>Ask your question</b>
          <textarea id="question" rows="2" placeholder="e.g. Which UFC fighters have the most knockouts since 2015?">${esc(p.get("ask") || "")}</textarea></li>
        <li><b>Give the prompt to your AI assistant</b>
          <span class="muted">It explains how this database works, and ends with your question.</span>
          <div class="run-row"><button class="btn primary" id="copy-prompt" type="button" disabled>Copy prompt</button>
            ${ASSISTANTS.map(([name]) => `<a class="btn" data-ai="${name}" target="_blank" rel="noopener" aria-disabled="true">Open in ${name}</a>`).join("")}</div>
          <span class="note" id="ask-status"></span></li>
        <li><b>Paste the query it writes below, and run it</b></li>
      </ol>
    </section>
    <details class="tables-help"><summary>Tables and columns</summary><div id="tables">${loading()}</div></details>
    <div class="section examples"><span class="label">Or start from an example</span>
      <div class="chips scroll-x" id="ex" style="margin-top:8px">${Object.keys(EXAMPLES).map((k) =>
        `<button class="chip" type="button">${esc(k)}</button>`).join("")}</div></div>
    <textarea id="sqltext" class="editor" spellcheck="false" aria-label="SQL">${esc(p.get("q") || first)}</textarea>
    <div class="run-row"><button class="btn primary" id="run">Run</button><span class="note" style="margin:0">Ctrl+Enter</span>
      <span class="note" id="status" style="margin:0"></span></div>
    <div id="out"></div>
    <p class="note">For your own analysis, the whole database is a <a href="${RELEASES}">free download</a> (SQLite).</p>`)) return;
  const ta = document.getElementById("sqltext"), qa = document.getElementById("question");
  const help = api("query.json");
  const keep = () => setParams(new URLSearchParams({ ...(qa.value.trim() && { ask: qa.value.trim() }), q: ta.value }));

  // Tables and columns: click a name to put it in the query.
  help.then((h) => {
    document.getElementById("tables").innerHTML = (h?.tables || []).map((t) => `
      <div class="table-help"><button class="tname" type="button" data-ins="${esc(t.name)}">${esc(t.name)}</button>
        <p class="note">${esc(t.about)}</p>
        <ul>${t.columns.map(([n, type, about]) => `<li><button type="button" data-ins="${esc(n)}">${esc(n)}</button>
          <span class="faint">${esc(type)}</span>${about ? ` <span class="muted">${esc(about)}</span>` : ""}</li>`).join("")}</ul></div>`).join("");
    document.querySelectorAll("#tables [data-ins]").forEach((b) => (b.onclick = () => {
      const at = ta.selectionStart ?? ta.value.length;
      ta.value = ta.value.slice(0, at) + b.dataset.ins + ta.value.slice(ta.selectionEnd ?? at);
      ta.focus();
      ta.selectionStart = ta.selectionEnd = at + b.dataset.ins.length;
    }));
  }).catch(() => {});

  // The prompt's buttons work once there's a question (and the prompt's rules have loaded).
  // The assistants' buttons are plain links, kept up to date, so a phone opens the app or the
  // site, not both; a click copies the prompt as it opens.
  const askStatus = document.getElementById("ask-status"), copyBtn = document.getElementById("copy-prompt");
  const aiLinks = [...document.querySelectorAll("[data-ai]")];
  let rules = null;
  const text = () => prompt(rules, qa.value.trim());
  const sync = () => {
    const ready = Boolean(rules && qa.value.trim());
    copyBtn.disabled = !ready;
    for (const a of aiLinks) {
      const [, url, q] = ASSISTANTS.find(([n]) => n === a.dataset.ai);
      if (!ready) { a.removeAttribute("href"); a.setAttribute("aria-disabled", "true"); continue; }
      // A long prompt doesn't fit in a link: it's on the clipboard to paste instead.
      const enc = encodeURIComponent(text());
      a.dataset.filled = enc.length < 7000 ? "1" : "";
      a.href = a.dataset.filled ? url + q + enc : url;
      a.removeAttribute("aria-disabled");
    }
    askStatus.textContent = qa.value.trim() ? "" : "Type your question first.";
  };
  help.then((h) => { rules = h; sync(); }).catch(() => {});
  sync();
  const keepSoon = debounce(keep, 400);
  qa.oninput = () => { sync(); keepSoon(); };
  copyBtn.onclick = () => {
    copyText(text(), copyBtn, "Prompt copied");
    askStatus.textContent = "Now paste it into your AI assistant.";
  };
  for (const a of aiLinks) a.onclick = (e) => {
    if (!a.href) { e.preventDefault(); return; }
    copyText(text(), a, "Prompt copied");  // starts the copy now, while the click still counts
    askStatus.textContent = a.dataset.filled ? `${a.dataset.ai} opens with the prompt filled in.`
      : `Paste the prompt into ${a.dataset.ai}.`;
  };

  let current = null;  // the last result: {cols, rows, sorted, dir}, rows sorted as shown
  const out = document.getElementById("out"), status = document.getElementById("status");
  const draw = () => {
    const { cols, rows } = current;
    out.innerHTML = `<div class="run-row result-actions">
        <button class="btn small" type="button" id="copy-md">Copy as Markdown</button>
        <button class="btn small" type="button" id="copy-tsv">Copy Raw</button>
        <button class="btn small" type="button" id="csv">Download CSV</button></div>` + resultTable(cols, rows);
    out.querySelectorAll("th[data-j]").forEach((th) => (th.onclick = () => {
      const c = cols[th.dataset.j];
      const dir = current.sorted === th.dataset.j ? -current.dir : (RIGHT.has(c.kind) ? -1 : 1);
      rows.sort((a, b) => {
        const x = a[c.i], y = b[c.i];
        if (x == null) return 1;
        if (y == null) return -1;
        return (x < y ? -1 : x > y ? 1 : 0) * dir;
      });
      Object.assign(current, { sorted: th.dataset.j, dir });
      draw();
    }));
    document.getElementById("copy-md").onclick = (e) => copyText(asMarkdown(cols, rows), e.target);
    document.getElementById("copy-tsv").onclick = (e) => copyText(asDelimited(cols, rows, "\t"), e.target);
    document.getElementById("csv").onclick = () => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([asDelimited(cols, rows, ",")], { type: "text/csv" }));
      a.download = "fight-canon.csv";
      a.click();
      URL.revokeObjectURL(a.href);
    };
  };
  const run = async () => {
    keep();
    out.innerHTML = "";
    status.textContent = engine ? "Running…" : "Loading the query engine…";
    try {
      const conn = await db();
      status.textContent = "Running…";
      const t = performance.now();
      await views(conn, ta.value);
      const res = await conn.query(ta.value);
      const took = `${((performance.now() - t) / 1000).toFixed(2)} s`;
      const columns = res.schema.fields.map((f) => f.name);
      const rows = res.toArray().map((r) => columns.map((c) => plain(r[c])));
      if (!columns.length) { status.textContent = `Done in ${took}.`; return; }
      status.textContent = `${plural(rows.length, "row")}${rows.length > 1000 ? " (showing 1,000; copies and downloads have them all)" : ""} · ${took}`;
      current = { cols: layout(columns, rows), rows };
      draw();
    } catch (e) {
      status.textContent = "";
      out.innerHTML = `<div class="error" style="margin-top:12px"><p>${esc(e.message)}</p>
        <p class="note">Asking an AI assistant? Copy this and paste it back to it: it has your question, the query and the error.</p>
        <button class="btn small" type="button" id="copy-fix">Copy for your assistant</button></div>`;
      document.getElementById("copy-fix").onclick = (ev) => copyText(
        `${qa.value.trim() ? `My question: ${qa.value.trim()}\n\n` : ""}This query failed in DuckDB:\n\n${ta.value}\n\n` +
        `Error: ${e.message}\n\nPlease fix the query. Reply with only the corrected query in one code block.`, ev.target);
    }
  };
  document.getElementById("run").onclick = run;
  ta.onkeydown = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) run(); };
  document.querySelectorAll("#ex .chip").forEach((b) => (b.onclick = () => { ta.value = EXAMPLES[b.textContent]; run(); }));
  await run();
}

// Last, so every view and constant above is defined.
// Links within the site open without reloading the page (unless opened in a new tab).
document.addEventListener("click", (e) => {
  const a = e.target.closest("a[href]");
  if (!a || a.target || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  const url = new URL(a.href);
  if (url.origin !== location.origin || /\.[a-z0-9]+$/i.test(url.pathname)) return;  // a file, not a page
  e.preventDefault();
  go(url.pathname + url.search);
});
window.addEventListener("popstate", route);
// Links from before the site had paths (#/fighter/<id>) still work.
if (location.hash.startsWith("#/")) history.replaceState(null, "", location.hash.slice(1));
route();
