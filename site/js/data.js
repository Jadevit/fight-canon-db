// The site's data: where it is (config.json, written at deploy) and its JSON files.

const DB_DIR = "/db/";

let configFile;
export function dbConfig() {
  configFile ||= fetch(DB_DIR + "config.json", { cache: "no-cache" }).then((r) => {
    if (!r.ok) throw new Error(`${DB_DIR}config.json: HTTP ${r.status}`);
    return r.json();
  });
  return configFile;
}
export const dbBase = () => new URL(DB_DIR, location.href);
const apiBase = async () => new URL((await dbConfig()).api || "v1/", dbBase()).href;

// A JSON file of the API, fetched once per page load; null if there's no such file. `ready`
// holds the files that have arrived.
const files = new Map();
export const ready = new Map();
export function api(path) {
  if (!files.has(path)) {
    files.set(path, (async () => {
      const r = await fetch((await apiBase()) + path);
      if (r.status === 404 || r.status === 403) return null;
      if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
      const j = await r.json();
      ready.set(path, j);
      return j;
    })().catch((e) => { files.delete(path); throw e; }));
  }
  return files.get(path);
}
export const meta = () => api("meta.json");
// {cols, rows} to objects.
export const objs = (t) => (t ? t.rows.map((r) => Object.fromEntries(t.cols.map((c, i) => [c, r[i]]))) : []);
export const hex = (s) => [...new TextEncoder().encode(s)].map((b) => b.toString(16).padStart(2, "0")).join("");
// Which fx/ file a fight is in: the low byte of its id's FNV-1a hash (site_json.py does the same).
export function shard(id) {
  let h = 0x811c9dc5;
  for (const b of new TextEncoder().encode(String(id))) h = Math.imul(h ^ b, 0x01000193) >>> 0;
  return (h & 0xff).toString(16).padStart(2, "0");
}

// Promotions with enough events to list (there are thousands of one-off shows).
export const promotions = async () => objs((await meta()).promotions);
