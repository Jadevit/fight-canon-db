"""Tests for the site's build: scripts/site_json.py (the pages as JSON) and
scripts/export_parquet.py (the database as Parquet)."""

import importlib.util
import json
import sqlite3

import pytest

from canon_db import db

pytest.importorskip("duckdb")


def _script(name):
    spec = importlib.util.spec_from_file_location(name, db.ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


site_json = _script("site_json")
export_parquet = _script("export_parquet")


def _canon(path):
    """José Aldo (1), Ben Baker (2), Carl Cole (3); UFC 1 (event 1: fights 1 then 2), UFC 2 (event 2: fight 3)."""
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA.read_text())
    db.sync_promotions(conn)
    conn.executemany("INSERT INTO fighters (fighter_id, name, wins, losses, draws, no_contests) VALUES (?,?,?,?,?,?)",
                     [(1, "José Aldo", 31, 8, 0, 0), (2, "Ben Baker", None, None, None, None),
                      (3, "Carl Cole", None, None, None, None)])
    conn.executemany("INSERT INTO fighter_aliases VALUES (1, ?, ?, ?)",
                     [("sherdog", "1", "Junior"), ("ufcstats", "a" * 16, "José Aldo")])
    conn.executemany("INSERT INTO events (event_id, name, date, location, promotion) VALUES (?,?,?,?,?)",
                     [(1, "UFC 1", "2020-01-02", "London, England", "UFC"),
                      (2, "UFC 2", "2021-05-05", "Las Vegas", "UFC")])
    conn.execute("INSERT INTO event_aliases VALUES (1, 'ufcstats', 'e1')")
    conn.executemany("INSERT INTO fights (fight_id, event_id, weight_class, method) VALUES (?,?,?,?)",
                     [(1, 1, "Lightweight Bout", "Decision"), (2, 1, "UFC Featherweight Title Bout", "KO/TKO"),
                      (3, 2, None, "Submission")])
    conn.execute("INSERT INTO fight_aliases VALUES (2, 'ufcstats', 'f1')")
    conn.executemany("INSERT INTO fight_participants VALUES (?,?,?,?)",
                     [(1, 2, 0, "W"), (1, 3, 1, "L"), (2, 1, 0, "W"), (2, 2, 1, "L"), (3, 1, 0, "W"), (3, 3, 1, "L")])
    conn.execute("INSERT INTO round_stats (fight_id, fighter_id, round, sig_str_land, sig_str_att, source) "
                 "VALUES (2, 1, 1, 30, 60, 'ufcstats')")
    conn.executemany("INSERT INTO round_stats (fight_id, fighter_id, round, sig_str_land, source) VALUES (3, 1, 1, ?, ?)",
                     [(6, "sofascore"), (5, "oktagon")])  # two counts of one fight
    conn.commit()
    return conn


def test_site_tables(tmp_path):
    canon = _canon(tmp_path / "canon.db")
    con = site_json.load(tmp_path / "canon.db")
    # A fighter's fights from either side; an event's fights in card order.
    assert con.execute("SELECT fight_id FROM site_fights WHERE ? IN (a_id, b_id) ORDER BY date",
                       [1]).fetchall() == [(2,), (3,)]
    assert con.execute("SELECT fight_id, division, has_stats FROM site_fights WHERE event_id = 1 "
                       "ORDER BY card").fetchall() == [(1, "Lightweight", 0), (2, "Featherweight", 1)]
    assert con.execute("SELECT events, promotions FROM site_stats").fetchone() == (
        canon.execute("SELECT COUNT(*), COUNT(DISTINCT promotion) FROM events").fetchone())
    assert con.execute("SELECT division('UFC Women''s Flyweight Title Bout'), division('Catchweight Bout'), "
                       "division(NULL)").fetchone() == ("Women's Flyweight", "Catch Weight", "Other")


def test_site_json(tmp_path, monkeypatch):
    _canon(tmp_path / "canon.db")
    monkeypatch.setattr(site_json, "SEARCH_CAP", 1)  # split every prefix with two matches
    monkeypatch.setattr(site_json, "SEARCH_TOP", 1)
    monkeypatch.setattr(site_json, "SEARCH_EXACT", 1)
    monkeypatch.setattr(site_json, "RANDOM_MIN", 2)  # everyone has 2 fights
    monkeypatch.setattr(site_json, "RANDOM_FILES", 2)
    site_json.build(tmp_path / "canon.db", tmp_path / "json")
    root = tmp_path / "json" / "v1"
    read = lambda p: json.loads((root / p).read_text(encoding="utf-8"))  # noqa: E731
    rows = lambda t: [dict(zip(t["cols"], r)) for r in t["rows"]]  # noqa: E731

    aldo = read("f/1.json")
    assert aldo["fighter"]["wins"] == 31 and aldo["aliases"] == {"sherdog": ["1"], "ufcstats": ["a" * 16]}
    assert [(f["fight_id"], f["result"], f["opp"]) for f in rows(aldo["fights"])] == [
        (3, "W", "Carl Cole"), (2, "W", "Ben Baker")]  # newest first
    assert [(st["fight_id"], st["source"], st["sl"], st["sa"]) for st in rows(aldo["stats"])] == [
        (2, "ufcstats", 30, 60), (3, "oktagon", 5, None), (3, "sofascore", 6, None)]  # per source

    e1 = read("e/1.json")
    assert [f["fight_id"] for f in rows(e1["fights"])] == [1, 2]  # card order
    assert [st["source"] for st in e1["stats"]["2"]] == ["ufcstats"] and "1" not in e1["stats"]
    assert e1["aliases"] == {"ufcstats": ["e1"]} and e1["ufcstats"] == {"2": "f1"}  # links to UFC Stats' pages
    assert read(f"fx/{site_json.shard(2)}.json")["2"] == 1
    # Each count of a fight, the promotion's own first.
    assert [(st["source"], st["rows"][0][st["cols"].index("sig_str_land")]) for st in read("e/2.json")["stats"]["3"]] == [
        ("oktagon", 5), ("sofascore", 6)]
    assert [e["event_id"] for e in rows(read("events/y/2020.json"))] == [1]

    # Search: "jo" matches José (and Junior, his alias) only, so it isn't split; Carl Cole and
    # Ben Baker share "b"/"c" prefixes with no one. Events: "lo" is London alone.
    jo = read(f"sf/{site_json.hexname('jo')}.json")
    assert not jo["split"] and [r["fighter_id"] for r in rows(jo)] == [1]
    assert "junior" in rows(jo)[0]["words"].split()
    assert [r["event_id"] for r in rows(read(f"se/{site_json.hexname('lo')}.json"))] == [1]
    # "uf" (UFC 1, UFC 2) is split: its file keeps the top one and those whose word is "uf"
    # itself (none), and "ufc" is a file of its own.
    uf = read(f"se/{site_json.hexname('uf')}.json")
    assert uf["split"] and [r["event_id"] for r in rows(uf)] == [2]
    # "ufc" is both events' word itself: split again, it keeps only the newest (SEARCH_EXACT).
    ufc = read(f"se/{site_json.hexname('ufc')}.json")
    assert ufc["split"] and [r["event_id"] for r in rows(ufc)] == [2]

    assert read("l/results/wins.json")["0"][0][:3] == [1, "José Aldo", 2]
    # The Query page's help: the rules for an assistant, and the tables with schema.sql's comments.
    help_ = read("query.json")
    assert "DuckDB" in help_["rules"] and "Lightweight" in help_["rules"]
    tables = {t["name"]: t for t in help_["tables"]}
    assert "bouts" in tables and "event_pages" not in tables
    assert dict((c[0], c[2]) for c in tables["fights"]["columns"])["end_time"] == "m:ss"
    m = read("meta.json")
    assert m["stats"]["events"] == 2 and m["boards"]["results"]["stats"]["wins"]["label"] == "Wins"
    assert m["sums"]["round_stats"]["sl"] == "sig_str_land" and "kd" in m["stats_cols"]
    assert m["boards"]["ufcstats"]["source"] == ["round_stats", "ufcstats"] and m["boards"]["results"]["source"] is None
    # The Fighters page: the last week's pairs (only UFC 2's fight), and random-pick files by id.
    page = read("fighters.json")
    assert [[s[page["cols"].index("fighter_id")] for s in m] for m in page["matchups"]] == [[1, 3]]
    assert page["random"] == 2 and read("fr/0.json") == [2] and read("fr/1.json") == [1, 3]
    assert "top_fighters" not in m
    assert {"uf", "ufc"} <= set(m["splits"]["se"]) and "jo" not in m["splits"]["sf"]  # the split search files
    # Leaderboards' opening views: every division (0) and each one.
    assert [r[:4] for r in read("l/results/wins.json")["0"]] == [[1, "José Aldo", 2, 2], [2, "Ben Baker", 2, 1],
                                                               [3, "Carl Cole", 2, 0]]
    sig = read("l/ufcstats/sig.json")
    assert sig["0"] == [[1, "José Aldo", 1, 30]] and list(sig) == ["0", "9"]  # all, and Featherweight


def test_export_parquet(tmp_path):
    import duckdb
    canon = _canon(tmp_path / "canon.db")
    config = export_parquet.export(tmp_path / "canon.db", tmp_path / "pq")
    con = duckdb.connect()
    assert "fights" in config["tables"] and len(config["build"]) == 12
    # Every table comes through whole; events in date order.
    for t in config["tables"]:
        n = con.execute(f"SELECT COUNT(*) FROM '{(tmp_path / 'pq' / t).as_posix()}.parquet'").fetchone()[0]
        assert n == canon.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0], t
    assert con.execute(f"SELECT list(event_id) FROM '{(tmp_path / 'pq').as_posix()}/events.parquet'").fetchone()[0] == [
        1, 2]
    # The same input gives the same files (the build name doesn't change without new data).
    assert export_parquet.export(tmp_path / "canon.db", tmp_path / "pq2")["build"] == config["build"]
