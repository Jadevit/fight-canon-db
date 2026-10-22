"""Build the working copy of canon.db that scripts/site_json.py writes the site's pages from:
`site_` tables with what the pages list (search names, events, leaderboard totals and their
opening views), each row holding what it shows.

- site_fights: each fight with its event's date and name and both fighters, in card order.
- site_results / site_leaders: leaderboard totals per fighter, promotion, division (and year
  on the stats boards); site_top: each board's opening view. Boards are defined in BOARDS.

    python scripts/site_db.py [canon.db] [out file]   # defaults: data/canon.db site/db/site.db

The SQL page doesn't read this file: it queries canon.db itself, as Parquet
(scripts/export_parquet.py).
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from pathlib import Path

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
        note="Counts pro fights in this database, which doesn't have every fighter's whole career."),
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
    conn.execute("INSERT INTO site_boards VALUES (?)", (json.dumps(BOARDS),))


def fill(conn: sqlite3.Connection) -> None:
    """Add the site's tables."""
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
    conn.commit()


def build(src: Path, out: Path) -> None:
    """Write the site's working copy of `src` to `out`."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "site.db"
        shutil.copyfile(src, db)
        conn = sqlite3.connect(db)
        try:
            fill(conn)
        finally:
            conn.close()
        shutil.copyfile(db, out)


if __name__ == "__main__":
    args = sys.argv[1:]
    out = Path(args[1] if len(args) > 1 else "site/db/site.db")
    build(Path(args[0] if args else "data/canon.db"), out)
    print(f"{out}: {out.stat().st_size / 1e6:.0f} MB")
