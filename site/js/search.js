// Search: the split index on the JSON files, and the box in the header.

import { api, hex, meta, objs, ready } from "./data.js";
import { debounce, esc, eventHref, fighterHref, go, proRecord } from "./ui.js";

const norm = (s) => (s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

// The words of a search term, normalized like the search index, longest first; null if none
// has two letters.
export function searchWords(term) {
  const words = (norm(term).match(/[\p{L}\p{N}_]+/gu) || []).sort((a, b) => b.length - a.length);
  if (!words.length || words[0].length < 2) return null;
  return { words, first: words[0] };
}

// Whether every word of a term starts a different word of a name ("jon jones" isn't
// "Edwynn Jones", though "jon" starts "jones").
export function wordsMatch(terms, words, used = new Set()) {
  if (!terms.length) return true;
  const [t, ...rest] = terms;
  return words.some((w, i) => !used.has(i) && w.startsWith(t) && wordsMatch(rest, words, new Set(used).add(i)));
}

// Search a split index (sf/ fighters, se/ events): read the file of the longest word's prefix,
// as deep as the index is split (meta.json lists the split prefixes), and keep the rows in
// which every word of the term starts a different word. Rows come in rank order (most fights;
// newest). While that file loads, early() gets the matches in a shorter prefix's file that is
// already here: the top of the same list.
export async function searchIndex(folder, s, early) {
  const split = new Set((await meta()).splits?.[folder]);
  const path = (p) => `${folder}/${hex(p)}.json`;
  const keep = (file) => objs(file).filter((r) => wordsMatch(s.words, r.words.split(" ")));
  let p = s.first.slice(0, 2);
  while (split.has(p) && p.length < s.first.length) p = s.first.slice(0, p.length + 1);
  if (early && !ready.has(path(p))) {
    for (let q = p.slice(0, -1); q.length >= 2; q = q.slice(0, -1)) {
      if (ready.get(path(q))) { early(keep(ready.get(path(q)))); break; }
    }
  }
  let file = await api(path(p));
  while (file?.split && p.length < s.first.length) {  // a meta.json older than the files
    p = s.first.slice(0, p.length + 1);
    file = await api(path(p));
  }
  return keep(file);
}

export const searchFighters = async (s, limit, early) =>
  (await searchIndex("sf", s, early && ((rows) => early(rows.slice(0, limit))))).slice(0, limit);

// --- header --------------------------------------------------------------------------
// One box searches fighters and events together.

export function headerSearch() {
  const form = document.getElementById("search"), input = document.getElementById("search-q");
  const box = document.getElementById("suggest");
  let links = [], active = -1;
  const close = () => { box.hidden = true; active = -1; };
  let n = 0;  // only the latest search draws: an older one can finish last
  const render = async () => {
    const term = input.value.trim(), mine = ++n;
    if (!term) { close(); return; }
    const s = searchWords(term);
    if (!s) { box.innerHTML = `<p>Type at least two letters.</p>`; box.hidden = false; return; }
    const early = [null, null], soon = (i) => (rows) => {
      early[i] = rows;
      if (early[0] && early[1]) draw(mine, early[0], early[1].slice(0, 5));
    };
    const [people, events] = await Promise.all([searchFighters(s, 6, soon(0)), searchIndex("se", s, soon(1))]);
    draw(mine, people, events.slice(0, 5));
  };
  const draw = (mine, people, events) => {
    if (mine !== n) return;
    box.innerHTML = (people.length ? `<span class="label">Fighters</span>` + people.map((r) =>
        `<a href="${fighterHref(r.fighter_id, r.name)}">${esc(r.name)}<small>${proRecord(r, r)}</small></a>`).join("") : "") +
      (events.length ? `<span class="label">Events</span>` + events.map((e) =>
        `<a href="${eventHref(e.event_id, e.name)}">${esc(e.name)}<small>${esc(e.date)}</small></a>`).join("") : "") +
      (people.length || events.length ? "" : `<p>Nothing found.</p>`);
    links = [...box.querySelectorAll("a")];
    active = -1;
    box.hidden = false;
  };
  input.addEventListener("input", debounce(render, 120));
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
}
