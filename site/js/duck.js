// The query engine: DuckDB-WASM over the database as Parquet (scripts/export_parquet.py), for
// the Query page and filtered leaderboards.

import { dbBase, dbConfig, meta, objs } from "./data.js";

const DUCKDB = "https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.29.0/+esm";

// The database, started the first time something queries it: a DuckDB connection with
// division(). Views over the Parquet files are made as queries need them (views()): each
// one reads its file's header, and the files are far away.
const sqlString = (s) => `'${String(s).replaceAll("'", "''")}'`;
export let engine;
export function db() {
  engine ||= (async () => {
    const [m, duckdb] = await Promise.all([meta(), import(DUCKDB)]);
    const bundle = await duckdb.selectBundle(duckdb.getJsDelivrBundles());
    const url = URL.createObjectURL(new Blob([`importScripts("${bundle.mainWorker}");`], { type: "text/javascript" }));
    const instance = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(duckdb.LogLevel.WARNING), new Worker(url));
    await instance.instantiate(bundle.mainModule, bundle.pthreadWorker);
    URL.revokeObjectURL(url);
    const conn = await instance.connect();
    await conn.query("SET enable_http_metadata_cache = true");
    // The division a bout title names: the first in meta's list (site_boards.py builds the same).
    const names = objs(m.divisions).map((d) => d.division).filter((d) => d !== "Other");
    const title = "regexp_replace(coalesce(wc, ''), '(?i)catchweight', 'Catch Weight')";
    await conn.query(`CREATE MACRO division(wc) AS CASE ${names.map((d) =>
      `WHEN contains(${title}, ${sqlString(d)}) THEN ${sqlString(d)}`).join(" ")} ELSE 'Other' END`);
    return conn;
  })();
  engine.catch(() => { engine = null; });  // a failed start is tried again next time
  return engine;
}
// The views a query needs: the tables it names, and bouts' tables if it names bouts (every
// table if it looks at the catalogue). Made one at a time, in turn, whatever asks for them.
const made = new Set();
let making = Promise.resolve();
export function views(conn, sql) {
  making = making.catch(() => {}).then(async () => {  // one failure doesn't stop the next
    const config = await dbConfig();
    const at = new URL(config.parquet ?? "", dbBase()).href;
    const words = new Set(sql.toLowerCase().match(/[a-z_]+/g) || []);
    const all = words.has("information_schema") || [...words].some((w) => w.startsWith("duckdb_"));
    const bouts = all || words.has("bouts");
    const want = config.tables.filter((t) => all || words.has(t) || (bouts && BOUT_TABLES.includes(t)));
    for (const t of want.filter((t) => !made.has(t))) {
      await conn.query(`CREATE OR REPLACE VIEW ${t} AS SELECT * FROM read_parquet('${at}${t}.parquet')`);
      made.add(t);
    }
    if (bouts && !made.has("bouts")) {
      await conn.query(BOUTS);
      made.add("bouts");
    }
  });
  return making;
}
const BOUT_TABLES = ["fights", "events", "fight_participants", "fighters"];
// Each fight with its event and both fighters.
const BOUTS = `CREATE OR REPLACE VIEW bouts AS
  SELECT x.fight_id, x.event_id, e.date, e.name AS event, e.promotion, division(x.weight_class) AS division,
         a.fighter_id AS a_id, fa.name AS a_name, a.result AS a_res, b.fighter_id AS b_id, fb.name AS b_name,
         b.result AS b_res, x.* EXCLUDE (fight_id, event_id)
  FROM fights x JOIN events e USING (event_id)
  LEFT JOIN fight_participants a ON a.fight_id = x.fight_id AND a.corner = 0
  LEFT JOIN fighters fa ON fa.fighter_id = a.fighter_id
  LEFT JOIN fight_participants b ON b.fight_id = x.fight_id AND b.corner = 1
  LEFT JOIN fighters fb ON fb.fighter_id = b.fighter_id`;
// A query's rows as objects; DuckDB's 64-bit integers come back as BigInt, made Numbers here.
// A SUM of integers is a 128-bit integer (and some results decimals), which come back as
// objects that are numbers only as text: they become Numbers too.
export function plain(v) {
  if (typeof v === "bigint") return Number(v);
  if (v && typeof v === "object" && !Array.isArray(v) && !(v instanceof Date)) {
    const s = String(v);
    if (/^-?\d+(\.\d+)?$/.test(s)) return Number(s);
  }
  return v;
}
export async function q(sql, params = []) {
  const conn = await db();
  await views(conn, sql);
  const st = await conn.prepare(sql);
  try {
    return (await st.query(...params)).toArray().map((r) =>
      Object.fromEntries(Object.entries(r.toJSON()).map(([k, v]) => [k, plain(v)])));
  } finally {
    await st.close();
  }
}
