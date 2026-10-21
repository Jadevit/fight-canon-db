import sqlite3

from canon_db import db
from canon_db.sources.sherdog import cards
from canon_db.sources.sherdog.load import State

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


def _bout(event, day, f1, f2, **kw):
    """A bout as card_bouts() makes it from an event page."""
    (n1, s1), (n2, s2) = f1, f2
    b = dict(event=str(event), event_name=f"CW {event} - X", date=day, location="Leeds", label="CWFC",
             via="event_page", fetched_at="2026-01-01", weight_class="Lightweight", method="TKO (Punches)",
             referee="Ref", round=2, time="3:10", title=False, placed=True,
             sides=[dict(sid=sid, name=name, result=res, nickname=None, nationality="England", dob="1990-01-01",
                         height_in=72, weight_lbs=155) for name, sid, res in ((n1, s1, "W"), (n2, s2, "L"))])
    b.update(kw)
    return b


def write(conn, bouts):
    return cards.write(conn, State(conn), bouts)


def test_load_cards():
    conn = _db()
    rows = [_bout(900, "2020-01-01", ("Alan Able", "100"), ("Ben Baker", "200")),   # UFC Stats has it
            _bout(901, "2019-05-05", ("Carl Cole", "300"), ("Alan Able", "100"))]   # regional
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
        ("e1", "event_page", 1), ("sherdog:901", "event_page", 1)]


def test_event_page_replaces_sherdog_fight():
    conn = _db()
    conn.execute("INSERT INTO events (event_id, name, date, source) VALUES ('sherdog:901', 'CW 901', "
                 "'2019-05-05', 'sherdog')")
    conn.execute("INSERT INTO fighters (fighter_id, name, source) VALUES ('sherdog:300', 'Carl Cole', 'sherdog')")
    conn.execute("INSERT INTO fights (fight_id, event_id, method, source) VALUES "
                 "('sherdog:901:100-300', 'sherdog:901', 'No Contest (Overturned)', 'sherdog')")
    conn.executemany("INSERT INTO fight_participants VALUES ('sherdog:901:100-300', ?, ?, 'NC')",
                     [("a" * 16, 0), ("sherdog:300", 1)])
    write(conn, [_bout(901, "2019-05-05", ("Carl Cole", "300"), ("Alan Able", "100"))])
    assert conn.execute("SELECT method, weight_class FROM fights WHERE fight_id = 'sherdog:901:100-300'"
                        ).fetchone() == ("TKO (Punches)", "Lightweight")
    assert sorted(conn.execute("SELECT fighter_id, result FROM fight_participants "
                               "WHERE fight_id = 'sherdog:901:100-300'")) == [("a" * 16, "L"), ("sherdog:300", "W")]


def test_crowded_card_needs_names():
    conn = _db()
    rows = [_bout(900, "2020-01-02", ("Alan Able", "100"), ("Ben Baker", "200")),
            _bout(900, "2020-01-02", ("Alan Able", "100"), ("Zed Zane", "500"))]  # a tournament night
    counts = write(conn, rows)
    assert counts["bouts UFC Stats has"] == 1 and counts["new fights"] == 1
    assert conn.execute("SELECT fighter_id FROM fighter_aliases WHERE source_id = '500'").fetchone() == (
        "sherdog:500",)


def test_card_follows_its_event():
    from canon_db.sources.sherdog.load import drop_empty_events, move_event
    conn = _db()
    write(conn, [_bout(901, "2019-05-05", ("Carl Cole", "300"), ("Alan Able", "100"))])
    move_event(conn, "sherdog:901", "e1")
    assert conn.execute("SELECT event_id FROM event_cards").fetchall() == [("e1",)]
    write(conn, [_bout(905, "2019-06-06", ("Carl Cole", "300"), ("Dan Dent", "400"))])
    conn.execute("DELETE FROM fight_participants WHERE fight_id LIKE 'sherdog:905:%'")
    conn.execute("DELETE FROM fights WHERE event_id = 'sherdog:905'")
    drop_empty_events(conn)
    assert conn.execute("SELECT event_id FROM event_cards").fetchall() == [("e1",)]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_bout_types():
    from canon_db.sources.sherdog.load import default_pro, mark_bout_types
    conn = _db()
    write(conn, [_bout(901, "2019-05-05", ("Carl Cole", "300"), ("Alan Able", "100")),
                 _bout(902, "2019-06-06", ("Carl Cole", "300"), ("Dan Dent", "400"))])
    default_pro(conn)  # card fights stay unchecked
    assert conn.execute("SELECT count(*) FROM fights WHERE bout_type IS NULL").fetchone() == (1 + 2,)  # f1 too
    page = dict(fights=[dict(event_id="901", opponent_id="100")],
                exhibition=[dict(event_id="902", opponent_id="400")], amateur=[])
    assert mark_bout_types(conn, {"300": page}) == {"sherdog:901:100-300", "sherdog:902:300-400"}
    assert dict(conn.execute("SELECT fight_id, bout_type FROM fights WHERE source = 'sherdog'")) == {
        "sherdog:901:100-300": "pro", "sherdog:902:300-400": "exhibition"}


def test_bout_against_oneself_is_skipped():
    conn = _db()
    counts = write(conn, [_bout(901, "2019-05-05", ("Carl Cole", "300"), ("Carl Cole", "300")),
                          _bout(901, "2019-05-05", ("Dan Dent", "400"), ("Eve East", "500"))])
    assert counts["bouts not written"] == 1 and counts["new fights"] == 1
    assert conn.execute("SELECT complete FROM event_cards").fetchone() == (0,)


def test_non_pro_fights_are_dropped():
    from canon_db.sources.sherdog.load import drop_non_pro, mark_bout_types
    conn = _db()
    write(conn, [_bout(901, "2019-05-05", ("Carl Cole", "300"), ("Alan Able", "100")),
                 _bout(902, "2019-06-06", ("Carl Cole", "300"), ("Dan Dent", "400")),
                 _bout(903, "2019-07-07", ("Carl Cole", "300"), ("Eve East", "500"))])
    conn.execute("INSERT INTO event_aliases VALUES ('sherdog:902', 'sofascore', '77')")
    conn.execute("INSERT INTO round_stats (fight_id, fighter_id, round, source) "
                 "VALUES ('sherdog:902:300-400', 'sherdog:400', 1, 'sofascore')")
    page = dict(fights=[dict(event_id="901", opponent_id="100")],
                exhibition=[dict(event_id="903", opponent_id="500")],
                amateur=[dict(event_id="902", opponent_id="400")])
    mark_bout_types(conn, {"300": page})
    assert drop_non_pro(conn) == 2
    assert conn.execute("SELECT fight_id FROM fights WHERE source = 'sherdog'").fetchall() == [
        ("sherdog:901:100-300",)]
    # Their events and the fighters who had nothing else go with them; Carl Cole stays.
    assert conn.execute("SELECT event_id FROM events WHERE source = 'sherdog'").fetchall() == [("sherdog:901",)]
    assert conn.execute("SELECT fighter_id FROM fighters WHERE source = 'sherdog'").fetchall() == [
        ("sherdog:300",)]
    assert conn.execute("SELECT count(*) FROM round_stats").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM event_aliases WHERE source = 'sofascore'").fetchone() == (0,)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_event_pages_due():
    seen = {"1": ("2026-01-01", "2026-03-01"),   # read long after: settled
            "2": ("2026-01-01", "2026-01-05"),   # read before its results settled
            "3": ("2026-12-01", "2026-01-05")}   # not held yet
    due = [e for e in ("1", "2", "3", "4") if cards.due(seen, e, None, "2026-02-01", 21)]
    assert due == ["2", "4"]
    assert cards.due(seen, "3", None, "2026-12-02", 21)
    conn = _db()
    write(conn, [_bout(901, "2019-05-05", ("Carl Cole", "300"), ("Alan Able", "100"))])
    assert "901" in cards.pages_read(conn)  # cards read before event_pages count as read
