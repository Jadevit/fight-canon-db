"""What differs between two canon.db files, table by table: row counts, rows only in one, a
few of each, and the id sequences. Exits 1 if anything differs, so a reload that should change
nothing can be checked:

    cp data/canon.db before.db
    python -m canon_db update <source> --offline
    python scripts/diffdb.py before.db data/canon.db
"""

from __future__ import annotations

import sqlite3
import sys

SHOW = 3  # rows shown per side of a table that differs


def tables(conn: sqlite3.Connection, schema: str) -> set[str]:
    return {r[0] for r in conn.execute(f"SELECT name FROM {schema}.sqlite_master "
                                       "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}


def diff(a: str, b: str) -> bool:
    """Print what differs between `a` and `b`; True if anything does."""
    conn = sqlite3.connect(a)
    conn.execute("ATTACH ? AS b", (b,))
    ta, tb = tables(conn, "main"), tables(conn, "b")
    differs = False
    for t in sorted(ta ^ tb):
        print(f"{t}: only in {a if t in ta else b}")
        differs = True
    for t in sorted(ta & tb):
        na, nb = (conn.execute(f"SELECT count(*) FROM {s}.{t}").fetchone()[0] for s in ("main", "b"))
        gone, new = (f"SELECT * FROM main.{t} EXCEPT SELECT * FROM b.{t}",
                     f"SELECT * FROM b.{t} EXCEPT SELECT * FROM main.{t}")
        n_gone, n_new = (conn.execute(f"SELECT count(*) FROM ({q})").fetchone()[0] for q in (gone, new))
        if na == nb and not n_gone and not n_new:
            continue
        differs = True
        print(f"{t}: {na} -> {nb} rows; {n_gone} only before, {n_new} only after")
        for label, q in (("before", gone), ("after ", new)):
            for r in conn.execute(f"{q} LIMIT {SHOW}"):
                print(f"    {label} {r}")
    seq = "SELECT name, seq FROM {}.sqlite_sequence ORDER BY name"
    sa, sb = (conn.execute(seq.format(s)).fetchall() for s in ("main", "b"))
    if sa != sb:
        differs = True
        print(f"id sequences: {sa} -> {sb}")
    return differs


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    if diff(sys.argv[1], sys.argv[2]):
        sys.exit(1)
    print("No differences.")
