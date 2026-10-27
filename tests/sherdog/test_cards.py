import sqlite3

from canon_db import db, ids
from canon_db.sources.sherdog import cards
from canon_db.sources.sherdog.load import State


def _db():
    """Alan Able (1, linked to Sherdog 100) and Ben Baker (2, not linked) fought at UFC 1:
    UFC Stats rows."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA.read_text())
    db.sync_promotions(conn)
    conn.executemany("INSERT INTO fighters (fighter_id, name, height_in) VALUES (?,?,?)",
                     [(1, "Alan Able", 70), (2, "Ben Baker", None)])
    conn.executemany("INSERT INTO fighter_aliases VALUES (?,?,?,?)",
                     [(1, "ufcstats", "a" * 16, "Alan Able"), (1, "sherdog", "100", "Alan Able"),
                      (2, "ufcstats", "b" * 16, "Ben Baker")])
    conn.execute("INSERT INTO events (event_id, name, date) VALUES (1, 'UFC 1', '2020-01-02')")
    conn.execute("INSERT INTO event_aliases VALUES (1, 'ufcstats', 'e1')")
    conn.execute("INSERT INTO fights (fight_id, event_id) VALUES (1, 1)")
    conn.execute("INSERT INTO fight_aliases VALUES (1, 'ufcstats', 'f1')")
    conn.executemany("INSERT INTO fight_participants VALUES (1, ?, ?, ?)", [(1, 0, "W"), (2, 1, "L")])
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
    assert counts["fights a promotion's site has"] == 1 and counts["new fights"] == 1
    # The shared fight links Ben Baker, gets Sherdog's alias, and makes the Sherdog event ours.
    assert ids.find(conn, "fighter", "sherdog", "200") == 2
    assert ids.find(conn, "event", "sherdog", "900") == 1
    assert ids.find(conn, "fight", "sherdog", "900:100-200") == 1
    # Sherdog only fills the UFC Stats fight's blanks.
    assert conn.execute("SELECT weight_class, method FROM fights WHERE fight_id = 1").fetchone() == (
        "Lightweight", "TKO (Punches)")
    assert conn.execute("SELECT result FROM fight_participants WHERE fight_id = 1 ORDER BY corner").fetchall() == [
        ("W",), ("L",)]
    fight, event, carl = (ids.find(conn, k, "sherdog", s) for k, s in (("fight", "901:100-300"), ("event", "901"),
                                                                      ("fighter", "300")))
    assert conn.execute("SELECT event_id, weight_class, end_round, end_time FROM fights WHERE fight_id = ?",
                        (fight,)).fetchone() == (event, "Lightweight", 2, "3:10")
    assert conn.execute("SELECT promotion, location FROM events WHERE event_id = ?", (event,)).fetchone() == (
        "CWFC", "Leeds")
    assert sorted(conn.execute("SELECT fighter_id, result FROM fight_participants WHERE fight_id = ?",
                               (fight,))) == [(1, "L"), (carl, "W")]
    # Bios fill blank columns: all of a new fighter's, only the blanks of a UFC Stats fighter.
    assert conn.execute("SELECT dob, height_in, weight_lbs, nationality FROM fighters WHERE fighter_id = ?",
                        (carl,)).fetchone() == ("1990-01-01", 72, 155, "England")
    assert conn.execute("SELECT dob, height_in, nationality FROM fighters WHERE fighter_id = 1").fetchone() == (
        "1990-01-01", 70, "England")
    assert sorted(conn.execute("SELECT event_id, via, complete FROM event_cards")) == [
        (1, "event_page", 1), (event, "event_page", 1)]


def test_event_page_replaces_sherdog_values():
    conn = _db()
    write(conn, [_bout(901, "2019-05-05", ("Carl Cole", "300"), ("Alan Able", "100"), method="No Contest")])
    fight = ids.find(conn, "fight", "sherdog", "901:100-300")
    write(conn, [_bout(901, "2019-05-05", ("Carl Cole", "300"), ("Alan Able", "100"))])
    assert conn.execute("SELECT method, weight_class FROM fights WHERE fight_id = ?", (fight,)).fetchone() == (
        "TKO (Punches)", "Lightweight")
    assert ids.find(conn, "fight", "sherdog", "901:100-300") == fight  # same id after a reload
    assert conn.execute("SELECT count(*) FROM fights").fetchone() == (2,)


def test_crowded_card_needs_names():
    conn = _db()
    rows = [_bout(900, "2020-01-02", ("Alan Able", "100"), ("Ben Baker", "200")),
            _bout(900, "2020-01-02", ("Alan Able", "100"), ("Zed Zane", "500"))]  # a tournament night
    counts = write(conn, rows)
    assert counts["fights a promotion's site has"] == 1 and counts["new fights"] == 1
    assert ids.find(conn, "fighter", "sherdog", "500") == 3


def test_events_found_to_be_one_merge():
    conn = _db()
    write(conn, [_bout(901, "2019-05-05", ("Carl Cole", "300"), ("Alan Able", "100"))])
    event = ids.find(conn, "event", "sherdog", "901")
    st = State(conn)
    st.event_is("901", 1)  # a fight shows Sherdog event 901 is UFC 1
    cards.write(conn, st, [])
    assert ids.find(conn, "event", "sherdog", "901") == 1
    assert conn.execute("SELECT event_id FROM event_cards").fetchall() == [(1,)]
    assert conn.execute("SELECT kind, old_id, new_id FROM merged_ids").fetchall() == [("event", event, 1)]
    write(conn, [_bout(905, "2019-06-06", ("Carl Cole", "300"), ("Dan Dent", "400"))])
    ids.delete_fight(conn, ids.find(conn, "fight", "sherdog", "905:300-400"))
    ids.drop_empty(conn)
    assert conn.execute("SELECT event_id FROM event_cards").fetchall() == [(1,)]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_bout_types():
    from canon_db.sources.sherdog.load import default_pro, mark_bout_types
    conn = _db()
    write(conn, [_bout(901, "2019-05-05", ("Carl Cole", "300"), ("Alan Able", "100")),
                 _bout(902, "2019-06-06", ("Carl Cole", "300"), ("Dan Dent", "400"))])
    default_pro(conn)  # card fights stay unchecked
    assert conn.execute("SELECT count(*) FROM fights WHERE bout_type IS NULL").fetchone() == (1 + 2,)  # UFC 1 too
    page = dict(fights=[dict(event_id="901", opponent_id="100")],
                exhibition=[dict(event_id="902", opponent_id="400")], amateur=[])
    assert mark_bout_types(conn, {"300": page}) == {"901:100-300", "902:300-400"}
    assert dict(conn.execute("SELECT a.source_id, f.bout_type FROM fights f JOIN fight_aliases a USING (fight_id) "
                             "WHERE a.source = 'sherdog' AND f.fight_id <> 1")) == {
        "901:100-300": "pro", "902:300-400": "exhibition"}


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
    amateur = ids.find(conn, "fight", "sherdog", "902:300-400")
    dan = ids.find(conn, "fighter", "sherdog", "400")
    conn.execute("INSERT INTO event_aliases VALUES (?, 'sofascore', '77')", (ids.find(conn, "event", "sherdog", "902"),))
    conn.execute("INSERT INTO round_stats (fight_id, fighter_id, round, source) VALUES (?, ?, 1, 'sofascore')",
                 (amateur, dan))
    page = dict(fights=[dict(event_id="901", opponent_id="100")],
                exhibition=[dict(event_id="903", opponent_id="500")],
                amateur=[dict(event_id="902", opponent_id="400")])
    mark_bout_types(conn, {"300": page})
    conn.execute("UPDATE fights SET bout_type = 'exhibition' WHERE fight_id = 1")  # kept: UFC Stats has it
    assert drop_non_pro(conn) == 2
    assert {r[0] for r in conn.execute("SELECT source_id FROM fight_aliases")} == {"f1", "901:100-300"}
    # Their events and the fighters who had nothing else go with them; Carl Cole stays.
    assert {r[0] for r in conn.execute("SELECT source_id FROM event_aliases")} == {"e1", "901"}
    assert ids.find(conn, "fighter", "sherdog", "300") and not ids.find(conn, "fighter", "sherdog", "400")
    assert conn.execute("SELECT count(*) FROM fighters").fetchone() == (3,)
    assert conn.execute("SELECT count(*) FROM round_stats").fetchone() == (0,)
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
