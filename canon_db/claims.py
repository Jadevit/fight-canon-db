"""Claims: what each source's page says, and the main tables worked out from them.

A loader finds or makes our row (canon_db.ids), records its alias, and writes what the page
says as a claim (claim(), sides()): one row per page in fighter_claims, event_claims,
fight_claims and participant_claims. resolve() then works out fighters, events, fights and
fight_participants from every claim at once, so no value depends on the order sources ran in:

- A column takes the best-ranked claim that has a value (source_tiers: tier, then priority),
  so a blank falls through to the next source. Third-party sources never fill these tables.
- A fight's two fighters, results and corners come from one source: the best-ranked whose two
  sides are both linked to fighters of ours. Two sources that disagree on a result are flagged
  in review_flags, whichever wins.
- Rows no ranked claim speaks for are left as they are.
- Rules that aren't any page's word run after (RULES), such as sex from a fight's division.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter

from canon_db.db import COLUMNS, site

# Tiers that fill the main tables, best first. third_party is kept beside them, never in them.
RANKED = ("correction", "official", "primary", "secondary", "legacy")

# kind -> (claims table, id column, main table, the source_tiers kind its columns rank by)
TABLES = {"fighter": ("fighter_claims", "fighter_id", "fighters", "fighter"),
          "event": ("event_claims", "event_id", "events", "event"),
          "fight": ("fight_claims", "fight_id", "fights", "fight")}
KEY = ("source", "site", "source_id")
SIDE_COLS = ("fighter_id", "fighter_source_id", "name", "result", "corner")
# Main-table columns a rule works out, not a claim (a source's printed record stays in its claim).
COUNTED = {("fighters", c) for c in ("wins", "losses", "draws", "no_contests")}
CHECKS = ("results disagree", "birth dates disagree")  # review_flags rows resolve() rebuilds
dirty = False  # claims changed since the last resolve() (claim(), sides(), and ids' merges and deletions)


def claim(conn: sqlite3.Connection, kind: str, id_: int, source: str, source_id: str, values: dict,
          site_: str | None = None, mode: str = "whole") -> None:
    """Record what `source`'s page (`source_id` on `site_`, its default site if not given) says about
    our row `id_`. `mode`, for a source whose several pages each say part of it:
      whole:  the claim is this page's, replaced whole (what it doesn't give is blank);
      fill:   the values given replace the claim's, and its blanks keep what it had;
      blanks: the values given only fill the claim's blanks (a page that says less, and less well)."""
    global dirty
    dirty = True
    table, col, _, _ = TABLES[kind]
    cols = [c for c in values if c in COLUMNS[table]]
    if len(cols) != len(values):
        raise KeyError(f"{table} has no {sorted(set(values) - set(cols))}")
    where = site_ or site(source)
    if mode == "fill":  # (with nothing to fill, only the row's id is set)
        sets = ", ".join(f"{c} = coalesce(excluded.{c}, {c})" for c in cols)
    elif mode == "blanks":
        sets = ", ".join(f"{c} = coalesce({c}, excluded.{c})" for c in cols)
    elif mode == "whole":
        sets = ", ".join(f"{c} = excluded.{c}" for c in COLUMNS[table] if c not in (*KEY, col))
    else:
        raise ValueError(f"no claim mode {mode!r}")
    conn.execute(f"INSERT INTO {table} ({col}, source, site, source_id, {', '.join(cols)}) "
                 f"VALUES (?,?,?,?{', ?' * len(cols)}) ON CONFLICT DO UPDATE SET {col} = excluded.{col}"
                 f"{', ' + sets if sets else ''}", (id_, source, where, str(source_id), *(values[c] for c in cols)))


def sides(conn: sqlite3.Connection, fight: int, source: str, source_id: str, people: list[dict],
          site_: str | None = None) -> None:
    """Record a source's two sides of a bout, in its order: [{fighter_id, fighter_source_id, name,
    result, corner}] (missing keys are blank)."""
    global dirty
    if len(people) != 2:
        raise ValueError(f"a bout has two sides, not {len(people)}")
    dirty = True
    where = site_ or site(source)
    conn.execute("DELETE FROM participant_claims WHERE source = ? AND site = ? AND source_id = ?",
                 (source, where, str(source_id)))
    conn.executemany(f"INSERT INTO participant_claims (fight_id, source, site, source_id, side, {', '.join(SIDE_COLS)}) "
                     f"VALUES (?,?,?,?,?{', ?' * len(SIDE_COLS)})",
                     [(fight, source, where, str(source_id), n, *(p.get(c) for c in SIDE_COLS))
                      for n, p in enumerate(people)])


def _ranks(conn: sqlite3.Connection) -> None:
    """temp.ranks: (source, kind, rank), lower first, for the tiers that fill the main tables."""
    conn.execute("DROP TABLE IF EXISTS temp.ranks")
    conn.execute("CREATE TEMP TABLE ranks (source TEXT, kind TEXT, rank INTEGER, PRIMARY KEY (source, kind))")
    order = " ".join(f"WHEN '{t}' THEN {i}" for i, t in enumerate(RANKED))
    conn.execute(f"INSERT INTO temp.ranks SELECT source, kind, (CASE tier {order} END) * 1000 + priority "
                 f"FROM source_tiers WHERE tier IN ({', '.join(repr(t) for t in RANKED)})")


def _pick(conn: sqlite3.Connection, kind: str) -> int:
    """Set every column of `kind`'s main table that its claims have, on the rows a ranked claim
    speaks for. Returns those rows' count."""
    claims, col, main, tier_kind = TABLES[kind]
    info = {r[1]: r for r in conn.execute(f"PRAGMA table_info({main})")}
    spoken = (f"SELECT k.{col} FROM {claims} k JOIN temp.ranks r ON r.source = k.source AND r.kind = '{tier_kind}'")
    sets = []
    for c in COLUMNS[claims]:
        if c not in info or c == col or (main, c) in COUNTED:
            continue
        best = (f"(SELECT k.{c} FROM {claims} k JOIN temp.ranks r ON r.source = k.source AND r.kind = '{tier_kind}' "
                f"WHERE k.{col} = {main}.{col} AND k.{c} IS NOT NULL ORDER BY r.rank, k.source, k.site, k.source_id LIMIT 1)")
        _, _, _, notnull, default, _ = info[c]
        if notnull:  # a blank falls back to the column's default, or keeps what's there
            best = f"coalesce({best}, {default if default is not None else c})"
        sets.append(f"{c} = {best}")
    conn.execute(f"UPDATE {main} SET {', '.join(sets)} WHERE {col} IN ({spoken})")  # each row written once
    return conn.execute(f"SELECT count(DISTINCT {col}) FROM ({spoken})").fetchone()[0]


def _participants(conn: sqlite3.Connection) -> int:
    """fight_participants from the best-ranked source whose two sides are both linked. Corners are
    that source's where it gives them, else by fighter id. Returns the fights set."""
    conn.execute("DROP TABLE IF EXISTS temp.best_sides")
    conn.execute("""
        CREATE TEMP TABLE best_sides AS
        WITH whole AS (
            SELECT p.fight_id, p.source, p.site, p.source_id, r.rank
            FROM participant_claims p JOIN temp.ranks r ON r.source = p.source AND r.kind = 'result'
            GROUP BY p.fight_id, p.source, p.site, p.source_id
            HAVING count(p.fighter_id) = 2 AND count(DISTINCT p.fighter_id) = 2),
        best AS (
            SELECT *, row_number() OVER (PARTITION BY fight_id ORDER BY rank, source, site, source_id) AS n FROM whole)
        SELECT p.fight_id, p.fighter_id, p.result,
               CASE WHEN count(p.corner) OVER (PARTITION BY p.fight_id) = 2 THEN p.corner
                    ELSE row_number() OVER (PARTITION BY p.fight_id ORDER BY p.fighter_id) - 1 END AS corner
        FROM best b JOIN participant_claims p USING (fight_id, source, site, source_id) WHERE b.n = 1""")
    conn.execute("CREATE INDEX temp.best_sides_fight ON best_sides(fight_id)")
    # Only fights whose sides changed are rewritten.
    conn.execute("""
        CREATE TEMP TABLE changed_sides AS
        SELECT fight_id FROM (SELECT fight_id, fighter_id, corner, result FROM temp.best_sides
                              EXCEPT SELECT fight_id, fighter_id, corner, result FROM fight_participants)
        UNION
        SELECT fight_id FROM (SELECT fight_id, fighter_id, corner, result FROM fight_participants
                              WHERE fight_id IN (SELECT fight_id FROM temp.best_sides)
                              EXCEPT SELECT fight_id, fighter_id, corner, result FROM temp.best_sides)""")
    conn.execute("DELETE FROM fight_participants WHERE fight_id IN (SELECT fight_id FROM temp.changed_sides)")
    conn.execute("INSERT INTO fight_participants (fight_id, fighter_id, corner, result) "
                 "SELECT fight_id, fighter_id, corner, result FROM temp.best_sides "
                 "WHERE fight_id IN (SELECT fight_id FROM temp.changed_sides)")
    n = conn.execute("SELECT count(DISTINCT fight_id) FROM temp.best_sides").fetchone()[0]
    conn.execute("DROP TABLE temp.best_sides")
    conn.execute("DROP TABLE temp.changed_sides")
    return n


def _flag_results(conn: sqlite3.Connection) -> int:
    """Flag fighters whose result in a fight differs between sources (a source with no result yet
    doesn't disagree). Returns the flags."""
    rows = conn.execute("""
        SELECT p.fight_id, p.fighter_id, group_concat(p.source || ': ' || p.result, '; ')
        FROM (SELECT DISTINCT fight_id, fighter_id, source, result FROM participant_claims
              WHERE fighter_id IS NOT NULL AND result IS NOT NULL ORDER BY fight_id, fighter_id, source, result) p
        GROUP BY p.fight_id, p.fighter_id
        HAVING count(DISTINCT p.result) > 1""").fetchall()
    conn.executemany(
        "INSERT INTO review_flags (check_name, table_name, row_key, column_name, value, reason, fighter_id, fight_id) "
        "VALUES ('results disagree', 'fight_participants', ?, 'result', ?, 'sources give this fighter different "
        "results', ?, ?)",
        [(json.dumps({"fight_id": fight, "fighter_id": fid}), said, fid, fight) for fight, fid, said in rows])
    return len(rows)


def _flag_dobs(conn: sqlite3.Connection) -> int:
    """Flag fighters whose sources give different birth dates (legacy values aside). Returns the flags."""
    rows = conn.execute("""
        SELECT fighter_id, group_concat(source || ': ' || dob, '; ')
        FROM (SELECT DISTINCT fighter_id, source, dob FROM fighter_claims
              WHERE dob IS NOT NULL AND source <> 'legacy' ORDER BY fighter_id, source, dob)
        GROUP BY fighter_id HAVING count(DISTINCT dob) > 1""").fetchall()
    conn.executemany(
        "INSERT INTO review_flags (check_name, table_name, row_key, column_name, value, reason, fighter_id) "
        "VALUES ('birth dates disagree', 'fighters', ?, 'dob', ?, 'sources give this fighter different birth dates', ?)",
        [(json.dumps({"fighter_id": fid}), said, fid) for fid, said in rows])
    return len(rows)


def sex_from_divisions(conn: sqlite3.Connection) -> None:
    """A fighter with no stated sex: 'F' if they fought in a women's bout UFC Stats lists, 'M' if in
    one of its named men's divisions (catch weight and open weight don't say)."""
    bouts = ("SELECT p.fighter_id FROM fight_participants p JOIN fights f USING (fight_id) "
             "JOIN fight_aliases a ON a.fight_id = f.fight_id AND a.source = 'ufc' AND a.site = 'ufcstats.com' "
             "WHERE f.weight_class LIKE ")
    conn.execute(f"UPDATE fighters SET sex = 'F' WHERE sex IS NULL AND fighter_id IN ({bouts} '%Women%')")
    conn.execute(f"UPDATE fighters SET sex = 'M' WHERE sex IS NULL AND fighter_id IN ({bouts} '%weight%' "
                 "AND f.weight_class NOT LIKE '%Catch%' AND f.weight_class NOT LIKE '%Open%')")


def records(conn: sqlite3.Connection) -> None:
    """Every fighter's record, counted from their fights here."""
    conn.execute("""
        UPDATE fighters SET (wins, losses, draws, no_contests) = (
            SELECT coalesce(sum(result = 'W'), 0), coalesce(sum(result = 'L'), 0), coalesce(sum(result = 'D'), 0),
                   coalesce(sum(result = 'NC'), 0)
            FROM fight_participants p WHERE p.fighter_id = fighters.fighter_id)""")


RULES = (sex_from_divisions, records)  # after the claims are picked, in order


def current(conn: sqlite3.Connection) -> None:
    """Make the main tables current with the claims, if any changed: for code that reads them."""
    if dirty:
        resolve(conn)


def resolve(conn: sqlite3.Connection) -> Counter:
    """Work out fighters, events, fights and fight_participants from the claims, run RULES, and
    rebuild the review flags of CHECKS. Returns counts."""
    global dirty
    counts = Counter()
    with conn:
        _ranks(conn)
        for kind in ("event", "fight", "fighter"):
            counts[f"{kind}s from claims"] = _pick(conn, kind)
        counts["fights' sides from claims"] = _participants(conn)
        for rule in RULES:
            rule(conn)
        conn.execute(f"DELETE FROM review_flags WHERE check_name IN ({', '.join('?' * len(CHECKS))})", CHECKS)
        counts["results disagree"] = _flag_results(conn)
        counts["birth dates disagree"] = _flag_dobs(conn)
        conn.execute("DROP TABLE temp.ranks")
    dirty = False
    return counts
