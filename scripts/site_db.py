"""Build the website's copy of canon.db. It's what scripts/site_json.py writes the browsing
pages from, and what the SQL page and filtered leaderboards query in the browser with
sql.js-httpvfs, which fetches only the pages a query touches, one request at a time; so
each such query should read a few contiguous ranges, not hundreds of scattered rows.

- fights and fight_participants become one table, site_fights: each fight with its event's
  date and name and both fighters, in card order (an event's fights are contiguous). Views
  named fights and fight_participants give the original columns back for the SQL page.
- `site_` tables hold the lists the pages show (search names, events, leaderboard totals and
  their opening views), with what each row displays in the row itself.
- The round stats tables are stored in fight order, so a fight's rounds are together and a
  query that reads them all by fight reads the table straight through.
- Pages are 4 KB and the file is split into 10 MB chunks.

    python scripts/site_db.py [canon.db] [out dir] [keep]   # defaults: data/canon.db site/db

`keep` also saves the whole file (for scripts/site_json.py).

Writes <out>/canon.db.000, .001, ... and <out>/config.json.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from pathlib import Path

PAGE = 4096                   # SQLite page size, and the size of each range request
CHUNK = 10 * 1024 * 1024      # bytes per file on the server

# Keep in step with division() and norm() in site/app.js.
DIVISIONS = ["Women's Strawweight", "Women's Flyweight", "Women's Bantamweight",
             "Women's Featherweight", "Atomweight", "Strawweight", "Flyweight", "Bantamweight", "Featherweight",
             "Lightweight", "Welterweight", "Middleweight", "Light Heavyweight", "Super Heavyweight", "Heavyweight",
             "Catch Weight", "Open Weight"]


def division(wc: str | None) -> str:
    if not wc:
        return "Other"
    s = re.sub("catchweight", "Catch Weight", wc, count=1, flags=re.I)
    return next((d for d in DIVISIONS if d in s), "Other")


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if not 0x300 <= ord(c) <= 0x36F).lower()


def words(s: str | None) -> set[str]:
    return set(re.findall(r"\w+", norm(s)))


# Leaderboards: which fights each board counts (the stats table and source; results count
# every fight), its stats as SQL over the summed columns, and what the site says about it.
# The site reads this from site_boards.
LANDED = {
    "sig":    dict(label="Sig. strikes landed", expr="sl"),
    "total":  dict(label="Total strikes landed", expr="tsl"),
    "td":     dict(label="Takedowns landed", expr="tl"),
    "subatt": dict(label="Submission attempts", expr="suba"),
}
KD = dict(label="Knockdowns", expr="kd")
TDACC = dict(label="Takedown accuracy (min. 10 attempted)", expr="1.0 * tl / ta", where="ta >= 10", fmt="pct")
CTRL = dict(label="Control time", expr="ctrl", fmt="time")
FULL = {
    "kd": KD, "sig": LANDED["sig"],
    "sigacc": dict(label="Sig. strike accuracy (min. 100 attempted)", expr="1.0 * sl / sa", where="sa >= 100",
                   fmt="pct"),
    "total": LANDED["total"], "td": LANDED["td"], "tdacc": TDACC, "subatt": LANDED["subatt"], "ctrl": CTRL,
}
BOARDS = {
    "results": dict(
        label="Results (all fights)", source=None,
        stats={"wins": dict(label="Wins", expr="w"),
               "fin": dict(label="Finishes (KO/TKO + submission wins)", expr="fin"),
               "ko": dict(label="KO/TKO wins", expr="ko"),
               "subw": dict(label="Submission wins", expr="subw"),
               "fights": dict(label="Fights", expr="n")},
        note="Counts pro fights in this database, which doesn't have every fighter's whole career. "
             "All years: results aren't split by year."),
    "ufcstats": dict(label="UFC Stats", source=("round_stats", "ufcstats"), stats=FULL,
                     note="Fights counted by UFC Stats: the UFC, and the other promotions it covers."),
    "pfl": dict(label="PFL (from Dec 2025)", source=("round_stats", "pfl"), stats=FULL,
                note="PFL's own stats, from December 2025 on."),
    "smartcage": dict(
        label="PFL SmartCage (2018–2025)", source=("smartcage_round_stats", "pfl"),
        stats={"kd": KD, "total": LANDED["total"],
               "totacc": dict(label="Strike accuracy (min. 100 thrown)", expr="1.0 * tsl / tsa",
                              where="tsa >= 100", fmt="pct"),
               "arm": dict(label="Arm strikes landed", expr="arm"),
               "leg": dict(label="Leg strikes landed", expr="leg"),
               "ground": dict(label="Ground strikes landed", expr="grd"),
               "td": LANDED["td"], "tdacc": TDACC, "subatt": LANDED["subatt"]},
        note="PFL's older system, 2018 to November 2025. It counts all strikes, not significant ones."),
    "oktagon": dict(label="Oktagon", source=("round_stats", "oktagon"), stats=LANDED,
                    note="Oktagon's own stats, on the Oktagon fights Sofascore doesn't have. Only what landed "
                         "is counted, so there are no accuracy, knockdown or control boards."),
    "sofascore": dict(
        label="Sofascore", source=("round_stats", "sofascore"),
        stats={"kd": KD, "sig": LANDED["sig"], "thrown": dict(label="Total strikes thrown", expr="tsa"),
               "td": LANDED["td"], "tdacc": TDACC, "subatt": LANDED["subatt"], "ctrl": CTRL},
        note="Sofascore's count for promotions with no official stats (ONE, RIZIN, KSW, ACA, BRAVE, Cage "
             "Warriors and others), and for Oktagon. Not comparable with the other boards."),
}
SUMS = {  # leaderboard column -> column of the stats table
    "round_stats": dict(kd="knockdowns", sl="sig_str_land", sa="sig_str_att", tsl="total_str_land",
                        tsa="total_str_att", tl="td_land", ta="td_att", suba="sub_att", ctrl="ctrl_sec"),
    "smartcage_round_stats": dict(kd="knockdowns", tsl="total_str_land", tsa="total_str_att", arm="arm_land",
                                  leg="leg_land", grd="ground_land", tl="td_land", ta="td_att", suba="sub_att"),
}
STATS = ["kd", "sl", "sa", "tsl", "tsa", "tl", "ta", "suba", "ctrl", "arm", "leg", "grd"]
RESULTS = ["n", "w", "fin", "ko", "subw"]
FIGHT_COLS = ["weight_class", "title_fight", "scheduled_rounds", "method", "end_round", "end_time", "time_format",
              "referee", "details", "overturned", "no_contest", "bout_type", "source"]

SITE_SQL = f"""
-- Fights with their event and both fighters, in card order (card is the order of `fights`).
CREATE TABLE site_fights (fight_id TEXT, event_id TEXT, card INTEGER, date TEXT, event TEXT, promotion TEXT,
    division TEXT, has_stats INTEGER, a_id TEXT, a_name TEXT, a_res TEXT, b_id TEXT, b_name TEXT, b_res TEXT,
    {", ".join(FIGHT_COLS)});
INSERT INTO site_fights
    SELECT x.fight_id, x.event_id, x.rowid, e.date, e.name, e.promotion, division(x.weight_class),
           EXISTS (SELECT 1 FROM round_stats r WHERE r.fight_id = x.fight_id)
             OR EXISTS (SELECT 1 FROM smartcage_round_stats r WHERE r.fight_id = x.fight_id),
           a.fighter_id, fa.name, a.result, b.fighter_id, fb.name, b.result, {", ".join("x." + c for c in FIGHT_COLS)}
    FROM fights x JOIN events e USING (event_id)
    LEFT JOIN fight_participants a ON a.fight_id = x.fight_id AND a.corner = 0
    LEFT JOIN fighters fa ON fa.fighter_id = a.fighter_id
    LEFT JOIN fight_participants b ON b.fight_id = x.fight_id AND b.corner = 1
    LEFT JOIN fighters fb ON fb.fighter_id = b.fighter_id
    ORDER BY x.event_id, x.rowid;
CREATE UNIQUE INDEX site_fights_id ON site_fights(fight_id);
CREATE INDEX site_fights_event ON site_fights(event_id, card);
CREATE INDEX site_fights_a ON site_fights(a_id);
CREATE INDEX site_fights_b ON site_fights(b_id);

CREATE INDEX site_fighter_aliases_fighter ON fighter_aliases(fighter_id);
CREATE INDEX site_event_aliases_event ON event_aliases(event_id);

-- Counts for the home page.
CREATE TABLE site_stats AS SELECT
    (SELECT COUNT(*) FROM events) events, (SELECT COUNT(*) FROM fights) fights,
    (SELECT COUNT(*) FROM fighters) fighters, (SELECT COUNT(DISTINCT promotion) FROM events) promotions,
    (SELECT MIN(date) FROM events) first_date, (SELECT MAX(date) FROM events) last_date;

-- What a fighter row in a list shows: their record from UFC Stats and their fights here.
CREATE TEMP TABLE cards AS
    SELECT f.fighter_id, f.name, f.nickname, f.wins, f.losses, f.draws, f.no_contests,
           COUNT(p.fight_id) n, SUM(p.result = 'W') w, SUM(p.result = 'L') l, SUM(p.result = 'D') d,
           SUM(p.result = 'NC') nc
    FROM fighters f LEFT JOIN fight_participants p USING (fighter_id) GROUP BY f.fighter_id;

-- Fighter search: every word of every name a fighter goes by, with the row it shows.
CREATE TABLE site_names (token TEXT, fighter_id TEXT, search TEXT, n INTEGER, name TEXT, nickname TEXT,
    wins INTEGER, losses INTEGER, draws INTEGER, no_contests INTEGER, w INTEGER, l INTEGER, d INTEGER, nc INTEGER,
    PRIMARY KEY (token, fighter_id, search)) WITHOUT ROWID;
CREATE TABLE site_top_fighters AS SELECT * FROM temp.cards ORDER BY n DESC, name LIMIT 50;

-- Events, newest first, with their fight counts.
CREATE INDEX site_events_date ON events(date);
CREATE TABLE site_events (date TEXT, event_id TEXT, name TEXT, location TEXT, promotion TEXT, n INTEGER,
    PRIMARY KEY (date DESC, event_id)) WITHOUT ROWID;
INSERT INTO site_events SELECT e.date, e.event_id, e.name, e.location, e.promotion, COUNT(f.fight_id)
    FROM events e LEFT JOIN fights f USING (event_id) WHERE e.date IS NOT NULL GROUP BY e.event_id;

-- Promotions and divisions by number, for the leaderboard tables (0 means all). The site
-- lists the promotions with 25 or more events (there are thousands of one-off shows).
CREATE TABLE site_promotions (code INTEGER PRIMARY KEY, promotion TEXT UNIQUE, events INTEGER, listed INTEGER);
INSERT INTO site_promotions (promotion, events, listed)
    SELECT promotion, COUNT(*), COUNT(*) >= 25 FROM events GROUP BY promotion ORDER BY COUNT(*) DESC;
CREATE TABLE site_divisions (code INTEGER PRIMARY KEY, division TEXT UNIQUE);

-- Leaderboard totals per fighter, promotion and division (and year on the stats boards; 0
-- means all). The results board counts every fight, over all years only: by year it would be
-- millions of rows. Each stats board counts the fights its source counted.
CREATE TABLE site_results (promotion INTEGER, division INTEGER, fighter_id TEXT, n INTEGER, w INTEGER,
    fin INTEGER, ko INTEGER, subw INTEGER);
CREATE TABLE site_leaders (board TEXT, promotion INTEGER, division INTEGER, year INTEGER, fighter_id TEXT,
    n INTEGER, w INTEGER, fin INTEGER, ko INTEGER, subw INTEGER, kd INTEGER, sl INTEGER, sa INTEGER,
    tsl INTEGER, tsa INTEGER, tl INTEGER, ta INTEGER, suba INTEGER, ctrl INTEGER, arm INTEGER, leg INTEGER,
    grd INTEGER, PRIMARY KEY (board, promotion, division, year, fighter_id)) WITHOUT ROWID;
-- The boards' definitions, and each board's top 50 per stat and division over all
-- promotions and years (what the page opens on), with names.
CREATE TABLE site_boards (json TEXT);
CREATE TABLE site_top (board TEXT, stat TEXT, division INTEGER, rank INTEGER, fighter_id TEXT, name TEXT,
    n INTEGER, val REAL, PRIMARY KEY (board, stat, division, rank)) WITHOUT ROWID;
"""

# fights and fight_participants, rebuilt from site_fights for the SQL page.
VIEWS_SQL = f"""
DROP TABLE fight_participants;
DROP TABLE fights;
CREATE VIEW fights AS SELECT fight_id, event_id, {", ".join(FIGHT_COLS)} FROM site_fights;
CREATE VIEW fight_participants AS
    SELECT fight_id, a_id AS fighter_id, 0 AS corner, a_res AS result FROM site_fights WHERE a_id IS NOT NULL
    UNION ALL
    SELECT fight_id, b_id, 1, b_res FROM site_fights WHERE b_id IS NOT NULL;
"""


def leaders(conn: sqlite3.Connection) -> None:
    """Fill site_results, site_leaders and site_top: each board's fights, per fighter, rolled
    up over every mix of promotion, division and year being 'all'."""
    conn.executemany("INSERT INTO site_divisions VALUES (?, ?)", list(enumerate(DIVISIONS + ["Other"], 1)))
    for board, b in BOARDS.items():
        src = b["source"]
        counted = (f"AND EXISTS (SELECT 1 FROM {src[0]} r WHERE r.fight_id = x.fight_id AND r.source = '{src[1]}')"
                   if src else "")
        sums = SUMS[src[0]] if src else {}
        stats = "".join(f", (SELECT SUM(r.{sums[c]}) FROM {src[0]} r WHERE r.fight_id = p.fight_id "
                        f"AND r.fighter_id = p.fighter_id AND r.source = '{src[1]}') {c}"
                        if c in sums else f", NULL {c}" for c in STATS) if src else ""
        conn.execute("DROP TABLE IF EXISTS temp.base")
        conn.execute(f"""
            CREATE TEMP TABLE base AS
            SELECT p.fighter_id, sp.code promotion, sd.code division, CAST(substr(e.date, 1, 4) AS INTEGER) year,
                   1 n, p.result = 'W' w,
                   p.result = 'W' AND (x.method LIKE '%KO%' OR x.method LIKE '%Submission%') fin,
                   p.result = 'W' AND x.method LIKE '%KO%' ko,
                   p.result = 'W' AND x.method LIKE '%Submission%' subw{stats}
            FROM fight_participants p JOIN fights x USING (fight_id) JOIN events e USING (event_id)
            JOIN site_divisions sd ON sd.division = division(x.weight_class)
            JOIN site_promotions sp ON sp.promotion = e.promotion
            WHERE e.date IS NOT NULL {counted}""")
        cols = ", ".join(f"SUM({c})" for c in RESULTS + (STATS if src else []))
        table = "site_leaders" if src else "site_results"
        for promo in ("promotion", "0"):
            for div in ("division", "0"):
                for year in ("year", "0") if src else ("0",):
                    keys = ", ".join(k for k in (promo, div, year) if k[0].isalpha())  # a constant isn't a key
                    pick = f"'{board}', {promo}, {div}, {year}" if src else f"{promo}, {div}"
                    conn.execute(f"""
                        INSERT INTO {table} SELECT {pick}, fighter_id, {cols} FROM temp.base
                        GROUP BY {keys + ', ' if keys else ''}fighter_id
                        ORDER BY {keys + ', ' if keys else ''}fighter_id""")
        board_filter = "" if not src else f"AND board = '{board}' AND year = 0"
        for stat, s in b["stats"].items():
            keep = f"{s['expr']} IS NOT NULL" + (f" AND {s['where']}" if s.get("where") else "")
            conn.execute(f"""
                INSERT INTO site_top
                SELECT '{board}', '{stat}', division, rank, fighter_id, f.name, n, val FROM (
                    SELECT division, fighter_id, n, {s['expr']} val,
                           ROW_NUMBER() OVER (PARTITION BY division ORDER BY {s['expr']} DESC, n) rank
                    FROM {table} WHERE promotion = 0 {board_filter} AND {keep})
                JOIN fighters f USING (fighter_id) WHERE rank <= 50""")
    conn.execute("INSERT INTO site_boards VALUES (?)",
                 (json.dumps({k: {c: v for c, v in b.items() if c != "source"} for k, b in BOARDS.items()}),))
    # One promotion's slice is small enough to read whole; all promotions together are ranked
    # through an index per stat.
    conn.execute("CREATE INDEX site_results_slice ON site_results(promotion, division) WHERE promotion <> 0")
    for c in RESULTS:
        conn.execute(f"CREATE INDEX site_results_{c} ON site_results(division, {c} DESC, n) WHERE promotion = 0")


def cluster(conn: sqlite3.Connection, table: str, order: str) -> None:
    """Rewrite a table with its rows stored in `order`, so reading it in that order (through
    its primary key, say) is one long range instead of a request per row."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()[0]
    indexes = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL", (table,))]
    conn.execute(f"ALTER TABLE {table} RENAME TO {table}_unsorted")
    conn.execute(sql)
    conn.execute(f"INSERT INTO {table} SELECT * FROM {table}_unsorted ORDER BY {order}")
    conn.execute(f"DROP TABLE {table}_unsorted")
    for s in indexes:
        conn.execute(s)


def fill(conn: sqlite3.Connection) -> None:
    """Add the site's tables, swap fights and fight_participants for views, then rewrite the
    file in the site's page size."""
    conn.create_function("division", 1, division, deterministic=True)
    conn.create_function("norm", 1, norm, deterministic=True)
    conn.executescript(SITE_SQL)
    rows = conn.execute("SELECT c.*, a.name FROM temp.cards c LEFT JOIN fighter_aliases a USING (fighter_id)")
    seen = set()
    out = []
    for r in rows:
        fid, name, alias = r[0], r[1], r[-1]
        for search in {norm(name), norm(alias)} - {""}:
            if (fid, search) in seen:
                continue
            seen.add((fid, search))
            out += [(t, fid, search, r[7], *r[1:7], *r[8:12]) for t in words(search)]
    conn.executemany("INSERT OR IGNORE INTO site_names VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", out)
    leaders(conn)
    conn.executescript(VIEWS_SQL)
    for table in ("round_stats", "smartcage_round_stats", "sofascore_round_stats"):
        cluster(conn, table, "fight_id, fighter_id, round")
    conn.commit()
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute(f"PRAGMA page_size = {PAGE}")
    conn.execute("VACUUM")
    conn.execute("ANALYZE")
    conn.commit()


def build(src: Path, out: Path, keep: Path | None = None) -> dict:
    """Write the site's database to `out` in chunks, with its config.json; returns the config.
    `keep`: also save the whole file there (scripts/site_json.py reads it)."""
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "site.db"
        shutil.copyfile(src, db)
        conn = sqlite3.connect(db)
        try:
            fill(conn)
        finally:
            conn.close()
        if keep:
            shutil.copyfile(db, keep)
        for old in out.glob("canon.db.*"):
            old.unlink()
        size, digest = db.stat().st_size, hashlib.sha256()
        with open(db, "rb") as fh:
            for i in range((size + CHUNK - 1) // CHUNK):
                data = fh.read(CHUNK)
                digest.update(data)
                (out / f"canon.db.{i:03d}").write_bytes(data)
    config = dict(serverMode="chunked", requestChunkSize=PAGE, databaseLengthBytes=size, serverChunkSize=CHUNK,
                  urlPrefix="canon.db.", suffixLength=3, cacheBust=digest.hexdigest()[:12])
    (out / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config


if __name__ == "__main__":
    args = sys.argv[1:]
    c = build(Path(args[0] if args else "data/canon.db"), Path(args[1] if len(args) > 1 else "site/db"),
              Path(args[2]) if len(args) > 2 else None)
    print(f"{c['databaseLengthBytes'] / 1e6:.0f} MB in {-(-c['databaseLengthBytes'] // CHUNK)} chunks")
