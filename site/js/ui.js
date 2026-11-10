// What every page draws with: formatting, links, small pieces of HTML, the URL's filters.

const REPO = "https://github.com/jadevit/fight-canon-db";
export const RELEASES = `${REPO}/releases/latest`;

export const app = () => document.getElementById("app");

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
export const num = (n) => (n == null ? "—" : Number(n).toLocaleString("en-US"));
export const pct = (l, a) => (l == null || !a ? null : Math.round((100 * l) / a) + "%");
export const mmss = (s) => (s == null ? "—" : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`);
export const hmm = (s) => (s == null ? "—" : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`);
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export const monthYear = (d) => (d ? `${MONTHS[Number(d.slice(5, 7)) - 1]} ${d.slice(0, 4)}` : "");
export const longDate = (d) => (d ? `${Number(d.slice(8, 10))} ${MONTHS[Number(d.slice(5, 7)) - 1]} ${d.slice(0, 4)}` : "");
export const plural = (n, one, many = one + "s") => `${num(n)} ${n === 1 ? one : many}`;
export const american = (v) => (v == null ? null : v > 0 ? `+${v}` : String(v));
export const dot = (...parts) => parts.filter(Boolean).join(" · ");

// A page's address: its id, then its name for people (only the number is read).
const slug = (s) => (s || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
  .replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
export const pagePath = (kind, id, name) => `/${kind}/${id}${slug(name) ? `-${slug(name)}` : ""}`;
export const fighterHref = (id, name) => esc(pagePath("fighter", id, name));
export const eventHref = (id, name) => esc(pagePath("event", id, name));
export const fighterLink = (id, name, cls = "") => `<a href="${fighterHref(id, name)}"${cls ? ` class="${cls}"` : ""}>${esc(name)}</a>`;
export const eventLink = (id, name) => `<a href="${eventHref(id, name)}">${esc(name)}</a>`;
export const setTitle = (text) => { document.title = text ? `${text} · Fight Canon` : "Fight Canon"; };
// The page's one address for search engines, and the address bar's (an old or misspelled name
// in a link is put right), unless another page was opened meanwhile.
export function canonical(path, title) {
  const page = (p) => p.match(/^\/[^/]*(?:\/\d+)?/)[0];
  if (page(location.pathname) !== page(path)) return;
  if (title) setTitle(title);
  let link = document.querySelector('link[rel="canonical"]');
  if (!link) document.head.append(link = Object.assign(document.createElement("link"), { rel: "canonical" }));
  link.href = location.origin + path;
  if (location.pathname !== path) history.replaceState(null, "", path + location.search);
}
export const ext = (url, label) => `<a href="${esc(url)}" target="_blank" rel="noopener">${label}</a>`;
export const badge = (text) => `<span class="badge">${esc(text)}</span>`;
export const res = (r) => r ? `<span class="res res-${esc(r)}" title="${{ W: "Win", L: "Loss", D: "Draw", NC: "No contest" }[r] || ""}">${esc(r)}</span>`
  : `<span class="res res-none">–</span>`;
export const nothing = (text = "Nothing found.") => `<p class="muted" style="padding:14px 0">${text}</p>`;
export const loading = (text = "Loading…") => `<p class="loading"><span class="spinner"></span>${text}</p>`;

export const ICON = {
  chev: `<svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>`,
  terminal: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 8 4 4-4 4M12 17h7"/></svg>`,
  fighters: `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.6 3.4-5.5 6.5-5.5s5.7 1.9 6.5 5.5"/><circle cx="17" cy="9" r="2.5"/><path d="M17 14.5c2.4 0 4 1.5 4.5 4"/></svg>`,
  events: `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/></svg>`,
  rounds: `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M12 6v12M12 9H5M12 15H8M12 9h7M12 15h4"/></svg>`,
  open: `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="6" rx="7.5" ry="3"/><path d="M4.5 6v12c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3V6M4.5 12c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3"/></svg>`,
};

// A record ({w, l, d, nc}), counted from the fights in the database.
export const proRecord = (r) => `${r.w || 0}-${r.l || 0}-${r.d || 0}` + (r.nc ? ` (${r.nc} NC)` : "");

export function debounce(fn, ms = 150) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// Only the latest call's result is used: queries are async, and an older one can finish last.
export function latest(fn) {
  let n = 0;
  return async (...a) => {
    const mine = ++n;
    try {
      const out = await fn(...a);
      return mine === n ? out : undefined;
    } catch (e) {
      if (mine === n) throw e;
    }
  };
}

// A page's redraws (a search, a filter), which the router's catch doesn't see: a failure shows
// where the results go.
export function guard(fn) {
  return async (...a) => {
    const out = document.getElementById("results");
    try {
      await fn(...a);
    } catch (e) {
      out.innerHTML = `<p class="error">Couldn't load this: ${esc(e.message)}</p>`;
    }
  };
}

// Filters live in the query string (/leaders?stat=kd), so they survive back and refresh.
export function params() { return new URLSearchParams(location.search); }
export function setParams(p) {
  const s = p.toString();
  history.replaceState(null, "", s ? `${location.pathname}?${s}` : location.pathname);
}

// Open a page of the site without reloading it (the router listens).
export function go(href) {
  history.replaceState({ y: window.scrollY }, "");  // where Back returns to
  history.pushState(null, "", href);
  window.dispatchEvent(new Event("go"));
}

// The years with events, newest first, as a chip's options.
export function yearOptions(stats) {
  const years = [];
  for (let y = Number(stats.last_date.slice(0, 4)); y >= Number(stats.first_date.slice(0, 4)); y--) years.push([String(y), String(y)]);
  return years;
}

// A chip around a native select: the label, then the chosen option's text.
export const chip = (id, label, options, cur) => {
  const on = options.find(([v]) => String(v) === String(cur)) || options[0];
  return `<label class="chip${String(on[0]) !== String(options[0][0]) ? " on" : ""}">${label}<b>${esc(on[1])}</b>
    <select id="${id}" aria-label="${label}">${options.map(([v, t]) =>
      `<option value="${esc(v)}"${String(v) === String(on[0]) ? " selected" : ""}>${esc(t)}</option>`).join("")}</select></label>`;
};
export const syncChip = (sel) => {
  const label = sel.closest(".chip");
  label.querySelector("b").textContent = sel.selectedOptions[0]?.text || "";
  label.classList.toggle("on", sel.selectedIndex > 0);
};
