import json
import sqlite3

from canon_db import db
from canon_db.sources.oktagon.load import is_test, load
from canon_db.sources.oktagon.pages import card_fights, fighters, rounds
from canon_db.sources.ufcstats.load import fill_sex


def _fighter(fid, first, last, born=None):
    return dict(id=fid, firstname=first, lastname=last, birthDate=born)


def _hit(who, rnd, kind="significant", target="head", result="landed"):
    return dict(attacker=dict(id=who), matchRound=rnd, matchTime=10, type=kind, target=target, result=result)


MATCH = dict(
    id=7, externalId=70, tournamentName="Oktagon 1", rounds=3,  # the log says 3 rounds; only 2 have anything
    fighter1=_fighter(1, "Alan", "Able", "1990-01-01T00:00:00+01:00"), fighter2=_fighter(2, "Ben", "Baker"),
    hits=[_hit(1, 1), _hit(1, 1, target="legs"), _hit(1, 1, kind="insignificant"), _hit(1, 1, result="defended"),
          _hit(2, 1, target="body"), _hit(1, 2, target=None)],
    takedowns=[dict(attacker=dict(id=2), matchRound=1, result="completed"),
               dict(attacker=dict(id=2), matchRound=2, result="attempted")],
    submissionAttempts=[dict(attacker=dict(id=1), matchRound=2, result="attempted")])


def test_rounds_counts_what_landed():
    rows = {(r["fighter"], r["round"]): r for r in rounds(MATCH)}
    assert sorted(rows) == [("1", 1), ("1", 2), ("2", 1), ("2", 2)]  # no empty third round
    a1 = rows["1", 1]
    assert (a1["sig_str_land"], a1["total_str_land"], a1["head_land"], a1["leg_land"], a1["body_land"]) == (2, 3, 1, 1, 0)
    assert (rows["2", 1]["td_land"], rows["2", 2]["td_land"]) == (1, 0)  # an attempt isn't a takedown
    assert (rows["1", 2]["sub_att"], rows["1", 2]["sig_str_land"], rows["1", 2]["head_land"]) == (1, 1, 0)
    assert rows["2", 2]["total_str_land"] == 0  # fought the round, landed nothing
    assert rounds(dict(MATCH, hits=[])) == []  # nothing logged: not recorded


def test_fighters_and_cards():
    assert fighters(MATCH) == [dict(id="1", name="Alan Able", dob="1990-01-01"), dict(id="2", name="Ben Baker", dob=None)]
    card = [dict(fights=[
        dict(metadata=dict(esportsId=7), weightClass=dict(gender="MALE", weightLbs=155)),
        dict(metadata=dict(esportsId=8), weightClass=dict(gender="MALE", weightLbs=115)),  # could be a women's bout
        dict(legacyId=70, weightClass=dict(gender="FEMALE", weightLbs=115))])]
    assert [f["sex"] for f in card_fights(card)] == ["M", None, "F"]
    assert card_fights(card)[2]["legacy_id"] == 70
    assert is_test("OKTAGON 9999 test") and is_test("Testovací věci") and not is_test("Oktagon 91")


def _db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA.read_text())
    db.sync_promotions(conn)
    conn.executemany("INSERT INTO fighters (fighter_id, name, dob) VALUES (?,?,?)",
                     [(1, "Alan Able", None), (2, "Ben Baker", "1985-05-05")])
    conn.executemany("INSERT INTO fighter_aliases VALUES (?, 'sherdog', ?, ?)",
                     [(1, "100", "Alan Able"), (2, "200", "Ben Baker")])
    conn.execute("INSERT INTO events (event_id, name, date) VALUES (9, 'Oktagon 1', '2020-01-03')")
    conn.execute("INSERT INTO fights (fight_id, event_id) VALUES (1, 9)")
    conn.executemany("INSERT INTO fight_participants VALUES (1, ?, ?, ?)",
                     [(1, 0, "W"), (2, 1, "L")])
    return conn


def _raw(tmp_path, tid, match):
    for folder in ("tournaments", "matches", "fightcards"):
        (tmp_path / folder).mkdir(exist_ok=True)
    listing = dict(id=tid, date="2020-01-02T00:00:00+01:00",
                   matches=[{k: match[k] for k in ("id", "fighter1", "fighter2")}])
    (tmp_path / "tournaments" / f"{tid}.json").write_text(json.dumps(listing), encoding="utf-8")
    (tmp_path / "matches" / f"{match['id']}.json").write_text(json.dumps(match), encoding="utf-8")


def test_load(tmp_path):
    conn = _db()
    _raw(tmp_path, 5, MATCH)
    card = [dict(fights=[dict(metadata=dict(esportsId=7), weightClass=dict(gender="MALE", weightLbs=155))])]
    (tmp_path / "fightcards" / "oktagon-1.json").write_text(json.dumps(card), encoding="utf-8")
    counts = load(conn, tmp_path)
    assert counts["fights with stats"] == 1 and not counts["bouts not matched"]
    assert conn.execute("SELECT fighter_id, round, sig_str_land, total_str_land, sig_str_att, knockdowns, source "
                        "FROM round_stats WHERE round = 1 ORDER BY fighter_id").fetchall() == [
        (1, 1, 2, 3, None, None, "oktagon"), (2, 1, 1, 1, None, None, "oktagon")]
    assert conn.execute("SELECT event_id FROM event_aliases WHERE source = 'oktagon' AND source_id = '5'").fetchone() == (
        9,)
    # A missing birth date is filled, one we have is kept; both get the bout's sex.
    assert conn.execute("SELECT fighter_id, dob, sex FROM fighters ORDER BY fighter_id").fetchall() == [
        (1, "1990-01-01", "M"), (2, "1985-05-05", "M")]
    load(conn, tmp_path)  # loading again replaces, doesn't duplicate
    assert conn.execute("SELECT count(*) FROM round_stats").fetchone() == (4,)


def test_test_tournaments_are_left_out(tmp_path):
    conn = _db()
    _raw(tmp_path, 8, dict(MATCH, tournamentName="OKTAGON 9999 test"))
    assert load(conn, tmp_path)["events read"] == 0
    assert conn.execute("SELECT count(*) FROM round_stats").fetchone() == (0,)


def test_fill_sex_from_ufcstats_divisions():
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA.read_text())
    db.sync_promotions(conn)
    conn.execute("INSERT INTO events (event_id, name, date) VALUES (1, 'UFC 1', '2020-01-01')")
    conn.executemany("INSERT INTO fighters (fighter_id, name) VALUES (?, ?)", [(i, x) for i, x in enumerate("abcdef", 1)])
    for fid, wc, pair in ((1, "Women's Strawweight Bout", (1, 2)), (2, "Lightweight Bout", (3, 4)),
                          (3, "Catch Weight Bout", (5, 6)), (4, "Bantamweight Bout", (1, 3))):
        conn.execute("INSERT INTO fights (fight_id, event_id, weight_class) VALUES (?, 1, ?)", (fid, wc))
        conn.execute("INSERT INTO fight_aliases VALUES (?, 'ufcstats', ?)", (fid, f"f{fid}"))
        conn.executemany("INSERT INTO fight_participants VALUES (?, ?, ?, 'W')", [(fid, p, i) for i, p in enumerate(pair)])
    fill_sex(conn)
    # A women's bout decides first; catch weight says nothing.
    assert dict(conn.execute("SELECT name, sex FROM fighters")) == dict(a="F", b="F", c="M", d="M", e=None, f=None)
