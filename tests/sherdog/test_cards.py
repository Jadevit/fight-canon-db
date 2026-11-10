import sqlite3

from canon_db import claims, db, ids
from canon_db.sources.sherdog import cards
from canon_db.sources.sherdog.linking import State
from canon_db.sources.sherdog.write import write as write_pages


def _db():
    """Alan Able (1, linked to Sherdog 100) and Ben Baker (2, not linked) fought at UFC 1:
    UFC Stats rows, with its claims."""
    conn = sqlite3.connect(":memory:")
    db.create(conn)
    db.sync_promotions(conn)
    conn.executemany("INSERT INTO fighters (fighter_id, name, height_in) VALUES (?,?,?)",
                     [(1, "Alan Able", 70), (2, "Ben Baker", None)])
    conn.executemany("INSERT INTO fighter_aliases (fighter_id, source, site, source_id, name) SELECT ?1, ?2, (SELECT sites FROM sources WHERE source = ?2), ?3, ?4",
                     [(1, "ufc", "a" * 16, "Alan Able"), (1, "sherdog", "100", "Alan Able"),
                      (2, "ufc", "b" * 16, "Ben Baker")])
    conn.execute("INSERT INTO events (event_id, name, date, promotion) VALUES (1, 'UFC 1', '2020-01-02', 'UFC')")
    conn.execute("INSERT INTO event_aliases (event_id, source, site, source_id, name) VALUES (1, 'ufc', 'ufcstats.com', 'e1', NULL)")
    conn.execute("INSERT INTO fights (fight_id, event_id) VALUES (1, 1)")
    conn.execute("INSERT INTO fight_aliases (fight_id, source, site, source_id, name) VALUES (1, 'ufc', 'ufcstats.com', 'f1', NULL)")
    conn.executemany("INSERT INTO fight_participants VALUES (1, ?, ?, ?)", [(1, 0, "W"), (2, 1, "L")])
    claims.claim(conn, "fighter", 1, "ufc", "a" * 16, dict(name="Alan Able", height_in=70))
    claims.claim(conn, "fighter", 2, "ufc", "b" * 16, dict(name="Ben Baker"))
    claims.claim(conn, "event", 1, "ufc", "e1", dict(name="UFC 1", date="2020-01-02", promotion="UFC"))
    claims.claim(conn, "fight", 1, "ufc", "f1", dict(event_id=1))
    claims.sides(conn, 1, "ufc", "f1", [dict(fighter_id=1, result="W", corner=0), dict(fighter_id=2, result="L", corner=1)])
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


def test_a_bout_against_someone_else_the_next_day_is_not_our_fight():
    conn = _db()
    counts = write(conn, [_bout(902, "2020-01-03", ("Alan Able", "100"), ("Ola Other", "600"))])
    assert counts["new fights"] == 1 and counts["fights a promotion's site has"] == 0
    assert ids.find(conn, "event", "sherdog", "902") != 1  # UFC 1 keeps its own card


def test_an_opponent_linked_to_someone_else_is_not_ours_however_alike_the_names():
    conn = _db()
    write(conn, [_bout(901, "2019-05-05", ("Carl Cole", "300"), ("Dan Dent", "400"))])
    # Ben Baker's only fight within a day, against a linked fighter who shares a name word
    conn.execute("INSERT INTO fighter_aliases (fighter_id, source, site, source_id, name) "
                 "VALUES (2, 'sherdog', 'sherdog.com', '200', 'Ben Baker')")
    counts = write(conn, [_bout(904, "2020-01-03", ("Ben Baker", "200"), ("Abe Baker", "300"))])
    assert counts["fighters merged"] == 0 and counts["new fights"] == 1
    assert ids.find(conn, "fighter", "sherdog", "300") != 2


def test_a_tournament_opponent_with_a_like_name_is_not_one_already_on_the_card():
    conn = _db()
    conn.execute("INSERT INTO fighter_aliases (fighter_id, source, site, source_id, name) "
                 "VALUES (2, 'sherdog', 'sherdog.com', '200', 'Ben Baker')")
    rows = [_bout(900, "2020-01-02", ("Alan Able", "100"), ("Ben Baker", "200")),
            _bout(900, "2020-01-02", ("Alan Able", "100"), ("Ben Barker", "500"))]  # a tournament night
    counts = write(conn, rows)
    assert counts["fights a promotion's site has"] == 1 and counts["new fights"] == 1
    assert ids.find(conn, "fighter", "sherdog", "500") not in (None, 2)


def test_a_state_older_than_the_database_merges_nothing_gone():
    conn = _db()
    st = State(conn)
    st.event_is("901", 1)
    st.event_is("901", 99)  # an event since merged away or dropped
    cards.write(conn, st, [])
    assert conn.execute("SELECT count(*) FROM events").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM retired_ids").fetchone() == (0,)


def test_events_found_to_be_one_merge():
    conn = _db()
    write(conn, [_bout(901, "2019-05-05", ("Carl Cole", "300"), ("Alan Able", "100"))])
    event = ids.find(conn, "event", "sherdog", "901")
    st = State(conn)
    st.event_is("901", 1)  # a fight shows Sherdog event 901 is UFC 1
    cards.write(conn, st, [])
    assert ids.find(conn, "event", "sherdog", "901") == 1
    assert conn.execute("SELECT event_id FROM event_cards").fetchall() == [(1,)]
    assert conn.execute("SELECT kind, old_id, new_id FROM retired_ids WHERE reason = 'merged'").fetchall() == [("event", event, 1)]
    write(conn, [_bout(905, "2019-06-06", ("Carl Cole", "300"), ("Dan Dent", "400"))])
    ids.delete_fight(conn, ids.find(conn, "fight", "sherdog", "905:300-400"), "test")
    ids.drop_empty(conn)
    assert conn.execute("SELECT event_id FROM event_cards").fetchall() == [(1,)]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_bout_types():
    from canon_db.sources.sherdog.write import default_pro, mark_bout_types
    conn = _db()
    write(conn, [_bout(901, "2019-05-05", ("Carl Cole", "300"), ("Alan Able", "100")),
                 _bout(902, "2019-06-06", ("Carl Cole", "300"), ("Dan Dent", "400"))])
    default_pro(conn)  # card fights stay unchecked
    assert conn.execute("SELECT count(*) FROM fights WHERE bout_type IS NULL").fetchone() == (1 + 2,)  # UFC 1 too
    page = dict(fights=[dict(event_id="901", opponent_id="100")],
                exhibition=[dict(event_id="902", opponent_id="400")], amateur=[])
    assert mark_bout_types(conn, {"300": page}) == {"901:100-300", "902:300-400"}
    claims.current(conn)
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
    from canon_db.sources.sherdog.write import drop_non_pro, mark_bout_types
    conn = _db()
    write(conn, [_bout(901, "2019-05-05", ("Carl Cole", "300"), ("Alan Able", "100")),
                 _bout(902, "2019-06-06", ("Carl Cole", "300"), ("Dan Dent", "400")),
                 _bout(903, "2019-07-07", ("Carl Cole", "300"), ("Eve East", "500"))])
    amateur = ids.find(conn, "fight", "sherdog", "902:300-400")
    dan = ids.find(conn, "fighter", "sherdog", "400")
    conn.execute("INSERT INTO event_aliases (event_id, source, site, source_id, name) VALUES (?, 'sofascore', 'sofascore.com', '77', NULL)", (ids.find(conn, "event", "sherdog", "902"),))
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
    # Reading the cards again doesn't bring them back, and the cards stay complete.
    assert sorted(conn.execute("SELECT source_id, bout_type FROM non_pro_bouts")) == [
        ("902:300-400", "amateur"), ("903:300-500", "exhibition")]
    counts = write(conn, [_bout(902, "2019-06-06", ("Carl Cole", "300"), ("Dan Dent", "400")),
                          _bout(903, "2019-07-07", ("Carl Cole", "300"), ("Eve East", "500"))])
    assert counts["non-pro skipped"] == 2 and not counts["new fights"]
    assert not ids.find(conn, "fight", "sherdog", "902:300-400")
    # Until Sherdog lists one as pro.
    mark_bout_types(conn, {"300": dict(fights=[dict(event_id="903", opponent_id="500")], exhibition=[], amateur=[])})
    assert write(conn, [_bout(903, "2019-07-07", ("Carl Cole", "300"), ("Eve East", "500"))])["new fights"] == 1


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


def test_a_fighter_page_only_fills_the_blanks_of_a_carded_fight():
    conn = _db()
    write(conn, [_bout(901, "2019-05-05", ("Carl Cole", "300"), ("Dan Dent", "400"), method="TKO (Punches)",
                       referee=None)])
    fight = ids.find(conn, "fight", "sherdog", "901:300-400")
    before = conn.execute("SELECT side, fighter_id, result FROM participant_claims ORDER BY side").fetchall()
    st = State(conn)
    carl = ids.find(conn, "fighter", "sherdog", "300")
    page = dict(name="Carl Cole", fights=[dict(result="L", opponent="Dan Dent", opponent_id="400", event_id="901",
                                               event="CW 901 - X", date="2019-05-05", title=False,
                                               method="Submission (Armbar)", referee="Herb", round="2", time="3:00")])
    write_pages(conn, st, {carl: page})
    claims.resolve(conn)
    assert conn.execute("SELECT method, end_round, referee FROM fights WHERE fight_id = ?", (fight,)).fetchone() == (
        "TKO (Punches)", 2, "Herb")  # the card's values stand; the page fills the blank referee
    assert conn.execute("SELECT side, fighter_id, result FROM participant_claims ORDER BY side").fetchall() == before
