// Leaderboards, one board per source of numbers: results come from every fight, and each
// stats source (they count differently) ranks only its own fights, with the stats it has.
// The boards and their stats (SQL over per-fighter totals: n, w, fin, ko, subw, and the stats
// columns) are defined in scripts/site_json.py and come in meta.json.

import { api, meta, objs, promotions } from "./data.js";
import { q } from "./duck.js";
import {
  app, chip, esc, fighterHref, hmm, latest, loading, nothing, num, params, plural, setParams, syncChip
} from "./ui.js";

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

export async function leaders(_, show) {
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
    app().querySelectorAll(".chip select").forEach(syncChip);
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
        <a class="row${i ? "" : " first"}" href="${fighterHref(r.fighter_id, r.name)}">
          <span class="rank">${i + 1}</span>
          <div class="main"><div class="title">${esc(r.name)}</div>
            <div class="sub">${plural(r.n, "fight")}${results ? "" : " counted"}</div></div>
          <div class="end"><b class="num">${shown(r.val)}</b>${esc(s.label)}</div>
        </a>`).join("")}</div>` : nothing()) +
      `<p class="note">${esc(b.note)}</p>`;
  };
  app().querySelectorAll(".chip select").forEach((el) => (el.onchange = render));
  // Each board has its own stats: changing board refills the stat list.
  document.getElementById("board").onchange = () => {
    const cur = val("stat");
    document.getElementById("stat-chip").innerHTML = chip("stat", "Stat", statOptions(val("board")), cur);
    document.getElementById("stat").onchange = render;
    render();
  };
  await render();
}
