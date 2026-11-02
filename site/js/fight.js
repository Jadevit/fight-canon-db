// A fight's page.

import { api, objs, shard } from "./data.js";
import {
  counted, eventFile, eventSources, landed, mirrorRow, onCounted, redraw, roundStats, sourceLine, srcName,
  STATS_NOTES
} from "./stats.js";
import {
  american, badge, canonical, dot, esc, eventLink, ext, fighterHref, longDate, mmss, nothing, num, params,
  setParams
} from "./ui.js";

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

export async function fight(id, show) {
  // Everything is in its event's file; a link without the event (?e=) looks it up in fx/.
  const eid = params().get("e") || (await api(`fx/${shard(id)}.json`))?.[id];
  const d = eid && await eventFile(eid);
  const x = d && objs(d.fights).find((f) => f.fight_id === id);
  if (!x) { show(nothing("Fight not found.")); return; }
  Object.assign(x, { event_id: eid, event: d.event.name, date: d.event.date });
  const sets = roundStats(d, id);
  let st = sets.find((x) => x.source === params().get("src")) || sets[0];
  const scores = objs({ cols: d.scores.cols, rows: d.scores.fights[id] || [] });
  const odds = objs({ cols: d.odds.cols, rows: d.odds.fights[id] || [] });
  // The winner goes left, in gold; with no winner, the first fighter listed.
  const sides = [{ id: x.a_id, name: x.a_name, res: x.a_res }, { id: x.b_id, name: x.b_name, res: x.b_res }];
  if (x.b_res === "W") sides.reverse();
  const [L, R] = sides;
  const links = d.ufcstats?.[id] ? [ext(`http://ufcstats.com/fight-details/${d.ufcstats[id]}`, "UFC Stats")] : [];
  eventSources(d.aliases).filter((l) => !l.includes("ufcstats.com")).forEach((l) => links.push(l));
  canonical(`/fight/${id}`);
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
  // Round by round, for the source picked: its rounds as chips, the bars, and what it leaves out.
  const roundsBlock = () => `
    <div class="block-head"><span class="label">Round by round</span>${counted(sets.map((x) => x.source), st.source)}</div>
    <div class="chips" id="rounds" style="margin-bottom:14px">${["t", ...new Set(st.rows.map((r) => r.round))].map((n, i) =>
      `<button class="chip" data-r="${n}" aria-pressed="${i === 0}">${n === "t" ? "Totals" : `Round ${n}`}</button>`).join("")}</div>
    <div class="who"><span class="gold">${esc(L.name || "")}</span><span class="muted">${esc(R.name || "")}</span></div>
    <div class="mirror" id="bars"></div>
    ${STATS_NOTES[st.source] ? `<p class="note">${esc(STATS_NOTES[st.source])}</p>` : ""}`;
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
          <div class="side">${L.id ? `<a class="name ${nameCls(L)}" href="${fighterHref(L.id, L.name)}">${esc(L.name)}</a>` : ""}</div>
          <div class="vs">VS</div>
          <div class="side">${R.id ? `<a class="name ${nameCls(R)}" href="${fighterHref(R.id, R.name)}">${esc(R.name)}</a>` : ""}</div>
        </div>
        <div class="outcome">
          ${outcome ? `<b>${outcome}</b><br>` : ""}
          <span class="muted">${esc(dot(x.method, x.end_round && `Round ${x.end_round}, ${x.end_time || ""}`, x.time_format))}</span>
          <div class="note">${esc(dot(x.weight_class, x.referee && `Referee ${x.referee}`))} ${x.title_fight ? badge("Title") : ""}</div>
          ${x.details ? `<div class="note">${esc(x.details)}</div>` : ""}
        </div>
      </section>
      ${st ? `<section class="section" id="rbr">${roundsBlock()}</section>`
        : `<p class="muted">No round-by-round stats recorded for this fight.</p>`}
      ${judges.length ? `<section class="section"><div class="block-head"><span class="label">Judges' scorecards</span>${badge(srcName(scores[0].source))}</div>
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
  // The source goes in the link (?src=) only when it isn't the first.
  const wire = () => {
    document.querySelectorAll("#rounds .chip").forEach((btn) => (btn.onclick = () => {
      document.querySelectorAll("#rounds .chip").forEach((c) => c.setAttribute("aria-pressed", String(c === btn)));
      render(btn.dataset.r);
    }));
    onCounted(document.getElementById("rbr"), (src) => {
      st = sets.find((x) => x.source === src);
      const p = params();
      if (st === sets[0]) p.delete("src"); else p.set("src", src);
      setParams(p);
      document.getElementById("rbr").innerHTML = roundsBlock();
      wire();
    });
    render("t");
  };
  wire();
}
