// Fight Canon. Browsing pages read JSON files written daily by scripts/site_json.py: one per
// fighter and per event, the event lists, a search index split by prefix, and the
// leaderboards' opening views. Only the Query page and filtered leaderboards query the database:
// DuckDB-WASM over the database as Parquet (scripts/export_parquet.py), reading only the
// columns and parts of the files a query needs; it loads the first time it's needed.
// Views are chosen by the path: / (landing), /fighters, /fighter/<id>, /events, /event/<id>,
// /fight/<id>, /leaders, /query (the server answers every path with this page). How it looks is
// in DESIGN.md. app.js starts this.

import { meta, objs } from "./data.js";
import { event, events } from "./events.js";
import { fight } from "./fight.js";
import { fighter, fighters } from "./fighters.js";
import { landing } from "./landing.js";
import { headerSearch } from "./search.js";
import { app, canonical, esc, eventLink, go, loading, nothing, setTitle } from "./ui.js";

let page = 0;  // the page being shown; a view drops its result if another page was opened since

// Each view gets the id from the URL and show(html), which draws the page unless another one
// was opened meanwhile (it returns false then, and the view stops). Leaders and Query load when
// they're opened.
const lazy = (file, name) => async (id, show) => (await import(file))[name](id, show);
const query = lazy("./query.js", "query");
const TITLES = { fighters: "Fighters", events: "Events", leaders: "Leaders", query: "Query", sql: "Query" };
const VIEWS = { "": landing, fighters, fighter, events, event, fight, leaders: lazy("./leaders.js", "leaders"), query, sql: query };

async function route(e) {
  const path = location.pathname;
  const y = e?.type === "popstate" ? history.state?.y : 0;  // Back returns to where the page was left
  window.dispatchEvent(new Event("route"));
  const [name = "", part = ""] = path.split("/").filter(Boolean);
  const numbered = ["fighter", "event", "fight"].includes(name);
  const id = numbered ? Number((decodeURIComponent(part).match(/^(\d+)(?:-|$)/) || [, 0])[1]) : decodeURIComponent(part);
  const view = VIEWS[name] || landing;
  const mine = ++page;
  setTitle(TITLES[name]);  // a fighter, event or fight page names itself once it has the name
  const show = (html) => { if (mine !== page) return false; app().innerHTML = html; return true; };
  document.body.classList.toggle("landing", view === landing);
  const nav = document.getElementById("nav");
  nav.classList.remove("open");
  document.getElementById("menu-btn").setAttribute("aria-expanded", "false");
  const section = { fighter: "fighters", event: "events", fight: "events", sql: "query" }[name] || name;
  nav.querySelectorAll("a").forEach((a) =>
    a.toggleAttribute("aria-current", a.getAttribute("href") === `/${section}`));
  if (view !== landing) app().innerHTML = loading();
  window.scrollTo(0, 0);
  try {
    canonical(path);  // a fighter, event or fight page puts its name in once it has it
    if (numbered && !id) show(nothing("Page not found."));
    else await view(id, show);
    if (y && mine === page) window.scrollTo(0, y);
  } catch (e) {
    show(`<p class="error">Couldn't load this page: ${esc(e.message)}</p>`);
  }
}

export function start() {
  history.scrollRestoration = "manual";
  const nav = document.getElementById("nav"), menuBtn = document.getElementById("menu-btn");
  menuBtn.onclick = () => {
    const open = nav.classList.toggle("open");
    menuBtn.setAttribute("aria-expanded", String(open));
  };
  headerSearch();
  meta().then((m) => {
    const [e] = objs(m.latest);
    if (e) document.getElementById("latest").innerHTML = ` · Latest event: ${eventLink(e.event_id, e.name)}`;
  }).catch(() => {});
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
  window.addEventListener("go", route);
  // Links from before the site had paths (#/fighter/<id>) still work.
  if (location.hash.startsWith("#/")) history.replaceState(null, "", location.hash.slice(1));
  route();
}
