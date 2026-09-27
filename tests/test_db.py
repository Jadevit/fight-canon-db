"""Tests for the source-agnostic database plumbing and the published schema."""

import shutil
import sqlite3

from canon_db import db


def _schema(conn) -> list[str]:
    return sorted(r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))


def test_published_db_matches_schema_sql():
    """schema.sql is the contract other repos read against; the committed DB must match it."""
    fresh = sqlite3.connect(":memory:")
    fresh.executescript((db.ROOT / "canon_db" / "schema.sql").read_text())
    published = sqlite3.connect(db.DEFAULT_DB)
    assert _schema(published) == _schema(fresh)


def test_digest_ignores_row_order():
    a, b = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    for conn, rows in ((a, [(1,), (2,)]), (b, [(2,), (1,)])):
        conn.execute("CREATE TABLE t (x)")
        conn.executemany("INSERT INTO t VALUES (?)", rows)
    assert db.digest(a) == db.digest(b)
    b.execute("INSERT INTO t VALUES (3)")
    assert db.digest(a) != db.digest(b)


def _tiny_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()


def test_updating_leaves_file_untouched_when_content_unchanged(tmp_path):
    path = tmp_path / "x.db"
    _tiny_db(path)
    before = path.read_bytes()
    with db.updating(path) as (conn, result):
        with conn:  # rewrite the same row: same content, possibly new bytes
            conn.execute("DELETE FROM t")
            conn.execute("INSERT INTO t VALUES (1)")
    assert not result.changed
    assert path.read_bytes() == before
    assert not (tmp_path / "x.db.tmp").exists()


def test_updating_discards_work_on_error(tmp_path):
    path = tmp_path / "x.db"
    _tiny_db(path)
    before = path.read_bytes()
    try:
        with db.updating(path) as (conn, _):
            with conn:
                conn.execute("INSERT INTO t VALUES (2)")
            raise RuntimeError("source failed")
    except RuntimeError:
        pass
    assert path.read_bytes() == before
    assert not (tmp_path / "x.db.tmp").exists()
