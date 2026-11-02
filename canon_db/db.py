"""Source-agnostic database plumbing: the working copy, change detection, summary.

Every update runs against a temporary copy of data/canon.db. The real file is
replaced only if the content changed, so an idle run leaves it byte-identical and
git sees nothing. Rewriting identical rows can still change the file's bytes, which
is why the comparison is on content, not bytes.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "canon.db"
RAW = ROOT / "data" / "raw"
SCHEMA = Path(__file__).resolve().parent / "schema.sql"
PROMOTIONS = Path(__file__).resolve().parent / "promotions.csv"


def _columns() -> dict[str, list[str]]:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA.read_text())
    tables = [t for (t,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid")]
    out = {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")] for t in tables}
    conn.close()
    return out


COLUMNS = _columns()  # schema.sql's tables in its order, with their columns: the one list of them
TABLES = tuple(COLUMNS)


def changed(conn: sqlite3.Connection, original: Path) -> bool:
    """Whether `conn`'s content differs from the file `original`: its schema, or any table's
    rows (compared both ways inside SQLite, so file layout and row order don't matter:
    re-inserted rows land in a new order). Stops at the first table that differs."""
    conn.execute("ATTACH ? AS original", (str(original),))
    try:
        schema = "SELECT type, name, sql FROM {}.sqlite_master ORDER BY 1, 2"
        if conn.execute(schema.format("main")).fetchall() != conn.execute(schema.format("original")).fetchall():
            return True
        tables = [t for (t,) in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")]
        return any(conn.execute(f'SELECT EXISTS (SELECT * FROM main."{t}" EXCEPT SELECT * FROM original."{t}") '
                                f'OR EXISTS (SELECT * FROM original."{t}" EXCEPT SELECT * FROM main."{t}")').fetchone()[0]
                   for t in tables)
    finally:
        conn.execute("DETACH original")


def summary(conn: sqlite3.Connection) -> dict:
    """Coverage + row counts, published as data/summary.json next to the database."""
    eid, name, day = conn.execute(
        "SELECT event_id, name, date FROM events ORDER BY date DESC, event_id LIMIT 1").fetchone()
    first = conn.execute("SELECT MIN(date) FROM events").fetchone()[0]
    return dict(updated_at=datetime.now(UTC).isoformat(timespec="seconds"),
                coverage=dict(first_event_date=first, last_event_date=day,
                              last_event=name, last_event_id=eid),
                rows={t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in TABLES})


def sync_promotions(conn: sqlite3.Connection) -> None:
    """Make `promotions` match promotions.csv, plus a partial row for any events.promotion
    label the file doesn't have yet (logged, so it can be added)."""
    with open(PROMOTIONS, newline="", encoding="utf-8") as fh:
        rows = {r["promotion"]: (r["promotion"], r["parent"] or None, r["coverage"])
                for r in csv.DictReader(fh)}
    missing = [label for (label,) in conn.execute(
        "SELECT promotion FROM events UNION SELECT promotion FROM promotion_aliases")
        if label not in rows]
    if missing:
        logging.getLogger("canon_db").info(
            "%d promotions aren't in promotions.csv; added as partial (e.g. %s).",
            len(missing), ", ".join(sorted(missing)[:5]))
    rows.update({label: (label, None, "partial") for label in missing})
    with conn:
        conn.execute("DELETE FROM promotions")
        conn.execute("PRAGMA defer_foreign_keys = ON")  # parents may come later in the file
        conn.executemany("INSERT INTO promotions VALUES (?,?,?)", rows.values())


DROP_SHARE, DROP_ROWS = 0.02, 100  # a fall bigger than both stops a publish (see drops())
BY_SOURCE = tuple(t for t, cols in COLUMNS.items() if "source" in cols)  # counted per source too


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Rows per table, and per source in the tables that say whose a row is."""
    have = {n for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    out = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES if t in have}
    for t in (t for t in BY_SOURCE if t in have):
        out.update({f"{t} {s}": n for s, n in conn.execute(f"SELECT source, COUNT(*) FROM {t} GROUP BY 1")})
    return out


def drops(before: dict[str, int], after: dict[str, int]) -> list[str]:
    """What fell by more than DROP_SHARE and DROP_ROWS: a source whose pages changed and now
    parse to nothing, or a bug deleting rows, rather than the day's few merges and deletions."""
    return [f"{k}: {n:,} -> {after.get(k, 0):,}" for k, n in before.items()
            if n - after.get(k, 0) > max(DROP_ROWS, DROP_SHARE * n)]


class Update:
    """Result of an `updating()` block; `changed` is set when the block exits."""
    changed: bool = False


@contextmanager
def updating(db: Path, allow_drop: bool = False):
    """Yield (connection to a working copy, Update). On a clean exit, publish the copy
    over `db` (plus summary.json) only if its content changed; otherwise discard it.
    On an exception, discard it and leave `db` untouched. Rows falling by more than drops()
    allows raise RuntimeError instead of publishing, unless `allow_drop`."""
    if not db.exists():
        raise FileNotFoundError(f"No database at {db}. Get the latest one with: "
                                "gh release download --pattern canon.db --dir data")
    work = db.with_suffix(".db.tmp")
    shutil.copyfile(db, work)
    conn = sqlite3.connect(work)
    result = Update()
    try:
        rows = counts(conn)
        yield conn, result
        result.changed = changed(conn, db)  # `db` is untouched until the copy replaces it
        if result.changed and (fell := drops(rows, counts(conn))):
            if not allow_drop:
                raise RuntimeError("rows fell more than an update should (run with --allow-drop if "
                                   "that's intended): " + "; ".join(fell))
            logging.getLogger("canon_db").warning("Rows fell (allowed): %s", "; ".join(fell))
        if result.changed:
            conn.execute("VACUUM")
            s = summary(conn)
            conn.close()
            work.replace(db)
            (db.parent / "summary.json").write_text(json.dumps(s, indent=2) + "\n",
                                                    encoding="utf-8", newline="\n")
    finally:
        conn.close()
        work.unlink(missing_ok=True)


def github_output(**values) -> None:
    """Expose values to later GitHub Actions steps (no-op outside Actions)."""
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a", encoding="utf-8") as fh:
            for k, v in values.items():
                fh.write(f"{k}={str(v).lower() if isinstance(v, bool) else v}\n")


def migrate(db: Path) -> dict[str, int]:
    """Rebuild `db` from schema.sql, copying every row of every table.

    Columns are copied by name: new columns start empty, removed ones are dropped. The
    result replaces `db` only if every table kept its row count. Returns the row counts.
    """
    work = db.with_suffix(".db.migrating")
    work.unlink(missing_ok=True)
    conn = sqlite3.connect(work)
    conn.executescript(SCHEMA.read_text())
    conn.execute("PRAGMA foreign_keys = OFF")  # checked once, after promotions are synced
    conn.execute("ATTACH ? AS old", (str(db),))
    counts = {}
    with conn:
        for t in TABLES:
            new_cols = [r[1] for r in conn.execute(f"PRAGMA main.table_info({t})")]
            old_cols = {r[1] for r in conn.execute(f"PRAGMA old.table_info({t})")}
            cols = ", ".join(c for c in new_cols if c in old_cols)
            if cols:
                conn.execute(f"INSERT INTO main.{t} ({cols}) SELECT {cols} FROM old.{t}")
            n_new = conn.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]
            n_old = conn.execute(f"SELECT COUNT(*) FROM old.{t}").fetchone()[0] if old_cols else 0
            if n_new != n_old:
                raise RuntimeError(f"{t}: {n_old} rows before, {n_new} after")
            counts[t] = n_new
        # Ids are never reused: keep each table's counter, even past the rows left.
        if conn.execute("SELECT 1 FROM old.sqlite_master WHERE name = 'sqlite_sequence'").fetchone():
            for name, seq in conn.execute("SELECT name, seq FROM old.sqlite_sequence").fetchall():
                if not conn.execute("UPDATE main.sqlite_sequence SET seq = max(seq, ?) WHERE name = ?",
                                    (seq, name)).rowcount:
                    conn.execute("INSERT INTO main.sqlite_sequence (name, seq) VALUES (?, ?)", (name, seq))
    sync_promotions(conn)
    counts["promotions"] = conn.execute("SELECT COUNT(*) FROM promotions").fetchone()[0]
    problems = conn.execute("PRAGMA main.foreign_key_check").fetchall()
    conn.execute("DETACH old")
    conn.execute("VACUUM")
    conn.close()
    if problems:
        work.unlink()
        raise RuntimeError(f"foreign key problems after migrating: {problems[:5]}")
    work.replace(db)
    return counts
