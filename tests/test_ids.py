import sqlite3

from canon_db import db, ids


def _db():
    conn = sqlite3.connect(":memory:")
    db.create(conn)
    db.sync_promotions(conn)
    return conn


def _fighter(conn, name, source=None, sid=None, **cols):
    fid = ids.add(conn, "fighter", dict(name=name, **cols))
    if source:
        ids.alias(conn, "fighter", fid, source, sid or name, name)
    return fid


def _fight(conn, event, a, b, source=None, sid=None, results=("W", "L")):
    fight = ids.add(conn, "fight", dict(event_id=event))
    conn.executemany("INSERT INTO fight_participants (fight_id, fighter_id, corner, result) VALUES (?,?,?,?)",
                     [(fight, a, 0, results[0]), (fight, b, 1, results[1])])
    if source:
        ids.alias(conn, "fight", fight, source, sid or str(fight))
    return fight


def _stats(conn, fight, fighter, source, strikes):
    conn.execute("INSERT INTO round_stats (fight_id, fighter_id, round, sig_str_land, source) VALUES (?,?,1,?,?)",
                 (fight, fighter, strikes, source))


def test_add_alias_find():
    conn = _db()
    a = _fighter(conn, "Ann Avery", "sherdog", "77")
    assert ids.find(conn, "fighter", "sherdog", 77) == a  # source ids are kept as text
    assert ids.find(conn, "fighter", "ufc", "77") is None
    assert ids.aliases(conn, "fighter", "sherdog") == {"77": a}
    e = ids.add(conn, "event", dict(name="E 1", date="2024-01-01", promotion="New League"))
    assert conn.execute("SELECT coverage FROM promotions WHERE promotion = 'New League'").fetchone() == ("partial",)
    ids.alias(conn, "event", e, "sherdog", "5")
    ids.alias(conn, "event", e, "sherdog", "5")  # again: still one
    assert conn.execute("SELECT COUNT(*) FROM event_aliases").fetchone() == (1,)


def test_merge_fighters():
    conn = _db()
    old = _fighter(conn, "Ann Avery", "sherdog", "1", nickname="Sherdog's")
    new = _fighter(conn, "Ann Avery", "ufc", "x", nickname="Official", height_in=70)
    other = _fighter(conn, "Bea Brook")
    e = ids.add(conn, "event", dict(name="E", date="2024-01-01", promotion="UFC"))
    fight = _fight(conn, e, new, other)
    assert ids.merge(conn, "fighter", new, old) == old  # the older id stays
    # The official row's values win; everything that pointed at the other id moves.
    assert conn.execute("SELECT nickname, height_in FROM fighters WHERE fighter_id = ?", (old,)).fetchone() == (
        "Official", 70)
    assert conn.execute("SELECT fighter_id FROM fight_participants WHERE fight_id = ? AND corner = 0",
                        (fight,)).fetchone() == (old,)
    assert ids.aliases(conn, "fighter", "ufc") == {"x": old}
    assert conn.execute("SELECT COUNT(*) FROM fighters WHERE fighter_id = ?", (new,)).fetchone() == (0,)
    # A later merge into the kept id points earlier merges there too.
    newer = _fighter(conn, "A. Avery")
    ids.merge(conn, "fighter", newer, other)
    ids.merge(conn, "fighter", other, old)
    assert sorted(conn.execute("SELECT old_id, new_id FROM retired_ids WHERE reason = 'merged'")) == [(new, old), (other, old), (newer, old)]


def test_merge_fights_keeps_one_full_count():
    conn = _db()
    a, b = _fighter(conn, "Ann Avery"), _fighter(conn, "Bea Brook")
    e = ids.add(conn, "event", dict(name="E", date="2024-01-01", promotion="UFC"))
    sherdogs = _fight(conn, e, b, a, "sherdog", results=("L", "W"))
    official = _fight(conn, e, a, b, "ufc")
    _stats(conn, sherdogs, a, "sofascore", 10)
    _stats(conn, official, a, "ufc", 12)
    assert ids.merge(conn, "fight", sherdogs, official) == sherdogs
    # Corners come from the official row; a full official count stands alone.
    assert conn.execute("SELECT fighter_id, corner FROM fight_participants ORDER BY corner").fetchall() == [(a, 0), (b, 1)]
    assert conn.execute("SELECT source, sig_str_land FROM round_stats").fetchall() == [("ufc", 12)]
    assert {s for (s,) in conn.execute("SELECT source FROM fight_aliases WHERE fight_id = ?", (sherdogs,))} == {
        "sherdog", "ufc"}


def test_merge_fights_keeps_counts_side_by_side():
    conn = _db()
    a, b = _fighter(conn, "Ann Avery"), _fighter(conn, "Bea Brook")
    e = ids.add(conn, "event", dict(name="E", date="2024-01-01", promotion="UFC"))
    x, y = _fight(conn, e, a, b, "sherdog"), _fight(conn, e, a, b, "oktagon")
    _stats(conn, x, a, "sofascore", 10)
    _stats(conn, y, a, "oktagon", 8)
    _stats(conn, y, a, "sofascore", 99)  # both have Sofascore's: the kept fight's stay
    ids.merge(conn, "fight", x, y)
    assert sorted(conn.execute("SELECT source, sig_str_land FROM round_stats")) == [("oktagon", 8), ("sofascore", 10)]


def test_clear_and_drop_claim():
    conn = _db()
    a, b = _fighter(conn, "Ann Avery"), _fighter(conn, "Bea Brook")
    e = ids.add(conn, "event", dict(name="E", date="2024-01-01", promotion="UFC"))
    f = _fight(conn, e, a, b, "sherdog")
    ids.alias(conn, "fight", f, "ufc", "u")
    _stats(conn, f, a, "ufc", 12)
    _stats(conn, f, a, "sofascore", 10)
    ids.clear(conn, "sofascore", [f], ("round_stats",))
    assert conn.execute("SELECT source FROM round_stats").fetchall() == [("ufc",)]
    assert not ids.drop_claim(conn, f, "ufc")  # Sherdog still lists it
    assert conn.execute("SELECT COUNT(*) FROM round_stats").fetchone() == (0,)
    assert ids.drop_claim(conn, f, "sherdog")  # no source left: the fight goes
    assert conn.execute("SELECT COUNT(*) FROM fights").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM fight_participants").fetchone() == (0,)


def test_merge_duplicates_and_drop_empty():
    conn = _db()
    a, b = _fighter(conn, "Ann Avery"), _fighter(conn, "Bea Brook")
    _fighter(conn, "Cara Cole")  # no fights, on no promotion's site: goes
    listed = _fighter(conn, "Dina Dale", "ufc", "d")  # on a promotion's site: kept without fights
    e1 = ids.add(conn, "event", dict(name="E 1", date="2024-01-01", promotion="UFC"))
    e2 = ids.add(conn, "event", dict(name="E 1 (other source)", date="2024-01-02", promotion="UFC"))
    first = _fight(conn, e1, a, b, "sherdog", "s1")
    _fight(conn, e2, b, a, "ufc", "u1", results=("L", "W"))
    # A rematch the same source lists twice is two fights, not one.
    e3 = ids.add(conn, "event", dict(name="E 3", date="2024-06-01", promotion="UFC"))
    _fight(conn, e3, a, b, "sherdog", "s2")
    _fight(conn, e3, a, b, "sherdog", "s3")
    assert ids.merge_duplicates(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM fights").fetchone() == (3,)
    # The merged fight is on the official source's event, so the other one is left empty.
    assert conn.execute("SELECT event_id FROM fights WHERE fight_id = ?", (first,)).fetchone() == (e2,)
    ids.drop_empty(conn)
    assert {r[0] for r in conn.execute("SELECT event_id FROM events")} == {e2, e3}
    assert {r[0] for r in conn.execute("SELECT fighter_id FROM fighters")} == {a, b, listed}


def test_lists_of_tables_from_the_schema():
    # Every table holding fights' rows is one a merge or a deleted fight takes care of.
    with_fights = {t for t, cols in db.COLUMNS.items() if "fight_id" in cols}
    assert with_fights == {"fights", "fight_aliases", "fight_participants", "fight_claims", *ids.FIGHT_ROWS,
                           *ids.REBUILT}
    assert ids.FIGHT_ROWS == ("participant_claims", "round_stats", "smartcage_round_stats", "sofascore_round_stats",
                              "judge_scores", "media_scores", "odds")
    assert {t for t in db.COLUMNS if t.endswith("_claims")} == {*ids.CLAIMS.values(), "participant_claims"}
    assert ids.STATS_TABLES == ("round_stats", "smartcage_round_stats", "sofascore_round_stats")


def test_every_id_that_goes_is_retired():
    conn = _db()
    e = ids.add(conn, "event", dict(name="E", date="2024-01-01", promotion="UFC"))
    a, b, c = (ids.add(conn, "fighter", dict(name=n)) for n in "ABC")
    f = ids.add(conn, "fight", dict(event_id=e))
    conn.executemany("INSERT INTO fight_participants (fight_id, fighter_id, corner, result) VALUES (?,?,?,?)",
                     [(f, a, 0, "W"), (f, b, 1, "L")])
    ids.alias(conn, "fight", f, "sherdog", "1:1-2")
    conn.execute("INSERT INTO review_flags (check_name, table_name, row_key, value, reason, fight_id) "
                 "VALUES ('t', 'fights', '{}', 'x', 'test', ?)", (f,))
    assert ids.merge(conn, "fighter", a, c) == a  # c goes into a
    ids.drop_claim(conn, f, "sherdog")             # no source left: the fight goes
    ids.drop_empty(conn)                           # and then its event and fighters
    assert sorted(conn.execute("SELECT kind, old_id, new_id, reason FROM retired_ids")) == [
        ("event", e, None, "no fights left"), ("fight", f, None, "no source lists it"),
        ("fighter", a, None, "no fights left"), ("fighter", b, None, "no fights left"), ("fighter", c, a, "merged")]
    assert not conn.execute("SELECT 1 FROM review_flags").fetchone()  # a gone row's flags go with it
