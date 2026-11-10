// --- the Query page ------------------------------------------------------------------
// Ask a question, get the query from an AI assistant (the page writes the prompt: the rules
// and tables from query.json, then the question), run it, and read the results as a table
// with readable headers, formatted values and links; or write SQL directly.

import { api } from "./data.js";
import { db, engine, plain, views } from "./duck.js";
import { debounce, esc, loading, longDate, mmss, params, plural, RELEASES, setParams } from "./ui.js";

// Examples, as the questions they answer. Their columns are named the way the prompt asks
// assistants to name them, with the ids the page turns into links.
const EXAMPLES = {
  "What were the latest events?": `SELECT date AS "Date", name AS "Event", promotion AS "Promotion", location AS "Location", event_id
FROM events ORDER BY date DESC LIMIT 20;`,
  "Who has the most fights in the database?": `SELECT f.name AS "Fighter", COUNT(*) AS "Fights",
       count_if(p.result = 'W') AS "Wins", count_if(p.result = 'L') AS "Losses", p.fighter_id
FROM fight_participants p JOIN fighters f USING (fighter_id)
GROUP BY ALL ORDER BY "Fights" DESC LIMIT 25;`,
  "Who lands the most significant strikes per round in the UFC?": `SELECT f.name AS "Fighter", COUNT(*) AS "Rounds",
       round(SUM(r.sig_str_land) / COUNT(*), 1) AS "Sig. Strikes Per Round", r.fighter_id
FROM round_stats r JOIN fighters f USING (fighter_id) JOIN bouts b USING (fight_id)
WHERE r.source = 'ufc' AND b.promotion = 'UFC'
GROUP BY ALL HAVING COUNT(*) >= 30
ORDER BY "Sig. Strikes Per Round" DESC LIMIT 25;`,
  "Which fights had the most knockdowns?": `SELECT b.date AS "Date", b.event AS "Event", f.name AS "Fighter",
       SUM(r.knockdowns) AS "Knockdowns", r.fighter_id, r.fight_id
FROM round_stats r JOIN fighters f USING (fighter_id) JOIN bouts b USING (fight_id)
GROUP BY ALL ORDER BY "Knockdowns" DESC LIMIT 20;`,
  "How has the UFC's finish rate changed?": `SELECT substr(date, 1, 4) AS "Year", COUNT(*) AS "Fights",
       round(100 * avg((method ILIKE '%KO%' OR method ILIKE '%Submission%')::INT), 1) AS "Finish %"
FROM bouts WHERE promotion = 'UFC'
GROUP BY ALL ORDER BY "Year";`,
  "How do fights end?": `SELECT method AS "Method", COUNT(*) AS "Fights" FROM fights GROUP BY ALL ORDER BY "Fights" DESC LIMIT 50;`,
  "How many fights per division?": `SELECT division AS "Division", COUNT(*) AS "Fights" FROM bouts GROUP BY ALL ORDER BY "Fights" DESC;`,
  "Which promotions have held the most events?": `SELECT promotion AS "Promotion", COUNT(*) AS "Events", MIN(date) AS "First Event", MAX(date) AS "Latest Event"
FROM events GROUP BY ALL ORDER BY "Events" DESC LIMIT 30;`,
  "Where do the round stats come from?": `SELECT source AS "Source", COUNT(DISTINCT fight_id) AS "Fights", COUNT(*) AS "Fighter Rounds"
FROM round_stats GROUP BY ALL ORDER BY "Fights" DESC;`,
};

// Headers: our own column names get their label; others are Title Cased ("sig_per_round" ->
// "Sig Per Round", "finish_pct" -> "Finish %"); a name that already has capitals or spaces
// (AS "Control Time") is shown as written.
const LABELS = {
  fighter_id: "Fighter ID", event_id: "Event ID", fight_id: "Fight ID", a_name: "Fighter A", b_name: "Fighter B",
  a_res: "Result A", b_res: "Result B", a_id: "Fighter A ID", b_id: "Fighter B ID", dob: "Date of Birth",
  height_in: "Height", reach_in: "Reach", weight_lbs: "Weight (lbs)", end_round: "Round", end_time: "Time",
  sig_str_land: "Sig. Strikes Landed", sig_str_att: "Sig. Strikes Attempted",
  total_str_land: "Total Strikes Landed", total_str_att: "Total Strikes Attempted",
  td_land: "Takedowns Landed", td_att: "Takedowns Attempted", sub_att: "Submission Attempts",
  ctrl_sec: "Control Time", kd: "Knockdowns", n: "Fights", w: "Wins", l: "Losses", d: "Draws", nc: "No Contests",
};
const UPPER = new Set(["id", "ufc", "ko", "tko", "kd", "pfl", "mma", "td", "dob", "nc"]);
export function label(c) {
  if (LABELS[c]) return LABELS[c];
  if (/[A-Z ]/.test(c)) return c;
  return c.split(/[_\s]+/).filter(Boolean).map((w) => (w === "pct" ? "%" : UPPER.has(w) ? w.toUpperCase()
    : w[0].toUpperCase() + w.slice(1))).join(" ");
}
const colKey = (c) => c.toLowerCase().replace(/[^a-z0-9%]+/g, "_").replace(/^_|_$/g, "");

// How a column's values are shown, from its name and values.
export function kindOf(c, values) {
  const k = colKey(c), some = values.filter((v) => v != null);
  const numeric = some.length > 0 && some.every((v) => typeof v === "number");
  if (numeric && (/(^|_)(sec|secs|seconds)$/.test(k) || /(^|_)(ctrl|control)(_|$)/.test(k))) return "time";
  if (numeric && k === "height_in") return "height";
  if (numeric && k === "reach_in") return "inches";
  if (some.length && some.every((v) => typeof v === "string" && /^\d{4}-\d\d-\d\d$/.test(v))) return "date";
  if (numeric && /(pct|percent|%)/.test(k)) return "pct";
  if (numeric && /(^|_)(acc|accuracy|rate|ratio)(_|$)/.test(k) && some.every((v) => v >= 0 && v <= 1)) return "share";
  if (numeric && k === "year") return "year";
  return numeric ? "num" : "text";
}
function shown(v, kind) {
  if (v == null) return "—";
  switch (kind) {
    case "time": {
      const s = Math.round(v);
      return s >= 3600 ? `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}` : mmss(s);
    }
    case "height": return `${Math.floor(v / 12)}′${v % 12}″`;
    case "inches": return `${v}″`;
    case "date": return longDate(v);
    case "pct": return `${Number(v).toLocaleString("en-US", { maximumFractionDigits: 1 })}%`;
    case "share": return `${(v * 100).toLocaleString("en-US", { maximumFractionDigits: 1 })}%`;
    case "year": return String(v);
    case "num": return Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 });
    default: return typeof v === "object" ? JSON.stringify(v) : String(v);
  }
}

// Ids become links: on the name beside them (fighter_id + "Fighter"), or a link of their own.
// A fight gets a "Fight" column at the end.
const ID_NAMES = { fighter_id: ["fighter", "name", "fighter_name"], a_id: ["a_name", "fighter_a"],
                   b_id: ["b_name", "fighter_b"], event_id: ["event", "event_name", "name"] };
const HREF = { fighter_id: "/fighter/", a_id: "/fighter/", b_id: "/fighter/", event_id: "/event/", fight_id: "/fight/" };
function layout(columns, rows) {
  const keys = columns.map(colKey), cols = columns.map((c, i) => ({ name: c, i, label: label(c), hidden: false, link: null }));
  for (const [id, names] of Object.entries(ID_NAMES)) {
    const at = keys.indexOf(id);
    if (at < 0) continue;
    const partner = names.map((n) => keys.indexOf(n)).find((j) => j >= 0 && !cols[j].link);
    if (partner !== undefined) { cols[partner].link = { href: HREF[id], from: at }; cols[at].hidden = true; }
    else cols[at].link = { href: HREF[id], from: at };
  }
  const fight = keys.indexOf("fight_id");
  if (fight >= 0) {
    cols[fight].hidden = true;
    cols.push({ name: "", i: fight, label: "Fight", hidden: false, link: { href: "/fight/", from: fight, text: "View →" } });
  }
  for (const c of cols) c.kind = c.link?.text ? "text" : kindOf(c.name, rows.map((r) => r[c.i]));
  return cols.filter((c) => !c.hidden);
}
const cellText = (c, r) => (c.link?.text ? c.link.text : shown(r[c.i], c.kind));
const RIGHT = new Set(["num", "pct", "share", "time"]);

function resultTable(cols, rows) {
  return `<div class="grid-out"><table><thead><tr>${cols.map((c, j) =>
      `<th data-j="${j}" class="${RIGHT.has(c.kind) ? "r" : ""}" title="Sort">${esc(c.label)}</th>`).join("")}</tr></thead>
    <tbody>${rows.slice(0, 1000).map((r) => `<tr>${cols.map((c) => {
      const v = r[c.i], cls = [RIGHT.has(c.kind) ? "r" : "", v == null ? "null" : ""].join(" ").trim();
      const text = esc(cellText(c, r));
      const id = c.link && r[c.link.from];
      return `<td class="${cls}">${id ? `<a href="${c.link.href}${esc(id)}">${text}</a>` : text}</td>`;
    }).join("")}</tr>`).join("")}</tbody></table></div>`;
}

// The table as Markdown (Reddit, Discord, GitHub), CSV, or tab-separated text (pastes into a
// spreadsheet). Markdown keeps the links, to fightcanon.com.
function asMarkdown(cols, rows) {
  const cell = (s) => String(s).replaceAll("|", "\\|").replace(/\s+/g, " ");
  const line = (cells) => `| ${cells.join(" | ")} |`;
  const body = rows.map((r) => line(cols.map((c) => {
    const id = c.link && r[c.link.from], text = cell(cellText(c, r));
    return id ? `[${text}](${location.origin}${c.link.href}${encodeURIComponent(id)})` : text;
  })));
  return [line(cols.map((c) => cell(c.label))), line(cols.map((c) => (RIGHT.has(c.kind) ? "--:" : "---"))), ...body,
          "", `*Data: [Fight Canon](${location.origin})*`].join("\n");
}
// For a spreadsheet: numbers and dates as they are (1234, 2024-11-16), empty for missing, and
// a fight's link instead of "View".
function rawText(c, r) {
  const v = r[c.i];
  if (c.link?.text) return `${location.origin}${c.link.href}${encodeURIComponent(v)}`;
  if (v == null) return "";
  return ["num", "pct", "share", "year", "date"].includes(c.kind) ? String(v) : shown(v, c.kind);
}
export function asDelimited(cols, rows, sep) {
  const cell = (s) => (sep === "," && /[",\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s.replace(/[\t\n]/g, " "));
  return [cols.map((c) => cell(c.label)), ...rows.map((r) => cols.map((c) => cell(rawText(c, r))))]
    .map((cells) => cells.join(sep)).join("\n");
}
async function copyText(text, btn, done = "Copied") {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const t = document.createElement("textarea");
    t.value = text;
    document.body.appendChild(t);
    t.select();
    document.execCommand("copy");
    t.remove();
  }
  const was = (btn.dataset.was ||= btn.textContent);  // a second click comes back to the same label
  btn.textContent = done;
  setTimeout(() => (btn.textContent = was), 2000);
}

// What an assistant needs: the rules, the tables, and the question.
function prompt(help, question) {
  const tables = help.tables.map((t) => `\n${t.name}${t.about ? `: ${t.about}` : ""}\n` + t.columns.map(([n, type, about]) =>
    `  ${n}${type ? ` ${type}` : ""}${about ? ` -- ${about}` : ""}`).join("\n")).join("\n");
  return `${help.rules}\nTables:\n${tables}\n\nQuestion: ${question || "(write your question here)"}\n`;
}
const ASSISTANTS = [["ChatGPT", "https://chatgpt.com/", "?q="], ["Claude", "https://claude.ai/new", "?q="]];

export async function query(_, show) {
  const p = params();
  const first = Object.values(EXAMPLES)[0];
  if (!show(`
    <div class="page-head"><h1>Query</h1>
      <p class="muted">Ask the database anything: records, streaks, stats, trends. You don't need to know SQL:
        type your question, and this page adds it to a prompt for your own AI assistant (ChatGPT, Claude or
        any other). The assistant writes the query; paste it back here and the page runs it. It runs in your
        browser, so you can't break anything.</p>
    </div>
    <section class="card ask">
      <ol class="steps">
        <li><b>Ask your question</b>
          <textarea id="question" rows="2" placeholder="e.g. Which UFC fighters have the most knockouts since 2015?">${esc(p.get("ask") || "")}</textarea></li>
        <li><b>Give the prompt to your AI assistant</b>
          <span class="muted">It explains how this database works, and ends with your question.</span>
          <div class="run-row"><button class="btn primary" id="copy-prompt" type="button" disabled>Copy prompt</button>
            ${ASSISTANTS.map(([name]) => `<a class="btn" data-ai="${name}" target="_blank" rel="noopener" aria-disabled="true">Open in ${name}</a>`).join("")}</div>
          <span class="note" id="ask-status"></span></li>
        <li><b>Paste the query it writes below, and run it</b></li>
      </ol>
    </section>
    <details class="tables-help"><summary>Tables and columns</summary><div id="tables">${loading()}</div></details>
    <div class="section examples"><span class="label">Or start from an example</span>
      <div class="chips scroll-x" id="ex" style="margin-top:8px">${Object.keys(EXAMPLES).map((k) =>
        `<button class="chip" type="button">${esc(k)}</button>`).join("")}</div></div>
    <textarea id="sqltext" class="editor" spellcheck="false" aria-label="SQL">${esc(p.get("q") || first)}</textarea>
    <div class="run-row"><button class="btn primary" id="run">Run</button><span class="note" style="margin:0">Ctrl+Enter</span>
      <span class="note" id="status" style="margin:0"></span></div>
    <div id="out"></div>
    <p class="note">For your own analysis, the whole database is a <a href="${RELEASES}">free download</a> (SQLite).</p>`)) return;
  const ta = document.getElementById("sqltext"), qa = document.getElementById("question");
  const help = api("query.json");
  const keep = () => setParams(new URLSearchParams({ ...(qa.value.trim() && { ask: qa.value.trim() }), ...(ta.value !== first && { q: ta.value }) }));

  // Tables and columns: click a name to put it in the query.
  help.then((h) => {
    document.getElementById("tables").innerHTML = (h?.tables || []).map((t) => `
      <div class="table-help"><button class="tname" type="button" data-ins="${esc(t.name)}">${esc(t.name)}</button>
        <p class="note">${esc(t.about)}</p>
        <ul>${t.columns.map(([n, type, about]) => `<li><button type="button" data-ins="${esc(n)}">${esc(n)}</button>
          <span class="faint">${esc(type)}</span>${about ? ` <span class="muted">${esc(about)}</span>` : ""}</li>`).join("")}</ul></div>`).join("");
    document.querySelectorAll("#tables [data-ins]").forEach((b) => (b.onclick = () => {
      const at = ta.selectionStart ?? ta.value.length;
      ta.value = ta.value.slice(0, at) + b.dataset.ins + ta.value.slice(ta.selectionEnd ?? at);
      ta.focus();
      ta.selectionStart = ta.selectionEnd = at + b.dataset.ins.length;
    }));
  }).catch(() => {});

  // The prompt's buttons work once there's a question (and the prompt's rules have loaded).
  // The assistants' buttons are plain links, kept up to date, so a phone opens the app or the
  // site, not both; a click copies the prompt as it opens.
  const askStatus = document.getElementById("ask-status"), copyBtn = document.getElementById("copy-prompt");
  const aiLinks = [...document.querySelectorAll("[data-ai]")];
  let rules = null;
  const text = () => prompt(rules, qa.value.trim());
  const sync = () => {
    const ready = Boolean(rules && qa.value.trim());
    copyBtn.disabled = !ready;
    for (const a of aiLinks) {
      const [, url, q] = ASSISTANTS.find(([n]) => n === a.dataset.ai);
      if (!ready) { a.removeAttribute("href"); a.setAttribute("aria-disabled", "true"); continue; }
      // A long prompt doesn't fit in a link: it's on the clipboard to paste instead.
      const enc = encodeURIComponent(text());
      a.dataset.filled = enc.length < 7000 ? "1" : "";
      a.href = a.dataset.filled ? url + q + enc : url;
      a.removeAttribute("aria-disabled");
    }
    askStatus.textContent = qa.value.trim() ? "" : "Type your question first.";
  };
  help.then((h) => { rules = h; sync(); }).catch(() => {});
  sync();
  const keepSoon = debounce(keep, 400);
  qa.oninput = () => { sync(); keepSoon(); };
  copyBtn.onclick = () => {
    copyText(text(), copyBtn, "Prompt copied");
    askStatus.textContent = "Now paste it into your AI assistant.";
  };
  for (const a of aiLinks) a.onclick = (e) => {
    if (!a.href) { e.preventDefault(); return; }
    copyText(text(), a, "Prompt copied");  // starts the copy now, while the click still counts
    askStatus.textContent = a.dataset.filled ? `${a.dataset.ai} opens with the prompt filled in.`
      : `Paste the prompt into ${a.dataset.ai}.`;
  };

  let current = null;  // the last result: {cols, rows, sorted, dir}, rows sorted as shown
  const out = document.getElementById("out"), status = document.getElementById("status");
  const draw = () => {
    const { cols, rows } = current;
    out.innerHTML = `<div class="run-row result-actions">
        <button class="btn small" type="button" id="copy-md">Copy as Markdown</button>
        <button class="btn small" type="button" id="copy-tsv">Copy Raw</button>
        <button class="btn small" type="button" id="csv">Download CSV</button></div>` + resultTable(cols, rows);
    out.querySelectorAll("th[data-j]").forEach((th) => (th.onclick = () => {
      const c = cols[th.dataset.j];
      const dir = current.sorted === th.dataset.j ? -current.dir : (RIGHT.has(c.kind) ? -1 : 1);
      rows.sort((a, b) => {
        const x = a[c.i], y = b[c.i];
        if (x == null) return 1;
        if (y == null) return -1;
        return (x < y ? -1 : x > y ? 1 : 0) * dir;
      });
      Object.assign(current, { sorted: th.dataset.j, dir });
      draw();
    }));
    document.getElementById("copy-md").onclick = (e) => copyText(asMarkdown(cols, rows), e.target);
    document.getElementById("copy-tsv").onclick = (e) => copyText(asDelimited(cols, rows, "\t"), e.target);
    document.getElementById("csv").onclick = () => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([asDelimited(cols, rows, ",")], { type: "text/csv" }));
      a.download = "fight-canon.csv";
      a.click();
      URL.revokeObjectURL(a.href);
    };
  };
  const run = async () => {
    keep();
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
      const rows = res.toArray().map((r) => columns.map((c) => plain(r[c])));
      if (!columns.length) { status.textContent = `Done in ${took}.`; return; }
      status.textContent = `${plural(rows.length, "row")}${rows.length > 1000 ? " (showing 1,000; copies and downloads have them all)" : ""} · ${took}`;
      current = { cols: layout(columns, rows), rows };
      draw();
    } catch (e) {
      status.textContent = "";
      out.innerHTML = `<div class="error" style="margin-top:12px"><p>${esc(e.message)}</p>
        <p class="note">Asking an AI assistant? Copy this and paste it back to it: it has your question, the query and the error.</p>
        <button class="btn small" type="button" id="copy-fix">Copy for your assistant</button></div>`;
      document.getElementById("copy-fix").onclick = (ev) => copyText(
        `${qa.value.trim() ? `My question: ${qa.value.trim()}\n\n` : ""}This query failed in DuckDB:\n\n${ta.value}\n\n` +
        `Error: ${e.message}\n\nPlease fix the query. Reply with only the corrected query in one code block.`, ev.target);
    }
  };
  document.getElementById("run").onclick = run;
  ta.onkeydown = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) run(); };
  document.querySelectorAll("#ex .chip").forEach((b) => (b.onclick = () => { ta.value = EXAMPLES[b.textContent]; run(); }));
  // Only a link with a query runs by itself: opening the page shouldn't download the engine or
  // reach R2 (Cloudflare doesn't cache the size requests) for someone who only wants the prompt.
  if (p.get("q")) await run();
}
