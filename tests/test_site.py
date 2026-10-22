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
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA.read_text())
    db.sync_promotions(conn)
    conn.executemany("INSERT INTO fighters (fighter_id, name, wins, losses, draws, no_contests) VALUES (?,?,?,?,?,?)",
                     [("a" * 16, "José Aldo", 31, 8, 0, 0), ("b" * 16, "Ben Baker", None, None, None, None),
                      ("c" * 16, "Carl Cole", None, None, None, None)])
    conn.execute("INSERT INTO fighter_aliases VALUES (?, 'sherdog', '1', 'Junior')", ("a" * 16,))
    conn.executemany("INSERT INTO events (event_id, name, date, location, promotion) VALUES (?,?,?,?,?)",
                     [("e1", "UFC 1", "2020-01-02", "London, England", "UFC"),
                      ("e2", "UFC 2", "2021-05-05", "Las Vegas", "UFC")])
    conn.executemany("INSERT INTO fights (fight_id, event_id, weight_class, method) VALUES (?,?,?,?)",
                     [("f2", "e1", "Lightweight Bout", "Decision"), ("f1", "e1", "UFC Featherweight Title Bout", "KO/TKO"),
                      ("f3", "e2", None, "Submission")])
    conn.executemany("INSERT INTO fight_participants VALUES (?,?,?,?)",
                     [("f2", "b" * 16, 0, "W"), ("f2", "c" * 16, 1, "L"), ("f1", "a" * 16, 0, "W"),
                      ("f1", "b" * 16, 1, "L"), ("f3", "a" * 16, 0, "W"), ("f3", "c" * 16, 1, "L")])
    conn.execute("INSERT INTO round_stats (fight_id, fighter_id, round, sig_str_land, sig_str_att, source) "
                 "VALUES ('f1', ?, 1, 30, 60, 'ufcstats')", ("a" * 16,))
    conn.commit()
    return conn


def test_site_tables(tmp_path):
    canon = _canon(tmp_path / "canon.db")
    con = site_json.load(tmp_path / "canon.db")
    # A fighter's fights from either side; an event's fights in card order.
    assert con.execute("SELECT fight_id FROM site_fights WHERE ? IN (a_id, b_id) ORDER BY date",
                       ["a" * 16]).fetchall() == [("f1",), ("f3",)]
    assert con.execute("SELECT fight_id, division, has_stats FROM site_fights WHERE event_id = 'e1' "
                       "ORDER BY card").fetchall() == [("f2", "Lightweight", 0), ("f1", "Featherweight", 1)]
    assert con.execute("SELECT events, promotions FROM site_stats").fetchone() == (
        canon.execute("SELECT COUNT(*), COUNT(DISTINCT promotion) FROM events").fetchone())
    assert con.execute("SELECT division('UFC Women''s Flyweight Title Bout'), division('Catchweight Bout'), "
                       "division(NULL)").fetchone() == ("Women's Flyweight", "Catch Weight", "Other")


def test_site_json(tmp_path, monkeypatch):
    _canon(tmp_path / "canon.db")
    monkeypatch.setattr(site_json, "SEARCH_CAP", 1)  # split every prefix with two matches
    monkeypatch.setattr(site_json, "SEARCH_TOP", 1)
    monkeypatch.setattr(site_json, "SEARCH_EXACT", 1)
    site_json.build(tmp_path / "canon.db", tmp_path / "json")
    root = tmp_path / "json" / "v1"
    read = lambda p: json.loads((root / p).read_text(encoding="utf-8"))  # noqa: E731
    rows = lambda t: [dict(zip(t["cols"], r)) for r in t["rows"]]  # noqa: E731

    aldo = read(f"f/{'a' * 16}.json")
    assert aldo["fighter"]["wins"] == 31 and aldo["sherdog"] == ["1"]
    assert [(f["fight_id"], f["result"], f["opp"]) for f in rows(aldo["fights"])] == [
        ("f3", "W", "Carl Cole"), ("f1", "W", "Ben Baker")]  # newest first
    [st] = rows(aldo["stats"])
    assert (st["fight_id"], st["sl"], st["sa"], st["opp_sa"]) == ("f1", 30, 60, None)

    e1 = read("e/e1.json")
    assert [f["fight_id"] for f in rows(e1["fights"])] == ["f2", "f1"]  # card order
    assert e1["stats"]["f1"]["source"] == "ufcstats" and "f2" not in e1["stats"]
    assert read(f"fx/{site_json.shard('f1')}.json")["f1"] == "e1"
    assert [e["event_id"] for e in rows(read("events/y/2020.json"))] == ["e1"]

    # Search: "jo" matches José (and Junior, his alias) only, so it isn't split; Carl Cole and
    # Ben Baker share "b"/"c" prefixes with no one. Events: "lo" is London alone.
    jo = read(f"sf/{site_json.hexname('jo')}.json")
    assert not jo["split"] and [r["fighter_id"] for r in rows(jo)] == ["a" * 16]
    assert "junior" in rows(jo)[0]["words"].split()
    assert [r["event_id"] for r in rows(read(f"se/{site_json.hexname('lo')}.json"))] == ["e1"]
    # "uf" (UFC 1, UFC 2) is split: its file keeps the top one and those whose word is "uf"
    # itself (none), and "ufc" is a file of its own.
    uf = read(f"se/{site_json.hexname('uf')}.json")
    assert uf["split"] and [r["event_id"] for r in rows(uf)] == ["e2"]
    # "ufc" is both events' word itself: split again, it keeps only the newest (SEARCH_EXACT).
    ufc = read(f"se/{site_json.hexname('ufc')}.json")
    assert ufc["split"] and [r["event_id"] for r in rows(ufc)] == ["e2"]

    assert read("l/results/wins.json")["0"][0][:3] == ["a" * 16, "José Aldo", 2]
    m = read("meta.json")
    assert m["stats"]["events"] == 2 and m["boards"]["results"]["stats"]["wins"]["label"] == "Wins"
    assert m["sums"]["round_stats"]["sl"] == "sig_str_land" and "kd" in m["stats_cols"]
    assert m["boards"]["ufcstats"]["source"] == ["round_stats", "ufcstats"] and m["boards"]["results"]["source"] is None
    assert [r[0] for r in m["top_fighters"]["rows"]] == ["b" * 16, "c" * 16, "a" * 16]  # 2 fights each: by name
    # Leaderboards' opening views: every division (0) and each one.
    assert [r[:4] for r in read("l/results/wins.json")["0"]] == [["a" * 16, "José Aldo", 2, 2], ["b" * 16, "Ben Baker", 2, 1],
                                                               ["c" * 16, "Carl Cole", 2, 0]]
    sig = read("l/ufcstats/sig.json")
    assert sig["0"] == [["a" * 16, "José Aldo", 1, 30]] and list(sig) == ["0", "9"]  # all, and Featherweight


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
        "e1", "e2"]
    # The same input gives the same files (the build name doesn't change without new data).
    assert export_parquet.export(tmp_path / "canon.db", tmp_path / "pq2")["build"] == config["build"]
