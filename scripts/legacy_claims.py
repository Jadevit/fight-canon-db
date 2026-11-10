"""`legacy` claims: the values rows held before loaders wrote claims.

The move to claims: rows made before then hold values whose source nothing records. `make`
gives every fighter, event, fight and fight's sides a legacy claim holding its current values,
ranked below every source, so a value no converted source gives falls through to what the row
had instead of going blank. After the full re-read, `trim` keeps only what no page holds: a
legacy value goes where a ranked source's claim gives that column, and a fight's legacy sides
go where a ranked source gives both sides linked. What's left is listed for review by hand.

    python scripts/legacy_claims.py make data/canon.db    # replaces any legacy claims
    python scripts/legacy_claims.py trim data/canon.db    # then work out the tables (an update does)
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from canon_db import claims, db

SOURCE, SITE = "legacy", ""  # legacy has no site; its claim's source_id is our id


def make(conn: sqlite3.Connection) -> dict[str, int]:
    out = {}
    with conn:
        for table, col, main, _ in claims.TABLES.values():
            cols = [c for c in db.COLUMNS[table] if c in db.COLUMNS[main] and c != col]
            conn.execute(f"DELETE FROM {table} WHERE source = ?", (SOURCE,))
            out[table] = conn.execute(
                f"INSERT INTO {table} ({col}, source, site, source_id, {', '.join(cols)}) "
                f"SELECT {col}, ?, ?, {col}, {', '.join(cols)} FROM {main}", (SOURCE, SITE)).rowcount
        conn.execute("DELETE FROM participant_claims WHERE source = ?", (SOURCE,))
        out["participant_claims"] = conn.execute(
            "INSERT INTO participant_claims (fight_id, source, site, source_id, side, fighter_id, result, corner) "
            "SELECT fight_id, ?, ?, fight_id, corner, fighter_id, result, corner FROM fight_participants",
            (SOURCE, SITE)).rowcount
    return out


def trim(conn: sqlite3.Connection) -> dict[str, int]:
    """Drop the legacy values a ranked source's claim also gives; returns what's left per table."""
    claims.dirty = True
    out = {}
    with conn:
        for table, col, _, kind in claims.TABLES.values():
            ranked = (f"SELECT 1 FROM {table} k JOIN source_tiers t ON t.source = k.source AND t.kind = '{kind}' "
                      f"WHERE k.{col} = {table}.{col} AND k.source <> '{SOURCE}' AND t.tier IN "
                      f"({', '.join(repr(t) for t in claims.RANKED)})")
            cols = [c for c in db.COLUMNS[table] if c not in (*claims.KEY, col)]
            for c in cols:
                conn.execute(f"UPDATE {table} SET {c} = NULL WHERE source = ? AND {c} IS NOT NULL "
                             f"AND EXISTS ({ranked} AND k.{c} IS NOT NULL)", (SOURCE,))
            conn.execute(f"DELETE FROM {table} WHERE source = ? AND {' AND '.join(f'{c} IS NULL' for c in cols)}",
                         (SOURCE,))
            out[table] = conn.execute(f"SELECT count(*) FROM {table} WHERE source = ?", (SOURCE,)).fetchone()[0]
        conn.execute("""
            DELETE FROM participant_claims WHERE source = ? AND fight_id IN (
                SELECT p.fight_id FROM participant_claims p
                JOIN source_tiers t ON t.source = p.source AND t.kind = 'result'
                WHERE p.source <> ? AND t.tier <> 'third_party'
                GROUP BY p.fight_id, p.source, p.site, p.source_id HAVING count(DISTINCT p.fighter_id) = 2)""",
                     (SOURCE, SOURCE))
        out["participant_claims"] = conn.execute("SELECT count(*) FROM participant_claims WHERE source = ?",
                                                 (SOURCE,)).fetchone()[0]
    return out


if __name__ == "__main__":
    what, path = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else db.DEFAULT_DB
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    print({"make": make, "trim": trim}[what](conn))
