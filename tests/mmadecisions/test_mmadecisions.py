import sqlite3
from pathlib import Path

from canon_db import db
from canon_db.sources.mmadecisions.load import load, ufcstats_judges, valid
from canon_db.sources.mmadecisions.pages import decision, decisions, events

FIXTURES = Path(__file__).parent / "fixtures"


def page(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_year_and_event_pages():
    listed = events(page("year-2024.html"))
    assert len(listed) == 85 and listed[0]["date"] == "2024-12-21" and listed[0]["name"].startswith("Boxing")
    assert {"id": "1517", "path": "event/1517/UFC-on-ESPN-65-Covington-vs-Buckley",
            "name": "UFC on ESPN 65: Covington vs. Buckley", "date": "2024-12-14"} in listed
    found = decisions(page("event-1517.html"))
    assert len(found) == 6 and found[0] == "decision/15168/Daniel-Marcos-vs-Adrian-Yanez"


def test_decision_page():
    d = decision(page("decision-15168.html"))
    assert [f["name"] for f in d["fighters"]] == ["Daniel Marcos", "Adrian Yanez"]
    assert (d["method"], d["date"], d["event"]["id"]) == ("Split Decision", "2024-12-14", "1517")
    # Each judge's card, by fighter in the page's order (the winner first).
    assert [(j["name"], j["totals"]) for j in d["judges"]] == [
        ("Derek Cleary", (30, 27)), ("Eric Colón", (29, 28)), ("Chris Lee", (28, 29))]
    assert d["judges"][2]["rounds"] == [(1, 10, 9), (2, 9, 10), (3, 9, 10)]
    assert all(valid(j) for j in d["judges"])
    # Media scores: in the same order, with whom the writer picked.
    assert len(d["media"]) == 12
    assert (d["media"][0]["writer"], d["media"][0]["outlet"], d["media"][0]["score"], d["media"][0]["pick"]) == (
        "Tyler Treese", "Sherdog.com", (29, 28), 0)
    assert (d["media"][-1]["score"], d["media"][-1]["pick"]) == ((28, 29), 1)


def test_card_checks_and_judge_spelling():
    assert not valid({"rounds": [(1, 10, 9), (2, 10, None)], "totals": (20, 18)})
    assert not valid({"rounds": [(1, 10, 9), (2, 10, 9)], "totals": (20, 17)})
    assert ufcstats_judges("Derek Cleary 30 - 27. Eric Colon 29 - 28. Chris Lee 28 - 29.") == {
        "CLEARY": "Derek Cleary", "COLON": "Eric Colon", "LEE": "Chris Lee"}


def _db(results=("W", "L")):
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA.read_text())
    db.sync_promotions(conn)
    conn.executemany("INSERT INTO fighters (fighter_id, name) VALUES (?, ?)",
                     [(1, "Daniel Marcos"), (2, "Adrian Yanez"), (3, "Carl Cole"), (4, "Dan Dale")])
    conn.executemany("INSERT INTO fighter_aliases VALUES (?, 'sherdog', ?, ?)",
                     [(1, "1", "Daniel Marcos"), (2, "2", "Adrian Yanez")])
    conn.execute("INSERT INTO events (event_id, name, date) VALUES (9, 'UFC on ESPN 65', '2024-12-14')")
    conn.execute("INSERT INTO fights (fight_id, event_id, details) VALUES "
                 "(1, 9, 'Derek Cleary 30 - 27. Eric Colon 29 - 28. Chris Lee 28 - 29.')")
    conn.executemany("INSERT INTO fight_participants VALUES (1, ?, ?, ?)", [(1, 0, results[0]), (2, 1, results[1])])
    return conn


def _raw(tmp_path):
    (tmp_path / "decisions").mkdir()
    (tmp_path / "decisions" / "15168.html").write_text(page("decision-15168.html"), encoding="utf-8")
    return tmp_path


def test_load(tmp_path):
    conn = _db()
    counts = load(conn, _raw(tmp_path))
    assert counts["decisions matched"] == 1 and counts["fights with cards"] == 1
    # Three judges, three rounds, both fighters; names as UFC Stats spells them.
    assert conn.execute("SELECT COUNT(*), COUNT(DISTINCT judge) FROM judge_scores").fetchone() == (18, 3)
    assert conn.execute("SELECT fighter_id, SUM(score) FROM judge_scores WHERE judge = 'Eric Colon' "
                        "GROUP BY 1 ORDER BY 1").fetchall() == [(1, 29), (2, 28)]
    assert conn.execute("SELECT fighter_id, score FROM media_scores WHERE writer = 'Patrick McCorry' "
                        "ORDER BY 1").fetchall() == [(1, 28), (2, 29)]
    assert conn.execute("SELECT COUNT(*) FROM media_scores").fetchone() == (24,)
    assert conn.execute("SELECT event_id FROM event_aliases WHERE source = 'mmadecisions'").fetchone() == (9,)
    load(conn, tmp_path)  # loading again replaces, doesn't duplicate
    assert conn.execute("SELECT COUNT(*) FROM judge_scores").fetchone() == (18,)
    # A run with nothing on disk (as in CI) leaves what's loaded alone.
    load(conn, tmp_path / "empty")
    assert conn.execute("SELECT COUNT(*) FROM judge_scores").fetchone() == (18,)


def test_official_cards_and_disagreeing_results(tmp_path):
    conn = _db()
    conn.execute("INSERT INTO judge_scores (fight_id, fighter_id, round, judge, score, source) "
                 "VALUES (1, 1, 1, 'Derek Cleary', 10, 'ufc.com')")
    counts = load(conn, _raw(tmp_path))
    assert counts["fights with official card images"] == 1
    assert conn.execute("SELECT DISTINCT source FROM judge_scores").fetchall() == [("ufc.com",)]
    assert conn.execute("SELECT COUNT(*) FROM media_scores").fetchone() == (24,)  # media scores still come

    conn = _db(results=("L", "W"))  # our winner is the page's loser: not the same fight
    assert load(conn, tmp_path)["decisions whose result disagrees"] == 1
    assert conn.execute("SELECT COUNT(*) FROM judge_scores").fetchone() == (0,)
