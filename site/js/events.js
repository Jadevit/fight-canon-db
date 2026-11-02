// The Events page and an event's page.

import { api, hex, meta, objs, promotions } from "./data.js";
import { searchIndex, searchWords } from "./search.js";
import { eventFile, eventSources, expandable, fightLink, fightRow, sourceLine } from "./stats.js";
import {
  app, badge, canonical, chip, debounce, dot, esc, eventHref, fighterLink, guard, latest, loading, longDate,
  nothing, pagePath, params, plural, setParams, syncChip, yearOptions
} from "./ui.js";

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

const eventRow = (e) => `
  <a class="row" href="${eventHref(e.event_id, e.name)}">
    <div class="main"><div class="title">${esc(e.name)}</div>
      <div class="sub">${esc(dot(longDate(e.date), e.promotion, e.location))}</div></div>
    <div class="end">${plural(e.n, "fight")}</div>
  </a>`;

const EVENTS_SHOWN = 300;  // a page of the list; "Show more" adds the next

export async function events(_, show) {
  const [c, promos] = await Promise.all([meta().then((m) => m.stats), promotions()]);
  const years = yearOptions(c);
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
    if (btn) btn.onclick = guard(async () => {
      btn.disabled = true;
      shown += EVENTS_SHOWN;
      while (list.rows.length < shown && (list.canMore = await list.more()));
      draw();
    });
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
  qi.oninput = debounce(guard(render), 120);
  yi.onchange = pi.onchange = guard(render);
  await render();
}

export async function event(id, show) {
  const d = await eventFile(id);
  if (!d) { show(nothing("Event not found.")); return; }
  const e = d.event, fights = objs(d.fights), links = eventSources(d.aliases);
  canonical(pagePath("event", id, e.name), e.name);
  // The whole card is here if UFC Stats covers the promotion's cards or the card was read whole.
  const whole = (e.coverage === "full" && d.aliases.ufcstats) || e.card_complete === 1;
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
  expandable(app(), (d) => JSON.parse(d.dataset.names));
}
