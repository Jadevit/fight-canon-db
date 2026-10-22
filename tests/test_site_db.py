"""Tests for scripts/site_db.py, the website's copy of the database."""

import importlib.util
import json
import sqlite3

from canon_db import db

spec = importlib.util.spec_from_file_location("site_db", db.ROOT / "scripts" / "site_db.py")
site_db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site_db)


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


def test_site_db(tmp_path):
    canon = _canon(tmp_path / "canon.db")
    site_db.build(tmp_path / "canon.db", tmp_path / "site.db")
    site = sqlite3.connect(tmp_path / "site.db")
    # A fighter's fights from either side; an event's fights in card order.
    assert site.execute("SELECT fight_id FROM site_fights WHERE ? IN (a_id, b_id) ORDER BY date",
                        ("a" * 16,)).fetchall() == [("f1",), ("f3",)]
    assert site.execute("SELECT fight_id, division, has_stats FROM site_fights WHERE event_id = 'e1' "
                        "ORDER BY card").fetchall() == [("f2", "Lightweight", 0), ("f1", "Featherweight", 1)]
    # Search by any word of any name, accents dropped.
    assert site.execute("SELECT DISTINCT fighter_id, n, wins FROM site_names WHERE token IN ('jose', 'junior')"
                        ).fetchall() == [("a" * 16, 2, 31)]
    assert site.execute("SELECT event_id FROM site_events LIMIT 1").fetchone() == ("e2",)  # newest first
    assert site.execute("SELECT events, promotions FROM site_stats").fetchone() == (
        canon.execute("SELECT COUNT(*), COUNT(DISTINCT promotion) FROM events").fetchone())
    # Leaderboards: totals and the pre-computed opening view.
    assert site.execute("SELECT n, w, ko, subw FROM site_results WHERE promotion = 0 AND division = 0 "
                        "AND fighter_id = ?", ("a" * 16,)).fetchone() == (2, 2, 1, 1)
    assert site.execute("SELECT fighter_id, val FROM site_top WHERE board = 'results' AND stat = 'wins' "
                        "AND division = 0 ORDER BY rank").fetchall() == [("a" * 16, 2), ("b" * 16, 1), ("c" * 16, 0)]
    assert site.execute("SELECT fighter_id, val FROM site_top WHERE board = 'ufcstats' AND stat = 'sig'"
                        ).fetchall() == [("a" * 16, 30), ("a" * 16, 30)]  # all divisions, and Featherweight
    boards = json.loads(site.execute("SELECT json FROM site_boards").fetchone()[0])
    assert boards["ufcstats"]["stats"]["sigacc"]["where"] == "sa >= 100"
    assert boards["ufcstats"]["source"] == ["round_stats", "ufcstats"] and boards["results"]["source"] is None


spec_json = importlib.util.spec_from_file_location("site_json", db.ROOT / "scripts" / "site_json.py")
site_json = importlib.util.module_from_spec(spec_json)
spec_json.loader.exec_module(site_json)


def test_site_json(tmp_path, monkeypatch):
    _canon(tmp_path / "canon.db")
    site_db.build(tmp_path / "canon.db", tmp_path / "site.db")
    monkeypatch.setattr(site_json, "SEARCH_CAP", 1)  # split every prefix with two matches
    monkeypatch.setattr(site_json, "SEARCH_TOP", 1)
    monkeypatch.setattr(site_json, "SEARCH_EXACT", 1)
    site_json.build(tmp_path / "site.db", tmp_path / "json")
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


def test_export_parquet(tmp_path):
    duckdb = __import__("pytest").importorskip("duckdb")
    spec_pq = importlib.util.spec_from_file_location("export_parquet", db.ROOT / "scripts" / "export_parquet.py")
    export_parquet = importlib.util.module_from_spec(spec_pq)
    spec_pq.loader.exec_module(export_parquet)
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
