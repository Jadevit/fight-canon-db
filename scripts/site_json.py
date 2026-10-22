"""Write the site's browsing pages as JSON from canon.db: a file per fighter and per event, the
event lists, a search index, the leaderboards' opening views and the landing page's numbers.
A page then reads one small file instead of the database; only the SQL page and filtered
leaderboards query it (as Parquet, scripts/export_parquet.py). The files are the site's
public API too, under /v1/.

    python scripts/site_json.py build [canon.db] [out dir]   # writes <out>/v1/; defaults: data/canon.db site/db
    python scripts/site_json.py upload <out dir>             # to R2: only what changed

The database is read into DuckDB (in memory), where the lists the pages show are built as
`site_` tables: each fight with its event and both fighters, events with their fight counts,
promotion and division codes, and each leaderboard's top 50.

Search: names (fighters) and words (events) are split into files by prefix. A prefix with
more than SEARCH_CAP matches gets a file of its top SEARCH_TOP (most fights; newest events)
and the top SEARCH_EXACT of those whose word is the prefix itself (a common word, such as a
country, would otherwise bring tens of thousands), and a file for each prefix a letter longer.
Upload compares each file's hash with the manifest of the last upload (v1-manifest.json in
the bucket) and sends only the changed ones.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

VERSION = "v1"
SEARCH_CAP = 800
SEARCH_TOP = 100
SEARCH_EXACT = 300
CACHE = "public, max-age=300"  # files keep their names, so a daily update shows up within minutes

# The divisions a bout title can name, the first that matches wins. Keep in step with
# division() in site/app.js (made from this list, in meta.json).
DIVISIONS = ["Women's Strawweight", "Women's Flyweight", "Women's Bantamweight",
             "Women's Featherweight", "Atomweight", "Strawweight", "Flyweight", "Bantamweight", "Featherweight",
             "Lightweight", "Welterweight", "Middleweight", "Light Heavyweight", "Super Heavyweight", "Heavyweight",
             "Catch Weight", "Open Weight"]

# Leaderboards: which fights each board counts (the stats table and source; results count
# every fight), its stats as SQL over each fighter's summed columns (results: n, w, fin, ko,
# subw; stats boards also the STATS columns), and what the site says about it. meta.json
# carries these to the page, which builds filtered boards from them (leaderQuery in app.js).
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
FIGHT_COLS = ["weight_class", "title_fight", "scheduled_rounds", "method", "end_round", "end_time", "time_format",
              "referee", "details", "overturned", "no_contest", "bout_type", "source"]
TABLES = ["events", "fight_participants", "fighters", "fighter_aliases", "event_aliases", "event_cards",
          "promotions", "round_stats", "smartcage_round_stats", "judge_scores", "odds"]


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if not 0x300 <= ord(c) <= 0x36F).lower()


def words(s: str | None) -> set[str]:
    return set(re.findall(r"\w+", norm(s)))


def sql_list(items: list[str]) -> str:
    return ", ".join("'" + i.replace("'", "''") + "'" for i in items)


def division_macro() -> str:
    """SQL: division(bout title), the first of DIVISIONS it names, else 'Other' (a CASE, which
    every DuckDB version takes; app.js builds the same one)."""
    t = "regexp_replace(coalesce(wc, ''), '(?i)catchweight', 'Catch Weight')"
    whens = " ".join(f"WHEN contains({t}, {sql_list([d])}) THEN {sql_list([d])}" for d in DIVISIONS)
    return f"CREATE MACRO division(wc) AS CASE {whens} ELSE 'Other' END"


SITE_SQL = f"""
{division_macro()};

-- Fights with their event and both fighters, in card order (card: the order of `fights`).
CREATE TABLE stat_fights AS SELECT fight_id FROM round_stats UNION SELECT fight_id FROM smartcage_round_stats;
CREATE TABLE site_fights AS
    SELECT x.fight_id, x.event_id, x.card, e.date, e.name AS event, e.promotion, division(x.weight_class) AS division,
           (s.fight_id IS NOT NULL)::INT AS has_stats, a.fighter_id AS a_id, fa.name AS a_name, a.result AS a_res,
           b.fighter_id AS b_id, fb.name AS b_name, b.result AS b_res, {", ".join("x." + c for c in FIGHT_COLS)}
    FROM fights x JOIN events e USING (event_id)
    LEFT JOIN stat_fights s ON s.fight_id = x.fight_id
    LEFT JOIN fight_participants a ON a.fight_id = x.fight_id AND a.corner = 0
    LEFT JOIN fighters fa ON fa.fighter_id = a.fighter_id
    LEFT JOIN fight_participants b ON b.fight_id = x.fight_id AND b.corner = 1
    LEFT JOIN fighters fb ON fb.fighter_id = b.fighter_id;

-- Counts for the landing page.
CREATE TABLE site_stats AS SELECT
    (SELECT COUNT(*) FROM events) events, (SELECT COUNT(*) FROM fights) fights,
    (SELECT COUNT(*) FROM fighters) fighters, (SELECT COUNT(DISTINCT promotion) FROM events) promotions,
    (SELECT MIN(date) FROM events) first_date, (SELECT MAX(date) FROM events) last_date;

-- What a fighter row in a list shows: their record from UFC Stats and their fights here.
CREATE TABLE site_cards AS
    SELECT f.fighter_id, f.name, f.nickname, f.wins, f.losses, f.draws, f.no_contests,
           COUNT(p.fight_id) n, count_if(p.result = 'W') w, count_if(p.result = 'L') l,
           count_if(p.result = 'D') d, count_if(p.result = 'NC') nc
    FROM fighters f LEFT JOIN fight_participants p USING (fighter_id) GROUP BY ALL;

-- Events with their fight counts.
CREATE TABLE site_events AS
    SELECT e.date, e.event_id, e.name, e.location, e.promotion, COUNT(f.fight_id) n
    FROM events e LEFT JOIN fights f USING (event_id) WHERE e.date IS NOT NULL GROUP BY ALL;

-- Promotions with their event counts; the site lists those with 25 or more (there are
-- thousands of one-off shows). Divisions by number (0 means all).
CREATE TABLE site_promotions AS
    SELECT promotion, COUNT(*) events, COUNT(*) >= 25 listed FROM events GROUP BY promotion;
CREATE TABLE site_divisions AS
    SELECT * FROM (VALUES {", ".join(f"({i}, '{d.replace("'", "''")}')" for i, d in enumerate(DIVISIONS + ["Other"], 1))})
    AS t(code, division);
"""


def board_totals(b: dict) -> str:
    """SQL: a board's totals per fighter and division code, and per fighter over all divisions
    (division 0), over every promotion and year."""
    # ILIKE: Sherdog writes some methods in lower case ("submission (armbar)").
    won, ko, sub = "p.result = 'W'", "x.method ILIKE '%KO%'", "x.method ILIKE '%Submission%'"
    cols = (f"COUNT(*) n, count_if({won}) w, count_if({won} AND ({ko} OR {sub})) fin, "
            f"count_if({won} AND {ko}) ko, count_if({won} AND {sub}) subw")
    source = "fight_participants p"
    if b["source"]:  # everyone in each fight this source counted, with their totals if they have rows
        table, src = b["source"]  # (a fighter with nothing in any round can have none)
        sums = SUMS[table]
        source = (f"fight_participants p JOIN (SELECT DISTINCT fight_id FROM {table} WHERE source = '{src}') c "
                  "ON c.fight_id = p.fight_id "
                  f"LEFT JOIN (SELECT fight_id, fighter_id, {', '.join(f'SUM({c}) {k}' for k, c in sums.items())} "
                  f"FROM {table} WHERE source = '{src}' GROUP BY ALL) r "
                  "ON r.fight_id = p.fight_id AND r.fighter_id = p.fighter_id")
        cols += ", " + ", ".join(f"SUM(r.{k}) {k}" if k in sums else f"NULL::BIGINT {k}" for k in STATS)
    return f"""SELECT coalesce(d.code, 0) division, p.fighter_id, {cols}
               FROM {source} JOIN fights x ON x.fight_id = p.fight_id JOIN events e ON e.event_id = x.event_id
               JOIN site_divisions d ON d.division = division(x.weight_class)
               WHERE e.date IS NOT NULL
               GROUP BY GROUPING SETS ((d.code, p.fighter_id), (p.fighter_id))"""


def load(src: Path):
    """canon.db in an in-memory DuckDB, with the site_ tables."""
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL sqlite; LOAD sqlite")
    con.execute("ATTACH ? AS canon (TYPE sqlite, READ_ONLY)", [str(src)])
    for t in TABLES:
        con.execute(f"CREATE TABLE {t} AS SELECT * FROM canon.{t}")
    con.execute("CREATE TABLE fights AS SELECT rowid AS card, * FROM canon.fights")
    con.execute("DETACH canon")
    con.execute(SITE_SQL)
    return con


def key(id_: str) -> str:
    """A file name for an id (ids have colons: sherdog:123)."""
    return id_.replace(":", "~")


def hexname(prefix: str) -> str:
    return prefix.encode().hex()


def shard(id_: str) -> str:
    """Which fx/ file a fight is in: the low byte of its FNV-1a hash (app.js does the same)."""
    h = 0x811C9DC5
    for b in id_.encode():
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return f"{h & 0xFF:02x}"


def rows(con, sql: str, params=()) -> tuple[list[str], list[list]]:
    """(columns, rows) of a query."""
    cur = con.execute(sql, list(params))
    return [d[0] for d in cur.description], [list(r) for r in cur.fetchall()]


def table(con, sql: str, params=()) -> dict:
    """A query as {cols, rows}: a header and lists, much smaller than an object per row."""
    cols, rs = rows(con, sql, params)
    return {"cols": cols, "rows": rs}


class Writer:
    def __init__(self, out: Path):
        self.root = out / VERSION
        if self.root.exists():
            shutil.rmtree(self.root)
        self.n = 0
        self.bytes = 0

    def __call__(self, path: str, obj) -> None:
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=float).encode()
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        self.n += 1
        self.bytes += len(data)


FIGHT_HIST = ["date", "event_id", "event", "promotion", "fight_id", "division", "has_stats", "method", "end_round",
              "end_time", "time_format", "title_fight"]
STAT_SUMS = """SELECT fight_id, fighter_id, any_value(source) source, SUM(knockdowns) kd, SUM(sig_str_land) sl,
    SUM(sig_str_att) sa, SUM(CASE WHEN sig_str_att IS NOT NULL THEN sig_str_land END) sla, SUM(td_land) tl,
    SUM(td_att) ta, SUM(CASE WHEN td_att IS NOT NULL THEN td_land END) tla, SUM(sub_att) sub, SUM(ctrl_sec) ctrl
    FROM round_stats GROUP BY fight_id, fighter_id ORDER BY fight_id, fighter_id"""


def fighters(con, w: Writer) -> None:
    """f/<id>.json: the fighter, their fights newest first (from their side), and per fight the
    stat totals of each side (the opponent's for strike defense)."""
    hist = defaultdict(list)
    _, fs = rows(con, f"""SELECT {", ".join(FIGHT_HIST)}, a_id, a_name, a_res, b_id, b_name, b_res
                          FROM site_fights ORDER BY date DESC, event_id, card""")
    for r in fs:
        base, (a_id, a_name, a_res, b_id, b_name, b_res) = r[:len(FIGHT_HIST)], r[len(FIGHT_HIST):]
        if a_id:
            hist[a_id].append(base + [a_res, b_id, b_name])
        if b_id:
            hist[b_id].append(base + [b_res, a_id, a_name])
    sums = defaultdict(dict)
    for r in rows(con, STAT_SUMS)[1]:
        sums[r[0]][r[1]] = r
    stats = defaultdict(list)
    for fight, sides in sums.items():
        for fid, r in sides.items():
            opp = next((o for o in sides.values() if o[1] != fid), None)
            stats[fid].append([fight, *r[2:], opp[6] if opp else None, opp[5] if opp else None])
    sherdog = defaultdict(list)
    for fid, sid in rows(con, "SELECT fighter_id, source_id FROM fighter_aliases WHERE source = 'sherdog' "
                              "ORDER BY fighter_id, source_id")[1]:
        sherdog[fid].append(sid)
    cols, people = rows(con, """SELECT fighter_id, name, nickname, dob, height_in, reach_in, weight_lbs, stance,
                                       nationality, wins, losses, draws, no_contests FROM fighters ORDER BY fighter_id""")
    for r in people:
        fid = r[0]
        w(f"f/{key(fid)}.json", {
            "fighter": dict(zip(cols, r)),
            "sherdog": sherdog.get(fid, []),
            "fights": {"cols": FIGHT_HIST + ["result", "opp_id", "opp"], "rows": hist.get(fid, [])},
            "stats": {"cols": ["fight_id", "source", "kd", "sl", "sa", "sla", "tl", "ta", "tla", "sub", "ctrl",
                               "opp_sl", "opp_sa"], "rows": stats.get(fid, [])},
        })


def grouped(con, sql: str, by: int = 0) -> tuple[list[str], dict]:
    """Run sql; return its columns without column `by`, and its rows grouped by that column."""
    cols, rs = rows(con, sql)
    out = defaultdict(list)
    for r in rs:
        out[r[by]].append([v for i, v in enumerate(r) if i != by])
    return [c for i, c in enumerate(cols) if i != by], out


def events(con, w: Writer) -> None:
    """e/<id>.json: the event and its card, each bout with its round stats, judges' cards and
    odds; fx/<xx>.json: which event each fight is on, for links to a fight alone."""
    fcols, fights = grouped(con, """SELECT event_id, fight_id, card, division, has_stats, a_id, a_name, a_res,
        b_id, b_name, b_res, weight_class, title_fight, scheduled_rounds, method, end_round, end_time, time_format,
        referee, details, overturned, no_contest, source FROM site_fights ORDER BY event_id, card""")
    rcols, rounds = grouped(con, "SELECT * FROM round_stats ORDER BY fight_id, round, fighter_id")
    ccols, cage = grouped(con, "SELECT * FROM smartcage_round_stats ORDER BY fight_id, round, fighter_id")
    scols, scores = grouped(con, "SELECT fight_id, fighter_id, round, judge, score, deduction FROM judge_scores "
                                 "ORDER BY fight_id, judge, round, fighter_id")
    ocols, odds = grouped(con, "SELECT fight_id, fighter_id, open, close, close_low, close_high FROM odds "
                               "WHERE book = 'all' ORDER BY fight_id, fighter_id, source")
    aliases = defaultdict(lambda: defaultdict(list))
    for eid, src, sid in rows(con, "SELECT event_id, source, source_id FROM event_aliases "
                                   "WHERE source IN ('sherdog', 'pfl') ORDER BY event_id, source, source_id")[1]:
        aliases[eid][src].append(sid)
    src_at = rcols.index("source")
    shards = defaultdict(dict)
    cols, evs = rows(con, """SELECT e.event_id, e.name, e.date, e.location, e.promotion, e.source, p.coverage,
                                    k.complete card_complete
                             FROM events e LEFT JOIN promotions p USING (promotion)
                             LEFT JOIN event_cards k USING (event_id) ORDER BY e.event_id""")
    for r in evs:
        eid = r[0]
        card = fights.get(eid, [])
        stats, cards, lines = {}, {}, {}
        for f in card:
            fight = f[0]
            if fight in rounds:
                stats[fight] = {"source": rounds[fight][0][src_at], "cage": False, "cols": rcols, "rows": rounds[fight]}
            elif fight in cage:
                stats[fight] = {"source": "smartcage", "cage": True, "cols": ccols, "rows": cage[fight]}
            if fight in scores:
                cards[fight] = scores[fight]
            if fight in odds:
                lines[fight] = odds[fight]
            shards[shard(fight)][fight] = eid
        w(f"e/{key(eid)}.json", {
            "event": dict(zip(cols, r)), "aliases": aliases.get(eid, {}),
            "fights": {"cols": fcols, "rows": card},
            "stats": stats,
            "scores": {"cols": scols, "fights": cards},
            "odds": {"cols": ocols, "fights": lines},
        })
    for name, m in shards.items():
        w(f"fx/{name}.json", dict(sorted(m.items())))


def event_lists(con, w: Writer) -> None:
    """events/y/<year>.json, events/p/<promotion's name in hex>.json (listed ones)."""
    cols = "event_id, name, date, location, promotion, n"
    years = [y for (y,) in rows(con, "SELECT DISTINCT substr(date, 1, 4) y FROM site_events ORDER BY 1")[1]]
    for y in years:
        w(f"events/y/{y}.json", table(con, f"SELECT {cols} FROM site_events WHERE date >= ? AND date < ? "
                                           "ORDER BY date DESC, event_id", (y, str(int(y) + 1))))
    for (promo,) in rows(con, "SELECT promotion FROM site_promotions WHERE listed ORDER BY promotion")[1]:
        w(f"events/p/{hexname(promo)}.json", table(con, f"SELECT {cols} FROM site_events WHERE promotion = ? "
                                                        "ORDER BY date DESC, event_id", (promo,)))


def trie(w: Writer, folder: str, cols: list[str], entries: list[tuple[set[str], list]]) -> None:
    """Search files for entries (their words, the row shown), already in rank order: a file per
    two-letter prefix, split a letter deeper while a prefix has more than SEARCH_CAP matches."""
    def emit(prefix: str, items: list[int]) -> None:
        if len(items) <= SEARCH_CAP or len(prefix) >= 16:
            w(f"{folder}/{hexname(prefix)}.json", {"split": False, "cols": cols, "rows": [entries[i][1] for i in items]})
            return
        exact = [i for i in items if prefix in entries[i][0]][:SEARCH_EXACT]
        keep = sorted(set(items[:SEARCH_TOP]) | set(exact))
        w(f"{folder}/{hexname(prefix)}.json", {"split": True, "cols": cols, "rows": [entries[i][1] for i in keep]})
        children = defaultdict(list)
        for i in items:
            for nxt in {wd[:len(prefix) + 1] for wd in entries[i][0] if wd.startswith(prefix) and len(wd) > len(prefix)}:
                children[nxt].append(i)
        for nxt, its in sorted(children.items()):
            emit(nxt, its)

    top = defaultdict(list)
    for i, (ws, _) in enumerate(entries):
        for p in sorted({wd[:2] for wd in ws if len(wd) >= 2}):
            top[p].append(i)
    for p, items in sorted(top.items()):
        emit(p, items)


def search(con, w: Writer) -> None:
    """sf/: fighters by every word of every name they go by, most fights first.
    se/: events by every word of their name and place, newest first."""
    names = defaultdict(set)
    for fid, name in rows(con, "SELECT fighter_id, name FROM fighters UNION ALL "
                               "SELECT fighter_id, name FROM fighter_aliases")[1]:
        names[fid] |= words(name)
    entries = []
    for r in rows(con, """SELECT fighter_id, name, nickname, wins, losses, draws, no_contests, n, w, l, d, nc
                          FROM site_cards ORDER BY n DESC, name, fighter_id""")[1]:
        ws = names.get(r[0], set())
        if ws:
            entries.append((ws, r + [" ".join(sorted(ws))]))
    trie(w, "sf", ["fighter_id", "name", "nickname", "wins", "losses", "draws", "no_contests", "n", "w", "l", "d",
                   "nc", "words"], entries)
    evs = []
    for eid, name, date, loc, promo, n in rows(
            con, "SELECT event_id, name, date, location, promotion, n FROM site_events ORDER BY date DESC, event_id")[1]:
        ws = words(f"{name} {loc or ''}")
        evs.append((ws, [eid, name, date, loc, promo, n, " ".join(sorted(ws))]))
    trie(w, "se", ["event_id", "name", "date", "location", "promotion", "n", "words"], evs)


def leaders(con, w: Writer) -> None:
    """l/<board>/<stat>.json: each board's opening view (all promotions and years, min. 1 fight),
    the top 50 per division code (0 = all)."""
    for board, b in BOARDS.items():
        con.execute(f"CREATE OR REPLACE TEMP TABLE totals AS {board_totals(b)}")
        for stat, s in b["stats"].items():
            keep = f"{s['expr']} IS NOT NULL" + (f" AND {s['where']}" if s.get("where") else "")
            divs = defaultdict(list)
            for div, fid, name, n, val in rows(con, f"""
                    SELECT t.division, t.fighter_id, f.name, t.n, t.val FROM (
                        SELECT division, fighter_id, n, {s['expr']} val,
                               ROW_NUMBER() OVER (PARTITION BY division ORDER BY {s['expr']} DESC, n, fighter_id) rank
                        FROM totals WHERE {keep}) t
                    JOIN fighters f USING (fighter_id) WHERE rank <= 50 ORDER BY t.division, rank""")[1]:
                divs[str(div)].append([fid, name, n, val])
            w(f"l/{board}/{stat}.json", divs)


def meta(con, w: Writer) -> None:
    """meta.json: the landing page's numbers, the latest event, and the lists the filters offer."""
    stats = table(con, "SELECT * FROM site_stats")
    w("meta.json", {
        "stats": dict(zip(stats["cols"], stats["rows"][0])),
        "latest": table(con, "SELECT event_id, name, date, location, promotion, n FROM site_events "
                             "ORDER BY date DESC, event_id LIMIT 1"),
        "promotions": table(con, "SELECT promotion, events FROM site_promotions WHERE listed "
                                 "ORDER BY events DESC, promotion"),
        "divisions": table(con, "SELECT code, division FROM site_divisions ORDER BY code"),
        "boards": BOARDS,
        "sums": SUMS, "stats_cols": STATS,  # stats boards: column -> its stats table's column
        "top_fighters": table(con, "SELECT * FROM site_cards ORDER BY n DESC, name, fighter_id LIMIT 50"),
    })


def build(src: Path, out: Path) -> Writer:
    w = Writer(out)
    con = load(src)
    try:
        for step in (meta, leaders, event_lists, search, events, fighters):
            step(con, w)
    finally:
        con.close()
    return w


def upload(out: Path, workers: int = 32) -> None:
    """Send the files that changed since the last upload to R2 (R2_BUCKET; AWS_ENDPOINT_URL and
    keys from the environment), delete the ones that are gone, then save the new manifest."""
    import boto3
    s3 = boto3.client("s3", endpoint_url=os.environ["AWS_ENDPOINT_URL"])
    bucket, manifest = os.environ["R2_BUCKET"], f"{VERSION}-manifest.json"
    root = out / VERSION
    files = {f"{VERSION}/{p.relative_to(root).as_posix()}": p for p in root.rglob("*.json")}
    hashes = {k: hashlib.md5(p.read_bytes()).hexdigest() for k, p in files.items()}
    try:
        old = json.loads(s3.get_object(Bucket=bucket, Key=manifest)["Body"].read())
    except s3.exceptions.NoSuchKey:
        old = {}
    changed = [k for k, h in hashes.items() if old.get(k) != h]
    gone = [k for k in old if k not in hashes]

    def put(k: str) -> None:
        s3.put_object(Bucket=bucket, Key=k, Body=files[k].read_bytes(), ContentType="application/json",
                      CacheControl=CACHE)

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(put, changed))
    for i in range(0, len(gone), 1000):
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in gone[i:i + 1000]], "Quiet": True})
    s3.put_object(Bucket=bucket, Key=manifest, Body=json.dumps(hashes).encode(), ContentType="application/json")
    print(f"{len(files)} files: {len(changed)} uploaded, {len(gone)} deleted")


if __name__ == "__main__":
    cmd, *rest = sys.argv[1:] or [""]
    if cmd == "build":
        w = build(Path(rest[0] if rest else "data/canon.db"), Path(rest[1] if len(rest) > 1 else "site/db"))
        print(f"{w.n} files, {w.bytes / 1e6:.0f} MB")
    elif cmd == "upload":
        upload(Path(rest[0]))
    else:
        sys.exit(__doc__)
