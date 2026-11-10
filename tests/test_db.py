"""Tests for the source-agnostic database plumbing and the published schema."""

import sqlite3

import pytest

from canon_db import db


def _schema(conn) -> list[str]:
    return sorted(r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))


def test_published_db_matches_schema_sql():
    """schema.sql is the contract other repos read against; the published DB must match it."""
    if not db.DEFAULT_DB.exists():
        pytest.skip("no data/canon.db: gh release download --pattern canon.db --dir data")
    fresh = sqlite3.connect(":memory:")
    fresh.executescript((db.ROOT / "canon_db" / "schema.sql").read_text())
    published = sqlite3.connect(db.DEFAULT_DB)
    assert _schema(published) == _schema(fresh)


def test_changed_ignores_row_order(tmp_path):
    a, b = tmp_path / "a.db", sqlite3.connect(":memory:")
    for conn, rows in ((sqlite3.connect(a), [(1, None), (2, "x")]), (b, [(2, "x"), (1, None)])):
        conn.execute("CREATE TABLE t (x, y)")
        conn.executemany("INSERT INTO t VALUES (?, ?)", rows)
        conn.commit()
    assert not db.changed(b, a)
    with b:
        b.execute("UPDATE t SET y = 'z' WHERE x = 1")  # a NULL filled
    assert db.changed(b, a)
    with b:
        b.execute("UPDATE t SET y = NULL WHERE x = 1")
        b.execute("CREATE TABLE u (x)")  # a new table, even an empty one
    assert db.changed(b, a)


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


def test_updating_refuses_a_big_fall(tmp_path):
    path = tmp_path / "x.db"
    conn = sqlite3.connect(path)
    db.create(conn)
    db.sync_promotions(conn)
    conn.execute("INSERT INTO events (event_id, name, date, promotion) VALUES (1, 'E 1', '2020-01-01', 'UFC')")
    conn.executemany("INSERT INTO fighters (fighter_id, name) VALUES (?, ?)", [(i, f"F {i}") for i in range(1, 1001)])
    conn.executemany("INSERT INTO fighter_aliases (fighter_id, source, site, source_id, name) VALUES (?, 'bestfightodds', 'bestfightodds.com', ?, ?)", [(i, str(i), f"F {i}") for i in range(1, 301)])
    conn.commit()
    conn.close()
    before = path.read_bytes()
    with db.updating(path) as (conn, _):  # a few gone: fine
        with conn:
            conn.execute("DELETE FROM fighters WHERE fighter_id > 990")
    try:
        with db.updating(path) as (conn, _):  # one source's rows gone: a parser that broke
            with conn:
                conn.execute("DELETE FROM fighter_aliases WHERE fighter_id > 150")
        raise AssertionError("published")
    except RuntimeError as e:
        assert "fighter_aliases bestfightodds: 300 -> 150" in str(e)
    assert path.read_bytes() != before and not (tmp_path / "x.db.tmp").exists()
    with db.updating(path, allow_drop=True) as (conn, result):
        with conn:
            conn.execute("DELETE FROM fighter_aliases WHERE fighter_id > 150")
    assert result.changed


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


def test_migrate_adds_columns_and_keeps_rows(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    schema = (db.ROOT / "canon_db" / "schema.sql").read_text()
    old.executescript(schema.replace("    reach_in     INTEGER,\n", "", 1))  # an older schema
    old.executemany("INSERT INTO fighters (fighter_id, name) VALUES (?, ?)", [(1, "A"), (2, "B")])
    old.execute("DELETE FROM fighters WHERE fighter_id = 2")  # the newest id, gone: never given again
    old.commit()
    old.close()
    assert db.migrate(path)["fighters"] == 1
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT name, reach_in FROM fighters").fetchone() == ("A", None)
    assert conn.execute("INSERT INTO fighters (name) VALUES ('C')").lastrowid == 3


def test_migrate_renames_sources_gives_old_aliases_their_site_and_keeps_merges(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE fighters (fighter_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL);
        CREATE TABLE fighter_aliases (fighter_id INTEGER, source TEXT, source_id TEXT, name TEXT);
        CREATE TABLE merged_ids (kind TEXT, old_id INTEGER, new_id INTEGER);
        INSERT INTO fighters VALUES (1, 'A');
        INSERT INTO fighter_aliases VALUES (1, 'sherdog', '7', 'A'), (1, 'ufcstats', 'x', 'A');
        INSERT INTO merged_ids VALUES ('fighter', 2, 1);""")
    old.commit()
    old.close()
    db.migrate(path)
    conn = sqlite3.connect(path)
    assert sorted(conn.execute("SELECT source, site FROM fighter_aliases")) == [  # renamed sources too
        ("sherdog", "sherdog.com"), ("ufc", "ufcstats.com")]
    assert conn.execute("SELECT kind, old_id, new_id, reason FROM retired_ids").fetchall() == [("fighter", 2, 1, "merged")]
    assert conn.execute("SELECT count(*) FROM sources").fetchone()[0] == len(db.SITES)


KINDS = {"fighter", "event", "fight", "result", "round_stats", "judge_scores", "media_scores", "odds", "rankings"}


def test_source_files_agree():
    """Every tier is a known source's and a known kind; every source but the two by hand has a site."""
    sources = {r["source"]: r for r in db._csv(db.SOURCES)}
    assert {r["source"] for r in db.TIER_ROWS} <= set(sources)
    assert {r["kind"] for r in db.TIER_ROWS} <= KINDS
    assert {s for s, r in sources.items() if not r["sites"]} == {"correction", "legacy"}
    assert all(r["full_results"] in ("0", "1") for r in sources.values())


def test_published_aliases_use_their_sources_sites():
    if not db.DEFAULT_DB.exists():
        pytest.skip("no data/canon.db")
    conn = sqlite3.connect(db.DEFAULT_DB)
    for t in ("fighter_aliases", "event_aliases", "fight_aliases", "promotion_aliases"):
        for source, site in conn.execute(f"SELECT DISTINCT source, site FROM {t}"):
            assert site in db.SITES[source], (t, source, site)
