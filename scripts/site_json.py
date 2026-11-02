"""Write the site's browsing pages as JSON from canon.db: a file per fighter and per event, the
event lists, a search index, the leaderboards' opening views and the landing page's numbers.
A page then reads one small file instead of the database; only the SQL page and filtered
leaderboards query it (as Parquet, scripts/export_parquet.py). The files are the site's
public API too, under /v1/.

    python scripts/site_json.py build [canon.db] [out dir]   # writes <out>/v1/; defaults: data/canon.db site/db
    python scripts/site_json.py upload <out dir>             # to R2: only what changed
    python scripts/site_json.py purge <out dir>              # then clear those from Cloudflare's cache

The database is read into DuckDB (in memory), where the lists the pages show are built as
`site_` tables: each fight with its event and both fighters, events with their fight counts,
promotion and division codes, and each leaderboard's top 50.

Search: names (fighters) and words (events) are split into files by prefix. A prefix with
more than SEARCH_CAP matches gets a file of its top SEARCH_TOP (most fights; newest events)
and the top SEARCH_EXACT of those whose word is the prefix itself (a common word, such as a
country, would otherwise bring tens of thousands), and a file for each prefix a letter longer.

What each leaderboard counts is in site_boards.py, the Query page's help in site_query.py, and
sending the files in site_upload.py.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

from site_boards import BOARDS, DIVISIONS, STATS, SUMS, board_totals, division_macro, sql_list
from site_query import query_help
from site_upload import VERSION, purge, upload

SEARCH_CAP = 800
SEARCH_TOP = 100
SEARCH_EXACT = 300
MATCHUP_DAYS, MATCHUP_COUNT = 7, 30  # the Fighters page's recent matchups
RANDOM_FILES, RANDOM_MIN = 100, 5  # its random fighter: files of ids, fighters with this many fights

FIGHT_COLS = ["weight_class", "title_fight", "scheduled_rounds", "method", "end_round", "end_time", "time_format",
              "referee", "details", "overturned", "no_contest", "bout_type"]
TABLES = ["events", "fight_participants", "fighters", "fighter_aliases", "event_aliases", "fight_aliases",
          "event_cards", "promotions", "round_stats", "smartcage_round_stats", "judge_scores", "odds"]
# Stats sources in the order a fight's are shown: the promotions' own first.
STATS_ORDER = ["ufcstats", "pfl", "smartcage", "oktagon", "sofascore"]
LINKED = ("ufcstats", "sherdog", "pfl")  # sources the pages link to, by their ids


FIGHT_HIST = ["date", "event_id", "event", "promotion", "fight_id", "division", "has_stats", "method", "end_round",
              "end_time", "time_format", "title_fight"]
STAT_SUMS = """SELECT fight_id, fighter_id, source, SUM(knockdowns) kd, SUM(sig_str_land) sl,
    SUM(sig_str_att) sa, SUM(CASE WHEN sig_str_att IS NOT NULL THEN sig_str_land END) sla, SUM(td_land) tl,
    SUM(td_att) ta, SUM(CASE WHEN td_att IS NOT NULL THEN td_land END) tla, SUM(sub_att) sub, SUM(ctrl_sec) ctrl
    FROM round_stats GROUP BY fight_id, fighter_id, source ORDER BY fight_id, source, fighter_id"""


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if not 0x300 <= ord(c) <= 0x36F).lower()


def words(s: str | None) -> set[str]:
    return set(re.findall(r"\w+", norm(s)))


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


def hexname(prefix: str) -> str:
    return prefix.encode().hex()


def shard(id_) -> str:
    """Which fx/ file a fight is in: the low byte of its id's FNV-1a hash (app.js does the same)."""
    h = 0x811C9DC5
    for b in str(id_).encode():
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


def fighters(con, w: Writer) -> None:
    """f/<id>.json: the fighter, their fights newest first (from their side), and per fight the
    stat totals of each side by source (the opponent's for strike defense)."""
    hist = defaultdict(list)
    _, fs = rows(con, f"""SELECT {", ".join(FIGHT_HIST)}, a_id, a_name, a_res, b_id, b_name, b_res
                          FROM site_fights ORDER BY date DESC, event_id, card""")
    for r in fs:
        base, (a_id, a_name, a_res, b_id, b_name, b_res) = r[:len(FIGHT_HIST)], r[len(FIGHT_HIST):]
        if a_id:
            hist[a_id].append(base + [a_res, b_id, b_name])
        if b_id:
            hist[b_id].append(base + [b_res, a_id, a_name])
    sums = defaultdict(dict)  # (fight, source) -> fighter -> totals
    for r in rows(con, STAT_SUMS)[1]:
        sums[r[0], r[2]][r[1]] = r
    stats = defaultdict(list)
    for (fight, _), sides in sums.items():
        for fid, r in sides.items():
            opp = next((o for o in sides.values() if o[1] != fid), None)
            stats[fid].append([fight, *r[2:], opp[6] if opp else None, opp[5] if opp else None])
    aliases = defaultdict(lambda: defaultdict(list))
    for fid, src, sid in rows(con, f"SELECT DISTINCT fighter_id, source, source_id FROM fighter_aliases "
                                   f"WHERE source IN ({sql_list(LINKED)}) ORDER BY fighter_id, source, source_id")[1]:
        aliases[fid][src].append(sid)
    cols, people = rows(con, """SELECT fighter_id, name, nickname, dob, height_in, reach_in, weight_lbs, stance,
                                       nationality, wins, losses, draws, no_contests FROM fighters ORDER BY fighter_id""")
    for r in people:
        fid = r[0]
        w(f"f/{fid}.json", {
            "fighter": dict(zip(cols, r)),
            "aliases": aliases.get(fid, {}),
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
    """e/<id>.json: the event and its card, each bout with its round stats (a list, one per
    source, official sources first), judges' cards and odds; fx/<xx>.json: which event each fight is on, for links to a fight alone."""
    fcols, fights = grouped(con, """SELECT event_id, fight_id, card, division, has_stats, a_id, a_name, a_res,
        b_id, b_name, b_res, weight_class, title_fight, scheduled_rounds, method, end_round, end_time, time_format,
        referee, details, overturned, no_contest FROM site_fights ORDER BY event_id, card""")
    rcols, rounds = grouped(con, "SELECT * FROM round_stats ORDER BY fight_id, source, round, fighter_id")
    ccols, cage = grouped(con, "SELECT * FROM smartcage_round_stats ORDER BY fight_id, round, fighter_id")
    scols, scores = grouped(con, "SELECT fight_id, fighter_id, round, judge, score, deduction, source FROM judge_scores "
                                 "ORDER BY fight_id, judge, round, fighter_id")
    ocols, odds = grouped(con, "SELECT fight_id, fighter_id, open, close, close_low, close_high FROM odds "
                               "WHERE book = 'all' ORDER BY fight_id, fighter_id, source")
    aliases = defaultdict(lambda: defaultdict(list))
    for eid, src, sid in rows(con, f"SELECT event_id, source, source_id FROM event_aliases "
                                   f"WHERE source IN ({sql_list(LINKED)}) ORDER BY event_id, source, source_id")[1]:
        aliases[eid][src].append(sid)
    ufc_fights = dict(rows(con, "SELECT fight_id, source_id FROM fight_aliases WHERE source = 'ufcstats'")[1])
    src_at = rcols.index("source")
    shards = defaultdict(dict)
    cols, evs = rows(con, """SELECT e.event_id, e.name, e.date, e.location, e.promotion, p.coverage,
                                    k.complete card_complete
                             FROM events e LEFT JOIN promotions p USING (promotion)
                             LEFT JOIN event_cards k USING (event_id) ORDER BY e.event_id""")
    for r in evs:
        eid = r[0]
        card = fights.get(eid, [])
        stats, cards, lines, ufc = {}, {}, {}, {}
        for f in card:
            fight = f[0]
            sets = defaultdict(list)
            for row in rounds.get(fight, []):
                sets[row[src_at]].append(row)
            found = [{"source": src, "cage": False, "cols": rcols, "rows": rs} for src, rs in sets.items()]
            if fight in cage:
                found.append({"source": "smartcage", "cage": True, "cols": ccols, "rows": cage[fight]})
            if found:
                stats[fight] = sorted(found, key=lambda st: STATS_ORDER.index(st["source"]))
            if fight in scores:
                cards[fight] = scores[fight]
            if fight in odds:
                lines[fight] = odds[fight]
            if fight in ufc_fights:
                ufc[fight] = ufc_fights[fight]
            shards[shard(fight)][fight] = eid
        w(f"e/{eid}.json", {
            "event": dict(zip(cols, r)), "aliases": aliases.get(eid, {}), "ufcstats": ufc,
            "fights": {"cols": fcols, "rows": card},
            "stats": stats,
            "scores": {"cols": scols, "fights": cards},
            "odds": {"cols": ocols, "fights": lines},
        })
    for name, m in shards.items():
        w(f"fx/{name}.json", dict(sorted(m.items())))


def fighters_page(con, w: Writer) -> None:
    """fighters.json: recent matchups (fighter pairs from the last MATCHUP_DAYS of fights, the
    pairs whose fighters both have the most fights first; nothing about the fight itself) and
    how many random-pick files there are. fr/<k>.json: the ids of fighters with RANDOM_MIN
    fights or more whose id ends in k (mod RANDOM_FILES), so a new fighter changes one file."""
    cols, pairs = rows(con, f"""
        WITH recent AS (SELECT * FROM site_fights WHERE a_id IS NOT NULL AND b_id IS NOT NULL
                        AND date::DATE > (SELECT max(date)::DATE FROM site_fights) - INTERVAL {MATCHUP_DAYS} DAY)
        SELECT a.*, b.* FROM recent x JOIN site_cards a ON a.fighter_id = x.a_id JOIN site_cards b ON b.fighter_id = x.b_id
        ORDER BY least(a.n, b.n) DESC, greatest(a.n, b.n) DESC, x.date DESC, x.fight_id LIMIT {MATCHUP_COUNT}""")
    half = len(cols) // 2
    w("fighters.json", {"cols": cols[:half], "matchups": [[r[:half], r[half:]] for r in pairs],
                        "random": RANDOM_FILES})
    pool = defaultdict(list)
    for (fid,) in rows(con, f"SELECT fighter_id FROM site_cards WHERE n >= {RANDOM_MIN} ORDER BY fighter_id")[1]:
        pool[fid % RANDOM_FILES].append(fid)
    for k in range(RANDOM_FILES):
        w(f"fr/{k}.json", pool.get(k, []))


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


def trie(w: Writer, folder: str, cols: list[str], entries: list[tuple[set[str], list]]) -> list[str]:
    """Search files for entries (their words, the row shown), already in rank order: a file per
    two-letter prefix, split a letter deeper while a prefix has more than SEARCH_CAP matches.
    Returns the split prefixes, so the page can read the deepest file a term needs at once."""
    splits = []

    def emit(prefix: str, items: list[int]) -> None:
        if len(items) <= SEARCH_CAP or len(prefix) >= 16:
            w(f"{folder}/{hexname(prefix)}.json", {"split": False, "cols": cols, "rows": [entries[i][1] for i in items]})
            return
        exact = [i for i in items if prefix in entries[i][0]][:SEARCH_EXACT]
        keep = sorted(set(items[:SEARCH_TOP]) | set(exact))
        w(f"{folder}/{hexname(prefix)}.json", {"split": True, "cols": cols, "rows": [entries[i][1] for i in keep]})
        splits.append(prefix)
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
    return splits


def search(con, w: Writer) -> dict:
    """sf/: fighters by every word of every name they go by, most fights first.
    se/: events by every word of their name and place, newest first. Returns each folder's
    split prefixes."""
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
    sf = trie(w, "sf", ["fighter_id", "name", "nickname", "wins", "losses", "draws", "no_contests", "n", "w", "l", "d",
                        "nc", "words"], entries)
    evs = []
    for eid, name, date, loc, promo, n in rows(
            con, "SELECT event_id, name, date, location, promotion, n FROM site_events ORDER BY date DESC, event_id")[1]:
        ws = words(f"{name} {loc or ''}")
        evs.append((ws, [eid, name, date, loc, promo, n, " ".join(sorted(ws))]))
    se = trie(w, "se", ["event_id", "name", "date", "location", "promotion", "n", "words"], evs)
    return {"sf": sf, "se": se}


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


def meta(con, w: Writer, splits: dict) -> None:
    """meta.json: the landing page's numbers, the latest event, the lists the filters offer, and
    the search files that are split."""
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
        "splits": splits,
    })


def build(src: Path, out: Path) -> Writer:
    w = Writer(out)
    con = load(src)
    try:
        meta(con, w, search(con, w))
        w("query.json", query_help())
        for step in (leaders, fighters_page, event_lists, events, fighters):
            step(con, w)
    finally:
        con.close()
    return w


if __name__ == "__main__":
    cmd, *rest = sys.argv[1:] or [""]
    if cmd == "build":
        w = build(Path(rest[0] if rest else "data/canon.db"), Path(rest[1] if len(rest) > 1 else "site/db"))
        print(f"{w.n} files, {w.bytes / 1e6:.0f} MB")
    elif cmd == "upload":
        upload(Path(rest[0]))
    elif cmd == "purge":
        purge(Path(rest[0]))
    else:
        sys.exit(__doc__)
