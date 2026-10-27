"""Fight Canon ids: rows found by a source's alias, new rows, values by source rank, merges.

Fighters, events and fights have our ids; a source only adds aliases (its id for our row)
and data. A source with full results that finds no row of ours for something makes one.

When two sources give a column, the promotions' own sites (OFFICIAL) are taken over the rest
(Sherdog), which only fill what they leave blank: write() does that per column. When two rows
turn out to be one, merge() keeps the older id, records the other in merged_ids, and moves
everything that pointed at it.
"""

from __future__ import annotations

import sqlite3

OFFICIAL = frozenset({"ufcstats", "pfl", "oktagon"})  # promotions' own sites

# kind -> (table, id column, aliases table)
KINDS = {"fighter": ("fighters", "fighter_id", "fighter_aliases"),
         "event": ("events", "event_id", "event_aliases"),
         "fight": ("fights", "fight_id", "fight_aliases")}

# Tables with a fight's rows from one source each, best first (a fight keeps one source's).
STATS_RANK = ("ufcstats", "pfl", "sofascore", "oktagon")
FIGHT_ROWS = ("round_stats", "smartcage_round_stats", "sofascore_round_stats", "judge_scores", "odds")
FIGHTER_ROWS = ("fight_participants", *FIGHT_ROWS)


def find(conn: sqlite3.Connection, kind: str, source: str, source_id: str) -> int | None:
    """Our id for a source's id, or None."""
    _, col, aliases = KINDS[kind]
    row = conn.execute(f"SELECT {col} FROM {aliases} WHERE source = ? AND source_id = ?",
                       (source, str(source_id))).fetchone()
    return row[0] if row else None


def aliases(conn: sqlite3.Connection, kind: str, source: str) -> dict[str, int]:
    """Every id this source has for this kind: {source id: our id}."""
    _, col, table = KINDS[kind]
    return {s: i for s, i in conn.execute(f"SELECT source_id, {col} FROM {table} WHERE source = ?", (source,))}


def add(conn: sqlite3.Connection, kind: str, values: dict) -> int:
    """A new row; returns its id. A new event's promotion label gets a (partial) row if it has none."""
    table, _, _ = KINDS[kind]
    if kind == "event" and values.get("promotion"):
        conn.execute("INSERT OR IGNORE INTO promotions (promotion, coverage) VALUES (?, 'partial')",
                     (values["promotion"],))
    return conn.execute(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                        list(values.values())).lastrowid


def alias(conn: sqlite3.Connection, kind: str, id_: int, source: str, source_id: str, name: str | None = None) -> None:
    """Record a source's id for our row (fighters: with a spelling of their name)."""
    _, col, table = KINDS[kind]
    if kind == "fighter":
        conn.execute(f"INSERT OR IGNORE INTO {table} ({col}, source, source_id, name) VALUES (?,?,?,?)",
                     (id_, source, str(source_id), name))
    else:
        conn.execute(f"INSERT OR REPLACE INTO {table} ({col}, source, source_id) VALUES (?,?,?)",
                     (id_, source, str(source_id)))


def sources(conn: sqlite3.Connection, kind: str, id_: int) -> set[str]:
    _, col, table = KINDS[kind]
    return {r[0] for r in conn.execute(f"SELECT DISTINCT source FROM {table} WHERE {col} = ?", (id_,))}


def official(conn: sqlite3.Connection, kind: str) -> set[int]:
    """The rows of this kind some promotion's own site has."""
    _, col, table = KINDS[kind]
    return {r[0] for r in conn.execute(
        f"SELECT DISTINCT {col} FROM {table} WHERE source IN ({', '.join('?' * len(OFFICIAL))})", sorted(OFFICIAL))}


def write(conn: sqlite3.Connection, kind: str, id_: int, source: str, values: dict, ranked: bool | None = None) -> None:
    """Set columns of our row from `source`. An official source's value replaces what's there
    (a blank doesn't); any other source's only fills a blank if a promotion's site has the
    row (`ranked`: whether one has it, if the caller knows), and otherwise replaces it."""
    if not values:
        return
    table, col, _ = KINDS[kind]
    if ranked is None:
        ranked = bool(sources(conn, kind, id_) & OFFICIAL)
    fill = source not in OFFICIAL and ranked
    sets = ", ".join(f"{c} = coalesce({c}, ?)" if fill else f"{c} = coalesce(?, {c})" for c in values)
    conn.execute(f"UPDATE {table} SET {sets} WHERE {col} = ?", (*values.values(), id_))


def best_stats(conn: sqlite3.Connection, fight: int) -> int:
    """Rank of the best source of a fight's round stats (lower is better; len(STATS_RANK) if none)."""
    have = {r[0] for r in conn.execute("SELECT DISTINCT source FROM round_stats WHERE fight_id = ?", (fight,))}
    have |= {"pfl"} if conn.execute("SELECT 1 FROM smartcage_round_stats WHERE fight_id = ?", (fight,)).fetchone() else set()
    return min((STATS_RANK.index(s) for s in have if s in STATS_RANK), default=len(STATS_RANK))


def merge(conn: sqlite3.Connection, kind: str, a: int, b: int) -> int:
    """Make rows `a` and `b` one. The older (lower) id is kept; per column, the value of the
    row a promotion's site has wins, else the kept row's. Returns the kept id."""
    if a == b:
        return a
    keep, drop = min(a, b), max(a, b)
    table, col, aliases_table = KINDS[kind]
    first, second = (drop, keep) if sources(conn, kind, drop) & OFFICIAL and not sources(conn, kind, keep) & OFFICIAL \
        else (keep, drop)
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[1] != col]
    conn.execute(f"UPDATE {table} SET ({', '.join(cols)}) = (SELECT {', '.join(f'coalesce(x.{c}, y.{c})' for c in cols)} "
                 f"FROM {table} x, {table} y WHERE x.{col} = ? AND y.{col} = ?) WHERE {col} = ?", (first, second, keep))
    if kind == "fighter":
        for t in FIGHTER_ROWS:
            conn.execute(f"UPDATE OR IGNORE {t} SET fighter_id = ? WHERE fighter_id = ?", (keep, drop))
            conn.execute(f"DELETE FROM {t} WHERE fighter_id = ?", (drop,))
    elif kind == "event":
        conn.execute("UPDATE fights SET event_id = ? WHERE event_id = ?", (keep, drop))
        conn.execute("UPDATE OR IGNORE event_cards SET event_id = ? WHERE event_id = ?", (keep, drop))
        conn.execute("DELETE FROM event_cards WHERE event_id = ?", (drop,))
    else:
        # The fight's corners and result come from the row a promotion's site has.
        if first == drop:
            conn.execute("DELETE FROM fight_participants WHERE fight_id = ?", (keep,))
            conn.execute("UPDATE fight_participants SET fight_id = ? WHERE fight_id = ?", (keep, drop))
        conn.execute("DELETE FROM fight_participants WHERE fight_id = ?", (drop,))
        # Round stats: only the better source's (a fight's come from one source).
        loser = drop if best_stats(conn, keep) <= best_stats(conn, drop) else keep
        if best_stats(conn, keep) < len(STATS_RANK) and best_stats(conn, drop) < len(STATS_RANK):
            for t in ("round_stats", "smartcage_round_stats", "sofascore_round_stats"):
                conn.execute(f"DELETE FROM {t} WHERE fight_id = ?", (loser,))
        for t in FIGHT_ROWS:
            conn.execute(f"UPDATE OR IGNORE {t} SET fight_id = ? WHERE fight_id = ?", (keep, drop))
            conn.execute(f"DELETE FROM {t} WHERE fight_id = ?", (drop,))
    conn.execute(f"UPDATE OR IGNORE {aliases_table} SET {col} = ? WHERE {col} = ?", (keep, drop))
    conn.execute(f"DELETE FROM {aliases_table} WHERE {col} = ?", (drop,))
    conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (drop,))
    conn.execute("UPDATE merged_ids SET new_id = ? WHERE kind = ? AND new_id = ?", (keep, kind, drop))
    conn.execute("INSERT OR REPLACE INTO merged_ids (kind, old_id, new_id) VALUES (?,?,?)", (kind, drop, keep))
    return keep


def delete_fight(conn: sqlite3.Connection, fight: int) -> None:
    """Delete a fight and everything attached to it."""
    for t in (*FIGHT_ROWS, "fight_participants", "fight_aliases", "fights"):
        conn.execute(f"DELETE FROM {t} WHERE fight_id = ?", (fight,))


def drop_claim(conn: sqlite3.Connection, fight: int, source: str) -> bool:
    """A source no longer lists this fight: remove its aliases (and its stats and odds), and
    the fight if no source is left. Returns True if the fight was deleted."""
    conn.execute("DELETE FROM fight_aliases WHERE fight_id = ? AND source = ?", (fight, source))
    for t in FIGHT_ROWS:
        conn.execute(f"DELETE FROM {t} WHERE fight_id = ? AND source = ?", (fight, source))
    if not conn.execute("SELECT 1 FROM fight_aliases WHERE fight_id = ?", (fight,)).fetchone():
        delete_fight(conn, fight)
        return True
    return False


def merge_duplicates(conn: sqlite3.Connection) -> int:
    """Merge fights between the same two fighters within a day of each other, unless both have
    an id from one source (a real rematch, or that source's own duplicate). Returns the merges."""
    conn.execute("DROP TABLE IF EXISTS temp.pairs")
    conn.execute("""CREATE TEMP TABLE pairs AS
        SELECT p.fight_id, e.date, min(p.fighter_id) AS a, max(p.fighter_id) AS b
        FROM fight_participants p JOIN fights f USING (fight_id) JOIN events e USING (event_id)
        WHERE e.date IS NOT NULL GROUP BY p.fight_id HAVING count(*) = 2""")
    conn.execute("CREATE INDEX temp.pairs_ab ON pairs(a, b)")
    found = conn.execute("""
        SELECT x.fight_id, y.fight_id FROM pairs x JOIN pairs y ON y.a = x.a AND y.b = x.b AND y.fight_id > x.fight_id
        WHERE abs(julianday(x.date) - julianday(y.date)) <= 1
          AND NOT EXISTS (SELECT 1 FROM fight_aliases s JOIN fight_aliases t ON t.source = s.source
                          WHERE s.fight_id = x.fight_id AND t.fight_id = y.fight_id)""").fetchall()
    conn.execute("DROP TABLE temp.pairs")
    gone, n = {}, 0
    for x, y in found:
        x, y = gone.get(x, x), gone.get(y, y)
        if x != y:
            keep = merge(conn, "fight", x, y)
            gone[max(x, y)] = keep
            n += 1
    return n


def drop_empty(conn: sqlite3.Connection) -> None:
    """Delete events with no fights and fighters with none, with their aliases and cards, unless
    a promotion's site has them (it lists fighters whose fights are all elsewhere)."""
    on_site = f"SELECT {{}} FROM {{}} WHERE source IN ({', '.join(repr(s) for s in sorted(OFFICIAL))})"
    empty = (f"SELECT event_id FROM events WHERE event_id NOT IN (SELECT event_id FROM fights) "
             f"AND event_id NOT IN ({on_site.format('event_id', 'event_aliases')})")
    for t in ("event_cards", "event_aliases", "events"):
        conn.execute(f"DELETE FROM {t} WHERE event_id IN ({empty})")
    idle = (f"SELECT fighter_id FROM fighters WHERE fighter_id NOT IN (SELECT fighter_id FROM fight_participants) "
            f"AND fighter_id NOT IN ({on_site.format('fighter_id', 'fighter_aliases')})")
    for t in ("fighter_aliases", "fighters"):
        conn.execute(f"DELETE FROM {t} WHERE fighter_id IN ({idle})")
