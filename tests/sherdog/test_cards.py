import sqlite3

from canon_db import db
from canon_db.sources.sherdog import cards
from canon_db.sources.sherdog.kaggle import bouts
from canon_db.sources.sherdog.load import State

CW = "cage-warriors-mma-dataset"


def _db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA.read_text())
    db.sync_promotions(conn)
    conn.executemany("INSERT INTO fighters (fighter_id, name, height_in) VALUES (?,?,?)",
                     [("a" * 16, "Alan Able", 70), ("b" * 16, "Ben Baker", None)])
    conn.execute("INSERT INTO fighter_aliases VALUES (?, 'sherdog', '100', 'Alan Able')", ("a" * 16,))
    conn.execute("INSERT INTO events (event_id, name, date) VALUES ('e1', 'UFC 1', '2020-01-02')")
    conn.execute("INSERT INTO fights (fight_id, event_id) VALUES ('f1', 'e1')")
    conn.executemany("INSERT INTO fight_participants VALUES ('f1', ?, ?, ?)",
                     [("a" * 16, 0, "W"), ("b" * 16, 1, "L")])
    return conn


def _row(event, day, f1, f2, status="REALIZADO", **kw):
    (n1, s1), (n2, s2) = f1, f2
    r = dict(event_name=f"CW {event} - X", event_date=f"{day} 00:00:00+00:00", event_location="Leeds",
             event_status=status, weight_class="Lightweight", method="TKO (Punches)", referee="Ref",
             round_num="2", time="03:10", fighter_1=n1, fighter_1_result="win", fighter_2=n2,
             fighter_2_result="loss", event_url=f"https://www.sherdog.com/events/CW-{event}-{event}",
             fighter_1_url=f"https://www.sherdog.com/fighter/x-{s1}",
             fighter_2_url=f"https://www.sherdog.com/fighter/x-{s2}", referee_url="")
    for i in "12":
        r.update({f"f{i}_nickname": "", f"f{i}_nationality": "England", f"f{i}_birthplace": "",
                  f"f{i}_birthDate": "1990-01-01", f"f{i}_height_cm": "182.88", f"f{i}_weight_kg": "70.31",
                  f"f{i}_gym": ""})
    r.update(kw)
    return r


def write(conn, rows, fresh=False):
    return cards.write(conn, State(conn), bouts(CW, rows), fresh)


def test_load_cards():
    conn = _db()
    rows = [_row(900, "2020-01-01", ("Alan Able", "100"), ("Ben Baker", "200")),   # UFC Stats has it
            _row(901, "2019-05-05", ("Carl Cole", "300"), ("Alan Able", "100")),   # regional
            _row(902, "2030-01-01", ("Carl Cole", "300"), ("Dan Dent", "400"), status="CANCELADO")]
    counts = write(conn, rows)
    assert counts["bouts UFC Stats has"] == 1 and counts["new fights"] == 1
    # The shared fight links Ben Baker and makes the Sherdog event ours.
    assert conn.execute("SELECT fighter_id FROM fighter_aliases WHERE source_id = '200'").fetchone() == ("b" * 16,)
    assert conn.execute("SELECT event_id FROM event_aliases WHERE source_id = '900'").fetchone() == ("e1",)
    assert conn.execute("SELECT fight_id, event_id, weight_class, end_round, end_time FROM fights "
                        "WHERE source = 'sherdog'").fetchall() == [
        ("sherdog:901:100-300", "sherdog:901", "Lightweight", 2, "3:10")]
    assert conn.execute("SELECT promotion, location FROM events WHERE event_id = 'sherdog:901'").fetchone() == (
        "CWFC", "Leeds")
    assert sorted(conn.execute("SELECT fighter_id, result FROM fight_participants "
                               "WHERE fight_id = 'sherdog:901:100-300'")) == [("a" * 16, "L"), ("sherdog:300", "W")]
    # A new fighter gets the whole bio; a UFC Stats fighter only a missing dob.
    assert conn.execute("SELECT dob, height_in, weight_lbs, nationality FROM fighters "
                        "WHERE fighter_id = 'sherdog:300'").fetchone() == ("1990-01-01", 72, 155, "England")
    assert conn.execute("SELECT dob, height_in, nationality FROM fighters WHERE fighter_id = ?",
                        ("a" * 16,)).fetchone() == ("1990-01-01", 70, None)
    assert conn.execute("SELECT 1 FROM fighters WHERE fighter_id = 'sherdog:400'").fetchone() is None
    assert sorted(conn.execute("SELECT event_id, via, complete FROM event_cards")) == [
        ("e1", f"kaggle:leandroiber/{CW} v8", 1), ("sherdog:901", f"kaggle:leandroiber/{CW} v8", 1)]


def test_existing_sherdog_fight_only_filled_in():
    conn = _db()
    conn.execute("INSERT INTO events (event_id, name, date, source) VALUES ('sherdog:901', 'CW 901', "
                 "'2019-05-05', 'sherdog')")
    conn.execute("INSERT INTO fighters (fighter_id, name, source) VALUES ('sherdog:300', 'Carl Cole', 'sherdog')")
    conn.execute("INSERT INTO fights (fight_id, event_id, method, source) VALUES "
                 "('sherdog:901:100-300', 'sherdog:901', 'No Contest (Overturned)', 'sherdog')")
    conn.executemany("INSERT INTO fight_participants VALUES ('sherdog:901:100-300', ?, ?, 'NC')",
                     [("a" * 16, 0), ("sherdog:300", 1)])
    rows = [_row(901, "2019-05-05", ("Carl Cole", "300"), ("Alan Able", "100"))]
    write(conn, rows)
    assert conn.execute("SELECT method, weight_class FROM fights WHERE fight_id = 'sherdog:901:100-300'"
                        ).fetchone() == ("No Contest (Overturned)", "Lightweight")
    assert {r[0] for r in conn.execute("SELECT result FROM fight_participants "
                                       "WHERE fight_id = 'sherdog:901:100-300'")} == {"NC"}
    # A fresh event page replaces it.
    write(conn, rows, fresh=True)
    assert conn.execute("SELECT method FROM fights WHERE fight_id = 'sherdog:901:100-300'").fetchone() == (
        "TKO (Punches)",)
    assert sorted(conn.execute("SELECT fighter_id, result FROM fight_participants "
                               "WHERE fight_id = 'sherdog:901:100-300'")) == [("a" * 16, "L"), ("sherdog:300", "W")]


def test_crowded_card_needs_names():
    conn = _db()
    rows = [_row(900, "2020-01-02", ("Alan Able", "100"), ("Ben Baker", "200")),
            _row(900, "2020-01-02", ("Alan Able", "100"), ("Zed Zane", "500"))]  # a tournament night
    counts = write(conn, rows)
    assert counts["bouts UFC Stats has"] == 1 and counts["new fights"] == 1
    assert conn.execute("SELECT fighter_id FROM fighter_aliases WHERE source_id = '500'").fetchone() == (
        "sherdog:500",)


def test_card_follows_its_event():
    from canon_db.sources.sherdog.load import drop_empty_events, move_event
    conn = _db()
    write(conn, [_row(901, "2019-05-05", ("Carl Cole", "300"), ("Alan Able", "100"))])
    move_event(conn, "sherdog:901", "e1")
    assert conn.execute("SELECT event_id FROM event_cards").fetchall() == [("e1",)]
    write(conn, [_row(905, "2019-06-06", ("Carl Cole", "300"), ("Dan Dent", "400"))])
    conn.execute("DELETE FROM fight_participants WHERE fight_id LIKE 'sherdog:905:%'")
    conn.execute("DELETE FROM fights WHERE event_id = 'sherdog:905'")
    drop_empty_events(conn)
    assert conn.execute("SELECT event_id FROM event_cards").fetchall() == [("e1",)]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_bout_types():
    from canon_db.sources.sherdog.load import default_pro, mark_bout_types
    conn = _db()
    write(conn, [_row(901, "2019-05-05", ("Carl Cole", "300"), ("Alan Able", "100")),
                 _row(902, "2019-06-06", ("Carl Cole", "300"), ("Dan Dent", "400"))])
    default_pro(conn)  # card fights stay unchecked
    assert conn.execute("SELECT count(*) FROM fights WHERE bout_type IS NULL").fetchone() == (1 + 2,)  # f1 too
    page = dict(fights=[dict(event_id="901", opponent_id="100")],
                exhibition=[dict(event_id="902", opponent_id="400")], amateur=[])
    assert mark_bout_types(conn, {"300": page}) == {"sherdog:901:100-300", "sherdog:902:300-400"}
    assert dict(conn.execute("SELECT fight_id, bout_type FROM fights WHERE source = 'sherdog'")) == {
        "sherdog:901:100-300": "pro", "sherdog:902:300-400": "exhibition"}
