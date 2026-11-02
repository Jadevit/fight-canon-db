// The Fighters page and a fighter's page.

import { api, meta, objs } from "./data.js";
import { searchFighters, searchWords } from "./search.js";
import {
  byOrder, counted, expandable, fightRow, onCounted, SOURCE_NAMES, sourceLine, STATS_NOTES
} from "./stats.js";
import {
  app, badge, canonical, debounce, dot, esc, eventLink, ext, fighterHref, fighterLink, go, hmm, loading,
  monthYear, nothing, num, pagePath, params, pct, plural, proRecord, res, setParams
} from "./ui.js";

// How a win came: UFC Stats writes "KO/TKO", "Submission", "Decision - Split"; Sherdog writes
// "TKO (Punches)", "Submission (Armbar)", "Decision (Unanimous)".
export function winKind(method) {
  const m = (method || "").toLowerCase();
  if (/^t?ko\b/.test(m)) return "ko";
  if (m.includes("submission")) return "sub";
  if (m.includes("decision")) return "dec";
  return "other";
}

// Seconds actually fought, from "3 Rnd (5-5-5)" + end round + end time.
export function fightSeconds(timeFormat, endRound, endTime) {
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

const fighterRow = (r) => `
  <a class="row" href="${fighterHref(r.fighter_id, r.name)}">
    <div class="main"><div class="title">${esc(r.name)}</div>
      ${r.nickname ? `<div class="sub">“${esc(r.nickname)}”</div>` : ""}</div>
    <div class="end"><b class="num">${proRecord(r, r)}</b>${plural(r.n, "fight")}</div>
  </a>`;

// Fighters opened lately, newest first, kept in this browser only ({id, name}).
const RECENT_KEY = "recent-fighters", RECENT_MAX = 8;
function recentFighters() {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY)) || []; } catch { return []; }
}
function rememberFighter(id, name) {
  try {
    const list = [{ id, name }, ...recentFighters().filter((r) => r.id !== id)].slice(0, RECENT_MAX);
    localStorage.setItem(RECENT_KEY, JSON.stringify(list));
  } catch { /* storage blocked: nothing to remember */ }
}

// Recently fought: a row per pair who fought each other, each fighter's name and record
// (wins in gold), nothing about the fight. Record as proRecord: their UFC Stats page, else here.
function recordWins(r) {
  const [w, l, d, nc] = r.wins != null ? [r.wins, r.losses, r.draws, r.no_contests] : [r.w, r.l, r.d, r.nc];
  return `<span class="w">${w || 0}</span>-${l || 0}-${d || 0}${nc ? ` (${nc} NC)` : ""}`;
}
const pairCell = (r) => `<div class="cell"><a href="${fighterHref(r.fighter_id, r.name)}">${esc(r.name)}</a>
  <div class="rec num">${recordWins(r)}</div></div>`;
const pairRow = ([a, b]) => `<div class="pair-row">${pairCell(a)}${pairCell(b)}</div>`;
const DIE = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="3.5" width="17" height="17" rx="3.5"/><circle cx="8.5" cy="8.5" r="1.1" fill="currentColor"/><circle cx="15.5" cy="15.5" r="1.1" fill="currentColor"/><circle cx="12" cy="12" r="1.1" fill="currentColor"/></svg>`;

async function randomFighter() {
  const page = await api("fighters.json");
  const ids = page && (await api(`fr/${Math.floor(Math.random() * page.random)}.json`));
  if (ids && ids.length) go(`/fighter/${ids[Math.floor(Math.random() * ids.length)]}`);
}

export async function fighters(_, show) {
  const p = params();
  if (!show(`
    <div class="fighters-home">
      <span class="kicker">FIGHTERS</span>
      <input type="search" id="q" placeholder="Search fighters" value="${esc(p.get("q") || "")}" autofocus>
      <button class="random-link" id="random">${DIE}Random fighter</button>
      <div id="results">${loading()}</div>
    </div>`)) return;
  const input = document.getElementById("q");
  document.getElementById("random").onclick = randomFighter;
  meta().then((m) => { input.placeholder = `Search ${num(m.stats.fighters)} fighters`; }).catch(() => {});
  let n = 0;  // only the latest search draws: an older one can finish last
  const browse = async (mine) => {
    const page = await api("fighters.json");
    if (mine !== n) return;
    const recent = recentFighters();
    const pairs = page ? page.matchups.map((m) => m.map((r) => Object.fromEntries(page.cols.map((c, i) => [c, r[i]])))) : [];
    document.getElementById("results").innerHTML = `<div class="stack">
      ${recent.length ? `<section class="section"><span class="label">Recently viewed</span>
        <div class="chips">${recent.map((r) => `<a class="chip link" href="${fighterHref(r.id, r.name)}">${esc(r.name)}</a>`).join("")}</div></section>` : ""}
      ${pairs.length ? `<section class="section"><span class="label">Recently fought</span>
        <div class="pairs">${pairs.map(pairRow).join("")}</div></section>` : ""}</div>`;
  };
  const render = async () => {
    const term = input.value.trim(), mine = ++n;
    setParams(new URLSearchParams(term ? { q: term } : {}));
    if (!term) return browse(mine);
    const draw = (rows) => {
      if (mine !== n) return;
      document.getElementById("results").innerHTML = rows === null ? nothing("Type at least two letters of a name.")
        : rows.length ? `<div class="list">${rows.map(fighterRow).join("")}</div>` : nothing();
    };
    const s = searchWords(term);
    draw(s ? await searchFighters(s, 50, draw) : null);
  };
  input.oninput = debounce(render, 120);
  await render();
}

const TYPICAL = (d) => d && !["Other", "Catch Weight", "Open Weight"].includes(d);

export async function fighter(id, show) {
  // Their file: the fighter, their fights newest first, and per fight their stat totals and
  // their opponent's (strike defense). Some sources count only what landed, so accuracy uses
  // the rounds that have attempts (sla, tla).
  const d = await api(`f/${id}.json`);
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
  const links = (d.aliases.ufcstats || []).map((s) => ext(`http://ufcstats.com/fighter-details/${s}`, "UFC Stats"))
    .concat((d.aliases.sherdog || []).map((s) => ext(`https://www.sherdog.com/fighter/x-${s}`, "Sherdog")));
  canonical(pagePath("fighter", id, f.name));
  rememberFighter(id, f.name);

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

  // Career stats per source (they count differently), the promotions' own first.
  const bySource = {};
  const fightOf = Object.fromEntries(fights.map((x) => [x.fight_id, x]));
  for (const row of own) (bySource[row.source] ||= []).push(row);
  const sources = Object.keys(bySource).sort(byOrder);
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
      <div class="block-head" id="src"><span class="label">Stats</span>${counted(sources, sources[0])}</div>
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
  expandable(app(), (d) => ({ [id]: f.name, [d.dataset.opp]: d.dataset.oppName }));
  const head = document.getElementById("src");
  if (head) onCounted(head, (src) => (document.getElementById("stats").innerHTML = statsFor(src)));
}
