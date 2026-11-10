import json
import sqlite3

from canon_db import claims, db, ids


def _conn():
    conn = sqlite3.connect(":memory:")
    db.create(conn)
    conn.execute("INSERT INTO promotions VALUES ('UFC', NULL, 'full')")
    return conn


def _bout(conn, a="Ann Avery", b="Bea Bell"):
    """An event, a fight and two fighters, with UFC Stats' and Sherdog's aliases."""
    fa, fb = ids.add(conn, "fighter", {"name": a}), ids.add(conn, "fighter", {"name": b})
    e = ids.add(conn, "event", {"name": "E", "promotion": "UFC"})
    f = ids.add(conn, "fight", {"event_id": e})
    for kind, id_, src, sid in (("fighter", fa, "ufc", "a"), ("fighter", fb, "ufc", "b"), ("fighter", fa, "sherdog", "1"),
                                ("fighter", fb, "sherdog", "2"), ("event", e, "ufc", "e"), ("event", e, "sherdog", "9"),
                                ("fight", f, "ufc", "f"), ("fight", f, "sherdog", "9:1-2")):
        ids.alias(conn, kind, id_, src, sid, a if id_ == fa and kind == "fighter" else None)
    return fa, fb, e, f


def test_a_column_takes_the_best_ranked_value_and_a_blank_falls_through():
    conn = _conn()
    fa, _, e, _ = _bout(conn)
    claims.claim(conn, "fighter", fa, "sherdog", "1", dict(name="Ann Avery", height_in=66, nationality="USA", wins=9))
    claims.claim(conn, "fighter", fa, "ufc", "a", dict(name="Ann  Avery", height_in=67, nationality=None, wins=10))
    claims.claim(conn, "event", e, "sherdog", "9", dict(name="E: Sherdog", date="2020-01-01", promotion="UFC"))
    conn.execute("UPDATE fighters SET stance = 'Orthodox' WHERE fighter_id = ?", (fa,))  # no page says so
    claims.resolve(conn)
    assert conn.execute("SELECT name, height_in, nationality, stance FROM fighters WHERE fighter_id = ?",
                        (fa,)).fetchone() == ("Ann  Avery", 67, "USA", None)
    assert conn.execute("SELECT name, date FROM events").fetchone() == ("E: Sherdog", "2020-01-01")
    before = conn.execute("SELECT * FROM fighters").fetchall()
    claims.resolve(conn)  # nothing depends on what was there
    assert conn.execute("SELECT * FROM fighters").fetchall() == before


def test_records_are_counted_and_a_printed_one_stays_in_its_claim():
    conn = _conn()
    _, fb, _, _ = _bout(conn)
    claims.claim(conn, "fighter", fb, "sherdog", "2", dict(name="Bea Bell", wins=3, losses=1))
    claims.sides(conn, conn.execute("SELECT fight_id FROM fights").fetchone()[0], "sherdog", "9:1-2",
                 [dict(fighter_id=fb, result="W"), dict(fighter_id=fb - 1, result="L")])
    claims.resolve(conn)  # records are counted from the fights, not taken from a page
    assert conn.execute("SELECT wins, losses, draws, no_contests FROM fighters WHERE fighter_id = ?",
                        (fb,)).fetchone() == (1, 0, 0, 0)
    assert conn.execute("SELECT wins FROM fighter_claims").fetchone() == (3,)


def test_claim_modes():
    conn = _conn()
    fa, *_ = _bout(conn)
    claims.claim(conn, "fighter", fa, "sherdog", "1", dict(name="Ann Avery", dob="1990-01-01", height_in=66))
    claims.claim(conn, "fighter", fa, "sherdog", "1", dict(height_in=65, nickname="The A"), mode="fill")  # a card
    assert conn.execute("SELECT name, dob, height_in, nickname FROM fighter_claims").fetchone() == (
        "Ann Avery", "1990-01-01", 65, "The A")
    claims.claim(conn, "fighter", fa, "sherdog", "1", dict(height_in=64, stance="Southpaw"), mode="blanks")
    assert conn.execute("SELECT height_in, stance FROM fighter_claims").fetchone() == (65, "Southpaw")
    claims.claim(conn, "fighter", fa, "sherdog", "1", dict(name="Ann Avery"))  # the page again
    assert conn.execute("SELECT dob, height_in, nickname FROM fighter_claims").fetchone() == (None, None, None)


def test_sides_come_from_one_source_and_disagreements_are_flagged():
    conn = _conn()
    fa, fb, e, f = _bout(conn)
    claims.claim(conn, "fight", f, "sherdog", "9:1-2", dict(event_id=e, method="KO (Punch)", referee="Herb"))
    claims.claim(conn, "fight", f, "ufc", "f", dict(event_id=e, method="KO/TKO", title_fight=None))
    claims.sides(conn, f, "sherdog", "9:1-2", [dict(fighter_id=fb, name="Bea Bell", result="W"),
                                              dict(fighter_id=fa, name="Ann Avery", result="L")])
    claims.sides(conn, f, "ufc", "f", [dict(fighter_id=fb, result="L", corner=0), dict(fighter_id=fa, result="W", corner=1)])
    claims.resolve(conn)
    assert conn.execute("SELECT method, referee, title_fight FROM fights").fetchone() == ("KO/TKO", "Herb", 0)
    assert conn.execute("SELECT fighter_id, corner, result FROM fight_participants ORDER BY corner").fetchall() == [
        (fb, 0, "L"), (fa, 1, "W")]
    flags = conn.execute("SELECT row_key, value FROM review_flags ORDER BY fighter_id").fetchall()
    assert [(json.loads(k)["fighter_id"], v) for k, v in flags] == [(fa, "sherdog: L; ufc: W"), (fb, "sherdog: W; ufc: L")]
    claims.resolve(conn)  # rebuilt, not added to
    assert conn.execute("SELECT count(*) FROM review_flags").fetchone() == (2,)

    # Without UFC Stats' sides, Sherdog's: corners by fighter id. A side not linked doesn't count.
    claims.sides(conn, f, "ufc", "f", [dict(fighter_id=None, name="Ann Avery", result="W"), dict(fighter_id=fb, result="L")])
    claims.resolve(conn)
    assert conn.execute("SELECT fighter_id, corner, result FROM fight_participants ORDER BY corner").fetchall() == [
        (fa, 0, "L"), (fb, 1, "W")]


def test_a_missing_result_doesnt_disagree():
    conn = _conn()
    fa, fb, _, f = _bout(conn)
    ids.alias(conn, "fight", f, "pfl", "p1")
    claims.sides(conn, f, "sherdog", "9:1-2", [dict(fighter_id=fa, result="W"), dict(fighter_id=fb, result="L")])
    claims.sides(conn, f, "pfl", "p1", [dict(fighter_id=fa), dict(fighter_id=fb)])
    claims.resolve(conn)
    assert conn.execute("SELECT count(*) FROM review_flags").fetchone() == (0,)


def test_third_party_never_fills_the_tables():
    conn = _conn()
    fa, *_ = _bout(conn)
    ids.alias(conn, "fighter", fa, "sofascore", "s1", "Ann Avery")
    claims.claim(conn, "fighter", fa, "sofascore", "s1", dict(name="A. Avery", height_in=70))
    claims.resolve(conn)
    assert conn.execute("SELECT name, height_in FROM fighters WHERE fighter_id = ?", (fa,)).fetchone() == ("Ann Avery", None)


def test_merges_move_claims_and_a_dropped_claim_goes():
    conn = _conn()
    fa, fb, e, f = _bout(conn)
    twin = ids.add(conn, "fighter", {"name": "Ann Avery"})
    ids.alias(conn, "fighter", twin, "sherdog", "11", "Ann Avery")
    claims.claim(conn, "fighter", twin, "sherdog", "11", dict(name="Ann Avery", dob="1990-01-01"))
    e2 = ids.add(conn, "event", {"name": "E", "promotion": "UFC"})
    claims.claim(conn, "fight", f, "sherdog", "9:1-2", dict(event_id=e2))
    claims.sides(conn, f, "sherdog", "9:1-2", [dict(fighter_id=twin), dict(fighter_id=fb)])
    ids.merge(conn, "fighter", fa, twin)
    ids.merge(conn, "event", e, e2)
    assert conn.execute("SELECT fighter_id FROM fighter_claims").fetchall() == [(fa,)]
    assert conn.execute("SELECT fighter_id FROM participant_claims WHERE side = 0").fetchone() == (fa,)
    assert conn.execute("SELECT event_id FROM fight_claims").fetchone() == (e,)
    claims.claim(conn, "fight", f, "ufc", "f", dict(event_id=e))
    ids.drop_claim(conn, f, "sherdog")
    assert conn.execute("SELECT source FROM fight_claims UNION ALL SELECT source FROM participant_claims").fetchall() == [
        ("ufc",)]
    ids.drop_claim(conn, f, "ufc")  # no source left: the fight and its claims go
    assert conn.execute("SELECT count(*) FROM fight_claims").fetchone() == (0,)


def test_sex_from_ufc_stats_divisions_after_the_claims():
    conn = _conn()
    fa, fb, e, f = _bout(conn)
    claims.claim(conn, "fight", f, "ufc", "f", dict(event_id=e, weight_class="Women's Strawweight Bout"))
    claims.sides(conn, f, "ufc", "f", [dict(fighter_id=fa, result="W", corner=0), dict(fighter_id=fb, result="L", corner=1)])
    claims.claim(conn, "fighter", fa, "ufc", "a", dict(name="Ann Avery"))
    claims.claim(conn, "fighter", fb, "ufc", "b", dict(name="Bea Bell"))
    claims.resolve(conn)
    assert conn.execute("SELECT sex FROM fighters ORDER BY fighter_id").fetchall() == [("F",), ("F",)]


def test_sex_from_divisions_cases():
    conn = sqlite3.connect(":memory:")
    db.create(conn)
    db.sync_promotions(conn)
    conn.execute("INSERT INTO events (event_id, name, date, promotion) VALUES (1, 'UFC 1', '2020-01-01', 'UFC')")
    conn.executemany("INSERT INTO fighters (fighter_id, name) VALUES (?, ?)", [(i, x) for i, x in enumerate("abcdef", 1)])
    for fid, wc, pair in ((1, "Women's Strawweight Bout", (1, 2)), (2, "Lightweight Bout", (3, 4)),
                          (3, "Catch Weight Bout", (5, 6)), (4, "Bantamweight Bout", (1, 3))):
        conn.execute("INSERT INTO fights (fight_id, event_id, weight_class) VALUES (?, 1, ?)", (fid, wc))
        conn.execute("INSERT INTO fight_aliases (fight_id, source, site, source_id, name) VALUES (?, 'ufc', 'ufcstats.com', ?, NULL)", (fid, f"f{fid}"))
        conn.executemany("INSERT INTO fight_participants VALUES (?, ?, ?, 'W')", [(fid, p, i) for i, p in enumerate(pair)])
    claims.sex_from_divisions(conn)
    # A women's bout decides first; catch weight says nothing.
    assert dict(conn.execute("SELECT name, sex FROM fighters")) == dict(a="F", b="F", c="M", d="M", e=None, f=None)


def test_birth_dates_that_disagree_are_flagged_and_oktagon_ranks_below_sherdog():
    conn = _conn()
    fa, *_ = _bout(conn)
    ids.alias(conn, "fighter", fa, "oktagon", "o1", "Ann Avery")
    claims.claim(conn, "fighter", fa, "sherdog", "1", dict(name="Ann Avery", dob="1990-05-17"))
    claims.claim(conn, "fighter", fa, "oktagon", "o1", dict(dob="1990-01-01", sex="F"))
    claims.claim(conn, "fighter", fa, "legacy", str(fa), dict(dob="1990-05-18"), site_="")  # not a source's word
    claims.resolve(conn)
    assert conn.execute("SELECT dob, sex FROM fighters WHERE fighter_id = ?", (fa,)).fetchone() == ("1990-05-17", "F")
    assert conn.execute("SELECT check_name, value FROM review_flags").fetchall() == [
        ("birth dates disagree", "oktagon: 1990-01-01; sherdog: 1990-05-17")]
