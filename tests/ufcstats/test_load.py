import sqlite3

from canon_db import db, ids
from canon_db.sources.ufcstats.load import Writer


def _db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA.read_text())
    db.sync_promotions(conn)
    return conn


def _fight(fid, a, b, method="KO/TKO", rounds=None):
    """A bout as load_event() gives it: sides are (UFC Stats id, name)."""
    return dict(id=fid, rounds=rounds or {},
                values=dict(weight_class="Lightweight Bout", title_fight=0, scheduled_rounds=3, method=method,
                            end_round=1, end_time="1:00", time_format="3 Rnd (5-5-5)", referee="Herb Dean",
                            details=None, overturned=0, no_contest=0),
                sides=[dict(id=a[0], name=a[1], result="W"), dict(id=b[0], name=b[1], result="L")])


def _event(eid, day, *fights):
    return dict(event=dict(id=eid, name=f"UFC {eid}", date=day, location="Las Vegas"), fights=list(fights))


def _write(conn, ev):
    with conn:
        w = Writer(conn)
        w.event(ev, "UFC")
        ids.merge_duplicates(conn)
        ids.drop_empty(conn)
    return w.counts


AL, BEN, CARL = ("a" * 16, "Alan Able"), ("b" * 16, "Ben Baker"), ("c" * 16, "Carl Cole")
ROUND = dict(round=1, knockdowns=1, sig_str_land=10, sig_str_att=20)


def test_new_event_then_reload_keeps_ids():
    conn = _db()
    ev = _event("e1", "2020-01-01", _fight("f1", AL, BEN, rounds={AL[0]: [ROUND]}), _fight("f2", CARL, BEN))
    counts = _write(conn, ev)
    assert counts["new fights"] == 2 and counts["new fighters"] == 3 and counts["new events"] == 1
    before = conn.execute("SELECT * FROM fight_aliases ORDER BY source_id").fetchall()
    assert before == [(1, "ufcstats", "f1"), (2, "ufcstats", "f2")]
    assert conn.execute("SELECT fight_id, fighter_id, knockdowns, source FROM round_stats").fetchall() == [
        (1, 1, 1, "ufcstats")]
    _write(conn, ev)  # reloading changes no ids
    assert conn.execute("SELECT * FROM fight_aliases ORDER BY source_id").fetchall() == before
    assert conn.execute("SELECT count(*) FROM fighters").fetchone() == (3,)
    # A bout UFC Stats drops from the card goes (nothing else has it).
    _write(conn, _event("e1", "2020-01-01", _fight("f1", AL, BEN)))
    assert conn.execute("SELECT fight_id FROM fights").fetchall() == [(1,)]


def test_bout_attaches_to_a_sherdog_fight():
    """A regional fight Sherdog gave, later on UFC Stats (a fight it tracks on another promotion's
    card): one fighter is known through UFC Stats, the other only through Sherdog."""
    conn = _db()
    _write(conn, _event("e0", "2019-01-01", _fight("f0", AL, CARL)))   # Alan (1) on UFC Stats
    conn.execute("INSERT INTO fighters (fighter_id, name, nationality) VALUES (10, 'Benjamin Baker', 'England')")
    conn.execute("INSERT INTO fighter_aliases VALUES (10, 'sherdog', '200', 'Benjamin Baker')")
    conn.execute("INSERT INTO events (event_id, name, date, promotion) VALUES (10, 'CW 1', '2020-01-01', 'CWFC')")
    conn.execute("INSERT INTO event_aliases VALUES (10, 'sherdog', '900')")
    conn.execute("INSERT INTO fights (fight_id, event_id, method, referee) VALUES (10, 10, 'TKO (Punches)', 'Ref')")
    conn.execute("INSERT INTO fight_aliases VALUES (10, 'sherdog', '900:100-200')")
    conn.executemany("INSERT INTO fight_participants VALUES (10, ?, ?, ?)", [(1, 0, "W"), (10, 1, "L")])
    counts = _write(conn, _event("e5", "2020-01-02", _fight("f5", AL, BEN, rounds={AL[0]: [ROUND]})))
    assert counts["fights other sources had"] == 1 and counts["fighters linked through a fight"] == 1
    # The fight and event keep Sherdog's ids and gain UFC Stats'; UFC Stats' values win.
    assert ids.find(conn, "fight", "ufcstats", "f5") == 10 and ids.find(conn, "event", "ufcstats", "e5") == 10
    assert ids.find(conn, "fighter", "ufcstats", BEN[0]) == 10
    assert conn.execute("SELECT method, referee FROM fights WHERE fight_id = 10").fetchone() == ("KO/TKO", "Herb Dean")
    assert conn.execute("SELECT name FROM events WHERE event_id = 10").fetchone() == ("UFC e5",)
    assert conn.execute("SELECT nationality FROM fighters WHERE fighter_id = 10").fetchone() == ("England",)
    assert conn.execute("SELECT count(*) FROM fights").fetchone() == (2,)
    assert conn.execute("SELECT source FROM round_stats").fetchall() == [("ufcstats",)]


def test_unknown_fighters_get_new_rows():
    """Neither fighter known anywhere: new rows (Sherdog links them later, and the duplicate
    rows it had are merged into the older ids)."""
    conn = _db()
    _write(conn, _event("e1", "2020-01-01", _fight("f1", AL, BEN)))
    assert ids.find(conn, "fight", "ufcstats", "f1") == 1
    conn.execute("INSERT INTO fighters (fighter_id, name) VALUES (5, 'Al Able'), (6, 'Ben Baker')")
    conn.execute("INSERT INTO events (event_id, name, date) VALUES (5, 'UFC 1', '2020-01-01')")
    conn.execute("INSERT INTO fights (fight_id, event_id, method) VALUES (5, 5, 'TKO (Punches)')")
    conn.execute("INSERT INTO fight_aliases VALUES (5, 'sherdog', '1:1-2')")
    conn.executemany("INSERT INTO fight_participants VALUES (5, ?, ?, ?)", [(1, 0, "W"), (2, 1, "L")])
    assert ids.merge_duplicates(conn) == 1  # same two fighters, same day: one fight, the older id
    assert conn.execute("SELECT source, source_id FROM fight_aliases WHERE fight_id = 1 ORDER BY 1").fetchall() == [
        ("sherdog", "1:1-2"), ("ufcstats", "f1")]
    assert conn.execute("SELECT method FROM fights").fetchall() == [("KO/TKO",)]
