// The landing page.

import { meta, objs } from "./data.js";
import { dot, esc, eventHref, ICON, loading, longDate, num, plural, RELEASES } from "./ui.js";

export async function landing(_, show) {
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
             promotions shows up as a wider range.</p>
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
  if (slot) slot.innerHTML = e ? `<a class="card" href="${eventHref(e.event_id, e.name)}">
      <span class="badge">${esc(e.promotion)}</span><h3>${esc(e.name)}</h3>
      <p>${esc(dot(longDate(e.date), e.location, plural(e.n, "fight")))}</p></a>` : "";
}
