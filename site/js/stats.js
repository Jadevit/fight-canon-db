// Round stats, wherever they're shown: who counted them, the mirrored bars, "At a glance".

import { api, objs } from "./data.js";
import { esc, ext, ICON, loading, mmss, num } from "./ui.js";

// Links to the pages a row came from, by the sources' own ids (the aliases tables).
export const SOURCE_NAMES = { ufcstats: "UFC Stats", pfl: "PFL", oktagon: "Oktagon", sofascore: "Sofascore", sherdog: "Sherdog", smartcage: "PFL SmartCage",
  "ufc.com": "UFC", mmadecisions: "MMA Decisions" };
// What a stats source leaves out, said under its stats.
export const STATS_NOTES = {
  oktagon: "Oktagon counts only what landed: no attempts, knockdowns or control time.",
  sofascore: "A third party's count, not the promotion's. Total strikes are counted thrown, not landed.",
  smartcage: "PFL's older system counts all strikes by arm, leg and ground, not significant strikes.",
};
// `aliases`: the event file's {ufcstats: [ids], sherdog: [ids], pfl: [ids]}.
export function eventSources(aliases) {
  const links = (aliases.ufcstats || []).map((s) => ext(`http://ufcstats.com/event-details/${s}`, "UFC Stats"));
  (aliases.sherdog || []).forEach((s) => links.push(ext(`https://www.sherdog.com/events/x-${s}`, "Sherdog")));
  (aliases.pfl || []).forEach((s) => links.push(ext(`https://pflmma.com/event/${s}`, "PFL")));
  return links;
}
// Stats sources in the order they're shown (the promotions' own first), and the switch between
// them: "Counted by" and the source, a button per source when more than one counted.
const STATS_ORDER = ["ufcstats", "pfl", "smartcage", "oktagon", "sofascore"];
export const byOrder = (a, b) => STATS_ORDER.indexOf(a) - STATS_ORDER.indexOf(b);
export const srcName = (s) => SOURCE_NAMES[s] || s;
export function counted(sources, current) {
  if (sources.length < 2) return `<span class="counted">Counted by <b>${esc(srcName(sources[0]))}</b></span>`;
  return `<span class="counted"><span class="by">Counted by</span><span class="seg" role="group" aria-label="Counted by">${
    sources.map((s) => `<button data-src="${esc(s)}" aria-pressed="${s === current}">${esc(srcName(s))}</button>`).join("")}</span></span>`;
}
export function onCounted(root, pick) {
  root.querySelectorAll(".counted .seg button").forEach((b) => (b.onclick = () => {
    root.querySelectorAll(".counted .seg button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    pick(b.dataset.src);
  }));
}
export const sourceLine = (links) => links.length ? `<p class="note">Source: ${links.join(" · ")}</p>` : "";

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
export function mirrorRow(label, a, b, fmt = num, text = null, key = label) {
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
export function redraw(el, html) {
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
export const landed = (l, a) => (l == null ? (a == null ? "—" : `${a} thr.`) : `${l}${a != null ? ` <small>/${a}</small>` : ""}`);

export const eventFile = (eid) => api(`e/${eid}.json`);
export const fightLink = (fid, eid) => `/fight/${esc(fid)}?e=${encodeURIComponent(eid)}`;

// A fight's round stats from its event's file, one set per source that counted it, the
// promotion's own first: round_stats' columns, or PFL's older SmartCage set (its own columns).
export function roundStats(ev, fid) {
  const sets = ev?.stats[fid];
  return sets ? [sets].flat().map((st) => ({ rows: objs(st), source: st.source, cage: st.cage })) : [];
}

// "At a glance": strikes per round, control per round, then knockdowns and takedowns.
async function glance(fid, eid, left, names, src) {
  const sets = roundStats(await eventFile(eid), fid);
  const st = sets.find((x) => x.source === src) || sets[0];
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
    <div class="glance-head"><span class="label">At a glance</span>${counted(sets.map((x) => x.source), st.source)}</div>
    <div class="who"><span class="gold">${esc(names[left] || "")}</span><span class="muted">${esc(names[right] || "")}</span></div>
    <div class="mirror">
      ${strikes.map((p, i) => row(`${st.cage ? "Strikes" : "Sig. Strikes"}, Round ${rounds[i]}`, p)).join("")}
      ${st.cage ? "" : ctrl.map((p, i) => row(`Control, Round ${rounds[i]}`, p, mmss)).join("")}
      ${row("Knockdowns", kd)}
      ${row("Takedowns", td)}
    </div>
    <a class="go" href="${fightLink(fid, eid)}${st === sets[0] ? "" : `&src=${encodeURIComponent(st.source)}`}">Full fight page →</a>
  </div>`;
}

// Rows that expand: only fights with round stats. The panel loads when it's first opened.
export function expandable(container, names) {
  container.querySelectorAll("details[data-fight]").forEach((d) => {
    d.addEventListener("toggle", async () => {
      if (!d.open || d.dataset.loaded) return;
      d.dataset.loaded = "1";
      const slot = d.querySelector(".slot");
      slot.innerHTML = loading();
      const draw = async (src) => {
        slot.innerHTML = await glance(d.dataset.fight, d.dataset.event, Number(d.dataset.left), names(d), src);
        onCounted(slot, draw);
      };
      try {
        await draw();
      } catch (e) {
        slot.innerHTML = `<p class="error">${esc(e.message)}</p>`;
      }
    });
  });
}

export const fightRow = (statsAttrs, inner, hasStats) => hasStats
  ? `<details class="row-x" ${statsAttrs}><summary class="row">${inner}${ICON.chev}</summary><div class="slot"></div></details>`
  : `<div class="row">${inner}<span class="chev-space"></span></div>`;
