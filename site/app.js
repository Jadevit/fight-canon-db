// Fight Canon. Browsing pages read JSON files written daily by scripts/site_json.py: one per
// fighter and per event, the event lists, a search index split by prefix, and the
// leaderboards' opening views. Only the SQL page and filtered leaderboards query the database:
// DuckDB-WASM over the database as Parquet (scripts/export_parquet.py), reading only the
// columns and parts of the files a query needs; it loads the first time it's needed.
// Views are chosen by the path: / (landing), /fighters, /fighter/<id>, /events, /event/<id>,
// /fight/<id>, /leaders, /sql (the server answers every path with this page). How it looks is
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
const plain = (v) => (typeof v === "bigint" ? Number(v) : v);
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

// Search a split index (sf/ fighters, se/ events): read the file of the longest word's first
// two letters, a letter deeper while that file is split, and keep the rows in which every
// word of the term starts a word. Rows come in rank order (most fights; newest).
async function searchIndex(folder, s) {
  let p = s.first.slice(0, 2), file = await api(`${folder}/${hex(p)}.json`);
  while (file?.split && p.length < s.first.length) {
    p = s.first.slice(0, p.length + 1);
    file = await api(`${folder}/${hex(p)}.json`);
  }
  return objs(file).filter((r) => {
    const ws = r.words.split(" ");
    return s.words.every((t) => ws.some((w) => w.startsWith(t)));
  });
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
// Mirrored bars: the left side (the fighter whose page it is, or the winner) in gold, growing
// left from the middle; the other side in slate, growing right. Rows where neither side was
// recorded aren't drawn.

// `text`, if given, is what each end shows instead of fmt(value) (e.g. "12 /30").
function mirrorRow(label, a, b, max, fmt = num, text = null) {
  if (a == null && b == null) return "";
  const w = (v) => (v == null || !max ? 0 : (100 * Math.max(0, v)) / max);
  const side = (v, cls) => `<span class="${cls}${v ? "" : " zero"}" style="width:${w(v).toFixed(1)}%"></span>`;
  const end = (v, i) => (text ? text[i] : v == null ? "—" : fmt(v));
  return `<div class="cap">${label}</div>
    <div class="v l">${end(a, 0)}</div><div class="track l">${side(a, "fill-me")}</div>
    <div class="track">${side(b, "fill-them")}</div><div class="v r">${end(b, 1)}</div>`;
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
  const maxOf = (pairs) => Math.max(1, ...pairs.flat().filter((v) => v != null));
  const ms = maxOf(strikes), mc = maxOf(ctrl);
  const kd = [sum(left, "knockdowns"), sum(right, "knockdowns")], td = [sum(left, "td_land"), sum(right, "td_land")];
  return `<div class="glance">
    <div class="glance-head"><span class="label">At a glance</span>${badge(SOURCE_NAMES[st.source] || st.source)}</div>
    <div class="who"><span class="gold">${esc(names[left] || "")}</span><span class="muted">${esc(names[right] || "")}</span></div>
    <div class="mirror">
      ${strikes.map(([a, b], i) => mirrorRow(`${st.cage ? "Strikes" : "Sig. strikes"}, round ${rounds[i]}`, a, b, ms)).join("")}
      ${st.cage ? "" : ctrl.map(([a, b], i) => mirrorRow(`Control, round ${rounds[i]}`, a, b, mc, mmss)).join("")}
      ${mirrorRow("Knockdowns", kd[0], kd[1], Math.max(1, ...kd.filter((v) => v != null)))}
      ${mirrorRow("Takedowns", td[0], td[1], Math.max(1, ...td.filter((v) => v != null)))}
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
  const views = { "": landing, fighters, fighter, events, event, fight, leaders, sql };
  const view = views[name] || landing;
  const mine = ++page;
  const show = (html) => { if (mine !== page) return false; app.innerHTML = html; return true; };
  document.body.classList.toggle("landing", view === landing);
  nav.classList.remove("open");
  menuBtn.setAttribute("aria-expanded", "false");
  const section = { fighter: "fighters", event: "events", fight: "events" }[name] || name;
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
        <a class="btn" href="/sql">${ICON.terminal}Query it with SQL</a>
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
        <div class="tile gold"><b class="num">${Math.round((100 * (kinds.ko + kinds.sub)) / winsHere)}%</b><span>Finish rate</span></div>
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
      secs && ["Sig. strikes / min", (slTimed / (secs / 60)).toFixed(2)],
      seen.sa && pct(t.sla, t.sa) && ["Sig. strike accuracy", pct(t.sla, t.sa)],
      oa && ["Sig. strike defense", pct(oa - os, oa)],
      seen.tl && ["Takedowns", `${t.tl}${seen.ta && t.ta ? ` <small class="muted" style="font:500 14px var(--sans)">${pct(t.tla, t.ta)}</small>` : ""}`],
      seen.kd && ["Knockdowns", t.kd],
      seen.sub && ["Sub. attempts", t.sub],
      seen.ctrl && ["Control time", hmm(t.ctrl)],
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
  ["Knockdowns", "knockdowns"], ["Sig. strikes", "sig_str_land", "sig_str_att"],
  ["Total strikes", "total_str_land", "total_str_att"], ["Takedowns", "td_land", "td_att"],
  ["Sub. attempts", "sub_att"], ["Reversals", "reversals"], ["Control time", "ctrl_sec", null, mmss],
  ["Head", "head_land", "head_att"], ["Body", "body_land", "body_att"], ["Leg", "leg_land", "leg_att"],
  ["Distance", "dist_land", "dist_att"], ["Clinch", "clinch_land", "clinch_att"], ["Ground", "ground_land", "ground_att"],
];
const SMARTCAGE_ROWS = [
  ["Knockdowns", "knockdowns"], ["Total strikes", "total_str_land", "total_str_att"],
  ["Arm strikes", "arm_land", "arm_att"], ["Leg strikes", "leg_land", "leg_att"],
  ["Ground strikes", "ground_land", "ground_att"], ["Power strikes", "power_land"],
  ["Takedowns", "td_land", "td_att"], ["Sub. attempts", "sub_att"], ["Dominant positions", "dominant_positions"],
  ["Time on the ground", "ground_sec", null, mmss], ["Time standing", "standing_sec", null, mmss],
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
    document.getElementById("bars").innerHTML = (st.cage ? SMARTCAGE_ROWS : ROUND_ROWS).map(([label, l, att, fmt]) => {
      // Sofascore counts total strikes thrown only: then the bar is what was thrown.
      if (att && a[l] == null && b[l] == null && (a[att] != null || b[att] != null)) {
        return mirrorRow(`${label} thrown`, a[att], b[att], Math.max(1, a[att] || 0, b[att] || 0));
      }
      const max = Math.max(1, a[l] || 0, b[l] || 0);
      return mirrorRow(label, a[l], b[l], max, fmt || num,
        att ? [landed(a[l], a[att]), landed(b[l], b[att])] : null);
    }).join("");
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

const EXAMPLES = {
  "Latest events": "SELECT date, name, location, promotion FROM events ORDER BY date DESC LIMIT 20;",
  "The most active fighter's fights": `SELECT date, event, method, a_name, a_res, b_name, b_res, fight_id
FROM bouts
WHERE (SELECT fighter_id FROM fight_participants GROUP BY ALL ORDER BY count(*) DESC LIMIT 1) IN (a_id, b_id)
ORDER BY date;`,
  "Most knockdowns in one fight": `SELECT f.name, b.event, b.date, SUM(r.knockdowns) AS kd, r.fight_id
FROM round_stats r
JOIN fighters f USING (fighter_id)
JOIN bouts b USING (fight_id)
GROUP BY ALL
ORDER BY kd DESC LIMIT 20;`,
  "Highest strike output per round": `SELECT b.event, b.date, f.name, COUNT(*) AS rounds,
       round(SUM(r.sig_str_land) / COUNT(*), 1) AS sig_per_round, r.fight_id
FROM round_stats r JOIN fighters f USING (fighter_id) JOIN bouts b USING (fight_id)
WHERE r.source = 'ufcstats'
GROUP BY ALL HAVING COUNT(*) >= 3
ORDER BY sig_per_round DESC LIMIT 25;`,
  "UFC finish rate by year": `SELECT substr(date, 1, 4) AS year, COUNT(*) AS fights,
       round(100 * avg((method LIKE '%KO%' OR method LIKE '%Submission%')::INT), 1) AS finish_pct
FROM bouts WHERE promotion = 'UFC'
GROUP BY ALL ORDER BY year;`,
  "Where round stats come from": "SELECT source, COUNT(DISTINCT fight_id) AS fights, COUNT(*) AS fighter_rounds FROM round_stats GROUP BY ALL ORDER BY fights DESC;",
  "Promotions with the most events": "SELECT promotion, COUNT(*) AS events FROM events GROUP BY ALL ORDER BY events DESC LIMIT 30;",
  "How fights end": "SELECT method, COUNT(*) AS fights FROM fights GROUP BY ALL ORDER BY fights DESC;",
  "Fights per division": "SELECT division, COUNT(*) AS fights FROM bouts GROUP BY ALL ORDER BY fights DESC;",
  "All tables and columns": "SELECT table_name, column_name, data_type FROM information_schema.columns ORDER BY table_name, ordinal_position;",
};

async function sql(_, show) {
  const first = Object.values(EXAMPLES)[0];
  if (!show(`
    <div class="page-head"><h1>SQL</h1>
      <p class="muted">Query the whole database directly, with <a href="https://duckdb.org/docs/sql/introduction">DuckDB's SQL</a>.
        It runs in your browser, so you can't break anything, and it reads only the columns and parts of
        the database a query needs, so even queries over every fight take a moment. For heavy work,
        <a href="${RELEASES}">download the database</a> (SQLite).</p>
      <p class="note" style="margin:0">The tables are the database's own. <code>bouts</code> is each fight with its
        event, both fighters and its <code>division</code>; <code>division(weight_class)</code> names the
        division of any bout title. Columns named <code>fighter_id</code>, <code>event_id</code> or
        <code>fight_id</code> become links. The query engine loads the first time you run something.</p>
    </div>
    <div class="chips scroll-x" id="ex" style="margin-bottom:12px">${Object.keys(EXAMPLES).map((k) =>
      `<button class="chip" type="button">${esc(k)}</button>`).join("")}</div>
    <textarea id="sqltext" class="editor" spellcheck="false" aria-label="SQL">${esc(params().get("q") || first)}</textarea>
    <div class="run-row"><button class="btn primary" id="run">Run</button><span class="note" style="margin:0">Ctrl+Enter</span>
      <span class="note" id="status" style="margin:0"></span></div>
    <div id="out"></div>`)) return;
  const ta = document.getElementById("sqltext");
  const run = async () => {
    const out = document.getElementById("out"), status = document.getElementById("status");
    setParams(new URLSearchParams({ q: ta.value }));
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
      const values = res.toArray().map((r) => columns.map((c) => plain(r[c])));
      if (!columns.length) { status.textContent = `Done in ${took}.`; return; }
      const cell = (c, v) => v == null ? `<td class="null">NULL</td>`
        : `<td>${c === "fighter_id" ? fighterLink(v, v) : c === "event_id" ? eventLink(v, v)
          : c === "fight_id" ? `<a href="/fight/${esc(v)}">${esc(v)}</a>` : esc(typeof v === "object" ? JSON.stringify(v) : v)}</td>`;
      status.textContent = `${plural(values.length, "row")}${values.length > 1000 ? " (showing 1,000)" : ""} · ${took}`;
      out.innerHTML = `<div class="grid-out"><table><thead><tr>${columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
        <tbody>${values.slice(0, 1000).map((row) => `<tr>${row.map((v, i) => cell(columns[i], v)).join("")}</tr>`).join("")}</tbody></table></div>`;
    } catch (e) {
      status.textContent = "";
      out.innerHTML = `<p class="error" style="margin-top:12px">${esc(e.message)}</p>`;
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
